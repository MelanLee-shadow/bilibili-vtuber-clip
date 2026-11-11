"""One word, one written form (writing_canon): the table's non-canonical writings are rewritten to the canonical one
wherever they are the same word, in the final subtitles (an import too), the adopted title and the cover text; a
different name of the same object is kept; the package step refuses a hit that has no verdict.  Every case runs on
fakes; no test reaches the network."""

from __future__ import annotations

import json

import pytest
from fakes import FakeCpa, FakeRemote, make_ctx, push_dir, writing_canon_answer
from test_title_approved import _answer, _asset, _cand
from test_trim_run import _text, answer, make_x, run

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane import writing_canon
from src.autoslice.manual_lane import subtitle_track as st
from src.autoslice.manual_lane.runner import StepOutcome

CASE_ONLY = "我最喜欢Mujica的歌"
SOUND_ALIKE = "母鸡卡真的好听"
OTHER_NAME = "星汐seki今天来了"
ROWS = [(0.0, 2.0, "李豆沙", CASE_ONLY), (2.0, 4.5, "李豆沙", SOUND_ALIKE), (4.5, 7.0, "李豆沙", OTHER_NAME)]
CANONICAL = ["我最喜欢mujica的歌", "mujica真的好听", OTHER_NAME]


def _same(*written):
    return lambda prompt: writing_canon_answer(prompt, set(written))


def _imported(tmp_path, *, owner=False, same=("母鸡卡",), rows=ROWS, writing_canon=None):
    cpa = FakeCpa([], writing_canon=writing_canon or _same(*same))
    ctx = make_ctx(tmp_path, remote=None, cpa=cpa)
    si.record_import(ctx.state, ctx.clip_dir, push_dir(tmp_path, ctx.state.data["clip"], rows), source="push-test",
                     authority="human", decided_by="owner" if owner else None)
    return ctx, cpa


