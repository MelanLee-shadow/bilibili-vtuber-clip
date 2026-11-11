"""TASK-10 problem 1: no isolated content character on its own subtitle line (80-package-delivery 739-741,
lidousha-srt-release-policy.v1).  The release check decides which cues are lone characters (its standalone set and
the name-echo exemption are inherited); every writer joins such a cue into the touching cue of the same speaker, never
across a silence and never into another speaker's line.  TASK-11 (U138): one that cannot be joined is not subtitled.  Guards go through the ordinary writers: the import, step 4's
labels on a linked clip (run_clip on fakes) and a review fix."""

from __future__ import annotations

import json

from fakes import make_ctx, srt
from fakes import push_dir as _push
from test_pipeline_fake_e2e import ScriptedCpa, _fake_fmr, _remote
from test_review_once import BOUNDARY, TITLE, _review

from src.autoslice.manual_lane import normalize, speaker_v8
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane import subtitle_track as st
from src.autoslice.manual_lane.runner import run_clip
from src.autoslice.manual_lane.stages import STEPS
from src.autoslice.subtitle_validation import validate_srt_text

GUEST, HOST = speaker_v8.GUEST_LABEL, speaker_v8.HOST


def _lone_blocks(text: str) -> list[int]:
    return [e["block"] for e in validate_srt_text(text)["errors"] if e["code"] == "SRT_SINGLE_CJK_CHARACTER"]


# ----- the ordinary import (U130) ----------------------------------------------------------------------------------
def test_an_imported_lone_reply_joins_its_touching_same_speaker_sentence(tmp_path):
    rows = [(0.0, 1.5, "李豆沙", "我是侄女"), (1.5, 2.1, "李豆沙", "对"),          # same speaker, touching: joined
            (2.9, 4.0, "雾深Girimi", "这样子啊"), (4.0, 4.7, "李豆沙", "对"),     # 111_330 #22: between guest lines
            (4.7, 6.2, "雾深Girimi", "你是侄女吗"),
            (8.1, 8.7, "李豆沙", "那"), (9.4, 9.9, "李豆沙", "那")]             # 111_330 #48/#49: across silence
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    si.record_import(ctx.state, ctx.clip_dir, _push(tmp_path, ctx.state.data["clip"], rows), source="push-test",
                     authority="U130")
    out = si.step3_subtitles(ctx)
    assert out.ok, (out.reason, out.note)
    final = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    cues = st.parse_blocks(final)
    assert cues[0]["text"] == "我是侄女，对" and (cues[0]["start_s"], cues[0]["end_s"]) == (0.0, 2.1)
    # U138: the ones another speaker's line or a silence keeps apart are not subtitled; nothing else is touched
    assert _lone_blocks(final) == []
    assert [c["text"] for c in cues] == ["我是侄女，对", "这样子啊", "你是侄女吗"]
    labels = json.loads((ctx.clip_dir / "subs/import_speakers.json").read_text(encoding="utf-8"))
    assert labels == [HOST, GUEST, GUEST]                                       # one label per final cue, still aligned
    line = next(p for p in out.process if p.startswith("孤立单字"))
    assert "并入同一说话人紧挨着的句子 1 处" in line and "并不进去、不出字幕 3 处" in line


# ----- a linked clip's own path: step 3 has no speakers, step 4 joins once they are known ---------------------------
def test_a_linked_clip_joins_its_lone_character_after_step_4_labels_the_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_fmr", _fake_fmr)
    monkeypatch.setattr(sp, "description_text", lambda t, p=None: "简介")
    remote = _remote()
    text = srt(40).replace("\n句子12\n", "\n对\n").encode()
    remote.files["/r/clip/asr/bcut.srt"] = text
    remote.files["/r/clip/asr/jianying.srt"] = text
    cpa = ScriptedCpa({"boundary": BOUNDARY, "title": [TITLE], "review": [_review(verdict="PASS")]})
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa)
    report = run_clip(ctx, STEPS)
    assert report.stopped_at is None, (report.stopped_at, report.reason)
    final = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    assert _lone_blocks(final) == []
    texts = [c["text"] for c in st.parse_blocks(final)]
    assert "对，句子13" in texts and "对" not in texts
    speaker = (ctx.clip_dir / "speaker/speaker.srt").read_text(encoding="utf-8")
    assert [c["text"] for c in st.parse_blocks(speaker)] == texts                # both files from the one track
    process = (ctx.clip_dir / "PROCESS.md").read_text(encoding="utf-8")
    assert "待第 4 步标出说话人 1 处" in process                                  # step 3 says why it waited
    assert "并入同一说话人紧挨着的句子 1 处" in process                           # step 4 joined it


