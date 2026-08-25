import json
from types import SimpleNamespace

import pytest

import src.autoslice.fastlane_c2_technical_receipt as technical_receipt
from src.autoslice.fastlane_c2_formal_adapter import NAMES, TITLE, visual_inventory
from src.autoslice.fastlane_c2_technical_receipt import (
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


def _passing_replay_audit(root: str) -> dict[str, object]:
    return {
        "schema_version": "lidousha-review-package-audit.v2",
        "policy_epoch": "2026-07-31.final-artifact-gates.v5",
        "policy_fingerprint": "sha256:" + "a" * 64,
        "auditor_source_sha256": "sha256:" + "b" * 64,
        "passed": True,
        "root": root,
        "audited_inputs": [{"path": "final.mp4", "sha256": "sha256:" + "c" * 64}],
        "issues": [],
        "issue_count": 0,
        "blocking_issue_count": 0,
    }


def _replay_fixture(tmp_path, monkeypatch, *, saved_root: str | None = None):
    root = _package(tmp_path)
    saved = _passing_replay_audit(saved_root or "/frozen/c2-formal")
    (root / "package_audit.json").write_text(json.dumps(saved), encoding="utf-8")
    current = json.loads(json.dumps(saved))
    current["root"] = str(root.absolute())

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(current))

    monkeypatch.setattr(technical_receipt.subprocess, "run", fake_run)
    return root, current


@pytest.fixture
def no_external_audit(monkeypatch):
    monkeypatch.setattr(technical_receipt, "_replay_current_audit", lambda root: None)


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
    changed["required_root_checks"][3] = "full burned playback"
    with pytest.raises(ValueError):
        validate_ready_proposal(root, changed)
    changed = json.loads(json.dumps(proposal))
    changed["bindings"]["audit_policy_fingerprint"] = "sha256:stale"
    with pytest.raises(ValueError):
        validate_ready_proposal(root, changed)


def test_c2_accepted_receipt_rejects_identity_reviewer_timestamp_and_scope_drifts(tmp_path, no_external_audit):
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


def test_c2_accepted_materializer_is_create_only_and_rejects_symlink_proposal(tmp_path, no_external_audit):
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


def test_c2_validator_rejects_audit_replay_drift_and_unsafe_package_paths(tmp_path, monkeypatch):
    root = _package(tmp_path)
    proposal_path = root / "ready.proposal.json"
    _write(proposal_path, _proposal(root))
    receipt = make_accepted_receipt(root, proposal_path, "2026-08-24T22:00:00+00:00", "Root decision.")
    monkeypatch.setattr(technical_receipt, "_replay_current_audit", lambda package: (_ for _ in ()).throw(ValueError("C2_AUDIT_REPLAY_DRIFT")))
    with pytest.raises(ValueError, match="C2_AUDIT_REPLAY_DRIFT"):
        validate_accepted_receipt(root, proposal_path, receipt)
    monkeypatch.setattr(technical_receipt, "_replay_current_audit", lambda package: None)
    manifest = root / "review_manifest.json"
    replacement = root / "manifest-real.json"
    manifest.rename(replacement)
    manifest.symlink_to(replacement)
    with pytest.raises(ValueError):
        validate_ready_proposal(root, json.loads(proposal_path.read_text(encoding="utf-8")))
    other = _package(tmp_path / "other")
    audit = other / "package_audit.json"
    audit_replacement = other / "audit-real.json"
    audit.rename(audit_replacement)
    audit.symlink_to(audit_replacement)
    with pytest.raises(ValueError):
        make_ready_proposal(other, {"passed": True, "blocking_issue_count": 0, "policy_fingerprint": "sha256:fresh"})


def test_c2_create_only_rejects_symlink_parent(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError):
        write_create_only_json(linked_parent / "receipt.json", {"x": 1})


def test_c2_replay_allows_only_relocation_and_auditor_identity_churn(
    tmp_path, monkeypatch
):
    root, current = _replay_fixture(tmp_path, monkeypatch)
    current["policy_fingerprint"] = "sha256:" + "d" * 64
    current["auditor_source_sha256"] = "sha256:" + "e" * 64

    technical_receipt._replay_current_audit(root, allow_root_relocation=True)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda audit: audit.update(
            audited_inputs=[{"path": "final.mp4", "sha256": "sha256:" + "f" * 64}]
        ),
        lambda audit: audit.update(
            issues=[{"code": "NEW_BLOCK", "severity": "BLOCK"}],
            issue_count=1,
            blocking_issue_count=1,
        ),
        lambda audit: audit.update(schema_version="other-audit-schema.v1"),
        lambda audit: audit.update(policy_epoch="other-policy-epoch"),
        lambda audit: audit.update(passed=False),
        lambda audit: audit.update(blocking_issue_count=1),
        lambda audit: audit.update(issue_count=1),
    ],
)
def test_c2_replay_rejects_content_or_verdict_drift(tmp_path, monkeypatch, mutate):
    root, current = _replay_fixture(tmp_path, monkeypatch)
    current["policy_fingerprint"] = "sha256:" + "d" * 64
    current["auditor_source_sha256"] = "sha256:" + "e" * 64
    mutate(current)

    with pytest.raises(ValueError, match="C2_AUDIT_REPLAY_DRIFT"):
        technical_receipt._replay_current_audit(root, allow_root_relocation=True)


def test_c2_replay_without_relocation_keeps_exact_audit_identity(tmp_path, monkeypatch):
    root, current = _replay_fixture(
        tmp_path,
        monkeypatch,
        saved_root=str(tmp_path.absolute()),
    )
    current["policy_fingerprint"] = "sha256:" + "d" * 64
    current["auditor_source_sha256"] = "sha256:" + "e" * 64

    with pytest.raises(ValueError, match="C2_AUDIT_REPLAY_DRIFT"):
        technical_receipt._replay_current_audit(root)
