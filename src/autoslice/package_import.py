"""Import one externally produced slice package into the free delivery chain.

wsl(ROG-EYE)/Mac 用同一份 repo、同一套 produce，产出的包是完整的；卡点只在
**跨主机导入**：上传凭据、``publication_registry`` 和 upload 读的 state 只在
free。本模块是那条导入链的机械实现（``scripts/import_external_package.py``
是它的 CLI）：

1. :func:`plan_import` —— 解析根、列出要搬的字节、拒绝一切不安全形态；
2. :func:`execute_copy` —— 逐文件 sha256 前后比对，证明传输没损坏；
3. :func:`relocate_package` —— 只投影运行期定位符，冻结证据逐字节不动，
   写下 ``slice-package-relocation.v2`` 事务日志（幂等重入的唯一凭据）；
4. :func:`build_bound_state` —— 纯函数算出 state 后像（picks 行
   ``status=review_ready`` / ``rc=0`` / 封面名称+sha 双绑），调用方持
   ``runner.lock`` 后落盘。

fail-closed：每个拒绝都是一个 :class:`PackageImportError`，带 typed ``code``
和修法提示；没有"尽力而为地继续"这条路。

**血泪教训（2026-08-09 实事故）**：runner 的 tick 把 state 读进内存、跑完（可长
达 90 分钟）才写回。任何不持 ``runner.lock`` 的带外 state 手术都会被陈旧写回整
体抹掉。本模块把 state 写入做成纯函数 + 调用方持锁，锁由
``scripts/import_external_package.py`` 在读—改—写全程持有。
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from src.autoslice.review_package_ass_audit import (
    uniform_host_fallback_declared,
)
from src.autoslice.surface_canon import CHANNEL_PROFILE
from src.autoslice.package_relocation_contract import (
    PUBLISH_PATH_POINTERS,
    RECORD_PATH_POINTERS,
    ROOT_ROLES,
    SPEAKER_PATH_POINTERS,
    JsonPointer,
    PackageRelocationError,
    get_value,
    is_mutable_pointer,
    match_root_role,
    pointer_text,
    reject_unknown_wsl_paths,
    set_value,
    validate_document_root_roles,
    validate_path_root_role,
)


RELOCATION_SCHEMA_VERSION = "slice-package-relocation.v2"
IMPORT_RECEIPT_SCHEMA_VERSION = "external-package-import.v1"
STATE_BINDING_SCHEMA_VERSION = "external-package-state-binding.v1"

# 批级状态白名单必须与 build_lidousha_daily_review_manifest.build 一致：
# manifest builder 拒绝的批级状态在这里就要给出 typed 拒绝，而不是等到第 5 步
# 才崩。`processing` 表示 runner 正在写 picks，永远不可导入。
REVIEWABLE_BATCH_STATUSES = frozenset(
    {
        "review_ready",
        "review_ready_with_failures",
        "review_ready_retry_wait",
        "publication_in_progress",
        "ready_unpublished",
        "ready_unpublished_with_failures",
    }
)

# 收编时被同名新绑定取代的诊断键：它们指向产它那台机上的旧交付，留着只会让
# 后续读者把外部包误当 free 自产件。审计链（manifest builder / package audit）
# 均不消费这些名字（2026-08-09 87finish 已核）。
_SUPERSEDED_PICK_KEYS = frozenset(
    {
        "cover_authority_preflight_error",
        "cover_binding_path",
        "cover_binding_sha256",
        "cover_generation_path",
        "cover_generation_sha256",
        "cover_integrity_status",
        "cover_pending_reason_codes",
        "cover_repair_last_generation_dir",
        "cover_transaction_path",
        "cover_transaction_status",
        "log",
        "pipeline_fingerprint",
        "retry_reason",
    }
)

_PUBLISH_STAGING_LOCAL_KEYS = frozenset({"publish_json_path", "status"})
_PUBLISH_NON_STAGING_KEYS = frozenset(
    {"artifact_hashes", "candidate_id", "schema_version", "video_path"}
)
_DOCUMENT_COMMIT_ORDER = ("speaker", "publish", "record")

_UNIFORM_STEM_SUFFIX = "burned-final-sapphire72"
_SPEAKER_STEM_SUFFIX = "burned-final-speaker"


class PackageImportError(RuntimeError):
    """One typed, fail-closed refusal with an operator-actionable hint."""

    def __init__(self, code: str, detail: str, *, hint: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.hint = hint

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail, "hint": self.hint}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def declared_digest(value: object, *, label: str) -> str:
    text = str(value or "").removeprefix("sha256:")
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PackageImportError(
            "DECLARED_DIGEST_INVALID", f"{label} is not a canonical SHA-256"
        )
    return text


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def require_regular_file(path: Path, *, label: str) -> Path:
    """Reject every symlink component before trusting one regular file."""

    absolute = Path(path).absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise PackageImportError(
                "UNSAFE_PATH_SYMLINK",
                f"{label} path contains a symlink: {cursor}",
            )
    if not _regular_file(absolute):
        raise PackageImportError(
            "SOURCE_FILE_MISSING", f"{label} is missing or not a regular file: {path}"
        )
    return absolute


def require_directory(path: Path, *, label: str) -> Path:
    absolute = Path(path).absolute()
    try:
        mode = os.lstat(absolute).st_mode
    except OSError as exc:
        raise PackageImportError(
            "DIRECTORY_MISSING", f"{label} directory missing: {path}"
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise PackageImportError(
            "UNSAFE_PATH_SYMLINK",
            f"{label} is a symlink or not a directory: {path}",
        )
    return absolute


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.tmp-"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> str:
    payload = json_bytes(value)
    atomic_write_bytes(path, payload)
    return sha256_bytes(payload)


@contextmanager
def exclusive_lock(path: Path, *, label: str) -> Iterator[int]:
    """Hold one advisory exclusive lock for the whole read-mutate-write window."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise PackageImportError(
                "LOCK_UNAVAILABLE", f"{label} could not be locked: {path}"
            ) from exc
        yield handle.fileno()
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


# --------------------------------------------------------------------------
# roots
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RelocationRoots:
    """The three root-role mappings plus the producing-host workspace marker."""

    source_package_root: str
    source_candidate_root: str
    source_repo_root: str
    source_workspace_root: str
    destination_package_root: str
    destination_candidate_root: str
    destination_repo_root: str

    def mappings(self) -> tuple[tuple[str, str], ...]:
        by_role = {
            "package": (self.source_package_root, self.destination_package_root),
            "candidate": (
                self.source_candidate_root,
                self.destination_candidate_root,
            ),
            "repo": (self.source_repo_root, self.destination_repo_root),
        }
        return tuple(by_role[role] for role in ROOT_ROLES)

    def as_dict(self) -> dict[str, str]:
        return {
            "source_package_root": self.source_package_root,
            "source_candidate_root": self.source_candidate_root,
            "source_repo_root": self.source_repo_root,
            "source_workspace_root": self.source_workspace_root,
            "destination_package_root": self.destination_package_root,
            "destination_candidate_root": self.destination_candidate_root,
            "destination_repo_root": self.destination_repo_root,
        }


def _normal_absolute_root(value: object, *, label: str) -> str:
    raw = str(value or "").rstrip("/")
    pure = PurePosixPath(raw)
    if not pure.is_absolute() or ".." in pure.parts or raw in {"", "/"}:
        raise PackageImportError(
            "UNSAFE_RELOCATION_ROOT", f"{label} is not a safe absolute root: {value!r}"
        )
    return raw


def _declared_parent(document: Mapping[str, Any], pointer: JsonPointer,
                     *, expected_name: str, label: str) -> str:
    value = get_value(document, pointer)
    if not isinstance(value, str) or not value.startswith("/"):
        raise PackageImportError(
            "SOURCE_ROOT_UNRESOLVED",
            f"{label} does not declare an absolute path at {pointer_text(pointer)}",
        )
    pure = PurePosixPath(value)
    if pure.name != expected_name:
        raise PackageImportError(
            "SOURCE_ROOT_UNRESOLVED",
            f"{label} at {pointer_text(pointer)} is not named {expected_name}: {value}",
        )
    return _normal_absolute_root(str(pure.parent), label=label)


