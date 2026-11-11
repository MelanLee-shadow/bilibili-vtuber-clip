"""An immutable candidate-less CPA gate may collect audio, never grant an edit."""
from copy import deepcopy
import hashlib
import json

import pytest

from src.autoslice import semantic_audio_recovery
from src.autoslice.exact_final_witness_authority import (
    history_audio_recovery_from_adjudication,
)
from src.autoslice.final_review_auditor import (
    _candidate_blind_missing_proposal_scaffold,
    build_context_adjudication_request,
)


SRT = "1\n00:00:01,000 --> 00:00:03,000\n前面解释\n\n2\n00:00:03,000 --> 00:00:04,000\n哈哈名字\n\n3\n00:00:04,000 --> 00:00:06,000\n后面回应\n"


def case():
    finding = {"cue_index": 2, "suspect": "哈哈名字", "suggestion": None,
               "proposed_full_cue": None, "repair_class": "phonetic"}
    current = build_context_adjudication_request(
        SRT, _candidate_blind_missing_proposal_scaffold(SRT, finding),
        source_media_timeline_offset_ms=10490,
    )
    # Fixture for the original candidate-less request schema, before the
    # CURRENT-only scaffold was introduced.  Production receipts are immutable.
    original = {**current, "proposed_cue": ""}
    original.pop("request_sha256")
    original["request_sha256"] = hashlib.sha256(json.dumps(
        original, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    gate = {"schema_version": "subtitle-missing-proposal-cpa-convergence.v1",
            "status": "UNRESOLVED", "decision": "UNRESOLVED", "needs_audio": True,
            "decision_authority": "CPA_JUDGE", "mutation_authorized": False,
            "timing_immutable": True, "reason_code": "CPA_CONVERGENCE_UNRESOLVED",
            "reason": "起始笑声和词尾仍未厘清，需要完整话语原音", "cue_index": 2,
            "current_cue_sha256": hashlib.sha256("哈哈名字".encode()).hexdigest(),
            "final_srt_sha256": hashlib.sha256(SRT.encode()).hexdigest(),
            "prompt_sha256": "a" * 64, "completion_sha256": "b" * 64,
            "context_sha256": "c" * 64}
    finding["exact_release_adjudication"] = {
        "status": "UNCERTAIN", "repaired": False, "decision_authority": "CPA_JUDGE",
        "policy_branch": "JUDGE_REQUESTS_MORE_AUDIO", "request": original,
        "cpa_missing_proposal_convergence": gate,
    }
    return finding, original, current


def test_original_empty_proposal_routes_new_audio_without_rebinding_old_judge(monkeypatch):
    finding, original, current = case()
    before = deepcopy(finding)
    assert current["proposed_cue"] == "哈哈名字"
    envelope = history_audio_recovery_from_adjudication(
        finding["exact_release_adjudication"], request=original,
    )
    assert envelope["status"] == "BLOCK" and envelope["needs_audio"] is True
    observed = []
    def verifier(_):
        return None
    verifier.observe_range_context = lambda **kw: observed.append(kw) or [{}]
    monkeypatch.setattr(semantic_audio_recovery, "validate_range_acoustic_observation",
                        lambda *_: ({"evidence_scope": "RANGE_CONTEXT_ONLY",
                                     "mutation_authorized": False}, None))
    result, audit = semantic_audio_recovery.recover_semantic_audio_range(
        srt_text=SRT, finding=finding, check_request=current, entity_verifier=verifier,
        llm_call=lambda _: '{"decision":"OBSERVE","first_cue":1,"last_cue":3,"reason":"完整话语"}',
    )
    assert audit["status"] == "OBSERVED" and audit["mutation_authorized"] is False
    assert observed[0]["range_witness_request"]["cue_indexes"] == [1, 2, 3]
    assert "proposed_cue" not in observed[0]["range_witness_request"]
    assert finding == before
    assert result["exact_release_adjudication"] == before["exact_release_adjudication"]


@pytest.mark.parametrize("change", ["request_hash", "gate_srt", "gate_cue", "mutation", "offset", "current_text"])
def test_empty_proposal_does_not_excuse_drift_or_mutation(change):
    finding, original, current = case()
    gate = finding["exact_release_adjudication"]["cpa_missing_proposal_convergence"]
    if change == "request_hash":
        original["request_sha256"] = "d" * 64
    elif change == "gate_srt":
        gate["final_srt_sha256"] = "d" * 64
    elif change == "gate_cue":
        gate["cue_index"] = 3
    elif change == "mutation":
        gate["mutation_authorized"] = True
    elif change == "offset":
        current["source_media_timeline_offset_ms"] = 0
    else:
        current["current_cue"] = "其他话"
    _, audit = semantic_audio_recovery.recover_semantic_audio_range(
        srt_text=SRT, finding=finding, check_request=current, entity_verifier=None,
        llm_call=lambda _: pytest.fail("invalid gate must not spend a CPA call"),
    )
    assert audit["status"] == "BLOCK"