def _final(ctx):
    return st.parse_blocks((ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"))


def _asked(cpa):
    return [c for c in cpa.calls if c.startswith("writing-canon")]


# ----- 1. the scan --------------------------------------------------------------------------------------------------
def test_find_prefers_the_longest_writing_without_overlap_and_skips_canonical_aliases_and_rejected_writings():
    hits = writing_canon.find("星汐seki来了，母鸡卡也来了")
    assert [(h.row, h.written, h.canonical) for h in hits] == [("xxsk", "星汐seki", "xxsk"),
                                                               ("mujica", "母鸡卡", "mujica")]
    assert [(h.start, h.end) for h in hits] == [(0, 6), (9, 12)]
    assert [h.written for h in writing_canon.find("星汐来了")] == ["星汐"]
    assert writing_canon.find("mujica 少女歌剧 豆沙 小李 Shadow Lee lds 天不熊") == []     # canonical, alias, rejected
    assert [h.written for h in writing_canon.find("少女歌来了")] == ["少女歌"]            # only where it is not the canonical


def test_find_matches_a_latin_writing_only_as_a_whole_word_and_knows_case_only():
    assert [h.written for h in writing_canon.find("more ore before boku")] == ["ore", "boku"]
    assert [h.written for h in writing_canon.find("moreore beforeboku")] == []
    case, sound = writing_canon.find("Mujica 母鸡卡")
    assert case.case_only and not sound.case_only
    assert writing_canon.canonical_form("母鸡卡和Mujica") == "mujica和mujica"


# ----- 2/3. step 3 --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("owner", [False, True])
def test_an_import_writes_the_same_word_canonically_and_keeps_another_name(tmp_path, owner):
    ctx, cpa = _imported(tmp_path, owner=owner)
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    final = _final(ctx)
    assert [c["text"] for c in final] == CANONICAL                                         # the words only
    assert [(c["start_s"], c["end_s"]) for c in final] == [(a, b) for a, b, _s, _t in ROWS]
    assert len(final) == len(ROWS)
    assert _asked(cpa) == ["writing-canon"]
    prompt = next(p for c, p in zip(cpa.calls, cpa.prompts) if c == "writing-canon")
    asked = json.loads(prompt.split("待判的处：\n", 1)[1])
    assert [r["写法"] for r in asked] == ["母鸡卡", "星汐seki"]                       # the case-only writing is not asked
    assert asked[0]["前两句"] == [CASE_ONLY] and asked[0]["后两句"] == [OTHER_NAME] and asked[1]["条件"]
    record = json.loads((ctx.clip_dir / "subs/writing_canon.json").read_text(encoding="utf-8"))
    assert [(c["cue"], c["written"], c["canonical"], c["verdict"], c["basis"]) for c in record["conclusions"]] == [
        (1, "Mujica", "mujica", "rewritten", "case_only"), (2, "母鸡卡", "mujica", "rewritten", "model"),
        (3, "星汐seki", "xxsk", "kept", "model")]
    assert all(c["where"] == "subtitle" and c["why"] and c["sentence"] for c in record["conclusions"])
    assert out.products["writing_canon"] == str(ctx.clip_dir / "subs/writing_canon.json")
    assert "规范写法：改 2 处（Mujica→mujica（句1）；母鸡卡→mujica（句2）），保留 1 处" in out.process
    assert out.cpa and out.cpa[-1]["purpose"] == "writing-canon" and out.cpa[-1]["effort"] == "high"


def test_the_rewrite_changes_only_the_word_the_sentence_split_and_speakers_stay(tmp_path):
    rows = [(0.0, 3.0, "李豆沙", "母鸡卡和母鸡卡都好听"), (3.0, 5.0, "李豆沙", "没有写法")]
    ctx, _cpa = _imported(tmp_path, rows=rows)
    assert si.step3_subtitles(ctx).ok
    assert [c["text"] for c in _final(ctx)] == ["mujica和mujica都好听", "没有写法"]
    sidecar = json.loads((ctx.clip_dir / "subs/import_speakers.json").read_text(encoding="utf-8"))
    assert sidecar == ["李豆沙", "李豆沙"]


def test_no_hit_to_ask_means_no_model_call_and_a_failed_call_fails_the_step(tmp_path):
    ctx, cpa = _imported(tmp_path, rows=[(0.0, 2.0, "李豆沙", "没有写法"), (2.0, 4.0, "李豆沙", "也没有")])
    out = si.step3_subtitles(ctx)
    assert out.ok and cpa.calls == [] and out.cpa == []
    assert "规范写法：改 0 处，保留 0 处" in out.process
    only_case = [(0.0, 2.0, "李豆沙", CASE_ONLY)]
    ctx, cpa = _imported(tmp_path / "case", rows=only_case)
    assert si.step3_subtitles(ctx).ok and cpa.calls == [] and _final(ctx)[0]["text"] == "我最喜欢mujica的歌"
    for n, answer in enumerate((lambda prompt: None, lambda prompt: "[]", lambda prompt: "不是 JSON")):
        failing, _ = _imported(tmp_path / f"fail{n}", writing_canon=answer)
        out = si.step3_subtitles(failing)
        assert out.ok is False and out.reason == "WRITING_CANON_FAILED"


def test_trim_hands_the_verdicts_of_the_original_on_to_its_successor(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6), (12, 12)), answer((5, 6), (12, 12))]))
    one = {"where": "subtitle", "row": "xxsk", "written": "星汐seki", "canonical": "xxsk", "verdict": "kept",
           "basis": "model", "why": "全名"}
    writing_canon.save(ctx.clip_dir, [dict(one, cue=8, sentence=_text(8)), dict(one, cue=5, sentence=_text(5))])
    assert run(ctx)["status"] == "TRIMMED"
    mine = writing_canon.load(tmp_path / "clips/clip_trim")
    assert [(c["cue"], c["sentence"]) for c in mine] == [(6, _text(8))]                # cue 5 was cut out, 8 is now 6
    assert (tmp_path / "clips/clip_trim/import/writing_canon.json").is_file()