def derive_source_roots(
    *,
    record: Mapping[str, Any],
    publish: Mapping[str, Any],
    speaker: Mapping[str, Any] | None,
    candidate_id: str,
    destination_package_root: Path,
    destination_repo_root: Path,
    source_repo_root: str | None = None,
    source_workspace_root: str | None = None,
) -> RelocationRoots:
    """Recover the producing host's roots from the package's own declarations.

    Package/candidate roots are unambiguous (the record names package-internal
    artifacts).  The repo root is recovered by matching the declared speaker
    profile against the destination repository; the workspace marker is their
    common ancestor.  Any ambiguity refuses instead of guessing — pass
    ``--source-repo-root`` / ``--source-workspace-root`` to resolve it by hand.
    """

    package_root = _declared_parent(
        record,
        ("subtitle_path",),
        expected_name=f"{candidate_id}.recut.srt",
        label="record.subtitle_path",
    )
    for kind, document, pointer in (
        ("publish", publish, ("cover_generation", "final_cover")),
        ("record", record, ("burned_preview", "path")),
        ("record", record, ("subtitle_ass_path",)),
    ):
        value = get_value(document, pointer)
        if isinstance(value, str) and value.startswith("/"):
            if not value.startswith(package_root + "/"):
                raise PackageImportError(
                    "SOURCE_ROOT_INCONSISTENT",
                    f"{kind}{pointer_text(pointer)} is not under the derived "
                    f"source package root {package_root}: {value}",
                    hint="the package mixes producing-host roots; import refuses "
                    "to guess which one is authoritative",
                )
    candidate_root = _normal_absolute_root(
        str(PurePosixPath(package_root).parent), label="source candidate root"
    )
    if PurePosixPath(package_root).name != "replacement_recuts":
        raise PackageImportError(
            "SOURCE_ROOT_UNRESOLVED",
            "source package root is not a replacement_recuts directory: "
            f"{package_root}",
        )
    if PurePosixPath(candidate_root).name != candidate_id:
        raise PackageImportError(
            "SOURCE_ROOT_INCONSISTENT",
            f"source candidate root {candidate_root} is not named {candidate_id}",
        )

    destination_package = _normal_absolute_root(
        destination_package_root, label="destination package root"
    )
    destination_candidate = _normal_absolute_root(
        str(PurePosixPath(destination_package).parent),
        label="destination candidate root",
    )
    destination_repo = _normal_absolute_root(
        destination_repo_root, label="destination repo root"
    )

    repo_root = (
        _normal_absolute_root(source_repo_root, label="source repo root")
        if source_repo_root
        else _derive_repo_root(
            record=record,
            speaker=speaker,
            local_roots=(package_root, candidate_root, destination_package,
                         destination_candidate),
            destination_repo_root=destination_repo,
        )
    )
    workspace_root = (
        _normal_absolute_root(source_workspace_root, label="source workspace root")
        if source_workspace_root
        else _derive_workspace_root(
            candidate_root,
            repo_root,
            destination_repo_root=destination_repo,
        )
    )
    for label, root, destination in (
        ("source package root", package_root, destination_package),
        ("source candidate root", candidate_root, destination_candidate),
    ):
        if root == destination:
            raise PackageImportError(
                "SOURCE_IS_DESTINATION",
                f"{label} equals its destination ({root}); relocation cannot "
                "disambiguate the two sides",
                hint="stage the external package somewhere else (the source "
                "roots are the ones its own JSON declares, not where you put "
                "the bytes)",
            )
    return RelocationRoots(
        source_package_root=package_root,
        source_candidate_root=candidate_root,
        source_repo_root=repo_root,
        source_workspace_root=workspace_root,
        destination_package_root=destination_package,
        destination_candidate_root=destination_candidate,
        destination_repo_root=destination_repo,
    )


def _repo_role_values(
    record: Mapping[str, Any], speaker: Mapping[str, Any] | None
) -> list[tuple[JsonPointer, str]]:
    """Locators the contract allows to live under a repository root."""

    found: list[tuple[JsonPointer, str]] = []
    for pointer in sorted(SPEAKER_PATH_POINTERS):
        for document, prefix in (
            (speaker, ()),
            (record.get("speaker_finalization"), ("speaker_finalization",)),
        ):
            if not isinstance(document, Mapping):
                continue
            value = get_value(document, pointer)
            if isinstance(value, str) and value.startswith("/"):
                found.append(((*prefix, *pointer), value))
    return [
        (pointer, value)
        for pointer, value in found
        if "repo" in _pointer_root_roles_for_speaker(pointer)
    ]


def _pointer_root_roles_for_speaker(pointer: JsonPointer) -> frozenset[str]:
    from src.autoslice.package_relocation_contract import _SPEAKER_ROOT_ROLES

    key = pointer[1:] if pointer[:1] == ("speaker_finalization",) else pointer
    return _SPEAKER_ROOT_ROLES.get(key, frozenset())


def _derive_repo_root(
    *,
    record: Mapping[str, Any],
    speaker: Mapping[str, Any] | None,
    local_roots: Sequence[str],
    destination_repo_root: str,
) -> str:
    """Decide whether the package needs a repo-side projection at all.

    Real wsl production runs against free as the compute arm, so repo-side
    assets (voiceprint profile, overrides) are already declared with the
    destination's own repository path.  When nothing points at a producing-host
    repository there is nothing to project, and the two sides coincide.  When
    something does, the producing repo root cannot be guessed from a single
    path — refuse and name the pointer.
    """

    unresolved = [
        (pointer, value)
        for pointer, value in _repo_role_values(record, speaker)
        if not any(
            value == root or value.startswith(root.rstrip("/") + "/")
            for root in (*local_roots, destination_repo_root)
        )
    ]
    if not unresolved:
        return destination_repo_root
    raise PackageImportError(
        "SOURCE_REPO_ROOT_UNRESOLVED",
        "repo-role locators point at a producing-host repository: "
        + "; ".join(
            f"{pointer_text(pointer)}={value}" for pointer, value in unresolved[:5]
        ),
        hint="pass --source-repo-root with the producing host's repository root",
    )


def _derive_workspace_root(
    candidate_root: str, repo_root: str, *, destination_repo_root: str
) -> str:
    """The producing host marker used to catch unrelocated paths anywhere.

    Broader is stricter here: the marker only ever causes refusals, so when the
    repo root carries no information (it already equals the destination) the
    candidate root's own user-level prefix is used.  Override it when that is
    too coarse for the host at hand.
    """

    parts = PurePosixPath(candidate_root).parts
    if repo_root != destination_repo_root:
        shared: list[str] = []
        for one, other in zip(parts, PurePosixPath(repo_root).parts):
            if one != other:
                break
            shared.append(one)
        if len(shared) >= 3:
            return _normal_absolute_root(
                str(PurePosixPath(*shared)), label="source workspace root"
            )
    if len(parts) < 3:
        raise PackageImportError(
            "SOURCE_WORKSPACE_ROOT_UNRESOLVED",
            f"candidate root {candidate_root} is too shallow to derive an "
            "external-host marker from",
            hint="pass --source-workspace-root explicitly",
        )
    return _normal_absolute_root(
        str(PurePosixPath(*parts[:3])), label="source workspace root"
    )


# --------------------------------------------------------------------------
# package documents
# --------------------------------------------------------------------------


