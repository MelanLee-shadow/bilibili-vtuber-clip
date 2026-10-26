"""Synthetic relative-anchor witnesses; no development labels or live providers."""
import json
from pathlib import Path

import pytest

from src.autoslice import speaker_attribution as sa
from src.autoslice import speaker_attribution_geometry as sg


def make_srt(count=6):
    def stamp(ms):
        return f"00:00:{ms // 1000:02d},{ms % 1000:03d}"
    return "\n\n".join(
        f"{i + 1}\n{stamp(i * 2000 + 200)} --> {stamp(i * 2000 + 1200)}\n"
        f"她说测试语句{i + 1}" for i in range(count)
    ) + "\n"


def bound_scorer(srt, values, *, offset=0):
    count = len(values)
    binding = {
        "schema_version": sg.SCHEMA,
        "media_sha256": "a" * 64,
        "profile_sha256": "b" * 64,
        "model_tree_sha256": "c" * 64,
        "references": [
            {"id": f"reference-{i}", "filename": f"reference-{i}.wav", "sha256": str(i + 1) * 64}
            for i in range(3)
        ],
        "srt_sha256": sa._sha(srt),
        "timeline_offset_ms": offset,
        "geometry": sg.geometry(srt, offset, offset + count * 2000, offset + count * 2000),
    }
    return sa._SourceBoundScorer(lambda i: values[i - 1], binding, lambda: None)


def context_reply(prompt):
    if "# 播放素材筛查" in prompt:
        return json.dumps({"segments": [
            {"start_cue": 1, "end_cue": 6, "kind": "LIVE", "confidence": 0.9,
             "reason": "Synthetic continuous live conversation"}
        ]})
    return json.dumps({"labels": [
        {"n": i, "speaker": "OTHER", "address": "对话者", "confidence": 0.9,
         "reason": "Synthetic answer from the other participant"} for i in (3, 4)
    ]})


def test_bound_strong_relative_group_enters_normal_context_and_consumer_without_absolute_anchor():
    srt = make_srt()
    # All scores are below the absolute policy. Two clear groups use the
    # existing score geometry; no cue identity is supplied to the algorithm.
    scorer = bound_scorer(srt, [0.54, 0.56, 0.08, 0.10, 0.50, 0.52])
    doc = sa.attribute_speakers(srt, scorer=scorer, llm_call=context_reply)
    assert [r["speaker"] for r in doc["cues"]] == [
        sa.HOST, sa.HOST, sa.OTHER, sa.OTHER, sa.HOST, sa.HOST
    ]
    assert all(doc["cues"][i]["evidence"] == "voiceprint_relative_anchor"
               for i in (0, 1, 4, 5))
    rendered = sa.attribution_context_for_srt(
        doc, srt, expected_source_media_sha256="a" * 64,
        expected_profile_sha256="b" * 64, expected_timeline_offset_ms=0)
    assert "voiceprint_relative_anchor" in rendered
    assert doc["display_speaker_mode"] == "uniform_host"
    assert doc["release_authorized"] is False


def screen_reply(segments, count, *, fail_identity=False):
    calls = []

    def call(prompt):
        calls.append(prompt)
        if "# 播放素材筛查" in prompt:
            return json.dumps({"segments": [
                {"start_cue": a, "end_cue": b, "kind": kind, "confidence": 0.9,
                 "reason": "Synthetic source-context interval"} for a, b, kind in segments
            ]})
        if fail_identity:
            raise RuntimeError("Synthetic context outage")
        # Deliberately includes anchor/excluded rows to test that context cannot
        # overwrite either. No negative voiceprint identity is manufactured.
        return json.dumps({"labels": [
            {"n": i, "speaker": "OTHER", "address": "对话者", "confidence": 0.9,
             "reason": "Synthetic turn-taking evidence"} for i in range(1, count + 1)
        ]})
    return call, calls