def test_a_verbatim_push_takes_over_the_verdicts_of_its_source_and_asks_nothing(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=FakeCpa([]))
    rows = [(0.0, 2.0, "李豆沙", "先说点别的"), (2.0, 4.0, "李豆沙", OTHER_NAME)]
    push = push_dir(tmp_path, ctx.state.data["clip"], rows)
    source = [{"where": "subtitle", "cue": 7, "row": "xxsk", "written": "星汐seki", "canonical": "xxsk",
               "verdict": "kept", "basis": "model", "why": "全名", "sentence": OTHER_NAME},
              {"where": "subtitle", "cue": 9, "row": "x", "written": "x", "canonical": "y", "verdict": "kept",
               "basis": "model", "why": "w", "sentence": "这句已经不在本片"}]
    writing_canon.save(push.parent / "src", source)
    (push / "subs").mkdir(exist_ok=True)
    (push / "subs/writing_canon.json").write_bytes((push.parent / "src/subs/writing_canon.json").read_bytes())
    si.record_import(ctx.state, ctx.clip_dir, push, source="trim", authority="trim", verbatim=True)
    out = si.step3_subtitles(ctx)
    assert out.ok and ctx.adapters.cpa.calls == []
    record = writing_canon.load(ctx.clip_dir)
    assert [(c["cue"], c["written"], c["verdict"], c.get("inherited")) for c in record] == [(2, "星汐seki", "kept", True)]
    assert any("沿用原片结论" in line for line in out.process)


