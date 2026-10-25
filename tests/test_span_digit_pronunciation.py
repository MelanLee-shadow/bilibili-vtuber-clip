"""Single-digit readings enter the ordinary orthography prompt, not a new map."""

import json

import pytest

from src.autoslice import candidate_support as support
from src.autoslice import acoustic_witness_adjudication as acoustic
from src.autoslice.final_review_auditor import build_context_adjudication_request


@pytest.mark.parametrize("digit,word", list(zip("0123456789", "零一二三四五六七八九")))
def test_single_digit_uses_existing_topic_pronunciation(digit, word, monkeypatch):
    from src.autoslice import closed_set_proposal_rebuild as topic

    original = topic._pinyin
    used = []

    def tracked(text):
        used.append(text)
        return original(text)

    monkeypatch.setattr(topic, "_pinyin", tracked)
    assert support.orthography_pronunciation_key(
        "甲" + digit
    ) == support.orthography_pronunciation_key("甲" + word)
    assert digit in used


@pytest.mark.parametrize(
    "left,right",
    [("姐10", "姐一零"), ("A1", "A一"), ("1.2", "一点二"), ("12-3", "一二三"), ("凌云", "零")],
)
def test_number_identifiers_and_extra_syllables_are_not_homophones(left, right):
    assert not support.orthography_ambiguous(
        current_cue=left, proposed_cue=right, suspect=left, replacement=right
    )


def test_ordinary_text_first_judge_receives_digit_homophone_instruction(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    source = "1\n00:00:01,000 --> 00:00:04,000\n请写甲0吧\n"
    finding = {"cue_index": 1, "suspect": "甲0", "suggestion": "甲零", "repair_class": "phonetic"}
    request = build_context_adjudication_request(source, finding)
    witness = {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": "blind_pinyin",
        "status": "UNCERTAIN",
        "request_sha256": acoustic.build_witness_request(request)["request_sha256"],
        "reason_code": acoustic.TEXT_FIRST_REASON,
    }
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return json.dumps(
            {
                "choice": "PROPOSED",
                "needs_audio": False,
                "reason": "Synthetic contextual orthography.",
            }
        )

    result = acoustic.judge_word_choice(llm_call=model, check_request=request, witness=witness)
    assert len(prompts) == 1 and "同音字形的文字裁决" in prompts[0]
    assert result["choice"] == "PROPOSED"
