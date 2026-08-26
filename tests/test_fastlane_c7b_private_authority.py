from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "assets/lidousha/fastlane_c7b_private/auto_130040_201_255.private-authority.v1.json"
CLOSURE = ROOT / "assets/lidousha/fastlane_c7b_private/auto_130040_201_255.freeze-closure.v1.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cues(srt: Path) -> list[tuple[int, str, str]]:
    blocks = srt.read_text(encoding="utf-8").strip().split("\n\n")
    result = []
    for block in blocks:
        ordinal, timing, text = block.splitlines()
        result.append((int(ordinal), timing, text))
    return result


def _canonical_self_hash(payload: dict) -> str:
    sealed = dict(payload)
    sealed.pop("canonical_self_sha256")
    return hashlib.sha256(
        json.dumps(sealed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_c7b_private_authority_is_exact_and_narrow() -> None:
    authority = json.loads(ASSET.read_text(encoding="utf-8"))
    closure = json.loads(CLOSURE.read_text(encoding="utf-8"))
    reviewed = ROOT / authority["reviewed_subtitle"]["path"]
    cues = _cues(reviewed)

    assert authority["candidate_id"] == "auto_130040_201_255"
    assert authority["source_recording"]["absolute_interval_ms"] == [191190, 303140]
    assert _sha256(reviewed) == authority["reviewed_subtitle"]["sha256"]
    assert len(cues) == authority["reviewed_subtitle"]["cue_count"] == 36
    assert authority["reviewed_subtitle"]["changed_cues"] == [10, 18, 19, 20]
    assert authority["reviewed_subtitle"]["frozen_cue_count"] == 32
    assert authority["reviewed_subtitle"]["operator_drop_cue_count"] == 0
    assert closure["canonical_self_sha256"] == _canonical_self_hash(closure)
    assert closure["predecessor_srt_sha256"] == authority["private_predecessor"]["pipeline_diagnostic_sha256"]
    assert closure["reviewed_srt_sha256"] == _sha256(reviewed)

    by_ordinal = {ordinal: (timing, text) for ordinal, timing, text in cues}
    assert by_ordinal[10] == ("00:00:22,060 --> 00:00:24,060", "可能这就是kmx")
    assert by_ordinal[18] == ("00:00:44,960 --> 00:00:47,000", "脑控状态都信不了李1")
    assert by_ordinal[19] == ("00:00:47,000 --> 00:00:48,560", "怎么这样")
    assert by_ordinal[20] == ("00:00:49,050 --> 00:00:52,110", "信李侄的是个什么状态")
    text = reviewed.read_text(encoding="utf-8")
    assert "怎么了你" not in text
    assert "信李侄的是个女状态" not in text
    assert authority["title"] == "李姐也是脑控大师，但即使被脑控仍然信不了李1是怎么回事呢"
    assert authority["ruling"]["historical_content_boundary_rejection"].startswith("SUPERSEDED_FOR_REVIVAL_ONLY")
    assert authority["scope"]["upload_authorized_by_this_asset"] is False


def test_c7b_freeze_closure_reconstructs_and_compares_every_cue() -> None:
    authority = json.loads(ASSET.read_text(encoding="utf-8"))
    closure = json.loads(CLOSURE.read_text(encoding="utf-8"))
    reviewed = ROOT / authority["reviewed_subtitle"]["path"]
    reviewed_cues = _cues(reviewed)
    changes = {row["ordinal"]: row for row in closure["changes"]}

    predecessor_blocks = []
    predecessor_cues = []
    for ordinal, timing, new_text in reviewed_cues:
        old_text = changes.get(ordinal, {}).get("old_text", new_text)
        predecessor_blocks.append(f"{ordinal}\n{timing}\n{old_text}")
        predecessor_cues.append((ordinal, timing, old_text))
    predecessor_bytes = ("\n\n".join(predecessor_blocks) + "\n").encode()

    assert hashlib.sha256(predecessor_bytes).hexdigest() == closure["predecessor_srt_sha256"]
    assert len(predecessor_cues) == len(reviewed_cues) == closure["cue_count"]
    changed = []
    for old, new in zip(predecessor_cues, reviewed_cues, strict=True):
        assert old[0] == new[0]
        assert old[1] == new[1]
        if old[2] != new[2]:
            changed.append(old[0])
            row = changes[old[0]]
            assert row["timing"] == old[1]
            assert row["old_text"] == old[2]
            assert row["new_text"] == new[2]
    assert changed == closure["changed_ordinals"] == [10, 18, 19, 20]
    assert sum(old[2] == new[2] for old, new in zip(predecessor_cues, reviewed_cues, strict=True)) == 32
    assert closure["frozen_ordinal_count"] == 32
    assert closure["operator_drop_count"] == 0
    assert closure["human_approximate_timestamp_mapping"]["status"] == "UNVERIFIED_NOT_USED_AS_EXACT_BINDING"