# ----- line breaks: writings are found on the sentence without its display lines ---------------------------------------
def _settled(tmp_path, texts):
    """Step 3's settle on a final.srt of display-line sentences (4 s each): (outcome, parsed cues, raw file text)."""
    from src.autoslice.manual_lane.runner import StepOutcome

    ctx = make_ctx(tmp_path, remote=None, cpa=FakeCpa([], writing_canon=_same("母鸡卡")))
    cues = [{"start_s": 4.0 * i, "end_s": 4.0 * i + 4.0, "text": t, "speaker": None} for i, t in enumerate(texts)]
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(st.format_blocks(cues, labels=False), encoding="utf-8")
    out = writing_canon.settle_step3(ctx, StepOutcome(True), verbatim=False)
    return out, _final(ctx), (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")


def test_join_lines_puts_a_space_only_between_two_ascii_alphanumerics():
    assert writing_canon.join_lines("母鸡\n卡") == "母鸡卡"
    assert writing_canon.join_lines("hello\nworld") == "hello world"
    assert writing_canon.join_lines("星汐\nseki") == "星汐seki" and writing_canon.join_lines("seki\n来了") == "seki来了"
    assert writing_canon.join_lines("单行") == "单行"


def test_a_writing_split_by_a_line_break_is_found_and_written_canonically_without_splitting_the_word(tmp_path):
    out, cues, _raw = _settled(tmp_path, ["我最喜欢母鸡\n卡的歌"])
    assert out.ok, (out.reason, out.note)
    assert writing_canon.join_lines(cues[0]["text"]) == "我最喜欢mujica的歌" and "mujica" in cues[0]["text"]
    assert len(cues) == 1 and (cues[0]["start_s"], cues[0]["end_s"]) == (0.0, 4.0)
    assert [(c["cue"], c["written"], c["verdict"]) for c in writing_canon.load(tmp_path / "clips/clip")] == [
        (1, "母鸡卡", "rewritten")]


def test_a_longer_canonical_writing_is_wrapped_again_within_the_line_limit(tmp_path):
    from src.autoslice.manual_lane import normalize

    audit = normalize._audit()
    text = "一二三四五六七八九十一二三四母鸡卡"
    assert len(text) <= audit.DEFAULT_MAX_VISUAL_LINE_CHARS < len(text.replace("母鸡卡", "mujica"))
    out, cues, _raw = _settled(tmp_path, [text])
    assert out.ok, (out.reason, out.note)
    lines = cues[0]["text"].split("\n")
    assert 2 <= len(lines) <= audit.DEFAULT_MAX_VISUAL_LINES
    assert all(len(line) <= audit.DEFAULT_MAX_VISUAL_LINE_CHARS for line in lines)
    assert "".join(lines) == "一二三四五六七八九十一二三四mujica" and "mujica" in "".join(lines)
    assert (cues[0]["start_s"], cues[0]["end_s"]) == (0.0, 4.0)


def test_a_sentence_that_is_not_rewritten_keeps_its_bytes_and_its_lines(tmp_path):
    _out, _cues, before = _settled(tmp_path / "a", ["第一行\n第二行", "没有写法"])
    _out, cues, after = _settled(tmp_path / "b", ["第一行\n第二行", "母鸡卡来了"])
    assert cues[0]["text"] == "第一行\n第二行" and cues[1]["text"] == "mujica来了"
    assert after.split("\n\n")[0] == before.split("\n\n")[0]
    unchanged, _c, raw = _settled(tmp_path / "c", ["第一行\n第二行", "没有写法"])
    assert raw == before                                         # nothing rewritten: the file is not even rewritten


def test_a_rewrite_that_cannot_be_wrapped_fails_the_step_and_names_the_sentence(tmp_path):
    long = "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十母鸡卡"
    out, _cues, _raw = _settled(tmp_path, ["没有写法", long])
    assert out.ok is False and out.reason == "WRITING_CANON_FAILED" and "句2" in out.note


# ----- 4. title and cover text ---------------------------------------------------------------------------------------
def _subtitle_verdicts(ctx, *conclusions):
    writing_canon.save(ctx.clip_dir, [dict({"where": "subtitle", "cue": 1, "why": "w", "sentence": "s"}, **c)
                                      for c in conclusions])


def test_a_cover_text_takes_over_the_verdict_the_subtitles_already_have(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch, approved=False)
    cpa = FakeCpa([json.dumps({"candidates": [_cand("她半分钟后真的破防了"), _cand("这一段她越讲越心虚了")], "recommended": 0,
                               "cover_lines": ["母鸡卡破防", "半分钟"]}, ensure_ascii=False)])
    ctx = make_ctx(tmp_path / "w", remote=FakeRemote(), cpa=cpa)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nmujica真好\n\n2\n00:00:02,000 --> 00:00:04,000\n句子二\n", encoding="utf-8")
    _subtitle_verdicts(ctx, {"row": "mujica", "written": "母鸡卡", "canonical": "mujica", "verdict": "rewritten",
                             "basis": "model"})
    picked = si.pick_title(ctx)
    assert not isinstance(picked, StepOutcome), picked
    assert picked[1] == ["mujica破防", "半分钟"] and _asked(cpa) == []                 # settled with no new call
    record = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert record["cover_lines"] == ["mujica破防", "半分钟"]
    mine = [c for c in writing_canon.load(ctx.clip_dir) if c["where"] == "cover"]
    assert [(c["cue"], c["written"], c["verdict"], c.get("reused")) for c in mine] == [(1, "母鸡卡", "rewritten", True)]
    assert any(line.startswith("规范写法（标题与封面字）：改 1 处") for line in picked[2])


def test_a_new_hit_in_the_title_or_cover_is_asked_once_and_a_kept_verdict_stands(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch, approved=False)
    cpa = FakeCpa([json.dumps({"candidates": [_cand("她半分钟后真的破防了"), _cand("这一段她越讲越心虚了")], "recommended": 0,
                               "cover_lines": ["星汐seki破防", "半分钟"]}, ensure_ascii=False)])
    ctx = make_ctx(tmp_path / "w", remote=FakeRemote(), cpa=cpa)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\n句子一\n", encoding="utf-8")
    picked = si.pick_title(ctx)
    assert not isinstance(picked, StepOutcome), picked
    assert _asked(cpa) == ["writing-canon-title"] and picked[1] == ["星汐seki破防", "半分钟"]
    assert [(c["where"], c["verdict"]) for c in writing_canon.load(ctx.clip_dir)] == [("cover", "kept")]


def test_the_owners_picked_title_body_is_written_canonically_and_still_counts_as_written_out(tmp_path, monkeypatch):
    body = "她被母鸡卡的歌弄破防了"
    _asset(tmp_path, monkeypatch, approved_title_body=body)
    for model_writes in (body, "她被mujica的歌弄破防了"):
        cpa = FakeCpa([_answer(_cand("这一段她越讲越心虚了"), _cand(model_writes))], writing_canon=_same("母鸡卡"))
        ctx = make_ctx(tmp_path / model_writes, remote=FakeRemote(), cpa=cpa)
        (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
        (ctx.clip_dir / "subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\n句子一\n", encoding="utf-8")
        picked = si.pick_title(ctx)
        assert not isinstance(picked, StepOutcome), (picked.reason, picked.note)
        assert picked[0] == "【李豆沙】她被mujica的歌弄破防了"
        assert _asked(cpa) == ["writing-canon-approved"]                      # the title took over the owner-body verdict
        verdicts = [(c["where"], c["verdict"]) for c in writing_canon.load(ctx.clip_dir)]
        assert verdicts == [("approved_title", "rewritten")] + ([("title", "rewritten")] if model_writes == body else [])
        assert (ctx.clip_dir / "title/title.txt").read_text(encoding="utf-8").strip() == picked[0]


def test_a_failed_writing_call_in_step_6_stops_with_its_own_reason(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch, approved=False)
    cpa = FakeCpa([json.dumps({"candidates": [_cand("她半分钟后真的破防了")], "recommended": 0,
                               "cover_lines": ["星汐seki破防", "半分钟"]}, ensure_ascii=False)],
                  writing_canon=lambda prompt: None)
    ctx = make_ctx(tmp_path / "w", remote=FakeRemote(), cpa=cpa)
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\n句子一\n", encoding="utf-8")
    picked = si.pick_title(ctx)
    assert isinstance(picked, StepOutcome) and picked.reason == "WRITING_CANON_FAILED"


# ----- 5. the package step ------------------------------------------------------------------------------------------
def _package_ctx(tmp_path, final_text, *, title="【李豆沙】她破防了", cover=("破防了",)):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]))
    for rel in ("title/title.txt", "cover/cover.png", "tags/tags.json", "burn/intro_result.json", "dedup/result.json",
                "cut/probe.json"):
        (ctx.clip_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (ctx.clip_dir / rel).write_text("{}", encoding="utf-8")
    (ctx.clip_dir / "title/title.txt").write_text(title + "\n", encoding="utf-8")
    (ctx.clip_dir / "title/candidates.json").write_text(json.dumps({"cover_lines": list(cover)}, ensure_ascii=False),
                                                        encoding="utf-8")
    srt = f"1\n00:00:00,000 --> 00:00:02,000\n{final_text}\n"
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt, encoding="utf-8")
    (ctx.clip_dir / "speaker").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "speaker/speaker.srt").write_text(srt.replace(final_text, f"[李豆沙] {final_text}"), encoding="utf-8")
    return ctx


