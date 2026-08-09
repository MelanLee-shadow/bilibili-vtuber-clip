"""Recomputable acoustic authority for exact-final convergence mutations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.acoustic_witness_protocol import (
    BLIND_PINYIN_PROTOCOL,
    contains_han_text,
    supported_witness_protocol,
    witness_protocol,
)


BINDING_SCHEMA = "subtitle-repair-acoustic-witness-binding.v1"
GATE_SCHEMA = "history-convergence-acoustic-witness-authority.v1"
REQUIRED_REASON = "HISTORY_CONVERGENCE_ACOUSTIC_WITNESS_REQUIRED"
DOWNGRADE_BRANCH = "HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY"
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_PINYIN = re.compile(r"^(?:[a-zü]+|\?)$")
_WITNESS_TEXT_CHANNELS = frozenset(
    {
        "candidate_entities",
        "candidate_id",
        "canonical_entity",
        "current_cue",
        "proposed_cue",
        "matched_audio_text",
        "context_before",
        "context_after",
        "suspect",
        "replacement",
        "rewritten_text",
        "current_fit",
        "proposed_fit",
    }
)
_CONVERGENCE_APPLY_CONTRACTS = {
    "CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED": {
        "memo_key": "exact_final_cpa_convergence_memo",
        "memo_schema": "exact-final-cpa-convergence-memo.v1",
        "memo_status": "RESOLVED",
        "mutation_basis": "CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED",
        "witness_status": "HISTORY_AND_CURRENT_EVIDENCE_BOUND",
        "judge_schema": "exact-final-cpa-convergence-judge.v1",
        "pre_key": "pre_convergence_adjudication",
    },
    "CPA_EXACT_FINAL_CYCLE_CLOSED_SET_APPLY": {
        "memo_key": "exact_final_cpa_cycle_memo",
        "memo_schema": "exact-final-cpa-cycle-memo.v1",
        "memo_status": "LOCKED",
        "mutation_basis": "CPA_EXACT_FINAL_CYCLE_CLOSED_SET_ADJUDICATION",
        "witness_status": "CYCLE_HISTORY_AND_EVIDENCE_BOUND",
        "judge_schema": "exact-final-cpa-cycle-judge.v1",
        "pre_key": "pre_cycle_adjudications",
    },
}
_CONVERGENCE_MUTATION_BASES = frozenset(
    contract["mutation_basis"]
    for contract in _CONVERGENCE_APPLY_CONTRACTS.values()
)
_CONVERGENCE_WITNESS_STATUSES = frozenset(
    contract["witness_status"]
    for contract in _CONVERGENCE_APPLY_CONTRACTS.values()
)
_CONVERGENCE_JUDGE_SCHEMAS = frozenset(
    contract["judge_schema"]
    for contract in _CONVERGENCE_APPLY_CONTRACTS.values()
)


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _digest(value: object) -> str:
    return str(value or "").removeprefix("sha256:")


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _valid_check_request(
    request: object,
    *,
    current: str,
    proposed: str,
    window: tuple[int, int],
) -> bool:
    if not isinstance(request, Mapping):
        return False
    payload = dict(request)
    stored_sha = _digest(payload.pop("request_sha256", None))
    return bool(
        request.get("schema_version")
        == "subtitle-span-acoustic-check-request.v1"
        and len(stored_sha) == 64
        and stored_sha == _sha_json(payload)
        and request.get("current_cue") == current
        and request.get("proposed_cue") == proposed
        and request.get("base_text_sha256") == _sha_text(current)
        and request.get("matched_start_ms") == window[0]
        and request.get("matched_end_ms") == window[1]
    )


def _valid_blind_witness(
    witness: Mapping[str, Any],
    *,
    request_sha256: str,
) -> bool:
    heard = str(witness.get("heard_pinyin") or "").strip()
    tokens = heard.split()
    uncertain = witness.get("uncertain_positions")
    confidence = witness.get("confidence")
    syllable_count = witness.get("syllable_count")
    return bool(
        witness.get("schema_version")
        == "subtitle-span-acoustic-witness.v1"
        and witness.get("witness_protocol") == BLIND_PINYIN_PROTOCOL
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is True
        and witness.get("request_sha256") == request_sha256
        and bool(tokens)
        and all(_PINYIN.fullmatch(token) for token in tokens)
        and not any(key in witness for key in _WITNESS_TEXT_CHANNELS)
        and not contains_han_text(json.dumps(dict(witness), ensure_ascii=False))
        and isinstance(uncertain, list)
        and all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value < len(tokens)
            for value in uncertain
        )
        and isinstance(syllable_count, int)
        and not isinstance(syllable_count, bool)
        and syllable_count == len(tokens)
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0.0 <= float(confidence) <= 1.0
        and _valid_digest(witness.get("source_media_sha256"))
        and _valid_digest(witness.get("audio_clip_sha256"))
        and _valid_digest(witness.get("prompt_sha256"))
        and _valid_digest(witness.get("response_sha256"))
    )


def _valid_proposed_judge(
    judge: Mapping[str, Any],
    *,
    check_request_sha256: str,
) -> bool:
    similarities = judge.get("candidate_pinyin_similarity")
    choice_set = judge.get("choice_set")
    return bool(
        judge.get("schema_version") == "acoustic-witness-adjudication.v1"
        and judge.get("status") == "JUDGED"
        and judge.get("choice") == "PROPOSED"
        and judge.get("decision_contract") == "current-proposed-neither.v1"
        and isinstance(choice_set, list)
        and all(isinstance(value, str) for value in choice_set)
        and set(choice_set) == {"CURRENT", "PROPOSED", "NEITHER"}
        and _digest(judge.get("check_request_sha256"))
        == check_request_sha256
        and isinstance(judge.get("reason"), str)
        and bool(str(judge.get("reason") or "").strip())
        and isinstance(similarities, Mapping)
        and set(similarities) == {"current", "proposed"}
        and all(
            value is None
            or (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0.0 <= float(value) <= 1.0
            )
            for value in (
                similarities.get("current"),
                similarities.get("proposed"),
            )
        )
        and _valid_digest(judge.get("prompt_sha256"))
        and _valid_digest(judge.get("completion_sha256"))
    )


def build_acoustic_witness_binding(
    *,
    check_request: Mapping[str, Any],
    witness: Mapping[str, Any],
    judge: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Freeze all payloads needed to independently revalidate one witness."""

    current = check_request.get("current_cue")
    proposed = check_request.get("proposed_cue")
    start = check_request.get("matched_start_ms")
    end = check_request.get("matched_end_ms")
    if not (
        isinstance(current, str)
        and bool(current)
        and isinstance(proposed, str)
        and bool(proposed.strip())
        and isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end
        and _valid_check_request(
            check_request,
            current=current,
            proposed=proposed,
            window=(start, end),
        )
    ):
        return None
    try:
        witness_request = build_witness_request(check_request)
    except (KeyError, TypeError, ValueError):
        return None
    protocol = witness_protocol(witness)
    check_request_sha256 = _digest(check_request.get("request_sha256"))
    if not (
        supported_witness_protocol(witness)
        and protocol == BLIND_PINYIN_PROTOCOL
        and _valid_blind_witness(
            witness,
            request_sha256=str(witness_request.get("request_sha256") or ""),
        )
        and _valid_proposed_judge(
            judge,
            check_request_sha256=check_request_sha256,
        )
    ):
        return None
    binding: dict[str, Any] = {
        "schema_version": BINDING_SCHEMA,
        "status": "PASS",
        "witness_protocol": protocol,
        "proposed_text_sha256": "sha256:" + _sha_text(proposed),
        "matched_start_ms": start,
        "matched_end_ms": end,
        "check_request": dict(check_request),
        "witness_request": witness_request,
        "witness": dict(witness),
        "judge": dict(judge),
    }
    binding["binding_sha256"] = "sha256:" + _sha_json(binding)
    return binding