def load_json_document(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    payload = require_regular_file(path, label=label).read_bytes()
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PackageImportError(
            "PACKAGE_DOCUMENT_INVALID", f"{label} is not valid JSON: {path}"
        ) from exc
    if not isinstance(document, dict):
        raise PackageImportError(
            "PACKAGE_DOCUMENT_INVALID", f"{label} root is not an object: {path}"
        )
    return document, payload


@dataclass(frozen=True)
class PackageDocuments:
    candidate_id: str
    record: dict[str, Any]
    publish: dict[str, Any]
    speaker: dict[str, Any] | None
    record_path: Path
    publish_path: Path
    speaker_path: Path | None
    uniform_fallback: bool

    @property
    def upload_stem(self) -> str:
        suffix = (
            _UNIFORM_STEM_SUFFIX if self.uniform_fallback else _SPEAKER_STEM_SUFFIX
        )
        return f"{self.candidate_id}.recut.{suffix}"


def locate_chat_authority(package_root: Path, candidate_id: str) -> Path:
    """Find the frozen chat authority: package copy first, candidate root next."""

    name = f"{candidate_id}.chat-authority.json"
    for path in (package_root / name, package_root.parent / name):
        if _regular_file(path):
            return path
    raise PackageImportError(
        "CHAT_AUTHORITY_MISSING",
        f"neither {package_root / name} nor {package_root.parent / name} exists",
        hint="the frozen chat authority is required evidence; stage the whole "
        "candidate directory, not only replacement_recuts",
    )


def read_package_documents(
    package_root: Path, candidate_id: str
) -> PackageDocuments:
    """Load the three mutable documents and decide the finalization style.

    The finalization style must be decided by the same self-declaration the
    release audit chain uses (``uniform_host_fallback_declared``), otherwise the
    two chains can name different burned artifacts for one package.  The
    presence of the speaker manifest is cross-checked against it and any
    disagreement refuses.
    """

    record_path = package_root / f"{candidate_id}.record.json"
    publish_path = package_root / f"{candidate_id}.recut.publish.json"
    speaker_path = package_root / f"{candidate_id}.recut.speaker-final.json"
    record, _ = load_json_document(record_path, label="record")
    publish, _ = load_json_document(publish_path, label="publish")
    speaker: dict[str, Any] | None = None
    if _regular_file(speaker_path):
        speaker, _ = load_json_document(speaker_path, label="speaker manifest")
    if str(publish.get("candidate_id") or "") != candidate_id:
        raise PackageImportError(
            "CANDIDATE_MISMATCH",
            f"publish declares candidate_id={publish.get('candidate_id')!r}, "
            f"import was asked for {candidate_id!r}",
        )
    story = record.get("story_contract")
    if isinstance(story, Mapping):
        story_candidate = str(story.get("candidate_id") or "")
        if story_candidate and story_candidate != candidate_id:
            raise PackageImportError(
                "CANDIDATE_MISMATCH",
                "record.story_contract declares "
                f"candidate_id={story_candidate!r}, import was asked for "
                f"{candidate_id!r}",
            )
    chat_authority, _ = load_json_document(
        locate_chat_authority(package_root, candidate_id), label="chat authority"
    )
    uniform_fallback = uniform_host_fallback_declared(record, chat_authority)
    if uniform_fallback != (speaker is None):
        raise PackageImportError(
            "FINALIZATION_STYLE_DISAGREEMENT",
            "the frozen chat authority declares uniform_host="
            f"{uniform_fallback} while the package "
            f"{'has' if speaker is not None else 'lacks'} a speaker manifest",
            hint="the release audit chain and this importer would name different "
            "burned artifacts; do not import until the package is coherent",
        )
    burn = package_root / (
        f"{candidate_id}.recut."
        f"{_UNIFORM_STEM_SUFFIX if uniform_fallback else _SPEAKER_STEM_SUFFIX}.mp4"
    )
    if not _regular_file(burn):
        raise PackageImportError(
            "PACKAGE_INCOMPLETE", f"declared final burn is missing: {burn}"
        )
    return PackageDocuments(
        candidate_id=candidate_id,
        record=record,
        publish=publish,
        speaker=speaker,
        record_path=record_path,
        publish_path=publish_path,
        speaker_path=speaker_path if speaker is not None else None,
        uniform_fallback=uniform_fallback,
    )


def package_lane(record: Mapping[str, Any], publish: Mapping[str, Any]) -> str:
    """Mirror ``build_lidousha_daily_review_manifest._candidate_lane`` exactly.

    A song package whose record omits ``classification`` is still a song — the
    manifest builder decides by the channel's song title prefix.  Disagreeing
    here would let a song reach the talk-only bind and land in ``picks``
    instead of ``songs``.
    """

    if str(record.get("classification") or "").lower() == "song":
        return "song"
    if str(publish.get("title") or "").startswith(
        CHANNEL_PROFILE.song_title_prefix
    ):
        return "song"
    if isinstance(publish.get("lyrics_proof"), Mapping):
        return "song"
    return "talk"


# --------------------------------------------------------------------------
# copy planning + integrity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CopyItem:
    source: Path
    destination: Path
    relative: str
    role: str


@dataclass(frozen=True)
class ImportPlan:
    candidate_id: str
    roots: RelocationRoots
    documents: PackageDocuments
    copies: tuple[CopyItem, ...]
    repo_assets: tuple[str, ...]


def _iter_source_files(root: Path) -> Iterator[Path]:
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        for name in sorted(directories):
            if (current_path / name).is_symlink():
                raise PackageImportError(
                    "UNSAFE_PATH_SYMLINK",
                    f"source package contains a symlinked directory: "
                    f"{current_path / name}",
                )
        for name in sorted(files):
            path = current_path / name
            if path.is_symlink():
                raise PackageImportError(
                    "UNSAFE_PATH_SYMLINK",
                    f"source package contains a symlink: {path}",
                )
            if not _regular_file(path):
                raise PackageImportError(
                    "UNSAFE_SOURCE_FILE",
                    f"source package contains a non-regular file: {path}",
                )
            yield path
        directories[:] = sorted(directories)


def _referenced_locators(
    documents: PackageDocuments,
) -> list[tuple[str, JsonPointer, str]]:
    """Every locator value the relocation contract is allowed to project."""

    found: list[tuple[str, JsonPointer, str]] = []
    for kind, document, pointers in (
        ("record", documents.record, RECORD_PATH_POINTERS),
        ("publish", documents.publish, PUBLISH_PATH_POINTERS),
        (
            "speaker",
            documents.speaker if documents.speaker is not None else {},
            SPEAKER_PATH_POINTERS,
        ),
    ):
        for pointer in sorted(pointers):
            value = get_value(document, pointer)
            if isinstance(value, str) and value.startswith("/"):
                found.append((kind, pointer, value))
    embedded = documents.record.get("speaker_finalization")
    if isinstance(embedded, Mapping):
        for pointer in sorted(SPEAKER_PATH_POINTERS):
            value = get_value(embedded, pointer)
            if isinstance(value, str) and value.startswith("/"):
                found.append(("record", ("speaker_finalization", *pointer), value))
    return found


def plan_import(
    *,
    source_package_dir: Path,
    destination_package_root: Path,
    destination_repo_root: Path,
    candidate_id: str,
    source_repo_root: str | None = None,
    source_workspace_root: str | None = None,
) -> ImportPlan:
    """Resolve roots and enumerate exactly which bytes must move."""

    source_package_dir = require_directory(
        source_package_dir, label="source package"
    )
    documents = read_package_documents(source_package_dir, candidate_id)
    lane = package_lane(documents.record, documents.publish)
    if lane != "talk":
        raise PackageImportError(
            "LANE_NOT_SUPPORTED",
            f"candidate lane is {lane!r}; this importer only covers the talk lane",
            hint="song packages carry a different proof chain (lyrics/alignment); "
            "import them by hand until a song lane is added here",
        )
    roots = derive_source_roots(
        record=documents.record,
        publish=documents.publish,
        speaker=documents.speaker,
        candidate_id=candidate_id,
        destination_package_root=destination_package_root,
        destination_repo_root=destination_repo_root,
        source_repo_root=source_repo_root,
        source_workspace_root=source_workspace_root,
    )
    mappings = roots.mappings()
    for kind, document in (
        ("record", documents.record),
        ("publish", documents.publish),
        ("speaker", documents.speaker),
    ):
        if document is None:
            continue
        try:
            validate_document_root_roles(document, kind=kind, mappings=mappings)
        except PackageRelocationError as exc:
            raise PackageImportError(
                "LOCATOR_ROOT_ROLE_VIOLATION", f"{exc}"
            ) from exc

    destination_package = Path(roots.destination_package_root)
    copies: list[CopyItem] = []
    seen: set[str] = set()
    for path in _iter_source_files(source_package_dir):
        relative = str(path.relative_to(source_package_dir))
        if relative.startswith(".") or "/." in relative:
            # Transaction scratch from an earlier import attempt on the
            # producing host must never travel; the destination writes its own.
            continue
        copies.append(
            CopyItem(
                source=path,
                destination=destination_package / relative,
                relative=relative,
                role="package",
            )
        )
        seen.add(str(destination_package / relative))

    # 冻结的 candidate-root 证据必须以包内副本交付：manifest builder 和远端
    # auditor 都只认包内路径（"不偷读包外文件"），relocation 因此把 record 的
    # 两个指针指向包内副本——副本得先在这里落地。
    for name in (
        f"{candidate_id}.chat-authority.json",
        f"{candidate_id}.clip-context.json",
    ):
        destination = destination_package / name
        if str(destination) in seen:
            continue
        physical = source_package_dir / name
        if not _regular_file(physical):
            physical = source_package_dir.parent / name
        copies.append(
            CopyItem(
                source=require_regular_file(physical, label=f"staged {name}"),
                destination=destination,
                relative=name,
                role="package",
            )
        )
        seen.add(str(destination))

    repo_assets: list[str] = []
    for kind, pointer, value in _referenced_locators(documents):
        if pointer in {("chat_authority_audit_path",), ("clip_context_path",)}:
            continue  # staged into the package above
        matched = match_root_role(value, mappings=mappings)
        if matched is None:
            raise PackageImportError(
                "LOCATOR_OUTSIDE_ROOTS",
                f"{kind}{pointer_text(pointer)} escapes the relocation roots: "
                f"{value}",
            )
        role, side, suffix = matched
        if side == "destination":
            continue
        destination_root = mappings[ROOT_ROLES.index(role)][1]
        destination_path = Path(destination_root + suffix)
        if role == "repo":
            require_regular_file(destination_path, label=f"{kind} repo asset")
            repo_assets.append(str(destination_path))
            continue
        if role == "package":
            continue  # already covered by the recursive package copy
        if str(destination_path) in seen:
            continue
        physical_source = Path(
            _rebase(
                value,
                roots.source_candidate_root,
                str(source_package_dir.parent),
            )
        )
        copies.append(
            CopyItem(
                source=require_regular_file(
                    physical_source,
                    label=f"{kind}{pointer_text(pointer)} candidate-root artifact",
                ),
                destination=destination_path,
                relative=str(
                    destination_path.relative_to(roots.destination_candidate_root)
                ),
                role="candidate",
            )
        )
        seen.add(str(destination_path))
    return ImportPlan(
        candidate_id=candidate_id,
        roots=roots,
        documents=documents,
        copies=tuple(copies),
        repo_assets=tuple(sorted(set(repo_assets))),
    )


def _rebase(value: str, source_root: str, physical_root: str) -> str:
    """Map one declared source path onto the physical staging directory."""

    if value == source_root:
        return physical_root
    if not value.startswith(source_root + "/"):
        raise PackageImportError(
            "LOCATOR_OUTSIDE_ROOTS",
            f"{value} is not under the declared source root {source_root}",
        )
    return physical_root + value[len(source_root):]


def relocated_document_guard(
    package_root: Path, candidate_id: str
) -> dict[str, str]:
    """Relative names whose destination bytes a committed relocation owns.

    Re-running an import must not re-copy the producing host's pre-relocation
    documents over the relocated ones — that would silently un-relocate the
    package.  A committed journal is the authority for which names are already
    projected and what they must hash to.
    """

    journal_path = relocation_journal_path(package_root, candidate_id)
    if not _regular_file(journal_path):
        return {}
    journal, _ = load_json_document(journal_path, label="relocation journal")
    if journal.get("status") != "COMMITTED":
        return {}
    if str(journal.get("candidate_id") or "") != candidate_id:
        return {}
    guard: dict[str, str] = {}
    for kind, entry in (journal.get("documents") or {}).items():
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("path") or "")
        if not name:
            continue
        guard[name] = declared_digest(
            entry.get("after_sha256"), label=f"journal {kind}"
        )
    return guard


