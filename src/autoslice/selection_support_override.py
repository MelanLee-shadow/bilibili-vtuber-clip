"""Sealed, one-use human authority for a terminal selection-support verdict.

Unlike ``talk-selection-override.v1``, this cannot rewrite a scorecard, hook,
source window, queue position, or upload permission.  It solely releases the
selection-support gate for one terminal, candidate-bound refresh verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from src.autoslice.historical_fastlane_authority import (
    HistoricalFastlaneAuthorityError,
    _canonical,
    _create_only,
    _mkdir_private,
    _read_small_regular,
    _replace_owned,
    _sha,
    _state_path,
    _state_terminal_newline,
    _streaming_regular_fingerprint,
    _self_bound,
    _validate_self_bound,
    exclusive_tick,
)
from src.autoslice.producer_delivery_transaction import deployment_authority_binding
from src.autoslice.runner_state_writeback import (
    exclusive_runner_commit,
    read_exact_state_preimage,
    state_bytes,
    write_exact_state_bytes_under_lease,
)


SCHEMA = "selection-support-only-authority.v1"
CONSUMPTION_SCHEMA = "selection-support-only-consumption.v1"
STATE_KEY = "operator_selection_support_overrides"
NAMESPACE = ".selection-support-overrides"
MAX_LIFETIME = timedelta(hours=6)
_RULING_CANDIDATE_ID = "auto_123036_727_785"
_RULING_EVENT_ID = "0df2296b-500a-4681-ab5e-6fb46dc39579"
_RULING_USER_ID = "555195ed-ec18-418d-a311-558f7e54291f"
_RULING_TIMESTAMP = "2026-08-19T00:08:52.249Z"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_NONCE = re.compile(r"selection-support-[a-z0-9][a-z0-9-]{5,95}\Z")
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FIELDS = frozenset({
    "schema_version", "status", "nonce", "recording_date", "candidate_id",
    "candidate_binding", "candidate_binding_sha256", "scorecard_sha256",
    "terminal_refresh_receipt_sha256", "terminal_refresh_canonical_sha256",
    "terminal_source_binding", "terminal_source_binding_sha256",
    "current_source_binding", "current_source_binding_sha256",
    "authorization", "deployed_authority", "state_before_sha256",
    "state_after_sha256", "authority_binding_sha256", "expires_at",
    "upload_allowed", "prepared_receipt_sha256", "receipt_sha256",
})
_CONSUMED_MARKER_FIELDS = frozenset({
    "schema_version", "status", "candidate_id", "authority_path", "nonce",
    "authority_binding_sha256", "authority_receipt_sha256", "consumption",
})


class SelectionSupportOverrideError(RuntimeError):
    """A selection-support-only human authority is absent or unsafe."""


def _canonical_sha(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_EXPIRY_INVALID") from exc
    if parsed.tzinfo is None:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_EXPIRY_INVALID")
    return parsed.astimezone(timezone.utc)


def _cid(row: Mapping[str, object]) -> str:
    return str(row.get("cid") or row.get("candidate_id") or "")


def _find_row(state: Mapping[str, object], candidate_id: str) -> tuple[str, dict[str, object]]:
    matches = [
        (collection, row)
        for collection in ("pending_talk", "talk_backlog")
        for row in (state.get(collection) if isinstance(state.get(collection), list) else [])
        if isinstance(row, dict) and _cid(row) == candidate_id
    ]
    if len(matches) != 1:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_CANDIDATE_AMBIGUOUS")
    return matches[0]


def _terminal_receipt(row: Mapping[str, object], candidate_id: str) -> dict[str, object]:
    receipt = row.get("semantic_evidence_scorecard_refresh")
    if not isinstance(receipt, dict):
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_TERMINAL_MISSING")
    body = dict(receipt)
    declared = body.pop("receipt_sha256", None)
    attempts = receipt.get("attempts")
    valid_attempts = isinstance(attempts, list) and len(attempts) == 3 and all(
        isinstance(attempt, dict)
        and attempt.get("attempt_number") == index
        and attempt.get("outcome") == "UNSUPPORTED"
        and attempt.get("reason_code") == "REFRESH_HOOK_UNSUPPORTED"
        for index, attempt in enumerate(attempts, start=1)
    )
    provenance = receipt.get("input_provenance")
    if not (
        receipt.get("schema_version") == "semantic-evidence-scorecard-refresh-receipt.v1"
        and receipt.get("candidate_id") == candidate_id
        and receipt.get("status") == "BLOCKED_TERMINAL"
        and receipt.get("reason_code") == "REFRESH_HOOK_UNSUPPORTED"
        and isinstance(declared, str)
        and declared == _canonical_sha(body)
        and valid_attempts
        and isinstance(provenance, dict)
        and set(provenance) == {"source_media", "bcut_srt", "semantic_chat", "semantic_chat_source"}
        and all(isinstance(provenance[key], dict) for key in provenance)
    ):
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_TERMINAL_INVALID")
    return receipt


def _binding(row: Mapping[str, object], candidate_id: str) -> dict[str, object]:
    scorecard = row.get("selection_scorecard")
    if not isinstance(scorecard, dict):
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_SCORECARD_INVALID")
    value = {
        "candidate_id": candidate_id,
        "lane": row.get("lane"), "hook": row.get("hook"),
        "start_ms": row.get("start_ms"), "end_ms": row.get("end_ms"),
        "segment_path": row.get("segment_path"), "bcut_srt_path": row.get("bcut_srt_path"),
        "xml_path": row.get("xml"),
        "semantic_chat_evidence": scorecard.get("semantic_recall_chat_evidence"),
    }
    if (
        value["lane"] not in {"semantic_recall", "semantic_recall_sharded"}
        or not isinstance(value["hook"], str) or not value["hook"].strip()
        or not isinstance(value["start_ms"], int) or not isinstance(value["end_ms"], int)
        or value["start_ms"] >= value["end_ms"]
        or not all(isinstance(value[key], str) and value[key] for key in ("segment_path", "bcut_srt_path", "xml_path"))
        or not isinstance(value["semantic_chat_evidence"], dict)
    ):
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_BINDING_INVALID")
    return value


def _terminal_source_binding(
    row: Mapping[str, object], terminal: Mapping[str, object]
) -> dict[str, object]:
    """Rebind receipt source proofs to the candidate's current exact paths."""
    provenance = terminal["input_provenance"]
    assert isinstance(provenance, Mapping)
    media, bcut, chat_source = (
        provenance["source_media"], provenance["bcut_srt"], provenance["semantic_chat_source"]
    )
    if not (
        isinstance(media, Mapping) and media.get("path") == row.get("segment_path")
        and isinstance(bcut, Mapping) and bcut.get("path") == row.get("bcut_srt_path")
        and isinstance(chat_source, Mapping) and chat_source.get("path") == row.get("xml")
    ):
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_SOURCE_DRIFT")
    return dict(provenance)


