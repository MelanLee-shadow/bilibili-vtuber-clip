"""`discover` on a channel whose draws name stories and roles (core_draws `stories`/`roles`,): what the recall is given, what
becomes of every danmaku burst, the evening's candidates on one matter, and the clip they are joined into.

the channel's request; of two clips on who is whose pet,
同话题的两条应合并成一条."""
import json
import types

import pytest

from src.autoslice.danmaku_evidence import SessionDensity
from src.autoslice.llm_client import LlmCallError
from src.autoslice.manual_lane import compilation as comp, core_draws as cd, discover as dv, discover_aids as da
from src.autoslice.manual_lane import discover_lead_in as dl, discover_select as ds, discover_topics as dt
from src.autoslice.manual_lane.config import load_session_config

from test_compilation import ROOT, _adapters, _cues, _srt
from test_core_draws import _drawn, row

SEGMENT = "22966160_20261124-22-30-04"
SRT = "".join(f"{n}\n00:00:{n:02d},000 --> 00:00:{n + 1:02d},000\n{text}\n\n" for n, text in enumerate(
    ["开场", "为什么他们在说难听难听", "这里面哪个是难听难听啊", "他们在说那个绿色", "原来是这样", "下一局"], start=1))


def _xml(rows):
    return ("<i>" + "".join(f'<d p="{at},1,25,16777215,0,0,7,0">{text}</d>' for at, text in rows) + "</i>").encode()


def _candidate(cid, first, last, cues):
    mine = [types.SimpleNamespace(cue_id=c.cue_id, text=c.text) for c in cues if first <= int(c.cue_id) <= last]
    return types.SimpleNamespace(anchor=types.SimpleNamespace(candidate_id=cid), content_type_hint="talk", cues=mine,
                                 boundary=types.SimpleNamespace(resolved_start_ms=first * 1000, resolved_end_ms=(last + 1) * 1000,
                                                                start_boundary_score=0.9))


def _timed_srt(rows):
    clock = lambda s: f"00:{s // 60:02d}:{s % 60:02d},000"
    return "".join(f"{n}\n{clock(at)} --> {clock(at + 1)}\n{text}\n\n" for n, (at, text) in enumerate(rows, start=1))


