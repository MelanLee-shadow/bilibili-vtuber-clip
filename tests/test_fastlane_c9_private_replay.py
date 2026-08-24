from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.autoslice.fastlane_c9_private_replay import (
    FastlaneC9PrivateReplayError,
    materialize_c9_private_replay,
    validate_c9_source_action,
)


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = "auto_143025_1112_1285"


def test_c9_private_replay_is_a_create_only_no_upload_packet(tmp_path: Path) -> None:
    packet = materialize_c9_private_replay(root=ROOT, output_dir=tmp_path / CANDIDATE)

    assert packet["upload_allowed"] is False
    assert packet["dropped_source_cues"] == [10, 11, 12, 13, 15, 16, 17, 18, 19, 20, 21, 28, 29, 30, 31, 32, 36, 37, 38, 39, 40, 41, 42, 46, 50]
    assert (tmp_path / CANDIDATE / "reviewed.srt").read_bytes() == (
        ROOT / "assets/lidousha/fastlane_c9_private" / f"{CANDIDATE}.reviewed.srt"
    ).read_bytes()
    stored = json.loads((tmp_path / CANDIDATE / "private-replay.json").read_text(encoding="utf-8"))
    assert stored == packet
    with pytest.raises(FastlaneC9PrivateReplayError, match="CREATE_ONLY"):
        materialize_c9_private_replay(root=ROOT, output_dir=tmp_path / CANDIDATE)


def test_c9_private_replay_rejects_an_unclassified_or_wrongly_dropped_cue(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "assets", repo / "assets")
    receipt_path = repo / "assets/lidousha/fastlane_c9_private" / f"{CANDIDATE}.foreign-video-source-action.v1.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["cue_actions"][4]["source_classification"] = "IN_VIDEO"
    unsigned = dict(receipt)
    unsigned.pop("self_sha256")
    import hashlib
    receipt["self_sha256"] = "sha256:" + hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(FastlaneC9PrivateReplayError, match="DROP_SCOPE_INVALID"):
        validate_c9_source_action(repo)
