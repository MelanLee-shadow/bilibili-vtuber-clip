"""Scope regressions for the unaccepted CPA-conflict candidate; no live providers.

These test the existing decision seam, not downstream mutation permission. A
relaxation for ordinary nonempty text must not extend to malformed proposals or
replace the separate explicit-DROP contract.
"""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness
from tests.test_cpa_conflict_authority import _inputs, _judge


@pytest.mark.parametrize(
    "proposed", ["", " ", "\n\t", None, [], {}, False, 17, ["我们今天去看书"]],
    ids=["empty", "space", "control-whitespace", "null", "list", "object", "bool", "number", "nonempty-list"],
)
def test_ordinary_conflict_override_does_not_expand_to_invalid_text(proposed):
    request, witness = _inputs("wo men jin tian qu kan hai")
    request.update(
        proposed_cue=proposed, suspect=request["current_cue"], replacement=proposed,
        repair_class="phonetic",
    )
    before = deepcopy((request, witness))
    calls: list[str] = []
    apply, branch, audit = adjudicate_with_witness(
        check_request=request, witness=witness, llm_call=_judge("PROPOSED", calls),
    )
    assert len(calls) == 1
    assert audit["judge"]["choice"] == "PROPOSED"
    # Preserve the accepted baseline's refusal, not an implicit whole-cue DROP.
    assert apply is False
    assert branch == "WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT"
    assert audit["witness_conflict_gate"]["status"] == "BLOCK"
    assert "witness_conflict_diagnostic" not in audit
    assert (request, witness) == before


@pytest.mark.parametrize("repair_class", [None, "phonetic", "spoken_unit", "source_backed_entity"])
def test_nonempty_ordinary_text_remains_in_the_candidate_scope(repair_class):
    request, witness = _inputs("wo men jin tian qu kan hai")
    if repair_class is not None:
        request["repair_class"] = repair_class
    apply, branch, audit = adjudicate_with_witness(
        check_request=request, witness=witness, llm_call=_judge("PROPOSED", []),
    )
    assert apply is True
    assert branch == "CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT"
    assert audit["witness_conflict_diagnostic"]["effect"] == "DISCLOSURE_ONLY"
    assert "witness_conflict_gate" not in audit


@pytest.mark.parametrize("audible", [True, False])
def test_whole_cue_drop_still_requires_explicit_inaudible_decision(audible):
    request, witness = _inputs("wo men jin tian qu kan hai")
    request.update(proposed_cue="", suspect=request["current_cue"], replacement="",
                   repair_class="acoustic_drop_cue")
    witness["target_audible"] = audible
    before = deepcopy((request, witness))
    apply, branch, audit = adjudicate_with_witness(
        check_request=request, witness=witness,
        llm_call=lambda _: json.dumps({"choice": "DROP"}),
    )
    assert apply is (not audible)
    if audible:
        assert audit["judge"]["status"] == "JUDGE_OUT_OF_SET"
        assert "selected_action" not in audit
    else:
        assert branch == "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE"
        assert audit["selected_action"] == "DROP_CUE"
        assert audit["selected_target_cue"] == ""
    assert (request, witness) == before


def test_empty_proposed_is_not_a_substitute_for_inaudible_drop():
    request, witness = _inputs("wo men jin tian qu kan hai")
    request.update(proposed_cue="", suspect=request["current_cue"], replacement="",
                   repair_class="acoustic_drop_cue")
    witness["target_audible"] = False
    apply, branch, audit = adjudicate_with_witness(
        check_request=request, witness=witness,
        llm_call=lambda _: json.dumps({"choice": "PROPOSED"}),
    )
    assert apply is False
    assert branch == "INAUDIBLE_EMPTY_PROPOSED_REQUIRES_EXPLICIT_DROP"
    assert "selected_action" not in audit