@pytest.mark.parametrize("values,expected_hosts", [
    ([0.52, 0.54, 0.10, 0.12, 0.56, 0.58], [1, 2, 5, 6]),
    ([0.10, 0.12, 0.56, 0.58, 0.52, 0.54], [3, 4, 5, 6]),
])
def test_group_order_and_cue_position_do_not_choose_identity(values, expected_hosts):
    srt = make_srt(len(values))
    call, calls = screen_reply([(1, len(values), "LIVE")], len(values))
    doc = sa.attribute_speakers(srt, scorer=bound_scorer(srt, values), llm_call=call)
    assert [r["cue_index"] for r in doc["cues"] if r["speaker"] == sa.HOST] == expected_hosts
    assert len(calls) == 2
    assert "声纹锚点]" not in calls[0] and "0.52" not in calls[0]
    assert "片内相对声纹锚点" in calls[1]


@pytest.mark.parametrize("values", [
    [0.4, 0.4, 0.4, 0.4],
    [0.40, 0.42, 0.50, 0.52],
    [0.08, 0.09, 0.1, 0.5],
])
def test_weak_or_singleton_groups_remain_unknown(values):
    srt = make_srt(len(values))
    call, calls = screen_reply([(1, len(values), "LIVE")], len(values))
    doc = sa.attribute_speakers(srt, scorer=bound_scorer(srt, values), llm_call=call)
    assert {r["speaker"] for r in doc["cues"]} == {sa.UNKNOWN}
    assert doc["score_partition"]["identity_authorized"] is False
    assert len(calls) == 1  # Screen ran; identity was never guessed.


def test_media_is_removed_before_group_geometry_even_if_it_hides_the_two_groups():
    values = [0.36, 0.37, 0.10, 0.11, 0.99]
    srt = make_srt(len(values))
    call, _ = screen_reply([(1, 4, "LIVE"), (5, 5, "MEDIA")], len(values))
    doc = sa.attribute_speakers(srt, scorer=bound_scorer(srt, values), llm_call=call)
    assert [r["speaker"] for r in doc["cues"]] == [
        sa.HOST, sa.HOST, sa.OTHER, sa.OTHER, sa.UNKNOWN]
    assert doc["score_partition"]["excluded_cue_indexes"] == [5]
    assert [g["center"] for g in doc["score_partition"]["groups"]] == pytest.approx([0.105, 0.365])
    assert all(5 not in group["cue_indexes"] for group in doc["score_partition"]["groups"])


@pytest.mark.parametrize("kind", ["MEDIA", "UNKNOWN"])
def test_excluded_absolute_scores_cannot_reenter_through_context_or_anchor_fallback(kind):
    values = [0.85, 0.82, 0.55, 0.56, 0.10, 0.12]
    srt = make_srt()
    call, calls = screen_reply([(1, 2, kind), (3, 6, "LIVE")], 6)
    doc = sa.attribute_speakers(srt, scorer=bound_scorer(srt, values), llm_call=call)
    assert [r["speaker"] for r in doc["cues"][:2]] == [sa.UNKNOWN, sa.UNKNOWN]
    assert all(r["voiceprint"]["anchor"] is None for r in doc["cues"][:2])
    assert doc["cues"][2]["evidence"] == sa.EVIDENCE_RELATIVE
    assert "需要返回的行号（1-based，其余行不要返回）：[5, 6]" in calls[1]
    assert sa.attribution_context_for_srt(doc, srt)


def test_actual_padding_touching_media_is_excluded_on_the_source_clock():
    srt = make_srt().replace("00:00:02,200", "00:00:01,400")
    values = [0.90, 0.95, 0.55, 0.56, 0.10, 0.12]
    scorer = bound_scorer(srt, values, offset=2300)
    call, _ = screen_reply([(1, 1, "LIVE"), (2, 2, "MEDIA"), (3, 6, "LIVE")], 6)
    doc = sa.attribute_speakers(srt, scorer=scorer, llm_call=call)
    assert doc["score_partition"]["excluded_cue_indexes"] == [1, 2]
    assert doc["cues"][0]["speaker"] == sa.UNKNOWN
    assert doc["cues"][0]["end_ms"] < doc["cues"][1]["start_ms"]
    assert doc["media_screen"]["segments"][1]["source_window_ms"] == [3550, 5650]
    assert sa.attribution_context_for_srt(doc, srt, expected_timeline_offset_ms=2300)
    assert not sa.attribution_context_for_srt(doc, srt, expected_timeline_offset_ms=0)