def execute_copy(
    plan: ImportPlan, *, apply: bool, protected: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Copy every planned byte and prove sha256 identity on both sides."""

    protected = dict(protected or {})
    entries: list[dict[str, Any]] = []
    for item in plan.copies:
        source_digest = sha256_file(item.source)
        size = item.source.stat().st_size
        entry = {
            "relative": item.relative,
            "role": item.role,
            "source": str(item.source),
            "destination": str(item.destination),
            "sha256": source_digest,
            "bytes": size,
        }
        expected_relocated = protected.get(item.relative)
        if expected_relocated is not None:
            landed = (
                sha256_file(item.destination)
                if _regular_file(item.destination)
                else ""
            )
            if landed != expected_relocated:
                raise PackageImportError(
                    "RELOCATED_DOCUMENT_DRIFT",
                    f"{item.relative} is owned by a committed relocation "
                    f"(expected {expected_relocated}) but the destination holds "
                    f"{landed or 'nothing'}",
                    hint="restore the relocated document, or archive the whole "
                    "destination package and import again from scratch",
                )
            entry["status"] = "PRESERVED_RELOCATED"
            entry["destination_sha256"] = landed
            entries.append(entry)
            continue
        if not apply:
            if _regular_file(item.destination):
                entry["status"] = (
                    "ALREADY_IDENTICAL"
                    if sha256_file(item.destination) == source_digest
                    else "WOULD_OVERWRITE_DIVERGENT"
                )
            else:
                entry["status"] = "WOULD_COPY"
            entries.append(entry)
            continue
        if _regular_file(item.destination) and (
            sha256_file(item.destination) == source_digest
        ):
            entry["status"] = "ALREADY_IDENTICAL"
            entries.append(entry)
            continue
        atomic_write_bytes(item.destination, item.source.read_bytes())
        landed = sha256_file(item.destination)
        if landed != source_digest:
            raise PackageImportError(
                "COPY_INTEGRITY_MISMATCH",
                f"{item.relative}: source={source_digest} destination={landed}",
                hint="the transport corrupted these bytes; re-stage the package "
                "and re-run",
            )
        entry["status"] = "COPIED"
        entries.append(entry)
    divergent = [row for row in entries if row["status"] == "WOULD_OVERWRITE_DIVERGENT"]
    return {
        "file_count": len(entries),
        "total_bytes": sum(int(row["bytes"]) for row in entries),
        "divergent_destination_count": len(divergent),
        "files": entries,
    }


def verify_declared_artifacts(
    package_root: Path, documents: PackageDocuments
) -> dict[str, str]:
    """Check the package's own hash declarations against the landed bytes.

    Copy-integrity only proves "the same bytes arrived".  These declarations
    prove "the bytes the producing host signed are the bytes we have", which is
    what every downstream gate actually binds to.
    """

    artifact_hashes = documents.record.get("artifact_hashes")
    if not isinstance(artifact_hashes, Mapping):
        raise PackageImportError(
            "PACKAGE_DOCUMENT_INVALID", "record.artifact_hashes is not an object"
        )
    cover_generation = documents.publish.get("cover_generation")
    if not isinstance(cover_generation, Mapping):
        raise PackageImportError(
            "PACKAGE_DOCUMENT_INVALID", "publish.cover_generation is not an object"
        )
    stem = documents.upload_stem
    checks = {
        "burned_video": (
            package_root / f"{stem}.mp4",
            artifact_hashes.get("burned_video_sha256"),
        ),
        "subtitle": (
            package_root / f"{documents.candidate_id}.recut.srt",
            artifact_hashes.get("subtitle_sha256"),
        ),
        "final_cover": (
            _package_internal_cover(package_root, cover_generation),
            cover_generation.get("final_cover_sha256"),
        ),
    }
    verified: dict[str, str] = {}
    for label, (path, declared) in checks.items():
        expected = declared_digest(declared, label=f"{label} declaration")
        actual = sha256_file(require_regular_file(path, label=label))
        if actual != expected:
            raise PackageImportError(
                "DECLARED_ARTIFACT_SHA_DRIFT",
                f"{label}: declared={expected} actual={actual} path={path}",
                hint="the package is internally inconsistent; do not import it",
            )
        verified[label] = expected
    record_cover = declared_digest(
        artifact_hashes.get("cover_sha256"), label="record cover declaration"
    )
    if record_cover != verified["final_cover"]:
        raise PackageImportError(
            "DECLARED_ARTIFACT_SHA_DRIFT",
            "record/publish disagree on the final cover: "
            f"record={record_cover} publish={verified['final_cover']}",
        )
    return verified


def _package_internal_cover(
    package_root: Path, cover_generation: Mapping[str, Any]
) -> Path:
    declared = cover_generation.get("final_cover")
    if not isinstance(declared, str) or not declared:
        raise PackageImportError(
            "PACKAGE_DOCUMENT_INVALID",
            "publish.cover_generation lacks final_cover",
        )
    return package_root / PurePosixPath(declared).parent.name / PurePosixPath(
        declared
    ).name


# --------------------------------------------------------------------------
# relocation
# --------------------------------------------------------------------------


def relocation_journal_path(package_root: Path, candidate_id: str) -> Path:
    return package_root / f".{candidate_id}.package-relocation.json"


def _preimage_path(package_root: Path, candidate_id: str, kind: str) -> Path:
    return package_root / f".{candidate_id}.pre-relocation-{kind}.json"


def _rewrite_path_value(
    value: object,
    *,
    kind: str,
    pointer: JsonPointer,
    mappings: Sequence[tuple[str, str]],
) -> object:
    matched = validate_path_root_role(
        value, kind=kind, pointer=pointer, mappings=mappings
    )
    if matched is None:
        return None
    role, side, suffix = matched
    if side == "destination":
        return value
    return mappings[ROOT_ROLES.index(role)][1] + suffix


def _changed_pointers(
    before: object, after: object, pointer: JsonPointer = ()
) -> set[JsonPointer]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        if set(before) != set(after):
            return {pointer}
        changed: set[JsonPointer] = set()
        for key in before:
            changed.update(
                _changed_pointers(before[key], after[key], (*pointer, str(key)))
            )
        return changed
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            return {pointer}
        changed = set()
        for index, (left, right) in enumerate(zip(before, after)):
            changed.update(_changed_pointers(left, right, (*pointer, str(index))))
        return changed
    return set() if before == after else {pointer}


def _allowed_changed_pointer(kind: str, pointer: JsonPointer) -> bool:
    if is_mutable_pointer(kind, pointer):
        return True
    return kind == "record" and pointer in {
        ("speaker_finalization_manifest_sha256",),
        ("artifact_hashes", "publish_draft_sha256"),
    }


def _guard_immutable(
    kind: str,
    source: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    source_workspace_root: str,
) -> None:
    changed = _changed_pointers(source, result)
    illegal = sorted(
        pointer_text(pointer)
        for pointer in changed
        if not _allowed_changed_pointer(kind, pointer)
    )
    if illegal:
        raise PackageImportError(
            "IMMUTABLE_EVIDENCE_CHANGED",
            f"{kind}: relocation would change frozen evidence at "
            + ",".join(illegal),
        )
    try:
        reject_unknown_wsl_paths(
            result, kind=kind, source_workspace_root=source_workspace_root
        )
    except PackageRelocationError as exc:
        raise PackageImportError("UNRELOCATED_EXTERNAL_PATH", f"{exc}") from exc


def transform_speaker(
    source: Mapping[str, Any], *, roots: RelocationRoots
) -> dict[str, Any]:
    mappings = roots.mappings()
    result = copy.deepcopy(dict(source))
    for pointer in SPEAKER_PATH_POINTERS:
        if pointer[0] not in result:
            continue
        set_value(
            result,
            pointer,
            _rewrite_path_value(
                get_value(result, pointer),
                kind="speaker",
                pointer=pointer,
                mappings=mappings,
            ),
        )
    _guard_immutable(
        "speaker",
        source,
        result,
        source_workspace_root=roots.source_workspace_root,
    )
    return result


def transform_publish(
    source: Mapping[str, Any], *, roots: RelocationRoots
) -> dict[str, Any]:
    mappings = roots.mappings()
    result = copy.deepcopy(dict(source))
    for pointer in PUBLISH_PATH_POINTERS:
        if get_value(result, pointer) is None:
            continue
        set_value(
            result,
            pointer,
            _rewrite_path_value(
                get_value(result, pointer),
                kind="publish",
                pointer=pointer,
                mappings=mappings,
            ),
        )
    _guard_immutable(
        "publish",
        source,
        result,
        source_workspace_root=roots.source_workspace_root,
    )
    return result


def transform_record(
    source: Mapping[str, Any],
    *,
    roots: RelocationRoots,
    speaker: Mapping[str, Any] | None,
    speaker_sha256: str | None,
    publish: Mapping[str, Any],
    publish_sha256: str,
    chat_target: str,
    context_target: str,
) -> dict[str, Any]:
    """Project the record last: it binds both other documents' hashes."""

    mappings = roots.mappings()
    result = copy.deepcopy(dict(source))
    for pointer in RECORD_PATH_POINTERS:
        if get_value(result, pointer) is None:
            continue
        if pointer == ("chat_authority_audit_path",):
            replacement: object = chat_target
        elif pointer == ("clip_context_path",):
            replacement = context_target
        else:
            replacement = _rewrite_path_value(
                get_value(result, pointer),
                kind="record",
                pointer=pointer,
                mappings=mappings,
            )
        set_value(result, pointer, replacement)

    if speaker is not None:
        if not isinstance(result.get("speaker_finalization"), dict):
            raise PackageImportError(
                "PACKAGE_DOCUMENT_INVALID",
                "record.speaker_finalization is not an object",
            )
        result["speaker_finalization"] = copy.deepcopy(dict(speaker))
        if speaker_sha256 is None:
            raise PackageImportError(
                "PACKAGE_DOCUMENT_INVALID", "speaker manifest hash is missing"
            )
        result["speaker_finalization_manifest_sha256"] = "sha256:" + speaker_sha256

    artifact_hashes = result.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict):
        raise PackageImportError(
            "PACKAGE_DOCUMENT_INVALID", "record.artifact_hashes is not an object"
        )
    artifact_hashes["publish_draft_sha256"] = "sha256:" + publish_sha256

    publish_staging = result.get("publish_staging")
    if not isinstance(publish_staging, dict):
        raise PackageImportError(
            "PACKAGE_DOCUMENT_INVALID", "record.publish_staging is not an object"
        )
    expected_keys = (
        set(publish) - _PUBLISH_NON_STAGING_KEYS
    ) | set(_PUBLISH_STAGING_LOCAL_KEYS)
    if set(publish_staging) != expected_keys:
        raise PackageImportError(
            "PUBLISH_STAGING_FIELD_SET_DRIFT",
            "record.publish_staging and publish.json declare different field "
            "sets; the mirror contract is broken",
        )
    for key in publish_staging:
        if key in _PUBLISH_STAGING_LOCAL_KEYS:
            continue
        publish_staging[key] = copy.deepcopy(publish[key])

    _guard_immutable(
        "record",
        source,
        result,
        source_workspace_root=roots.source_workspace_root,
    )
    return result