def binding_from_adjudication(
    adjudication: object,
) -> dict[str, Any] | None:
    if not isinstance(adjudication, Mapping):
        return None
    request = adjudication.get("request")
    witness = adjudication.get("verdict")
    witness_judge = adjudication.get("witness_judge")
    judge = (
        witness_judge.get("judge")
        if isinstance(witness_judge, Mapping)
        else None
    )
    if not all(isinstance(row, Mapping) for row in (request, witness, judge)):
        return None
    return build_acoustic_witness_binding(
        check_request=request,
        witness=witness,
        judge=judge,
    )


def valid_acoustic_witness_binding(
    binding: object,
    *,
    proposed: str,
    window: tuple[int, int],
) -> bool:
    if not isinstance(binding, Mapping):
        return False
    payload = dict(binding)
    binding_sha = payload.pop("binding_sha256", None)
    check_request = binding.get("check_request")
    witness = binding.get("witness")
    judge = binding.get("judge")
    if not all(
        isinstance(row, Mapping)
        for row in (check_request, witness, judge)
    ):
        return False
    rebuilt = build_acoustic_witness_binding(
        check_request=check_request,
        witness=witness,
        judge=judge,
    )
    return bool(
        binding.get("schema_version") == BINDING_SCHEMA
        and binding.get("status") == "PASS"
        and binding.get("proposed_text_sha256")
        == "sha256:" + _sha_text(proposed)
        and binding.get("matched_start_ms") == window[0]
        and binding.get("matched_end_ms") == window[1]
        and binding_sha == "sha256:" + _sha_json(payload)
        and rebuilt == dict(binding)
    )