def test_the_recall_gets_the_focus_the_reading_aids_and_numbered_bursts_and_every_burst_is_accounted_for(tmp_path):
    """the channel's request: every half minute at three
    times the broadcast's own baseline is a burst the recall accounts for -- in a candidate (one that ends a minute before it
    holds it: the laughs come late), or the model says why not; one left silent is asked about again, alone; one silent
    after that stays UNANSWERED (the day's ledger names it).  Song calls are not counted (「弹幕数量要排除掉歌切」)."""
    from test_discover import card

    (tmp_path / "bcut.srt").write_text(_timed_srt(
        [(n, t) for n, t in enumerate(["开场", "为什么他们在说难听难听", "这里面哪个是难听难听啊", "他们在说那个绿色", "原来是这样",
                                       "下一局"], start=1)]
        + [(250, "我跟你们说个事"), (260, "我昨天被骗了"), (289, "整个人都很小丑啊"), (430, "随便聊聊")]), encoding="utf-8")
    # over a quiet broadcast (baseline 2 a half minute): 0-30 s (the candidate is there), 180-210 s (a song call: no burst),
    # 210-240 s (the model says why not), 300-330 s (the laughs at 289 s: found when asked again), 420-450 s (silent twice)
    chat = ([(2 + i * 0.5, "矮人哈哈哈哈") for i in range(7)] + [(4, "捡破烂的绿色就是南町")] + [(20, "[李豆沙_大笑]")]
            + [(40 + 30 * b, "路过") for b in range(4) for _ in range(2)] + [(181 + i, f"打call第{i}声") for i in range(7)]
            + [(211 + i, f"抽奖抽奖{i}") for i in range(7)] + [(301 + i, "哈哈哈哈小丑") for i in range(6)] + [(305, "好可怜")]
            + [(421 + i, f"没人管的一段{i}") for i in range(7)])
    (tmp_path / "source.xml").write_bytes(_xml(chat))
    prepared = {"date": "2026-11-24", "segment": SEGMENT, "path": f"/arc/{SEGMENT}.flv", "size": 1, "mtime": "1",
                "directory": str(tmp_path), "source_fp": "s", "xml": {"sha256": "x", "exists": True}, "srt_sha256": "t"}
    calls = []

    def selector(cues, *, llm_call, max_candidates, danmaku_hints, danmaku_xml, **focus):
        calls.append(dict(focus, hints=danmaku_hints, cues=[c.cue_id for c in cues], max_candidates=max_candidates))
        if len(calls) == 1:                                      # the recall of the whole recording
            return [_candidate("semantictalk_2000_6000", 2, 5, cues)], {
                "hooks": {"semantictalk_2000_6000": "把绿矮人叫成同事"},
                "scorecards": {"semantictalk_2000_6000": dict(card(4), start_cue=2, end_cue=5)},
                "burst_answers": {"B2": {"selected": False, "reason": "抽奖口令刷屏，和她说的话无关"}}}
        if danmaku_hints.startswith("B3 "):                      # asked again: the story the laughs are at, ending before them
            found = types.SimpleNamespace(anchor=types.SimpleNamespace(candidate_id="semantictalk_250000_290000"),
                                          content_type_hint="talk", cues=[c for c in cues if c.cue_id in {"7", "8", "9"}],
                                          boundary=types.SimpleNamespace(resolved_start_ms=250_000, resolved_end_ms=290_000,
                                                                         start_boundary_score=0.9))
            return [found], {"hooks": {"semantictalk_250000_290000": "被骗"},
                             "scorecards": {"semantictalk_250000_290000": dict(card(4), start_cue=7, end_cue=9)}}
        return [], {"hooks": {}, "scorecards": {}}

    asked = []

    def ask(prompt, accepts=None):
        asked.append(prompt)
        labels = [row("semantictalk_2000_6000", ["relation"], story="person",
                      peaks=[{"cue": 2, "quote": "为什么他们在说难听难听", "who": "李豆沙", "why": "她没听懂"}]),
                  row("semantictalk_250000_290000", ["relation"], peaks=[{"cue": 9, "quote": "整个人都很小丑啊", "who": "李豆沙",
                                                                         "why": "自嘲"}])]
        return json.dumps({"labels": [r for r in labels if r["id"] in prompt]}, ensure_ascii=False)

    doc = ds.recall_segment(prepared, tmp_path, ask=ask, selector=selector, density=SessionDensity(2.0, 6.0))
    first = calls[0]
    # the channel's gates and kinds of story, in its asset's words, in no order; what helps to read the raw recognition
    assert "先过两道门" in first["selection_focus"] and "- 人对人" in first["selection_focus"] and "不分先后" in first["selection_focus"]
    assert "字幕里的「难听」和「南町」同音，可能就是在说南町" in first["reading_aids"]
    hints = first["hints"].split("\n")
    assert [h.split(" ", 2)[:2] for h in hints] == [["B1", "[00:00-00:30]"], ["B2", "[03:30-04:00]"], ["B3", "[05:00-05:30]"],
                                                    ["B4", "[07:00-07:30]"]]
    # the count against the broadcast's baseline; the sample is not the first five of the window: the line that explains the
    # joke is in, the emote and the repeats are not -- the repeats are counted instead
    assert hints[0].startswith("B1 [00:00-00:30] 9 条（这场平时每半分钟 2 条）；刷得最多：矮人哈哈哈哈×7、路过×4；说了什么：")
    assert "00:04 捡破烂的绿色就是南町" in hints[0] and "大笑" not in hints[0].split("说了什么")[1]
    # asked again: B3 and B4 one at a time, each alone, with the lines from three minutes before to a minute after
    assert [c["hints"].split(" ")[0] for c in calls[1:]] == ["B3", "B4"] and calls[1]["hints"] == hints[2]
    assert calls[1]["cues"] == ["7", "8", "9"] and calls[2]["cues"] == ["7", "8", "9", "10"]
    assert calls[1]["max_candidates"] == 3 and "先过两道门" in calls[1]["selection_focus"]
    assert [(b["id"], b["disposition"], b["candidate_id"]) for b in doc["bursts"]] == [
        ("B1", ds.BURST_IN_CANDIDATE, "semantictalk_2000_6000"), ("B2", ds.BURST_NOT_SELECTED, None),
        ("B3", ds.BURST_IN_CANDIDATE, "semantictalk_250000_290000"), ("B4", ds.BURST_UNANSWERED, None)]
    assert doc["bursts"][1]["reason"] == "抽奖口令刷屏，和她说的话无关" and doc["bursts"][0]["baseline"] == 2.0
    assert doc["bursts"][0]["threshold"] == 6.0
    assert doc["burst_reask"]["asked"] == ["B3", "B4"] and [b["reask"] for b in doc["bursts"]] == [None, None, "ASKED", "ASKED"]
    # every candidate names the bursts it holds (the seating's burst points)
    assert {e["candidate_id"]: e.get("bursts") for e in doc["input_pool"]} == {
        "semantictalk_2000_6000": ["B1"], "semantictalk_250000_290000": ["B3"]}
    # labelled from the recording's subtitles; the key moment is on the candidate, at its time in the recording
    entry = doc["input_pool"][0]
    assert "#2 为什么他们在说难听难听" in asked[0]
    assert entry["core_draws"]["story"] == "person" and entry["peaks"][0]["at_ms"] == 2000
    assert json.loads((tmp_path / "recall.json").read_text(encoding="utf-8"))["bursts"] == doc["bursts"]
    # read back: neither recalled nor asked about B4 again
    assert ds.recall_segment(prepared, tmp_path, ask=lambda p, accepts=None: pytest.fail("asked again"),
                             selector=lambda *a, **k: pytest.fail("recalled again"), density=SessionDensity(2.0, 6.0)) == doc
    # a day without any danmaku has no baseline: no bursts, nothing to account for
    (tmp_path / "recall.json").unlink()
    calls.clear()
    assert ds.recall_segment(prepared, tmp_path, ask=ask, selector=selector, density=None)["bursts"] == []
    assert calls[0]["hints"] is None and len(calls) == 1


