"""Canonical provider refresh for Qixi's corrected terminal evidence.

This is intentionally a *review* primitive, rather than a text-repair
primitive.  It is fixed to the 女友感 candidate and consumes the already
sealed human correction.  Callers must stage its returned JSON documents and
perform their own sealed transaction; this module never writes a runtime
target and never alters the SRT, ASS, or media bytes.
"""

from __future__ import annotations

import copy
import base64
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.autoslice.final_review_contract import (
    FinalReviewContractError,
    validate_final_review_release,
)
from src.autoslice.final_review_auditor import _final_review_structured_context
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.chat_evidence import ChatEvidence
from src.autoslice.producer_boundary_review_stage import exact_delivery_correction_audit
from src.autoslice.producer_text_pipeline import (
    TextPipelineAdapters,
    _run_exact_final_release_review,
)
from src.autoslice.repository_asset_authority import require_repository_asset_authority
from src.autoslice.qixi_transaction_core import (
    FileSnapshot,
    InstallCallbacks,
    QixiTransactionCoreError,
    create_staged_inode,
    install_checkpointed_inode,
    journal_before_snapshot,
    restore_owned_inode,
    safe_parent,
    stable_regular_snapshot,
)


CANDIDATE_ID = "auto_123655_771_844"
RECORDING_DATE = "2026-08-17"
ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_PATH = Path(
    "assets/lidousha/qixi_terminal_evidence_refresh/auto_123655_771_844.v1.json"
)
_JOURNAL_ROLES = ("chat", "record", "delivery_record", "publish", "state")
_ENTRY_PHASES = frozenset({"PREPARED", "INSTALLING", "INSTALLED"})
_JOURNAL_STATUSES = frozenset(
    {"PREPARED", "ROLLBACK_REQUIRED", "ROLLED_BACK", "COMMITTED"}
)


class QixiTerminalEvidenceRefreshError(ValueError):
    """The sealed correction cannot safely receive fresh terminal reviews."""


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read_regular(path: Path) -> bytes:
    try:
        snapshot = stable_regular_snapshot(path, label="terminal refresh file")
    except QixiTransactionCoreError as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_FILE_UNREADABLE") from exc
    if snapshot is None:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_FILE_UNSAFE")
    return snapshot.payload


