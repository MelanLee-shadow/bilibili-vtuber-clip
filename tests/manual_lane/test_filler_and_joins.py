"""Filler answers are speech; laughter is not (U112).  A repeat is removed only where two cues touch or overlap (the same
speech recorded twice), never across a real pause.  Shared by step 3's modes A, B, C."""

from __future__ import annotations

from fakes import srt
from test_steps import _subs_ctx

from src.autoslice.manual_lane import doubt_policy as dp
from src.autoslice.manual_lane import normalize as nz
from src.autoslice.manual_lane import stage_impl as si


def test_filler_words_are_speech_and_laughter_is_not():
    assert dp.voiced("嗯") and dp.voiced("啊") and dp.voiced("嗯，对") and dp.voiced("真的吗？ 啊。")
    assert not dp.voiced("哈哈哈哈哈哈") and not dp.voiced("（笑）") and not dp.voiced("") and not dp.voiced(None)


def test_a_filler_answer_with_no_witness_is_sent_to_cpa_in_full_mode(tmp_path):
    filler = srt(3, step=2.0).replace("句子2", "嗯").encode()
    ctx, cpa = _subs_ctx(tmp_path, [], [])
    ctx.adapters.remote.files["/r/clip/asr/bcut.srt"] = filler
    ctx.adapters.remote.files["/r/clip/asr/jianying.srt"] = filler
    out = si.step3_subtitles(ctx)
    assert out.ok, out.note
    assert "嗯" in (ctx.clip_dir / "subs" / "final.srt").read_text(encoding="utf-8")
    assert any('"d002"' in p for p in cpa.prompts)


def test_a_laughter_only_cue_is_not_subtitled(tmp_path):
    laugh = srt(3, step=2.0).replace("句子2", "哈哈哈哈").encode()
    ctx, _ = _subs_ctx(tmp_path, [], [])
    ctx.adapters.remote.files["/r/clip/asr/bcut.srt"] = laugh
    ctx.adapters.remote.files["/r/clip/asr/jianying.srt"] = laugh
    assert si.step3_subtitles(ctx).ok
    assert "哈哈" not in (ctx.clip_dir / "subs" / "final.srt").read_text(encoding="utf-8")


def _cue(i, start, end, text):
    return {"id": i, "start_s": start, "end_s": end, "text": text}


def test_the_same_sentence_after_a_real_pause_is_kept_twice():
    cues, log = nz.normalize_cues([_cue(1, 0.0, 1.0, "真的吗"), _cue(2, 2.0, 3.0, "真的吗")])
    assert [c["text"] for c in cues].count("真的吗") == 2
    assert log["adjacent_repeats"] == []


def test_a_repeat_at_a_touching_join_is_removed():
    cues, log = nz.normalize_cues([_cue(1, 0.0, 1.0, "今天我们走"), _cue(2, 1.1, 2.5, "我们走吧好")])
    assert log["adjacent_repeats"] and log["adjacent_repeats"][0]["kind"] == "tail"   # 我们走 is the join


def test_a_repeat_in_an_overlap_is_removed():
    cues, log = nz.normalize_cues([_cue(1, 0.0, 1.0, "今天我们走"), _cue(2, 0.8, 2.5, "我们走吧好")])
    assert log["adjacent_repeats"], "overlapping cues are one speech recorded twice"


def test_the_join_tolerance_is_the_asr_window_not_a_guess():
    assert nz.ADJACENT_JOIN_S == 0.3
