"""A cached word decision must not consume a pending semantic audio request."""
from copy import deepcopy
import json

import pytest

from src.autoslice.final_review_auditor import adjudicate_context_finding
from src.autoslice.final_review_provider_budget import ContextAdjudicationBudget
from tests.test_semantic_audio_recovery import SOURCE, missing_proposal_case


def _budget(verifier, adjudicate, *, allowed=True):
    return ContextAdjudicationBudget(
        limit=12, initial_provider_adjudication_count=81,
        provider_calls_allowed=allowed, entity_verifier=verifier,
        clip_context=None, source_media_timeline_offset_ms=30000,
        judge_llm_call=lambda _: json.dumps({
            "decision": "OBSERVE", "first_cue": 1, "last_cue": 3,
            "reason": "完整话语补听",
        }),
        screen_read_probe=None, adjudicate_context=adjudicate,
    )


@pytest.mark.parametrize("allowed", [True, False])
def test_pending_audio_uses_live_normal_range_or_stays_offline(allowed):
    finding, request = missing_proposal_case()
    original = deepcopy(finding)
    seen = []
    cache_reads = []

    def verifier(_):
        pytest.fail("failed range must stop before an unchanged exact witness")

    def observe(**kwargs):
        seen.append(kwargs)
        return [{"status": "UNCERTAIN", "reason_code": "EXTERNAL_AUDIO_FAILURE"}]

    verifier.observe_range_context = observe
    verifier.probe_witness_cache = lambda _: cache_reads.append(True) or {
        "served_from_cache": True,
    }

    def adjudicate(srt, row, **kwargs):
        if not callable(getattr(kwargs["entity_verifier"], "observe_range_context", None)):
            # Model the old exact witness + word-judge cache hit, whose
            # unresolved decision still requests a different semantic range.
            witness = kwargs["entity_verifier"](request)
            return srt, {
                **row["exact_release_adjudication"], "verdict": witness,
                "request": request,
                "witness_judge": {
                    "witness_request_sha256": witness.get("request_sha256"),
                    "judge": {"status": "JUDGED", "served_from_cache": True,
                              "check_request_sha256": request["request_sha256"]},
                },
            }
        return adjudicate_context_finding(srt, row, **kwargs)

    budget = _budget(verifier, adjudicate, allowed=allowed)
    first = budget.adjudicate(SOURCE, finding)
    repeated = budget.adjudicate(SOURCE, finding)

    assert cache_reads == []
    assert finding == original
    assert first["repaired"] is False
    assert repeated["same_input_replay"]["provider_call_count"] == 0
    if allowed:
        assert len(seen) == 1
        assert seen[0]["range_witness_request"]["cue_indexes"] == [1, 2, 3]
        assert first["semantic_audio_recovery"]["status"] == "BLOCK"
        assert budget.provider_adjudication_count == 82
    else:
        assert seen == []
        assert first["status"] == "SKIPPED_PROVIDER_DISABLED"
        assert budget.provider_adjudication_count == 81


def test_new_valid_audio_obligation_changes_identity_without_counter_reset():
    finding, _ = missing_proposal_case()
    untyped = {k: v for k, v in finding.items() if k != "exact_release_adjudication"}
    calls = []
    budget = _budget(None, lambda srt, row, **_: (
        calls.append(deepcopy(row)) or srt,
        {"status": "UNCERTAIN", "repaired": False},
    ))
    budget.adjudicate(SOURCE, untyped)
    budget.adjudicate(SOURCE, finding)
    budget.adjudicate(SOURCE, deepcopy(finding))
    assert len(calls) == 2
    assert budget.provider_adjudication_count == 83


def test_invalid_audio_gate_does_not_change_same_input_identity():
    finding, _ = missing_proposal_case()
    finding["exact_release_adjudication"]["cpa_missing_proposal_convergence"]["prompt_sha256"] = "invalid"
    untyped = {k: v for k, v in finding.items() if k != "exact_release_adjudication"}
    calls = []
    budget = _budget(None, lambda srt, row, **_: (
        calls.append(row) or srt, {"status": "UNCERTAIN", "repaired": False},
    ))
    budget.adjudicate(SOURCE, untyped)
    budget.adjudicate(SOURCE, finding)
    assert len(calls) == 1
    assert budget.provider_adjudication_count == 82