def _validate_live_source(
    binding: Mapping[str, object], *, require_hash: bool, require_inode: bool
) -> dict[str, object]:
    """Compare current source identity without retaining media payload bytes."""
    path = Path(str(binding.get("path") or ""))
    try:
        observed = os.lstat(path)
    except OSError as exc:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_SOURCE_DRIFT") from exc
    observed_binding = {
        "path": str(path),
        "size_bytes": observed.st_size,
        "mtime_ns": observed.st_mtime_ns,
        "ctime_ns": observed.st_ctime_ns,
        "device": observed.st_dev,
        "inode": observed.st_ino,
        "mode": stat.S_IMODE(observed.st_mode),
    }
    if not require_inode:
        # CloudFS may remount the same immutable object on a new inode.  Never
        # retain that unstable identity in a current binding we later compare.
        observed_binding.pop("inode")
    compared = tuple(observed_binding)
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode) or any(
        binding.get(key) != observed_binding[key] for key in compared
    ):
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_SOURCE_DRIFT")
    if require_hash:
        try:
            fingerprint = _streaming_regular_fingerprint(path, label="selection_support_source")
        except HistoricalFastlaneAuthorityError as exc:
            raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_SOURCE_DRIFT") from exc
        if fingerprint is None or binding.get("sha256") != fingerprint.sha256:
            raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_SOURCE_DRIFT")
        observed_binding["sha256"] = fingerprint.sha256
    return observed_binding


