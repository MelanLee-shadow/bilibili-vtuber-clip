"""A collab with two named guests: a guest line is told to be one of them only through the witnesses' speaker groups and
a cited line that holds (11-23 443680: 「果然awawa就是这种幕后黑手的人」 → 「我我我不是，不要瞎说。」 is 安晚awa, and so is
the bomb proposal in the same MOSS group), the channel owner's own labels, or a confident voiceprint of a guest the
classifier knows (A12)."""

from __future__ import annotations

import json

from test_title_history import context

from src.autoslice.manual_lane import guest_identity, step6_title, subtitle_track

LINES = [("李豆沙", "果然awawa就是这种幕后黑手的人。"), ("连线", "我我我不是，不要瞎说。"),
         ("连线", "我可以拿那个炸弹，稍微暖一暖。"), ("连线", "好好可怕。"), ("李豆沙", "那我直接死了。"),
         ("连线", "这个是什么必杀技吗？")]
MOSS = ["S01", "S04", "S04", "S02", "S01", "S02"]
MAI = ["S00", "S02", "S02", "S01", "S00", "S01"]
ANSWER = {"anchors": [{"line": 2, "name": "安晚awa", "kind": "被点名后接话", "quote": "我我我不是"}]}


def _clip(tmp_path, cid, *, moss=MOSS, mai=MAI, guests=(["安晚awa"], ["礼墨Sumi"])):
    ctx, cpa = context(tmp_path, cid, '提议拿炸弹暖一暖')
    ctx.session['participants'] = [['李豆沙', '小李'], *guests]
    cues = [{"start_s": 2.0 * i, "end_s": 2.0 * i + 1.8, "text": t, "speaker": s} for i, (s, t) in enumerate(LINES)]
    subtitle_track.write_track(ctx.clip_dir, cues, speakers=True)
    (ctx.clip_dir / 'witness').mkdir()
    for engine, groups in (("moss", moss), ("mai", mai)):
        segs = [{"start_ms": 2000 * i, "end_ms": 2000 * i + 1800, "speaker": g} for i, g in enumerate(groups)]
        (ctx.clip_dir / f'witness/{engine}.json').write_text(json.dumps({"status": "OK", "segments": segs}))
    return ctx, cpa


def _identify(ctx, answer=ANSWER, asked=None):
    def ask(call_id, prompt):
        if asked is not None:
            asked.append(prompt)
        return answer
    return guest_identity.identify(ctx, subtitle_track.read_track(ctx.clip_dir), ask)


def test_a_cited_answer_names_her_group_and_the_other_lines_of_it(tmp_path):
    asked = []
    ctx, _ = _clip(tmp_path, 'a')
    assert _identify(ctx, asked=asked) == {2: "安晚awa", 3: "安晚awa"}
    assert '[连线·甲] 我我我不是' in asked[0] and '[连线·乙] 好好可怕' in asked[0]
    _track, lines, srt = step6_title.title_inputs(ctx, ['李豆沙', '安晚awa', '礼墨Sumi'])
    assert '[安晚awa] 我可以拿那个炸弹' in lines and '[连线] 好好可怕' in lines and '[安晚awa]' in srt


def test_citations_that_do_not_hold_name_nobody(tmp_path):
    ctx, _ = _clip(tmp_path, 'b')
    made_up = {"anchors": [{"line": 2, "name": "安晚awa", "kind": "被点名后接话", "quote": "我是安晚"},      # not in the line
                           {"line": 4, "name": "礼墨Sumi", "kind": "被点名后接话", "quote": "好好可怕"},     # nobody named her
                           {"line": 5, "name": "礼墨Sumi", "kind": "自称", "quote": "那我直接死了"},        # the host's line
                           {"line": 6, "name": "露医生", "kind": "自称", "quote": "必杀技"},              # not a guest
                           {"line": 6, "name": "礼墨Sumi", "kind": "口吻像她", "quote": "必杀技"}]}       # not evidence
    assert _identify(ctx, made_up) == {}


def test_a_line_whose_engines_name_different_guests_stays_unnamed(tmp_path):
    # MAI puts line 3 with line 4, whose group a second citation names 礼墨Sumi; MOSS keeps it with 安晚awa
    ctx, _ = _clip(tmp_path, 'c', mai=["S00", "S02", "S01", "S01", "S00", "S01"])
    two = {"anchors": [*ANSWER["anchors"], {"line": 6, "name": "礼墨Sumi", "kind": "自称", "quote": "必杀技"}]}
    got = _identify(ctx, two)
    assert got[2] == "安晚awa" and got[6] == "礼墨Sumi" and got[4] == "礼墨Sumi" and 3 not in got


