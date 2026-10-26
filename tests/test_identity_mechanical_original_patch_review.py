import copy
import json

import pytest

from src.autoslice import original_patch_review as review
from scripts import audit_review_package as auditor


def test_m31_audit_root_is_locator_but_content_drift_is_rejected(tmp_path, monkeypatch):
    original = tmp_path / "original"
    original.mkdir()
    moved = tmp_path / "moved"
    moved.mkdir()
    (moved / "review_manifest.json").write_bytes(b"{}")
    saved = {"passed": True, "blocking_issue_count": 0, "root": str(original),
             "audited_inputs": {"media": {"sha256": "a" * 64, "bytes": 5}}}
    audit_path = moved / "package_audit.json"
    audit_path.write_text(json.dumps(saved))
    current = copy.deepcopy(saved)
    current["root"] = str(moved)
    monkeypatch.setattr(review, "validate_package", lambda *_a, **_kw: {"candidate_id": "c"})
    monkeypatch.setattr(auditor, "audit_package", lambda _root: current)
    assert review._bindings(moved, audit_path)["closure"]["candidate_id"] == "c"
    current["audited_inputs"]["media"]["sha256"] = "b" * 64
    with pytest.raises(ValueError, match="technical audit drift"):
        review._bindings(moved, audit_path)
