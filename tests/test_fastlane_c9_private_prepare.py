"""C9 must not turn an unsealed visual/audio judgment into subtitle edits."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BLOCKER = ROOT / "assets/lidousha/fastlane_c9_private/auto_143025_1112_1285.foreign-video-truth-blocker.v1.json"
OVERRIDE = ROOT / "assets/lidousha/subtitle_text_overrides/auto_143025_1112_1285.text.v1.json"


def _canonical_sha256(document: dict) -> str:
    unsigned = dict(document)
    unsigned.pop("self_sha256")
    encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_c9_foreign_video_truth_is_sealed_as_a_blocker_until_source_actions_exist() -> None:
    document = json.loads(BLOCKER.read_text(encoding="utf-8"))

    assert document["candidate_id"] == "auto_143025_1112_1285"
    assert document["outcome"] == "BLOCKED_UNSEALED_FOREIGN_VIDEO_CUE_TRUTH"
    assert document["upload"] is False
    inventory = document["cue_inventory"]
    assert inventory["cue_count"] == 66
    assert inventory["cue_indexes"] == list(range(1, 67))
    assert inventory["dropped_cue_indexes"] == []
    assert inventory["frozen_cue_indexes"] == list(range(1, 67))
    assert document["retained_snapshot"]["current_srt_sha256"] != document["retained_snapshot"]["recorded_subtitle_sha256"]
    assert document["self_sha256"] == _canonical_sha256(document)
    assert not OVERRIDE.exists()
