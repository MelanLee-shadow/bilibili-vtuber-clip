#!/usr/bin/env python3
"""Apply the sealed no-provider source-fact refresh for one fixed candidate.

This is a tightly scoped repair transaction, not a producer rerun.  It changes
only the source-fact receipt mirrors and their state projection.  Media,
subtitles, ASS, cover, boundary evidence and every other StoryContract member
must remain byte-for-byte unchanged.
"""

from __future__ import annotations

import argparse
import base64
import copy
import fcntl
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.authorized_upload import DEFAULT_UPLOAD_LOCK, UploadLockBusy, exclusive_upload_lock
from scripts.session_autoslice import (
    BASE,
    _active_cover_documents,
    delivered_paths,
)
from src.autoslice.candidate_source_fact_refresh import (
    CANDIDATE_ID,
    CandidateSourceFactRefreshError,
    build_candidate_public_text_source_fact_refresh_review,
    load_candidate_source_fact_refresh_preimages,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.source_fact_review import validate_source_fact_review


class CandidateSourceFactRefreshRunError(RuntimeError):
    pass


TRANSACTION_SCHEMA = "candidate-public-text-source-fact-refresh-transaction.v1"


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_sha(value: object) -> str:
    return _sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat(follow_symlinks=False).st_mode) and not path.is_symlink()
    except OSError:
        return False


def _regular_no_symlink_ancestors(path: Path) -> bool:
    """Require a regular file and a physical, non-symlink locator chain."""

    try:
        absolute = path.absolute()
        cursor = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            cursor = cursor / part
            if cursor.is_symlink():
                return False
    except OSError:
        return False
    return _regular(path)


