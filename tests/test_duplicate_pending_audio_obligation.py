"""Duplicate discoveries must not erase a bound unresolved audio obligation."""
from copy import deepcopy
import json

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from tests.test_producer_text_pipeline_final_review import (
    _pending_successor_with_typed_audio_gate,
)


@pytest.mark.parametrize("duplicate_kind", ["no_new_evidence", "requestless", "wrong_window"])
@pytest.mark.parametrize("duplicate_first", [False, True])
def test_duplicate_preserves_valid_audio_gate_and_immutable_receipts(
    tmp_path, duplicate_kind, duplicate_first,
):
    authority, reviewed_srt, _, adjudication = _pending_successor_with_typed_audio_gate(tmp_path)
    valid = authority["final_review_audit"]["findings"][0]
    duplicate = deepcopy(valid)
    duplicate.update(proposed_full_cue=None, why="同句重复发现没有新证据")
    other = duplicate["exact_release_adjudication"]
    if duplicate_kind == "no_new_evidence":
        other["policy_branch"] = "JUDGE_NO_NEW_EVIDENCE"
        other["history_convergence_acoustic_witness"]["judge_prompt_sha256"] = "invalid"
    elif duplicate_kind == "requestless":
        other.pop("request")
    else:
        other["request"]["matched_start_ms"] += 1
    authority["final_review_audit"]["findings"] = (
        [duplicate, valid] if duplicate_first else [valid, duplicate]
    )
    before = json.dumps(authority, sort_keys=True)

    pending = pipeline._pending_current_exact_findings(reviewed_srt, authority)

    # The shared fixture also carries independent disclosed doubts. Only the
    # two discoveries of this bound cue must coalesce into one pending row.
    target = [row for row in pending if row["cue"] == valid["cue_index"]]
    assert len(target) == 1
    assert target[0]["exact_release_adjudication"] == adjudication
    assert target[0]["proposed_full_cue"] is None
    assert "mutation_authority" not in target[0]
    assert authority["final_review_audit"]["release_gate"] == "BLOCK"
    assert json.dumps(authority, sort_keys=True) == before