def _current_source_binding(terminal: Mapping[str, object]) -> dict[str, object]:
    provenance = terminal["input_provenance"]
    assert isinstance(provenance, Mapping)
    # CloudFS may remount the same immutable source on a new inode.  Media and
    # XML therefore bind no-follow path/stat identity without inode equality;
    # XML/BCUT additionally hash their bounded control content.  Historical
    # source inventory performs the expensive whole-media SHA before STARTED.
    return {
        "source_media": _validate_live_source(provenance["source_media"], require_hash=False, require_inode=False),
        "bcut_srt": _validate_live_source(provenance["bcut_srt"], require_hash=True, require_inode=True),
        "semantic_chat_source": _validate_live_source(provenance["semantic_chat_source"], require_hash=True, require_inode=False),
    }


def _authorization(value: Mapping[str, object]) -> dict[str, object]:
    expected = {"source_path", "source_sha256", "event_id", "user_id", "timestamp", "scope"}
    if set(value) != expected or value.get("scope") != "SELECTION_SUPPORT_ONLY":
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_AUTHORIZATION_INVALID")
    if not all(isinstance(value.get(key), str) and value.get(key) for key in expected):
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_AUTHORIZATION_INVALID")
    if _SHA.fullmatch(str(value["source_sha256"])) is None:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_AUTHORIZATION_INVALID")
    if (
        value.get("event_id") != _RULING_EVENT_ID
        or value.get("user_id") != _RULING_USER_ID
        or value.get("timestamp") != _RULING_TIMESTAMP
    ):
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_AUTHORIZATION_INVALID")
    _utc(str(value["timestamp"]))
    return dict(value)


def _verify_authorization_source(root: Path, authorization: Mapping[str, object]) -> None:
    """Bind the sole v1 grant to Ivan's durable #7 and global fastlane ruling.

    A caller may name the source in a receipt, but cannot merely claim that it
    contains the human decision.  The deployed repository copy must be the
    regular, hash-bound review file and contain both limited grants.
    """
    value = _authorization(authorization)
    expected = root / "repo" / "docs/reviews/2026-08-19-ivan-review-batch-rulings.md"
    path = Path(value["source_path"])
    if path != expected:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_AUTHORIZATION_INVALID")
    try:
        fingerprint = _streaming_regular_fingerprint(path, label="selection_support_authorization")
        if fingerprint is None or fingerprint.mode not in {0o600, 0o644}:
            raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_AUTHORIZATION_INVALID")
        if fingerprint.sha256 != value["source_sha256"]:
            raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_AUTHORIZATION_INVALID")
        payload = _read_small_regular(path, fingerprint=fingerprint, label="selection_support_authorization")
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError, HistoricalFastlaneAuthorityError) as exc:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_AUTHORIZATION_INVALID") from exc
    if not (
        any(_RULING_CANDIDATE_ID in line and "修" in line for line in text.splitlines())
        and "以上我说的所有内容修复后都可以走快车道上传" in text
    ):
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_AUTHORIZATION_INVALID")


def _authority_binding(document: Mapping[str, object]) -> str:
    """Stable body identity; deliberately excludes state-after and receipt seal."""
    keys = (
        "schema_version", "nonce", "recording_date", "candidate_id",
        "candidate_binding_sha256", "scorecard_sha256",
        "terminal_refresh_receipt_sha256", "terminal_refresh_canonical_sha256",
        "terminal_source_binding_sha256", "current_source_binding_sha256",
        "authorization", "deployed_authority", "state_before_sha256", "expires_at",
        "upload_allowed",
    )
    return _canonical_sha({key: document[key] for key in keys})


def _marker(path: Path, document: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA, "status": "STAGED", "authority_path": str(path),
        "nonce": document["nonce"], "candidate_id": document["candidate_id"],
        "authority_binding_sha256": document["authority_binding_sha256"],
    }


def _state_after(state: Mapping[str, object], *, candidate_id: str, path: Path, document: Mapping[str, object]) -> dict[str, object]:
    after = deepcopy(dict(state))
    entries = after.get(STATE_KEY)
    if entries is None:
        entries = {}
        after[STATE_KEY] = entries
    if not isinstance(entries, dict) or candidate_id in entries:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_ALREADY_MATERIALIZED")
    entries[candidate_id] = _marker(path, document)
    return after