def _committed_journal_matches(
    journal: Mapping[str, Any],
    *,
    candidate_id: str,
    roots: RelocationRoots,
    current: Mapping[str, bytes],
) -> bool:
    if journal.get("schema_version") != RELOCATION_SCHEMA_VERSION:
        return False
    if journal.get("status") != "COMMITTED":
        return False
    if str(journal.get("candidate_id") or "") != candidate_id:
        return False
    journal_roots = journal.get("roots")
    if not isinstance(journal_roots, Mapping):
        return False
    for key in (
        "destination_package_root",
        "destination_candidate_root",
        "destination_repo_root",
    ):
        if str(journal_roots.get(key) or "") != roots.as_dict()[key]:
            return False
    documents = journal.get("documents")
    if not isinstance(documents, Mapping):
        return False
    if set(documents) != set(current):
        return False
    for kind, payload in current.items():
        declared = (documents.get(kind) or {}).get("after_sha256")
        if declared_digest(declared, label=f"journal {kind}") != sha256_bytes(
            payload
        ):
            return False
    return True


def relocate_package(
    *,
    package_root: Path,
    candidate_id: str,
    roots: RelocationRoots,
    apply: bool,
) -> dict[str, Any]:
    """Project runtime locators onto the destination host, transactionally.

    Commit order is speaker → publish → record because the record binds both
    other documents' hashes; a crash therefore never leaves the record pointing
    at a hash that does not exist on disk.  Re-entry is decided only by the
    committed journal: a matching COMMITTED journal is a no-op, and any other
    journal state refuses with restore instructions rather than guessing.
    """

    documents = read_package_documents(package_root, candidate_id)
    journal_path = relocation_journal_path(package_root, candidate_id)
    current_payloads: dict[str, bytes] = {
        "publish": documents.publish_path.read_bytes(),
        "record": documents.record_path.read_bytes(),
    }
    if documents.speaker_path is not None:
        current_payloads["speaker"] = documents.speaker_path.read_bytes()

    if _regular_file(journal_path):
        journal, _ = load_json_document(journal_path, label="relocation journal")
        if _committed_journal_matches(
            journal,
            candidate_id=candidate_id,
            roots=roots,
            current=current_payloads,
        ):
            return {
                "status": "ALREADY_RELOCATED",
                "journal_path": str(journal_path),
                "journal_sha256": sha256_file(journal_path),
                "transaction_id": str(journal.get("transaction_id") or ""),
                "committed_at": str(journal.get("committed_at") or ""),
            }
        raise PackageImportError(
            "RELOCATION_JOURNAL_INCONSISTENT",
            f"{journal_path} does not describe the documents currently on disk "
            f"(status={journal.get('status')!r})",
            hint="an earlier relocation did not finish; restore "
            f".{candidate_id}.pre-relocation-*.json over the live documents, "
            "delete the journal, then re-run",
        )

    chat_target = package_root / f"{candidate_id}.chat-authority.json"
    context_target = package_root / f"{candidate_id}.clip-context.json"
    artifact_hashes = documents.record.get("artifact_hashes") or {}
    for label, target, declared in (
        (
            "chat authority",
            chat_target,
            artifact_hashes.get("chat_authority_audit_sha256"),
        ),
        (
            "clip context",
            context_target,
            artifact_hashes.get("clip_context_file_sha256"),
        ),
    ):
        expected = declared_digest(declared, label=f"{label} declaration")
        actual = sha256_file(require_regular_file(target, label=label))
        if actual != expected:
            raise PackageImportError(
                "STAGED_EVIDENCE_SHA_DRIFT",
                f"{label}: declared={expected} actual={actual} path={target}",
            )

    speaker_after = (
        transform_speaker(documents.speaker, roots=roots)
        if documents.speaker is not None
        else None
    )
    speaker_payload = (
        json_bytes(speaker_after) if speaker_after is not None else None
    )
    publish_after = transform_publish(documents.publish, roots=roots)
    publish_payload = json_bytes(publish_after)
    record_after = transform_record(
        documents.record,
        roots=roots,
        speaker=speaker_after,
        speaker_sha256=(
            sha256_bytes(speaker_payload) if speaker_payload is not None else None
        ),
        publish=publish_after,
        publish_sha256=sha256_bytes(publish_payload),
        chat_target=str(chat_target),
        context_target=str(context_target),
    )
    record_payload = json_bytes(record_after)

    after_by_kind: dict[str, bytes] = {
        "publish": publish_payload,
        "record": record_payload,
    }
    if speaker_payload is not None:
        after_by_kind["speaker"] = speaker_payload
    paths_by_kind = {
        "publish": documents.publish_path,
        "record": documents.record_path,
        "speaker": documents.speaker_path,
    }
    journal = {
        "schema_version": RELOCATION_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "transaction_id": os.urandom(16).hex(),
        "prepared_at": now_utc(),
        "status": "PREPARED",
        "roots": roots.as_dict(),
        "commit_order": [
            kind for kind in _DOCUMENT_COMMIT_ORDER if kind in after_by_kind
        ],
        "uniform_host_fallback": documents.speaker is None,
        "documents": {
            kind: {
                "path": paths_by_kind[kind].name,
                "before_sha256": sha256_bytes(current_payloads[kind]),
                "after_sha256": sha256_bytes(payload),
                "changed_pointers": sorted(
                    pointer_text(pointer)
                    for pointer in _changed_pointers(
                        json.loads(current_payloads[kind].decode("utf-8")),
                        json.loads(payload.decode("utf-8")),
                    )
                ),
            }
            for kind, payload in after_by_kind.items()
        },
        "staged_evidence": {
            "chat_authority": {
                "path": chat_target.name,
                "sha256": sha256_file(chat_target),
            },
            "clip_context": {
                "path": context_target.name,
                "sha256": sha256_file(context_target),
            },
        },
    }
    if documents.speaker is not None:
        # chat-authority is frozen evidence and keeps the producing host's
        # speaker-manifest hash.  Relocation rewrites the manifest's locator
        # fields, so the two hashes legitimately diverge; record the lineage
        # instead of touching frozen evidence (2026-08-09 87finish precedent).
        chat_authority, _ = load_json_document(chat_target, label="chat authority")
        journal["speaker_manifest_lineage"] = {
            "authority": "preserved_chat_pre_relocation_speaker_manifest",
            "chat_speaker_manifest_sha256": str(
                chat_authority.get("speaker_manifest_sha256") or ""
            ),
            "before_sha256": sha256_bytes(current_payloads["speaker"]),
            "after_sha256": sha256_bytes(after_by_kind["speaker"]),
        }

    if not apply:
        return {
            "status": "WOULD_RELOCATE",
            "journal_path": str(journal_path),
            "journal": journal,
        }

    for kind, payload in current_payloads.items():
        atomic_write_bytes(_preimage_path(package_root, candidate_id, kind), payload)
    atomic_write_json(journal_path, journal)
    for kind in _DOCUMENT_COMMIT_ORDER:
        if kind not in after_by_kind:
            continue
        atomic_write_bytes(paths_by_kind[kind], after_by_kind[kind])
        landed = sha256_file(paths_by_kind[kind])
        if landed != sha256_bytes(after_by_kind[kind]):
            raise PackageImportError(
                "RELOCATION_WRITE_DRIFT",
                f"{kind} document did not land with its projected hash",
            )
    journal["status"] = "COMMITTED"
    journal["committed_at"] = now_utc()
    journal_sha256 = atomic_write_json(journal_path, journal)
    return {
        "status": "RELOCATED",
        "journal_path": str(journal_path),
        "journal_sha256": journal_sha256,
        "transaction_id": journal["transaction_id"],
        "committed_at": journal["committed_at"],
        "changed_pointers": {
            kind: value["changed_pointers"]
            for kind, value in journal["documents"].items()
        },
    }