def _record_map(state: Mapping[str, object]) -> dict[str, dict[str, Any]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    for lane in ("picks", "songs"):
        for value in state.get(lane, []) if isinstance(state.get(lane, []), list) else []:
            if isinstance(value, dict):
                rows.setdefault(str(value.get("candidate_id") or ""), []).append(value)
    if not rows.get(CANDIDATE_ID):
        raise CandidateSourceFactRefreshRunError("refresh candidate is absent from state")
    if len(rows[CANDIDATE_ID]) != 1:
        raise CandidateSourceFactRefreshRunError("refresh candidate is not unique in state")
    return {candidate_id: values[0] for candidate_id, values in rows.items()}


def _transcript(path: Path) -> str:
    try:
        cues = parse_srt_cues(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CandidateSourceFactRefreshRunError("reviewed SRT is unreadable") from exc
    value = "\n".join(cue.text.strip() for cue in cues if cue.text.strip())
    if not value:
        raise CandidateSourceFactRefreshRunError("reviewed SRT transcript is empty")
    return value


def _active_documents(*, date: str, record: Mapping[str, Any]) -> list[tuple[Path, dict[str, Any]]]:
    paths = delivered_paths(date, dict(record))
    if paths is None:
        raise CandidateSourceFactRefreshRunError("refresh candidate has no frozen delivery")
    media, _cover = paths
    active = _active_cover_documents(
        date=date,
        candidate_id=CANDIDATE_ID,
        title=str(record.get("title") or ""),
        mp4=media,
        media_sha256=_file_sha(media),
    )
    if len(active) != 3 or len({path.resolve(strict=True) for path, _doc in active}) != 3:
        raise CandidateSourceFactRefreshRunError("refresh requires exactly three active publish documents")
    for path, _document in active:
        if not _regular_no_symlink_ancestors(path):
            raise CandidateSourceFactRefreshRunError("refresh active document is unsafe")
    return active


def _view(document: Mapping[str, Any]) -> dict[str, Any]:
    if document.get("schema_version") == "shadow-publish-draft.v1":
        return dict(document)
    value = document.get("publish_staging")
    if not isinstance(value, dict):
        raise CandidateSourceFactRefreshRunError("refresh record lacks publish staging")
    return value


def _old_receipt(documents: list[tuple[Path, dict[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    receipts: list[object] = []
    for _path, document in documents:
        if document.get("schema_version") == "shadow-publish-draft.v1":
            receipts.append(document.get("source_fact_review"))
            continue
        story = document.get("story_contract")
        if not isinstance(story, dict):
            raise CandidateSourceFactRefreshRunError("refresh record lacks StoryContract")
        contracts.append(story)
        receipts.extend((story.get("source_fact_review"), _view(document).get("source_fact_review")))
    if len(contracts) != 2 or _canonical_sha(contracts[0]) != _canonical_sha(contracts[1]):
        raise CandidateSourceFactRefreshRunError("refresh StoryContract mirrors drift")
    if not all(isinstance(value, dict) for value in receipts) or any(
        value != receipts[0] for value in receipts[1:]
    ):
        raise CandidateSourceFactRefreshRunError("refresh old source-fact receipt mirrors drift")
    return contracts[0], copy.deepcopy(receipts[0])


def _project_document(document: Mapping[str, Any], receipt: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(document))
    if result.get("schema_version") == "shadow-publish-draft.v1":
        result["source_fact_review"] = copy.deepcopy(dict(receipt))
        return result
    story = result.get("story_contract")
    stage = result.get("publish_staging")
    if not isinstance(story, dict) or not isinstance(stage, dict):
        raise CandidateSourceFactRefreshRunError("refresh document view is incomplete")
    story["source_fact_review"] = copy.deepcopy(dict(receipt))
    stage["source_fact_review"] = copy.deepcopy(dict(receipt))
    return result


def prepare_refresh(
    *, date: str, state: Mapping[str, Any]
) -> tuple[dict[str, Any], list[tuple[Path, bytes, bytes]], dict[str, Any]]:
    """Validate every input and return intended state/doc bytes without writing."""

    record = _record_map(state)[CANDIDATE_ID]
    try:
        preimages = load_candidate_source_fact_refresh_preimages(repo_root=ROOT)
    except CandidateSourceFactRefreshError as exc:
        raise CandidateSourceFactRefreshRunError("refresh sealed preimages are unavailable") from exc
    if _canonical_sha(record) != preimages.get("state_record_canonical_sha256"):
        raise CandidateSourceFactRefreshRunError("refresh state record preimage drifted")
    documents = _active_documents(date=date, record=record)
    expected_docs = preimages.get("documents")
    expected_by_path = {
        Path(str(row.get("path") or "")): row for row in expected_docs if isinstance(row, dict)
    } if isinstance(expected_docs, list) else {}
    try:
        documents_match = all(
            _matches_sealed_target(
                path, descriptor=expected_by_path[path], payload=path.read_bytes()
            )
            for path, _document in documents
        )
    except OSError:
        documents_match = False
    if (
        len(expected_by_path) != 3
        or set(expected_by_path) != {path for path, _document in documents}
        or not documents_match
    ):
        raise CandidateSourceFactRefreshRunError("refresh active document preimages drifted")
    contract, historical = _old_receipt(documents)
    record_document = next(
        (document for _path, document in documents if document.get("schema_version") != "shadow-publish-draft.v1"),
        None,
    )
    if not isinstance(record_document, dict):
        raise CandidateSourceFactRefreshRunError("refresh record document is missing")
    subtitle = Path(str(record_document.get("subtitle_path") or ""))
    if not _regular_no_symlink_ancestors(subtitle):
        raise CandidateSourceFactRefreshRunError("refresh subtitle path is unsafe")
    title = str(record.get("title") or "")
    if any(_view(document).get("title") != title for _path, document in documents):
        raise CandidateSourceFactRefreshRunError("refresh title mirrors drift")
    speaker_evidence = historical.get("speaker_evidence")
    if not isinstance(speaker_evidence, dict):
        raise CandidateSourceFactRefreshRunError("refresh historical speaker evidence is missing")
    try:
        receipt = build_candidate_public_text_source_fact_refresh_review(
            repo_root=ROOT,
            historical_provider_receipt=historical,
            candidate_id=CANDIDATE_ID,
            selection_hook=str(contract.get("selection_hook") or ""),
            title=title,
            final_transcript=_transcript(subtitle),
            clip_context_prompt=str(contract.get("clip_context_prompt") or ""),
            selection_scorecard=contract.get("selection_scorecard"),
            story_contract=contract,
            final_reviewed_srt_path=subtitle,
            speaker_evidence=speaker_evidence,
        )
    except (CandidateSourceFactRefreshError, OSError, ValueError) as exc:
        raise CandidateSourceFactRefreshRunError("refresh authority rejected before write") from exc
    intended: list[tuple[Path, bytes, bytes]] = []
    for path, document in documents:
        before = path.read_bytes()
        after = (json.dumps(_project_document(document, receipt), ensure_ascii=False, indent=2) + "\n").encode()
        intended.append((path, before, after))
    post_document = _project_document(record_document, receipt)
    post_contract = post_document.get("story_contract")
    if not isinstance(post_contract, dict) or not validate_source_fact_review(
        receipt,
        selection_hook=str(contract.get("selection_hook") or ""),
        title=title,
        final_transcript=_transcript(subtitle),
        clip_context_prompt=str(contract.get("clip_context_prompt") or ""),
        selection_scorecard=contract.get("selection_scorecard"),
        story_contract=post_contract,
        candidate_id=CANDIDATE_ID,
        final_reviewed_srt_path=subtitle,
        speaker_evidence=speaker_evidence,
    ):
        raise CandidateSourceFactRefreshRunError("refresh receipt failed post-projection canonical validation")
    updated = copy.deepcopy(dict(state))
    updated_record = _record_map(updated)[CANDIDATE_ID]
    updated_record["source_fact_review"] = copy.deepcopy(receipt)
    updated_record["source_fact_review_sha256"] = receipt["receipt_sha256"]
    updated_record["source_fact_review_refresh"] = {
        "schema_version": "candidate-public-text-source-fact-refresh-state.v1",
        "status": "REFRESHED_NO_PROVIDER",
        "authority_candidate_id": CANDIDATE_ID,
        "receipt_sha256": receipt["receipt_sha256"],
        "upload_enabled": False,
    }
    return updated, intended, receipt


def _transaction_root(date: str) -> Path:
    return BASE / "out" / date / CANDIDATE_ID / ".source-fact-refresh-private" / "transaction"


def _state_path(date: str) -> Path:
    return BASE / "state" / f"{date}.json"


_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_ENTRY_KEYS = {
    "target", "before_sha256", "after_sha256",
    "before_bytes_b64", "after_bytes_b64", "before_mode", "after_mode", "before_uid", "before_gid", "after_uid", "after_gid",
}
_JOURNAL_KEYS = {
    "schema_version", "status", "candidate_id", "date", "upload_enabled",
    "authority_sha256", "source_fact_review", "source_fact_review_sha256",
    "sealed_targets_sha256", "entries", "transaction_receipt", "journal_sha256",
}
_TRANSACTION_RECEIPT_KEYS = {
    "schema_version", "status", "candidate_id", "date", "authority_sha256",
    "source_fact_review_sha256", "sealed_targets_sha256", "entries_sha256", "receipt_sha256",
}
_TARGET_TEMP_FSYNC_HOOK = None  # Test-only interruption seam; production remains None.
_FINAL_RECEIPT_PREWRITE_HOOK = None  # Test-only drift seam; production remains None.
_PRIVATE_PENDING_PARTIAL_HOOK = None  # Test-only interruption seam; production remains None.
_PRIVATE_PUBLICATION_BEFORE_PUBLISH_HOOK = None  # Test-only interruption seam; production remains None.
_PRIVATE_PUBLICATION_AFTER_PUBLISH_HOOK = None  # Test-only interruption seam; production remains None.


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _private(path: Path, *, directory: bool) -> bool:
    try:
        value = os.lstat(path)
    except OSError:
        return False
    return (
        (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))
        and not path.is_symlink()
        and value.st_uid == os.geteuid()
        and stat.S_IMODE(value.st_mode) == (_PRIVATE_DIR_MODE if directory else _PRIVATE_FILE_MODE)
    )


def _pending_path(path: Path) -> Path:
    return path.with_name(path.name + ".pending")


def _pending_payload(path: Path, payload: bytes) -> bytes:
    del path
    return payload


def _pending_is_owned_recoverable(path: Path, *, target: Path, payload: bytes, target_mode: int, target_uid: int, target_gid: int, target_exists: bool) -> bool:
    """A pending file is mutable only after its sealed header identifies this target."""

    try:
        observed = os.lstat(path)
        raw = path.read_bytes()
    except OSError:
        return False
    expected_mode = target_mode if target_exists else _PRIVATE_FILE_MODE
    expected_uid = target_uid if target_exists else os.geteuid()
    expected_gid = target_gid if target_exists else os.getegid()
    return (
        not path.is_symlink() and stat.S_ISREG(observed.st_mode)
        and observed.st_uid == expected_uid and observed.st_gid == expected_gid
        and stat.S_IMODE(observed.st_mode) == expected_mode
        and payload.startswith(raw)
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_or_recover_pending(path: Path, *, target: Path, payload: bytes) -> None:
    """Create or complete a marker-bound private pending artifact, never a foreign one."""

    expected = _pending_payload(target, payload)
    if path.exists() or path.is_symlink():
        if not _pending_is_owned_recoverable(
            path, target=target, payload=payload, target_mode=_PRIVATE_FILE_MODE,
            target_uid=os.geteuid(), target_gid=os.getegid(), target_exists=False,
        ):
            raise CandidateSourceFactRefreshRunError("refresh private pending artifact is foreign or drifted")
        if path.read_bytes() == expected:
            return
        flags = os.O_WRONLY | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, _PRIVATE_FILE_MODE)
    try:
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.geteuid():
            raise CandidateSourceFactRefreshRunError("refresh private pending artifact is unsafe")
        with os.fdopen(descriptor, "wb") as handle:
            split = min(1, len(payload))
            handle.write(payload[:split])
            handle.flush()
            os.fsync(handle.fileno())
            if _PRIVATE_PENDING_PARTIAL_HOOK is not None:
                _PRIVATE_PENDING_PARTIAL_HOOK(path)
            handle.write(payload[split:])
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    if not _pending_is_owned_recoverable(
        path, target=target, payload=payload, target_mode=_PRIVATE_FILE_MODE,
        target_uid=os.geteuid(), target_gid=os.getegid(), target_exists=False,
    ) or path.read_bytes() != expected:
        raise CandidateSourceFactRefreshRunError("refresh private pending artifact verification failed")


def _create_private_bytes(path: Path, payload: bytes, *, mode: int = _PRIVATE_FILE_MODE, uid: int | None = None, gid: int | None = None) -> None:
    """Publish bytes by hard-linking a verified pending file into an absent final path."""

    uid = os.geteuid() if uid is None else uid
    gid = os.getegid() if gid is None else gid
    pending = _pending_path(path)
    if path.exists() or path.is_symlink():
        try:
            observed = os.lstat(path)
            matches = (
                not path.is_symlink() and stat.S_ISREG(observed.st_mode)
                and observed.st_uid == uid and observed.st_gid == gid
                and stat.S_IMODE(observed.st_mode) == mode and path.read_bytes() == payload
            )
        except OSError:
            matches = False
        if not matches:
            raise CandidateSourceFactRefreshRunError("refresh final private artifact is foreign or drifted")
        if pending.exists() or pending.is_symlink():
            if not _pending_is_owned_recoverable(
                pending, target=path, payload=payload, target_mode=mode,
                target_uid=uid, target_gid=gid, target_exists=True,
            ):
                raise CandidateSourceFactRefreshRunError("refresh published pending artifact is foreign or drifted")
            pending.unlink()
        return
    _write_or_recover_pending(pending, target=path, payload=payload)
    if _PRIVATE_PUBLICATION_BEFORE_PUBLISH_HOOK is not None:
        _PRIVATE_PUBLICATION_BEFORE_PUBLISH_HOOK(path)
    try:
        os.link(pending, path, follow_symlinks=False)
    except FileExistsError as exc:
        raise CandidateSourceFactRefreshRunError("refresh final private artifact raced publication") from exc
    try:
        os.chown(path, uid, gid)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
        observed = os.lstat(path)
        if (
            path.is_symlink() or not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != uid or observed.st_gid != gid
            or stat.S_IMODE(observed.st_mode) != mode or path.read_bytes() != payload
        ):
            raise CandidateSourceFactRefreshRunError("refresh private artifact publication verification failed")
        if _PRIVATE_PUBLICATION_AFTER_PUBLISH_HOOK is not None:
            _PRIVATE_PUBLICATION_AFTER_PUBLISH_HOOK(path)
    except BaseException:
        raise
    pending.unlink()
    _fsync_directory(path.parent)


def _install_target_bytes(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
    expected_before_sha256: str,
    expected_before_mode: int,
    expected_before_uid: int,
    expected_before_gid: int,
    temporary: Path,
) -> None:
    """Atomically replace one sealed target without changing its metadata."""

    _create_private_bytes(temporary, payload, mode=mode, uid=uid, gid=gid)
    if _TARGET_TEMP_FSYNC_HOOK is not None:
        _TARGET_TEMP_FSYNC_HOOK(temporary)
    before = os.lstat(path)
    if (
        path.is_symlink() or not stat.S_ISREG(before.st_mode)
        or _file_sha(path) != expected_before_sha256 or stat.S_IMODE(before.st_mode) != expected_before_mode
        or before.st_uid != expected_before_uid or before.st_gid != expected_before_gid
    ):
        raise CandidateSourceFactRefreshRunError("refresh target changed before install")
    os.replace(temporary, path)
    target = os.lstat(path)
    if (
        not stat.S_ISREG(target.st_mode)
        or path.is_symlink()
        or target.st_uid != uid
        or target.st_gid != gid
        or stat.S_IMODE(target.st_mode) != mode
        or path.read_bytes() != payload
    ):
        raise CandidateSourceFactRefreshRunError("refresh target installer verification failed")


def _safe_transaction_root(root: Path) -> None:
    scope = BASE / "out"
    if not scope.is_absolute() or not scope.is_dir() or scope.is_symlink():
        raise CandidateSourceFactRefreshRunError("refresh transaction scope is unsafe")
    try:
        relative = root.relative_to(scope)
    except ValueError as exc:
        raise CandidateSourceFactRefreshRunError("refresh transaction root escapes runtime scope") from exc
    cursor = scope
    for part in relative.parts[:-2]:
        cursor = cursor / part
        if not cursor.exists() or cursor.is_symlink() or not cursor.is_dir() or os.lstat(cursor).st_uid != os.geteuid():
            raise CandidateSourceFactRefreshRunError("refresh transaction parent is unsafe")
    private_parent = root.parent
    if (private_parent.exists() or private_parent.is_symlink()) and not _private(private_parent, directory=True):
        raise CandidateSourceFactRefreshRunError("refresh transaction private parent is unsafe")
    if not private_parent.exists() and not private_parent.is_symlink():
        private_parent.mkdir(mode=_PRIVATE_DIR_MODE)
    if not _private(private_parent, directory=True):
        raise CandidateSourceFactRefreshRunError("refresh transaction private parent is unsafe")
    if (root.exists() or root.is_symlink()) and not _private(root, directory=True):
        raise CandidateSourceFactRefreshRunError("refresh transaction root is unsafe")
    if not root.exists() and not root.is_symlink():
        root.mkdir(mode=_PRIVATE_DIR_MODE)
    if not _private(root, directory=True):
        raise CandidateSourceFactRefreshRunError("refresh transaction root is unsafe")
    if root.is_symlink() or not root.resolve(strict=True).is_relative_to(scope.resolve(strict=True)):
        raise CandidateSourceFactRefreshRunError("refresh transaction root escapes runtime scope")


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _unb64(value: object) -> bytes:
    if not isinstance(value, str):
        raise CandidateSourceFactRefreshRunError("refresh transaction payload is invalid")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except ValueError as exc:
        raise CandidateSourceFactRefreshRunError("refresh transaction payload is invalid") from exc


def _seal_hash(value: object) -> str:
    return _canonical_sha(value)


def _sealed_target_descriptors(date: str) -> dict[Path, dict[str, Any]]:
    try:
        preimages = load_candidate_source_fact_refresh_preimages(repo_root=ROOT)
    except CandidateSourceFactRefreshError as exc:
        raise CandidateSourceFactRefreshRunError("refresh sealed preimages are unavailable") from exc
    documents = preimages.get("documents")
    state_file = preimages.get("state_file")
    if not isinstance(documents, list) or not isinstance(state_file, dict):
        raise CandidateSourceFactRefreshRunError("refresh sealed document targets are unavailable")
    descriptors = {Path(str(row.get("path") or "")): dict(row) for row in documents if isinstance(row, dict)}
    descriptors[Path(str(state_file.get("path") or ""))] = dict(state_file)
    if len(descriptors) != 4 or any(not path.is_absolute() for path in descriptors) or _state_path(date) not in descriptors:
        raise CandidateSourceFactRefreshRunError("refresh sealed target set is invalid")
    return descriptors


def _sealed_target_paths(date: str) -> set[Path]:
    return set(_sealed_target_descriptors(date))


def _matches_sealed_target(path: Path, *, descriptor: Mapping[str, Any], payload: bytes) -> bool:
    try:
        value = os.lstat(path)
    except OSError:
        return False
    try:
        return (
            stat.S_ISREG(value.st_mode)
            and not path.is_symlink()
            and payload == path.read_bytes()
            and _sha256(payload) == descriptor.get("sha256")
            and len(payload) == descriptor.get("bytes")
            and value.st_uid == descriptor.get("uid")
            and value.st_gid == descriptor.get("gid")
            and stat.S_IMODE(value.st_mode) == descriptor.get("mode")
            and descriptor.get("type") == "regular"
        )
    except OSError:
        return False


def _journal_bytes(journal: Mapping[str, Any]) -> bytes:
    return (json.dumps(journal, ensure_ascii=False, indent=2) + "\n").encode()


def _signed_receipt(*, status: str, authority_sha256: str, receipt_sha256: str, targets_sha256: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    result = {
        "schema_version": "candidate-public-text-source-fact-refresh-transaction-receipt.v1",
        "status": status, "candidate_id": CANDIDATE_ID, "date": "2026-08-11",
        "authority_sha256": authority_sha256, "source_fact_review_sha256": receipt_sha256,
        "sealed_targets_sha256": targets_sha256, "entries_sha256": _seal_hash(entries),
    }
    result["receipt_sha256"] = _seal_hash(result)
    return result


def _signed_journal(journal: dict[str, Any]) -> dict[str, Any]:
    unsigned = copy.deepcopy(journal)
    unsigned.pop("journal_sha256", None)
    journal["journal_sha256"] = _seal_hash(unsigned)
    return journal




def _postcommit_validate(*, journal: Mapping[str, Any]) -> None:
    """Rebuild the canonical source-fact gate from the committed four targets."""

    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise CandidateSourceFactRefreshRunError("refresh transaction entries are invalid")
    state_path = _state_path("2026-08-11")
    documents: list[tuple[Path, dict[str, Any]]] = []
    state: dict[str, Any] | None = None
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise CandidateSourceFactRefreshRunError("refresh transaction entry is invalid")
        target = Path(str(entry.get("target") or ""))
        if not _regular_no_symlink_ancestors(target):
            raise CandidateSourceFactRefreshRunError("refresh committed target is unsafe")
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise CandidateSourceFactRefreshRunError("refresh committed target is unreadable") from exc
        if not isinstance(payload, dict):
            raise CandidateSourceFactRefreshRunError("refresh committed target is invalid")
        if target == state_path:
            state = payload
        else:
            documents.append((target, payload))
    if state is None or len(documents) != 3:
        raise CandidateSourceFactRefreshRunError("refresh committed target roles drifted")
    contract, receipt = _old_receipt(documents)
    record = _record_map(state)[CANDIDATE_ID]
    record_document = next(
        (document for _path, document in documents if document.get("schema_version") != "shadow-publish-draft.v1"),
        None,
    )
    if not isinstance(record_document, dict):
        raise CandidateSourceFactRefreshRunError("refresh committed record document is missing")
    subtitle = Path(str(record_document.get("subtitle_path") or ""))
    speaker_evidence = receipt.get("historical_provider_receipt", {}).get("speaker_evidence")
    if (
        not _regular_no_symlink_ancestors(subtitle)
        or not isinstance(speaker_evidence, dict)
        or record.get("source_fact_review") != receipt
        or record.get("source_fact_review_sha256") != receipt.get("receipt_sha256")
        or any(_view(document).get("title") != record.get("title") for _path, document in documents)
        or not validate_source_fact_review(
            receipt,
            selection_hook=str(contract.get("selection_hook") or ""),
            title=str(record.get("title") or ""),
            final_transcript=_transcript(subtitle),
            clip_context_prompt=str(contract.get("clip_context_prompt") or ""),
            selection_scorecard=contract.get("selection_scorecard"),
            story_contract=contract,
            candidate_id=CANDIDATE_ID,
            final_reviewed_srt_path=subtitle,
            speaker_evidence=speaker_evidence,
        )
    ):
        raise CandidateSourceFactRefreshRunError("refresh committed post-image failed canonical validation")


def _validate_journal(journal: object, *, root: Path) -> dict[str, Any]:
    if not isinstance(journal, dict) or set(journal) != _JOURNAL_KEYS:
        raise CandidateSourceFactRefreshRunError("refresh transaction journal schema drifted")
    unsigned = copy.deepcopy(journal)
    claimed = unsigned.pop("journal_sha256", None)
    if not isinstance(claimed, str) or _seal_hash(unsigned) != claimed:
        raise CandidateSourceFactRefreshRunError("refresh transaction journal hash drifts")
    entries = journal.get("entries")
    receipt = journal.get("source_fact_review")
    transaction_receipt = journal.get("transaction_receipt")
    if not (
        journal.get("schema_version") == TRANSACTION_SCHEMA
        and journal.get("candidate_id") == CANDIDATE_ID
        and journal.get("date") == "2026-08-11"
        and journal.get("upload_enabled") is False
        and journal.get("status") == "PREPARED"
        and isinstance(entries, list) and len(entries) == 4
        and isinstance(receipt, dict)
        and receipt.get("receipt_sha256") == journal.get("source_fact_review_sha256")
        and isinstance(transaction_receipt, dict) and set(transaction_receipt) == _TRANSACTION_RECEIPT_KEYS
    ):
        raise CandidateSourceFactRefreshRunError("refresh transaction journal identity drifts")
    targets = _sealed_target_paths("2026-08-11")
    targets_sha = _seal_hash(sorted(str(path) for path in targets))
    authority = receipt.get("candidate_public_text_source_fact_refresh", {}).get("authority_sha256")
    expected_receipt = _signed_receipt(
        status=str(journal["status"]), authority_sha256=str(authority or ""),
        receipt_sha256=str(receipt.get("receipt_sha256") or ""), targets_sha256=targets_sha,
        entries=entries,
    )
    if (
        journal.get("sealed_targets_sha256") != targets_sha
        or journal.get("authority_sha256") != authority
        or transaction_receipt != expected_receipt
    ):
        raise CandidateSourceFactRefreshRunError("refresh transaction receipt binding drifts")
    observed_targets: set[Path] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise CandidateSourceFactRefreshRunError("refresh transaction entry schema drifts")
        target = Path(str(entry["target"]))
        before = _unb64(entry["before_bytes_b64"])
        after = _unb64(entry["after_bytes_b64"])
        if (
            target in observed_targets or target not in targets
            or _sha256(before) != entry["before_sha256"]
            or _sha256(after) != entry["after_sha256"]
            or not isinstance(entry["before_mode"], int)
            or entry["before_mode"] < 0 or entry["before_mode"] > 0o777
            or not isinstance(entry["after_mode"], int)
            or entry["after_mode"] < 0 or entry["after_mode"] > 0o777
            or not all(isinstance(entry[key], int) and entry[key] >= 0 for key in ("before_uid", "before_gid", "after_uid", "after_gid"))
        ):
            raise CandidateSourceFactRefreshRunError("refresh transaction entry binding drifts")
        observed_targets.add(target)
    if observed_targets != targets:
        raise CandidateSourceFactRefreshRunError("refresh transaction target set drifts")
    descriptors = _sealed_target_descriptors("2026-08-11")
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    after_documents: list[tuple[Path, dict[str, Any]]] = []
    for entry in entries:
        target = Path(str(entry["target"]))
        before = _unb64(entry["before_bytes_b64"])
        after = _unb64(entry["after_bytes_b64"])
        descriptor = descriptors[target]
        if (
            _sha256(before) != descriptor.get("sha256")
            or len(before) != descriptor.get("bytes")
            or entry["before_mode"] != descriptor.get("mode")
            or entry["before_uid"] != descriptor.get("uid")
            or entry["before_gid"] != descriptor.get("gid")
            or entry["after_mode"] != descriptor.get("post_mode")
            or entry["after_uid"] != descriptor.get("uid")
            or entry["after_gid"] != descriptor.get("gid")
        ):
            raise CandidateSourceFactRefreshRunError("refresh intent preimage descriptor drifts")
        try:
            before_object = json.loads(before.decode("utf-8"))
            after_object = json.loads(after.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise CandidateSourceFactRefreshRunError("refresh intent bytes are unreadable") from exc
        if target == _state_path("2026-08-11"):
            if not isinstance(before_object, dict) or not isinstance(after_object, dict):
                raise CandidateSourceFactRefreshRunError("refresh intent state is invalid")
            before_state, after_state = before_object, after_object
        else:
            if not isinstance(after_object, dict) or not _document_diff_is_exact(before, after, receipt):
                raise CandidateSourceFactRefreshRunError("refresh intent document diff is invalid")
            after_documents.append((target, after_object))
    if before_state is None or after_state is None or not _state_diff_is_exact(before_state, after_state, receipt):
        raise CandidateSourceFactRefreshRunError("refresh intent state diff is invalid")
    contract, projected_receipt = _old_receipt(after_documents)
    record = _record_map(after_state)[CANDIDATE_ID]
    record_document = next((document for _path, document in after_documents if document.get("schema_version") != "shadow-publish-draft.v1"), None)
    if not isinstance(record_document, dict) or projected_receipt != receipt:
        raise CandidateSourceFactRefreshRunError("refresh intent receipt mirror drifts")
    subtitle = Path(str(record_document.get("subtitle_path") or ""))
    speaker = receipt.get("historical_provider_receipt", {}).get("speaker_evidence")
    if not isinstance(speaker, dict) or not validate_source_fact_review(
        receipt, selection_hook=str(contract.get("selection_hook") or ""), title=str(record.get("title") or ""),
        final_transcript=_transcript(subtitle), clip_context_prompt=str(contract.get("clip_context_prompt") or ""),
        selection_scorecard=contract.get("selection_scorecard"), story_contract=contract, candidate_id=CANDIDATE_ID,
        final_reviewed_srt_path=subtitle, speaker_evidence=speaker,
    ):
        raise CandidateSourceFactRefreshRunError("refresh intent authority validation failed")
    return journal


def _read_journal(path: Path, *, root: Path) -> dict[str, Any]:
    if not _private(path, directory=False):
        raise CandidateSourceFactRefreshRunError("refresh transaction journal is unsafe")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise CandidateSourceFactRefreshRunError("refresh transaction journal is unreadable") from exc
    if raw != _journal_bytes(value):
        raise CandidateSourceFactRefreshRunError("refresh transaction journal bytes are non-canonical")
    return _validate_journal(value, root=root)


def _state_diff_is_exact(before: Mapping[str, Any], after: Mapping[str, Any], receipt: Mapping[str, Any]) -> bool:
    left, right = copy.deepcopy(dict(before)), copy.deepcopy(dict(after))
    try:
        left_record = copy.deepcopy(_record_map(left)[CANDIDATE_ID])
        right_record = copy.deepcopy(_record_map(right)[CANDIDATE_ID])
    except CandidateSourceFactRefreshRunError:
        return False
    for lane in ("picks", "songs"):
        for row in left.get(lane, []) if isinstance(left.get(lane), list) else []:
            if isinstance(row, dict) and row.get("candidate_id") == CANDIDATE_ID:
                row.clear()
        for row in right.get(lane, []) if isinstance(right.get(lane), list) else []:
            if isinstance(row, dict) and row.get("candidate_id") == CANDIDATE_ID:
                row.clear()
    refresh = right_record.get("source_fact_review_refresh")
    return (
        _canonical_sha(left) == _canonical_sha(right)
        and {key for key in set(left_record) | set(right_record) if left_record.get(key) != right_record.get(key)}
        == {"source_fact_review", "source_fact_review_sha256", "source_fact_review_refresh"}
        and right_record.get("source_fact_review") == receipt
        and right_record.get("source_fact_review_sha256") == receipt.get("receipt_sha256")
        and refresh == {
            "schema_version": "candidate-public-text-source-fact-refresh-state.v1",
            "status": "REFRESHED_NO_PROVIDER",
            "authority_candidate_id": CANDIDATE_ID,
            "receipt_sha256": receipt.get("receipt_sha256"),
            "upload_enabled": False,
        }
    )


def _document_diff_is_exact(before: bytes, after: bytes, receipt: Mapping[str, Any]) -> bool:
    try:
        left, right = json.loads(before), json.loads(after)
    except (UnicodeError, ValueError):
        return False
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if right.get("schema_version") == "shadow-publish-draft.v1":
        left.pop("source_fact_review", None)
        right_receipt = right.pop("source_fact_review", None)
        return left == right and right_receipt == receipt
    for document in (left, right):
        if not isinstance(document.get("story_contract"), dict) or not isinstance(document.get("publish_staging"), dict):
            return False
    left["story_contract"].pop("source_fact_review", None)
    left["publish_staging"].pop("source_fact_review", None)
    story_receipt = right["story_contract"].pop("source_fact_review", None)
    stage_receipt = right["publish_staging"].pop("source_fact_review", None)
    return left == right and story_receipt == stage_receipt == receipt


def _validate_root_inventory(journal: Mapping[str, Any], *, root: Path, intent_path: Path) -> None:
    """Reject every non-intent root artifact before any target mutation."""

    expected = {root / "intent.json", root / "finalized-receipt.json"}
    expected.update(root / f"target-{index:02d}.tmp" for index, _entry in enumerate(journal["entries"]))
    expected.update(_pending_path(path) for path in tuple(expected))
    if any(path not in expected for path in root.iterdir()):
        raise CandidateSourceFactRefreshRunError("refresh transaction root contains unknown artifacts")
    artifacts: list[tuple[Path, bytes, int, int, int]] = [
        (intent_path, _journal_bytes(journal), _PRIVATE_FILE_MODE, os.geteuid(), os.getegid()),
    ]
    for index, entry in enumerate(journal["entries"]):
        artifacts.append((
            root / f"target-{index:02d}.tmp", _unb64(entry["after_bytes_b64"]),
            entry["after_mode"], entry["after_uid"], entry["after_gid"],
        ))
    finalized = root / "finalized-receipt.json"
    artifacts.append((finalized, _finalized_payload(journal=journal, intent_path=intent_path), _PRIVATE_FILE_MODE, os.geteuid(), os.getegid()))
    for final, payload, mode, uid, gid in artifacts:
        pending = _pending_path(final)
        if pending.exists() or pending.is_symlink():
            if not _pending_is_owned_recoverable(
                pending, target=final, payload=payload, target_mode=mode,
                target_uid=uid, target_gid=gid, target_exists=final.exists(),
            ):
                raise CandidateSourceFactRefreshRunError("refresh pending artifact is foreign or drifted")
    for index, entry in enumerate(journal["entries"]):
        temporary = root / f"target-{index:02d}.tmp"
        if temporary.exists() or temporary.is_symlink():
            observed = os.lstat(temporary)
            if (
                temporary.is_symlink() or not stat.S_ISREG(observed.st_mode)
                or temporary.read_bytes() != _unb64(entry["after_bytes_b64"])
                or stat.S_IMODE(observed.st_mode) != entry["after_mode"]
                or observed.st_uid != entry["after_uid"] or observed.st_gid != entry["after_gid"]
            ):
                raise CandidateSourceFactRefreshRunError("refresh target temporary is foreign or drifted")
    if finalized.exists() or finalized.is_symlink():
        if not _private(finalized, directory=False):
            raise CandidateSourceFactRefreshRunError("refresh finalized receipt is unsafe")
        expected_payload = _finalized_payload(journal=journal, intent_path=intent_path)
        if finalized.read_bytes() != expected_payload:
            raise CandidateSourceFactRefreshRunError("refresh finalized receipt drifted")


def _finalized_payload(*, journal: Mapping[str, Any], intent_path: Path) -> bytes:
    snapshots = [
        {
            "target": entry["target"],
            "sha256": entry["after_sha256"],
            "mode": entry["after_mode"],
            "uid": entry["after_uid"],
            "gid": entry["after_gid"],
        }
        for entry in journal["entries"]
    ]
    receipt: dict[str, Any] = {
        "schema_version": "candidate-public-text-source-fact-refresh-finalized-receipt.v1",
        "candidate_id": CANDIDATE_ID,
        "intent_sha256": _sha256(intent_path.read_bytes()),
        "source_fact_review_sha256": journal["source_fact_review_sha256"],
        "snapshots": snapshots,
    }
    receipt["receipt_sha256"] = _seal_hash(receipt)
    return _journal_bytes(receipt)


def _target_parent_device(path: Path) -> int:
    return os.lstat(path.parent).st_dev


def _assert_target_filesystems(journal: Mapping[str, Any], *, root: Path) -> None:
    root_device = os.lstat(root).st_dev
    if any(_target_parent_device(Path(entry["target"])) != root_device for entry in journal["entries"]):
        raise CandidateSourceFactRefreshRunError("refresh target is not on transaction filesystem")


def _assert_final_target_snapshots(journal: Mapping[str, Any]) -> None:
    for entry in journal["entries"]:
        target = Path(entry["target"])
        value = os.lstat(target)
        if (
            target.is_symlink() or not stat.S_ISREG(value.st_mode)
            or _file_sha(target) != entry["after_sha256"]
            or stat.S_IMODE(value.st_mode) != entry["after_mode"]
            or value.st_uid != entry["after_uid"] or value.st_gid != entry["after_gid"]
        ):
            raise CandidateSourceFactRefreshRunError("refresh final target snapshot drifted")


def _stage_transaction(*, date: str, state_raw: bytes, updated: Mapping[str, Any], intended: list[tuple[Path, bytes, bytes]], receipt: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    root = _transaction_root(date)
    _safe_transaction_root(root)
    journal_path = root / "intent.json"
    if journal_path.exists() or journal_path.is_symlink():
        return journal_path, _read_journal(journal_path, root=root)
    try:
        before_state = json.loads(state_raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise CandidateSourceFactRefreshRunError("refresh state preimage is unreadable") from exc
    if not isinstance(before_state, dict) or not _state_diff_is_exact(before_state, updated, receipt):
        raise CandidateSourceFactRefreshRunError("refresh state semantic diff is not exact")
    state_path = _state_path(date)
    targets = [*intended, (state_path, state_raw, _journal_bytes(updated))]
    descriptors = _sealed_target_descriptors(date)
    if {path for path, _before, _after in targets} != set(descriptors):
        raise CandidateSourceFactRefreshRunError("refresh transaction targets differ from sealed authority")
    entries: list[dict[str, Any]] = []
    for index, (target, before, after) in enumerate(targets):
        descriptor = descriptors[target]
        if not _regular_no_symlink_ancestors(target) or not _matches_sealed_target(target, descriptor=descriptor, payload=before):
            raise CandidateSourceFactRefreshRunError("refresh target preimage drifted")
        if target != state_path and not _document_diff_is_exact(before, after, receipt):
            raise CandidateSourceFactRefreshRunError("refresh document semantic diff is not exact")
        entries.append({
            "target": str(target),
            "before_sha256": _sha256(before), "after_sha256": _sha256(after), "before_bytes_b64": _b64(before), "after_bytes_b64": _b64(after),
            "before_mode": _mode(target), "after_mode": descriptor.get("post_mode"),
            "before_uid": os.lstat(target).st_uid, "before_gid": os.lstat(target).st_gid,
            "after_uid": os.geteuid(), "after_gid": os.getegid(),
        })
    authority = receipt.get("candidate_public_text_source_fact_refresh", {}).get("authority_sha256")
    if not isinstance(authority, str):
        raise CandidateSourceFactRefreshRunError("refresh authority receipt is missing")
    target_hash = _seal_hash(sorted(str(path) for path in _sealed_target_paths(date)))
    journal: dict[str, Any] = {
        "schema_version": TRANSACTION_SCHEMA, "status": "PREPARED", "candidate_id": CANDIDATE_ID, "date": date, "upload_enabled": False,
        "authority_sha256": authority, "source_fact_review": copy.deepcopy(dict(receipt)), "source_fact_review_sha256": receipt.get("receipt_sha256"),
        "sealed_targets_sha256": target_hash, "entries": entries,
        "transaction_receipt": _signed_receipt(status="PREPARED", authority_sha256=authority, receipt_sha256=str(receipt.get("receipt_sha256") or ""), targets_sha256=target_hash, entries=entries),
        "journal_sha256": "",
    }
    journal = _signed_journal(journal)
    pending = _pending_path(journal_path)
    if any(path != pending for path in root.iterdir()):
        raise CandidateSourceFactRefreshRunError("refresh transaction root has foreign artifacts")
    if pending.exists() or pending.is_symlink():
        if not _pending_is_owned_recoverable(
            pending, target=journal_path, payload=_journal_bytes(journal),
            target_mode=_PRIVATE_FILE_MODE, target_uid=os.geteuid(), target_gid=os.getegid(), target_exists=False,
        ):
            raise CandidateSourceFactRefreshRunError("refresh intent pending artifact is foreign or drifted")
    _create_private_bytes(journal_path, _journal_bytes(journal))
    _validate_root_inventory(journal, root=root, intent_path=journal_path)
    return journal_path, journal


def _commit_transaction(*, journal_path: Path, journal: dict[str, Any]) -> dict[str, Any]:
    root = journal_path.parent
    _safe_transaction_root(root)
    journal = _read_journal(journal_path, root=root)
    _validate_root_inventory(journal, root=root, intent_path=journal_path)
    _assert_target_filesystems(journal, root=root)
    for index, entry in enumerate(journal["entries"]):
        target = Path(str(entry["target"]))
        after = _unb64(entry["after_bytes_b64"])
        observed = _file_sha(target) if _regular_no_symlink_ancestors(target) else None
        target_stat = os.lstat(target) if _regular_no_symlink_ancestors(target) else None
        if (
            observed == entry["before_sha256"] and _mode(target) == entry["before_mode"]
            and target_stat is not None and target_stat.st_uid == entry["before_uid"] and target_stat.st_gid == entry["before_gid"]
        ):
            _install_target_bytes(
                target,
                after,
                mode=entry["after_mode"],
                uid=entry["after_uid"],
                gid=entry["after_gid"],
                expected_before_sha256=entry["before_sha256"],
                expected_before_mode=entry["before_mode"],
                expected_before_uid=entry["before_uid"],
                expected_before_gid=entry["before_gid"],
                temporary=root / f"target-{index:02d}.tmp",
            )
        elif (
            observed != entry["after_sha256"] or _mode(target) != entry["after_mode"]
            or target_stat is None or target_stat.st_uid != entry["after_uid"] or target_stat.st_gid != entry["after_gid"]
        ):
            raise CandidateSourceFactRefreshRunError("refresh transaction target foreign drift")
        after_stat = os.lstat(target)
        if (
            _file_sha(target) != entry["after_sha256"] or _mode(target) != entry["after_mode"]
            or after_stat.st_uid != entry["after_uid"] or after_stat.st_gid != entry["after_gid"]
        ):
            raise CandidateSourceFactRefreshRunError("refresh transaction post-write verification failed")
    return journal


def run(*, date: str) -> dict[str, Any]:
    disabled, auto_upload = BASE / "DISABLED", BASE / "AUTO_UPLOAD"
    if date != "2026-08-11" or not _regular_no_symlink_ancestors(disabled) or auto_upload.exists() or auto_upload.is_symlink():
        raise CandidateSourceFactRefreshRunError("refresh runtime date or upload boundary is invalid")
    root = _transaction_root(date)
    _safe_transaction_root(root)
    journal_path = root / "intent.json"
    if journal_path.exists() or journal_path.is_symlink():
        if journal_path.is_symlink() or not _regular(journal_path):
            raise CandidateSourceFactRefreshRunError("refresh transaction journal is unsafe")
        _create_private_bytes(journal_path, journal_path.read_bytes())
        journal = _commit_transaction(journal_path=journal_path, journal=_read_journal(journal_path, root=root))
    else:
        state_path = _state_path(date)
        if not _regular_no_symlink_ancestors(state_path):
            raise CandidateSourceFactRefreshRunError("refresh state file is unsafe")
        state_raw = state_path.read_bytes()
        try:
            state = json.loads(state_raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise CandidateSourceFactRefreshRunError("refresh state file is unreadable") from exc
        if not isinstance(state, dict):
            raise CandidateSourceFactRefreshRunError("refresh state file is invalid")
        updated, intended, receipt = prepare_refresh(date=date, state=state)
        journal_path, journal = _stage_transaction(date=date, state_raw=state_raw, updated=updated, intended=intended, receipt=receipt)
        journal = _commit_transaction(journal_path=journal_path, journal=journal)
    _postcommit_validate(journal=journal)
    if _FINAL_RECEIPT_PREWRITE_HOOK is not None:
        _FINAL_RECEIPT_PREWRITE_HOOK(journal)
    _assert_final_target_snapshots(journal)
    finalized_path = root / "finalized-receipt.json"
    payload = _finalized_payload(journal=journal, intent_path=journal_path)
    _create_private_bytes(finalized_path, payload)
    _assert_final_target_snapshots(journal)
    return {"candidate_id": CANDIDATE_ID, "date": date, "status": "REFRESHED_NO_PROVIDER", "receipt_sha256": journal["source_fact_review_sha256"], "upload_enabled": False, "transaction_receipt": str(journal_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    args = parser.parse_args(argv)
    runner_lock = BASE / "runner.lock"
    with runner_lock.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CandidateSourceFactRefreshRunError("runner.lock is busy") from exc
        try:
            with exclusive_upload_lock(DEFAULT_UPLOAD_LOCK):
                print(json.dumps(run(date=args.date), ensure_ascii=False, sort_keys=True))
        except UploadLockBusy as exc:
            raise CandidateSourceFactRefreshRunError("upload.lock is busy") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