def load_authority(repo_root: Path = ROOT) -> dict[str, object]:
    """Load the one committed/deployed authority before any runtime read."""

    path = repo_root / AUTHORITY_PATH
    payload = _read_regular(path)
    try:
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=AUTHORITY_PATH, observed_bytes=payload
        )
        value = json.loads(payload)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise QixiTerminalEvidenceRefreshError(
            "QIXI_TERMINAL_REFRESH_AUTHORITY_UNSEALED"
        ) from exc
    authority = _require_mapping(value, "AUTHORITY")
    claimed = authority.pop("authority_sha256", None)
    required = {
        "schema_version", "candidate_id", "recording_date", "runtime_root", "upload_enabled",
        "preimage", "repository_preimage", "predecessor_recovery", "correction", "allowed_mutations",
    }
    if (
        set(authority) != required
        or authority.get("schema_version") != "qixi-terminal-evidence-refresh-authority.v1"
        or authority.get("candidate_id") != CANDIDATE_ID
        or authority.get("recording_date") != RECORDING_DATE
        or authority.get("upload_enabled") is not False
        or not isinstance(claimed, str)
        or _canonical_sha(authority) != claimed
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_AUTHORITY_INVALID")
    authority["authority_sha256"] = claimed
    return authority


def validate_runtime(authority: Mapping[str, object]) -> dict[str, bytes]:
    """Snapshot every sealed runtime input; no target is opened for writing."""

    preimage = _require_mapping(authority.get("preimage"), "PREIMAGE")
    expected = {"record", "delivery_record", "publish", "state", "chat", "clip_context", "srt", "ass", "burn", "correction"}
    if set(preimage) != expected:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_PREIMAGE_SCHEMA_INVALID")
    result: dict[str, bytes] = {}
    for role, row in preimage.items():
        descriptor = _require_mapping(row, "DESCRIPTOR")
        keys = {"path", "sha256", "bytes"} | ({"mode"} if role in {"record", "delivery_record", "publish", "state", "chat"} else set())
        if set(descriptor) != keys or not isinstance(descriptor.get("path"), str):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_DESCRIPTOR_INVALID")
        path = Path(str(descriptor["path"]))
        payload = _read_regular(path)
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if digest != descriptor["sha256"] or len(payload) != descriptor["bytes"]:
            raise QixiTerminalEvidenceRefreshError(f"QIXI_TERMINAL_REFRESH_{role.upper()}_DRIFT")
        if "mode" in descriptor and stat.S_IMODE(os.lstat(path).st_mode) != descriptor["mode"]:
            raise QixiTerminalEvidenceRefreshError(f"QIXI_TERMINAL_REFRESH_{role.upper()}_MODE_DRIFT")
        result[role] = payload
    return result


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise QixiTerminalEvidenceRefreshError(f"QIXI_TERMINAL_REFRESH_{label}_INVALID")
    return dict(value)


def _assert_text_and_grid_immutable(
    *, before_srt: str, final_srt: str, correction: Mapping[str, object]
) -> None:
    """Accept only the sealed four-line human correction and no timing drift."""

    before_cues = parse_srt_cues(before_srt)
    final_cues = parse_srt_cues(final_srt)
    operations = correction.get("set_line_operations")
    if not isinstance(operations, list) or len(before_cues) != len(final_cues):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_GRID_INVALID")
    expected: dict[int, str] = {}
    for operation in operations:
        if not isinstance(operation, str) or "=" not in operation:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_OP_INVALID")
        index_text, text = operation.split("=", 1)
        try:
            index = int(index_text)
        except ValueError as exc:
            raise QixiTerminalEvidenceRefreshError(
                "QIXI_TERMINAL_REFRESH_CORRECTION_OP_INVALID"
            ) from exc
        if index in expected or not text:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_OP_INVALID")
        expected[index] = text
    if set(expected) != {1, 3, 6, 27}:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_SCOPE_INVALID")
    for index, (before, final) in enumerate(zip(before_cues, final_cues), start=1):
        if before.start_ms != final.start_ms or before.end_ms != final.end_ms:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TIMING_DRIFT")
        if index in expected:
            if final.text != expected[index]:
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_LISTED_TEXT_DRIFT")
        elif final.text != before.text:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_UNLISTED_TEXT_DRIFT")


def refresh_terminal_evidence(
    *,
    before_srt: str,
    final_srt: str,
    correction: Mapping[str, object],
    old_chat_authority: Mapping[str, object],
    selection_hook: str,
    selection_scorecard: object,
    structured_context: str,
    clip_context: Mapping[str, object],
    source_final_start_ms: int,
    source_final_end_ms: int,
    boundary_max_forward_ms: int,
    adapters: TextPipelineAdapters,
    authoritative_chat: Sequence[object],
    verify_confusable_entity: Callable | None = None,
    final_review_llm: Callable[[str], str] | None = None,
    boundary_review_llm: Callable[[str], str] | None = None,
    extract_json: Callable[[str], Any] | None = None,
) -> dict[str, object]:
    """Produce new provider-backed final-review and boundary receipts.

    Tests may inject the two provider seams.  In production, the fixed runner
    deliberately leaves them unset, causing the canonical pipeline's normal
    CPA transports to be constructed at the point of review.
    """

    _assert_text_and_grid_immutable(
        before_srt=before_srt, final_srt=final_srt, correction=correction
    )
    old_chat = _require_mapping(old_chat_authority, "CHAT")
    old_audit = _require_mapping(old_chat.get("final_review_audit"), "OLD_REVIEW")
    if old_chat.get("final_text_srt_sha256") != _sha(before_srt)[7:]:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_OLD_CHAT_TEXT_DRIFT")
    if correction.get("before_srt_sha256") != _sha(before_srt):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_BEFORE_DRIFT")
    if correction.get("after_srt_sha256") != _sha(final_srt) or not clip_context:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CORRECTION_AFTER_DRIFT")

    # The boundary reviewer is intentionally called first.  Its fresh receipt
    # becomes the exact boundary input bound by the new final-review receipt.
    from src.autoslice import producer_text_pipeline as text_pipeline

    from src.autoslice.llm_client import extract_json_object
    boundary_correction = exact_delivery_correction_audit(
        final_srt_text=final_srt,
        correction_audit=old_audit,
        source_final_start_ms=source_final_start_ms,
        source_final_end_ms=source_final_end_ms,
        candidate_id=CANDIDATE_ID,
        selection_hook=selection_hook,
        selection_scorecard=selection_scorecard,
        structured_context=structured_context,
        candidate_context="",
        boundary_max_forward_ms=boundary_max_forward_ms,
        llm_call=boundary_review_llm or text_pipeline._build_final_review_llm_call(),
        extract_json=extract_json or extract_json_object,
    )

    final_audit = _run_exact_final_release_review(
        srt_text=final_srt,
        correction_audit=boundary_correction,
        adapters=adapters,
        authoritative_chat=authoritative_chat,
        selection_hook=selection_hook,
        clip_context=clip_context,
        verify_confusable_entity=verify_confusable_entity,
        final_review_llm=final_review_llm,
        pronoun_audit_llm=final_review_llm,
    )
    try:
        validate_final_review_release(final_audit, expected_srt_sha256=_sha(final_srt))
    except FinalReviewContractError as exc:
        raise QixiTerminalEvidenceRefreshError(
            "QIXI_TERMINAL_REFRESH_FINAL_REVIEW_BLOCKED"
        ) from exc

    refreshed_chat = copy.deepcopy(old_chat)
    final_sha = _sha(final_srt)[7:]
    for key in ("final_text_srt_sha256", "final_speaker_srt_sha256", "final_output_srt_sha256"):
        refreshed_chat[key] = final_sha
    refreshed_chat["final_review_audit"] = final_audit
    refreshed_chat["final_status"] = "FINAL_ARTIFACTS_VERIFIED"
    return {
        "chat_authority": refreshed_chat,
        "final_review_audit": final_audit,
        "final_delivery_boundary_semantic_review": boundary_correction["boundary_semantic_review"],
    }


def project_evidence_mirrors(
    *,
    record: Mapping[str, object],
    delivery_record: Mapping[str, object],
    publish: Mapping[str, object],
    state: Mapping[str, object],
    refreshed_chat_bytes: bytes,
    final_delivery_boundary: Mapping[str, object],
    allowed_mutations: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    """Project only the new evidence bindings into the four public mirrors.

    This deliberately does not accept a caller supplied list of JSON pointers.
    The two record copies must be byte-identical semantic objects, and every
    title, cover, media, subtitle and ASS field remains inherited verbatim.
    """

    old_record = _require_mapping(record, "RECORD")
    old_delivery = _require_mapping(delivery_record, "DELIVERY_RECORD")
    if old_record != old_delivery:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RECORD_MIRROR_DRIFT")
    old_publish = _require_mapping(publish, "PUBLISH")
    old_state = _require_mapping(state, "STATE")
    boundary = _require_mapping(old_record.get("boundary_audit"), "BOUNDARY")
    story = _require_mapping(old_record.get("story_contract"), "STORY")
    artifact_hashes = _require_mapping(old_record.get("artifact_hashes"), "ARTIFACT_HASHES")
    chat_path = old_record.get("chat_authority_audit_path")
    if not isinstance(chat_path, str) or not chat_path.endswith(".chat-authority.json"):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CHAT_LOCATOR_DRIFT")
    new_boundary = dict(boundary)
    new_boundary["final_delivery_boundary_semantic_review"] = copy.deepcopy(
        dict(final_delivery_boundary)
    )
    new_story = dict(story)
    new_story["boundary_semantic_review"] = copy.deepcopy(dict(final_delivery_boundary))
    new_hashes = dict(artifact_hashes)
    new_hashes["chat_authority_audit_sha256"] = (
        "sha256:" + hashlib.sha256(refreshed_chat_bytes).hexdigest()
    )
    new_record = copy.deepcopy(old_record)
    new_record["boundary_audit"] = new_boundary
    new_record["story_contract"] = new_story
    new_record["artifact_hashes"] = new_hashes

    # The post-correction publish document carries the same StoryContract; it
    # is not allowed to grow a second, independently generated review object.
    new_publish = copy.deepcopy(old_publish)
    if isinstance(new_publish.get("story_contract"), Mapping):
        new_publish["story_contract"] = copy.deepcopy(new_story)
    # State may contain more than one collection.  Locate exactly one current
    # candidate row and update only its nested public record projection when it
    # exists; a missing or duplicate candidate is a hard block.
    new_state = copy.deepcopy(old_state)
    rows: list[dict[str, object]] = []
    for collection in ("picks", "talk", "talks", "pending_talk"):
        value = new_state.get(collection)
        if isinstance(value, list):
            rows.extend(
                row for row in value if isinstance(row, dict) and row.get("candidate_id") == CANDIDATE_ID
            )
    if len(rows) != 1:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_STATE_CANDIDATE_DRIFT")
    if isinstance(rows[0].get("story_contract"), Mapping):
        rows[0]["story_contract"] = copy.deepcopy(new_story)
    projected = {
        "record": new_record,
        "delivery_record": copy.deepcopy(new_record),
        "publish": new_publish,
        "state": new_state,
    }
    allowed = _require_mapping(allowed_mutations, "ALLOWLIST")
    for role, prior, current in (
        ("record", old_record, projected["record"]),
        ("delivery_record", old_delivery, projected["delivery_record"]),
        ("publish", old_publish, projected["publish"]),
        ("state", old_state, projected["state"]),
    ):
        changed = _changed_pointers(prior, current)
        expected = set(allowed.get(role, []))
        if changed != expected:
            raise QixiTerminalEvidenceRefreshError(
                f"QIXI_TERMINAL_REFRESH_{role.upper()}_ALLOWLIST_DRIFT"
            )
    return projected


def _changed_pointers(before: object, after: object, prefix: str = "") -> set[str]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        keys = set(before) | set(after)
        return set().union(*(
            _changed_pointers(before.get(key), after.get(key), f"{prefix}/{key}")
            for key in keys
        )) if keys else set()
    if before == after:
        return set()
    return {prefix or "/"}


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _refresh_root(authority: Mapping[str, object]) -> Path:
    record = _require_mapping(_require_mapping(authority["preimage"], "PREIMAGE")["record"], "RECORD")
    return Path(str(record["path"])).parent / "qixi-terminal-evidence-refresh" / str(authority["authority_sha256"])[7:23]


def _create_refresh_root(root: Path) -> None:
    """Create only the two private journal directories below a sealed parent."""

    namespace = root.parent
    try:
        safe_parent(namespace)
        if not os.path.lexists(namespace):
            namespace.mkdir(mode=0o700)
        namespace_mode = os.lstat(namespace).st_mode
        if stat.S_ISLNK(namespace_mode) or not stat.S_ISDIR(namespace_mode) or stat.S_IMODE(namespace_mode) != 0o700:
            raise OSError("terminal refresh namespace is unsafe")
        root.mkdir(mode=0o700)
    except (OSError, QixiTransactionCoreError) as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_ROOT_UNSAFE") from exc


def _write_new(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    try:
        safe_parent(path)
    except QixiTransactionCoreError as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_UNSAFE") from exc
    if path.exists() or path.is_symlink():
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CREATE_ONLY_COLLISION")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_exact(path: Path, *, before: bytes, after: bytes, mode: int) -> None:
    """CAS replace one owned target, preserving its regular-file mode."""

    try:
        safe_parent(path)
    except QixiTransactionCoreError as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_UNSAFE") from exc
    observed = _sealed_snapshot(path, payload=before, mode=mode, label="terminal refresh replace")
    if observed.payload != before:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_DRIFT")
    fd, temporary = tempfile.mkstemp(prefix=".qixi-terminal-refresh-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        os.write(fd, after)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if _read_regular(path) != before:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_DRIFT")
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _sealed_snapshot(path, payload=after, mode=mode, label="terminal refresh replace")
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _sealed_snapshot(path: Path, *, payload: bytes, mode: int, label: str) -> FileSnapshot:
    """Freeze one runtime target's inode for the terminal lane's CAS journal."""

    try:
        snapshot = stable_regular_snapshot(path, label=label)
    except QixiTransactionCoreError as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_UNSAFE") from exc
    if snapshot is None or snapshot.payload != payload or snapshot.mode != mode:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_DRIFT")
    return snapshot


def _write_journal(path: Path, journal: dict[str, object]) -> None:
    """Replace the sole journal inode only after sealing its self-hash."""

    journal["journal_sha256"] = _canonical_sha({k: v for k, v in journal.items() if k != "journal_sha256"})
    _replace_exact(path, before=_read_regular(path), after=_json_bytes(journal), mode=0o600)


def _journal_sha256(journal: Mapping[str, object]) -> str:
    return _canonical_sha({key: value for key, value in journal.items() if key != "journal_sha256"})


def _receipt_for(journal: Mapping[str, object]) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": "qixi-terminal-evidence-refresh-receipt.v1",
        "status": "COMMITTED",
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "authority_sha256": journal["authority_sha256"],
        "journal_sha256": journal["journal_sha256"],
        "entries_sha256": _canonical_sha(journal["entries"]),
        "matrix_sha256": _canonical_sha(journal["matrix"]),
    }
    receipt["receipt_sha256"] = _canonical_sha(receipt)
    return receipt


def _terminal_matrix() -> dict[str, object]:
    return {
        "schema_version": "qixi-terminal-evidence-refresh-matrix.v1",
        "status": "PASS",
        "predicates": [
            {"id": "sealed_preimages", "status": "PASS"},
            {"id": "subtitle_grid", "status": "PASS"},
            {"id": "provider_final_review", "status": "PASS"},
            {"id": "provider_boundary_review", "status": "PASS"},
            {"id": "mirror_projection", "status": "PASS"},
        ],
    }


def _validate_journal(
    journal: Mapping[str, object], *, authority: Mapping[str, object], before: Mapping[str, bytes], after: Mapping[str, bytes]
) -> dict[str, object]:
    """Make a self-rehashed journal prove the fixed authority and after-image."""

    required = {
        "schema_version", "status", "candidate_id", "recording_date", "authority_sha256",
        "entries", "matrix", "journal_sha256",
    }
    if set(journal) != required or journal.get("schema_version") != "qixi-terminal-evidence-refresh-journal.v1":
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    if (
        journal.get("status") not in _JOURNAL_STATUSES
        or journal.get("candidate_id") != CANDIDATE_ID
        or journal.get("recording_date") != RECORDING_DATE
        or journal.get("authority_sha256") != authority.get("authority_sha256")
        or journal.get("journal_sha256") != _journal_sha256(journal)
        or journal.get("matrix") != _terminal_matrix()
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
    entries = journal.get("entries")
    if not isinstance(entries, list) or len(entries) != len(_JOURNAL_ROLES):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    preimage = _require_mapping(authority.get("preimage"), "PREIMAGE")
    seen: set[str] = set()
    entry_keys = {
        "role", "path", "before_sha256", "after_sha256", "before_bytes_b64", "after_bytes_b64",
        "before_mode", "before_device", "before_inode", "after_mode", "backup_name", "staged_name",
        "staged_device", "staged_inode", "installed_device", "installed_inode", "phase",
    }
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != entry_keys:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
        role = entry.get("role")
        if not isinstance(role, str) or role not in _JOURNAL_ROLES or role in seen:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
        seen.add(role)
        descriptor = _require_mapping(preimage.get(role), role)
        try:
            old = base64.b64decode(str(entry["before_bytes_b64"]), validate=True)
            new = base64.b64decode(str(entry["after_bytes_b64"]), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID") from exc
        if (
            entry.get("path") != descriptor.get("path")
            or old != before[role]
            or new != after[role]
            or entry.get("before_sha256") != "sha256:" + hashlib.sha256(old).hexdigest()
            or entry.get("after_sha256") != "sha256:" + hashlib.sha256(new).hexdigest()
            or entry.get("before_mode") != descriptor.get("mode")
            or entry.get("after_mode") != descriptor.get("mode")
            or entry.get("phase") not in _ENTRY_PHASES
            or not isinstance(entry.get("before_device"), int)
            or isinstance(entry.get("before_device"), bool)
            or not isinstance(entry.get("before_inode"), int)
            or isinstance(entry.get("before_inode"), bool)
            or not isinstance(entry.get("backup_name"), str)
            or Path(str(entry["backup_name"])).name != entry["backup_name"]
            or not isinstance(entry.get("staged_name"), str)
            or not str(entry["staged_name"]).startswith(".")
            or Path(str(entry["staged_name"])).name != entry["staged_name"]
        ):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
        phase = entry["phase"]
        for field in ("staged_device", "staged_inode"):
            value = entry[field]
            if phase == "PREPARED" and value is not None:
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
            # INSTALLING has a deliberate checkpoint before stage creation;
            # a crash there is resumable without inventing inode evidence.
            if phase == "INSTALLED" and (isinstance(value, bool) or not isinstance(value, int)):
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
        if phase == "INSTALLING" and ((entry["staged_device"] is None) != (entry["staged_inode"] is None)):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
        if phase == "INSTALLING" and entry["staged_device"] is not None and (
            isinstance(entry["staged_device"], bool) or not isinstance(entry["staged_device"], int)
            or isinstance(entry["staged_inode"], bool) or not isinstance(entry["staged_inode"], int)
        ):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
        for field in ("installed_device", "installed_inode"):
            value = entry[field]
            if phase != "INSTALLED" and value is not None:
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
            if phase == "INSTALLED" and (isinstance(value, bool) or not isinstance(value, int)):
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_DRIFT")
    if seen != set(_JOURNAL_ROLES):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    return dict(journal)


def _verify_committed_projection(
    root: Path, journal: Mapping[str, object], *, authority: Mapping[str, object], before: Mapping[str, bytes], after: Mapping[str, bytes]
) -> None:
    _validate_journal(journal, authority=authority, before=before, after=after)
    if journal.get("status") != "COMMITTED":
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMIT_MISSING")
    entries = journal["entries"]
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, Mapping)
        role = str(entry["role"])
        observed = _sealed_snapshot(
            Path(str(entry["path"])), payload=after[role], mode=int(entry["after_mode"]),
            label="terminal refresh committed target",
        )
        if (observed.device, observed.inode) != (entry["installed_device"], entry["installed_inode"]):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMITTED_TARGET_DRIFT")
    expected = _json_bytes(_receipt_for(journal))
    receipt = root / "receipt.json"
    if os.path.lexists(receipt):
        if _read_regular(receipt) != expected:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RECEIPT_DRIFT")
    else:
        _write_new(receipt, expected)


def _checkpoint(root: Path, journal: dict[str, object], index: int, **changes: object) -> None:
    entries = journal["entries"]
    assert isinstance(entries, list) and isinstance(entries[index], Mapping)
    updated = dict(entries[index])
    updated.update(changes)
    entries[index] = updated
    _write_journal(root / "journal.json", journal)


def _backup_path(root: Path, entry: Mapping[str, object]) -> Path:
    name = entry.get("backup_name")
    if not isinstance(name, str) or Path(name).name != name:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    return root / "backups" / name


def _ensure_backup(root: Path, entry: Mapping[str, object]) -> None:
    path = _backup_path(root, entry)
    expected = base64.b64decode(str(entry["before_bytes_b64"]))
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700)
    if path.parent.is_symlink() or not path.parent.is_dir() or stat.S_IMODE(os.lstat(path.parent).st_mode) != 0o700:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_BACKUP_ROOT_UNSAFE")
    if path.exists():
        if _read_regular(path) != expected:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_BACKUP_DRIFT")
        return
    _write_new(path, expected)


def _entry_staged_path(entry: Mapping[str, object]) -> Path:
    target = Path(str(entry["path"]))
    name = entry.get("staged_name")
    if not isinstance(name, str) or Path(name).name != name or not name.startswith("."):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    return target.parent / name


def _rollback_entries(root: Path, journal: dict[str, object]) -> None:
    """Reverse only transaction-owned after inodes; retain failures durably."""

    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    for index in reversed(range(len(entries))):
        entry = entries[index]
        if not isinstance(entry, Mapping) or entry.get("phase") == "PREPARED":
            continue
        target = Path(str(entry["path"]))
        after = base64.b64decode(str(entry["after_bytes_b64"]))
        current = stable_regular_snapshot(target, label="terminal refresh rollback")
        if current is not None and current.payload == after:
            if entry.get("phase") == "INSTALLED" and (
                current.device, current.inode
            ) != (entry.get("installed_device"), entry.get("installed_inode")):
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLBACK_OWNERSHIP_DRIFT")
            try:
                restored = restore_owned_inode(
                    current,
                    before=journal_before_snapshot(entry, target=target),
                    label="terminal refresh rollback",
                )
            except QixiTransactionCoreError as exc:
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLBACK_OWNERSHIP_DRIFT") from exc
            if restored is None or restored.payload != base64.b64decode(str(entry["before_bytes_b64"])):
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLBACK_OWNERSHIP_DRIFT")
        elif entry.get("phase") == "INSTALLED":
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLBACK_OWNERSHIP_DRIFT")
        staged = _entry_staged_path(entry)
        if os.path.lexists(staged):
            owned = _adopt_staged(entry)
            try:
                os.unlink(owned.path)
            except OSError as exc:
                raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLBACK_STAGE_FAILURE") from exc
        marker = _stage_marker(entry)
        if os.path.lexists(marker):
            _read_regular(marker)
            marker.unlink()
        _checkpoint(
            root, journal, index, phase="PREPARED", staged_device=None, staged_inode=None,
            installed_device=None, installed_inode=None,
        )
    journal["status"] = "ROLLED_BACK"
    _write_journal(root / "journal.json", journal)


def _stage_marker(entry: Mapping[str, object]) -> Path:
    path = _entry_staged_path(entry)
    return path.with_name(path.name + ".owner")


def _write_stage_marker(entry: Mapping[str, object], snapshot: FileSnapshot) -> None:
    payload = _json_bytes(
        {
            "schema_version": "qixi-terminal-evidence-refresh-stage-owner.v1",
            "path": snapshot.path.name,
            "device": snapshot.device,
            "inode": snapshot.inode,
            "sha256": snapshot.sha256,
            "mode": snapshot.mode,
        }
    )
    _write_new(_stage_marker(entry), payload)


def _adopt_staged(entry: Mapping[str, object]) -> FileSnapshot:
    staged = _entry_staged_path(entry)
    marker = _json_document(_read_regular(_stage_marker(entry)), "STAGE_OWNER")
    snapshot = _sealed_snapshot(
        staged,
        payload=base64.b64decode(str(entry["after_bytes_b64"])),
        mode=int(entry["after_mode"]),
        label="terminal refresh staged owner",
    )
    if marker != {
        "schema_version": "qixi-terminal-evidence-refresh-stage-owner.v1",
        "path": staged.name,
        "device": snapshot.device,
        "inode": snapshot.inode,
        "sha256": snapshot.sha256,
        "mode": snapshot.mode,
    }:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_STAGED_OWNER_DRIFT")
    return snapshot


def apply_projection(
    *, authority: Mapping[str, object], before: Mapping[str, bytes], after: Mapping[str, bytes], apply: bool
) -> dict[str, object]:
    """Stage a bounded four-document/chat refresh under a resumable receipt.

    `apply=False` is a complete no-target-write preflight.  The function
    writes no provider evidence: callers must complete provider review before
    calling it and pass the exact staged bytes here.
    """

    roles = ("chat", "record", "delivery_record", "publish", "state")
    if set(after) != set(roles) or any(role not in before for role in roles):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_PROJECTION_SCHEMA_INVALID")
    preimage = _require_mapping(authority["preimage"], "PREIMAGE")
    matrix = _terminal_matrix()
    if not apply:
        return {"status": "DRY_RUN_PASS", "matrix": matrix, "target_writes": 0}
    root = _refresh_root(authority)
    existing_journal: dict[str, object] | None = None
    if os.path.lexists(root):
        if root.is_symlink() or not root.is_dir() or stat.S_IMODE(os.lstat(root).st_mode) != 0o700:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RESUME_JOURNAL_INVALID")
        journal_path = root / "journal.json"
        if not journal_path.is_file() or journal_path.is_symlink():
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RESUME_JOURNAL_INVALID")
        journal = _validate_journal(
            _json_document(_read_regular(journal_path), "JOURNAL"), authority=authority,
            before=before, after=after,
        )
        if journal.get("status") == "COMMITTED":
            _verify_committed_projection(root, journal, authority=authority, before=before, after=after)
            return {"status": "ALREADY_COMMITTED", "matrix": matrix, "journal": str(journal_path)}
        if journal.get("status") == "ROLLBACK_REQUIRED":
            try:
                _rollback_entries(root, journal)
            except BaseException as exc:
                exc.add_note("terminal refresh rollback failed; ROLLBACK_REQUIRED journal is retained")
                raise
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLED_BACK")
        if journal.get("status") == "ROLLED_BACK":
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_ROLLED_BACK")
        if journal.get("status") != "PREPARED":
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RESUME_JOURNAL_INVALID")
        existing_journal = journal
    else:
        _create_refresh_root(root)
    frozen: dict[str, FileSnapshot] = {}
    if existing_journal is None:
        for role in roles:
            descriptor = _require_mapping(preimage[role], role)
            frozen[role] = _sealed_snapshot(
                Path(str(descriptor["path"])), payload=before[role], mode=int(descriptor["mode"]),
                label=f"terminal refresh {role}",
            )
    journal_body = existing_journal or {
        "schema_version": "qixi-terminal-evidence-refresh-journal.v1",
        "status": "PREPARED",
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "authority_sha256": authority["authority_sha256"],
        "entries": [
            {
                "role": role,
                "path": _require_mapping(preimage[role], role)["path"],
                "before_sha256": "sha256:" + hashlib.sha256(before[role]).hexdigest(),
                "after_sha256": "sha256:" + hashlib.sha256(after[role]).hexdigest(),
                "after_bytes_b64": base64.b64encode(after[role]).decode("ascii"),
                "before_bytes_b64": base64.b64encode(before[role]).decode("ascii"),
                "before_mode": frozen[role].mode,
                "before_device": frozen[role].device,
                "before_inode": frozen[role].inode,
                "after_mode": frozen[role].mode,
                "backup_name": f"{role}.before",
                "staged_name": ".{name}.terminal-refresh-{role}.tmp".format(
                    name=Path(str(_require_mapping(preimage[role], role)["path"])).name,
                    role=role,
                ),
                "staged_device": None,
                "staged_inode": None,
                "installed_device": None,
                "installed_inode": None,
                "phase": "PREPARED",
            }
            for role in roles
        ],
        "matrix": matrix,
    }
    journal_body["journal_sha256"] = _journal_sha256(journal_body)
    journal_path = root / "journal.json"
    if existing_journal is None:
        _write_new(journal_path, _json_bytes(journal_body))
    entries = journal_body["entries"]
    assert isinstance(entries, list)
    try:
        for index, entry in enumerate(entries):
            assert isinstance(entry, Mapping)
            target = Path(str(entry["path"]))
            if entry["phase"] == "PREPARED":
                _ensure_backup(root, entry)
                _checkpoint(root, journal_body, index, phase="INSTALLING")
                entry = entries[index]
            assert isinstance(entry, Mapping)
            if entry["phase"] == "INSTALLING":
                staged_path = _entry_staged_path(entry)
                payload = base64.b64decode(str(entry["after_bytes_b64"]))
                target_after = _sealed_snapshot(
                    target, payload=payload, mode=int(entry["after_mode"]),
                    label="terminal refresh crash-after-rename",
                ) if _read_regular(target) == payload else None
                if target_after is not None and os.path.lexists(_stage_marker(entry)):
                    marker = _json_document(_read_regular(_stage_marker(entry)), "STAGE_OWNER")
                    if marker.get("inode") != target_after.inode or marker.get("device") != target_after.device:
                        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_STAGED_OWNER_DRIFT")
                    _checkpoint(root, journal_body, index, phase="INSTALLED", installed_device=target_after.device, installed_inode=target_after.inode)
                    _stage_marker(entry).unlink()
                    continue
                if os.path.lexists(staged_path):
                    staged = _adopt_staged(entry)
                else:
                    staged = create_staged_inode(
                        staged_path,
                        payload=payload,
                        mode=int(entry["after_mode"]),
                        label="terminal refresh stage",
                    )
                    _write_stage_marker(entry, staged)
                _checkpoint(
                    root,
                    journal_body,
                    index,
                    staged_device=staged.device,
                    staged_inode=staged.inode,
                )
                entry = entries[index]
                assert isinstance(entry, Mapping)
                before_snapshot = (
                    frozen[str(entry["role"])] if existing_journal is None
                    else journal_before_snapshot(entry, target=target)
                )
                installed = install_checkpointed_inode(
                    staged, target=target, expected_before=before_snapshot,
                    callbacks=InstallCallbacks(
                        checkpoint_installed=lambda snap: _checkpoint(root, journal_body, index, phase="INSTALLED", installed_device=snap.device, installed_inode=snap.inode),
                        verify_installed=lambda snap: _sealed_snapshot(target, payload=payload, mode=snap.mode, label="terminal refresh install"),
                    ), label="terminal refresh install",
                )
                if installed.sha256 != entry["after_sha256"]:
                    raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_INSTALL_DRIFT")
                _stage_marker(entry).unlink()
    except Exception as failure:
        journal_body["status"] = "ROLLBACK_REQUIRED"
        try:
            _write_journal(journal_path, journal_body)
        except Exception as checkpoint_error:
            failure.add_note(
                "terminal refresh rollback state could not be checkpointed: "
                + type(checkpoint_error).__name__
            )
        try:
            _rollback_entries(root, journal_body)
        except BaseException as rollback_error:
            failure.add_note(
                "terminal refresh rollback failed; ROLLBACK_REQUIRED journal is retained: "
                + type(rollback_error).__name__
            )
        else:
            failure.add_note("terminal refresh rollback completed; ROLLED_BACK journal is retained")
        raise
    journal_body["status"] = "COMMITTED"
    _write_journal(journal_path, journal_body)
    _verify_committed_projection(root, journal_body, authority=authority, before=before, after=after)
    return {"status": "COMMITTED", "matrix": matrix, "journal": str(journal_path)}


def validate_committed_refresh(*, repo_root: Path = ROOT) -> None:
    """Replay the fixed terminal-refresh successor after basename recovery."""

    authority = load_authority(repo_root)
    predecessor = _require_mapping(authority["predecessor_recovery"], "PREDECESSOR")
    for role in ("journal", "receipt"):
        descriptor = _require_mapping(predecessor.get(role), "PREDECESSOR")
        payload = _read_regular(Path(str(descriptor.get("path") or "")))
        if len(payload) != descriptor.get("bytes") or "sha256:" + hashlib.sha256(payload).hexdigest() != descriptor.get("sha256"):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_PREDECESSOR_DRIFT")
    try:
        from src.autoslice.qixi_operator_exact_title_source_fact import load_authority as load_title_authority
        from src.autoslice.qixi_post_correction_public_artifact_recovery import validate_committed_successor

        title_authority = load_title_authority(CANDIDATE_ID, repo_root=repo_root)
        if title_authority is None:
            raise ValueError("title authority missing")
        public = title_authority.document["source_binding"]["public_surface_authority"]
        if not isinstance(public, Mapping):
            raise ValueError("public authority binding missing")
        # Recovery owns its public authority parser and journal semantics.
        from src.autoslice import qixi_post_correction_public_surface as public_surface

        public_payload = _read_regular(repo_root / str(public["relative_path"]))
        public_document = public_surface.validate_authority(json.loads(public_payload))
        validate_committed_successor(public_document)
    except (OSError, ValueError, KeyError) as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_PREDECESSOR_INVALID") from exc
    root = _refresh_root(authority)
    journal_path, receipt_path = root / "journal.json", root / "receipt.json"
    journal = _json_document(_read_regular(journal_path), "JOURNAL")
    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
    before: dict[str, bytes] = {}
    after: dict[str, bytes] = {}
    for row in entries:
        if not isinstance(row, Mapping) or not isinstance(row.get("role"), str):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID")
        role = str(row["role"])
        try:
            before[role] = base64.b64decode(str(row["before_bytes_b64"]), validate=True)
            after[role] = base64.b64decode(str(row["after_bytes_b64"]), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_JOURNAL_SCHEMA_INVALID") from exc
    _verify_committed_projection(root, journal, authority=authority, before=before, after=after)
    receipt = _json_document(_read_regular(receipt_path), "RECEIPT")
    if receipt != _receipt_for(journal):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RECEIPT_DRIFT")
    current_chat = _json_document(after["chat"], "COMMITTED_CHAT")
    current_record = _json_document(after["record"], "COMMITTED_RECORD")
    current_delivery = _json_document(after["delivery_record"], "COMMITTED_DELIVERY")
    current_publish = _json_document(after["publish"], "COMMITTED_PUBLISH")
    if current_record != current_delivery:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_RECORD_MIRROR_DRIFT")
    final_srt = _read_regular(Path(str(_require_mapping(authority["preimage"], "PREIMAGE")["srt"]["path"]))).decode("utf-8")
    final_hash = _sha(final_srt)
    if any(current_chat.get(key) != final_hash[7:] for key in (
        "final_text_srt_sha256", "final_speaker_srt_sha256", "final_output_srt_sha256",
    )):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMITTED_CHAT_DRIFT")
    final_audit = _require_mapping(current_chat.get("final_review_audit"), "COMMITTED_FINAL_REVIEW")
    try:
        validate_final_review_release(final_audit, expected_srt_sha256=final_hash)
    except FinalReviewContractError as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMITTED_FINAL_REVIEW_DRIFT") from exc
    boundary = _require_mapping(final_audit.get("boundary_semantic_review"), "COMMITTED_BOUNDARY")
    story = _require_mapping(current_record.get("story_contract"), "COMMITTED_STORY")
    record_boundary = _require_mapping(current_record.get("boundary_audit"), "COMMITTED_RECORD_BOUNDARY")
    hashes = _require_mapping(current_record.get("artifact_hashes"), "COMMITTED_HASHES")
    if (
        story.get("boundary_semantic_review") != boundary
        or record_boundary.get("final_delivery_boundary_semantic_review") != boundary
        or hashes.get("chat_authority_audit_sha256") != "sha256:" + hashlib.sha256(after["chat"]).hexdigest()
        or current_publish.get("story_contract") != story
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_COMMITTED_CONTEXT_DRIFT")


def build_staged_refresh(
    *,
    repo_root: Path,
    authority: Mapping[str, object],
    runtime: Mapping[str, bytes],
    adapters: TextPipelineAdapters,
    final_review_llm: Callable[[str], str] | None = None,
    boundary_review_llm: Callable[[str], str] | None = None,
) -> tuple[dict[str, bytes], dict[str, object]]:
    """Run both canonical provider reviews in memory before any target write."""

    repository = _require_mapping(authority["repository_preimage"], "REPOSITORY_PREIMAGE")
    old_path = repo_root / str(repository["path"])
    old_bytes = _read_regular(old_path)
    if (
        len(old_bytes) != repository["bytes"]
        or "sha256:" + hashlib.sha256(old_bytes).hexdigest() != repository["sha256"]
    ):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_REPOSITORY_PREIMAGE_DRIFT")
    final_srt = runtime["srt"].decode("utf-8")
    before_srt = old_bytes.decode("utf-8")
    record = _json_document(runtime["record"], "RECORD")
    delivery = _json_document(runtime["delivery_record"], "DELIVERY_RECORD")
    publish = _json_document(runtime["publish"], "PUBLISH")
    state = _json_document(runtime["state"], "STATE")
    clip_context = _json_document(runtime["clip_context"], "CLIP_CONTEXT")
    chat = _json_document(runtime["chat"], "CHAT")
    story = _require_mapping(record.get("story_contract"), "STORY")
    boundary = _require_mapping(record.get("boundary_audit"), "BOUNDARY")
    start, end = boundary.get("final_start_ms"), boundary.get("final_end_ms")
    if not isinstance(start, int) or not isinstance(end, int) or not start < end:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_BOUNDARY_RANGE_INVALID")
    result = refresh_terminal_evidence(
        before_srt=before_srt,
        final_srt=final_srt,
        correction=_json_document(runtime["correction"], "CORRECTION"),
        old_chat_authority=chat,
        selection_hook=str(story.get("selection_hook") or ""),
        selection_scorecard=story.get("selection_scorecard"),
        structured_context=_final_review_structured_context(
            selection_hook=str(story.get("selection_hook") or ""),
            authoritative_chat=_canonical_chat_from_applied(chat),
        ),
        clip_context=clip_context,
        source_final_start_ms=start,
        source_final_end_ms=end,
        boundary_max_forward_ms=int(boundary.get("boundary_repair_extend_cap_ms") or 30_000),
        adapters=adapters,
        authoritative_chat=_canonical_chat_from_applied(chat),
        final_review_llm=final_review_llm,
        boundary_review_llm=boundary_review_llm,
    )
    chat_bytes = _json_bytes(_require_mapping(result["chat_authority"], "REFRESHED_CHAT"))
    projected = project_evidence_mirrors(
        record=record,
        delivery_record=delivery,
        publish=publish,
        state=state,
        refreshed_chat_bytes=chat_bytes,
        final_delivery_boundary=_require_mapping(
            result["final_delivery_boundary_semantic_review"], "REFRESHED_BOUNDARY"
        ),
        allowed_mutations=_require_mapping(authority["allowed_mutations"], "ALLOWLIST"),
    )
    return (
        {
            "chat": chat_bytes,
            "record": _json_bytes(projected["record"]),
            "delivery_record": _json_bytes(projected["delivery_record"]),
            "publish": _json_bytes(projected["publish"]),
            "state": _json_bytes(projected["state"]),
        },
        result,
    )


def _json_document(payload: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiTerminalEvidenceRefreshError(
            f"QIXI_TERMINAL_REFRESH_{label}_JSON_INVALID"
        ) from exc
    return _require_mapping(value, label)


def _canonical_chat_from_applied(chat: Mapping[str, object]) -> tuple[ChatEvidence, ...]:
    """Rehydrate only the retained, final-delivery chat evidence."""

    applied = chat.get("applied")
    if not isinstance(applied, list):
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CHAT_EVIDENCE_MISSING")
    rows: list[ChatEvidence] = []
    for row in applied:
        if not isinstance(row, Mapping) or row.get("survived_final_text_srt") is not True:
            continue
        kind, text, offset = row.get("kind"), row.get("exact_text"), row.get("source_offset_ms")
        if not isinstance(kind, str) or not isinstance(text, str) or not isinstance(offset, int):
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CHAT_EVIDENCE_INVALID")
        rows.append(ChatEvidence(kind=kind, text=text, offset_ms=offset, sender=str(row.get("sender") or ""), source=str(row.get("source") or ""), source_sha256=str(row.get("source_sha256") or ""), source_event_id=str(row.get("source_event_id") or "")))
    if not rows:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_CHAT_EVIDENCE_MISSING")
    return tuple(rows)
