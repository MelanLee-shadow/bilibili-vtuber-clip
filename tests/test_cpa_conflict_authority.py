"""CPA selection and acoustic disagreement are distinct; no live provider calls."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness


def _inputs(heard: str) -> tuple[dict, dict]:
    request = {
        "schema_version": "subtitle-span-acoustic-check-request.v1",
        "request_sha256": "1" * 64,
        "cue_indexes": [1],
        "current_cue": "我们今天去看海",
        "proposed_cue": "我们今天去看书",
        "suspect": "海",
        "replacement": "书",
        "candidate_provenance": None,
        "context_before": "",
        "context_after": "",
        "matched_start_ms": 1000,
        "matched_end_ms": 3000,
        "whole_clip_current_srt": "1\n00:00:01,000 --> 00:00:03,000\n我们今天去看海\n",
    }
    witness = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "status": "OBSERVED",
        "witness_protocol": "blind_pinyin",
        "target_audible": True,
        "request_sha256": "2" * 64,
        "heard_pinyin": heard,
        "uncertain_positions": [],
        "provider": "synthetic-test",
        "model": "no-model-called",
        "confidence": 0.8,
    }
    return request, witness


def _judge(choice: str, calls: list[str]):
    def synthetic(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({
            "choice": choice,
            "needs_audio": False,
            "ranking": [{"choice": token, "p": 0.9 if token == choice else 0.05}
                        for token in ("PROPOSED", "CURRENT", "NEITHER")],
            "reason": "Synthetic decision control, not evidence of spoken text.",
        })
    return synthetic


@pytest.mark.parametrize("heard", ["jia yi bing ding", "wo men jin tian qu kan hai"])
@pytest.mark.parametrize("choice", ["PROPOSED", "CURRENT", "NEITHER"])
def test_explicit_cpa_decision_is_not_replaced_by_a_pinyin_vote(heard, choice):
    request, witness = _inputs(heard)
    before_request, before_witness = deepcopy(request), deepcopy(witness)
    calls: list[str] = []
    apply, branch, audit = adjudicate_with_witness(
        check_request=request, witness=witness, llm_call=_judge(choice, calls),
    )
    assert len(calls) == 1
    assert audit["judge"]["choice"] == choice
    assert apply is (choice == "PROPOSED")
    if choice == "PROPOSED":
        # Disagreement remains visible, but is not a second final-text authority.
        assert audit["witness_diagnostic_conflict"] is True
        assert branch == "CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT"
        diagnostic = audit["witness_conflict_diagnostic"]
        assert diagnostic["effect"] == "DISCLOSURE_ONLY"
        assert diagnostic["additional_support_found"] is False
        assert "witness_conflict_gate" not in audit
    else:
        assert branch == ("JUDGE_KEEPS_CURRENT" if choice == "CURRENT"
                          else "JUDGE_REJECTS_CLOSED_SET")
    assert request == before_request
    assert witness == before_witness


def test_matching_pinyin_keeps_the_normal_proposed_path():
    request, witness = _inputs("wo men jin tian qu kan shu")
    apply, branch, audit = adjudicate_with_witness(
        check_request=request, witness=witness, llm_call=_judge("PROPOSED", []),
    )
    assert apply is True
    assert branch == "WITNESS_JUDGE_APPLY_PROPOSED"
    assert audit["witness_diagnostic_conflict"] is False


@pytest.mark.parametrize("invalid", ["missing_schema", "legacy_sighted", "missing_judge"])
def test_no_cpa_or_invalid_witness_cannot_gain_mutation_permission(invalid):
    request, witness = _inputs("jia yi bing ding")
    calls: list[str] = []
    callback = _judge("PROPOSED", calls)
    if invalid == "missing_schema":
        witness.pop("schema_version")
    elif invalid == "legacy_sighted":
        witness["witness_protocol"] = "legacy_sighted"
    else:
        callback = None
    apply, _branch, _audit = adjudicate_with_witness(
        check_request=request, witness=witness, llm_call=callback,
    )
    assert apply is False
    assert calls == []


def test_invalid_judge_answer_does_not_become_proposed():
    request, witness = _inputs("jia yi bing ding")
    apply, _branch, audit = adjudicate_with_witness(
        check_request=request, witness=witness, llm_call=lambda _prompt: "not a verdict",
    )
    assert apply is False
    assert audit["judge"].get("choice") != "PROPOSED"


def test_acoustic_delete_retains_the_existing_audible_prefix_protection():
    request, witness = _inputs("wo men jin tian qu kan hai")
    request.update(proposed_cue="今天去看海", suspect="我们", replacement="",
                   repair_class="acoustic_delete")
    apply, branch, audit = adjudicate_with_witness(
        check_request=request, witness=witness, llm_call=_judge("PROPOSED", []),
    )
    assert apply is False
    assert branch == "WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT"
    assert audit["witness_conflict_gate"]["status"] == "BLOCK"
