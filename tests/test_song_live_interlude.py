"""Ordinary offline producer -> persisted proof -> subtitle/completion consumers."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.review_evidence import SourceCue
from src.autoslice.song_common import CHANNEL_PROFILE, LrcLine, LrcResult, is_lrc_non_lyric_metadata
from src.autoslice.song_alignment import _parse_asr_anchor_srt
from src.autoslice.song_repair import attempt_song_repair
from src.autoslice.song_lrc_provider import parse_lrc_text
from src.autoslice.live_source_review import _verify_live_performance_observation
from src.autoslice.recut_materialization import _load_lyric_timeline, _write_lyric_timeline_srt
from src.autoslice.subtitle_rendering import _write_sapphire_ass_from_srt
from src.autoslice.song_completion import _validate_alignment_report, _is_int
from test_song_repair import _write_fake_audio_alignment_run, _rebind_fake_audio_alignment_run, _omit_live_arrangement_rows


NEW_CLASS = "COMPLETE_LIVE_WITH_SPOKEN_INTERLUDE"
WORDS = ["amber lantern", "cobalt river", "violet meadow", "silver willow", "scarlet comet",
         "golden sparrow", "velvet orchard", "crimson planet", "indigo forest", "copper ocean",
         "marble thunder", "saffron valley", "verdant island", "azure mountain", "lunar garden",
         "pearly sunrise", "russet autumn", "snowy rooftop"]


def ordinary_interlude(tmp_path, *, mutation=None, metadata=False, hole=(7, 8, 9)):
    lines = [LrcLine(10000 + i * 6000, word) for i, word in enumerate(WORDS)]
    # True lyric prefix, including a role-looking prefix, must survive.
    lines[3] = replace(lines[3], text="（男）silver willow")
    if metadata:
        lines.insert(0, LrcLine(0, "【Aurora_27】"))
    lrc = LrcResult("fixture", "synthetic live", "fixture singer", "fixture://interlude", tuple(lines))
    shift = int(metadata)
    # Three silent rows are neither a small skip nor a repeat: only source
    # speech can make this hole complete (song_omission_shape).
    omitted = [shift + i for i in hole]
    if mutation == "two_holes":
        omitted = [shift + 7, shift + 10]
    elif mutation == "missing_first":
        omitted = [shift, shift + 1]
    elif mutation == "missing_last":
        omitted = [len(lines) - 2, len(lines) - 1]
    elif mutation == "block_too_large":
        omitted = list(range(shift + 3, shift + 9))
    run = _write_fake_audio_alignment_run(tmp_path, lrc, candidate_id="synthetic-interlude",
                                           offset_ms=40000, source_duration_ms=200000)
    payload = json.loads(json.dumps(run.payload))
    _omit_live_arrangement_rows(payload, omitted + ([0] if metadata else []))
    payload["live_arrangement"]["classification"] = "INCOMPLETE_OR_FRAGMENT"
    if mutation == "missing_opening":
        payload["live_arrangement"]["observed_live_song_opening"] = False
    elif mutation == "missing_ending":
        payload["live_arrangement"]["observed_live_song_ending"] = False
    elif mutation == "discontinuous":
        payload["live_performance"]["continuous_live_song_performance"] = False
    elif mutation == "no_transition":
        payload["live_arrangement"].update(post_song_transition_kind="NONE_OR_UNKNOWN", post_song_transition_ms=None)
        payload["post_song_talk_start_ms"] = None
    elif mutation == "long_outro":
        payload["live_arrangement"]["post_song_transition_ms"] = 199000
        payload["post_song_talk_start_ms"] = 199000
    run = _rebind_fake_audio_alignment_run(run, payload)
    cues = [SourceCue(f"lyric-{i}", line.time_ms + 40000, line.time_ms + 43000, line.text)
            for i, line in enumerate(lines) if i not in omitted and not is_lrc_non_lyric_metadata(line.text)]
    speech = [SourceCue("speech-a", 90000, 92000, "刚刚差点忘记怎么唱了"),
              SourceCue("speech-b", 94000, 96000, "让我跟着节拍再想一想")]
    if mutation == "instrumental":
        speech = [replace(c, text="[instrumental]") for c in speech]
    elif mutation == "empty":
        speech = []
    elif mutation == "lyric_as_speech":
        speech = [replace(c, text=lines[omitted[0]].text) for c in speech]
    elif mutation == "outside_window":
        speech = [replace(c, source_start_ms=c.source_start_ms + 100000,
                          source_end_ms=c.source_end_ms + 100000) for c in speech]
    elif mutation == "other_speaker":
        speech = [replace(c, speaker="GUEST") for c in speech]
    elif mutation == "other_speaker_label":
        speech = [replace(c, text=f"[{CHANNEL_PROFILE.guest_speaker_label}] {c.text}") for c in speech]
    elif mutation == "unknown_speaker_label":
        speech = [replace(c, text=f"[unresolved voice] {c.text}") for c in speech]
    elif mutation == "host_speaker_label":
        speech = [replace(c, text=f"[{CHANNEL_PROFILE.host_speaker_label}] {c.text}") for c in speech]
    elif mutation == "internal_disclosure":
        speech = [replace(c, text="[听不清]") for c in speech]
    elif mutation == "entity_spelling":
        speech = [replace(speech[0], text="谢谢kimo熊来看我唱歌"), speech[1]]
    cues = sorted(cues + speech, key=lambda c: c.source_start_ms)
    result = attempt_song_repair(
        candidate_id="synthetic-interlude", cues=cues, anchor_start_ms=50000,
        anchor_end_ms=70000, source_duration_ms=200000, output_dir=tmp_path / "repair",
        lrc_provider=lambda _query: lrc, source_media_path=Path(run.source_path),
        audio_lrc_aligner=lambda *_args: run, asr_anchor_cues=cues,
    )
    return result, speech


def read_report(result):
    return json.loads(Path(result.lyrics_alignment["alignment_report_path"]).read_text())


def check_completion(result, report):
    failures = []
    _validate_alignment_report(report=report, alignment=dict(result.lyrics_alignment),
        boundary=dict(result.song_boundary), boundary_zero=40000,
        record={"candidate_id": "synthetic-interlude"}, failures=failures, is_int=_is_int)
    return failures


@pytest.mark.parametrize("metadata", [False, True])
def test_ordinary_source_interlude_roundtrip_to_both_consumers_and_srt_ass(tmp_path, metadata):
    result, speech = ordinary_interlude(tmp_path, metadata=metadata)
    assert result.repaired, result.attempts
    assert result.song_boundary["completion_basis"] == NEW_CLASS
    report = read_report(result)
    assert report["arrangement_completeness"]["canonical_line_count"] == 18
    assert report["line_count"] == 15
    assert _verify_live_performance_observation(result.lyrics_alignment, output_dir=tmp_path / "repair") is None
    assert check_completion(result, report) == []
    timeline, offset = _load_lyric_timeline({"lyrics_alignment": result.lyrics_alignment}, output_dir=tmp_path / "repair")
    srt = tmp_path / "final.srt"
    _write_lyric_timeline_srt(timeline, offset, 40000, 160000, srt)
    text = srt.read_text()
    assert all(c.text in text for c in speech)
    assert all(word not in text for word in ("crimson planet", "indigo forest", "copper ocean"))
    assert "Aurora_27" not in text
    assert "（男）silver willow" in text
    from src.autoslice.subtitle_validation import validate_srt_text
    assert validate_srt_text(text)["status"] == "PASS"
    ass = tmp_path / "final.ass"
    _write_sapphire_ass_from_srt(srt, ass)
    events = [line for line in ass.read_text().splitlines() if line.startswith("Dialogue:")]
    assert all(",LDS," in next(e for e in events if c.text in e) for c in speech)
    assert all(",Default," in e for e in events if not any(c.text in e for c in speech))
    assert all(not e.split(",", 9)[9].startswith("[") for e in events)


@pytest.mark.parametrize("mutation", ["missing_opening", "missing_ending", "missing_first",
    "missing_last", "block_too_large", "instrumental", "empty", "lyric_as_speech", "outside_window",
    "other_speaker", "other_speaker_label", "unknown_speaker_label", "discontinuous",
    "no_transition", "long_outro", "internal_disclosure"])
def test_interlude_negative_shapes_stay_unresolved(tmp_path, mutation):
    result, _ = ordinary_interlude(tmp_path, mutation=mutation)
    assert not result.repaired




@pytest.mark.parametrize(("hole", "mutation", "classification", "spoken"), [
    ((7, 8), None, NEW_CLASS, True),
    ((7, 8), "two_holes", NEW_CLASS, True),
    ((7, 8), "instrumental", "COMPLETE_LIVE_ARRANGEMENT", False),
    ((7, 8), "empty", "COMPLETE_LIVE_ARRANGEMENT", False),
])
def test_small_holes_are_complete_and_speech_follows_one_rule(tmp_path, hole, mutation, classification, spoken):
    result, speech = ordinary_interlude(tmp_path, hole=hole, mutation=mutation)
    assert result.repaired, result.attempts
    assert result.song_boundary["completion_basis"] == classification
    assert _verify_live_performance_observation(result.lyrics_alignment, output_dir=tmp_path / "repair") is None
    report = read_report(result)
    assert check_completion(result, report) == []
    timeline, offset = _load_lyric_timeline({"lyrics_alignment": result.lyrics_alignment}, output_dir=tmp_path / "repair")
    srt = tmp_path / "small.srt"
    _write_lyric_timeline_srt(timeline, offset, 40000, 160000, srt)
    text = srt.read_text()
    unheard = [r["text"] for r in report["canonical_lyric_lines"]
               if r["lrc_index"] not in {a["canonical_lrc_index"] for a in report["alignment"]}]
    assert len(unheard) == 2 and all(word not in text for word in unheard)
    assert (f"[{CHANNEL_PROFILE.host_speaker_label}]" in text) is spoken
    if spoken:
        assert all(c.text in text for c in speech)


def test_spoken_entity_spelling_uses_existing_authority(tmp_path):
    result, _ = ordinary_interlude(tmp_path, mutation="entity_spelling")
    assert result.repaired, result.attempts
    cue = read_report(result)["arrangement_completeness"]["omitted_ranges"][0]["speech_cues"][0]
    assert cue["source_text"] == "谢谢kimo熊来看我唱歌"
    assert cue["text"] == "谢谢kmx来看我唱歌"


def test_source_host_label_is_bound_but_not_duplicated_in_display(tmp_path):
    result, speech = ordinary_interlude(tmp_path, mutation="host_speaker_label")
    assert result.repaired, result.attempts
    report = read_report(result)
    assert report["arrangement_completeness"]["omitted_ranges"][0]["speech_cues"][0]["source_text"] == speech[0].text
    assert _verify_live_performance_observation(result.lyrics_alignment, output_dir=tmp_path / "repair") is None
    timeline, offset = _load_lyric_timeline({"lyrics_alignment": result.lyrics_alignment}, output_dir=tmp_path / "repair")
    srt = tmp_path / "labelled.srt"
    _write_lyric_timeline_srt(timeline, offset, 40000, 160000, srt)
    assert srt.read_text().count(f"[{CHANNEL_PROFILE.host_speaker_label}]") == 2


@pytest.mark.parametrize("mutation", ["speech_text", "source_hash", "speech_time", "speaker", "remove_source",
    "null_completeness", "legacy_speech_injection"])
def test_cold_consumers_reject_interlude_evidence_drift(tmp_path, mutation):
    result, _ = ordinary_interlude(tmp_path)
    assert result.repaired
    report = read_report(result)
    if mutation == "speech_text":
        report["arrangement_completeness"]["omitted_ranges"][0]["speech_cues"][0]["text"] = "made up caption"
    elif mutation == "null_completeness":
        report["arrangement_completeness"] = None
    elif mutation == "legacy_speech_injection":
        for key in ("timing_model", "asr_timing_input", "timing_sections"):
            report.pop(key, None)
        report["arrangement_completeness"]["classification"] = "FULL_STUDIO_SEQUENCE"
    else:
        cue_path = Path(report["asr_timing_input"]["path"])
        data = json.loads(cue_path.read_text())
        if mutation == "source_hash":
            data["source_sha256"] = "f" * 64
        elif mutation == "remove_source":
            cue_path.unlink()
        else:
            row = next(r for r in data["cues"] if r[0] == "speech-a")
            if mutation == "speech_time":
                row[1] = 1
            else:
                row.extend(["speech", "GUEST"])
        if mutation != "remove_source":
            cue_path.write_text(json.dumps(data))
            report["asr_timing_input"]["sha256"] = hashlib.sha256(cue_path.read_bytes()).hexdigest()
    report_path = Path(result.lyrics_alignment["alignment_report_path"])
    report_path.write_text(json.dumps(report))
    alignment = dict(result.lyrics_alignment, alignment_report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest())
    assert _verify_live_performance_observation(alignment, output_dir=tmp_path / "repair") is not None
    assert check_completion(result, report)
    assert _load_lyric_timeline({"lyrics_alignment": alignment}, output_dir=tmp_path / "repair") is None


@pytest.mark.parametrize("text", ["【hanser】", "【KBShinya】", "【合】", "（男）", "[女]", "【NeonFox_8】",
    "【合 hanser主】", "【hanser这里要录两遍主声】", "【hanser 这里要录两遍主声】", "翻唱：某歌手", "曲绘：某画师"])
def test_performer_rows_are_shared_non_lyric_metadata(text):
    assert is_lrc_non_lyric_metadata(text)


@pytest.mark.parametrize("text", ["（男）我走在雨中", "【hanser】我走在雨中", "[女]我想起你",
    "（我想你）", "【我走在雨中】", "(I love you)", "(Hello)", "【All my love】"])
def test_bracketed_lyric_text_is_preserved(text):
    assert not is_lrc_non_lyric_metadata(text)


def test_source_srt_index_is_not_spoken_text(tmp_path):
    path = tmp_path / "source.srt"
    path.write_text("89\n00:04:52,620 --> 00:04:54,100\n这段好像有词\n")
    assert _parse_asr_anchor_srt(path)[0].text == "这段好像有词"


def test_lrc_parser_preserves_bracket_lyrics_and_defers_labels_to_shared_classifier():
    text = "[00:01.000][NeonFox_8]\n[00:02.000][女]我想起你\n[00:03.000][All my love]\n"
    assert [line.text for line in parse_lrc_text(text, exclude_metadata=False)] == [
        "[NeonFox_8]", "[女]我想起你", "[All my love]",
    ]
    assert [line.text for line in parse_lrc_text(text)] == ["[女]我想起你", "[All my love]"]


def test_song03_semantic_recall_rank_and_refill_use_measured_danmaku(monkeypatch, tmp_path):
    from scripts import session_autoslice as runner
    monkeypatch.setattr(runner, "MAX_SONGS_PER_DATE", 1)
    xml = tmp_path / "danmaku.xml"
    xml.write_text("<i>" + "".join(f'<d p="{s},1,25,1,1,0,u,1">弹幕</d>'
        for s, count in [(10, 128), (30, 174), (50, 100)] for _ in range(count)) + "</i>")
    songs = [{"cid": name, "anchor_start_ms": start, "anchor_end_ms": start + 20000,
              "danmaku": runner.danmaku_count_in(str(xml), start, start + 20000),
              "confidence": confidence, "lane": "semantic_recall", "session_id": "one-session"}
             for name, start, confidence in [("失眠飞行", 0, .99), ("快乐手帐", 20000, .7), ("下雨天", 40000, .95)]]
    assert [s["danmaku"] for s in songs] == [128, 174, 100]
    state = {"pending_song": songs, "song_backlog": [], "songs": []}
    runner.refill_songs(state)
    assert [s["cid"] for s in state["pending_song"]] == ["快乐手帐"]
    attempted = state["pending_song"].pop()
    state["songs"].append(dict(attempted, status="blocked", delivered=False))
    runner.refill_songs(state)
    assert [s["cid"] for s in state["pending_song"]] == ["失眠飞行"]
    assert [s["cid"] for s in state["song_backlog"]] == ["下雨天"]