def _pass_gate(
    *,
    source: str,
    binding: Mapping[str, Any],
    proposed: str,
    window: tuple[int, int],
) -> dict[str, Any]:
    gate: dict[str, Any] = {
        "schema_version": GATE_SCHEMA,
        "status": "PASS",
        "source": source,
        "witness_protocol": binding.get("witness_protocol"),
        "proposed_text_sha256": "sha256:" + _sha_text(proposed),
        "matched_start_ms": window[0],
        "matched_end_ms": window[1],
        "acoustic_witness_binding": dict(binding),
    }
    gate["gate_sha256"] = "sha256:" + _sha_json(gate)
    return gate


def convergence_witness_gate(
    *,
    proposed: str,
    window: tuple[int, int],
    findings: Iterable[Mapping[str, Any]],
    history: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Prefer a fresh blind observation, then an exact historical binding."""

    for finding in findings:
        adjudication = finding.get("exact_release_adjudication")
        binding = binding_from_adjudication(adjudication)
        if (
            binding is not None
            and binding.get("witness_protocol") == BLIND_PINYIN_PROTOCOL
            and valid_acoustic_witness_binding(
                binding,
                proposed=proposed,
                window=window,
            )
        ):
            return _pass_gate(
                source="FRESH_OBSERVED",
                binding=binding,
                proposed=proposed,
                window=window,
            )
    for repair in reversed(list(history)):
        binding = repair.get("acoustic_witness_binding")
        if (
            repair.get("after") == proposed
            and valid_acoustic_witness_binding(
                binding,
                proposed=proposed,
                window=window,
            )
        ):
            assert isinstance(binding, Mapping)
            return _pass_gate(
                source="HISTORY_OBSERVED_REUSE",
                binding=binding,
                proposed=proposed,
                window=window,
            )
    return {
        "schema_version": GATE_SCHEMA,
        "status": "BLOCK",
        "reason_code": REQUIRED_REASON,
        "proposed_text_sha256": "sha256:" + _sha_text(proposed),
        "matched_start_ms": window[0],
        "matched_end_ms": window[1],
    }


def valid_convergence_witness_gate(
    gate: object,
    *,
    proposed: str,
    window: tuple[int, int],
) -> bool:
    if not isinstance(gate, Mapping):
        return False
    payload = dict(gate)
    gate_sha = payload.pop("gate_sha256", None)
    binding = gate.get("acoustic_witness_binding")
    source = gate.get("source")
    return bool(
        gate.get("schema_version") == GATE_SCHEMA
        and gate.get("status") == "PASS"
        and isinstance(source, str)
        and source in {"FRESH_OBSERVED", "HISTORY_OBSERVED_REUSE"}
        and gate.get("proposed_text_sha256")
        == "sha256:" + _sha_text(proposed)
        and gate.get("matched_start_ms") == window[0]
        and gate.get("matched_end_ms") == window[1]
        and gate_sha == "sha256:" + _sha_json(payload)
        and valid_acoustic_witness_binding(
            binding,
            proposed=proposed,
            window=window,
        )
    )


def valid_convergence_mutation_authority(
    adjudication: Mapping[str, Any],
    *,
    proposed: str,
    window: tuple[int, int],
) -> bool:
    """Reject convergence provenance relabelled as an ordinary mutation."""

    raw_policy = adjudication.get("policy_branch")
    policy = raw_policy if isinstance(raw_policy, str) else None
    mutation = adjudication.get("mutation_authority")
    witness_judge = adjudication.get("witness_judge")
    judge = (
        witness_judge.get("judge")
        if isinstance(witness_judge, Mapping)
        else None
    )
    raw_basis = mutation.get("basis") if isinstance(mutation, Mapping) else None
    basis = raw_basis if isinstance(raw_basis, str) else None
    raw_witness_status = (
        witness_judge.get("witness_status")
        if isinstance(witness_judge, Mapping)
        else None
    )
    witness_status = (
        raw_witness_status if isinstance(raw_witness_status, str) else None
    )
    raw_judge_schema = judge.get("schema_version") if isinstance(judge, Mapping) else None
    judge_schema = raw_judge_schema if isinstance(raw_judge_schema, str) else None
    structural_marker = bool(
        any(
            key in adjudication
            for key in (
                "exact_final_cpa_convergence_memo",
                "exact_final_cpa_cycle_memo",
                "history_convergence_acoustic_witness",
                "pre_convergence_adjudication",
                "pre_cycle_adjudications",
            )
        )
        or policy in _CONVERGENCE_APPLY_CONTRACTS
        or basis in _CONVERGENCE_MUTATION_BASES
        or witness_status in _CONVERGENCE_WITNESS_STATUSES
        or judge_schema in _CONVERGENCE_JUDGE_SCHEMAS
    )
    if not structural_marker:
        return True
    if proposed == "":
        memo = adjudication.get("exact_final_cpa_cycle_memo")
        return bool(
            isinstance(memo, Mapping)
            and memo.get("schema_version") == "exact-final-cpa-cycle-memo.v1"
            and memo.get("status") == "LOCKED"
            and memo.get("choice_id") == "DROP"
            and memo.get("final_text") == ""
            and memo.get("final_text_sha256") == "sha256:" + _sha_text("")
            and memo.get("matched_start_ms") == window[0]
            and memo.get("matched_end_ms") == window[1]
            and "exact_final_cpa_convergence_memo" not in adjudication
            and "history_convergence_acoustic_witness" not in adjudication
            and policy == "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE"
            and basis == "CPA_EXPLICIT_INAUDIBLE_DROP"
            and witness_status == "OBSERVED"
            and judge_schema == "acoustic-witness-adjudication.v1"
            and isinstance(judge, Mapping)
            and judge.get("status") == "JUDGED"
            and judge.get("choice") == "DROP"
        )
    contract = _CONVERGENCE_APPLY_CONTRACTS.get(policy or "")
    if contract is None:
        return False
    memo = adjudication.get(contract["memo_key"])
    other_memo_key = (
        "exact_final_cpa_cycle_memo"
        if contract["memo_key"] == "exact_final_cpa_convergence_memo"
        else "exact_final_cpa_convergence_memo"
    )
    if not (
        isinstance(memo, Mapping)
        and memo.get("schema_version") == contract["memo_schema"]
        and memo.get("status") == contract["memo_status"]
        and memo.get("final_text") == proposed
        and memo.get("final_text_sha256") == "sha256:" + _sha_text(proposed)
        and memo.get("matched_start_ms") == window[0]
        and memo.get("matched_end_ms") == window[1]
        and other_memo_key not in adjudication
        and contract["pre_key"] in adjudication
        and isinstance(mutation, Mapping)
        and mutation.get("basis") == contract["mutation_basis"]
        and witness_status == contract["witness_status"]
        and isinstance(judge, Mapping)
        and judge.get("schema_version") == contract["judge_schema"]
        and judge.get("status") == "JUDGED"
        and judge.get("choice") == "PROPOSED"
    ):
        return False
    top_gate = adjudication.get("history_convergence_acoustic_witness")
    memo_gate = memo.get("acoustic_witness_gate")
    return bool(
        top_gate == memo_gate
        and valid_convergence_witness_gate(
            top_gate,
            proposed=proposed,
            window=window,
        )
    )


def downgrade_convergence_finding(
    finding: Mapping[str, Any],
    *,
    gate: Mapping[str, Any],
) -> dict[str, Any]:
    row = dict(finding)
    row["repair_class"] = "disclosure_only"
    row["why"] = (
        "[历史收敛缺声学见证，降为 disclosure_only] "
        + str(row.get("why") or "")
    )[:240]
    adjudication = row.get("exact_release_adjudication")
    final = dict(adjudication) if isinstance(adjudication, Mapping) else {}
    final.update(
        status="UNCERTAIN",
        repaired=False,
        reason_code=REQUIRED_REASON,
        policy_branch=DOWNGRADE_BRANCH,
        decision_authority="CPA_PROPOSAL_ONLY",
        witness_authority="ACOUSTIC_WITNESS_REQUIRED",
        history_convergence_acoustic_witness=dict(gate),
        mutation_authority={
            "schema_version": "subtitle-correction-mutation-authority.v1",
            "status": "NOT_APPLIED",
            "basis": REQUIRED_REASON,
        },
    )
    row["exact_release_adjudication"] = final
    return row


def convergence_gate_from_adjudication(
    adjudication: Mapping[str, Any],
) -> object:
    gate = adjudication.get("history_convergence_acoustic_witness")
    if gate is not None:
        return gate
    for memo_key in (
        "exact_final_cpa_convergence_memo",
        "exact_final_cpa_cycle_memo",
    ):
        memo = adjudication.get(memo_key)
        if isinstance(memo, Mapping) and memo.get("acoustic_witness_gate") is not None:
            return memo.get("acoustic_witness_gate")
    return None


def build_self_heal_repair_receipt(
    *,
    finding: Mapping[str, Any],
    cue_index: int,
    current_text: str,
    proposed: str,
    matched_start_ms: int,
    matched_end_ms: int,
    adjudication: Mapping[str, Any],
    judge: Mapping[str, Any],
    mutation: Mapping[str, Any],
    request_sha256: str,
    is_drop: bool,
    inaudible_override_valid: bool,
) -> dict[str, object]:
    witness = adjudication.get("verdict")
    witness_judge = adjudication.get("witness_judge")
    acoustic = (
        {
            key: witness.get(key)
            for key in (
                "schema_version",
                "status",
                "request_sha256",
                "witness_protocol",
                "target_audible",
                "heard_pinyin",
                "syllable_count",
                "confidence",
                "source_media_sha256",
                "audio_clip_sha256",
            )
            if key in witness
        }
        if isinstance(witness, Mapping)
        else None
    )
    gate = convergence_gate_from_adjudication(adjudication)
    similarities = (
        witness_judge.get("candidate_pinyin_similarity")
        if isinstance(witness_judge, Mapping)
        else None
    )
    if not isinstance(similarities, Mapping):
        similarities = judge.get("candidate_pinyin_similarity")
    binding = binding_from_adjudication(adjudication)
    if binding is None and isinstance(gate, Mapping):
        bound = gate.get("acoustic_witness_binding")
        binding = dict(bound) if isinstance(bound, Mapping) else None
    return {
        "schema_version": "exact-final-cpa-self-heal.v1",
        "cue_index": cue_index,
        "matched_start_ms": matched_start_ms,
        "matched_end_ms": matched_end_ms,
        "before": current_text,
        "after": proposed,
        "before_sha256": "sha256:" + _sha_text(current_text),
        "after_sha256": "sha256:" + _sha_text(proposed),
        "finding_sha256": "sha256:" + _sha_json(finding),
        "request_sha256": "sha256:" + request_sha256,
        "decision_authority": "CPA_JUDGE",
        "action": "DROP_CUE" if is_drop else "REPLACE_CUE_TEXT",
        "repair_class": (
            adjudication.get("request", {}).get("repair_class")
            if isinstance(adjudication.get("request"), Mapping)
            else None
        ),
        "policy_branch": adjudication.get("policy_branch"),
        "acoustic_witness": acoustic,
        "candidate_pinyin_similarity": (
            dict(similarities) if isinstance(similarities, Mapping) else None
        ),
        "acoustic_witness_binding": binding,
        "history_convergence_acoustic_witness": gate,
        "judge": dict(judge),
        "drop_authority": (
            dict(adjudication["drop_authority"])
            if is_drop
            and isinstance(adjudication.get("drop_authority"), Mapping)
            else None
        ),
        "inaudible_witness_override": (
            dict(witness_judge["inaudible_witness_override"])
            if inaudible_override_valid
            and isinstance(witness_judge, Mapping)
            and isinstance(
                witness_judge.get("inaudible_witness_override"),
                Mapping,
            )
            else None
        ),
        "mutation_authority": dict(mutation),
        "timing_immutable": True,
    }
