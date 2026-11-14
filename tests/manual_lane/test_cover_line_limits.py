"""The cover text length is held where the cover text is written (step 6, 70-cover), not found out at the renderer.

The punchline's most characters come from lane_rules `cover` (the feed-safe width divided by the punchline floor size);
the title step's own check fails a round whose punchline is longer, the rewrite prompt says why, and a too-long line that
still gets through goes down the existing "cover text too long" path (CPA shortens it) before anything is rendered."""

from __future__ import annotations

import json

from fakes import UNDERSTANDING_CALLS
from test_title_regen_and_blur import _cand, _ctx

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.lane_rules import rules
from src.autoslice.manual_lane.step_io import PROMPTS

COVER = rules("cover")


def _answer(lines):
    return json.dumps({"candidates": [_cand("【李豆沙】她半分钟后真的破防了")], "recommended": 0, "cover_lines": lines},
                      ensure_ascii=False)


def test_the_limits_are_derived_from_the_safe_width_and_the_text_floors_and_carry_their_basis():
    assert COVER["punch_line_max_chars"] == COVER["text_safe_width_px"] // COVER["punch_floor_px"] == 7
    assert COVER["first_line_max_chars"] == (COVER["text_safe_width_px"] - COVER["text_stagger_px"]) // COVER["first_line_min_px"]
    assert "punch_line_max_chars_source" in COVER and "first_line_max_chars_source" in COVER
    assert si.cover_line_limits() == (COVER["first_line_max_chars"], COVER["punch_line_max_chars"])


def test_the_overflow_sentence_names_the_line_and_its_limit_and_is_empty_when_all_fit():
    assert si.cover_lines_overflow(["情境一行", "七个字的笑点行"]) == ""
    assert "笑点行最多 7 字" in si.cover_lines_overflow(["情境", "这是八个字的笑点"])
    assert "第一行最多" in si.cover_lines_overflow(["长" * (COVER["first_line_max_chars"] + 1), "笑点"])
    assert si.cover_lines_overflow(["只有一行但是太长了啊啊"]) != ""        # a single line is the punchline


def test_a_title_round_with_a_too_long_punchline_fails_its_check_and_is_rewritten(tmp_path):
    ctx, cpa, _ = _ctx(tmp_path, [_answer(["情境", "这是八个字的笑点"]), _answer(["情境", "七个字的笑点行"])],
                       frames={"chosen": None, "checked": []})
    out = si.step6_title_cover(ctx)
    assert out.ok, (out.reason, out.note)
    assert cpa.calls[:len(UNDERSTANDING_CALLS) + 2] == [*UNDERSTANDING_CALLS, "title", "title-regen-1"]
    assert "封面字过长" in cpa.title_prompts[1] and "笑点行最多 7 字" in cpa.title_prompts[1]
    assert "封面字过长" not in cpa.title_prompts[0]
    cands = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert cands["cover_lines"] == ["情境", "七个字的笑点行"] and cands["forced"] is False


def test_a_punchline_within_the_limit_is_not_rewritten(tmp_path):
    ctx, cpa, _ = _ctx(tmp_path, [_answer(["情境", "七个字的笑点行"])], frames={"chosen": None, "checked": []})
    assert si.step6_title_cover(ctx).ok
    assert "title-regen-1" not in cpa.calls


def test_the_title_prompt_states_the_limits_before_anything_is_written(tmp_path):
    ctx, cpa, _ = _ctx(tmp_path, [_answer(["情境", "七个字的笑点行"])], frames={"chosen": None, "checked": []})
    assert si.step6_title_cover(ctx).ok
    assert "最多 7 个字" in cpa.title_prompts[0] and f"最多 {COVER['first_line_max_chars']} 个字" in cpa.title_prompts[0]
    assert "{punch_max}" not in (PROMPTS / "cover_retry.md").read_text(encoding="utf-8").replace("{punch_max}", "", 1)


def test_a_too_long_line_that_survives_the_rewrites_is_shortened_before_rendering(tmp_path):
    ctx, cpa, remote = _ctx(tmp_path, [_answer(["情境", "这是八个字的笑点"])] * 3, frames={"chosen": None, "checked": []})
    ask = cpa.ask

    def ask_with_retry(**kw):                 # the shortening call is answered by call id, whatever the title rounds took
        if kw["call_id"].startswith("title-cover-retry"):
            cpa._rows.append((kw["call_id"], kw["prompt_text"]))
            return {"call_id": kw["call_id"], "model": kw["model"], "effort": kw["effort"], "rc": 0, "elapsed_s": 0.1, "stderr_tail": "",
                    "response_text": json.dumps({"cover_lines": ["情境", "七个字的笑点行"]}, ensure_ascii=False)}
        return ask(**kw)
    cpa.ask = ask_with_retry
    out = si.step6_title_cover(ctx)
    assert out.ok, (out.reason, "|".join(cpa.calls))
    assert "title-cover-retry-1" in cpa.calls
    assert "笑点行（最后一行）最多 7 个字" in cpa.prompts[cpa.calls.index("title-cover-retry-1")]
    cands = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert cands["cover_lines"] == ["情境", "七个字的笑点行"]
    assert any("封面文字缩短（第 1 次）" in line for line in out.process)
    sent = [json.loads(remote.files[name].decode("utf-8"))["lines"] for name in remote.files
            if "job-" in name and name.endswith(".json") and b'"lines"' in remote.files[name]]
    assert sent and all("这是八个字的笑点" not in lines for lines in sent)      # the long line never reached a render job
