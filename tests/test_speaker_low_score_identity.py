"""Low HOST similarity is missing support, not a negative identity authority.

All audio/contexts here are synthetic. The numeric 0.36781/1880ms condition
comes from the separately retained real HOST control; no CAM++ is rerun.
"""
from copy import deepcopy
import json

import pytest

from src.autoslice import speaker_attribution as sa
from src.autoslice.pronoun_consistency import discover_candidate_pronoun_findings


SRT = (
    "1\n00:00:00,000 --> 00:00:03,000\n她在说第一句\n\n"
    "2\n00:00:04,000 --> 00:00:07,000\n她在说第二句\n\n"
    "3\n00:00:08,000 --> 00:00:09,880\n她在说第三句\n\n"
    "4\n00:00:11,000 --> 00:00:14,000\n她在说第四句\n"
)
SCORES = (0.85, 0.82, 0.36781, 0.55)


def context_row(n, speaker):
    return {"n": n, "speaker": speaker, "address": "对话方" if speaker == "OTHER" else None,
            "confidence": 0.9, "reason": "Synthetic dialogue turn evidence, not voiceprint truth"}


def produce(*answers, score=0.36781, srt_text=SRT):
    prompts = []

    def context(prompt):
        prompts.append(prompt)
        return json.dumps({"labels": list(answers)}, ensure_ascii=False)

    scores = (SCORES[0], SCORES[1], score, SCORES[3])
    doc = sa.attribute_speakers(srt_text, scorer=lambda n: scores[n - 1], llm_call=context)
    return doc, prompts


@pytest.mark.parametrize("answer", ["HOST", "OTHER", "MEDIA", "UNKNOWN"])
def test_public_low_score_context_decision_roundtrips_without_negative_anchor(tmp_path, answer):
    doc, prompts = produce(context_row(3, answer))
    row = doc["cues"][2]
    assert row["voiceprint"]["score"] == 0.36781
    assert row["end_ms"] - row["start_ms"] == 1880
    assert row["voiceprint"]["anchor"] is None
    assert row["speaker"] == answer
    assert row["evidence"] == (sa.EVIDENCE_UNKNOWN if answer == "UNKNOWN" else sa.EVIDENCE_CONTEXT)
    assert "3. [待判]" in prompts[0]
    assert "非李豆沙·声纹锚点" not in prompts[0]
    assert "只能在 OTHER 与 MEDIA" not in prompts[0]
    assert "低分" in prompts[0] and "人审真值" in prompts[0]
    assert doc["policy"] == {"host_session_seed_min": 0.68, "guest_seed_max": 0.42,
                             "guest_min_duration_ms": 1800.0}
    assert [r["speaker"] for r in doc["cues"][:2]] == ["HOST", "HOST"]
    assert all(r["evidence"] == sa.EVIDENCE_VOICEPRINT for r in doc["cues"][:2])
    # Ordinary persistence/consumer, not a helper fed a hand-converted receipt.
    saved = tmp_path / "attribution.json"
    saved.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    loaded = json.loads(saved.read_text(encoding="utf-8"))
    text = sa.attribution_context_for_srt(loaded, SRT)
    assert "1. HOST（voiceprint_anchor）" in text
    if answer == "UNKNOWN":
        assert "3." not in text
    else:
        assert f"3. {answer}" in text and "context_expansion" in text
    assert "非主播" not in text
    assert not doc["display_authorized"] and not doc["release_authorized"]
    assert not doc["mutation_authorized"]


@pytest.mark.parametrize("score", [-0.12, 0.42, 0.55])
@pytest.mark.parametrize("end", ["09,799", "09,800", "10,900"])
def test_unseen_scores_and_durations_cannot_create_negative_identity(score, end):
    # Vary the boundary that previously generated the immutable identity.
    srt_text = SRT.replace("09,880", end)
    doc, _ = produce(context_row(3, "HOST"), score=score, srt_text=srt_text)
    assert doc["cues"][2]["voiceprint"]["anchor"] is None
    assert doc["cues"][2]["speaker"] == "HOST"
    assert "3. HOST（context_expansion）" in sa.attribution_context_for_srt(doc, srt_text)


@pytest.mark.parametrize("mode", ["absent", "empty", "unknown", "failure"])
def test_unresolved_low_score_is_unknown_without_an_independent_context_decision(mode):
    def fail(_):
        raise RuntimeError("synthetic unavailable context")
    context = {"absent": None, "empty": lambda _: '{"labels": []}',
               "unknown": lambda _: json.dumps({"labels": [context_row(3, "UNKNOWN")]}),
               "failure": fail}[mode]
    doc = sa.attribute_speakers(SRT, scorer=lambda n: SCORES[n - 1], llm_call=context)
    assert doc["cues"][2]["voiceprint"]["anchor"] is None
    assert doc["cues"][2]["speaker"] == "UNKNOWN"
    assert "3." not in sa.attribution_context_for_srt(doc, SRT)


