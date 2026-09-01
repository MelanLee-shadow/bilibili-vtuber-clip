import pytest

from src.autoslice.fastlane_c2_formal_adapter import (
    CID,
    SCHEMA,
    audit_fastlane_c2_formal_package,
    is_fastlane_c2_formal_manifest,
    visual_inventory_valid,
)


def test_c2_formal_adapter_is_candidate_scoped(tmp_path):
    assert is_fastlane_c2_formal_manifest({"schema_version": SCHEMA, "candidate_id": CID})
    assert not is_fastlane_c2_formal_manifest({"schema_version": SCHEMA, "candidate_id": "other"})
    assert audit_fastlane_c2_formal_package(tmp_path)[0]["code"] == "C2_FORMAL_MANIFEST_INVALID"


@pytest.mark.parametrize("missing", [
    "cue5-start-14469ms.png",
    "cue5-13469ms-4s.mp4",
    "contact-sheet.png",
])
def test_c2_visual_inventory_rejects_a_missing_declared_artifact(tmp_path, missing):
    evidence = tmp_path / "visual-evidence"
    evidence.mkdir()
    receipt = {"artifacts": {missing: "0" * 64}}
    assert not visual_inventory_valid(tmp_path, receipt, [{"path": "visual-evidence/visual-evidence.v1.json", "sha256": "0" * 64, "bytes": 1}])