def test_step8_refuses_a_writing_that_was_canonical_and_went_back(tmp_path):
    ctx = _package_ctx(tmp_path, "我最喜欢mujica的歌")
    assert writing_canon.not_canonical(ctx) == ""
    (ctx.clip_dir / "subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\n我最喜欢母鸡卡的歌\n", encoding="utf-8")
    out = sp.step8_package(ctx)
    assert out.ok is False and out.reason == "WRITING_NOT_CANONICAL"
    assert "subs/final.srt 句1：母鸡卡（应为 mujica）" in out.note and "speaker.srt" not in out.note
    assert ctx.adapters.remote.files == {}                                          # refused before any package work


def test_step8_checks_the_title_and_the_cover_text_too(tmp_path):
    ctx = _package_ctx(tmp_path, "没有写法", title="【李豆沙】母鸡卡来了", cover=("磕cp了", "半分钟"))
    out = sp.step8_package(ctx)
    assert out.reason == "WRITING_NOT_CANONICAL"
    assert "标题：母鸡卡" in out.note and "封面字第1行：磕（应为 嗑）" in out.note


def test_step8_lets_a_kept_verdict_through_only_for_that_sentence(tmp_path):
    ctx = _package_ctx(tmp_path, OTHER_NAME)
    assert "星汐seki" in writing_canon.not_canonical(ctx)
    kept = {"where": "subtitle", "cue": 1, "row": "xxsk", "written": "星汐seki", "canonical": "xxsk", "verdict": "kept",
            "basis": "model", "why": "全名", "sentence": OTHER_NAME}
    writing_canon.save(ctx.clip_dir, [kept])
    assert writing_canon.not_canonical(ctx) == ""
    with pytest.raises(RuntimeError, match="burn/final.mp4"):                       # past the guard: the first remote fetch
        sp.step8_package(ctx)
    other = "星汐seki今天没来"
    (ctx.clip_dir / "subs/final.srt").write_text(f"1\n00:00:00,000 --> 00:00:02,000\n{other}\n", encoding="utf-8")
    assert "星汐seki" in writing_canon.not_canonical(ctx)                           # another sentence has no verdict



def test_step8_finds_a_non_canonical_writing_that_a_line_break_split(tmp_path):
    ctx = _package_ctx(tmp_path, "我最喜欢母鸡\n卡的歌")
    out = sp.step8_package(ctx)
    assert out.ok is False and out.reason == "WRITING_NOT_CANONICAL"
    assert "subs/final.srt 句1：母鸡卡（应为 mujica）" in out.note and "speaker/speaker.srt 句1" in out.note