def test_one_guest_or_a_failed_call_names_nothing(tmp_path):
    ctx, _ = _clip(tmp_path, 'd', guests=(["安晚awa"],))
    assert _identify(ctx, answer=AssertionError) == {}
    ctx, _ = _clip(tmp_path, 'e')
    assert _identify(ctx, answer=None) == {} and guest_identity.saved_lines(ctx) == {}


def _owner(ctx, names, *, decided_by="owner"):
    """The channel owner's own labels came with his import (speaker_label_page apply -> import/speakers.json)."""
    rows = [{"start_s": 2.0 * i, "end_s": 2.0 * i + 1.8, "text": t, "speaker": names.get(i + 1, s)}
            for i, (s, t) in enumerate(LINES)]
    path = ctx.clip_dir / 'import/speakers.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False))
    ctx.state.data['imported'] = {"source": "owner-labels", "from": "x", "authority": "x", "at": "x",
                                  "files": {"speakers.json": str(path)},
                                  **({"decided_by": decided_by} if decided_by else {})}


def test_the_owners_names_stand_over_the_model_and_name_his_unlabelled_lines(tmp_path):
    # 11-24 625460: he labelled the guest lines by name; the lane kept only 连线 and the title had to write 队友
    ctx, _ = _clip(tmp_path, 'f')
    _owner(ctx, {2: "礼墨Sumi", 3: "安晚awa", 4: "礼墨Sumi", 6: "未知"})
    asked = []
    got = _identify(ctx, asked=asked)          # the model's answer says line 2 is 安晚awa
    assert asked and got[2] == "礼墨Sumi" and got[3] == "安晚awa" and got[4] == "礼墨Sumi"
    assert got[6] == "礼墨Sumi"                  # 未知 to him; his line 4 names the group it shares with it
    assert json.loads((ctx.clip_dir / guest_identity.PATH).read_text())["owner"] == {"2": "礼墨Sumi", "3": "安晚awa", "4": "礼墨Sumi"}
    _track, lines, _srt = step6_title.title_inputs(ctx, ['李豆沙', '安晚awa', '礼墨Sumi'])
    assert '[礼墨Sumi] 我我我不是' in lines


def test_with_every_guest_line_named_by_him_no_model_is_asked(tmp_path):
    ctx, _ = _clip(tmp_path, 'g')
    _owner(ctx, {2: "安晚awa", 3: "安晚awa", 4: "礼墨Sumi", 6: "礼墨Sumi"})
    asked = []
    assert _identify(ctx, answer=None, asked=asked) == {2: "安晚awa", 3: "安晚awa", 4: "礼墨Sumi", 6: "礼墨Sumi"}
    assert asked == []


def test_names_in_an_import_he_did_not_decide_are_not_his(tmp_path):
    ctx, _ = _clip(tmp_path, 'h')
    _owner(ctx, {2: "礼墨Sumi", 3: "礼墨Sumi", 4: "礼墨Sumi", 6: "礼墨Sumi"}, decided_by=None)
    assert _identify(ctx) == {2: "安晚awa", 3: "安晚awa"}


# ----- the voiceprint's own guest classes (A12, U467; lane_rules guest_voiceprint) --------------------------------------
CLASSES = ["李豆沙", "礼墨Sumi", "安晚awa"]
SUMI = {"李豆沙": 0.01, "礼墨Sumi": 0.97, "安晚awa": 0.02}          # host 0.01, her share of the guests 0.98
AWA = {"李豆沙": 0.01, "礼墨Sumi": 0.02, "安晚awa": 0.97}


def _chunks(ctx, spans, *, classes=CLASSES, extra=None):
    """speaker/chunks-units.json + chunks-pred.json for [(start_ms, end_ms, probs, ok)] (step 3's ~1.2 s chunk run)."""
    units = [{"id": f"u{i}", "s": s, "e": e, "lw": e, "cue": i, "seg": "x"} for i, (s, e, _p, _ok) in enumerate(spans)]
    preds = [{"id": f"u{i}", "probs": p, "ok": ok} for i, (_s, _e, p, ok) in enumerate(spans)]
    (ctx.clip_dir / 'speaker').mkdir(exist_ok=True)
    (ctx.clip_dir / guest_identity.VOICE_UNITS).write_text(json.dumps({"units": units}))
    (ctx.clip_dir / guest_identity.VOICE_PREDS).write_text(json.dumps(
        {"classes": classes, "temperature": 0.95, "predictions": preds, **(extra or {})}, ensure_ascii=False))


def _line(n, probs, ok=True):
    """Two chunks covering line n (1-based) of LINES: 2.0*(n-1) .. +1.8 s."""
    a = 2000 * (n - 1)
    return [(a, a + 900, probs, ok), (a + 900, a + 1800, probs, ok)]