def test_the_models_word_on_a_burst_decides_and_the_time_checks_it(tmp_path):
    """A burst the model says is in none of its candidates is NOT_SELECTED with its reason, even when a candidate ends within
    the minute before it (the lead is for a burst the model does not answer about), and that candidate gets no burst points.
    One the model says is in a candidate when none holds it is no reason for leaving it: it is asked about again.  A second
    question the model does not answer leaves the recall's candidates in the pool; the burst is FAILED and asked next round."""
    from test_discover import card

    (tmp_path / "bcut.srt").write_text(_timed_srt([(1, "开场"), (5, "讲个事"), (20, "讲完了"), (100, "下一个话题"), (130, "又一个")]),
                                       encoding="utf-8")
    # B1 30-60 s: the candidate (1-21 s) ended 9 s before it; B2 150-180 s: the model claims a candidate, none is there
    chat = [(31 + i, f"礼物感谢{i}") for i in range(7)] + [(151 + i, f"好耶{i}") for i in range(7)]
    (tmp_path / "source.xml").write_bytes(_xml(chat))
    prepared = {"date": "2026-11-24", "segment": SEGMENT, "path": f"/arc/{SEGMENT}.flv", "size": 1, "mtime": "1",
                "directory": str(tmp_path), "source_fp": "s", "xml": {"sha256": "x", "exists": True}, "srt_sha256": "t"}
    calls, fail = [], [True]

    def selector(cues, *, llm_call, max_candidates, danmaku_hints, danmaku_xml, **focus):
        calls.append(danmaku_hints)
        if len(calls) == 1:
            return [_candidate("semantictalk_1000_21000", 1, 3, cues)], {
                "hooks": {"semantictalk_1000_21000": "讲个事"},
                "scorecards": {"semantictalk_1000_21000": dict(card(4), start_cue=1, end_cue=3)},
                "burst_answers": {"B1": {"selected": False, "reason": "在刷礼物感谢，和她讲的事无关"},
                                  "B2": {"selected": True, "reason": "进了候选“讲个事”"}}}
        if fail[0]:
            raise LlmCallError("semantic recall completion has no candidates array")
        return [], {"hooks": {}, "scorecards": {},
                    "burst_answers": {"B2": {"selected": False, "reason": "观众在刷好耶，她在换话题"}}}

    ask = lambda prompt, accepts=None: json.dumps({"labels": [row("semantictalk_1000_21000", ["relation"])]})
    density = SessionDensity(1.0, 6.0)
    doc = ds.recall_segment(prepared, tmp_path, ask=ask, selector=selector, density=density)
    assert [(b["id"], b["disposition"], b["selected"], b["reask"]) for b in doc["bursts"]] == [
        ("B1", ds.BURST_NOT_SELECTED, False, None), ("B2", ds.BURST_UNANSWERED, True, "FAILED")]
    assert doc["bursts"][0]["reason"] == "在刷礼物感谢，和她讲的事无关" and doc["bursts"][0]["candidate_id"] is None
    assert [e["candidate_id"] for e in doc["input_pool"]] == ["semantictalk_1000_21000"]       # kept, though the question failed
    assert "bursts" not in doc["input_pool"][0]                                               # no burst points for B1
    assert calls[1].startswith("B2 ") and len(calls) == 2
    # the next round reads the recall back and asks B2 again; answered now, it is settled
    fail[0] = False
    doc = ds.recall_segment(prepared, tmp_path, ask=ask, selector=selector, density=density)
    assert [(b["id"], b["disposition"], b["reask"]) for b in doc["bursts"]] == [
        ("B1", ds.BURST_NOT_SELECTED, None), ("B2", ds.BURST_NOT_SELECTED, "ASKED")]
    assert doc["bursts"][1]["reason"] == "观众在刷好耶，她在换话题" and len(calls) == 3


