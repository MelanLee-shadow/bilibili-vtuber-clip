"""Build a review_manifest.json for ONE daily-lane candidate package.

7/24 权宜上传缺口：v3 上传合同要求 package audit，audit 要求
包内 review_manifest.json，而 manifest 生成器只有 recovery 版（绑定 exact
contract/v7 rerun plan/upload_allowed=false 全套 recovery state 结构）——
daily 新 BV 上传车道自 7/14 后没有合法的 manifest 生成器。

本工具是 recovery builder 的 daily 对应物：字段全部 hash-bound 自真实产物
（record/publish/state），不发明任何值：

- 候选自身必须 state 里 rc=0 且 review_ready（批级 with_failures 可接受——
  维护者 行军令：能传的先传，不因批内其他候选失败扣押好片）；
- items 路径指向包内真实文件并带 sha256；
- Talk 显式声明 story_contract_required/source_fact_review_required，并在装配
  前验证 StoryContract、record.publish_staging、publish.json 三面携带逐字
  相同且 hash-bound 的 source-fact receipt；Song 保持既有歌词/标题/封面
  证明车道，不冒充拥有 Talk StoryContract/source-fact receipt；
- 不承诺 upload_allowed=true（daily 新上传不是 recovery same-BV 修复；
  上传授权仍由 authorized_upload 的 make-manifest --quote 层绑定 维护者 原话）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.jingting_chunker import parse_srt_cues  # noqa: E402
from src.autoslice.channel_profile import load_channel_profile  # noqa: E402
from src.autoslice.review_package_ass_audit import (  # noqa: E402
    uniform_host_fallback_declared,
)
from src.autoslice.review_evidence import SourceCue  # noqa: E402
from src.autoslice.addressee_attribution import (  # noqa: E402
    SpeakerEvidenceRejected,
    rebuild_speaker_evidence,
)
from src.autoslice.source_fact_review import (  # noqa: E402
    validate_source_fact_review,
)
from src.autoslice.operator_exact_title_source_fact_authority import (  # noqa: E402
    PASS_DECISION as OPERATOR_EXACT_TITLE_PASS_DECISION,
    validate_operator_exact_title_source_fact_receipt,
)
from src.autoslice.qixi_operator_exact_title_source_fact import (  # noqa: E402
    DECISION as QIXI_OPERATOR_EXACT_TITLE_DECISION,
    validate_receipt as validate_qixi_operator_exact_title_receipt,
)
from src.autoslice.fastlane_c3_terminal_source_fact_preservation import (  # noqa: E402
    CANDIDATE_ID as C3_SOURCE_FACT_CANDIDATE_ID,
    DECISION as C3_SOURCE_FACT_DECISION,
    validate_review as validate_c3_source_fact_review,
)


CHANNEL_PROFILE = load_channel_profile(ROOT)
_SPEAKER_EVIDENCE_UNSET = object()
_MANIFEST_REASON_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{2,159}\Z")


class DailyManifestError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str | None = None) -> None:
        self.reason_code = (
            reason_code
            if isinstance(reason_code, str) and _MANIFEST_REASON_CODE_RE.fullmatch(reason_code)
            else None
        )
        super().__init__(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_component(value: object, *, label: str) -> str:
    name = str(value or "")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
        or name in {".", ".."}
        or Path(name).name != name
    ):
        raise DailyManifestError(f"{label} is not one safe path component: {name!r}")
    return name


def _package_path(
    package_root: Path,
    relative: str | Path,
    *,
    label: str,
    create_parents: bool = False,
) -> Path:
    """Resolve one contained package path without following any symlink."""

    root = package_root.resolve(strict=True)
    raw = Path(relative)
    if raw.is_absolute() or not raw.parts or any(part in {"", ".", ".."} for part in raw.parts):
        raise DailyManifestError(f"{label} is not a safe package-relative path: {raw}")
    cursor = root
    for index, part in enumerate(raw.parts):
        cursor = cursor / part
        if cursor.is_symlink():
            raise DailyManifestError(f"{label} package path contains a symlink: {cursor}")
        if index < len(raw.parts) - 1:
            if cursor.exists() and not cursor.is_dir():
                raise DailyManifestError(f"{label} package parent is not a directory: {cursor}")
            if create_parents and not cursor.exists():
                cursor.mkdir()
                if cursor.is_symlink() or not cursor.is_dir():
                    raise DailyManifestError(
                        f"{label} package parent creation was unsafe: {cursor}"
                    )
    try:
        cursor.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise DailyManifestError(f"{label} escapes package root: {raw}") from exc
    return cursor


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
    except (FileNotFoundError, OSError):
        return False


def _package_regular_file(package_root: Path, relative: str | Path, *, label: str) -> Path | None:
    path = _package_path(package_root, relative, label=label)
    return path if _regular_file(path) else None


def _read_regular_source(path: Path, *, label: str) -> bytes:
    """Read one regular source after rejecting every existing symlink component."""

    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise DailyManifestError(f"{label} source path contains a symlink: {cursor}")
    if not _regular_file(absolute):
        raise DailyManifestError(f"{label} source missing or invalid: {path}")
    try:
        return absolute.read_bytes()
    except OSError as exc:
        raise DailyManifestError(f"{label} source unreadable: {path}") from exc


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_project_bytes(
    *,
    package_root: Path,
    relative: str | Path,
    payload: bytes,
    label: str,
) -> Path:
    """Atomically materialize exact bytes at a safe package-local path."""

    target = _package_path(
        package_root,
        relative,
        label=label,
        create_parents=True,
    )
    if target.exists() and not _regular_file(target):
        raise DailyManifestError(f"{label} package target is not a regular file: {target}")
    expected = _sha256_bytes(payload)
    if _regular_file(target) and _sha256(target) == expected:
        return target
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.tmp-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if _sha256(temporary) != expected:
            raise DailyManifestError(f"{label} temporary projection hash drift")
        # Recheck all components immediately before replacement.
        target = _package_path(package_root, relative, label=label)
        if target.exists() and not _regular_file(target):
            raise DailyManifestError(f"{label} package target changed to a non-regular file")
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except OSError as exc:
        raise DailyManifestError(f"{label} atomic package projection failed") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    verified = _package_regular_file(package_root, relative, label=label)
    if verified is None or _sha256(verified) != expected:
        raise DailyManifestError(f"{label} package projection verification failed")
    return verified


def _story_cues(path: Path) -> list[SourceCue]:
    try:
        cues = parse_srt_cues(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise DailyManifestError(f"source-fact subtitle unreadable: {path}") from exc
    if not cues:
        raise DailyManifestError("source-fact subtitle transcript is empty")
    return [
        SourceCue(
            cue_id=str(cue.index),
            source_start_ms=cue.start_ms,
            source_end_ms=cue.end_ms,
            text=cue.text,
        )
        for cue in cues
    ]


def _story_transcript(path: Path) -> str:
    cues = _story_cues(path)
    transcript = "\n".join(cue.text.strip() for cue in cues if cue.text.strip())
    if not transcript:
        raise DailyManifestError("source-fact subtitle transcript is empty")
    return transcript


def _validate_source_fact_receipts(
    *,
    record_doc: dict,
    publish_doc: dict,
    subtitle_path: Path,
    speaker_evidence: object = _SPEAKER_EVIDENCE_UNSET,
    qixi_repo_root: Path | None = None,
) -> str:
    """Require one immutable source-fact receipt on all publish surfaces."""

    story_contract = record_doc.get("story_contract")
    publish_staging = record_doc.get("publish_staging")
    if not isinstance(story_contract, dict):
        raise DailyManifestError("story contract missing from record")
    if not isinstance(publish_staging, dict):
        raise DailyManifestError("publish staging missing from record")
    receipts = [
        story_contract.get("source_fact_review"),
        publish_staging.get("source_fact_review"),
        publish_doc.get("source_fact_review"),
    ]
    if any(not isinstance(receipt, dict) for receipt in receipts):
        raise DailyManifestError("source-fact review receipt missing from final package surfaces")
    if not (receipts[0] == receipts[1] == receipts[2]):
        raise DailyManifestError("source-fact review receipt drift across final package surfaces")
    title = str(publish_doc.get("title") or "")
    if title != str(publish_staging.get("title") or ""):
        raise DailyManifestError("source-fact reviewed title drift across publish surfaces")
    validation_kwargs = {
        "selection_hook": str(story_contract.get("selection_hook") or ""),
        "title": title,
        "final_transcript": _story_transcript(subtitle_path),
        "clip_context_prompt": str(story_contract.get("clip_context_prompt") or ""),
        "selection_scorecard": story_contract.get("selection_scorecard"),
        "candidate_id": str(story_contract.get("candidate_id") or ""),
        "final_reviewed_srt_path": subtitle_path,
        "story_contract": story_contract,
    }
    if speaker_evidence is not _SPEAKER_EVIDENCE_UNSET:
        validation_kwargs["speaker_evidence"] = speaker_evidence
    if qixi_repo_root is not None:
        validation_kwargs["qixi_repo_root"] = qixi_repo_root
    receipt = receipts[0]
    if isinstance(receipt, dict) and receipt.get("decision") == OPERATOR_EXACT_TITLE_PASS_DECISION:
        if speaker_evidence is _SPEAKER_EVIDENCE_UNSET:
            raise DailyManifestError("operator title source-fact receipt requires speaker evidence")
        valid = validate_operator_exact_title_source_fact_receipt(
            receipt,
            record=record_doc,
            speaker_evidence=speaker_evidence,
            repo_root=qixi_repo_root or ROOT,
            selection_hook=validation_kwargs["selection_hook"],
            title=validation_kwargs["title"],
            final_transcript=validation_kwargs["final_transcript"],
            candidate_id=validation_kwargs["candidate_id"],
            final_reviewed_srt_path=subtitle_path,
        )
    elif isinstance(receipt, dict) and receipt.get("decision") == QIXI_OPERATOR_EXACT_TITLE_DECISION:
        if speaker_evidence is _SPEAKER_EVIDENCE_UNSET:
            raise DailyManifestError("Qixi operator title source-fact receipt requires speaker evidence")
        valid = validate_qixi_operator_exact_title_receipt(
            receipt,
            record=record_doc,
            speaker_evidence=speaker_evidence,
            repo_root=qixi_repo_root or ROOT,
            selection_hook=validation_kwargs["selection_hook"],
            title=validation_kwargs["title"],
            final_transcript=validation_kwargs["final_transcript"],
            candidate_id=validation_kwargs["candidate_id"],
            final_reviewed_srt_path=subtitle_path,
        )
    elif (
        isinstance(receipt, dict)
        and receipt.get("decision") == C3_SOURCE_FACT_DECISION
        and validation_kwargs["candidate_id"] == C3_SOURCE_FACT_CANDIDATE_ID
        and speaker_evidence is not _SPEAKER_EVIDENCE_UNSET
    ):
        # C3 is the sole repository-sealed terminal preservation decision.  It
        # must still execute the same exact-byte review contract; this branch
        # only selects the candidate-specific validator and cannot authorize a
        # different candidate or a generic receipt.
        valid = validate_c3_source_fact_review(
            receipt,
            repo_root=qixi_repo_root or ROOT,
            title=validation_kwargs["title"],
            selection_hook=validation_kwargs["selection_hook"],
            selection_scorecard=validation_kwargs["selection_scorecard"],
            clip_context_prompt=validation_kwargs["clip_context_prompt"],
            final_reviewed_srt_path=subtitle_path,
            speaker_evidence=speaker_evidence,
        )
    else:
        valid = validate_source_fact_review(receipt, **validation_kwargs)
    if not valid:
        raise DailyManifestError("source-fact review receipt is invalid or stale")
    return str(receipts[0]["receipt_sha256"])


def _candidate_lane(record_doc: dict, publish_doc: dict) -> str:
    if str(record_doc.get("classification") or "").lower() == "song":
        return "song"
    if str(publish_doc.get("title") or "").startswith(CHANNEL_PROFILE.song_title_prefix):
        return "song"
    return "talk"


def _source_fact_manifest_fields(
    *,
    lane: str,
    record_doc: dict,
    publish_doc: dict,
    subtitle_path: Path,
    speaker_evidence: object = _SPEAKER_EVIDENCE_UNSET,
    qixi_repo_root: Path | None = None,
) -> dict[str, object]:
    """Talk uses the CPA fact gate; Song keeps its lyric-proof lane."""

    if lane == "song":
        return {}
    return {
        "source_fact_review_required": True,
        "source_fact_review_sha256": _validate_source_fact_receipts(
            record_doc=record_doc,
            publish_doc=publish_doc,
            subtitle_path=subtitle_path,
            speaker_evidence=speaker_evidence,
            qixi_repo_root=qixi_repo_root,
        ),
    }


def _lane_manifest_contract_fields(
    *,
    lane: str,
    record_doc: dict,
    publish_doc: dict,
    subtitle_path: Path,
    speaker_evidence: object = _SPEAKER_EVIDENCE_UNSET,
    qixi_repo_root: Path | None = None,
) -> dict[str, object]:
    if lane == "song":
        return {}
    return {
        "story_contract_required": True,
        **_source_fact_manifest_fields(
            lane=lane,
            record_doc=record_doc,
            publish_doc=publish_doc,
            subtitle_path=subtitle_path,
            speaker_evidence=speaker_evidence,
            qixi_repo_root=qixi_repo_root,
        ),
    }


def _sync_declared_artifact(
    *,
    package_root: Path,
    target: Path,
    source: Path,
    declared_sha256: str,
    label: str,
) -> None:
    """Copy the record-bound candidate artifact into the portable package.

    A relocated package may no longer have its candidate-root source.  Exact
    regular bytes already present at ``target`` are therefore authoritative;
    otherwise repair from an exact regular source.  Neither path may be a
    symlink and a failed repair never mutates the target.
    """
    expected = str(declared_sha256 or "").removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise DailyManifestError(f"{label} lacks a declared SHA-256")
    root = package_root.resolve(strict=True)
    try:
        relative = target.absolute().relative_to(root)
    except ValueError as exc:
        raise DailyManifestError(f"{label} package target escapes root: {target}") from exc
    packaged = _package_regular_file(root, relative, label=label)
    if packaged is not None and _sha256(packaged) == expected:
        return
    payload = _read_regular_source(source, label=label)
    actual = _sha256_bytes(payload)
    if actual != expected:
        raise DailyManifestError(f"{label} sha drift: record={expected} actual={actual}")
    projected = _atomic_project_bytes(
        package_root=root,
        relative=relative,
        payload=payload,
        label=label,
    )
    if _sha256(projected) != expected:
        raise DailyManifestError(f"{label} package repair verification failed")


def _rebuild_package_speaker_evidence(
    *,
    package_root: Path,
    record_doc: dict,
    subtitle_path: Path,
    speaker_srt_path: Path | None,
) -> tuple[dict[str, object], Path | None]:
    """Rebuild only from package-local speaker bytes and reviewed cues.

    The producer record may name an absolute source path, but it is used only
    as a hash-bound import source during package assembly.  The evidence
    builder always consumes the verified package projection.
    """

    raw_speaker_path = record_doc.get("speaker_review_srt_path")
    raw_manifest_path = record_doc.get("speaker_finalization_manifest_path")
    claims_speaker_evidence = any(
        value is not None
        for value in (
            raw_speaker_path,
            raw_manifest_path,
            record_doc.get("speaker_finalization"),
            record_doc.get("speaker_finalization_manifest_sha256"),
        )
    )
    speaker_srt_bytes: bytes | None = None
    manifest_bytes: bytes | None = None
    packaged_manifest: Path | None = None
    if claims_speaker_evidence:
        if speaker_srt_path is None:
            raise DailyManifestError(
                "speaker evidence is claimed but package speaker SRT is missing"
            )
        try:
            speaker_relative = speaker_srt_path.resolve(strict=True).relative_to(
                package_root.resolve(strict=True)
            )
        except (OSError, ValueError) as exc:
            raise DailyManifestError(
                "speaker SRT is not a package-contained regular artifact"
            ) from exc
        packaged_speaker = _package_regular_file(
            package_root,
            speaker_relative,
            label="speaker-final SRT",
        )
        if packaged_speaker is None:
            raise DailyManifestError("speaker SRT is not a package-contained regular artifact")
        if not isinstance(raw_manifest_path, str) or not raw_manifest_path:
            raise DailyManifestError(
                "speaker evidence is claimed but finalization manifest path is missing"
            )
        manifest_basename = _safe_component(
            Path(raw_manifest_path).name,
            label="speaker finalization manifest basename",
        )
        packaged_manifest = package_root / manifest_basename
        _sync_declared_artifact(
            package_root=package_root,
            target=packaged_manifest,
            source=Path(raw_manifest_path),
            declared_sha256=str(record_doc.get("speaker_finalization_manifest_sha256") or ""),
            label="speaker finalization manifest",
        )
        packaged_manifest = _package_regular_file(
            package_root,
            manifest_basename,
            label="speaker finalization manifest",
        )
        if packaged_manifest is None:
            raise DailyManifestError("speaker finalization manifest package projection is missing")
        speaker_srt_bytes = packaged_speaker.read_bytes()
        manifest_bytes = packaged_manifest.read_bytes()
    try:
        rebuilt = rebuild_speaker_evidence(
            record_doc,
            _story_cues(subtitle_path),
            speaker_srt_bytes=speaker_srt_bytes,
            speaker_manifest_bytes=manifest_bytes,
        )
    except SpeakerEvidenceRejected as exc:
        raise DailyManifestError(
            "source-fact speaker evidence rejected",
            reason_code=exc.code,
        ) from exc
    except OSError as exc:
        raise DailyManifestError("source-fact package speaker evidence is unreadable") from exc
    return dict(rebuilt.speaker_evidence), packaged_manifest


def _resolve_final_cover(
    *,
    package_root: Path,
    pick: dict,
    cover_generation: dict,
) -> str:
    """Resolve only the exact final cover declared by state and record."""
    declared = cover_generation.get("final_cover")
    expected = str(cover_generation.get("final_cover_sha256") or "").removeprefix("sha256:")
    pick_path = pick.get("cover_path")
    pick_expected = str(pick.get("cover_sha256") or "").removeprefix("sha256:")
    if not isinstance(declared, str) or not declared or not expected:
        raise DailyManifestError("cover generation lacks final_cover/final_cover_sha256")
    if not isinstance(pick_path, str) or not pick_path or not pick_expected:
        raise DailyManifestError("state pick lacks cover_path/cover_sha256")

    pick_file = Path(pick_path)
    if pick_expected != expected:
        raise DailyManifestError(
            "state/record final cover binding drift: "
            f"state={Path(pick_path).name}:{pick_expected} "
            f"record={Path(declared).name}:{expected}"
        )
    pick_payload = _read_regular_source(pick_file, label="state final cover")
    pick_actual = _sha256_bytes(pick_payload)
    if pick_actual != pick_expected:
        raise DailyManifestError(
            f"state final cover sha drift: state={pick_expected} actual={pick_actual}"
        )

    # Cover repair writes a title-named delivery alias while keeping the
    # record's package-internal route filename.  Path basenames are therefore
    # not identity; both surfaces independently matching the same frozen hash
    # is the actual binding.
    basename = _safe_component(Path(declared).name, label="final cover basename")
    relative = f"covers/{basename}"
    # Cover-only repair commits the new bytes to the title-named delivery
    # alias after the original replacement_recuts package was frozen.  The
    # package builder is the portable assembly boundary, so materialize the
    # publish/state-bound bytes under the generation-declared basename instead
    # of requiring a stale package to have predicted a later repair path.
    final_cover = _atomic_project_bytes(
        package_root=package_root,
        relative=relative,
        payload=pick_payload,
        label="final cover",
    )
    if _sha256(final_cover) != expected:
        raise DailyManifestError("final cover package projection hash drift")
    return relative


def _need_package_file(package_root: Path, name: str) -> Path:
    path = _package_regular_file(package_root, name, label="required artifact")
    if path is None:
        raise DailyManifestError(f"required package file missing: {name}")
    return path


def _resolve_final_burn_artifacts(
    *,
    package_root: Path,
    stem: str,
    uniform_fallback: bool,
) -> tuple[Path, Path, Path | None]:
    """Locate the burned video + ASS (+ labeled speaker SRT) for this package.

    Two finalization styles exist depending on ``AUTOSLICE_SPEAKER_MODE`` at
    production time. Packages that self-declare the uniform_host fallback
    (single-speaker; the standing daily default — see
    ``review_package_ass_audit.uniform_host_fallback_declared``) never run
    producer_speaker finalization and only ever materialize the legacy
    unified-style ``sapphire72`` burn. Speaker-mode packages (``auto``/
    ``required``) instead materialize a labeled speaker ASS and a distinct
    ``-speaker`` burned video family; the packaged text SRT is no longer a
    stand-in for the speaker face in that case, so a real ``speaker-final``
    SRT is returned too. Detection here must agree with the release audit
    chain (``review_package_ass_audit.py``), which is why both consult the
    same self-declaration rather than each guessing from filenames.
    """
    if uniform_fallback:
        return (
            _need_package_file(package_root, f"{stem}.burned-final-sapphire72.mp4"),
            _need_package_file(package_root, f"{stem}.final-sapphire72.ass"),
            None,
        )
    return (
        _need_package_file(package_root, f"{stem}.burned-final-speaker.mp4"),
        _need_package_file(package_root, f"{stem}.speaker-final.ass"),
        _need_package_file(package_root, f"{stem}.speaker-final.srt"),
    )


def _sync_record_bound_candidate_artifacts(
    *,
    package_root: Path,
    candidate_id: str,
    record_doc: dict,
) -> dict[str, str]:
    """Make candidate-root evidence portable under its record hashes."""
    candidate_id = _safe_component(candidate_id, label="candidate_id")
    artifact_hashes = record_doc.get("artifact_hashes") or {}
    artifacts = {
        "chat_authority": (
            f"{candidate_id}.chat-authority.json",
            artifact_hashes.get("chat_authority_audit_sha256"),
        ),
        "clip_context": (
            f"{candidate_id}.clip-context.json",
            artifact_hashes.get("clip_context_file_sha256"),
        ),
    }
    resolved: dict[str, str] = {}
    for label, (name, declared_sha256) in artifacts.items():
        _sync_declared_artifact(
            package_root=package_root,
            target=package_root / name,
            source=package_root.parent / name,
            declared_sha256=str(declared_sha256 or ""),
            label=label.replace("_", " "),
        )
        resolved[label] = name
    return resolved


def build(
    package_root: Path, state_path: Path, deployed_commit_file: Path, candidate_id: str
) -> dict:
    candidate_id = _safe_component(candidate_id, label="candidate_id")
    package_root = package_root.resolve()
    if not package_root.is_dir():
        raise DailyManifestError(f"package root missing: {package_root}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    batch_status = str(state.get("status") or "")
    # retry_wait 只表示批内其他 pick 还有重试预算；review_ready pick 的产物
    # 已冻结（主车道从不 re-supersede review_ready）。processing 仍然拒绝：
    # runner 正在写 picks。
    #
    # 当天第一条投稿之后，`batch_terminal_state` 会把 publication-closure 的
    # 状态提升成顶层 `status`（`project_publication_closure` 只在“当天还没有
    # 任何已发布件”时返回 NOT_APPLICABLE）。这些也是 tick 收尾写入的终态，
    # 与 runner 正在写 picks 无关，因此同样可评审——否则一天里的第二条投稿
    # 永远造不出 manifest（`ready_unpublished_with_failures` 实拒；
    # Codex-F 在未合入的 `wsl/fasttrack-0809` 53dc752c 只补了
    # `publication_in_progress`，仍不够）。`published*` 蕴含没有 ready 行，
    # 逐条 pick 门会照常拒绝，这里不必也不应放行。
    if batch_status not in {
        "review_ready",
        "review_ready_with_failures",
        "review_ready_retry_wait",
        "publication_in_progress",
        "ready_unpublished",
        "ready_unpublished_with_failures",
    }:
        raise DailyManifestError(f"batch status not reviewable: {batch_status}")
    pick = next(
        (row for row in state.get("picks") or [] if row.get("candidate_id") == candidate_id),
        None,
    )
    if pick is None:
        raise DailyManifestError(f"candidate not in state: {candidate_id}")
    if pick.get("status") != "review_ready" or pick.get("rc") != 0:
        raise DailyManifestError(
            f"candidate not individually green: status={pick.get('status')} rc={pick.get('rc')}"
        )
    deployed_commit = deployed_commit_file.read_text(encoding="utf-8").split()[0]

    stem = f"{candidate_id}.recut"

    def need(name: str) -> Path:
        path = _package_regular_file(package_root, name, label="required package artifact")
        if path is None:
            raise DailyManifestError(f"required package file missing: {name}")
        return path

    publish = need(f"{stem}.publish.json")
    publish_doc = json.loads(publish.read_text(encoding="utf-8"))
    cover_generation = (
        publish_doc.get("cover_generation")
        or (publish_doc.get("publish_staging") or {}).get("cover_generation")
        or {}
    )
    if not isinstance(cover_generation, dict):
        raise DailyManifestError("cover generation is not an object")
    record = need(f"{candidate_id}.record.json")
    record_doc = json.loads(record.read_text(encoding="utf-8"))
    subtitle = need(f"{stem}.srt")
    lane = _candidate_lane(record_doc, publish_doc)
    cover_rel = _resolve_final_cover(
        package_root=package_root,
        pick=pick,
        cover_generation=cover_generation,
    )

    # 装配步骤：candidate-root 的冻结证据即使在包内已有旧副本，也必须
    # 重新对照当前 record 声明同步，避免远端 auditor 偷读包外文件。这一步
    # 必须先于烧录产物的文件名解析：speaker-mode 检测要读已同步的 chat
    # authority。
    portable_evidence = _sync_record_bound_candidate_artifacts(
        package_root=package_root,
        candidate_id=candidate_id,
        record_doc=record_doc,
    )
    chat_name = portable_evidence["chat_authority"]
    clip_context_name = portable_evidence["clip_context"]
    chat_path = _package_regular_file(package_root, chat_name, label="chat authority")
    if chat_path is None:
        raise DailyManifestError("portable chat authority is missing")
    chat_authority_doc = json.loads(chat_path.read_text(encoding="utf-8"))
    # AUTOSLICE_SPEAKER_MODE=auto 翻转后，speaker-finalized
    # 包不再产出旧 sapphire72 统一样式烧录；命名解析必须按同一份自证信号
    # 分岔，镜像 review_package_ass_audit.py 已用的判定，两条审计链才不会
    # 对同一包给出不同答案。
    uniform_fallback = uniform_host_fallback_declared(record_doc, chat_authority_doc)
    burned, ass, speaker_srt_file = _resolve_final_burn_artifacts(
        package_root=package_root,
        stem=stem,
        uniform_fallback=uniform_fallback,
    )
    packaged_speaker_manifest: Path | None = None
    if lane == "song":
        lane_manifest_contract_fields = _lane_manifest_contract_fields(
            lane=lane,
            record_doc=record_doc,
            publish_doc=publish_doc,
            subtitle_path=subtitle,
        )
    else:
        speaker_evidence, packaged_speaker_manifest = _rebuild_package_speaker_evidence(
            package_root=package_root,
            record_doc=record_doc,
            subtitle_path=subtitle,
            speaker_srt_path=speaker_srt_file,
        )
        lane_manifest_contract_fields = _lane_manifest_contract_fields(
            lane=lane,
            record_doc=record_doc,
            publish_doc=publish_doc,
            subtitle_path=subtitle,
            speaker_evidence=speaker_evidence,
        )

    def cover_artifact(generation_key: str, sha_key: str) -> str:
        """Locate a package-internal cover artifact declared by generation.

        The publish document records host staging paths; the package carries
        the same bytes under covers*/.  Cover-only repair may happen after the
        original package was frozen, so a missing/stale portable copy is
        assembled from the generation-declared regular file after its hash is
        verified.
        """
        declared = cover_generation.get(generation_key)
        expected = str(cover_generation.get(sha_key) or "").removeprefix("sha256:")
        if not isinstance(declared, str) or not declared or not expected:
            raise DailyManifestError(f"cover generation lacks {generation_key}/{sha_key}")
        basename = _safe_component(Path(declared).name, label=f"{generation_key} basename")
        for parent in ("covers", "covers_ai_original", "cover_refs"):
            relative = f"{parent}/{basename}"
            candidate_path = _package_regular_file(package_root, relative, label=generation_key)
            if candidate_path is not None and _sha256(candidate_path) == expected:
                return f"{parent}/{basename}"
        source = Path(declared)
        payload = _read_regular_source(source, label=f"cover artifact {generation_key}")
        actual = _sha256_bytes(payload)
        if actual != expected:
            raise DailyManifestError(
                f"cover artifact source sha drift: {generation_key}={expected} actual={actual}"
            )
        portable = _atomic_project_bytes(
            package_root=package_root,
            relative=f"covers_ai_original/{basename}",
            payload=payload,
            label=f"cover artifact {generation_key}",
        )
        if _sha256(portable) != expected:
            raise DailyManifestError(f"cover artifact package hash drift: {generation_key}")
        return f"covers_ai_original/{basename}"

    cover_pre_overlay = cover_artifact("pre_overlay_path", "pre_overlay_sha256")
    cover_route_background = cover_artifact("ai_background", "ai_background_sha256")
    rendered_text_pixels = cover_generation.get("rendered_text_pixels")
    if not isinstance(rendered_text_pixels, dict):
        raise DailyManifestError("cover generation lacks rendered_text_pixels")
    mask_declared = rendered_text_pixels.get("mask_path")
    mask_expected = str(rendered_text_pixels.get("mask_sha256") or "").removeprefix("sha256:")
    if not isinstance(mask_declared, str) or not mask_declared or not mask_expected:
        raise DailyManifestError("cover generation lacks rendered text mask path/hash")
    mask_source = Path(mask_declared)
    mask_name = _safe_component(mask_source.name, label="cover title mask basename")
    mask_rel = str(Path(cover_pre_overlay).parent / mask_name)
    mask_path = _package_regular_file(package_root, mask_rel, label="cover title mask")
    package_matches = bool(mask_path is not None and _sha256(mask_path) == mask_expected)
    if not package_matches:
        mask_payload = _read_regular_source(mask_source, label="cover title mask")
        mask_actual = _sha256_bytes(mask_payload)
        if mask_actual != mask_expected:
            raise DailyManifestError(
                f"cover title mask source sha drift: record={mask_expected} actual={mask_actual}"
            )
        mask_path = _atomic_project_bytes(
            package_root=package_root,
            relative=mask_rel,
            payload=mask_payload,
            label="cover title mask",
        )
        if _sha256(mask_path) != mask_expected:
            raise DailyManifestError("cover title mask repair verification failed")

    # authorized_upload v3 的 same-stem 合同：video stem X 要求包根直下
    # X.record.json / X.srt / X.cover.png。装配为审定字节的副本（字节级
    # 相同，sha 与 chat/record 冻结值天然一致），manifest item 指向副本。
    upload_stem = burned.name.removesuffix(".mp4")
    upload_family = {
        f"{upload_stem}.record.json": record,
        f"{upload_stem}.srt": subtitle,
        f"{upload_stem}.cover.png": package_root / cover_rel,
    }
    for name, source_path in upload_family.items():
        payload = source_path.read_bytes()
        _atomic_project_bytes(
            package_root=package_root,
            relative=name,
            payload=payload,
            label="same-stem upload artifact",
        )

    item = {
        "id": candidate_id,
        "candidate_id": candidate_id,
        "stem": stem,
        "kind": lane,
        "classification": lane,
        "title": str(publish_doc.get("title") or ""),
        "subtitle_srt": f"{upload_stem}.srt",
        "publish_json": publish.name,
        "evidence_json": record.name,
        "video": burned.name,
        "cover": f"{upload_stem}.cover.png",
        "cover_pre_overlay": cover_pre_overlay,
        "cover_title_mask": mask_rel,
        "cover_route_background": cover_route_background,
        "record": f"{upload_stem}.record.json",
        "chat_authority": chat_name,
        "clip_context": clip_context_name,
        "ass_path": ass.name,
        "ass_sha256": _sha256(ass),
        # uniform_host 政策（ee29e08）：单说话人包的 speaker 面与正文同体；
        # finalization 的 final_speaker_srt_sha256 即正文 srt 的 sha（已核）。
        # speaker-finalized 包（uniform_fallback=False）没有这一同体前提，
        # 必须指向真正带 LDS/GUEST 标签的 speaker-final SRT，否则
        # review_package_ass_audit 的事件级 parity 复放会对着错误的文件跑。
        "speaker_srt": (f"{upload_stem}.srt" if uniform_fallback else speaker_srt_file.name),
        "speaker_srt_sha256": _sha256(subtitle if uniform_fallback else speaker_srt_file),
        **(
            {
                "speaker_finalization_manifest": (packaged_speaker_manifest.name),
                "speaker_finalization_manifest_sha256": (
                    "sha256:" + _sha256(packaged_speaker_manifest)
                ),
            }
            if packaged_speaker_manifest is not None
            else {}
        ),
        "sha256": {
            "subtitle_srt": _sha256(subtitle),
            "publish_json": _sha256(publish),
            "evidence_json": _sha256(record),
            "video": _sha256(burned),
            "cover": _sha256(package_root / cover_rel),
        },
    }
    attestation = {
        "candidate_id": candidate_id,
        "reference_sha256": cover_generation.get("reference_sha256"),
        "final_cover_sha256": cover_generation.get("final_cover_sha256"),
        "method": cover_generation.get("method"),
        "route_decision": cover_generation.get("route_decision"),
        "reference_authority": cover_generation.get("reference_authority"),
    }
    manifest = {
        "schema_version": "lidousha-daily-review-manifest.v1",
        "generated_by": "build_daily_review_manifest.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "date": str(state.get("date") or state_path.stem),
        "status": "review_ready",
        "batch_status": batch_status,
        "candidate_id": candidate_id,
        "deployed_commit": deployed_commit,
        "state_path": str(state_path),
        "run_mode": "PRODUCTION_REVIEW",
        # 审片包本身不授权上传；上传授权由 authorized_upload make-manifest
        # 的 --quote 层绑定 维护者 原话。
        "upload_allowed": False,
        # Sapphire72 车道显式声明 28 字合同（audit docstring 指定该车道必须
        # 显式声明，否则被旧手工包 18 字默认误拒）。
        "subtitle_visual_contract": {
            "max_visual_lines": 2,
            "max_chars_per_line": 28,
        },
        "cover_route_attestations": [attestation],
        "items": [item],
    }
    manifest.update(lane_manifest_contract_fields)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--deployed-commit-file", required=True, type=Path)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    try:
        manifest = build(args.package_root, args.state, args.deployed_commit_file, args.candidate)
    except DailyManifestError as exc:
        print(f"REFUSE: {exc}")
        return 2
    out = args.out or args.package_root / "review_manifest.json"
    out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"WROTE {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
