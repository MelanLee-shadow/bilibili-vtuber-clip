"""Candidate-scoped refresh for stale semantic-chat selection scorecards.

This lane deliberately does *not* rerun semantic discovery.  It only revisits
an already recalled ``semantic_recall`` candidate when a valid historical v2
operator-processing grant names that candidate and the row is still queued in
``pending_talk`` or ``talk_backlog``.  Hook and source boundaries are immutable;
only the deterministic selection scorecard is replaced.

The executor returns the number of refreshed cards when every in-scope row is
current.  A negative return value is the number of still-blocked rows, expressed
as ``-N``; the runner must persist state and return before prioritization.  This
keeps provider/source failures from accidentally producing with a stale card.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import LlmCallError, extract_json_object
from src.autoslice.operator_processing_scope import (
    FAILED_PICK_RECOVERY_GRANT_SCHEMA,
    FAILED_PICK_RECOVERY_INTENT,
    STATE_KEY as OPERATOR_SCOPE_STATE_KEY,
    operator_scope_admission,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice.selection_scorecard import (
    SelectionCalibrationPolicyError,
    apply_reviewed_selection_calibration,
    normalize_selection_scorecard,
    selection_scorecard_is_valid,
)
from src.autoslice.semantic_candidate_selector import (
    SEMANTIC_CHAT_POLICY_SHA256,
    _build_bounded_semantic_chat_evidence,
    _load_hash_bound_semantic_chat,
    _SCORECARD_RUBRIC_BLOCK,
)


REFRESH_RECEIPT_SCHEMA = "semantic-evidence-scorecard-refresh-receipt.v1"
REFRESH_RUN_SCHEMA = "semantic-evidence-scorecard-refresh-run.v1"
REFRESH_PROMPT_SCHEMA = "semantic-evidence-scorecard-refresh-prompt.v1"
REFRESH_STATE_KEY = "semantic_evidence_scorecard_refresh_run"
ROW_RECEIPT_KEY = "semantic_evidence_scorecard_refresh"
MAX_ATTEMPTS_PER_INPUT = 3
MAX_ATTEMPT_HISTORY = 12
MAX_CANDIDATES_PER_TICK = 2

_QUEUED_COLLECTIONS = ("pending_talk", "talk_backlog")
_SEMANTIC_LANES = {"semantic_recall", "semantic_recall_sharded"}
_TERMINAL_REASON_CODES = {
    "CANDIDATE_BINDING_INVALID",
    "CANDIDATE_WINDOW_HAS_NO_CUES",
    "REFRESH_HOOK_UNSUPPORTED",
    "REFRESH_CALIBRATION_REJECTED",
}
_PROVIDER_CONTRACT = {
    "schema_version": "semantic-evidence-scorecard-refresh-provider.v1",
    "transport": "command",
    "command": "scripts/llm_via_cpa.sh",
    "model_chain": ["gpt-5.6-sol", "gpt-5.5", "gpt-5.4"],
    "reasoning_effort": "medium",
    "timeout_seconds": 600,
    "prompt_schema": REFRESH_PROMPT_SCHEMA,
}
_runner = RunnerProxy()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


PROVIDER_CONTRACT_SHA256 = _canonical_sha256(_PROVIDER_CONTRACT)


def _bytes_sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _candidate_id(row: Mapping[str, object]) -> str:
    return str(row.get("cid") or row.get("candidate_id") or "").strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _regular_stat(path: Path) -> dict[str, object]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"not a regular non-symlink file: {path}")
    return {
        "path": str(path),
        "size_bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
    }


def _read_stable_bytes(path: Path) -> tuple[bytes, dict[str, object]]:
    before = _regular_stat(path)
    payload = path.read_bytes()
    after = _regular_stat(path)
    if before != after or before["size_bytes"] != len(payload):
        raise OSError(f"file drifted while reading: {path}")
    return payload, {**after, "sha256": _bytes_sha256(payload)}


class RefreshPreparationError(RuntimeError):
    def __init__(self, reason_code: str, evidence: Mapping[str, object]) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.evidence = dict(evidence)


@dataclass(frozen=True)
class Scope:
    grant_id: str
    candidate_ids: tuple[str, ...]
    grant_sha256: str


@dataclass(frozen=True)
class PreparedRefresh:
    row: dict[str, object]
    collection: str
    candidate_id: str
    candidate_binding: dict[str, object]
    candidate_binding_sha256: str
    old_scorecard_sha256: str
    cues: tuple[SourceCue, ...]
    chat_prompt_block: str
    chat_provenance: dict[str, object]
    input_provenance: dict[str, object]
    attempt_fingerprint: str


def _historical_v2_scope(
    date: str,
    state: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> Scope | None:
    moment = now or datetime.now(timezone.utc)
    if date >= moment.astimezone(timezone.utc).date().isoformat():
        return None
    raw_grant = state.get(OPERATOR_SCOPE_STATE_KEY)
    if (
        not isinstance(raw_grant, Mapping)
        or raw_grant.get("schema_version") != FAILED_PICK_RECOVERY_GRANT_SCHEMA
        or raw_grant.get("intent") != FAILED_PICK_RECOVERY_INTENT
    ):
        return None
    admission = operator_scope_admission(state, date=date, now=moment)
    if (
        not admission.admitted
        or not isinstance(admission.disclosure, Mapping)
        or admission.disclosure.get("intent") != FAILED_PICK_RECOVERY_INTENT
        or not admission.grant_id
    ):
        return None
    return Scope(
        grant_id=admission.grant_id,
        candidate_ids=admission.candidate_ids,
        grant_sha256=_canonical_sha256(raw_grant),
    )


def _scoped_rows(
    state: Mapping[str, object], scope: Scope
) -> tuple[list[tuple[str, dict[str, object]]], tuple[str, ...]]:
    by_id: dict[str, list[tuple[str, dict[str, object]]]] = {}
    allowed = set(scope.candidate_ids)
    for collection in _QUEUED_COLLECTIONS:
        rows = state.get(collection)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            candidate_id = _candidate_id(row)
            if candidate_id in allowed and row.get("lane") in _SEMANTIC_LANES:
                by_id.setdefault(candidate_id, []).append((collection, row))
    duplicates = tuple(
        sorted(candidate_id for candidate_id, rows in by_id.items() if len(rows) != 1)
    )
    unique = [rows[0] for candidate_id, rows in sorted(by_id.items()) if len(rows) == 1]
    return unique, duplicates


def _raw_candidate_binding(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "candidate_id": _candidate_id(row),
        "lane": row.get("lane"),
        "hook": row.get("hook"),
        "start_ms": row.get("start_ms"),
        "end_ms": row.get("end_ms"),
        "segment_path": row.get("segment_path"),
        "bcut_srt_path": row.get("bcut_srt_path"),
        "xml_path": row.get("xml"),
    }


def _validated_candidate_binding(row: Mapping[str, object]) -> dict[str, object]:
    binding = _raw_candidate_binding(row)
    start_ms = binding["start_ms"]
    end_ms = binding["end_ms"]
    if (
        not binding["candidate_id"]
        or binding["lane"] not in _SEMANTIC_LANES
        or not isinstance(binding["hook"], str)
        or not str(binding["hook"]).strip()
        or isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or start_ms < 0
        or end_ms <= start_ms
        or any(
            not isinstance(binding[key], str) or not str(binding[key]).strip()
            for key in ("segment_path", "bcut_srt_path", "xml_path")
        )
        or any(
            not Path(str(binding[key])).is_absolute()
            for key in ("segment_path", "bcut_srt_path", "xml_path")
        )
    ):
        raise RefreshPreparationError(
            "CANDIDATE_BINDING_INVALID",
            {"candidate_binding": binding},
        )
    return binding


def _window_cues(payload: bytes, *, start_ms: int, end_ms: int) -> tuple[SourceCue, ...]:
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise RefreshPreparationError(
            "BCUT_SRT_UNREADABLE",
            {"error_type": type(exc).__name__},
        ) from exc
    selected = [
        cue
        for cue in parse_srt_cues(text)
        if cue.text.strip() and cue.end_ms > start_ms and cue.start_ms < end_ms
    ]
    if not selected:
        raise RefreshPreparationError(
            "CANDIDATE_WINDOW_HAS_NO_CUES",
            {"start_ms": start_ms, "end_ms": end_ms},
        )
    return tuple(
        SourceCue(
            cue_id=f"refresh_{position:04d}",
            source_start_ms=cue.start_ms,
            source_end_ms=cue.end_ms,
            text=cue.text.strip(),
            language="zh",
            kind="speech",
            confidence=1.0,
        )
        for position, cue in enumerate(selected, start=1)
    )


def _prepare(
    collection: str,
    row: dict[str, object],
) -> PreparedRefresh:
    binding = _validated_candidate_binding(row)
    candidate_id = str(binding["candidate_id"])
    base_evidence: dict[str, object] = {"candidate_binding": binding}
    try:
        source_stat = _regular_stat(Path(str(binding["segment_path"])))
    except OSError as exc:
        raise RefreshPreparationError(
            "SOURCE_MEDIA_UNAVAILABLE",
            {**base_evidence, "error_type": type(exc).__name__},
        ) from exc
    try:
        bcut_payload, bcut_binding = _read_stable_bytes(Path(str(binding["bcut_srt_path"])))
    except OSError as exc:
        raise RefreshPreparationError(
            "BCUT_SRT_UNAVAILABLE",
            {**base_evidence, "source_media": source_stat, "error_type": type(exc).__name__},
        ) from exc
    try:
        cues = _window_cues(
            bcut_payload,
            start_ms=int(binding["start_ms"]),
            end_ms=int(binding["end_ms"]),
        )
    except RefreshPreparationError as exc:
        raise RefreshPreparationError(
            exc.reason_code,
            {
                **base_evidence,
                "source_media": source_stat,
                "bcut_srt": bcut_binding,
                **exc.evidence,
            },
        ) from exc
    xml_path = Path(str(binding["xml_path"]))
    try:
        xml_stat_before = _regular_stat(xml_path)
    except OSError as exc:
        raise RefreshPreparationError(
            "CHAT_SOURCE_UNAVAILABLE",
            {
                **base_evidence,
                "source_media": source_stat,
                "bcut_srt": bcut_binding,
                "error_type": type(exc).__name__,
            },
        ) from exc
    chat_items, load_receipt = _load_hash_bound_semantic_chat(xml_path)
    try:
        xml_stat_after = _regular_stat(xml_path)
    except OSError as exc:
        raise RefreshPreparationError(
            "CHAT_SOURCE_DRIFT",
            {**base_evidence, "chat_load": load_receipt, "error_type": type(exc).__name__},
        ) from exc
    if xml_stat_before != xml_stat_after:
        raise RefreshPreparationError(
            "CHAT_SOURCE_DRIFT",
            {**base_evidence, "chat_load": load_receipt},
        )
    if load_receipt.get("status") != "LOADED":
        raise RefreshPreparationError(
            "CHAT_SOURCE_NOT_LOADED",
            {**base_evidence, "chat_load": load_receipt},
        )
    chat_block, chat_receipt = _build_bounded_semantic_chat_evidence(
        chat_items,
        window_start_ms=int(binding["start_ms"]),
        window_end_ms=int(binding["end_ms"]),
        load_receipt=load_receipt,
    )
    if not isinstance(chat_block, str) or not chat_block:
        raise RefreshPreparationError(
            "CHAT_EVIDENCE_BUILD_FAILED",
            {**base_evidence, "chat_load": load_receipt},
        )
    chat_provenance = {
        key: chat_receipt[key]
        for key in (
            "schema_version",
            "algorithm_id",
            "status",
            "policy_sha256",
            "source_sha256",
            "evidence_sha256",
            "window_start_ms",
            "window_end_ms",
        )
    }
    input_provenance = {
        "source_media": source_stat,
        "bcut_srt": bcut_binding,
        "semantic_chat": chat_provenance,
    }
    candidate_binding_sha256 = _canonical_sha256(binding)
    old_scorecard_sha256 = _canonical_sha256(row.get("selection_scorecard"))
    attempt_fingerprint = _canonical_sha256(
        {
            "schema_version": REFRESH_PROMPT_SCHEMA,
            "candidate_binding_sha256": candidate_binding_sha256,
            "old_scorecard_sha256": old_scorecard_sha256,
            "input_provenance": input_provenance,
            "provider_contract_sha256": PROVIDER_CONTRACT_SHA256,
        }
    )
    return PreparedRefresh(
        row=row,
        collection=collection,
        candidate_id=candidate_id,
        candidate_binding=binding,
        candidate_binding_sha256=candidate_binding_sha256,
        old_scorecard_sha256=old_scorecard_sha256,
        cues=cues,
        chat_prompt_block=chat_block,
        chat_provenance=chat_provenance,
        input_provenance=input_provenance,
        attempt_fingerprint=attempt_fingerprint,
    )


def _failure_fingerprint(
    row: Mapping[str, object], reason_code: str, evidence: Mapping[str, object]
) -> str:
    return _canonical_sha256(
        {
            "schema_version": REFRESH_PROMPT_SCHEMA,
            "candidate_binding": _raw_candidate_binding(row),
            "reason_code": reason_code,
            "evidence": evidence,
            "provider_contract_sha256": PROVIDER_CONTRACT_SHA256,
            "semantic_chat_policy_sha256": SEMANTIC_CHAT_POLICY_SHA256,
        }
    )


def _semantic_provenance_is_current(scorecard: object, prepared: PreparedRefresh) -> bool:
    if not selection_scorecard_is_valid(scorecard) or not isinstance(scorecard, Mapping):
        return False
    provenance = scorecard.get("semantic_recall_chat_evidence")
    if not isinstance(provenance, Mapping) or not all(
        provenance.get(key) == value for key, value in prepared.chat_provenance.items()
    ):
        return False
    receipt = prepared.row.get(ROW_RECEIPT_KEY)
    if not isinstance(receipt, Mapping):
        # A newly recalled scorecard carries this provenance directly; only
        # this refresh lane's own cards require the additional receipt below.
        return True
    declared_receipt_sha256 = receipt.get("receipt_sha256")
    receipt_body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    evidence = receipt.get("input_provenance")
    return bool(
        receipt.get("schema_version") == REFRESH_RECEIPT_SCHEMA
        and receipt.get("candidate_id") == prepared.candidate_id
        and receipt.get("status") == "REFRESHED"
        and receipt.get("new_scorecard_sha256") == _canonical_sha256(scorecard)
        and receipt.get("provider_contract_sha256") == PROVIDER_CONTRACT_SHA256
        and declared_receipt_sha256 == _canonical_sha256(receipt_body)
        and isinstance(evidence, Mapping)
        and evidence.get("source_media") == prepared.input_provenance["source_media"]
        and evidence.get("bcut_srt") == prepared.input_provenance["bcut_srt"]
        and evidence.get("semantic_chat") == prepared.input_provenance["semantic_chat"]
        and evidence.get("candidate_binding_sha256") == prepared.candidate_binding_sha256
    )


def operator_scoped_chat_refresh_needed(
    date: str,
    state: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> bool:
    """Pure work-flag probe, including quota-full named backlog rows."""

    scope = _historical_v2_scope(date, state, now=now)
    if scope is None:
        return False
    rows, duplicates = _scoped_rows(state, scope)
    if duplicates:
        return True
    for collection, row in rows:
        try:
            prepared = _prepare(collection, row)
        except RefreshPreparationError:
            return True
        if not _semantic_provenance_is_current(row.get("selection_scorecard"), prepared):
            return True
    return False


def runner_date_work_flags(
    date: str,
    state: dict,
    *,
    automatic_maintenance: bool,
    talk_candidate_ids: Collection[str] | None = None,
) -> tuple[bool, bool, bool]:
    """Runner work probe that keeps quota-full named refreshes reachable."""

    if talk_candidate_ids is not None:
        allowed = set(talk_candidate_ids)

        def in_scope(row: object) -> bool:
            return isinstance(row, Mapping) and str(
                row.get("cid") or row.get("candidate_id") or ""
            ) in allowed

        has_pending = any(
            in_scope(row)
            for key in ("pending_talk", "talk_backlog")
            for row in (state.get(key) if isinstance(state.get(key), list) else [])
        ) or (bool(allowed) and operator_scoped_chat_refresh_needed(date, state))
        needs_cover = automatic_maintenance and any(
            in_scope(record) and _runner.cover_repair_needed(date, record)
            for record in state.get("picks", [])
        )
        return False, has_pending, needs_cover

    done = set(state.get("segments_done", []))
    dead = state.get("segments_dead", {})
    has_new = any(
        segment.stem not in done and segment.stem not in dead
        for segment in _runner.list_segments(date)
    )
    has_pending = bool(
        state.get("pending_talk")
        or state.get("pending_song")
        or _runner.backlog_has_eligible_session_work(state)
        or operator_scoped_chat_refresh_needed(date, state)
    )
    needs_cover = automatic_maintenance and any(
        _runner.cover_repair_needed(date, record)
        for record in state.get("picks", []) + state.get("songs", [])
    )
    return has_new, has_pending, needs_cover


def build_refresh_llm_call() -> Callable[[str], str]:
    from src.autoslice.llm_client import LlmConfig, build_llm_call

    return build_llm_call(
        LlmConfig(
            transport="command",
            command_template=(
                "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} "
                "'gpt-5.6-sol gpt-5.5 gpt-5.4' medium"
            ),
            timeout_seconds=600.0,
        )
    )


def _prompt(prepared: PreparedRefresh) -> str:
    transcript = "\n".join(
        f"#{position} [{cue.source_start_ms}-{cue.source_end_ms}] {' '.join(cue.text.split())}"
        for position, cue in enumerate(prepared.cues, start=1)
    )
    binding = prepared.candidate_binding
    return f"""你是李豆沙切片频道的选题评分员。这条候选已经存在；本次只因旧评分卡没有看到当前