def test_media_removal_recomputes_support_counts():
    srt = make_srt(4)
    call, _ = screen_reply([(1, 1, "LIVE"), (2, 2, "MEDIA"), (3, 4, "LIVE")], 4)
    doc = sa.attribute_speakers(
        srt, scorer=bound_scorer(srt, [.55, .56, .10, .12]), llm_call=call)
    assert {r["speaker"] for r in doc["cues"]} == {sa.UNKNOWN}
    assert doc["score_partition"]["identity_authorized"] is False


@pytest.mark.parametrize("overlap_group", ["high", "low"])
def test_duplicate_audio_windows_do_not_supply_two_independent_group_supports(overlap_group):
    srt = make_srt(4)
    if overlap_group == "high":
        srt = srt.replace("00:00:02,200 --> 00:00:03,200", "00:00:00,200 --> 00:00:01,200")
    else:
        srt = srt.replace("00:00:06,200 --> 00:00:07,200", "00:00:04,200 --> 00:00:05,200")
    call, _ = screen_reply([(1, 4, "LIVE")], 4)
    doc = sa.attribute_speakers(
        srt, scorer=bound_scorer(srt, [.55, .56, .10, .12]), llm_call=call)
    assert {r["speaker"] for r in doc["cues"]} == {sa.UNKNOWN}


@pytest.mark.parametrize("damage", ["missing_refs", "ref_hash", "ref_duplicate", "geometry"])
def test_broken_source_contract_never_uses_its_scores_as_absolute_fallback(damage):
    srt = make_srt()
    scorer = bound_scorer(srt, [.85, .82, .10, .12, .55, .56])
    if damage == "missing_refs":
        scorer.source_binding["references"] = []
    elif damage == "ref_hash":
        scorer.source_binding["references"][0]["sha256"] = "not-a-hash"
    elif damage == "ref_duplicate":
        scorer.source_binding["references"][1] = scorer.source_binding["references"][0]
    else:
        scorer.source_binding["geometry"]["cue_windows"][0]["source_window_ms"][0] += 1
    call, calls = screen_reply([(1, 6, "LIVE")], 6)
    doc = sa.attribute_speakers(srt, scorer=scorer, llm_call=call)
    assert {r["speaker"] for r in doc["cues"]} == {sa.UNKNOWN}
    assert doc["reason_codes"] == ["SCORER_SOURCE_DRIFT"]
    assert calls == []


def test_no_source_or_no_screen_does_not_authorize_relative_identity():
    srt = make_srt()
    values = [.55, .56, .10, .12, .53, .54]
    for scorer in (lambda i: values[i - 1], bound_scorer(srt, values)):
        doc = sa.attribute_speakers(srt, scorer=scorer)
        assert {r["speaker"] for r in doc["cues"]} == {sa.UNKNOWN}
        assert doc["context_calls"] == []


@pytest.mark.parametrize("segments", [
    [(2, 6, "LIVE")], [(1, 3, "LIVE"), (3, 6, "LIVE")],
    [(1, 5, "LIVE")], [(1, 7, "LIVE")], [(1, 6, "unrecognized")],
])
def test_incomplete_or_invalid_media_screen_is_not_assumed_live(segments):
    srt = make_srt()
    call, calls = screen_reply(segments, 6)
    doc = sa.attribute_speakers(
        srt, scorer=bound_scorer(srt, [.85, .82, .10, .12, .53, .54]), llm_call=call)
    assert {r["speaker"] for r in doc["cues"]} == {sa.UNKNOWN}
    assert doc["media_screen"]["status"] == "UNAVAILABLE"
    assert len(calls) == 1
    assert sa.replay_attribution(doc, srt)["cues"] == doc["cues"]


