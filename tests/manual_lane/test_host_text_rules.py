"""Subtitle rules preserve decided pronouns while retaining laughter and spelling rules."""

from __future__ import annotations

from src.autoslice.manual_lane import normalize
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import subtitle_track as st
from src.autoslice.manual_lane.step3_adjudicate import _final_cues
from src.autoslice.manual_lane.step_io import _render

from fakes import make_ctx, push_dir


def test_laughter_tokens_are_removed_and_whitespace_tidied():
    assert normalize.strip_laughter("有1吗？ 嗯？有！（笑）") == "有1吗？ 嗯？有！"
    assert normalize.strip_laughter("（笑） 我没有") == "我没有"
    assert normalize.strip_laughter("（笑）") == ""


def test_import_keeps_human_decided_pronouns(tmp_path):
    rows = [(0.0, 1.0, "李豆沙", "家长们知道吗"),
            (1.0, 2.0, "李豆沙", "他们不会过来看吗"),
            (2.0, 3.0, "李豆沙", "她已经离开了")]
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    si.record_import(ctx.state, ctx.clip_dir, push_dir(tmp_path, ctx.state.data["clip"], rows),
                     source="push-test", authority="human")
    assert si.step3_subtitles(ctx).ok
    final = st.parse_blocks((ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"))
    assert [cue["text"] for cue in final] == [row[3] for row in rows]


def test_generated_decisions_keep_cpa_pronouns_and_prompt_asks_for_referents():
    bcut = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "他们都知道"},
            {"id": 2, "start_s": 2.0, "end_s": 4.0, "text": "她已经走了"}]
    doubts = [{"id": "d1", "cue_id": 1, "state": "KEEP"},
              {"id": "d2", "cue_id": 2, "state": "KEEP"}]
    cues, _ = _final_cues(bcut, doubts, one_speaker=True)
    assert [cue["text"] for cue in cues] == ["他们都知道", "她已经走了"]
    prompt = _render("subtitle_adjudicate.md")
    assert si.PRONOUN_RULE in prompt


def test_expected_value_canon_unifies_the_spelling_of_a_name():
    text, hits = normalize.canon_text("kimo熊来了")
    assert text == "kmx来了" and hits
