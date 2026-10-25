"""Synthetic controls for speaker-attribution.v1 (no CAM++, no network, no audio)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.autoslice import speaker_attribution as sa
from src.autoslice.speaker_common import DEFAULT_POLICY

HOST_MIN = float(DEFAULT_POLICY["host_session_seed_min"])
GUEST_MAX = float(DEFAULT_POLICY["guest_seed_max"])


def srt(*texts: str, seconds: float = 3.0) -> str:
    blocks = []
    for i, text in enumerate(texts):
        start = i * 4
        blocks.append(
            f"{i + 1}\n00:00:{start:02d},000 --> 00:00:{start + int(seconds):02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def scorer_from(scores: dict[int, float | None]):
    return lambda index: scores[index]


SOURCE = srt("我先说一下", "你这个想法不错", "对啊我也这么想", "那我们开始吧", "好的没问题")
# 1,3,4 voiceprint-HOST; 2 has low HOST support, not a known non-host; 5 ambiguous.
SCORES = {1: HOST_MIN + 0.1, 2: GUEST_MAX - 0.1, 3: HOST_MIN + 0.05, 4: HOST_MIN, 5: 0.55}


def reply(*rows):
    return lambda _prompt: json.dumps({"labels": list(rows)}, ensure_ascii=False)


def row(n, speaker, address=None, confidence=0.9, reason="依据"):
    return {"n": n, "speaker": speaker, "address": address, "confidence": confidence, "reason": reason}


def by_cue(doc):
    return {r["cue_index"]: r for r in doc["cues"]}


def test_thresholds_are_the_existing_policy_constants():
    doc = sa.attribute_speakers(SOURCE, scorer=scorer_from(SCORES))
    assert doc["policy"] == {
        "host_session_seed_min": DEFAULT_POLICY["host_session_seed_min"],
        "guest_seed_max": DEFAULT_POLICY["guest_seed_max"],
        "guest_min_duration_ms": DEFAULT_POLICY["guest_min_duration_ms"],
    }


def test_anchors_are_fixed_and_context_cannot_overturn_them():
    doc = sa.attribute_speakers(
        SOURCE,
        scorer=scorer_from(SCORES),
        # Context cannot flip positive anchors; the low-score row stays eligible.
        llm_call=reply(
            row(1, "OTHER", "闺蜜"),
            row(2, "HOST"),
            row(3, "MEDIA"),
            row(5, "OTHER", " 闺 蜜 "),
        ),
        runtime_location="synthetic-test",
    )
    cues = by_cue(doc)
    assert doc["schema_version"] == "speaker-attribution.v1"
    assert doc["status"] == "ATTRIBUTED"
    assert [cues[i]["speaker"] for i in (1, 3, 4)] == ["HOST"] * 3
    assert all(cues[i]["evidence"] == "voiceprint_anchor" for i in (1, 3, 4))
    assert cues[1]["voiceprint"]["anchor"] == "HOST"
    assert cues[1]["voiceprint"]["margin"] == pytest.approx(0.1, abs=1e-6)
    # A low HOST similarity cannot veto an otherwise valid context HOST decision.
    assert cues[2]["voiceprint"]["anchor"] is None
    assert cues[2]["speaker"] == "HOST" and cues[2]["evidence"] == "context_expansion"
    assert cues[5]["speaker"] == "OTHER"
    assert cues[5]["other_label"] == "闺蜜"
    assert cues[5]["evidence"] == "context_expansion"
    assert doc["other_label"] == "闺蜜"
    assert doc["runtime"]["location"] == "synthetic-test"


def test_low_score_can_be_inferred_as_media_by_context_without_a_negative_anchor():
    doc = sa.attribute_speakers(
        SOURCE,
        scorer=scorer_from(SCORES),
        llm_call=reply(row(2, "MEDIA"), row(5, "UNKNOWN")),
    )
    cues = by_cue(doc)
    assert cues[2]["speaker"] == "MEDIA" and cues[2]["evidence"] == "context_expansion"
    assert cues[5]["speaker"] == "UNKNOWN"


def test_display_and_release_authority_stay_unchanged():
    doc = sa.attribute_speakers(SOURCE, scorer=scorer_from(SCORES), llm_call=reply())
    assert doc["display_speaker_mode"] == "uniform_host"
    assert doc["display_authorized"] is doc["release_authorized"] is False
    assert doc["mutation_authorized"] is False


@pytest.mark.parametrize(
    "scorer",
    [
        None,
        lambda _i: (_ for _ in ()).throw(RuntimeError("no modelscope")),
        lambda _i: None,
    ],
    ids=["no-embedder", "embedder-raises", "embedder-returns-none"],
)
def test_unavailable_embedder_records_unknown_without_error_or_context_call(scorer):
    calls = []
    doc = sa.attribute_speakers(
        SOURCE, scorer=scorer, llm_call=lambda p: calls.append(p) or "{}"
    )
    assert doc["status"] == "EMBEDDER_UNAVAILABLE"
    assert [r["speaker"] for r in doc["cues"]] == ["UNKNOWN"] * 5
    assert {r["evidence"] for r in doc["cues"]} == {"unknown"}
    assert calls == []
    assert doc["release_authorized"] is False


def test_fewer_than_two_host_anchors_is_unknown_and_skips_context():
    calls = []
    scores = {1: HOST_MIN + 0.1, 2: 0.5, 3: 0.5, 4: 0.5, 5: GUEST_MAX - 0.1}
    doc = sa.attribute_speakers(
        SOURCE, scorer=scorer_from(scores), llm_call=lambda p: calls.append(p) or "{}"
    )
    assert doc["status"] == "NO_ANCHORS"
    assert [r["speaker"] for r in doc["cues"]] == ["UNKNOWN"] * 5
    assert all(r["voiceprint"]["anchor"] is None for r in doc["cues"])
    assert calls == []


def test_short_low_score_cue_is_not_a_non_host_anchor():
    short = srt("我先说一下", "嗯", "对啊我也这么想", "那我们开始吧", seconds=1.0)
    doc = sa.attribute_speakers(
        short, scorer=scorer_from({1: HOST_MIN, 2: GUEST_MAX - 0.1, 3: HOST_MIN, 4: HOST_MIN})
    )
    assert by_cue(doc)[2]["voiceprint"]["anchor"] is None


def test_context_failure_keeps_anchors_and_leaves_rest_unknown():
    def boom(_prompt):
        raise RuntimeError("provider down")

    doc = sa.attribute_speakers(SOURCE, scorer=scorer_from(SCORES), llm_call=boom)
    cues = by_cue(doc)
    assert doc["status"] == "ANCHORS_ONLY"
    assert "RuntimeError" in doc["reason_codes"]
    assert [cues[i]["speaker"] for i in (1, 3, 4)] == ["HOST"] * 3
    assert cues[5]["speaker"] == "UNKNOWN"


def test_malformed_context_reply_changes_nothing():
    doc = sa.attribute_speakers(
        SOURCE,
        scorer=scorer_from(SCORES),
        llm_call=reply(row(5, "OTHER", "闺蜜"), row(5, "MEDIA")),  # duplicate cue
    )
    assert doc["status"] == "ANCHORS_ONLY"
    assert by_cue(doc)[5]["speaker"] == "UNKNOWN"


def test_context_prompt_carries_anchor_tags_and_turn_rules():
    prompts = []
    sa.attribute_speakers(
        SOURCE,
        scorer=scorer_from(SCORES),
        llm_call=lambda p: prompts.append(p) or json.dumps({"labels": []}),
        other_label_hint="闺蜜",
    )
    prompt = prompts[0]
    assert "1. [李豆沙·声纹锚点] 我先说一下" in prompt
    assert "2. [待判] 你这个想法不错" in prompt
    assert "非李豆沙·声纹锚点" not in prompt
    assert "5. [待判] 好的没问题" in prompt
    assert "对话轮次" in prompt and "闺蜜" in prompt


def test_attribution_context_is_rendered_and_rejected_when_stale():
    doc = sa.attribute_speakers(
        SOURCE, scorer=scorer_from(SCORES), llm_call=reply(row(5, "OTHER", "闺蜜"))
    )
    text = sa.attribution_context_for_srt(doc, SOURCE)
    assert "1. HOST（voiceprint_anchor）" in text
    assert "5. OTHER:闺蜜（context_expansion）" in text
    assert "2." not in text  # UNKNOWN rows omitted
    assert sa.attribution_context_for_srt(doc, SOURCE.replace("先说", "先讲")) == ""
    assert sa.attribution_context_for_srt(None, SOURCE) == ""


def test_production_scorer_is_median_over_references(tmp_path, monkeypatch):
    from src.autoslice import speaker_finalizer as finalizer

    # The factory now binds actual input bytes before a fake ML runtime is used.
    (tmp_path / "m.mp4").write_bytes(b"synthetic media")
    (tmp_path / "p.json").write_text("{}")
    references = [{"path": tmp_path / f"r{i}.wav"} for i in range(3)]
    cue_paths = [tmp_path / f"c{i}.wav" for i in range(5)]
    seen = {}

    monkeypatch.setattr(
        finalizer, "_load_runtime", lambda *a: ({"p": 1}, references, "model-hash", object())
    )

    def extract(media, cues, work_dir):
        seen["cues"] = [(c.start, c.end, c.text) for c in cues]
        return None, 16000, cue_paths

    monkeypatch.setattr(finalizer, "_extract_cue_wavs", extract)
    table = {0: 0.2, 1: 0.9, 2: 0.4}
    monkeypatch.setattr(
        finalizer,
        "_build_embedding_similarity",
        lambda **_k: lambda ref, cue: table[int(ref.stem[1:])],
    )
    monkeypatch.setattr(finalizer, "_policy", lambda profile: dict(DEFAULT_POLICY))
    scorer, policy = sa.build_campp_cue_scorer(
        media_path=tmp_path / "m.mp4",
        srt_text=SOURCE,
        profile_path=tmp_path / "p.json",
        reference_dir=tmp_path,
        model_dir=tmp_path,
        work_dir=tmp_path / "w",
    )
    assert scorer(1) == pytest.approx(0.4)
    assert policy["host_session_seed_min"] == HOST_MIN
    assert seen["cues"][0] == ("00:00:00,000", "00:00:03,000", "我先说一下")


def test_cli_degrades_to_unknown_document_when_runtime_is_missing(tmp_path, capsys):
    source = tmp_path / "a.srt"
    source.write_text(SOURCE, encoding="utf-8")
    output = tmp_path / "out" / "attr.json"
    code = sa.main(
        [
            "--media", str(tmp_path / "missing.mp4"), "--srt", str(source),
            "--profile", str(tmp_path / "p.json"), "--reference-dir", str(tmp_path),
            "--model-dir", str(tmp_path), "--work-dir", str(tmp_path / "w"),
            "--output", str(output), "--runtime-location", "local-no-ml", "--no-context",
        ]
    )
    document = json.loads(Path(output).read_text(encoding="utf-8"))
    assert code == 0
    assert document["status"] == "EMBEDDER_UNAVAILABLE"
    assert document["runtime"]["location"] == "local-no-ml"
    assert any(code.startswith("EMBEDDER_RUNTIME:") for code in document["reason_codes"])
