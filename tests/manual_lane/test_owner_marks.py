"""The owner's marks in the danmaku are clip requests (「我在弹幕里标记了一处地点…我要自动流水线能切出这一块来」)."""
import json
import types

from src.autoslice.manual_lane import discover as dv, discover_select as ds

import test_discover as td
from test_discover import _no_core_draws  # noqa: F401 - the autouse fixture: these tests are about the marks

OWNER, OTHER = 10001, 12345
RULES = {"owner_mids": [OWNER], "mark_text_contains": ["标记"], "before_ms": 5000, "after_ms": 1000}


def xml(*danmaku):
    rows = "".join(f'<d p="{at},1,25,16777215,1794753329311,0,{mid},0" user="u">{text}</d>' for at, mid, text in danmaku)
    return f'<?xml version="1.0" encoding="utf-8"?><i>{rows}</i>'.encode()


def test_only_the_owners_danmaku_with_a_mark_word_is_a_mark(tmp_path):
    path = tmp_path / "source.xml"
    path.write_bytes(xml((299.4, OWNER, "初生牛犊不怕虎"), (835.2, OWNER, "标记了一处地点"), (836.0, OTHER, "标记了一处地点")))
    assert ds.owner_marks(path, RULES) == [{"at_ms": 835200, "text": "标记了一处地点", "mid": OWNER}]
    assert ds.owner_marks(path, None) == []



def test_the_default_channel_names_its_own_account():
    rules = ds.owner_mark_rules()
    assert rules["owner_mids"] and all(type(mid) is int and mid > 0 for mid in rules["owner_mids"])
    assert rules["mark_text_contains"]

def test_a_marked_candidate_is_seated_whatever_its_score_and_the_quota():
    strong = [td.entry(f"s{n}", f"seg{n}", 0, 500, level=4) for n in range(5)]
    weak = dict(td.entry("weak", "segX", 0, 500, confidence=0.5, level=0), owner_mark={"at_ms": 400})
    inside = td.entry("inside", "segX", 100, 300, level=4)
    song = dict(td.entry("song", "segY", 0, 500, kind="song"), owner_mark={"at_ms": 10})
    seating = ds.seat_day(td.DATE, [*strong, weak, inside, song], built=[])
    assert [e["cid"] for e in seating.seated] == ["weak", "s0", "s1", "s2", "s3", "s4"]      # outside the five seats
    assert seating.seated[0]["seat_reason"] == ds.OWNER_MARK and seating.policy["owner_marked"] == 1
    assert {r["cid"]: r["reason"] for r in seating.rejected} == {"inside": ds.OVERLAPS_SEATED, "song": ds.OWNER_MARK_NOT_TALK}
    built = [{"cid": "old", "segment": "segX", "start_ms": 0, "end_ms": 600}]
    again = ds.seat_day(td.DATE, [weak], built)
    assert again.seated == [] and again.rejected[0]["reason"] == ds.OWNER_MARK_ALREADY_BUILT


class MarkedHost(td.Host):
    def __call__(self, name, body, remote):
        result = super().__call__(name, body, remote)
        if name.startswith("disc-xml-"):
            segment = name.split("-", 2)[2]
            rd = f"{td.dm.layout().session_root(td.DATE)}/discover/{segment}"
            remote.files[f"{rd}/source.xml"] = xml((1.5, OWNER, "标记了一处地点"))
        return result


def run(tmp_path, monkeypatch, around):
    monkeypatch.setattr(ds, "owner_mark_rules", lambda: RULES)
    calls = []

    def selector(cues, **kwargs):
        if len(cues) == 6:                               # the whole recording: candidates at 2-5 s and 5-7 s (none holds 1.5 s)
            return td.selector(cues, **kwargs)
        calls.append([c.cue_id for c in cues])           # the recall around the mark
        if not around:
            return [], {"hooks": {}, "scorecards": {}}
        cid = "semantictalk_1000_3000"
        found = types.SimpleNamespace(anchor=types.SimpleNamespace(candidate_id=cid), content_type_hint="talk",
                                      cues=[types.SimpleNamespace(cue_id=c.cue_id, text=c.text) for c in cues],
                                      boundary=types.SimpleNamespace(resolved_start_ms=1000, resolved_end_ms=3000,
                                                                     start_boundary_score=0.5))
        return [found], {"hooks": {cid: "标记"}, "scorecards": {cid: None}}

    adapters = td.Adapters(remote=td.FakeRemote(on_script=MarkedHost()), cpa=None, danmaku=td.FakeDanmaku())
    report = dv.discover(task=tmp_path / "task", adapters=adapters, selector=selector, dates=[td.DATE])
    return report, calls


def test_a_mark_no_candidate_holds_gets_a_recall_around_it_and_is_made(tmp_path, monkeypatch):
    report, calls = run(tmp_path, monkeypatch, around=True)
    assert calls == [["1", "2"], ["1", "2"]]   # cues within 5 s before and 1 s after 1.5 s, once per recording
    day = dv.load_ledger(tmp_path / "task")["days"][td.DATE]
    assert "semantictalk_1000_3000_11092130" in day["selection"]["seated"] and day["selection"]["policy"]["owner_marked"] == 2
    assert (tmp_path / "task/state/semantictalk_1000_3000_11092130.json").exists() and not report.failures
    recall = json.loads(next((tmp_path / "task").rglob("22966160_20261109-21-30-00/recall.json")).read_text())
    assert recall["owner_marks"]["tied"][0]["candidate_id"] == "semantictalk_1000_3000"
    run(tmp_path, monkeypatch, around=True)
    assert calls == [["1", "2"], ["1", "2"]]   # the same marks are not recalled again


def test_a_mark_still_not_held_is_reported_not_dropped(tmp_path, monkeypatch):
    report, _ = run(tmp_path, monkeypatch, around=False)
    assert {f["code"] for f in report.failures} == {ds.OWNER_MARK_NOT_COVERED}
    assert dv.load_ledger(tmp_path / "task")["days"][td.DATE]["status"] == dv.PARTIAL