def _voice(ctx, track=None):
    track = track if track is not None else subtitle_track.read_track(ctx.clip_dir)
    return guest_identity.voiceprint_lines(ctx, track, ["安晚awa", "礼墨Sumi"])


def test_a_confident_guest_line_is_named_by_the_voiceprint_and_names_no_group(tmp_path):
    ctx, _ = _clip(tmp_path, 'v1')
    _chunks(ctx, [*_line(4, SUMI), *_line(5, {"李豆沙": 0.95, "礼墨Sumi": 0.04, "安晚awa": 0.01})])
    got = _identify(ctx)                      # the model names line 2 安晚awa and her MOSS group (line 3)
    assert got == {2: "安晚awa", 3: "安晚awa", 4: "礼墨Sumi"}     # line 6 shares MOSS S02 with 4 but stays unnamed
    assert json.loads((ctx.clip_dir / guest_identity.PATH).read_text())["voice"] == {"4": "礼墨Sumi"}
    _track, lines, _srt = step6_title.title_inputs(ctx, ['李豆沙', '安晚awa', '礼墨Sumi'])
    assert '[礼墨Sumi] 好好可怕' in lines and '[连线] 这个是什么必杀技吗' in lines


def test_host_share_duration_and_coverage_gates_each_block(tmp_path):
    ctx, _ = _clip(tmp_path, 'v2')
    _chunks(ctx, [*_line(2, {"李豆沙": 0.2, "礼墨Sumi": 0.78, "安晚awa": 0.02}),     # host 0.2 > host_max
                  *_line(3, {"李豆沙": 0.01, "礼墨Sumi": 0.79, "安晚awa": 0.2}),     # her share 0.8 < share_min
                  (6000, 6900, SUMI, True), (6900, 7800, SUMI, False),           # line 4: half heard (one chunk not ok)
                  *_line(6, AWA)])
    assert _voice(ctx) == {6: "安晚awa"}
    short = [{"start_s": 10.0, "end_s": 10.8, "text": "怎么办", "speaker": "连线"}]   # shorter than min_duration_s
    assert _voice(ctx, short) == {}
    assert _voice(ctx, [dict(short[0], end_s=11.8)]) == {1: "安晚awa"}              # the same chunks, long enough
    host_line = [dict(short[0], end_s=11.8, speaker="李豆沙")]                       # only [连线] lines are named
    assert _voice(ctx, host_line) == {}


def test_a_guest_the_classifier_does_not_know_turns_the_voiceprint_off(tmp_path):
    ctx, _ = _clip(tmp_path, 'v3', guests=(["安晚awa"], ["露医生"]))
    _chunks(ctx, [*_line(4, SUMI), *_line(6, AWA)])
    assert _voice(ctx) == {}
    ctx, _ = _clip(tmp_path, 'v4')
    _chunks(ctx, [*_line(4, SUMI), *_line(6, AWA)], classes=["李豆沙", "礼墨Sumi"])   # a participant is no class
    assert _voice(ctx) == {}


def test_the_owners_label_stands_over_the_voiceprint_and_together_they_spare_the_model(tmp_path):
    ctx, _ = _clip(tmp_path, 'v5')
    _chunks(ctx, [*_line(4, SUMI), *_line(6, SUMI)])
    _owner(ctx, {2: "安晚awa", 3: "安晚awa", 4: "安晚awa", 6: "未知"})
    asked = []
    assert _identify(ctx, answer=None, asked=asked) == {2: "安晚awa", 3: "安晚awa", 4: "安晚awa", 6: "礼墨Sumi"}
    assert asked == []
    saved = json.loads((ctx.clip_dir / guest_identity.PATH).read_text())
    assert saved["voice"] == {"4": "礼墨Sumi", "6": "礼墨Sumi"} and saved["answer"] is None


def test_a_changed_chunk_run_is_not_answered_from_the_old_record(tmp_path):
    ctx, _ = _clip(tmp_path, 'v6')
    _chunks(ctx, _line(4, SUMI))
    assert _identify(ctx)[4] == "礼墨Sumi"
    first = json.loads((ctx.clip_dir / guest_identity.PATH).read_text())["key"]
    _chunks(ctx, _line(4, SUMI), extra={"temperature": 1.0})     # same names, another run: still a new key
    asked = []
    _identify(ctx, asked=asked)
    assert asked and json.loads((ctx.clip_dir / guest_identity.PATH).read_text())["key"] != first
    _chunks(ctx, _line(4, AWA))
    assert _identify(ctx)[4] == "安晚awa"