hash-bound 弹幕互动证据而重评分。候选 ID、hook、边界都已冻结，不得重新选题、扩缩边界、
改写 hook 或输出任何候选替代项。若固定 hook 在固定字幕窗中没有支持，status 填 UNSUPPORTED。

candidate_id: {prepared.candidate_id}
固定边界: [{binding["start_ms"]}, {binding["end_ms"]}) ms
固定 hook: {binding["hook"]}

固定候选窗字幕（编号仅为本次评分证据编号）:
{transcript}

{prepared.chat_prompt_block}

{_SCORECARD_RUBRIC_BLOCK}
只输出键集合严格为 status/selection_scorecard 的一个 JSON 对象：
{{"status":"SUPPORTED"或"UNSUPPORTED","selection_scorecard":{{"tier":1或2或3,
"tier_basis":"上述枚举","tier_reason":"准入理由","tier_evidence_cues":[整数],
"dimensions":{{"lidousha_centrality":0到4整数,"stance_intensity":0到4整数,
"audience_salience":0到4整数,"relationship_interaction":0到4整数,
"persona_reversal":0到4整数,"comedic_payoff":0到4整数,"self_contained":0到4整数}},
"uncertainty_penalty":0到15,"fatigue_penalty":0到10}}}}
"""


def _input_still_matches(prepared: PreparedRefresh) -> bool:
    if _canonical_sha256(_raw_candidate_binding(prepared.row)) != prepared.candidate_binding_sha256:
        return False
    if _canonical_sha256(prepared.row.get("selection_scorecard")) != prepared.old_scorecard_sha256:
        return False
    provenance = prepared.input_provenance
    try:
        if (
            _regular_stat(Path(str(prepared.candidate_binding["segment_path"])))
            != provenance["source_media"]
        ):
            return False
        bcut_payload, bcut_binding = _read_stable_bytes(
            Path(str(prepared.candidate_binding["bcut_srt_path"]))
        )
        if bcut_binding != provenance["bcut_srt"] or not bcut_payload:
            return False
        xml_payload, xml_binding = _read_stable_bytes(
            Path(str(prepared.candidate_binding["xml_path"]))
        )
    except (OSError, KeyError, TypeError):
        return False
    semantic_chat = provenance.get("semantic_chat")
    return bool(
        isinstance(semantic_chat, Mapping)
        and xml_binding.get("sha256") == semantic_chat.get("source_sha256")
        and xml_payload
    )


def _attempts(row: Mapping[str, object]) -> list[dict[str, object]]:
    receipt = row.get(ROW_RECEIPT_KEY)
    raw = receipt.get("attempts") if isinstance(receipt, Mapping) else None
    return (
        [dict(item) for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []
    )


def _attempt_count(row: Mapping[str, object], fingerprint: str) -> int:
    return sum(1 for attempt in _attempts(row) if attempt.get("attempt_fingerprint") == fingerprint)


def _write_receipt(
    row: dict[str, object],
    *,
    date: str,
    scope: Scope,
    status: str,
    reason_code: str,
    attempt_fingerprint: str,
    evidence: Mapping[str, object],
    outcome: str,
    prompt_sha256: str | None = None,
    response_sha256: str | None = None,
    new_scorecard_sha256: str | None = None,
) -> None:
    attempts = _attempts(row)
    attempt_number = 1 + sum(
        1 for attempt in attempts if attempt.get("attempt_fingerprint") == attempt_fingerprint
    )
    attempt: dict[str, object] = {
        "attempted_at": _utc_now(),
        "attempt_fingerprint": attempt_fingerprint,
        "attempt_number": attempt_number,
        "outcome": outcome,
        "reason_code": reason_code,
    }
    if prompt_sha256:
        attempt["prompt_sha256"] = prompt_sha256
    if response_sha256:
        attempt["response_sha256"] = response_sha256
    attempts.append(attempt)
    receipt: dict[str, object] = {
        "schema_version": REFRESH_RECEIPT_SCHEMA,
        "candidate_id": _candidate_id(row),
        "recording_date": date,
        "scope_grant_id": scope.grant_id,
        "scope_grant_sha256": scope.grant_sha256,
        "status": status,
        "reason_code": reason_code,
        "attempt_fingerprint": attempt_fingerprint,
        "attempts": attempts[-MAX_ATTEMPT_HISTORY:],
        "old_scorecard_sha256": _canonical_sha256(row.get("selection_scorecard")),
        "provider_contract": dict(_PROVIDER_CONTRACT),
        "provider_contract_sha256": PROVIDER_CONTRACT_SHA256,
        "input_provenance": dict(evidence),
    }
    if new_scorecard_sha256:
        receipt["new_scorecard_sha256"] = new_scorecard_sha256
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    row[ROW_RECEIPT_KEY] = receipt


def _failure_status(reason_code: str, attempt_number: int) -> str:
    if reason_code in _TERMINAL_REASON_CODES:
        return "BLOCKED_TERMINAL"
    if attempt_number >= MAX_ATTEMPTS_PER_INPUT:
        return "BLOCKED_RETRY_EXHAUSTED"
    return "RETRY_WAIT"


def _record_failure(
    row: dict[str, object],
    *,
    date: str,
    scope: Scope,
    reason_code: str,
    fingerprint: str,
    evidence: Mapping[str, object],
    outcome: str,
    prompt_sha256: str | None = None,
    response_sha256: str | None = None,
) -> None:
    attempt_number = _attempt_count(row, fingerprint) + 1
    _write_receipt(
        row,
        date=date,
        scope=scope,
        status=_failure_status(reason_code, attempt_number),
        reason_code=reason_code,
        attempt_fingerprint=fingerprint,
        evidence=evidence,
        outcome=outcome,
        prompt_sha256=prompt_sha256,
        response_sha256=response_sha256,
    )


def _refresh_one(
    date: str,
    scope: Scope,
    prepared: PreparedRefresh,
    *,
    llm_call: Callable[[str], str],
) -> bool:
    row = prepared.row
    if _attempt_count(row, prepared.attempt_fingerprint) >= MAX_ATTEMPTS_PER_INPUT:
        return False
    prompt = _prompt(prepared)
    prompt_sha256 = _bytes_sha256(prompt.encode("utf-8"))
    try:
        raw = llm_call(prompt)
    except (LlmCallError, Exception):  # noqa: BLE001
        _record_failure(
            row,
            date=date,
            scope=scope,
            reason_code="REFRESH_PROVIDER_CALL_FAILED",
            fingerprint=prepared.attempt_fingerprint,
            evidence=prepared.input_provenance,
            outcome="PROVIDER_UNAVAILABLE",
            prompt_sha256=prompt_sha256,
        )
        return False
    response_sha256 = _bytes_sha256(str(raw).encode("utf-8"))
    if not _input_still_matches(prepared):
        _record_failure(
            row,
            date=date,
            scope=scope,
            reason_code="REFRESH_INPUT_BINDING_DRIFT",
            fingerprint=prepared.attempt_fingerprint,
            evidence=prepared.input_provenance,
            outcome="BINDING_DRIFT",
            prompt_sha256=prompt_sha256,
            response_sha256=response_sha256,
        )
        return False
    try:
        payload = extract_json_object(raw)
    except (LlmCallError, AttributeError, ValueError, TypeError):
        payload = {}
    if set(payload) != {"status", "selection_scorecard"} or payload.get("status") not in {
        "SUPPORTED",
        "UNSUPPORTED",
    }:
        _record_failure(
            row,
            date=date,
            scope=scope,
            reason_code="REFRESH_PROVIDER_RESPONSE_INVALID",
            fingerprint=prepared.attempt_fingerprint,
            evidence=prepared.input_provenance,
            outcome="INVALID_RESPONSE",
            prompt_sha256=prompt_sha256,
            response_sha256=response_sha256,
        )
        return False
    if payload["status"] == "UNSUPPORTED":
        _record_failure(
            row,
            date=date,
            scope=scope,
            reason_code="REFRESH_HOOK_UNSUPPORTED",
            fingerprint=prepared.attempt_fingerprint,
            evidence=prepared.input_provenance,
            outcome="UNSUPPORTED",
            prompt_sha256=prompt_sha256,
            response_sha256=response_sha256,
        )
        return False
    normalized = normalize_selection_scorecard(
        payload.get("selection_scorecard"),
        start_cue=1,
        end_cue=len(prepared.cues),
    )
    if normalized is None:
        _record_failure(
            row,
            date=date,
            scope=scope,
            reason_code="REFRESH_SCORECARD_INVALID",
            fingerprint=prepared.attempt_fingerprint,
            evidence=prepared.input_provenance,
            outcome="INVALID_SCORECARD",
            prompt_sha256=prompt_sha256,
            response_sha256=response_sha256,
        )
        return False
    try:
        calibrated = apply_reviewed_selection_calibration(prepared.candidate_id, normalized)
    except SelectionCalibrationPolicyError:
        calibrated = None
        reason_code = "REFRESH_CALIBRATION_REJECTED"
    else:
        reason_code = "REFRESH_SCORECARD_INVALID_AFTER_CALIBRATION"
    if not isinstance(calibrated, dict) or not selection_scorecard_is_valid(calibrated):
        _record_failure(
            row,
            date=date,
            scope=scope,
            reason_code=reason_code,
            fingerprint=prepared.attempt_fingerprint,
            evidence=prepared.input_provenance,
            outcome="INVALID_SCORECARD",
            prompt_sha256=prompt_sha256,
            response_sha256=response_sha256,
        )
        return False
    new_card = {
        **calibrated,
        "start_cue": 1,
        "end_cue": len(prepared.cues),
        "semantic_recall_chat_evidence": dict(prepared.chat_provenance),
    }
    if not selection_scorecard_is_valid(new_card):
        return False
    old_scorecard_sha256 = _canonical_sha256(row.get("selection_scorecard"))
    new_scorecard_sha256 = _canonical_sha256(new_card)
    row["selection_scorecard"] = new_card
    _write_receipt(
        row,
        date=date,
        scope=scope,
        status="REFRESHED",
        reason_code="REFRESHED_WITH_CURRENT_CHAT_EVIDENCE",
        attempt_fingerprint=prepared.attempt_fingerprint,
        evidence={
            **prepared.input_provenance,
            "candidate_binding": prepared.candidate_binding,
            "candidate_binding_sha256": prepared.candidate_binding_sha256,
            "prompt_sha256": prompt_sha256,
            "response_sha256": response_sha256,
            "old_scorecard_sha256": old_scorecard_sha256,
        },
        outcome="REFRESHED",
        prompt_sha256=prompt_sha256,
        response_sha256=response_sha256,
        new_scorecard_sha256=new_scorecard_sha256,
    )
    # _write_receipt observes the newly installed card; preserve the actual
    # pre-refresh identity separately instead of mislabelling it as old.
    receipt = row[ROW_RECEIPT_KEY]
    assert isinstance(receipt, dict)
    receipt["old_scorecard_sha256"] = old_scorecard_sha256
    receipt["receipt_sha256"] = _canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    return True


def _write_run_receipt(
    state: dict,
    *,
    date: str,
    scope: Scope,
    status: str,
    refreshed: Sequence[str],
    current: Sequence[str],
    blocked: Sequence[str],
    deferred: Sequence[str],
    duplicates: Sequence[str],
) -> None:
    receipt: dict[str, object] = {
        "schema_version": REFRESH_RUN_SCHEMA,
        "recording_date": date,
        "scope_grant_id": scope.grant_id,
        "scope_grant_sha256": scope.grant_sha256,
        "status": status,
        "refreshed_candidate_ids": list(refreshed),
        "current_candidate_ids": list(current),
        "blocked_candidate_ids": list(blocked),
        "deferred_candidate_ids": list(deferred),
        "duplicate_candidate_ids": list(duplicates),
        "missing_never_recalled_candidates_outside_scope": True,
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    state[REFRESH_STATE_KEY] = receipt


def refresh_operator_scoped_chat_scorecards(
    date: str,
    state: dict,
    *,
    llm_call: Callable[[str], str] | None = None,
    now: datetime | None = None,
) -> int:
    """Refresh stale named queued cards, or return ``-N`` to block production."""

    scope = _historical_v2_scope(date, state, now=now)
    if scope is None:
        return 0
    rows, duplicates = _scoped_rows(state, scope)
    if duplicates:
        _write_run_receipt(
            state,
            date=date,
            scope=scope,
            status="BLOCKED_DUPLICATE_CANDIDATE",
            refreshed=(),
            current=(),
            blocked=duplicates,
            deferred=(),
            duplicates=duplicates,
        )
        state["status"] = "semantic_chat_scorecard_refresh_blocked"
        return -len(duplicates)

    prepared_targets: list[PreparedRefresh] = []
    current_ids: list[str] = []
    blocked_ids: list[str] = []
    for collection, row in rows:
        try:
            prepared = _prepare(collection, row)
        except RefreshPreparationError as exc:
            fingerprint = _failure_fingerprint(row, exc.reason_code, exc.evidence)
            if _attempt_count(row, fingerprint) < MAX_ATTEMPTS_PER_INPUT:
                _record_failure(
                    row,
                    date=date,
                    scope=scope,
                    reason_code=exc.reason_code,
                    fingerprint=fingerprint,
                    evidence=exc.evidence,
                    outcome="INPUT_UNAVAILABLE",
                )
            blocked_ids.append(_candidate_id(row))
            continue
        if _semantic_provenance_is_current(row.get("selection_scorecard"), prepared):
            current_ids.append(prepared.candidate_id)
        else:
            prepared_targets.append(prepared)

    refreshed_ids: list[str] = []
    deferred_ids = [target.candidate_id for target in prepared_targets[MAX_CANDIDATES_PER_TICK:]]
    targets = prepared_targets[:MAX_CANDIDATES_PER_TICK]
    resolved_llm: Callable[[str], str] | None = llm_call
    if targets and resolved_llm is None:
        try:
            resolved_llm = build_refresh_llm_call()
        except Exception:  # noqa: BLE001
            resolved_llm = None
    for prepared in targets:
        if resolved_llm is None:
            _record_failure(
                prepared.row,
                date=date,
                scope=scope,
                reason_code="REFRESH_PROVIDER_UNAVAILABLE",
                fingerprint=prepared.attempt_fingerprint,
                evidence=prepared.input_provenance,
                outcome="PROVIDER_UNAVAILABLE",
            )
            blocked_ids.append(prepared.candidate_id)
            continue
        if _refresh_one(date, scope, prepared, llm_call=resolved_llm):
            refreshed_ids.append(prepared.candidate_id)
        else:
            blocked_ids.append(prepared.candidate_id)

    outstanding = sorted(set((*blocked_ids, *deferred_ids)))
    if outstanding:
        statuses = {
            str((row.get(ROW_RECEIPT_KEY) or {}).get("status") or "")
            for _collection, row in rows
            if _candidate_id(row) in outstanding and isinstance(row.get(ROW_RECEIPT_KEY), Mapping)
        }
        terminal = bool(statuses & {"BLOCKED_TERMINAL", "BLOCKED_RETRY_EXHAUSTED"})
        run_status = "BLOCKED" if terminal else "RETRY_WAIT"
        _write_run_receipt(
            state,
            date=date,
            scope=scope,
            status=run_status,
            refreshed=refreshed_ids,
            current=current_ids,
            blocked=blocked_ids,
            deferred=deferred_ids,
            duplicates=(),
        )
        state["status"] = (
            "semantic_chat_scorecard_refresh_blocked"
            if terminal
            else "paused_semantic_chat_scorecard_refresh"
        )
        return -len(outstanding)

    _write_run_receipt(
        state,
        date=date,
        scope=scope,
        status="COMPLETE",
        refreshed=refreshed_ids,
        current=current_ids,
        blocked=(),
        deferred=(),
        duplicates=(),
    )
    return len(refreshed_ids)
