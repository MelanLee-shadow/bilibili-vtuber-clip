"""Keep closures need explicit full-utterance support, never a relative win."""

from copy import deepcopy

import pytest

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.final_review_auditor import build_context_adjudication_request
from src.autoslice.span_finding_obligations import _valid_closing_adjudication
from src.autoslice.span_phoneme_resolution import digest


CURRENT = "我先喝口水"


@pytest.fixture
def closing():
    request = build_context_adjudication_request(
        f"1\n00:00:01,000 --> 00:00:03,000\n{CURRENT}\n",
        {"cue_index": 1, "suspect": "口", "suggestion": "一口",
         "proposed_full_cue": "我先喝一口水", "repair_class": "phonetic"},
        require_complete_utterance_support=True,
    )
    witness_request = build_witness_request(request)
    return {
        "status": "OBSERVED", "repaired": False, "timing_immutable": True,
        "decision_authority": "CPA_JUDGE", "request": request,
        "witness_judge": {
            "witness_request_sha256": witness_request["request_sha256"],
            "judge": {
                "status": "JUDGED", "choice": "CURRENT",
                "check_request_sha256": request["request_sha256"],
                "current_utterance_supported": True,
                "current_utterance_support_reason": "The full current utterance is supported.",
            },
        },
        "verdict": {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": "blind_pinyin", "status": "OBSERVED",
            "request_sha256": witness_request["request_sha256"],
            "target_audible": True, "heard_pinyin": "wo xian he kou shui",
            "syllable_count": 5, "uncertain_positions": [], "confidence": 0.9,
            "provider": "fixture", "model": "fixture",
            "source_media_sha256": "a" * 64, "audio_clip_sha256": "b" * 64,
            "prompt_sha256": "c" * 64, "response_sha256": "d" * 64,
            "audio_start_ms": 1000, "audio_end_ms": 3000,
        },
    }


def closes(adjudication):
    return _valid_closing_adjudication(
        adjudication, before=CURRENT, after=CURRENT, window=[1000, 3000], keep=True,
    )


def rebind_request(adjudication):
    request = adjudication["request"]
    request["request_sha256"] = digest(
        {key: value for key, value in request.items() if key != "request_sha256"}
    )
    adjudication["witness_judge"]["judge"]["check_request_sha256"] = request["request_sha256"]


def test_keep_closes_with_complete_support_and_valid_observation(closing):
    before = deepcopy(closing)
    assert closes(closing) is True
    assert closing == before


@pytest.mark.parametrize("field,value", [
    ("current_utterance_supported", None), ("current_utterance_supported", False),
    ("current_utterance_support_reason", None), ("current_utterance_support_reason", " "),
    ("choice", "PROPOSED"), ("choice", "NEITHER"), ("status", "ERROR"),
    ("needs_audio_requested", True), ("needs_audio_request_unresolved", True),
    ("check_request_sha256", "0" * 64),
])
def test_keep_rejects_missing_or_unresolved_support(closing, field, value):
    judge = closing["witness_judge"]["judge"]
    if value is None:
        judge.pop(field)
    else:
        judge[field] = value
    assert closes(closing) is False


@pytest.mark.parametrize("field", [
    "require_complete_utterance_support", "context_start_ms", "context_end_ms",
    "matched_start_ms", "matched_end_ms", "source_media_timeline_offset_ms",
])
def test_keep_rejects_missing_request_fields_even_when_resigned(closing, field):
    closing["request"].pop(field)
    rebind_request(closing)
    assert closes(closing) is False


@pytest.mark.parametrize("path", [
    ("request",), ("witness_judge",), ("witness_judge", "judge"), ("verdict",),
])
@pytest.mark.parametrize("value", [None, [], "invalid", {}])
def test_keep_rejects_missing_or_malformed_containers_without_exception(closing, path, value):
    target = closing
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert closes(closing) is False


@pytest.mark.parametrize("path,value", [
    (("status",), "UNCERTAIN"), (("repaired",), True), (("timing_immutable",), False),
    (("decision_authority",), "WITNESS"),
    (("witness_judge", "witness_request_sha256"), "0" * 64),
    (("verdict", "response_sha256"), "invalid"),
    (("request", "current_cue"), "changed cue"),
    (("request", "extra"), object()),
])
def test_keep_preserves_existing_binding_and_observation_guards(closing, path, value):
    target = closing
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert closes(closing) is False


@pytest.mark.parametrize("adjudication", [None, {}, [], "invalid"])
def test_keep_rejects_missing_adjudication_without_exception(adjudication):
    assert closes(adjudication) is False
