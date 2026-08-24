"""Hash-bound, candidate-private projection for the C9 watched-video ruling."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "assets/lidousha/fastlane_c9_private"
RECEIPT = BASE / "auto_143025_1112_1285.foreign-video-source-action.v1.json"
SRT = BASE / "auto_143025_1112_1285.reviewed.srt"
SOURCE_SRT = BASE / "auto_143025_1112_1285.source.speaker-final.srt"


def _canonical_sha256(document: dict) -> str:
    unsigned = dict(document)
    unsigned.pop("self_sha256")
    data = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _blocks(path: Path) -> list[list[str]]:
    return [block.splitlines() for block in path.read_text(encoding="utf-8").strip().split("\n\n") if block]


def test_c9_source_action_projection_is_exhaustive_and_preserves_uncertain_cues() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    dropped = receipt["complete_in_video_source_cues"]
    frozen = receipt["frozen_source_cues"]
    unresolved = receipt["unresolved_or_mixed_source_cues"]

    assert receipt["candidate_id"] == "auto_143025_1112_1285"
    assert receipt["upload"] is False
    actions = receipt["cue_actions"]
    assert [action["cue"] for action in actions] == list(range(1, 67))
    assert {action["source_classification"] for action in actions} == {
        "IN_VIDEO", "HOST_LIVE", "MIXED", "UNCERTAIN"
    }
    assert all(action["action"] == "DROP" for action in actions if action["source_classification"] == "IN_VIDEO")
    assert all(action["action"] == "RETAIN_FROZEN" for action in actions if action["source_classification"] != "IN_VIDEO")
    rows = receipt["drop_rows"]
    assert [row["cue"] for row in rows] == dropped
    assert all(row["no_host_overlap"] is True for row in rows)
    assert all("host" not in row["windows"] for row in rows)
    assert set(dropped).isdisjoint(frozen)
    assert sorted(dropped + frozen) == list(range(1, 67))
    assert set(unresolved).issubset(frozen)
    assert {5, 35, 44, 47, 48, 49, 52}.issubset(unresolved)
    assert receipt["exhaustive_projection"]["reviewed_cue_count"] == 66 - len(dropped)
    assert receipt["self_sha256"] == _canonical_sha256(receipt)

    source_blocks = _blocks(SOURCE_SRT)
    reviewed_blocks = _blocks(SRT)
    assert "sha256:" + hashlib.sha256(SOURCE_SRT.read_bytes()).hexdigest() == receipt["source_binding"]["speaker_final_srt_sha256"]
    assert len(source_blocks) == 66
    assert [int(block[0]) for block in source_blocks] == list(range(1, 67))
    source_by_index = {int(block[0]): block for block in source_blocks}
    for row in rows:
        timing = source_by_index[row["cue"]][1]
        start, end = timing.split(" --> ")
        def milliseconds(value: str) -> int:
            hour, minute, second_ms = value.split(":")
            second, millisecond = second_ms.split(",")
            return ((int(hour) * 60 + int(minute)) * 60 + int(second)) * 1000 + int(millisecond)
        assert (row["start_ms"], row["end_ms"]) == (milliseconds(start), milliseconds(end))
    assert len(reviewed_blocks) == 41
    assert [int(block[0]) for block in reviewed_blocks] == list(range(1, 42))
    expected = [block[1:] for block in source_blocks if int(block[0]) not in dropped]
    assert [block[1:] for block in reviewed_blocks] == expected
    assert "正在看这个哦哈" not in SRT.read_text(encoding="utf-8")
    assert "就是有点影响到。哇，好漂亮" in SRT.read_text(encoding="utf-8")
    assert "这哦不，不止任何人" in SRT.read_text(encoding="utf-8")
    assert "但是稍微不是很喜欢" in SRT.read_text(encoding="utf-8")
    assert "sha256:" + hashlib.sha256(SRT.read_bytes()).hexdigest() == receipt["exhaustive_projection"]["reviewed_srt_sha256"]
