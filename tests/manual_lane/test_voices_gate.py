"""A clip in which only the host is heard is not sent whole to the paid recognisers (voices.py, lane_rules
other_voices.whole_witness_only_with_others)."""
import json

import pytest

from fakes import FakeRemote, make_ctx
from src.autoslice.manual_lane import stage_impl as si, step4_speaker, voices
from src.autoslice.manual_lane.adapters import RemoteResult

real_others_heard = voices.others_heard       # the suite's default replaces it (conftest); these tests use the real one
SOLO = {"recording_date": "2026-10-24", "speaker_mode": "uniform_host", "enablement_evidence": None,
        "participants": [["主播"]], "own_uploader_mids": [1]}


def chunk(i, host_prob):
    return {"s": i * 1200, "e": (i + 1) * 1200, "host_prob": host_prob}


def ctx_with(tmp_path, monkeypatch, chunks, *, error="", session=None):
    """A clip whose voiceprint run gives `chunks`; returns (ctx, the names of the remote scripts that ran)."""
    ran = []

    def script(name, body, remote):
        ran.append(name)
        remote.files["/r/clip/witness/out/moss.json"] = json.dumps(
            {"status": "OK", "segment_count": 1, "segments": [{"start_ms": 1000, "end_ms": 2000, "text": "一句"}]},
            ensure_ascii=False).encode()
        remote.files["/r/clip/witness/out/mai.json"] = json.dumps({"status": "OK", "segment_count": 0, "segments": []}).encode()
        return RemoteResult(0, "", "")

    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=script), cpa=None, session=session or SOLO)
    asr = ctx.clip_dir / "asr"
    asr.mkdir(parents=True, exist_ok=True)
    (asr / "bcut.srt").write_text("1\n00:00:01,000 --> 00:00:03,000\n一句\n", encoding="utf-8")
    monkeypatch.setattr(voices, "others_heard", real_others_heard)
    monkeypatch.setattr(step4_speaker, "voice_chunks", lambda _ctx, _cues: (chunks, error))
    monkeypatch.delenv("MANUAL_LANE_WHOLE_WITNESS", raising=False)
    return ctx, ran


def test_share_and_longest_run_are_measured_over_the_clip_in_time_order():
    chunks = {2: [chunk(3, 0.1), chunk(4, 0.95)], 1: [chunk(0, 0.9), chunk(1, 0.2), chunk(2, 0.3)]}
    share, run = voices.measure(chunks)
    assert share == pytest.approx(3 / 5) and run == 3        # chunks 1, 2 (cue 1) and 3 (cue 2) follow one another
    assert voices.measure({}) == (0.0, 0)


def test_a_clip_with_only_the_host_is_not_sent_to_the_whole_clip_recognisers(tmp_path, monkeypatch):
    ctx, ran = ctx_with(tmp_path, monkeypatch, {1: [chunk(i, 0.97) for i in range(40)] + [chunk(40, 0.2)]})
    lines, status = si._asr_witnesses(ctx)
    assert lines == {} and "s3-witness" not in ran
    assert {n: status[n]["reason_code"] for n in ("mai", "moss")} == {"mai": "ONLY_HOST_HEARD", "moss": "ONLY_HOST_HEARD"}
    assert "只听到主播" in si.witness_status_line(status)
    kept = json.loads((ctx.clip_dir / "subs/voices.json").read_text(encoding="utf-8"))
    assert kept["others"] is False and kept["longest_run"] == 1


@pytest.mark.parametrize("chunks", [
    {1: [chunk(i, 0.97) for i in range(40)] + [chunk(40 + i, 0.1) for i in range(4)]},      # a stretch of another voice
    {1: [chunk(i, 0.1 if i % 2 else 0.97) for i in range(40)]},                             # half the clip, never two in a row
])
def test_another_voice_keeps_the_whole_clip_recognisers(tmp_path, monkeypatch, chunks):
    ctx, ran = ctx_with(tmp_path, monkeypatch, chunks)
    lines, status = si._asr_witnesses(ctx)
    assert "s3-witness" in ran and status["moss"]["status"] == "OK" and "asr:moss" in lines
    assert json.loads((ctx.clip_dir / "subs/voices.json").read_text(encoding="utf-8"))["others"] is True


def test_no_answer_keeps_what_was_configured(tmp_path, monkeypatch):
    ctx, ran = ctx_with(tmp_path, monkeypatch, {}, error="声纹分段失败")
    _lines, status = si._asr_witnesses(ctx)
    assert "s3-witness" in ran and status["moss"]["status"] == "OK"
    assert json.loads((ctx.clip_dir / "subs/voices.json").read_text(encoding="utf-8"))["others"] is None


def test_a_linked_clip_is_not_asked_and_an_explicit_choice_is_run_as_given(tmp_path, monkeypatch):
    linked = dict(SOLO, participants=[["主播"], ["嘉宾"]])
    ctx, ran = ctx_with(tmp_path / "linked", monkeypatch, {1: [chunk(0, 0.99)]}, session=linked)
    monkeypatch.setattr(step4_speaker, "voice_chunks", lambda *_: pytest.fail("a linked clip needs no voiceprint question"))
    si._asr_witnesses(ctx)
    assert "s3-witness" in ran
    ctx, ran = ctx_with(tmp_path / "explicit", monkeypatch, {1: [chunk(i, 0.99) for i in range(40)]})
    monkeypatch.setenv("MANUAL_LANE_WHOLE_WITNESS", "moss")
    monkeypatch.setattr(step4_speaker, "voice_chunks", lambda *_: pytest.fail("an explicit choice is not second-guessed"))
    _lines, status = si._asr_witnesses(ctx)
    assert "s3-witness" in ran and status["mai"]["reason_code"] == "WHOLE_CLIP_WITNESS_OFF"