def materialize_same_stem_cover(
    *, package_root: Path, documents: PackageDocuments, apply: bool
) -> dict[str, Any]:
    """Assemble the ``<video.stem>.cover.png`` delivery alias.

    ``authorized_upload make-manifest`` compares the joint-QC receipt against
    ``manifest.cover.path``, which is this same-stem alias — not the generation
    route filename.  Assembling it here (byte-identical to the declared final
    cover) is what lets the QC receipt and the upload manifest name one file.
    """

    cover_generation = documents.publish.get("cover_generation") or {}
    source = _package_internal_cover(package_root, cover_generation)
    expected = declared_digest(
        cover_generation.get("final_cover_sha256"), label="final cover declaration"
    )
    alias = package_root / f"{documents.upload_stem}.cover.png"
    if _regular_file(alias) and sha256_file(alias) == expected:
        return {"status": "ALREADY_PRESENT", "path": str(alias), "sha256": expected}
    if not apply:
        return {"status": "WOULD_ASSEMBLE", "path": str(alias), "sha256": expected}
    payload = require_regular_file(source, label="final cover").read_bytes()
    if sha256_bytes(payload) != expected:
        raise PackageImportError(
            "DECLARED_ARTIFACT_SHA_DRIFT",
            f"final cover: declared={expected} actual={sha256_bytes(payload)}",
        )
    atomic_write_bytes(alias, payload)
    if sha256_file(alias) != expected:
        raise PackageImportError(
            "SAME_STEM_COVER_WRITE_DRIFT",
            "same-stem cover alias did not land with the declared hash",
        )
    return {"status": "ASSEMBLED", "path": str(alias), "sha256": expected}


