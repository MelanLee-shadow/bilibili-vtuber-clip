"""assets/_template 骨架的可用性回归：新频道第一支切片不许被骨架自己炸掉。

二轮真实测试实锤的骨架类通病：empty_entries 派生把「键不存在」错造成
「空对象 {}」，而真值台账治理门只放行 None——每个新频道首切必炸。这里把
整个骨架构建结果按各消费门的真实契约逐项锁死。
"""

import json
from pathlib import Path

import pytest

from scripts.export_oss_snapshot import build_template_assets
from src.autoslice.source_truth_governance import validate_ledger_governance


@pytest.fixture(scope="module")
def skeleton_root(tmp_path_factory) -> Path:
    out_root = tmp_path_factory.mktemp("oss-skeleton")
    build_template_assets(out_root)
    return out_root / "assets/_template"


def _load(skeleton_root: Path, name: str) -> dict:
    return json.loads((skeleton_root / name).read_text(encoding="utf-8"))


def test_truth_ledger_skeleton_passes_its_own_governance_gate(skeleton_root):
    ledger = _load(skeleton_root, "subtitle_truth_ledger.v1.json")
    # 键不存在（= None 语义）才是可用形态；空 {} 会让治理门必炸
    assert "governance" not in ledger
    validate_ledger_governance(ledger, ledger.get("entries") or [])
    assert ledger["entries"] == []


def test_published_songs_skeleton_has_no_real_account_mid(skeleton_root):
    songs = _load(skeleton_root, "published_songs.v1.json")
    assert songs.get("account_mid") == 0
    assert songs["songs"] == []


def test_profile_scoped_schemas_are_replace_me_shaped(skeleton_root):
    for name, suffix in (
        ("speech_memory_ledger.v1.json", "-speech-memory-ledger.v1"),
        ("selection_score_calibration.v1.json", "-selection-score-calibration.v1"),
        ("cover_reference_overrides.v1.json", "-cover-reference-overrides.v1"),
        ("manual_title_overrides.v1.json", "-manual-title-overrides.v1"),
    ):
        doc = _load(skeleton_root, name)
        assert doc["schema_version"] == f"REPLACE_ME{suffix}", name


def test_calibration_skeleton_keeps_exact_loader_keys(skeleton_root):
    calibration = _load(skeleton_root, "selection_score_calibration.v1.json")
    assert set(calibration) == {
        "schema_version",
        "authority",
        "anchors",
        "ordering_constraints",
    }
    assert calibration["anchors"] == []
    assert calibration["ordering_constraints"] == []


def test_gift_names_ship_in_full(skeleton_root):
    gifts = _load(skeleton_root, "bilibili_gift_names.v1.json")
    assert len(gifts["names"]) >= 30


def test_no_maintainer_name_leaks_into_skeletons(skeleton_root):
    for path in skeleton_root.rglob("*.json"):
        assert "Ivan" not in path.read_text(encoding="utf-8"), path.name
