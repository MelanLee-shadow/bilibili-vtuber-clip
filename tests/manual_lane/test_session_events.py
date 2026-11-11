"""Which recorded segments are one live session, and that each chat event of it is counted once."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.autoslice.manual_lane import session_events as se

T0 = datetime(2026, 11, 4, 15, 0, 0, tzinfo=timezone(timedelta(hours=8)))


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def xml(start_s, body: str = "") -> bytes:
    info = "" if start_s is None else f'<BililiveRecorderRecordInfo roomid="1" start_time="{at(start_s)}"/>'
    return f"<i>{info}{body}</i>".encode()


def dm(t, text, sent_ms=None):
    return f'<d p="{t},1,25,16777215,{sent_ms or int(t * 1000) + 1793000000000},0,abc,0">{text}</d>'


def sc(t, text, user="甲", server=None):
    raw = "" if server is None else f' raw="{{&quot;start_time&quot;:{server},&quot;id&quot;:7}}"'
    return f'<sc ts="{t}" user="{user}" uid="1" price="30" time="60"{raw}>{text}</sc>'


def gift(t, name="辣条", count=1, user="乙", server=None):
    raw = "" if server is None else f' raw="{{&quot;timestamp&quot;:{server},&quot;tid&quot;:&quot;9&quot;}}"'
    return f'<gift ts="{t}" user="{user}" uid="2" giftname="{name}" giftcount="{count}"{raw}/>'


def guard(t, user="丙", server=None):
    raw = "" if server is None else f' raw="{{&quot;start_time&quot;:{server}}}"'
    return f'<guard ts="{t}" user="{user}" uid="3" level="3" count="1"{raw}/>'


class Source:
    def __init__(self, files: dict):
        self.files = files
        self.fetched: list[str] = []

    def names(self, _directory):
        return list(self.files)

    def fetch(self, path):
        self.fetched.append(path)
        value = self.files[path.rsplit("/", 1)[-1]]
        if isinstance(value, Exception):
            raise value
        return value


def texts(rows):
    return sorted(row["text"] for row in rows)


def test_two_lives_of_one_day_stay_apart():
    afternoon = xml(0, dm(100, "下午弹幕") + sc(200, "下午SC"))
    evening = xml(3 * 3600, dm(100, "晚上弹幕") + sc(200, "晚上SC"))
    source = Source({"a.xml": afternoon, "b.xml": evening})
    rows, unknown = se.load(source, "d", "b", evening)
    assert unknown == [] and texts(rows) == ["晚上SC", "晚上弹幕"]
    rows, unknown = se.load(source, "d", "a", afternoon)
    assert unknown == [] and texts(rows) == ["下午SC", "下午弹幕"]


def test_a_sparse_five_minute_gap_inside_one_live_is_still_one_live():
    first = xml(0, dm(100, "前段") + sc(1800, "前段尾SC", server=1000))
    second = xml(1800 + 300, dm(10, "后段") + gift(20, server=2000))
    source = Source({"a.xml": first, "b.xml": second})
    rows, unknown = se.load(source, "d", "b", second)
    assert unknown == []
    by_text = {row["text"]: row["t_s"] for row in rows}
    assert by_text["前段"] == pytest.approx(100 - 2100) and by_text["前段尾SC"] == pytest.approx(1800 - 2100)
    assert by_text["后段"] == 10 and len(rows) == 4


def test_the_gap_limit_is_measured_against_the_whole_chain_not_only_the_anchor():
    a = xml(0, dm(1700, "甲"))                  # spans 0 .. 1700
    b = xml(2800, dm(1700, "乙"))               # spans 2800 .. 4500, 1100 s after a
    c = xml(5600, dm(1, "丙"))                  # 1100 s after b, far more than the limit after a
    far = xml(5601 + 5000, dm(1, "远"))
    source = Source({"a.xml": a, "b.xml": b, "c.xml": c, "d.xml": far})
    rows, _ = se.load(source, "d", "a", a)
    assert texts(rows) == ["丙", "乙", "甲"]


def test_overlapping_recorders_count_each_event_once_at_the_earlier_time():
    stamp = 1793797189223
    one = xml(0, dm(100.0, "同一条", stamp) + dm(100.0, "同一条", stamp + 5)
              + sc(120, "同SC", server=555) + gift(130, server=556) + guard(140, server=557))
    two = xml(10, dm(89.7, "同一条", stamp) + dm(300, "只在二号", stamp + 99)
              + sc(110.4, "同SC", server=555) + gift(120, server=556) + guard(130, server=557))
    source = Source({"one.xml": one, "two.xml": two})
    rows, unknown = se.load(source, "d", "one", one)
    assert unknown == []
    assert sorted(row["text"] for row in rows if "type" not in row) == ["只在二号", "同一条", "同一条"]
    typed = [row for row in rows if "type" in row]
    assert [row["type"] for row in typed] == ["sc", "gift", "guard"]
    assert typed[0]["t_s"] == pytest.approx(120.0)      # one.xml's 120 is earlier than two.xml's 110.4 + 10 = 120.4
    assert min(row["t_s"] for row in rows if row["text"] == "同一条") == pytest.approx(99.7)
    assert len([row for row in rows if row["text"] == "同一条"]) == 2   # the shared one and the one sent at stamp + 5


def test_events_sent_twice_inside_one_segment_are_both_kept():
    one = xml(0, gift(10, server=5) + gift(10, server=5) + dm(1, "重", 1) + dm(1, "重", 1))
    two = xml(0, gift(10, server=5) + dm(1, "重", 1))
    rows, _ = se.load(Source({"one.xml": one, "two.xml": two}), "d", "one", one)
    assert len([r for r in rows if r.get("type") == "gift"]) == 2
    assert len([r for r in rows if r["text"] == "重"]) == 2


def test_stamped_events_with_different_server_seconds_are_different_events():
    one = xml(0, gift(10, server=5))
    two = xml(0, gift(10.5, server=6))
    rows, _ = se.load(Source({"one.xml": one, "two.xml": two}), "d", "one", one)
    assert len(rows) == 2


def test_without_a_server_second_equal_events_within_two_seconds_are_one():
    one = xml(0, sc(100, "无戳") + gift(200) + guard(300))
    two = xml(0, sc(101.9, "无戳") + gift(203) + guard(300.0))
    rows, _ = se.load(Source({"one.xml": one, "two.xml": two}), "d", "one", one)
    # sc 1.9 s apart and guard 0 s apart are one each; the gifts 3 s apart are two
    assert sorted((r["type"], r["t_s"]) for r in rows) == [("gift", 200.0), ("gift", 203.0), ("guard", 300.0),
                                                           ("sc", 100.0)]


def test_file_name_templates_do_not_decide_the_session():
    first = xml(0, dm(100, "第一段") + sc(1790, "尾"))
    second = xml(1800, dm(5, "第二段"))
    names = {"录制-22966160-20261104-195933-917-毛绒毛绒.xml": first, "22966160_20261104-20-29-37.xml": second}
    rows, unknown = se.load(Source(names), "d", "22966160_20261104-20-29-37", second)
    assert unknown == [] and texts(rows) == ["尾", "第一段", "第二段"]
    rows, _ = se.load(Source(names), "d", "录制-22966160-20261104-195933-917-毛绒毛绒", first)
    assert texts(rows) == ["尾", "第一段", "第二段"]


def test_an_unreadable_segment_is_unknown_cannot_bridge_a_gap_and_blocks_the_cache(tmp_path):
    a = xml(0, dm(1000, "甲"))
    bad = xml(None, dm(1, "没有开始时间"))
    broken = ValueError("cannot read")
    near = xml(1000 + 600, dm(1, "近"))
    far = xml(3200, dm(1, "隔着坏段的远"))
    source = Source({"a.xml": a, "bad.xml": bad, "broken.xml": broken, "far.xml": far, "near.xml": near})
    cache = tmp_path / "session.json"
    rows, unknown = se.load(source, "d", "a", a, cache)
    assert {u["segment"] for u in unknown} == {"bad", "broken"}
    assert texts(rows) == ["甲", "近"]    # far starts 1600 s after near ends; the unreadable ones carry no time to bridge it
    assert not cache.exists()


def test_the_unreadable_segment_does_not_hide_the_ones_around_it():
    a = xml(0, dm(100, "甲"))
    b = xml(500, dm(1, "乙"))
    source = Source({"a.xml": a, "x.xml": xml(None), "b.xml": b})
    rows, unknown = se.load(source, "d", "a", a)
    assert texts(rows) == ["乙", "甲"] and [u["segment"] for u in unknown] == ["x"]


@pytest.mark.parametrize("names", [[], ["r.xml"], ["other.xml"]])
def test_a_lone_or_unlisted_anchor_reads_like_its_own_rows(names):
    anchor = xml(0, dm(5, "本段") + sc(3, "SC", server=1) + gift(4))
    files = {n: (anchor if n == "r.xml" else xml(10, dm(1, "别人"))) for n in names}
    rows, unknown = se.load(Source(files), "d", "r", anchor)
    assert rows == se.segment_rows(anchor) and unknown == []


def test_the_second_call_is_served_from_the_cache_with_the_merged_result(tmp_path):
    stamp = 1793797189223
    one = xml(0, dm(100, "同一条", stamp) + sc(120, "同SC", server=555))
    two = xml(10, dm(90, "同一条", stamp) + sc(110, "同SC", server=555) + dm(5, "另一条", 1))
    source = Source({"one.xml": one, "two.xml": two})
    cache = tmp_path / "x" / "session.json"
    first = se.load(source, "d", "one", one, cache)
    fetched = len(source.fetched)
    assert fetched == 1 and first[1] == []
    assert len(first[0]) == 3
    second = se.load(source, "d", "one", one, cache)
    assert second == first and len(source.fetched) == fetched
    assert json.loads(cache.read_text(encoding="utf-8"))["rows"] == first[0]