# --------------------------------------------------------------------------
# state binding
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundPackage:
    """Everything the state row binds, read back from the relocated package."""

    candidate_id: str
    record: dict[str, Any]
    publish: dict[str, Any]
    speaker: dict[str, Any] | None
    record_path: Path
    publish_path: Path
    burned_path: Path
    burned_video_sha256: str
    cover_path: Path
    cover_sha256: str
    same_stem_cover_path: Path
    journal_path: Path
    journal_sha256: str


def read_bound_package(
    *, package_root: Path, candidate_id: str
) -> BoundPackage:
    documents = read_package_documents(package_root, candidate_id)
    journal_path = relocation_journal_path(package_root, candidate_id)
    journal, journal_payload = load_json_document(
        journal_path, label="relocation journal"
    )
    if journal.get("status") != "COMMITTED":
        raise PackageImportError(
            "RELOCATION_NOT_COMMITTED",
            f"relocation journal status={journal.get('status')!r}",
            hint="run the relocation step before binding state",
        )
    for kind, path in (
        ("record", documents.record_path),
        ("publish", documents.publish_path),
        ("speaker", documents.speaker_path),
    ):
        if path is None:
            continue
        declared = ((journal.get("documents") or {}).get(kind) or {}).get(
            "after_sha256"
        )
        if sha256_file(path) != declared_digest(declared, label=f"journal {kind}"):
            raise PackageImportError(
                "RELOCATION_POSTIMAGE_DRIFT",
                f"{kind} document drifted from the committed relocation postimage",
            )
    verified = verify_declared_artifacts(package_root, documents)
    cover_generation = documents.publish.get("cover_generation") or {}
    cover_path = _package_internal_cover(package_root, cover_generation)
    declared_cover = cover_generation.get("final_cover")
    if str(declared_cover or "") != str(cover_path):
        raise PackageImportError(
            "COVER_NOT_PACKAGE_INTERNAL",
            "publish.cover_generation.final_cover does not name the relocated "
            f"package copy: declared={declared_cover!r} package={cover_path}",
            hint="relocation did not project the cover locator; re-run the "
            "relocation step",
        )
    same_stem_cover = package_root / f"{documents.upload_stem}.cover.png"
    if sha256_file(
        require_regular_file(same_stem_cover, label="same-stem cover")
    ) != verified["final_cover"]:
        raise PackageImportError(
            "SAME_STEM_COVER_DRIFT",
            "the same-stem delivery alias is not the declared final cover",
        )
    title = documents.publish.get("title")
    if not isinstance(title, str) or not title.strip():
        raise PackageImportError("PUBLISH_TITLE_MISSING", "publish lacks a title")
    staging_title = (documents.record.get("publish_staging") or {}).get("title")
    if staging_title != title:
        raise PackageImportError(
            "TITLE_SURFACE_DRIFT", "record.publish_staging and publish disagree on title"
        )
    for label in ("boundary_audit", "subtitle_timing_qa", "redelivery_baseline"):
        if not isinstance(documents.record.get(label), Mapping):
            raise PackageImportError(
                "PACKAGE_DOCUMENT_INVALID", f"record lacks {label}"
            )
    return BoundPackage(
        candidate_id=candidate_id,
        record=documents.record,
        publish=documents.publish,
        speaker=documents.speaker,
        record_path=documents.record_path,
        publish_path=documents.publish_path,
        burned_path=package_root / f"{documents.upload_stem}.mp4",
        burned_video_sha256="sha256:" + verified["burned_video"],
        cover_path=cover_path,
        cover_sha256="sha256:" + verified["final_cover"],
        same_stem_cover_path=same_stem_cover,
        journal_path=journal_path,
        journal_sha256="sha256:" + sha256_bytes(journal_payload),
    )


def _package_summary(package: BoundPackage) -> dict[str, Any]:
    boundary = package.record.get("boundary_audit") or {}
    timing = package.record.get("subtitle_timing_qa") or {}
    speaker = package.record.get("speaker_finalization") or {}
    redelivery = package.record.get("redelivery_baseline") or {}
    return {
        "candidate_id": package.candidate_id,
        "final_end_ms": boundary.get("final_end_ms"),
        "duration_ms": package.record.get("duration_ms"),
        "closure_sentence": boundary.get("closure_sentence"),
        "boundary_verdict": boundary.get("verdict"),
        "red_flags": list(boundary.get("red_flags") or []),
        "boundary_repairs": list(boundary.get("boundary_repairs") or []),
        "timing_qa": dict(timing.get("counts") or {}),
        "cover_status": package.publish.get("cover_status"),
        "title": package.publish.get("title"),
        "speaker_status": speaker.get("status"),
        "redelivery_baseline_status": redelivery.get("status"),
    }


def _new_pick_row(package: BoundPackage, *, date: str) -> dict[str, Any]:
    """Assemble a pick row for a candidate free never produced itself.

    Every value is read out of the package's own frozen documents; nothing is
    invented.  Fields the producing runner would have filled from its in-memory
    selection item (segment/session identifiers) are only carried when the
    record actually declares them.
    """

    record = package.record
    story = record.get("story_contract") or {}
    media_path = record.get("media_path")
    row: dict[str, Any] = {
        "candidate_id": package.candidate_id,
        "date": date,
        "lane": "talk",
        "start_ms": record.get("start_ms"),
        "end_ms": record.get("end_ms"),
        "hook": story.get("selection_hook") or "",
    }
    if isinstance(media_path, str) and media_path:
        row["segment"] = PurePosixPath(media_path).name
        row["segment_path"] = media_path
    for key in ("selection_scorecard", "session_relation_authority"):
        value = record.get(key)
        if value is not None:
            row[key] = copy.deepcopy(value)
    if row["start_ms"] is None or row["end_ms"] is None:
        raise PackageImportError(
            "PACKAGE_DOCUMENT_INVALID",
            "record lacks start_ms/end_ms; a new pick row cannot be assembled "
            "without fabricating the selection window",
        )
    return row




@dataclass(frozen=True)
class StatePreconditions:
    """What the state says about this candidate before anything is written."""

    batch_status: str
    pick_index: int | None
    will_create_pick_row: bool


