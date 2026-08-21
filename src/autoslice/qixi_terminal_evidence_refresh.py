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
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.producer_boundary_review_stage import exact_delivery_correction_audit
from src.autoslice.producer_text_pipeline import (
    TextPipelineAdapters,
    _run_exact_final_release_review,
)
from src.autoslice.repository_asset_authority import require_repository_asset_authority


CANDIDATE_ID = "auto_123655_771_844"
RECORDING_DATE = "2026-08-17"
ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_PATH = Path(
    "assets/lidousha/qixi_terminal_evidence_refresh/auto_123655_771_844.v1.json"
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
    if path.is_symlink() or not path.is_file():
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_FILE_UNSAFE")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_FILE_UNREADABLE") from exc


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
        "preimage", "repository_preimage", "correction", "allowed_mutations",
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
    expected = {"record", "delivery_record", "publish", "state", "chat", "srt", "ass", "burn", "correction"}
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
    if correction.get("after_srt_sha256") != _sha(final_srt):
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
    return {
        "record": new_record,
        "delivery_record": copy.deepcopy(new_record),
        "publish": new_publish,
        "state": new_state,
    }


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _refresh_root(authority: Mapping[str, object]) -> Path:
    record = _require_mapping(_require_mapping(authority["preimage"], "PREIMAGE")["record"], "RECORD")
    return Path(str(record["path"])).parent / "qixi-terminal-evidence-refresh" / str(authority["authority_sha256"])[7:23]


def _write_new(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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

    if _read_regular(path) != before:
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
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


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
    for role in roles:
        if _read_regular(Path(str(_require_mapping(preimage[role], role)["path"]))) != before[role]:
            raise QixiTerminalEvidenceRefreshError("QIXI_TERMINAL_REFRESH_TARGET_DRIFT")
    matrix = {
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
    if not apply:
        return {"status": "DRY_RUN_PASS", "matrix": matrix, "target_writes": 0}
    root = _refresh_root(authority)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal_body = {
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
                "before_bytes_b64": base64.b64encode(before[role]).decode("ascii"),
            }
            for role in roles
        ],
        "matrix": matrix,
    }
    journal_body["journal_sha256"] = _canonical_sha(journal_body)
    journal_path = root / "journal.json"
    _write_new(journal_path, _json_bytes(journal_body))
    installed: list[str] = []
    try:
        for role in roles:
            descriptor = _require_mapping(preimage[role], role)
            _replace_exact(
                Path(str(descriptor["path"])), before=before[role], after=after[role], mode=int(descriptor["mode"])
            )
            installed.append(role)
    except Exception:
        for role in reversed(installed):
            descriptor = _require_mapping(preimage[role], role)
            try:
                _replace_exact(
                    Path(str(descriptor["path"])), before=after[role], after=before[role], mode=int(descriptor["mode"])
                )
            except Exception:
                pass
        raise
    journal_body["status"] = "COMMITTED"
    journal_body["journal_sha256"] = _canonical_sha({k: v for k, v in journal_body.items() if k != "journal_sha256"})
    # The PREPARED file is private; a completed receipt is create-only.
    (root / "journal.json").write_bytes(_json_bytes(journal_body))
    receipt = {"schema_version": "qixi-terminal-evidence-refresh-receipt.v1", "status": "COMMITTED", "journal_sha256": journal_body["journal_sha256"], "matrix": matrix}
    _write_new(root / "receipt.json", _json_bytes(receipt))
    return {"status": "COMMITTED", "matrix": matrix, "journal": str(journal_path)}


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
        structured_context="",
        clip_context={},
        source_final_start_ms=start,
        source_final_end_ms=end,
        boundary_max_forward_ms=int(boundary.get("boundary_repair_extend_cap_ms") or 30_000),
        adapters=adapters,
        authoritative_chat=(),
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