def _validate_document(document: object) -> dict[str, object]:
    try:
        value = _validate_self_bound(document, schema=SCHEMA, fields=_FIELDS, field="receipt_sha256")
    except HistoricalFastlaneAuthorityError as exc:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_RECEIPT_INVALID") from exc
    hashes = ("candidate_binding_sha256", "scorecard_sha256", "terminal_refresh_receipt_sha256", "terminal_refresh_canonical_sha256", "terminal_source_binding_sha256", "current_source_binding_sha256", "state_before_sha256", "state_after_sha256", "authority_binding_sha256")
    valid = (
        value.get("status") in {"PREPARED", "COMMITTED"}
        and isinstance(value.get("nonce"), str) and _NONCE.fullmatch(value["nonce"]) is not None
        and isinstance(value.get("recording_date"), str) and _DATE.fullmatch(value["recording_date"]) is not None
        and isinstance(value.get("candidate_id"), str) and bool(value["candidate_id"])
        and value.get("upload_allowed") is False
        and isinstance(value.get("candidate_binding"), dict)
        and isinstance(value.get("terminal_source_binding"), dict)
        and isinstance(value.get("current_source_binding"), dict)
        and isinstance(value.get("authorization"), dict)
        and isinstance(value.get("deployed_authority"), dict)
        and all(_SHA.fullmatch(str(value.get(key) or "")) for key in hashes)
        and value.get("authority_binding_sha256") == _authority_binding(value)
        and value.get("candidate_id") == _RULING_CANDIDATE_ID
    )
    if not valid:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_RECEIPT_INVALID")
    _authorization(value["authorization"])
    if value["status"] == "PREPARED":
        if value.get("prepared_receipt_sha256") is not None:
            raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_RECEIPT_INVALID")
    else:
        declared = value.get("prepared_receipt_sha256")
        prepared_body = {
            key: item for key, item in value.items()
            if key not in {"receipt_sha256", "prepared_receipt_sha256", "status"}
        } | {"status": "PREPARED", "prepared_receipt_sha256": None}
        expected = _self_bound(prepared_body, "receipt_sha256")["receipt_sha256"]
        if declared != expected:
            raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_RECEIPT_INVALID")
    _utc(str(value["expires_at"]))
    return value


