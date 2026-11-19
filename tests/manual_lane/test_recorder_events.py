"""Recorder input through evidence writing, attachment and written-name consumers."""
import json
import xml.etree.ElementTree as ET

import pytest
from fakes import FakeRemote, IdCpa, make_ctx

from src.autoslice.manual_lane import compilation, evidence_attach, name_consistency
from src.autoslice.manual_lane.recorder_events import recorder_events
from src.autoslice.manual_lane.step3_evidence import _danmaku_lines


XML = b'''<i><d p="1002,1,25,1">ordinary &amp; unchanged</d>
<sc ts="837.457" user="SC_sender">a delayed question</sc>
<gift ts="1001" user="masked***" giftname="gift" giftcount="3"/>
<guard ts="1003" user="guard_sender" level="3" count="1"/>
<gift ts="879" user="too_old" giftname="gift" giftcount="1"/>
<sc ts="99" user="too_old_sc">not eligible</sc></i>'''


def test_native_events_reach_written_evidence_and_consumers(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa(), danmaku_xml=XML)
    ctx.state.data["clip"]["start_ms"] = 1000000
    rows = _danmaku_lines(ctx, 60)
    assert [r for r in rows if "type" not in r] == [{"t_s": 2.0, "text": "ordinary & unchanged"}]
    typed = [r for r in rows if "type" in r]
    # paid events reach back to the start of the broadcast, however old: the old SC (ts 99) is kept too
    assert [r["type"] for r in typed] == ["sc", "gift", "guard", "gift", "sc"]
    assert typed[4]["sender"] == "too_old_sc" and typed[4]["t_s"] == -901
    assert typed[0]["t_s"] == -162.54
    assert typed[1]["sender"] == "masked***" and typed[1]["count"] == 3
    assert typed[2]["gift_name"] == "舰长"
    assert typed[3]['sender'] == 'too_old' and typed[3]['t_s'] == -121
    assert all(r["source_sha256"] and r["source_event_id"] for r in typed)
    assert json.loads((ctx.clip_dir / "danmaku/lines.json").read_text()) == rows
    pool = name_consistency.danmaku_pool(ctx.clip_dir)
    assert "SC_sender" in pool and "masked***" in pool and "guard_sender" in pool
    cue = {"start_s": 0., "end_s": 4., "text": "SC_sender"}
    attached = evidence_attach.danmaku_view(evidence_attach.danmaku_for_cue(cue, rows, ["SC_sender"]))
    assert any("[sc] 发送者：SC_sender；正文：a delayed question" in s for s in attached)
    # A gift label/name is never mistaken for a read-aloud comment.
    assert evidence_attach.read_aloud_hit({"start_s": 5., "end_s": 9., "text": "guard_sende"}, [typed[2]]) is None


def test_compilation_preserves_each_event_type_and_its_clock(tmp_path):
    manifest = {"pieces": [{"recording": "r.flv", "source_start_ms": 1000000,
                             "source_end_ms": 1060000, "compiled_start_ms": 0}]}
    remapped = compilation.remap_danmaku(manifest, {"r.flv": XML.decode()})
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa(), danmaku_xml=remapped.encode())
    ctx.state.data["clip"]["start_ms"] = 0
    rows = _danmaku_lines(ctx, 60)
    assert [(r["type"], r["sender"], r["t_s"]) for r in rows if "type" in r] == [
        ("sc", "SC_sender", -162.54), ("gift", "masked***", 1.), ("guard", "guard_sender", 3.)]
    assert "too_old" not in remapped


def test_an_sc_sent_in_a_cut_out_stretch_is_read_in_the_next_piece():
    # 11-18 1086800_trim: the SC was sent 0.45 s after the first piece ends and read in the second
    xml = '<i><sc ts="1222.11" user="u">陪主包唱南町歌</sc><d p="1225,1,25,1">剪掉那段的弹幕</d></i>'
    manifest = {"pieces": [{"recording": "r.flv", "source_start_ms": 1086800, "source_end_ms": 1221660,
                            "compiled_start_ms": 0},
                           {"recording": "r.flv", "source_start_ms": 1232815, "source_end_ms": 1253010,
                            "compiled_start_ms": 134867}]}
    remapped = compilation.remap_danmaku(manifest, {"r.flv": xml})
    [sc] = recorder_events(remapped.encode())
    assert sc["text"] == "陪主包唱南町歌" and round(sc["t_s"], 3) == 124.162   # before the second piece starts
    assert "剪掉那段的弹幕" not in remapped                                       # an ordinary comment stays out


@pytest.mark.parametrize("payload", [b'<i><sc ts="NaN" user="x">x</sc></i>',
                                     b'<i><gift ts="0" user="x"/></i>', b'<i>'])
def test_invalid_recorder_inputs_are_not_silently_dropped(payload):
    with pytest.raises((ValueError, KeyError, ET.ParseError)):
        recorder_events(payload)
