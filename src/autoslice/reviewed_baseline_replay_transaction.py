"""State-last transaction for a reviewed-baseline Talk package replay.

This is intentionally *not* a generic package importer.  A caller supplies a
sealed candidate-private after-image assembled by the reviewed-baseline lane;
this module owns only the no-follow CAS/journal/install protocol which makes
that image recoverable.  In particular it never invokes an LLM, uploader, or
cover generator.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.autoslice.qixi_transaction_core import (
    RunnerCommitLease,
    exclusive_runner_commit,
    require_runner_commit_lease,
)
from src.autoslice.runner_state_writeback import write_exact_state_bytes_under_lease


SCHEMA = "reviewed-baseline-package-replay-journal.v1"


class ReviewedBaselineReplayTransactionError(RuntimeError):
    """A replay after-image cannot be safely installed or resumed."""


@dataclass(frozen=True, slots=True)
class StreamBinding:
    path: Path
    sha256: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class ReplayArtifact:
    role: str
    staged: StreamBinding
    target: Path
    before: StreamBinding | None


@dataclass(frozen=True, slots=True)
class ReplayAfterImage:
    """Lane-owned sealed after-image; never constructed from CLI paths.

    ``record_before_sha256`` and ``stage_sha256`` bind this to the replay
    planner.  Keeping target discovery in the reviewed-baseline builder avoids
    turning the transaction core into an arbitrary state/artifact importer.
    """

    date: str
    candidate_id: str
    deployed: Mapping[str, str]
    state_path: Path
    state_before: bytes
    state_after: bytes
    record_before_sha256: str
    stage_sha256: str
    artifacts: tuple[ReplayArtifact, ...]
    upload_allowed: bool = False


def _canon(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _safe_dir(path: Path, *, create: bool = False, mode: int = 0o700) -> None:
    """Validate a complete no-symlink directory chain, optionally one leaf."""

    path = Path(path).absolute()
    cursor = Path(path.anchor)
    for index, part in enumerate(path.parts[1:], start=1):
        cursor /= part
        try:
            observed = os.lstat(cursor)
        except FileNotFoundError:
            if not create or index != len(path.parts) - 1:
                raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_PARENT_MISSING")
            os.mkdir(cursor, mode)
            observed = os.lstat(cursor)
        except OSError as exc:
            raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_PARENT_UNSAFE") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_PARENT_UNSAFE")
        if index == len(path.parts) - 1 and create and stat.S_IMODE(observed.st_mode) != mode:
            raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_PRIVATE_MODE_DRIFT")


def stream_binding(path: Path, *, label: str) -> StreamBinding | None:
    """Hash a regular file through a stable no-follow descriptor, streaming media."""

    path = Path(path).absolute()
    _safe_dir(path.parent)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReviewedBaselineReplayTransactionError(f"REPLAY_TXN_{label}_UNAVAILABLE") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ReviewedBaselineReplayTransactionError(f"REPLAY_TXN_{label}_UNSAFE")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReviewedBaselineReplayTransactionError(f"REPLAY_TXN_{label}_UNSAFE") from exc
    identity = (before.st_dev, before.st_ino, stat.S_IMODE(before.st_mode), before.st_size,
                before.st_mtime_ns, before.st_ctime_ns)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode), opened.st_size,
            opened.st_mtime_ns, opened.st_ctime_ns) != identity or not stat.S_ISREG(opened.st_mode):
            raise ReviewedBaselineReplayTransactionError(f"REPLAY_TXN_{label}_DRIFT")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        after_fd = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise ReviewedBaselineReplayTransactionError(f"REPLAY_TXN_{label}_DRIFT") from exc
    named = (after.st_dev, after.st_ino, stat.S_IMODE(after.st_mode), after.st_size,
             after.st_mtime_ns, after.st_ctime_ns)
    if named != identity or (after_fd.st_dev, after_fd.st_ino, stat.S_IMODE(after_fd.st_mode),
                             after_fd.st_size, after_fd.st_mtime_ns, after_fd.st_ctime_ns) != identity:
        raise ReviewedBaselineReplayTransactionError(f"REPLAY_TXN_{label}_DRIFT")
    return StreamBinding(path, "sha256:" + digest.hexdigest(), *identity)


def _matches(left: StreamBinding | None, right: StreamBinding | None) -> bool:
    return left == right


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_SHORT_WRITE")
        view = view[count:]


def _create(path: Path, payload: bytes) -> None:
    _safe_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_CREATE_COLLISION") from exc
    try:
        _write_all(fd, payload)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


def _replace(path: Path, payload: bytes) -> None:
    _safe_dir(path.parent)
    temporary = path.with_name(f".{path.name}.replay-tmp-{os.getpid()}")
    _create(temporary, payload)
    try:
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _binding_dict(value: StreamBinding | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"path": str(value.path), "sha256": value.sha256, "device": value.device,
            "inode": value.inode, "mode": value.mode, "size": value.size,
            "mtime_ns": value.mtime_ns, "ctime_ns": value.ctime_ns}


def _parse_binding(value: object, *, label: str) -> StreamBinding | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ReviewedBaselineReplayTransactionError(f"REPLAY_TXN_{label}_INVALID")
    expected = {"path", "sha256", "device", "inode", "mode", "size", "mtime_ns", "ctime_ns"}
    if set(value) != expected or not isinstance(value["path"], str) or not isinstance(value["sha256"], str):
        raise ReviewedBaselineReplayTransactionError(f"REPLAY_TXN_{label}_INVALID")
    integers = [value[key] for key in expected - {"path", "sha256"}]
    if any(isinstance(item, bool) or not isinstance(item, int) for item in integers):
        raise ReviewedBaselineReplayTransactionError(f"REPLAY_TXN_{label}_INVALID")
    return StreamBinding(Path(value["path"]), value["sha256"], value["device"], value["inode"],
                         value["mode"], value["size"], value["mtime_ns"], value["ctime_ns"])


def _journal_path(runtime_root: Path, *, date: str, candidate_id: str, seed: str) -> Path:
    root = Path(runtime_root) / ".reviewed-baseline-replay-journal"
    return root / f"{date}-{candidate_id}-{seed}.json"


def _read_journal(path: Path) -> dict[str, Any]:
    observed = stream_binding(path, label="JOURNAL")
    if observed is None or observed.mode != 0o600 or observed.size > 4 * 1024 * 1024:
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_JOURNAL_INVALID")
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            current = os.fstat(fd)
            if (current.st_dev, current.st_ino, stat.S_IMODE(current.st_mode), current.st_size,
                current.st_mtime_ns, current.st_ctime_ns) != (
                    observed.device, observed.inode, observed.mode, observed.size,
                    observed.mtime_ns, observed.ctime_ns,
                ):
                raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_JOURNAL_DRIFT")
            chunks: list[bytes] = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
        finally:
            os.close(fd)
        payload = b"".join(chunks)
        if _sha(payload) != observed.sha256 or stream_binding(path, label="JOURNAL") != observed:
            raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_JOURNAL_DRIFT")
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_JOURNAL_INVALID") from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA:
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_JOURNAL_INVALID")
    seal = document.pop("journal_sha256", None)
    if seal != _sha(_canon(document)):
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_JOURNAL_SEAL_DRIFT")
    document["journal_sha256"] = seal
    return document


def _seal(document: dict[str, Any]) -> bytes:
    body = dict(document)
    body.pop("journal_sha256", None)
    body["journal_sha256"] = _sha(_canon(body))
    return _canon(body)


def _prepare_artifacts(*, stage_root: Path, targets: Mapping[str, Path]) -> tuple[ReplayArtifact, ...]:
    """Seal each private staged artifact to a pre-existing public target binding."""

    if not targets or len(set(targets)) != len(targets):
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_ARTIFACT_ROLES_INVALID")
    values: list[ReplayArtifact] = []
    seen: set[Path] = set()
    for role, target in sorted(targets.items()):
        if not isinstance(role, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", role) is None or Path(target) in seen:
            raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_ARTIFACT_TARGET_INVALID")
        seen.add(Path(target))
        staged = stream_binding(Path(stage_root) / role, label="STAGED")
        if staged is None:
            raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_STAGED_ARTIFACT_MISSING")
        values.append(ReplayArtifact(role, staged, Path(target).absolute(), stream_binding(Path(target), label="TARGET")))
    return tuple(values)


def _journal_document(*, date: str, candidate_id: str, deployed: Mapping[str, str],
                      state_path: Path, state_before: bytes, state_after: bytes,
                      artifacts: Sequence[ReplayArtifact]) -> dict[str, Any]:
    def role_order(artifact: ReplayArtifact) -> tuple[int, str]:
        # Delivery bytes/sidecars first; public mirrors then record are only
        # usable after those bytes exist; exact state remains last below.
        role = artifact.role
        if role in {"record.json", "delivery-record.json"}:
            return (3, role)
        if "publish" in role or "delivery" in role:
            return (2, role)
        return (1, role)
    entries = [{"role": artifact.role, "staged": _binding_dict(artifact.staged),
                "target": str(artifact.target), "before": _binding_dict(artifact.before),
                "installed": False} for artifact in sorted(artifacts, key=role_order)]
    document: dict[str, Any] = {
        "schema_version": SCHEMA, "status": "PREPARED", "date": date,
        "candidate_id": candidate_id, "deployed": dict(deployed),
        "state_path": str(state_path), "state_before_sha256": _sha(state_before),
        "state_after_sha256": _sha(state_after), "state_after_utf8": state_after.decode("utf-8"),
        "artifacts": entries, "upload_allowed": False,
    }
    seed = _sha(_canon(document)).removeprefix("sha256:")
    document["seed"] = seed
    return document


def _assert_deployed(runtime_root: Path, deployed: Mapping[str, str]) -> None:
    for name in ("commit", "authority_manifest_sha256"):
        expected = deployed.get(name)
        if not isinstance(expected, str):
            raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_DEPLOYED_INVALID")
    commit = stream_binding(Path(runtime_root) / "repo" / "DEPLOYED_COMMIT", label="DEPLOYED_COMMIT")
    manifest = stream_binding(Path(runtime_root) / "repo" / "DEPLOYED_AUTHORITY_MANIFEST", label="DEPLOYED_AUTHORITY")
    if commit is None or manifest is None or commit.sha256 != deployed["commit"] or manifest.sha256 != deployed["authority_manifest_sha256"]:
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_DEPLOYED_DRIFT")


def commit_replay(
    *, runtime_root: Path, date: str, candidate_id: str, deployed: Mapping[str, str],
    state_path: Path, state_before: bytes, state_after: bytes,
    artifacts: Sequence[ReplayArtifact], fail_after_role: str | None = None,
) -> Path:
    """Install a fully staged candidate under one short runner lease.

    The journal is only created after all independent preimages have been
    re-read.  Each target rename has a durable checkpoint; a crash is resumed
    by :func:`resume_replay` before any future provider work.
    """

    if not artifacts or not all(not artifact.role.startswith("upload") for artifact in artifacts):
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_UPLOAD_FORBIDDEN")
    document = _journal_document(date=date, candidate_id=candidate_id, deployed=deployed,
                                 state_path=state_path, state_before=state_before,
                                 state_after=state_after, artifacts=artifacts)
    journal = _journal_path(runtime_root, date=date, candidate_id=candidate_id, seed=document["seed"])
    with exclusive_runner_commit(Path(runtime_root)) as lease:
        _assert_deployed(Path(runtime_root), deployed)
        from src.autoslice.runner_state_writeback import read_exact_state_preimage
        if read_exact_state_preimage(state_path, runtime_root=Path(runtime_root)) != state_before:
            raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_STATE_DRIFT")
        for artifact in artifacts:
            if stream_binding(artifact.staged.path, label="STAGED") != artifact.staged:
                raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_STAGED_DRIFT")
            if stream_binding(artifact.target, label="TARGET") != artifact.before:
                raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_TARGET_DRIFT")
            _safe_dir(artifact.target.parent)
            # Rename is the only install primitive.  A stage on another
            # device would force copy-and-overwrite semantics, so reject it
            # before the journal exists; the lane builder must use a
            # candidate-private sibling on the delivery filesystem.
            if artifact.staged.device != os.stat(artifact.target.parent).st_dev:
                raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_STAGE_FILESYSTEM_DRIFT")
        # No formal journal namespace is created until every independently
        # observable preimage has passed under the same lease.
        _safe_dir(journal.parent, create=True)
        _create(journal, _seal(document))
        _install_document(journal, document, runtime_root=Path(runtime_root), lease=lease,
                          fail_after_role=fail_after_role)
    return journal


def commit_prepared_after_image(*, runtime_root: Path, after: ReplayAfterImage,
                                fail_after_role: str | None = None) -> Path:
    """Commit only a candidate-specific after-image built inside this lane."""

    if after.upload_allowed is not False:
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_UPLOAD_FORBIDDEN")
    if not after.artifacts or len({artifact.role for artifact in after.artifacts}) != len(after.artifacts):
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_AFTER_IMAGE_INVALID")
    return commit_replay(
        runtime_root=runtime_root, date=after.date, candidate_id=after.candidate_id,
        deployed=after.deployed, state_path=after.state_path,
        state_before=after.state_before, state_after=after.state_after,
        artifacts=after.artifacts, fail_after_role=fail_after_role,
    )


def _install_document(journal: Path, document: dict[str, Any], *, runtime_root: Path,
                      lease: RunnerCommitLease, fail_after_role: str | None = None) -> None:
    require_runner_commit_lease(lease, runtime_root=runtime_root)
    if document.get("status") not in {"PREPARED", "INSTALLING"}:
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_JOURNAL_STATUS_INVALID")
    document["status"] = "INSTALLING"
    _replace(journal, _seal(document))
    for entry in document["artifacts"]:
        if not isinstance(entry, dict):
            raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_ENTRY_INVALID")
        staged = _parse_binding(entry.get("staged"), label="STAGED")
        before = _parse_binding(entry.get("before"), label="BEFORE")
        target = Path(str(entry.get("target") or ""))
        if staged is None or not target.is_absolute():
            raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_ENTRY_INVALID")
        current = stream_binding(target, label="TARGET")
        if entry.get("installed") is True:
            if current is None or (current.device, current.inode, current.sha256, current.mode) != (staged.device, staged.inode, staged.sha256, staged.mode):
                raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_CHECKPOINT_DRIFT")
            continue
        if current is not None and (current.device, current.inode) == (staged.device, staged.inode):
            installed = current
        else:
            if current != before or stream_binding(staged.path, label="STAGED") != staged:
                raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_ARTIFACT_DRIFT")
            os.replace(staged.path, target)
            _fsync_dir(target.parent)
            installed = stream_binding(target, label="TARGET")
            if installed is None or (installed.device, installed.inode, installed.sha256, installed.mode) != (staged.device, staged.inode, staged.sha256, staged.mode):
                raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_INSTALL_DRIFT")
        entry["installed"] = True
        entry["installed_binding"] = _binding_dict(installed)
        _replace(journal, _seal(document))
        if entry["role"] == fail_after_role:
            raise RuntimeError("REPLAY_TXN_INJECTED_CRASH")
    state_before = document["state_before_sha256"]
    state_after = str(document["state_after_utf8"]).encode("utf-8")
    from src.autoslice.runner_state_writeback import read_exact_state_preimage
    current_state = read_exact_state_preimage(Path(document["state_path"]), runtime_root=runtime_root)
    if _sha(current_state or b"") == state_after:
        pass
    elif _sha(current_state or b"") == state_before:
        write_exact_state_bytes_under_lease(Path(document["state_path"]), runtime_root=runtime_root,
                                            lease=lease, expected_before=current_state, after_bytes=state_after)
    else:
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_STATE_DRIFT")
    document["status"] = "COMMITTED"
    _replace(journal, _seal(document))


def resume_replay(*, runtime_root: Path, journal: Path) -> None:
    """Provider-free recovery of one known replay journal."""

    document = _read_journal(journal)
    if document.get("status") == "COMMITTED":
        return
    if document.get("status") not in {"PREPARED", "INSTALLING"}:
        raise ReviewedBaselineReplayTransactionError("REPLAY_TXN_JOURNAL_STATUS_INVALID")
    with exclusive_runner_commit(Path(runtime_root)) as lease:
        _assert_deployed(Path(runtime_root), document.get("deployed") if isinstance(document.get("deployed"), Mapping) else {})
        _install_document(journal, document, runtime_root=Path(runtime_root), lease=lease)