def test_failed_identity_context_keeps_relative_anchors_and_replays_without_provider():
    srt = make_srt()
    call, _ = screen_reply([(1, 6, "LIVE")], 6, fail_identity=True)
    doc = sa.attribute_speakers(
        srt, scorer=bound_scorer(srt, [.55, .56, .10, .12, .53, .54]), llm_call=call)
    assert doc["status"] == "ANCHORS_ONLY"
    assert doc["cues"][2]["speaker"] == sa.UNKNOWN
    assert doc["cues"][0]["evidence"] == sa.EVIDENCE_RELATIVE
    assert sa.replay_attribution(doc, srt) == doc
    assert sa.attribution_context_for_srt(doc, srt)


@pytest.mark.parametrize("damage", [
    "score_partition", "media_kind", "media_window", "response", "response_rehashed",
    "missing_trace", "missing_screen", "relabel", "reference", "geometry", "lower_policy",
])
def test_persisted_relative_claims_are_recomputed_before_the_text_consumer(damage):
    srt = make_srt()
    doc = sa.attribute_speakers(
        srt, scorer=bound_scorer(srt, [.55, .56, .10, .12, .53, .54]),
        llm_call=context_reply)
    doc = json.loads(json.dumps(doc))
    if damage == "score_partition":
        doc["score_partition"]["groups"][1]["center"] = .99
    elif damage == "media_kind":
        doc["media_screen"]["segments"][0]["kind"] = "MEDIA"
    elif damage == "media_window":
        doc["media_screen"]["segments"][0]["source_window_ms"][0] += 1
    elif damage in ("response", "response_rehashed"):
        doc["context_calls"][0]["response"] = '{"segments":[]}'
        if damage == "response_rehashed":
            doc["context_calls"][0]["response_sha256"] = sa.context_evidence.digest(
                doc["context_calls"][0]["response"])
    elif damage == "missing_trace":
        doc["context_calls"] = []
    elif damage == "missing_screen":
        doc["media_screen"] = None
    elif damage == "relabel":
        doc["cues"][2].update(speaker=sa.HOST, evidence=sa.EVIDENCE_RELATIVE)
        doc["cues"][2]["voiceprint"]["anchor"] = sa.HOST
    elif damage == "reference":
        doc["source_binding"]["references"][0]["sha256"] = "e" * 64
    elif damage == "geometry":
        doc["source_binding"]["geometry"]["cue_windows"][0]["source_window_ms"][0] += 1
    else:
        doc["policy"]["host_session_seed_min"] = .3
    assert sa.attribution_context_for_srt(doc, srt) == ""


# Exercise the existing ordinary producer and real subprocess/CLI/factory. Only
# the actual model forward and external context service are synthetic here.
from tests.test_producer_speaker_generation import (
    call_producer,
    case as case,  # Re-export the shared pytest fixture.
    sha,
    write_wave,
)


def configure_producer_context(case, values):
    write_wave(case["media"], 16)
    code = case["runtime"].read_text()
    old = "return {'cue-0001.wav':.85, 'cue-0002.wav':.82}.get(cue.name, .20)"
    table = {f"cue-{i:04d}.wav": value for i, value in enumerate(values, 1)}
    assert old in code
    code = code.replace(old, "return " + repr(table) + "[cue.name]")
    hook = """
from src.autoslice import speaker_context as context
def external_context(prompt, **kwargs):
    with pathlib.Path(CALL_LOG).open('a') as handle: handle.write('call\\n')
    if '# 播放素材筛查' in prompt:
        return json.dumps({'segments': [
            {'start_cue':1,'end_cue':5,'kind':'LIVE','confidence':.9,'reason':'Synthetic live scene'},
            {'start_cue':6,'end_cue':6,'kind':'MEDIA','confidence':.9,'reason':'Synthetic playback scene'}]})
    return json.dumps({'labels': [
        {'n':i,'speaker':'OTHER','address':'对话者','confidence':.9,'reason':'Synthetic conversation'}
        for i in range(1,7)]})
context._call_context_via_cpa = external_context
""".replace("CALL_LOG", repr(str(case["tmp"] / "context-call.log")))
    code = code.replace("sys.argv = ['speaker_attribution']", hook + "\nsys.argv = ['speaker_attribution']")
    case["runtime"].write_text(code)
    return make_srt()


