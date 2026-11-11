"""TASK-10 problem 4 (U136, 维护者): Arabic digits for the yuri 0/1 roles, a 0-1 score and a quantity with a
time or measure unit; characters for classifiers and fixed phrases.  One definition (normalize.numerals), used by
every writer of the subtitles: step 3's assembly, the import, a review fix.  The examples are generic sentences."""

from __future__ import annotations

import pytest

from fakes import make_ctx
from fakes import push_dir as _push

from src.autoslice.manual_lane import normalize
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane import subtitle_track as st

ROLE_TALK = [(0.0, 1.5, "李豆沙", "我们来聊聊攻受"), (1.6, 3.0, "雾深Girimi", "那你是一吗"),
             (3.1, 4.5, "李豆沙", "我当一还是零都行"), (4.6, 6.0, "李豆沙", "再等五分钟"),
             (6.1, 7.5, "李豆沙", "打分零点五"), (7.6, 9.0, "李豆沙", "这是一种感觉，下一题")]


def _texts(ctx):
    return [c["text"] for c in st.parse_blocks((ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"))]


def test_the_import_writes_the_numbers_by_reviewers_rule(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    si.record_import(ctx.state, ctx.clip_dir, _push(tmp_path, ctx.state.data["clip"], ROLE_TALK), source="push-test",
                     authority="U130")
    assert si.step3_subtitles(ctx).ok
    assert _texts(ctx) == ["我们来聊聊攻受", "那你是1吗", "我当1还是0都行", "再等5分钟", "打分0.5", "这是一种感觉，下一题"]


def test_step_3_assembly_writes_the_numbers_by_the_same_rule():
    bcut = [{"id": i, "start_s": a, "end_s": b, "text": t} for i, (a, b, _, t) in enumerate(ROLE_TALK, 1)]
    doubts = [{"id": f"d{i}", "cue_id": i, "state": "KEEP", "final_text": t} for i, (_, _, _, t) in enumerate(ROLE_TALK, 1)]
    cues, log = si._final_cues(bcut, doubts)
    assert [c["text"] for c in cues][1:5] == ["那你是1吗", "我当1还是0都行", "再等5分钟", "打分0.5"]
    assert {h["kind"] for h in log["numerals"]} == {"role", "quantity", "score"}


def test_a_review_fix_is_written_by_the_same_rule_with_the_whole_track_as_context(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    track = [{"start_s": 0.0, "end_s": 1.5, "text": "我们来聊聊攻受", "speaker": "李豆沙"},
             {"start_s": 1.6, "end_s": 3.0, "text": "你是哪个", "speaker": "连线"}]
    st.write_track(ctx.clip_dir, track, speakers=True)
    (ctx.clip_dir / "subs/doubts.json").write_text("[]", encoding="utf-8")
    (ctx.clip_dir / "title").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "title/title.txt").write_text("【李豆沙】标题\n", encoding="utf-8")
    sp.apply_fixes(ctx, {"fixes": {"subtitles": [{"cue_id": 2, "text": "你是零吗，等三秒"}]}})
    assert _texts(ctx)[1] == "你是0吗，等3秒"


def test_without_a_role_topic_and_for_fixed_phrases_the_characters_stay():
    plain = ["她是一个好人", "有一天下雨", "两位来了", "对半折", "一下子就懂了", "第一天", "《一千零一夜》"]
    out, hits = normalize.numeral_cues([{"text": t} for t in plain + ["你是一吗"]])
    assert [c["text"] for c in out] == plain + ["你是一吗"] and hits == []      # no 0/1 topic in these lines
    assert normalize.numerals("买了一千克，跑了一点五小时，十二岁")[0] == "买了1千克，跑了1.5小时，12岁"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("周六周一纸鸢在窗边", "周六周一纸鸢在窗边"),
        ("星期五星期日一起看云", "星期五星期日一起看云"),
        ("礼拜四周二都有雨", "礼拜四周二都有雨"),
        ("周五两周后再出发", "周五2周后再出发"),
        ("星期二三周后再见", "星期二3周后再见"),
        ("三周五天，四星期后", "3周5天，4星期后"),
        ("三周五天和周五礼拜天", "3周5天和周五礼拜天"),
        ("《周二周三》再等五分钟", "《周二周三》再等5分钟"),
    ],
)
def test_weekday_atoms_do_not_cross_quantity_boundaries(text, expected):
    assert normalize.numerals(text)[0] == expected


def test_review_writer_keeps_adjacent_weekdays(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    st.write_track(ctx.clip_dir, [{"start_s": 0.0, "end_s": 2.0, "text": "旧句子", "speaker": "李豆沙"}],
                   speakers=True)
    (ctx.clip_dir / "subs/doubts.json").write_text("[]", encoding="utf-8")
    (ctx.clip_dir / "title").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "title/title.txt").write_text("【李豆沙】标题\n", encoding="utf-8")
    applied = sp.apply_fixes(ctx, {"fixes": {"subtitles": [{"cue_id": 1, "text": "星期五周日纸舟起航"}]}})
    assert applied["subtitles"] == 1
    assert _texts(ctx) == ["星期五周日纸舟起航"]
    assert "星期五周日纸舟起航" in (ctx.clip_dir / "speaker/speaker.srt").read_text(encoding="utf-8")


def test_the_scorer_still_treats_both_spellings_alike():
    from src.autoslice.manual_lane import score

    assert score.normalize("你是1吗，打分0.5") == score.normalize("你是一吗，打分零.五")