def test_the_day_density_is_the_broadcasts_own_without_song_calls_and_platform_lines(tmp_path):
    """The baseline is the median half minute of every recording of the day, not of one file: 11-24 21:30 was busy from start
    to end, so against itself it had no burst.  A song call or a platform 口令 (info[0][9] != 0) is no reaction."""
    quiet, busy = tmp_path / "a", tmp_path / "b"
    quiet.mkdir(), busy.mkdir()
    (quiet / "source.xml").write_bytes(_xml([(30 * b + 1, "路过") for b in range(5)]))
    raw = '[[0,1,25,16777215,0,0,0,"",0,2]]'
    (busy / "source.xml").write_bytes(("<i>" + "".join(f'<d p="{t},1,25,16777215,0,0,7,0" raw=\'{raw}\'>口令</d>'
                                                        for t in range(1, 29)) + "</i>").encode())
    assert ds.session_danmaku_density([quiet, busy]).baseline == 1.0
    (busy / "source.xml").write_bytes(_xml([(1 + i, "打call") for i in range(20)] + [(31 + i, "好好笑") for i in range(3)]))
    assert ds.session_danmaku_density([quiet, busy]) == SessionDensity(1.0, 6.0)      # 1,1,1,1,1 and 3; at least six
    assert ds.session_danmaku_density([tmp_path / "none"]) is None

def test_reading_aids_name_only_people_something_names_and_never_rewrite():
    block = da.reading_aids("为什么他们在说难听难听\n理论上不需要了\n我一直在想", ["南町和七宝", "ntnt太坏了"])
    assert "- 南町（也写作／也叫：" in block and "「难听」和「南町」同音" in block
    assert "字幕里的「理论上」有时是「李豆沙」被听错" in block and "不改字幕" in block
    assert "亦枝" not in block               # 一直 sounds like 亦枝 too; nobody names her here
    assert da.same_sound("难听难听，也不难听", ["南町", "Nightin", "礼"]) == {"南町": ["难听"]}
    assert da.reading_aids("今天天气不错", []) is None


def _pool():
    first = _drawn("semantictalk_676950_759940_11242230", "22966160_20261124-22-30-04", 676950, 759940, ["relation"], story="gag")
    again = _drawn("semantictalk_192270_422230_11242300", "22966160_20261124-23-00-08", 192270, 422230, [], story="gag")
    other = _drawn("semantictalk_925960_1156130_11242300", "22966160_20261124-23-00-08", 925960, 1156130, ["mishap"])
    song = dict(_drawn("semanticsong_1_2_11242300", "22966160_20261124-23-00-08", 1, 2, []), kind="song")
    for clip, text in ((first, "礼墨是李豆沙的宠物"), (again, "礼墨的宠物也是李豆沙的宠物"), (other, "你不是43吗"), (song, "啦啦啦")):
        clip["text"] = "\n".join([text] * 20)
    return [again, other, song, first]


def test_the_days_candidates_on_one_matter_are_marked_and_the_answer_is_kept(tmp_path):
    pool = _pool()
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return json.dumps({"groups": [{"members": [1, 2], "theme": "谁是谁的宠物", "why": "后一条接着前一条的结论"}]}, ensure_ascii=False)

    record = dt.assign(pool, tmp_path, ask)
    lead = "semantictalk_676950_759940_11242230"
    assert record["groups"] == [{"members": [lead, "semantictalk_192270_422230_11242300"], "theme": "谁是谁的宠物",
                                 "why": "后一条接着前一条的结论"}]
    assert [e.get("topic") for e in pool] == [lead, None, None, lead] and pool[0]["topic_theme"] == "谁是谁的宠物"
    # in broadcast order, by the clock of the recording's name; songs are not asked about; what each was picked for, not its subtitles
    assert asked[0].index("候选 1（22:41 开始）") < asked[0].index("候选 2（23:03 开始）") < asked[0].index("候选 3（23:15 开始）")
    assert "候选 4" not in asked[0] and "礼墨的宠物也是李豆沙的宠物" not in asked[0] and "- 「那你晕吧」（反转）" in asked[0]
    assert "这个跟刚才那个一样" in asked[0] and "{candidates}" not in asked[0]
    assert dt.assign(pool, tmp_path, lambda p: pytest.fail("asked again")) == record
    assert dt.assign(pool[:1], tmp_path / "one", lambda p: pytest.fail("one candidate is not asked about"))["groups"] == []
    assert "topic" not in pool[0]            # marks of an earlier grouping do not outlive it


@pytest.mark.parametrize("answer", ['{"groups": [{"members": [1], "theme": "t"}]}', '{"groups": [{"members": [1, 9], "theme": "t"}]}',
                                    '{"groups": [{"members": [1, 2], "theme": ""}]}', "不是 JSON", '{"groups": "none"}',
                                    '{"groups": [{"members": [1, 2], "theme": "a"}, {"members": [2, 3], "theme": "b"}]}'])
def test_a_grouping_the_code_cannot_use_marks_nothing(tmp_path, answer):
    pool = _pool()
    with pytest.raises(dt.TopicsError):
        dt.assign(pool, tmp_path, lambda prompt: answer)
    assert not any("topic" in e for e in pool) and not (tmp_path / dt.FILE).exists()


