from copy import deepcopy
import json

import pytest

from src.autoslice.discovery_evidence_presentation import (
    compact_source_context, expand_source_context,
)
from src.autoslice.prompt_materials import same_prompt_materials, prompt_material_sha256
from src.autoslice.review_errors import ContractRejection


def source_context():
    body = {
        "authority": "EVIDENCE_ONLY", "provider": "mai", "model": "fixture",
        "source_media_sha256": "d" * 64,
        "exact_transcript": "The original source transcript. " * 100,
        "native_segments": [{"start_ms": 100, "end_ms": 600, "text": "original"}],
        "current_cue_texts": [{"cue_index": 1, "text": "current"}],
        "observed_cue_texts": [{"cue_index": 1, "text": "observed"}],
    }
    return {"range_audio": [
        {**deepcopy(body), "witness_request_sha256": key * 64} for key in "abc"
    ], "screen": [{"frame_ms": 20, "texts": ["actual screen text"]}]}


def prompt(value):
    return "Validated source observations. JSON\n" + json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_shared_body_preserves_every_binding_order_and_material_identity():
    rows = source_context()
    before = deepcopy(rows)
    compact = compact_source_context(rows)
    assert len(prompt(compact)) < len(prompt(rows)) * 0.6
    assert expand_source_context(compact) == rows == before
    assert same_prompt_materials(prompt(rows), prompt(compact))
    assert prompt_material_sha256(prompt(rows)) == prompt_material_sha256(prompt(compact))
    assert [r["witness_request_sha256"] for r in compact["range_audio"]] == [k * 64 for k in "abc"]


@pytest.mark.parametrize("change", ["binding", "order", "multiplicity", "clock", "current", "screen"])
def test_real_material_changes_remain_distinct(change):
    rows = source_context()
    altered = deepcopy(rows)
    if change == "binding":
        altered["range_audio"][0]["witness_request_sha256"] = "e" * 64
    elif change == "order":
        altered["range_audio"].reverse()
    elif change == "multiplicity":
        altered["range_audio"].append(deepcopy(altered["range_audio"][0]))
    elif change == "clock":
        altered["range_audio"][0]["native_segments"][0]["start_ms"] += 1
    elif change == "current":
        altered["range_audio"][0]["current_cue_texts"][0]["text"] = "changed"
    else:
        altered["screen"][0]["texts"] = ["changed"]
    assert expand_source_context(compact_source_context(altered)) == altered
    assert not same_prompt_materials(prompt(rows), prompt(compact_source_context(altered)))


@pytest.mark.parametrize("change", ["body", "ref", "rule", "extra", "unused", "row_extra"])
def test_invalid_presentation_cannot_alias_a_valid_owner(change):
    compact = compact_source_context(source_context())
    key = next(iter(compact["shared_observations"]))
    if change == "body":
        compact["shared_observations"][key]["exact_transcript"] = "fabricated"
    elif change == "ref":
        compact["range_audio"][0]["shared_observation_ref"] = "missing"
    elif change == "rule":
        compact["representation_rule"] += " New authority."
    elif change == "extra":
        compact["ignored"] = "must not disappear"
    elif change == "unused":
        compact["range_audio"] = []
    else:
        compact["range_audio"][0]["text"] = "must not disappear"
    with pytest.raises(ContractRejection, match="DISCOVERY_SHARED_OBSERVATION_INVALID"):
        prompt_material_sha256(prompt(compact))


def test_unique_observations_keep_existing_representation():
    rows = source_context()
    rows["range_audio"] = rows["range_audio"][:1]
    assert compact_source_context(rows) is rows


def test_compact_rendering_cannot_reset_exhausted_text_budget(tmp_path, monkeypatch):
    from src.autoslice import cpa_request_lifecycle as life, llm_client as llm
    from src.autoslice.semantic_retry_progress import RetryNeedsPlan
    from tests.test_cpa_persistent_request import MODEL
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    monkeypatch.setattr(life, "cpa_text_retry_delay_seconds", lambda *args: 0)
    sent = []

    def fail(text):
        sent.append(text)
        raise llm.LlmCallError("response missing", safe_reason="LLM_COMMAND_TIMEOUT",
                               response_received=False)

    rows = source_context()
    with pytest.raises(llm.LlmCallError):
        life.persistent_text_call(fail, prompt(rows), model=MODEL, root=tmp_path)
    state_path, = tmp_path.rglob("state")
    original = state_path.read_bytes()
    with pytest.raises(RetryNeedsPlan, match="CPA_TEXT_RETRY_EXHAUSTED"):
        life.persistent_text_call(fail, prompt(compact_source_context(rows)), model=MODEL, root=tmp_path)
    assert len(sent) == 10 and len(list(tmp_path.rglob("state"))) == 1
    assert state_path.read_bytes() == original