def test_a_failed_call_leaves_the_title_this_runs_names_not_an_older_tracks(tmp_path):
    # the title reads the record (saved_lines), not identify's return: an older track's record must not label this one
    ctx, _ = _clip(tmp_path, 'v9')
    assert _identify(ctx) == {2: "安晚awa", 3: "安晚awa"}           # a record of an earlier run
    _chunks(ctx, _line(4, SUMI))                                     # the chunk run changes: a new key
    assert _identify(ctx, answer=None) == {4: "礼墨Sumi"}
    assert guest_identity.saved_lines(ctx) == {4: "礼墨Sumi"}
    _track, lines, _srt = step6_title.title_inputs(ctx, ['李豆沙', '安晚awa', '礼墨Sumi'])
    assert '[连线] 我我我不是' in lines and '[礼墨Sumi] 好好可怕' in lines
    asked = []
    assert _identify(ctx, asked=asked) == {2: "安晚awa", 3: "安晚awa", 4: "礼墨Sumi"} and asked   # asked again


def test_the_process_says_which_evidence_named_the_lines(tmp_path):
    ctx, _ = _clip(tmp_path, 'v10')
    _chunks(ctx, [*_line(4, SUMI), *_line(6, SUMI)])
    _owner(ctx, {2: "安晚awa", 3: "安晚awa", 4: "安晚awa", 6: "未知"})
    got = _identify(ctx, answer=None)                                # no model: his labels and the voiceprint
    note = step6_title._identity_process(ctx, got, asked=False)
    assert note.startswith("嘉宾是谁：4 句") and "频道主标注 3 句" in note and "声纹 1 句" in note
    assert "对话证据" not in note


def test_without_chunk_files_the_voiceprint_names_nothing_and_the_rest_stands(tmp_path):
    ctx, _ = _clip(tmp_path, 'v7')                # a revision _r* clip has no chunk run
    assert _voice(ctx) == {}
    assert _identify(ctx) == {2: "安晚awa", 3: "安晚awa"}
    assert json.loads((ctx.clip_dir / guest_identity.PATH).read_text())["voice"] == {}
    ctx, _ = _clip(tmp_path, 'v8')
    (ctx.clip_dir / 'speaker').mkdir(exist_ok=True)
    (ctx.clip_dir / guest_identity.VOICE_PREDS).write_text("{not json")
    assert _voice(ctx) == {}


def test_her_own_words_naming_her_stand_over_the_voiceprint(tmp_path):
    # review of 3e282d614: a verbatim self-naming the citation check kept is stronger than a 0.95 voiceprint
    ctx, _ = _clip(tmp_path, 'v11')
    _chunks(ctx, _line(6, AWA))
    said = {"anchors": [*ANSWER["anchors"], {"line": 6, "name": "礼墨Sumi", "kind": "自称", "quote": "必杀技"}]}
    assert _identify(ctx, said)[6] == "礼墨Sumi"
    assert json.loads((ctx.clip_dir / guest_identity.PATH).read_text())["voice"] == {"6": "安晚awa"}


def test_the_process_names_the_model_answer_also_when_it_is_reused(tmp_path):
    ctx, _ = _clip(tmp_path, 'v12')
    got = _identify(ctx)
    assert "识别器分组＋对话证据；" in step6_title._identity_process(ctx, got, asked=True)
    again = step6_title._identity_process(ctx, _identify(ctx), asked=False)
    assert "沿用上次的回答" in again and "（；" not in again
    ctx, _ = _clip(tmp_path, 'v13')
    _chunks(ctx, _line(4, SUMI))
    _identify(ctx, answer=None)
    assert "调用失败" in step6_title._identity_process(ctx, {4: "礼墨Sumi"}, asked=True)


def test_a_failed_chunk_run_leaves_no_reading_of_other_chunks(tmp_path):
    # review of 3e282d614: new chunk units written, the classifier run failed, the old predictions stayed beside them
    from types import SimpleNamespace

    from fakes import make_ctx

    from src.autoslice.manual_lane import step4_speaker

    class FailingRemote:
        def put(self, *_a, **_k):
            return None

        def run_script(self, **_k):
            return SimpleNamespace(rc=1, stdout="", stderr="boom")

    ctx = make_ctx(tmp_path, remote=FailingRemote(), cpa=None)
    (ctx.clip_dir / 'speaker').mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / guest_identity.VOICE_UNITS).write_text(json.dumps({"units": []}))
    (ctx.clip_dir / guest_identity.VOICE_PREDS).write_text(json.dumps({"classes": CLASSES, "predictions": []}))
    got, note = step4_speaker.voice_chunks(ctx, [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "x"}])
    assert got == {} and "失败" in note
    assert not (ctx.clip_dir / guest_identity.VOICE_PREDS).exists()