# ----- a review fix (the third writer) -----------------------------------------------------------------------------
def test_a_review_fix_that_leaves_a_lone_character_is_joined_in_the_same_write(tmp_path):
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    track = [{"start_s": 0.0, "end_s": 1.5, "text": "我是侄女", "speaker": HOST},
             {"start_s": 1.5, "end_s": 2.1, "text": "对的", "speaker": HOST},
             {"start_s": 2.8, "end_s": 4.0, "text": "这样子啊", "speaker": GUEST}]
    st.write_track(ctx.clip_dir, track, speakers=True)
    (ctx.clip_dir / "subs/doubts.json").write_text("[]", encoding="utf-8")
    (ctx.clip_dir / "title").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "title/title.txt").write_text("【李豆沙】标题\n", encoding="utf-8")
    changed = sp.apply_fixes(ctx, {"fixes": {"subtitles": [{"cue_id": 2, "text": "对"}]}})
    assert changed["subtitles"] == 1
    final = (ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8")
    assert _lone_blocks(final) == [] and st.parse_blocks(final)[0]["text"] == "我是侄女，对"
    assert [c["speaker"] for c in st.read_track(ctx.clip_dir)] == [HOST, GUEST]


# ----- controls: what the release check accepts, and what may not be joined --------------------------------------
def _imported_final(tmp_path, rows):
    ctx = make_ctx(tmp_path, remote=None, cpa=None)
    si.record_import(ctx.state, ctx.clip_dir, _push(tmp_path, ctx.state.data["clip"], rows), source="push-test",
                     authority="U130")
    assert si.step3_subtitles(ctx).ok
    return [c["text"] for c in st.parse_blocks((ctx.clip_dir / "subs/final.srt").read_text(encoding="utf-8"))]


def test_standalone_utterances_and_name_echoes_stay_their_own_lines(tmp_path):
    rows = [(0.0, 1.0, "雾深Girimi", "有熊猫吗"), (1.0, 1.6, "雾深Girimi", "有"),   # 有 as a complete answer
            (1.6, 2.2, "雾深Girimi", "啊"),                                         # interjection
            (2.2, 2.8, "李豆沙", "秦"), (2.8, 4.0, "李豆沙", "秦秦你来了")]          # name echo of the next line
    assert _imported_final(tmp_path, rows) == [t for *_, t in rows]


def test_a_lone_character_is_never_joined_across_a_silence_or_into_another_speaker(tmp_path):
    gap = 0.15                                # merge_release_grade_cues' contiguous_gap_ms (the repo producer's)
    rows = [(0.0, 1.0, "李豆沙", "我是侄女"), (1.0 + gap + 0.05, 1.8, "李豆沙", "对"),
            (1.8, 3.0, "雾深Girimi", "那你是侄女吗")]
    assert _imported_final(tmp_path, rows) == ["我是侄女", "那你是侄女吗"]        # not joined: not subtitled


def test_unlabelled_lines_of_a_linked_clip_wait_and_a_one_person_session_joins():
    assert normalize.release_contiguous_gap_s() == 0.15            # read from merge_release_grade_cues, not copied
    cues = [{"start_s": 0.0, "end_s": 0.6, "text": "对"}, {"start_s": 0.6, "end_s": 2.0, "text": "那你是侄女吗"}]
    assert normalize.join_lone_chars(cues)[0] == cues               # a linked clip before step 4: speakers unknown
    assert [c["text"] for c in normalize.join_lone_chars(cues, one_speaker=True)[0]] == ["对，那你是侄女吗"]


def test_a_joined_line_is_shaped_again_and_the_nearer_side_wins():
    audit = normalize._audit()
    long_line = "甲" * (audit.DEFAULT_MAX_VISUAL_LINES * audit.DEFAULT_MAX_VISUAL_LINE_CHARS)
    cues = [{"start_s": 0.0, "end_s": 4.0, "text": long_line, "speaker": HOST},
            {"start_s": 4.0, "end_s": 4.5, "text": "对", "speaker": HOST},
            {"start_s": 4.6, "end_s": 6.0, "text": "下一句", "speaker": HOST}]
    out, rows = normalize.join_lone_chars(cues)
    assert rows[0]["action"] == "joined_prev"                                  # gap 0 before, 0.1 s after
    assert _lone_blocks(st.format_blocks(out, labels=False)) == []
    for cue in out:
        lines = cue["text"].split("\n")
        assert len(lines) <= audit.DEFAULT_MAX_VISUAL_LINES
        assert all(len(ln) <= audit.DEFAULT_MAX_VISUAL_LINE_CHARS for ln in lines)
    assert "".join(c["text"].replace("\n", "") for c in out[:-1]) == long_line + "，对"
