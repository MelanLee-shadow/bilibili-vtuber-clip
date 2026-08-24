"""Hash-bound, candidate-private projection for the C9 watched-video ruling."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "assets/lidousha/fastlane_c9_private"
RECEIPT = BASE / "auto_143025_1112_1285.foreign-video-source-action.v1.json"
SRT = BASE / "auto_143025_1112_1285.reviewed.srt"


def _canonical_sha256(document: dict) -> str:
    unsigned = dict(document)
    unsigned.pop("self_sha256")
    data = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_c9_source_action_projection_is_exhaustive_and_preserves_uncertain_cues() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    dropped = receipt["complete_in_video_source_cues"]
    frozen = receipt["frozen_source_cues"]
    unresolved = receipt["unresolved_or_mixed_source_cues"]

    assert receipt["candidate_id"] == "auto_143025_1112_1285"
    assert receipt["upload"] is False
    assert set(dropped).isdisjoint(frozen)
    assert sorted(dropped + frozen) == list(range(1, 67))
    assert set(unresolved).issubset(frozen)
    assert {5, 35, 44, 47, 48, 49, 52}.issubset(unresolved)
    assert receipt["exhaustive_projection"]["reviewed_cue_count"] == 66 - len(dropped)
    assert receipt["self_sha256"] == _canonical_sha256(receipt)

    blocks = [block for block in SRT.read_text(encoding="utf-8").strip().split("\n\n") if block]
    assert len(blocks) == 41
    assert [int(block.splitlines()[0]) for block in blocks] == list(range(1, 42))
    assert "正在看这个哦哈" not in SRT.read_text(encoding="utf-8")
    assert "就是有点影响到。哇，好漂亮" in SRT.read_text(encoding="utf-8")
    assert "这哦不，不止任何人" in SRT.read_text(encoding="utf-8")
    assert "但是稍微不是很喜欢" in SRT.read_text(encoding="utf-8")
    assert "sha256:" + hashlib.sha256(SRT.read_bytes()).hexdigest() == receipt["exhaustive_projection"]["reviewed_srt_sha256"]