def check_state_preconditions(
    before_state: Mapping[str, Any],
    *,
    candidate_id: str,
    date: str,
    allow_new_pick: bool,
    project_closure,
) -> StatePreconditions:
    """Refuse every state shape the bind could not legally land on.

    Split out of :func:`build_bound_state` so a dry run can answer "would this
    import be accepted?" before a single byte is copied — and so the batch-status
    whitelist gives a typed refusal here instead of a crash in step 5.
    """

    batch_status = str(before_state.get("status") or "")
    if batch_status not in REVIEWABLE_BATCH_STATUSES:
        raise PackageImportError(
            "BATCH_STATUS_NOT_REVIEWABLE",
            f"state.status={batch_status!r} is outside the manifest builder's "
            "whitelist " + ",".join(sorted(REVIEWABLE_BATCH_STATUSES)),
            hint="'processing' means the runner tick is currently writing picks "
            "— wait for the tick to finish; any other status needs the batch to "
            "reach a reviewable terminal state first",
        )
    state_date = str(before_state.get("date") or "")
    if state_date and state_date != date:
        raise PackageImportError(
            "STATE_DATE_MISMATCH",
            f"state declares date={state_date!r}, import was asked for {date!r}",
        )
    picks = before_state.get("picks")
    if not isinstance(picks, list):
        raise PackageImportError("STATE_SHAPE_INVALID", "state.picks is not a list")
    matches = [
        index
        for index, row in enumerate(picks)
        if isinstance(row, Mapping) and row.get("candidate_id") == candidate_id
    ]
    if len(matches) > 1:
        raise PackageImportError(
            "DUPLICATE_PICK_ROW",
            f"{candidate_id} already appears {len(matches)} times in picks",
        )
    for queue in ("pending_talk", "pending_song"):
        rows = before_state.get(queue)
        if isinstance(rows, list) and any(
            isinstance(row, Mapping)
            and str(row.get("candidate_id") or row.get("cid") or "") == candidate_id
            for row in rows
        ):
            raise PackageImportError(
                "CANDIDATE_STILL_QUEUED",
                f"{candidate_id} is still queued in {queue}; the runner would "
                "produce over the import",
            )
    closure = project_closure(before_state)
    if candidate_id in list(closure.get("published_candidate_ids") or []):
        raise PackageImportError(
            "CANDIDATE_ALREADY_PUBLISHED",
            f"{candidate_id} is already published; import refuses to rebind a "
            "published delivery",
            hint="published candidates only accept the same-BV edit/replace lane "
            "(publication registry is the sole upload authority)",
        )
    if not matches:
        if not allow_new_pick:
            raise PackageImportError(
                "PICK_ROW_ABSENT",
                f"{candidate_id} has no pick row in the day's state",
                hint="pass --allow-new-pick to create one from the package's own "
                "frozen documents",
            )
        return StatePreconditions(
            batch_status=batch_status, pick_index=None, will_create_pick_row=True
        )
    row = picks[matches[0]]
    status = row.get("status")
    if status != "review_ready" or row.get("rc") != 0:
        raise PackageImportError(
            "PICK_STATUS_NOT_REBINDABLE",
            f"{candidate_id} is status={status!r} rc={row.get('rc')!r}; import "
            "only rebinds an already review_ready row or creates a new one",
            hint="a rejected/failed row must go through "
            "scripts/revive_rejected_candidates.py first — import is not a "
            "revival channel",
        )
    return StatePreconditions(
        batch_status=batch_status,
        pick_index=matches[0],
        will_create_pick_row=False,
    )


def build_bound_state(
    before_state: Mapping[str, Any],
    *,
    package: BoundPackage,
    date: str,
    bound_at: str,
    allow_new_pick: bool,
    project_closure,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute the post-image of one state bind.  Pure — the caller writes it.

    Re-running the import over an already-bound row rewrites the same values and
    therefore converges (idempotent); it never appends a second row, because the
    duplicate check runs before and the row identity is the candidate id.
    """

    preconditions = check_state_preconditions(
        before_state,
        candidate_id=package.candidate_id,
        date=date,
        allow_new_pick=allow_new_pick,
        project_closure=project_closure,
    )
    after_state = copy.deepcopy(dict(before_state))
    picks = after_state["picks"]
    if preconditions.will_create_pick_row:
        row = _new_pick_row(package, date=date)
        picks.append(row)
    else:
        row = picks[preconditions.pick_index]
    previous_import = row.get("external_package_import")

    removed = sorted(key for key in _SUPERSEDED_PICK_KEYS if key in row)
    for key in removed:
        row.pop(key, None)
    publish = package.publish
    boundary = package.record.get("boundary_audit") or {}
    row.update(
        {
            "status": "review_ready",
            "rc": 0,
            "summary": _package_summary(package),
            "red_flags": list(boundary.get("red_flags") or []),
            "boundary_repairs": list(boundary.get("boundary_repairs") or []),
            "title": publish.get("title"),
            "title_source": publish.get("title_source"),
            "title_authority_status": publish.get("title_authority_status"),
            "title_authority_error": publish.get("title_authority_error"),
            "cover_status": publish.get("cover_status"),
            # `_resolve_final_cover` reads this exact path off the host and
            # requires its bytes to hash to the publish-declared final cover:
            # name + sha must move together (2026-08-09 chain lesson).
            "cover_path": str(package.cover_path),
            "cover_sha256": package.cover_sha256,
            "cover_generation": copy.deepcopy(publish.get("cover_generation")),
            "video_sha256": package.burned_video_sha256,
            "bundle_lifecycle": "CURRENT",
            "bundle_compliance": "COMPLIANT",
            "external_package_import": {
                "schema_version": STATE_BINDING_SCHEMA_VERSION,
                "status": "VERIFIED_PACKAGE_BOUND",
                "bound_at": bound_at,
                "created_pick_row": preconditions.will_create_pick_row,
                "record_path": str(package.record_path),
                "publish_path": str(package.publish_path),
                "burned_video_path": str(package.burned_path),
                "burned_video_sha256": package.burned_video_sha256,
                "cover_path": str(package.cover_path),
                "cover_sha256": package.cover_sha256,
                "same_stem_cover_path": str(package.same_stem_cover_path),
                "package_relocation_journal_path": str(package.journal_path),
                "package_relocation_journal_sha256": package.journal_sha256,
            },
        }
    )

    # 幂等：同一个包重复导入只是把同样的值再写一遍，只有时间戳会变。发现除
    # bound_at 外完全一致就沿用旧时间戳，后像因此逐字节等于前像，调用方可以
    # 直接跳过这次写入（而不是每次重入都动 state）。
    if isinstance(previous_import, Mapping):
        replayed = dict(row["external_package_import"])
        replayed["bound_at"] = previous_import.get("bound_at")
        # "这一行是不是 import 建出来的" 是历史事实，不随重入变化。
        replayed["created_pick_row"] = previous_import.get("created_pick_row")
        if replayed == dict(previous_import):
            row["external_package_import"] = replayed

    before_closure = project_closure(before_state)
    after_closure = project_closure(after_state)
    before_published = list(before_closure.get("published_candidate_ids") or [])
    after_published = list(after_closure.get("published_candidate_ids") or [])
    if before_published != after_published:
        raise PackageImportError(
            "PUBLICATION_MEMBERSHIP_MOVED",
            "the bind would change the day's published candidate set: "
            f"{before_published} -> {after_published}",
        )
    for key in ("pending_talk", "pending_song", "songs"):
        if after_state.get(key) != before_state.get(key):
            raise PackageImportError(
                "STATE_COLLECTION_MUTATED", f"the bind changed {key}"
            )
    if len(
        [
            row
            for row in after_state["picks"]
            if isinstance(row, Mapping)
            and row.get("candidate_id") == package.candidate_id
        ]
    ) != 1:
        raise PackageImportError(
            "DUPLICATE_PICK_ROW",
            f"the bind left {package.candidate_id} in picks more than once",
        )
    if before_state.get("publication_closure") is not None:
        after_state["publication_closure"] = after_closure
    if str(after_closure.get("status") or "") and str(
        after_state.get("status") or ""
    ) not in REVIEWABLE_BATCH_STATUSES:
        raise PackageImportError(
            "BATCH_STATUS_NOT_REVIEWABLE",
            "the bind would leave state.status="
            f"{after_state.get('status')!r}, outside the manifest whitelist",
        )
    changed = any(
        after_state.get(key) != before_state.get(key)
        for key in ("picks", "publication_closure")
    )
    if changed:
        after_state["updated_at"] = bound_at
    return after_state, {
        "created_pick_row": preconditions.will_create_pick_row,
        "removed_superseded_keys": removed,
        "state_changed": changed,
        "closure_before": before_closure,
        "closure_after": after_closure,
    }