def test_an_unusable_grouping_leaves_the_day_ungrouped_and_an_unanswered_call_holds_it(tmp_path):
    def unanswered(prompt):
        raise ds.RecallCallError("CPA_NOT_COMPLETE")

    assert dv._topics("2026-11-24", _pool(), tmp_path, lambda p: '{"groups": "none"}')["status"] == "UNUSABLE_ANSWER"
    held = dv._topics("2026-11-24", _pool(), tmp_path, unanswered)
    assert held["status"] == dv.TOPICS_CALL_FAILED and "CPA_NOT_COMPLETE" in held["detail"]
    assert dv._topics("2026-11-24", _pool(), tmp_path, lambda p: '{"groups": []}') == {"status": "OK", "groups": []}


def test_a_seated_group_is_joined_into_one_clip_from_its_members_own_cues(tmp_path, monkeypatch):
    """The pieces are the members' own first and last cue, in broadcast order: nothing is asked.  The joined clip's entry
    carries their key moments on the joined clock; the built record names every member so none is seated again."""
    monkeypatch.setattr(type(dv.layout()), "session_root", lambda self, date: ROOT)      # the (fake) media host's work directory
    adapters, remote, cpa = _adapters(tmp_path, [])
    lead, late = "semantictalk_192270_422230_11242300", "semantictalk_676950_759940_11242230"
    remote.files[f"{ROOT}/{lead}_m/compilation/asr/bcut.srt"] = _srt(_cues(12, step=2.0, length=1.8)).encode()
    members = []
    for cid, name, first, last in ((late, "22966160_20261124-22-30-04", 5, 8), (lead, "22966160_20261124-23-00-08", 12, 14)):
        directory = tmp_path / "discover" / name
        directory.mkdir(parents=True)
        (directory / "bcut.srt").write_text(_srt(_cues(40)), encoding="utf-8")
        start = (first - 1) * 3000
        members.append({"cid": cid, "candidate_id": cid[:-9], "kind": "talk", "hook": f"hook {first}", "start_ms": start,
                        "end_ms": (last - 1) * 3000 + 2000, "cue_ids": [str(n) for n in range(first, last + 1)],
                        "source": {"name": name, "path": f"/arc/2026-11-24/{name}.flv", "directory": str(directory)},
                        "peaks": [{"quote": "那你晕吧", "who": "李豆沙", "at_ms": start + 3000, "why": "反转"}]})
    entry = dict(members[1], topic=late, topic_theme="谁是谁的宠物", topic_why="后一条接着前一条的结论", topic_members=members)
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({"recording_date": "2026-11-24", "participants": [["李豆沙"]], "own_uploader_mids": [1]}))
    made = dv._build_joined(tmp_path / "task", load_session_config(session_file), adapters, entry)

    assert made["cid"] == f"{lead}_m" and [c["cid"] for c in made["covers"]] == [late, lead] and cpa.calls == []
    manifest = comp.load_manifest(tmp_path / "task/clips" / made["cid"])
    assert [(p["recording"], p["start_cue"], p["end_cue"], p["why"]) for p in manifest["pieces"]] == [
        ("22966160_20261124-22-30-04.flv", 5, 8, "hook 5"), ("22966160_20261124-23-00-08.flv", 12, 14, "hook 12")]
    assert manifest["theme"] == manifest["hook"] == "谁是谁的宠物" and made["duration_ms"] == manifest["duration_ms"]
    assert "/arc/2026-11-24/22966160_20261124-22-30-04.flv" in dict(remote.scripts)["compile-cut"]
    kept = json.loads((tmp_path / "task/clips" / made["cid"] / "compilation/selection.json").read_text(encoding="utf-8"))
    assert kept["decided_by"] == "discover_topics" and "cpa" not in kept
    joined = json.loads((tmp_path / "task/clips" / made["cid"] / "compilation/selection-result.json").read_text(encoding="utf-8"))["input_pool"][0]
    first_piece, second_piece = manifest["pieces"]
    assert joined["joined_from"] == [late, lead] and joined["topic_theme"] == "谁是谁的宠物"
    assert [p["at_ms"] for p in joined["peaks"]] == [
        3000 + members[0]["start_ms"] - first_piece["source_start_ms"],
        second_piece["compiled_start_ms"] + 3000 + members[1]["start_ms"] - second_piece["source_start_ms"]]
    state = json.loads((tmp_path / "task/state" / f"{made['cid']}.json").read_text(encoding="utf-8"))
    assert state["clip"]["source_name"] == f"compilation-{made['cid']}.flv" and state["clip"]["hook"] == "谁是谁的宠物"
    # seating then treats both members as made
    again = ds.seat_day("2026-11-24", [dict(_drawn(m["cid"], m["source"]["name"], m["start_ms"], m["end_ms"], ["relation"]))
                                       for m in members], built=[made])
    assert again.seated == [] and again.rejected == []


