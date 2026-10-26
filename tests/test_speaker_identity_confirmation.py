"""Identity controls: score separation is not a named-speaker identification."""
from copy import deepcopy
import json

import pytest

from src.autoslice import speaker_attribution as sa
from src.autoslice.pronoun_consistency import discover_candidate_pronoun_findings

SRT = "\n\n".join(
    f"{i}\n00:00:{(i - 1) * 4:02},000 --> 00:00:{(i - 1) * 4 + 3:02},000\n她在说第{i}句"
    for i in range(1, 5)
) + "\n"


def label(n, speaker):
    return {"n": n, "speaker": speaker, "address": "另一方" if speaker == "OTHER" else None,
            "confidence": 0.95, "reason": "Synthetic dialogue turn evidence"}


def attributed(scores, replies=()):
    calls = []
    def context(prompt):
        calls.append(prompt)
        return json.dumps({"labels": list(replies)}, ensure_ascii=False)
    doc = sa.attribute_speakers(SRT, scorer=lambda index: scores[index - 1], llm_call=context)
    return doc, calls


@pytest.mark.parametrize("scores", [
    [0.05, 0.08, 0.35, 0.38],  # Root's all-low-score relative-HOST counterexample.
    [0.42, 0.43, 0.66, 0.67],  # Separated but neither group identifies HOST.
])
def test_unconfirmed_two_groups_remain_anonymous_unknown(scores):
    doc, calls = attributed(scores, [label(i, "HOST") for i in range(1, 5)])
    assert [row["speaker"] for row in doc["cues"]] == ["UNKNOWN"] * 4
    assert calls == []
    partition = doc["score_partition"]
    assert partition["status"] == "CANDIDATES_ONLY"
    assert partition["identity_authorized"] is False
    assert {g["group_id"] for g in partition["groups"]} == {"LOW", "HIGH"}
    assert all(g["identity"] == "UNKNOWN" for g in partition["groups"])
    assert not doc["release_authorized"] and not doc["mutation_authorized"]
    assert sa.attribution_context_for_srt(doc, SRT) == ""


@pytest.mark.parametrize("score", [0.1, 0.55, 0.9])
def test_single_party_cannot_manufacture_a_second_identity(score):
    doc, calls = attributed([score] * 4)
    expected = "HOST" if score >= 0.68 else "UNKNOWN"
    assert {r["speaker"] for r in doc["cues"]} == {expected}
    assert doc["score_partition"]["groups"] == [] and calls == []


def test_positive_host_anchors_and_unanchored_context_decisions():
    doc, _ = attributed([0.85, 0.88, 0.1, 0.2],
                        [label(1, "OTHER"), label(2, "MEDIA"), label(3, "OTHER"), label(4, "HOST")])
    assert [row["speaker"] for row in doc["cues"]] == ["HOST", "HOST", "OTHER", "HOST"]
    assert doc["cues"][3]["voiceprint"]["anchor"] is None
    assert doc["cues"][3]["evidence"] == "context_expansion"
    assert doc["cues"][0]["evidence"] == "voiceprint_anchor"
    assert "3. OTHER" in sa.attribution_context_for_srt(doc, SRT)
    assert doc["policy"]["host_session_seed_min"] == 0.68
    assert doc["policy"]["guest_seed_max"] == 0.42


def relative_document():
    # A standalone synthetic receipt has the same defect as the Root's d5
    # result. It is deliberately not claimed to be a verified native receipt.
    doc, _ = attributed([0.05, 0.08, 0.35, 0.38])
    for row in doc["cues"][2:]:
        row.update(speaker="HOST", evidence="voiceprint_relative_anchor")
        row["voiceprint"]["anchor"] = "HOST"
    doc["status"] = "ANCHORS_ONLY"
    return doc


def test_legacy_relative_host_cannot_enter_text_context():
    assert sa.attribution_context_for_srt(relative_document(), SRT) == ""


@pytest.mark.parametrize("damage", ["downgraded_policy", "relative_relabelled_as_absolute",
                                    "duplicate_cue", "time", "text", "context_without_anchors"])
def test_context_consumer_rechecks_identity_basis_and_exact_cue_binding(damage):
    doc, _ = attributed([0.85, 0.88, 0.1, 0.2], [label(3, "OTHER"), label(4, "OTHER")])
    if damage == "downgraded_policy":
        doc["policy"]["host_session_seed_min"] = 0.01
        for row in doc["cues"][:2]:
            row["voiceprint"]["score"] = 0.35
    elif damage == "relative_relabelled_as_absolute":
        doc = relative_document()
        for row in doc["cues"][2:]:
            row["evidence"] = "voiceprint_anchor"
    elif damage == "duplicate_cue":
        doc["cues"][1] = deepcopy(doc["cues"][0])
    elif damage == "time":
        doc["cues"][2]["start_ms"] += 1
    elif damage == "text":
        doc["cues"][2]["text"] = "Unbound text"
    else:
        for row in doc["cues"]:
            row.update(speaker="OTHER", evidence="context_expansion", confidence=0.9, reason="Unanchored")
            row["voiceprint"]["anchor"] = None
    assert sa.attribution_context_for_srt(doc, SRT) == ""


@pytest.mark.parametrize("score", [True, "0.9", 1.5, float('nan'), float('inf')])
def test_invalid_scores_never_identify_host(score):
    doc, calls = attributed([score] * 4)
    assert all(row["speaker"] == "UNKNOWN" for row in doc["cues"])
    assert calls == []


def test_ordinary_pronoun_consumer_does_not_accept_relative_host_context():
    prompts = []
    def cpa(prompt):
        prompts.append(prompt)
        return json.dumps({"decisions": [
            {"occurrence_id": f"cue-{i}-occurrence-1", "action": "KEEP_CURRENT",
             "current_token": "她", "replacement_token": "她", "referent": "HOST",
             "reason": "Synthetic pronoun control only"} for i in range(1, 5)]})
    _, audit = discover_candidate_pronoun_findings(
        SRT, policy_text="Synthetic pronoun policy", candidate_context_text="Synthetic context",
        llm_call=cpa, extract_json=json.loads, speaker_attribution=relative_document())
    assert "speaker_attribution_context_sha256" not in audit
    assert "voiceprint_relative_anchor" not in prompts[0]