@pytest.mark.parametrize("values,hosts", [
    ([.54, .56, .10, .12, .55, .95], [1, 2, 5]),
    ([.10, .12, .54, .56, .55, .95], [3, 4, 5]),
])
def test_normal_producer_context_opt_in_and_cold_recovery_reach_real_consumer(case, monkeypatch, values, hosts):
    srt = configure_producer_context(case, values)
    first, authority = call_producer(case, srt=srt, end=12000, extra_options={"context": True})
    meta = first["speaker_attribution_generation"]
    assert meta["status"] == "GENERATED", meta
    assert meta["context_requested"] is True
    doc = authority["speaker_attribution"]
    assert [r["cue_index"] for r in doc["cues"] if r["speaker"] == sa.HOST] == hosts
    assert doc["cues"][5]["speaker"] == sa.UNKNOWN
    assert doc["score_partition"]["excluded_cue_indexes"] == [6]
    assert "voiceprint_relative_anchor" in case["prompts"][0]
    assert first["release_gate"] == "BLOCK"  # Ordinary unrelated release gate survives.
    from src.autoslice import producer_speaker_attribution as producer
    monkeypatch.setattr(producer, "_execute", lambda *a: pytest.fail("Cold replay must not dispatch"))
    raw = Path(meta["attribution_path"]).read_bytes()
    case["prompts"].clear()
    second, second_authority = call_producer(
        case, srt=srt, end=12000, extra_options={"context": True})
    assert second["speaker_attribution_generation"]["status"] == "CACHE_HIT"
    assert second_authority["speaker_attribution"] == doc
    assert Path(meta["attribution_path"]).read_bytes() == raw
    assert (case["tmp"] / "context-call.log").read_text().splitlines() == ["call", "call"]
    assert "voiceprint_relative_anchor" in case["prompts"][0]


@pytest.mark.parametrize("damage", ["response", "parsed_screen", "reference", "audio", "half_written"])
def test_normal_cold_recovery_retains_and_rejects_damaged_relative_evidence(case, monkeypatch, damage):
    from src.autoslice import producer_speaker_attribution as producer
    srt = configure_producer_context(case, [.54, .56, .10, .12, .55, .95])
    first, _ = call_producer(case, srt=srt, end=12000, extra_options={"context": True})
    raw = Path(first["speaker_attribution_generation"]["attribution_path"])
    directory = raw.parent
    doc = json.loads(raw.read_text())
    if damage == "response":
        doc["context_calls"][0]["response"] = '{"segments":[]}'
    elif damage == "parsed_screen":
        doc["media_screen"]["segments"][1]["kind"] = "LIVE"
    elif damage == "reference":
        doc["source_binding"]["references"][0]["sha256"] = "e" * 64
    elif damage == "audio":
        with (directory / "work/cue-wavs/cue-0001.wav").open("ab") as handle:
            handle.write(b"changed")
    else:
        (directory / "terminal.json").unlink()
    if damage in ("response", "parsed_screen", "reference"):
        raw.write_text(json.dumps(doc))
        terminal = json.loads((directory / "terminal.json").read_text())
        terminal["raw_sha256"] = sha(raw)
        (directory / "terminal.json").write_text(json.dumps(terminal))
    altered = raw.read_bytes()
    monkeypatch.setattr(producer, "_execute", lambda *a: pytest.fail("Do not rerun damaged request"))
    second, _ = call_producer(case, srt=srt, end=12000, extra_options={"context": True})
    assert second["speaker_attribution_generation"]["status"] == "UNKNOWN"
    assert raw.read_bytes() == altered
    assert (case["tmp"] / "context-call.log").read_text().splitlines() == ["call", "call"]