def test_members_whose_recordings_are_in_two_directories_are_not_joined(tmp_path):
    a = {"cid": "a_11242230", "source": {"name": "s1", "path": "/arc/2026-11-24/s1.flv", "directory": "/d1"}, "cue_ids": ["1"]}
    b = {"cid": "b_11250000", "source": {"name": "s2", "path": "/arc/2026-11-25/s2.flv", "directory": "/d2"}, "cue_ids": ["1"]}
    with pytest.raises(dv.DiscoverError, match="TOPIC_RECORDINGS_APART"):
        dv._build_joined(tmp_path, types.SimpleNamespace(recording_date="2026-11-24"), None,
                         dict(a, topic_theme="t", topic_members=[a, b]))


def test_the_focus_is_the_assets_own_words_and_a_channel_without_stories_has_none(monkeypatch):
    focus = cd.focus()
    assert "她只是旁观" in focus and "- 其他完整的事" in focus and "才选" in focus and "不分先后" in focus
    monkeypatch.setattr(cd, "load", lambda: None)
    with pytest.raises(cd.DrawsError):
        cd.focus()


def test_the_labelling_call_reads_whose_voice_each_line_is_and_a_failed_reading_says_so(tmp_path):
    """Before step 4 no line has a speaker: in a call of three the model credited her with a guest's line (11-23 21:41).
    The candidates' lines are marked by the host's voiceprint; a line it is unsure of is not marked."""
    from test_discover import card

    (tmp_path / "bcut.srt").write_text(SRT, encoding="utf-8")
    prepared = {"date": "2026-11-23", "segment": SEGMENT, "path": f"/arc/{SEGMENT}.flv", "size": 1, "mtime": "1",
                "directory": str(tmp_path), "remote_dir": "/remote/seg", "source_fp": "s", "xml": {"sha256": "x", "exists": False},
                "srt_sha256": "t"}

    def selector(cues, **_):
        return [_candidate("semantictalk_2000_6000", 2, 5, cues)], {"hooks": {"semantictalk_2000_6000": "石头"}, "scorecards": {
            "semantictalk_2000_6000": dict(card(4), start_cue=2, end_cue=5)}}

    asked, heard = [], []

    def hear(prep, directory, spans):
        heard.append(sorted(spans))
        return {"host_prob": {"2": 0.97, "3": 0.03, "4": 0.5}, "note": "按声纹标了说话人"}

    def ask(prompt, accepts=None):
        asked.append(prompt)
        return json.dumps({"labels": [row("semantictalk_2000_6000", ["collab"], story="person", role="participant", pin=None)]})

    doc = ds.recall_segment(prepared, tmp_path, ask=ask, selector=selector, hear=hear, density=None)
    assert heard == [["2", "3", "4", "5"]]          # the lines of the talk candidates, nothing else
    assert "#2 〔她〕为什么他们在说难听难听" in asked[0] and "#3 〔别人〕这里面哪个是难听难听啊" in asked[0]
    assert "#4 他们在说那个绿色" in asked[0] and "先看行首的声纹标记" in asked[0]
    assert "看点那一句就是标〔她〕的句子" in asked[0]   # her own marked line can pin the matter on her (11-23 20:11)
    assert doc["voiceprint"] == {"note": "按声纹标了说话人", "lines": 3}
    # a channel without a voiceprint workspace: labelled without marks, and the recall says why
    (tmp_path / "recall.json").unlink()
    doc = ds.recall_segment(prepared, tmp_path, ask=ask, selector=selector, density=None,
                            hear=lambda *_: {"host_prob": None, "note": "没有配置声纹工作区，字幕不标说话人"})
    plain = asked[-1]
    assert "〔" not in plain.split("候选：")[1] and "没有说话人标注" in plain and "标〔她〕" not in plain
    assert doc["voiceprint"]["note"].startswith("没有配置声纹工作区")
    # a reading that fails raises: the recording is left for the next run with its recall kept and nothing labelled,
    # since a label is kept by the asset's version and would never be asked again with the marks
    (tmp_path / "recall.json").unlink()

    def broken(*_):
        raise ds.DiscoverError("VOICEPRINT_FAILED", "rc=1")

    with pytest.raises(ds.DiscoverError, match="rc=1"):
        ds.recall_segment(prepared, tmp_path, ask=ask, selector=selector, hear=broken, density=None)
    kept = json.loads((tmp_path / "recall.json").read_text(encoding="utf-8"))
    assert "core_draws" not in kept["input_pool"][0] and cd.pending(kept["input_pool"])


