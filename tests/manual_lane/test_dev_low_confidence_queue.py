"""The listen page gets the low-confidence doubts only in development (TASK-5): default off, switch on for dev."""

from __future__ import annotations

from src.autoslice.manual_lane import listen

BCUT = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "句子甲"}, {"id": 2, "start_s": 2.0, "end_s": 4.0, "text": "句子乙"}]
FINAL = [{"id": 1, "start_s": 0.0, "end_s": 2.0, "text": "句子甲"}, {"id": 2, "start_s": 2.0, "end_s": 4.0, "text": "句子乙"}]
DOUBTS = [{"id": "d001", "cue_id": 1, "current": "句子甲", "state": "FIT_CONTEXT", "final_text": "句子甲",
           "affects_meaning": True, "confidence": "低", "readings": {"audio:38m@P0": "句子丙"}},
          {"id": "d002", "cue_id": 2, "current": "句子乙", "state": "KEEP", "final_text": "句子乙",
           "affects_meaning": False, "confidence": "高", "readings": {}}]


def _ids(items):
    return [i.get("doubt_id") or i.get("id") for i in items]


def test_low_confidence_doubts_stay_off_the_listen_page_by_default():
    items = listen.build_items(cid="c", doubts=DOUBTS, bcut_cues=BCUT, final_cues=FINAL, cut_len=10.0)
    assert items == []


def test_the_development_switch_queues_the_low_confidence_doubt_only():
    items = listen.build_items(cid="c", doubts=DOUBTS, bcut_cues=BCUT, final_cues=FINAL, cut_len=10.0,
                               dev_low_confidence=True)
    assert len(items) == 1 and "d001" in str(items[0])