def _safe_document(path: Path) -> dict[str, object]:
    try:
        fingerprint = _streaming_regular_fingerprint(path, label="selection_support_receipt")
        if fingerprint is None or fingerprint.mode != 0o600:
            raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_RECEIPT_INVALID")
        payload = _read_small_regular(path, fingerprint=fingerprint, label="selection_support_receipt")
        return _validate_document(json.loads(payload.decode("utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, HistoricalFastlaneAuthorityError) as exc:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_RECEIPT_INVALID") from exc


def prepare_selection_support_override(*, runtime_root: Path, date: str, candidate_id: str, nonce: str, expires_at: str, expected_state_sha256: str, expected_authority: Mapping[str, str], authorization: Mapping[str, object], now: datetime | None = None) -> tuple[Path, dict[str, object]]:
    """Validate a no-write authority document against the exact current row."""
    root = Path(runtime_root)
    moment = now or datetime.now(timezone.utc)
    if not _DATE.fullmatch(date) or candidate_id != _RULING_CANDIDATE_ID or _NONCE.fullmatch(nonce) is None:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_ARGUMENT_INVALID")
    if not moment < _utc(expires_at) <= moment + MAX_LIFETIME:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_EXPIRY_INVALID")
    authority = deployment_authority_binding(root)
    if authority != dict(expected_authority):
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_DEPLOYMENT_DRIFT")
    _verify_authorization_source(root, authorization)
    before = read_exact_state_preimage(_state_path(root, date), runtime_root=root)
    if before is None or _sha(before) != expected_state_sha256:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_STATE_DRIFT")
    state = json.loads(before)
    suffix = _state_terminal_newline(before, state)
    _collection, row = _find_row(state, candidate_id)
    terminal, binding = _terminal_receipt(row, candidate_id), _binding(row, candidate_id)
    source_binding = _terminal_source_binding(row, terminal)
    current_source_binding = _current_source_binding(terminal)
    document: dict[str, object] = {
        "schema_version": SCHEMA, "status": "PREPARED", "nonce": nonce,
        "recording_date": date, "candidate_id": candidate_id, "candidate_binding": binding,
        "candidate_binding_sha256": _canonical_sha(binding),
        "scorecard_sha256": _canonical_sha(row["selection_scorecard"]),
        "terminal_refresh_receipt_sha256": terminal["receipt_sha256"],
        "terminal_refresh_canonical_sha256": _canonical_sha(terminal),
        "terminal_source_binding": source_binding,
        "terminal_source_binding_sha256": _canonical_sha(source_binding),
        "current_source_binding": current_source_binding,
        "current_source_binding_sha256": _canonical_sha(current_source_binding),
        "authorization": _authorization(authorization), "deployed_authority": authority,
        "state_before_sha256": _sha(before), "state_after_sha256": "",
        "authority_binding_sha256": "", "expires_at": expires_at, "upload_allowed": False,
        "prepared_receipt_sha256": None,
    }
    document["authority_binding_sha256"] = _authority_binding(document)
    path = root / NAMESPACE / f"{date}-{nonce}.json"
    document["state_after_sha256"] = _sha(state_bytes(_state_after(state, candidate_id=candidate_id, path=path, document=document)) + suffix)
    return path, _self_bound(document, "receipt_sha256")


def create_or_apply_selection_support_override(path: Path, document: Mapping[str, object], *, runtime_root: Path, now: datetime | None = None) -> Path:
    """Durably bind PREPARED -> COMMITTED under tick then runner leases."""
    prepared = _validate_document(document)
    root = Path(runtime_root)
    if prepared["status"] != "PREPARED" or _utc(str(prepared["expires_at"])) <= (now or datetime.now(timezone.utc)):
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_REPLAY_REFUSED")
    if prepared["deployed_authority"] != deployment_authority_binding(root):
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_DEPLOYMENT_DRIFT")
    with exclusive_tick(root):
        with exclusive_runner_commit(root) as lease:
            if prepared["deployed_authority"] != deployment_authority_binding(root):
                raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_DEPLOYMENT_DRIFT")
            _verify_authorization_source(root, prepared["authorization"])
            namespace = _mkdir_private(root, NAMESPACE)
            if path.parent != namespace or path.name != f"{prepared['recording_date']}-{prepared['nonce']}.json":
                raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_NAMESPACE_INVALID")
            state_path = _state_path(root, str(prepared["recording_date"]))
            before = read_exact_state_preimage(state_path, runtime_root=root)
            if before is None:
                raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_STATE_DRIFT")
            state = json.loads(before)
            suffix = _state_terminal_newline(before, state)
            if _sha(before) == prepared["state_before_sha256"]:
                _collection, row = _find_row(state, str(prepared["candidate_id"]))
                terminal = _terminal_receipt(row, str(prepared["candidate_id"]))
                if (_binding(row, str(prepared["candidate_id"])) != prepared["candidate_binding"] or _canonical_sha(row["selection_scorecard"]) != prepared["scorecard_sha256"] or terminal["receipt_sha256"] != prepared["terminal_refresh_receipt_sha256"] or _terminal_source_binding(row, terminal) != prepared["terminal_source_binding"] or _current_source_binding(terminal) != prepared["current_source_binding"]):
                    raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_CANDIDATE_DRIFT")
                after = _state_after(state, candidate_id=str(prepared["candidate_id"]), path=path, document=prepared)
                after_bytes = state_bytes(after) + suffix
                if _sha(after_bytes) != prepared["state_after_sha256"]:
                    raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_AFTER_DRIFT")
                if path.exists() or path.is_symlink():
                    if _safe_document(path) != prepared:
                        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_RECEIPT_EXISTS")
                else:
                    _create_only(path, _canonical(prepared))
                write_exact_state_bytes_under_lease(state_path, runtime_root=root, lease=lease, expected_before=before, after_bytes=after_bytes)
                state = after
            elif _sha(before) == prepared["state_after_sha256"]:
                markers = state.get(STATE_KEY)
                if not isinstance(markers, Mapping) or markers.get(str(prepared["candidate_id"])) != _marker(path, prepared) or not path.exists() or _safe_document(path) != prepared:
                    raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_STATE_DRIFT")
            else:
                raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_STATE_DRIFT")
            committed = _self_bound({key: value for key, value in prepared.items() if key != "receipt_sha256"} | {"status": "COMMITTED", "prepared_receipt_sha256": prepared["receipt_sha256"]}, "receipt_sha256")
            _replace_owned(path, _canonical(committed))
    return path


def _document_matches_row(
    document: Mapping[str, object],
    row: Mapping[str, object],
    *,
    date: str,
    runtime_root: Path,
    now: datetime | None,
    require_unexpired: bool,
) -> bool:
    """Replay every immutable authority binding against the current row."""
    candidate_id = _cid(row)
    terminal = _terminal_receipt(row, candidate_id)
    binding = _binding(row, candidate_id)
    source_binding = _terminal_source_binding(row, terminal)
    current_source_binding = _current_source_binding(terminal)
    _verify_authorization_source(Path(runtime_root), document["authorization"])
    return bool(
        document["status"] == "COMMITTED"
        and document["recording_date"] == date
        and document["candidate_id"] == candidate_id == _RULING_CANDIDATE_ID
        and document["candidate_binding"] == binding
        and document["candidate_binding_sha256"] == _canonical_sha(binding)
        and document["scorecard_sha256"] == _canonical_sha(row.get("selection_scorecard"))
        and document["terminal_refresh_receipt_sha256"] == terminal["receipt_sha256"]
        and document["terminal_refresh_canonical_sha256"] == _canonical_sha(terminal)
        and document["terminal_source_binding"] == source_binding
        and document["terminal_source_binding_sha256"] == _canonical_sha(source_binding)
        and document["current_source_binding"] == current_source_binding
        and document["current_source_binding_sha256"] == _canonical_sha(current_source_binding)
        and document["deployed_authority"] == deployment_authority_binding(runtime_root)
        and document["upload_allowed"] is False
        and (
            not require_unexpired
            or _utc(str(document["expires_at"])) > (now or datetime.now(timezone.utc))
        )
    )


def _usable_document(row: Mapping[str, object], *, state: Mapping[str, object], date: str, runtime_root: Path, now: datetime | None) -> dict[str, object] | None:
    entries, candidate_id = state.get(STATE_KEY), _cid(row)
    if not isinstance(entries, Mapping):
        return None
    marker = entries.get(candidate_id)
    if not isinstance(marker, Mapping) or marker.get("status") != "STAGED":
        return None
    try:
        nonce, path = marker["nonce"], Path(str(marker["authority_path"]))
        namespace = Path(runtime_root) / NAMESPACE
        directory = os.lstat(namespace)
        if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None or stat.S_ISLNK(directory.st_mode) or not stat.S_ISDIR(directory.st_mode) or stat.S_IMODE(directory.st_mode) != 0o700 or path.parent != namespace or path.name != f"{date}-{nonce}.json":
            return None
        document = _safe_document(path)
        expected_marker = _marker(path, document)
        if not (
            marker == expected_marker
            and _document_matches_row(
                document, row, date=date, runtime_root=runtime_root, now=now, require_unexpired=True
            )
        ):
            return None
        return document
    except (KeyError, OSError, SelectionSupportOverrideError):
        return None


def selection_support_override_applies(row: Mapping[str, object], *, state: Mapping[str, object], date: str, runtime_root: Path, now: datetime | None = None) -> bool:
    return _usable_document(row, state=state, date=date, runtime_root=runtime_root, now=now) is not None


def consume_in_memory(row: dict[str, object], *, state: dict[str, object], date: str, runtime_root: Path, now: datetime | None = None) -> dict[str, object]:
    """Seal the one state-local consumption without altering the old verdict."""
    document = _usable_document(row, state=state, date=date, runtime_root=runtime_root, now=now)
    if document is None:
        raise SelectionSupportOverrideError("SELECTION_SUPPORT_OVERRIDE_UNUSABLE")
    candidate_id, terminal = _cid(row), _terminal_receipt(row, _cid(row))
    receipt = {
        "schema_version": CONSUMPTION_SCHEMA, "status": "CONSUMED_SELECTION_SUPPORT_ONLY",
        "candidate_id": candidate_id, "recording_date": date,
        "authority_binding_sha256": document["authority_binding_sha256"],
        "authority_receipt_sha256": document["receipt_sha256"],
        "terminal_refresh_receipt_sha256": terminal["receipt_sha256"],
        "scorecard_sha256": _canonical_sha(row["selection_scorecard"]), "upload_allowed": False,
    }
    receipt["receipt_sha256"] = _canonical_sha(receipt)
    entries = state[STATE_KEY]
    assert isinstance(entries, dict)
    previous = entries[candidate_id]
    assert isinstance(previous, Mapping)
    entries[candidate_id] = {
        "schema_version": SCHEMA,
        "status": "CONSUMED",
        "candidate_id": candidate_id,
        "authority_path": previous["authority_path"],
        "nonce": previous["nonce"],
        "authority_binding_sha256": document["authority_binding_sha256"],
        "authority_receipt_sha256": document["receipt_sha256"],
        "consumption": receipt,
    }
    return receipt


def terminal_selection_support_blocked(
    row: Mapping[str, object], candidate_id: str, *, state: Mapping[str, object], date: str, runtime_root: Path
) -> bool:
    entries = state.get(STATE_KEY)
    marker = entries.get(candidate_id) if isinstance(entries, Mapping) else None
    raw_terminal = row.get("semantic_evidence_scorecard_refresh")
    terminal_like = isinstance(raw_terminal, Mapping) and raw_terminal.get("status") == "BLOCKED_TERMINAL"
    try:
        _terminal_receipt(row, candidate_id)
    except SelectionSupportOverrideError:
        # A malformed terminal verdict is never an invitation to retry a
        # provider.  It remains a truth/state block, particularly where an
        # authority marker proves this row previously entered this narrow lane.
        return terminal_like or isinstance(marker, Mapping)
    receipt = marker.get("consumption") if isinstance(marker, Mapping) and marker.get("status") == "CONSUMED" else None
    if isinstance(receipt, Mapping) and isinstance(marker, Mapping):
        body = dict(receipt)
        declared = body.pop("receipt_sha256", None)
        try:
            path = Path(str(marker.get("authority_path") or ""))
            nonce = marker.get("nonce")
            namespace = Path(runtime_root) / NAMESPACE
            namespace_stat = os.lstat(namespace)
            document = _safe_document(path)
            if (
                set(marker) == _CONSUMED_MARKER_FIELDS
                and marker.get("schema_version") == SCHEMA
                and marker.get("status") == "CONSUMED"
                and marker.get("candidate_id") == candidate_id
                and receipt.get("schema_version") == CONSUMPTION_SCHEMA
                and receipt.get("status") == "CONSUMED_SELECTION_SUPPORT_ONLY"
                and receipt.get("candidate_id") == candidate_id
                and receipt.get("recording_date") == date
                and receipt.get("upload_allowed") is False
                and receipt.get("terminal_refresh_receipt_sha256") == _terminal_receipt(row, candidate_id)["receipt_sha256"]
                and receipt.get("scorecard_sha256") == _canonical_sha(row.get("selection_scorecard"))
                and receipt.get("authority_binding_sha256") == marker.get("authority_binding_sha256") == document.get("authority_binding_sha256")
                and receipt.get("authority_receipt_sha256") == marker.get("authority_receipt_sha256") == document.get("receipt_sha256")
                and isinstance(nonce, str)
                and _NONCE.fullmatch(nonce) is not None
                and not stat.S_ISLNK(namespace_stat.st_mode)
                and stat.S_ISDIR(namespace_stat.st_mode)
                and stat.S_IMODE(namespace_stat.st_mode) == 0o700
                and path.parent == namespace
                and path.name == f"{date}-{nonce}.json"
                and _document_matches_row(
                    document, row, date=date, runtime_root=runtime_root, now=None, require_unexpired=False
                )
                and declared == _canonical_sha(body)
            ):
                return False
        except (OSError, SelectionSupportOverrideError):
            pass
    return True


def historical_preflight(
    state: Mapping[str, object], *, date: str, candidate_ids: tuple[str, ...], runtime_root: Path, now: datetime | None = None
) -> None:
    """Refuse a historical STARTED authority before any provider work.

    A valid staged override is admission evidence; a consumed override has
    already released the only owned gate.  Every other terminal verdict needs
    fresh Ivan truth rather than a provider retry.
    """
    for candidate_id in candidate_ids:
        try:
            _collection, row = _find_row(state, candidate_id)
        except SelectionSupportOverrideError:
            continue
        if terminal_selection_support_blocked(row, candidate_id, state=state, date=date, runtime_root=runtime_root) and not selection_support_override_applies(
            row, state=state, date=date, runtime_root=runtime_root, now=now
        ):
            raise SelectionSupportOverrideError("SELECTION_SUPPORT_TERMINAL_BLOCKED")
