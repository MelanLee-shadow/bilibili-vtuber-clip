import json
import pytest

from src.autoslice.fastlane_c2_formal_adapter import NAMES, TITLE, visual_inventory
from src.autoslice.fastlane_c2_technical_receipt import (
    READY,
    make_accepted_receipt,
    make_ready_proposal,
    validate_accepted_receipt,
    validate_ready_proposal,
    write_create_only_json,
)


def _package(tmp_path):
    for key, name in NAMES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TITLE + "\n", encoding="utf-8") if key == "title" else path.write_bytes(b"c2")
    (tmp_path / "visual-evidence" / "contact-sheet.png").write_bytes(b"sheet")
    (tmp_path / "review_manifest.json").write_text(json.dumps({"visual_evidence_inventory": visual_inventory(tmp_path)}), encoding="utf-8")
    (tmp_path / "package_audit.json").write_text(json.dumps({"passed": True, "blocking_issue_count": 0, "policy_fingerprint": "sha256:fresh"}), encoding="utf-8")
    return tmp_path


def _proposal(root):
    return make_ready_proposal(root, {"passed": True, "blocking_issue_count": 0, "policy_fingerprint": "sha256:fresh"})


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_c2_ready_proposal_rejects_stale_status_and_binding_drifts(tmp_path):
    root = _package(tmp_path)
    proposal = _proposal(root)
    validate_ready_proposal(root, proposal)
    for field, value in [
        ("status", "ROOT_CONTENT_CHECKED_DEPLOYED_AUDITOR_RECLOSURE_PENDING"),
        ("title", "wrong"),
        ("reviewer", "someone else"),
    ]:
        changed = dict(proposal)
        changed[field] = value
        with pytest.raises(ValueError):
            validate_ready_proposal(root, changed)
    changed = json.loads(json.dumps(proposal))
    changed["bindings"]["artifacts"]["cover"]["sha256"] = "sha256:bad"
    with pytest.raises(ValueError):
        validate_ready_proposal(root, changed)
    changed = json.loads(json.dumps(proposal))
    changed["bindings"]["audit_policy_fingerprint"] = "sha256:stale"
    with pytest.raises(ValueError):
        validate_ready_proposal(root, changed)


def test_c2_accepted_receipt_rejects_identity_reviewer_timestamp_and_scope_drifts(tmp_path):
    root = _package(tmp_path)
    proposal_path = root / "ready.proposal.json"
    _write(proposal_path, _proposal(root))
    receipt = make_accepted_receipt(root, proposal_path, "2026-08-24T22:00:00+00:00", "Root accepted the exact reclosed C2 package.")
    validate_accepted_receipt(root, proposal_path, receipt)
    for field, value in [
        ("title", "wrong"),
        ("reviewed_by", "not root"),
        ("reviewed_at", "2026-08-24T22:00:00"),
        ("scope", "generic"),
        ("accepted", False),
    ]:
        changed = dict(receipt)
        changed[field] = value
        with pytest.raises(ValueError):
            validate_accepted_receipt(root, proposal_path, changed)
    (root / NAMES["cover"]).write_bytes(b"drift")
    with pytest.raises(ValueError):
        validate_accepted_receipt(root, proposal_path, receipt)


def test_c2_accepted_materializer_is_create_only_and_rejects_symlink_proposal(tmp_path):
    root = _package(tmp_path)
    proposal_path = root / "ready.proposal.json"
    _write(proposal_path, _proposal(root))
    receipt = make_accepted_receipt(root, proposal_path, "2026-08-24T22:00:00+00:00", "Root decision.")
    receipt_path = root / "accepted.json"
    write_create_only_json(receipt_path, receipt)
    with pytest.raises(FileExistsError):
        write_create_only_json(receipt_path, receipt)
    link = root / "proposal-link.json"
    link.symlink_to(proposal_path)
    with pytest.raises(ValueError):
        make_accepted_receipt(root, link, "2026-08-24T22:00:00+00:00", "Root decision.")
