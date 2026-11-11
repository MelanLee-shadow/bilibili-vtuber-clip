"""Prompt rules that must stay in the adjudication prompt, and the witness parsing (danmaku timing, MOSS/MAI)."""

from __future__ import annotations

import json

from fakes import FakeRemote, make_ctx

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

# the prompt as CPA receives it under the default vote scheme (the scheme fills {supporters_doc} and {vote_rule})
PROMPT = si._render("subtitle_adjudicate.md")


def test_prompt_lets_cpa_choose_among_all_candidates_and_never_defaults_to_the_current_text():
    # U108: the current text is one candidate; compare evidence and select the most likely spoken words.
    assert "current 是现稿，也只是一个候选" in PROMPT
    assert "candidates 和 supporters 是来源证据摘要，不是封闭选择题或答案" in PROMPT
    assert "综合完整候选、实际读法与全片语境，选最可能的说法" in PROMPT
    assert "现稿只有比较后最符合证据时才选 KEEP" in PROMPT
    assert "现稿读得通、证据不足以改动时选现稿" not in PROMPT
    assert "不必按票数选择" in PROMPT and "against_maintained" in PROMPT
    assert "原文去首尾空白后是否完全相同" in PROMPT and "不是封闭答案" in PROMPT


def test_prompt_lists_the_four_outcomes_and_the_unresolved_condition():
    for state in ("KEEP", "ADOPT", "FIT_CONTEXT", "UNRESOLVED"):
        assert state in PROMPT
    assert "综合证据仍定不了时" in PROMPT and "confidence" in PROMPT and "低" in PROMPT
    assert "不要选 UNRESOLVED" in PROMPT and "affects_meaning" in PROMPT


def test_prompt_keeps_reviewer_rules_pronoun_numbers_and_danmaku_clue_only():
    assert si.PRONOUN_RULE in PROMPT
    assert "用名单辨认身份和核对听到的名字" in PROMPT
    assert "说简称或昵称则照实际叫法写，不补成未说出的全名" in PROMPT
    assert "参与者" in PROMPT and "{participants}" in PROMPT and "{confusions}" in PROMPT   # rendered per call
    assert "不得任意挑一个读法硬定" in PROMPT
    assert "danmaku：本场弹幕" in PROMPT and "只是线索" in PROMPT


def test_no_versioned_prompt_files_remain():
    names = sorted(p.name for p in si.PROMPTS.iterdir())
    assert names == ["boundary.md", "cover_brief.md", "cover_judge.md", "cover_retry.md", "import_meaning.md", "listen_blind.md", "listen_two_speakers.md", "name_consistency.md", "review.md", "speaker_review.md", "subtitle_adjudicate.md", "subtitle_review.md", "title.md"]


def test_danmaku_lines_are_shifted_onto_the_cut_timeline(tmp_path):
    # the clip starts at 10.0 s into the recording, so p=15.0 lands at cut time 5.0
    xml = ('<?xml version="1.0"?><i>'
           '<d p="15.0,1,25,1,0,0,0,0">早一句</d>'
           '<d p="60.0,1,25,1,0,0,0,0">中间&amp;句</d>'
           '<d p="200.0,1,25,1,0,0,0,0">太晚</d>'
           '</i>').encode()
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=None, danmaku_xml=xml)
    rows = si._danmaku_lines(ctx, cut_len=70.0)
    assert [r["text"] for r in rows] == ["早一句", "中间&句"]
    assert rows[0]["t_s"] == 5.0 and rows[1]["t_s"] == 50.0


def test_asr_witnesses_keep_ok_segments_and_record_unavailable_providers(tmp_path):
    def script(name, body, remote):
        remote.files["/r/clip/witness/out/moss.json"] = json.dumps({
            "status": "OK", "segment_count": 1,
            "segments": [{"start_ms": 1000, "end_ms": 2000, "text": "我是李豆沙"}]}, ensure_ascii=False).encode()
        remote.files["/r/clip/witness/out/mai.json"] = json.dumps({
            "status": "UNAVAILABLE", "reason_code": "MAI_CREDENTIAL_MISSING", "message": "no key"}).encode()
        return RemoteResult(0, "", "")

    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=script), cpa=None)
    lines, status = si._asr_witnesses(ctx)
    assert list(lines) == ["asr:moss"] and lines["asr:moss"][0]["abs_start_s"] == 1.0
    assert status["moss"]["status"] == "OK"
    assert status["mai"] == {"status": "UNAVAILABLE", "reason_code": "MAI_CREDENTIAL_MISSING", "message": "no key"}
    cue = {"start_s": 1.0, "end_s": 2.0}
    assert si._readings_for_cue(cue, lines) == {"asr:moss": "我是李豆沙"}


def test_listen_pass_reads_the_blind_prompt_under_its_new_name(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(on_script=lambda n, b, r: RemoteResult(1, "", "x")), cpa=None)
    windows = si.listen_windows([{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "a"}], 10.0)
    assert si._listen_pass(ctx, "L2", windows, [si.M38]) is None
    assert "/r/clip/listen/listen_blind.md" in ctx.adapters.remote.files
    assert not any("v1" in path for path in ctx.adapters.remote.files)