@pytest.mark.parametrize("answer", ["HOST", "OTHER", "MEDIA", "UNKNOWN"])
def test_saved_legacy_non_host_authority_is_rejected_not_silently_relabelled(tmp_path, answer):
    doc, _ = produce(context_row(3, answer), context_row(4, "OTHER"))
    # Reproduce the old native receipt shape, including the UNKNOWN branch.
    # Its whole-clip context had received a false immutable premise, so merely
    # stripping the old tag cannot authenticate even the other expanded row.
    row = doc["cues"][2]
    row["voiceprint"]["anchor"] = "NON_HOST"
    saved = tmp_path / "old-attribution.json"
    saved.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    loaded = json.loads(saved.read_text(encoding="utf-8"))
    before = deepcopy(loaded)
    assert sa.attribution_context_for_srt(loaded, SRT) == ""
    assert loaded == before  # Preserve original evidence; no implicit rewrite.


def test_ordinary_pronoun_consumer_drops_legacy_negative_identity_document():
    doc, _ = produce(context_row(3, "OTHER"))
    doc["cues"][2]["voiceprint"]["anchor"] = "NON_HOST"
    prompts = []

    def context(prompt):
        prompts.append(prompt)
        return json.dumps({"decisions": [
            {"occurrence_id": f"cue-{i}-occurrence-1", "action": "KEEP_CURRENT",
             "current_token": "她", "replacement_token": "她", "referent": "UNSPECIFIED",
             "reason": "Synthetic neutral pronoun control"} for i in range(1, 5)]})

    _, audit = discover_candidate_pronoun_findings(
        SRT, policy_text="Synthetic policy", candidate_context_text="Synthetic context",
        llm_call=context, extract_json=json.loads, speaker_attribution=doc)
    assert "speaker_attribution_context_sha256" not in audit
    assert "voiceprint_anchor" not in prompts[0]
    assert "context_expansion" not in prompts[0]


def test_positive_host_only_old_shape_remains_consumable():
    doc = sa.attribute_speakers(SRT, scorer=lambda n: SCORES[n - 1])
    assert "1. HOST（voiceprint_anchor）" in sa.attribution_context_for_srt(doc, SRT)
    assert "2. HOST（voiceprint_anchor）" in sa.attribution_context_for_srt(doc, SRT)


@pytest.mark.parametrize("damage", ["srt", "window", "host_score", "duplicate_anchor",
                                    "fake_relative", "lowered_policy"])
def test_positive_and_binding_guards_still_reject_after_low_score_context_expansion(damage):
    doc, _ = produce(context_row(3, "HOST"))
    current_srt = SRT
    if damage == "srt":
        current_srt = SRT.replace("第一句", "另一句")
    elif damage == "window":
        doc["cues"][2]["start_ms"] += 1
    elif damage == "host_score":
        doc["cues"][0]["voiceprint"]["score"] = 0.67
    elif damage == "duplicate_anchor":
        doc["cues"][1] = deepcopy(doc["cues"][0])
    elif damage == "fake_relative":
        doc["cues"][0]["evidence"] = "voiceprint_relative_anchor"
    else:
        doc["policy"]["host_session_seed_min"] = 0.3
    assert sa.attribution_context_for_srt(doc, current_srt) == ""


def test_ordinary_pronoun_consumer_accepts_new_low_score_context_host_as_inference():
    doc, _ = produce(context_row(3, "HOST"))
    prompts = []

    def context(prompt):
        prompts.append(prompt)
        return json.dumps({"decisions": [
            {"occurrence_id": f"cue-{i}-occurrence-1", "action": "KEEP_CURRENT",
             "current_token": "她", "replacement_token": "她", "referent": "UNSPECIFIED",
             "reason": "Synthetic neutral pronoun control"} for i in range(1, 5)]})

    _, audit = discover_candidate_pronoun_findings(
        SRT, policy_text="Synthetic policy", candidate_context_text="Synthetic context",
        llm_call=context, extract_json=json.loads, speaker_attribution=doc)
    assert "speaker_attribution_context_sha256" in audit
    assert "3. HOST（context_expansion）" in prompts[0]
    assert "3. HOST（voiceprint_anchor）" not in prompts[0]