def test_the_voiceprint_runs_once_per_recording_and_is_read_back_while_the_lines_are_the_same(tmp_path):
    from src.autoslice.manual_lane import discover_voices as dvo

    runs = []

    class Remote:
        def put(self, local, remote_path):
            pass

        def run_script(self, *, name, body, timeout):
            runs.append(body)
            return types.SimpleNamespace(rc=0, stdout="SPEAKER_V8_DONE\n", stderr="")

        def get(self, remote_path, local):
            units = json.loads((tmp_path / dvo.WORKSPACE / "units.json").read_text(encoding="utf-8"))["units"]
            probs = {"2": 0.9, "3": 0.1}
            local.write_text(json.dumps({"predictions": [{"id": u["id"], "ok": True, "probs": {"李豆沙": probs[str(u["cue"])]}}
                                                         for u in units]}), encoding="utf-8")

    prepared = {"segment": SEGMENT, "path": f"/arc/{SEGMENT}.flv", "remote_dir": "/remote/seg"}
    hear = dvo.hearer(Remote())
    first = hear(prepared, tmp_path, {"2": (1000, 3600), "3": (4000, 5000)})
    assert first == {"host_prob": {"2": 0.9, "3": 0.1}, "note": "按声纹标了说话人"} and len(runs) == 1
    assert f"/arc/{SEGMENT}.flv" in runs[0] and "speaker_v8_run.sh" in runs[0]
    assert hear(prepared, tmp_path, {"2": (1000, 3600), "3": (4000, 5000)}) == first and len(runs) == 1
    assert [dvo.mark(p) for p in (0.9, 0.5, 0.1, None)] == ["她", "", "别人", ""]


def test_a_voiceprint_run_that_fails_raises_and_leaves_no_reading(tmp_path):
    from src.autoslice.manual_lane import discover_voices as dvo

    class Remote:
        def put(self, local, remote_path):
            pass

        def run_script(self, *, name, body, timeout):
            return types.SimpleNamespace(rc=1, stdout="", stderr="v8 timed out")

        def get(self, remote_path, local):
            pytest.fail("nothing to fetch")

    prepared = {"segment": SEGMENT, "path": f"/arc/{SEGMENT}.flv", "remote_dir": "/remote/seg"}
    with pytest.raises(ds.DiscoverError) as failed:
        dvo.hearer(Remote())(prepared, tmp_path, {"2": (1000, 3600)})
    assert failed.value.code == dvo.VOICEPRINT_FAILED and "v8 timed out" in str(failed.value)
    assert not (tmp_path / dvo.WORKSPACE / "pred.json").exists()


def _t(seconds):
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d},000"


def _recording(day, name, lines, *, step_s=10):
    """A selected recording of the day: its subtitles (one line every `step_s` seconds) and its recall's source facts."""
    directory = day / name
    directory.mkdir(parents=True)
    (directory / "bcut.srt").write_text("".join(
        f"{n}\n{_t(n * step_s)} --> {_t(n * step_s + 5)}\n{text}\n\n" for n, text in enumerate(lines, start=1)), encoding="utf-8")
    (directory / "recall.json").write_text(json.dumps({"source": {"date": "2026-11-24", "name": name, "path": f"/arc/{name}.flv",
                                                                  "directory": str(directory)}}), encoding="utf-8")
    return directory


def test_a_candidate_whose_cause_is_in_the_previous_recording_begins_with_it(tmp_path):
    """11-24 23:00: her stance on an idol's love affair went on from the recording of 22:30; labelled not followable
    (「开头讨论的“她”具体是谁…片内没有补出」), three replays of three dropped it.  It is given the lines before it, from the
    previous recording too; with them it can be followed, and its clip begins with them."""
    day = tmp_path / "2026-11-24"
    # 22:30:04 + 1790 s ends at 23:00:04, four seconds before the next recording begins
    _recording(day, "22966160_20261124-22-30-04", [f"前一段{n}" for n in range(1, 179)] + ["我不想知道他的私事", "她结婚了"])
    _recording(day, "22966160_20261124-23-00-08", ["她就去结婚了", "所以我不想看", "下一件事"])
    lost = _drawn("lost", "22966160_20261124-23-00-08", 10_000, 25_000, ["relation", "yuri"], story="person", understood=False)
    lost["cue_ids"] = ["1", "2"]
    plain = _drawn("plain", "22966160_20261124-23-00-08", 30_000, 40_000, ["relation"])
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return json.dumps({"leads": [{"id": 1, "from": "P57", "understood": True, "missing": "", "why": "交代了是哪位偶像结婚"}]},
                          ensure_ascii=False)

    record = dl.assign([lost, plain], day, ask, ds._cues)
    # back as far as a clip begun there is ten minutes (585 s before it): the previous recording's last lines (its clock:
    # 23:00:08 is 1804 s, so from 1229 s on), none of its own
    head = asked[0].split("候选 1\n")[1].split("片内的字幕")[0]
    assert "#P1 前一段123" in head and "前一段122" not in head
    assert "#P57 我不想知道他的私事" in head and "#P58 她结婚了" in head and "#P59" not in head
    assert "#1 她就去结婚了" in asked[0] and "候选 2" not in asked[0] and "前因在片外" in asked[0]
    assert [(s["source"]["name"], s["cue_ids"]) for s in lost["lead_in"]] == [
        ("22966160_20261124-22-30-04", ["179", "180"])]
    assert lost["core_draws"]["understood"] is True and lost["core_draws"]["missing_before"] == "前因在片外"
    assert record["leads"] == [{"cid": "lost", "applied": True, "from": ["lost_lead1"], "note": "交代了是哪位偶像结婚"}]
    # seated as one clip with its lead-in, in broadcast order; the lead-in's stretch is taken too
    seating = ds.seat_day("2026-11-24", [lost, plain], built=[])
    made = next(e for e in seating.seated if e["cid"] == "lost")
    assert [m["cid"] for m in made["topic_members"]] == ["lost_lead1", "lost"] and made["topic_theme"] == "lost"
    # read back while the prompt is the same; a previous recording that ends long before is not reached into
    again = [dict(lost, core_draws=dict(lost["core_draws"], understood=False, missing="前因在片外")), plain]
    dl.assign(again, day, lambda p: pytest.fail("asked again"), ds._cues)
    assert again[0]["lead_in"] == lost["lead_in"]
    gap = tmp_path / "gap"
    _recording(gap, "22966160_20261124-22-00-04", [f"早{n}" for n in range(1, 10)])
    _recording(gap, "22966160_20261124-23-00-08", ["她就去结婚了", "所以我不想看"])
    alone = _drawn("alone", "22966160_20261124-23-00-08", 10_000, 25_000, ["relation"], understood=False)
    alone["cue_ids"] = ["1", "2"]
    assert dl.assign([alone], gap, lambda p: pytest.fail("nothing before it to give"), ds._cues)["leads"] == [
        {"cid": "alone", "applied": False, "note": "片前没有字幕可接"}]
    assert ds.seat_day("2026-11-24", [alone], built=[]).rejected[0]["reason"] == ds.NOT_UNDERSTOOD


def test_a_lead_in_that_is_still_not_followable_or_too_long_is_not_given(tmp_path):
    day = tmp_path / "2026-11-24"
    _recording(day, "22966160_20261124-23-00-08", [f"句{n}" for n in range(1, 80)])
    short = _drawn("short", "22966160_20261124-23-00-08", 400_000, 410_000, ["relation"], understood=False)
    short["cue_ids"] = ["41"]
    # 598 s long: the line it is given before it (190-195 s) ends after it begins, so with it the clip is 603 s
    long = _drawn("long", "22966160_20261124-23-00-08", 192_000, 790_000, ["relation"], understood=False)
    long["cue_ids"] = ["21"]
    answer = {"leads": [{"id": 1, "from": "P1", "understood": True, "missing": "", "why": "w"},
                        {"id": 2, "from": "P1", "understood": False, "missing": "前因更早", "why": ""}]}
    record = dl.assign([short, long], day, lambda p: json.dumps(answer, ensure_ascii=False), ds._cues)
    assert {r["cid"]: r["applied"] for r in record["leads"]} == {"long": False, "short": False}
    assert "超过 600 秒" in next(r["note"] for r in record["leads"] if r["cid"] == "long")
    assert "前因更早" in next(r["note"] for r in record["leads"] if r["cid"] == "short")
    assert "lead_in" not in short and "lead_in" not in long
    with pytest.raises(dl.LeadInError):
        dl.assign([short], tmp_path / "2026-11-24", lambda p: '{"leads": [{"id": 1, "from": "P999", "understood": true}]}', ds._cues)


def test_an_unusable_lead_in_answer_leaves_the_day_without_and_an_unanswered_call_holds_it(tmp_path):
    day = tmp_path / "2026-11-24"
    _recording(day, "22966160_20261124-23-00-08", [f"句{n}" for n in range(1, 40)])
    lost = _drawn("lost", "22966160_20261124-23-00-08", 200_000, 210_000, ["relation"], understood=False)
    lost["cue_ids"] = ["21"]

    def unanswered(prompt):
        raise ds.RecallCallError("CPA_NOT_COMPLETE")

    assert dv._lead_ins("2026-11-24", [lost], day, lambda p: "不是 JSON")["status"] == "UNUSABLE_ANSWER"
    held = dv._lead_ins("2026-11-24", [lost], day, unanswered)
    assert held["status"] == dv.LEAD_IN_CALL_FAILED and "CPA_NOT_COMPLETE" in held["detail"]
    assert dv._lead_ins("2026-11-24", [], day, unanswered) == {"status": "OK", "leads": []}     # nothing to ask
