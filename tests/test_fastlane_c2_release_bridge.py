import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import fastlane_c2_release_bridge as bridge
from src.autoslice import fastlane_c2_legacy_recovery as legacy
from src.autoslice.fastlane_c2_legacy_recovery import make_accepted_execution_contract, validate_accepted_execution_contract
from src.autoslice.fastlane_c2_formal_adapter import NAMES


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _formal(tmp_path: Path) -> Path:
    root = tmp_path / "formal"
    root.mkdir()
    for role, name in NAMES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((role + "\n").encode())
    (root / "review_manifest.json").write_text("{}", encoding="utf-8")
    (root / "package_audit.json").write_text("{}", encoding="utf-8")
    return root


def _receipt(path: Path, formal: Path) -> Path:
    proposal = formal / "ready-proposal.json"
    proposal.write_text("{}", encoding="utf-8")
    payload = {
        "candidate_id": bridge.CID,
        "title": bridge.TITLE,
        "accepted": True,
        "reviewer": "Codex root",
        "reviewed_at": "2026-08-24T20:00:00+00:00",
        "proposal": {"path": proposal.name, "bytes": proposal.stat().st_size, "sha256": "sha256:" + _sha(proposal)},
        "bindings": {
            "review_manifest_sha256": "sha256:" + _sha(formal / "review_manifest.json"),
            "package_audit_sha256": "sha256:" + _sha(formal / "package_audit.json"),
            "artifacts": {role: {"sha256": "sha256:" + _sha(formal / name)} for role, name in NAMES.items()},
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return proposal


def _authorization(path: Path) -> None:
    payload = {
        "schema_version": bridge.AUTH_SCHEMA,
        "candidate_id": bridge.CID,
        "recording_date": bridge.DATE,
        "title": bridge.TITLE,
        "direct_ivan_lines": [
            {
                "line": line,
                "uuid": uuid,
                "timestamp": timestamp,
                "raw_line_sha256": raw_sha,
                "content_sha256": content_sha,
                "quote": quote,
            }
            for line, (uuid, timestamp, raw_sha, content_sha, quote) in bridge.DIRECT_IVAN_LINES.items()
        ],
        "remaining_machine_gates": [
            "accepted C2 root technical receipt bound to current formal audit and artifacts",
            "current C2 release-package audit",
            "CPA title-cover joint QC for exact final title and cover",
            "authorized-upload manifest verify",
            "single serialized upload and public Creator section reconciliation",
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["self_seal"] = {"canonical_json_without_self_seal_sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _tags(*_args, **_kwargs):
    return {
        "engine": "suggest-upload-tags.v2",
        "status": "OK",
        "final_tags": ["李豆沙", "虚拟主播", "虚拟UP主", "直播切片", "吐槽"],
        "final_tag_line": "李豆沙,虚拟主播,虚拟UP主,直播切片,吐槽",
        "proper_noun_tags": [], "important_content_ips": [], "content_tags": [], "warnings": [],
    }


def _receipt_binding(path: Path, proposal: Path) -> None:
    path.write_text(json.dumps({
        "proposal": {"path": proposal.name, "bytes": proposal.stat().st_size, "sha256": "sha256:" + _sha(proposal)},
    }), encoding="utf-8")


def _current_legacy_reclosure_fixture(tmp_path: Path, monkeypatch):
    """Build a C2-only binding fixture without relaxing the bridge checks.

    The real legacy validator owns the detailed C2 formal schema.  This
    fixture models its four current file bindings so this bridge test can
    isolate stale-envelope handling without manufacturing unrelated media.
    """
    formal = tmp_path / "formal"
    formal.mkdir()
    audit = formal / "package_audit.json"
    formal_input = formal / "c2.formal.record.v1.json"
    receipt = tmp_path / bridge.ROOT_RECEIPT_NAME
    authorization = tmp_path / bridge.AUTH_NAME
    proposal = tmp_path / bridge.LEGACY_PROPOSAL_NAME
    contract = tmp_path / bridge.LEGACY_CONTRACT_NAME
    audit.write_text('{"audit":"current"}', encoding="utf-8")
    formal_input.write_text('{"formal":"current"}', encoding="utf-8")
    receipt.write_text('{"receipt":"current"}', encoding="utf-8")
    authorization.write_text('{"authorization":"current"}', encoding="utf-8")

    def current_proposal() -> dict[str, str]:
        return {
            "candidate_id": bridge.CID,
            "package_audit_sha256": "sha256:" + _sha(audit),
            "technical_receipt_sha256": "sha256:" + _sha(receipt),
            "authorization_sha256": "sha256:" + _sha(authorization),
            "formal_input_sha256": "sha256:" + _sha(formal_input),
        }

    proposal.write_text(json.dumps(current_proposal()), encoding="utf-8")

    def validate_current_proposal(value, *, formal: Path, authorization: Path, receipt: Path) -> None:
        if (
            formal != audit.parent
            or authorization != authorization_path
            or receipt != receipt_path
            or value != current_proposal()
        ):
            raise ValueError("current C2 reclosure binding drift")

    authorization_path, receipt_path = authorization, receipt
    monkeypatch.setattr(legacy, "validate_proposal", validate_current_proposal)
    current = make_accepted_execution_contract(
        proposal=proposal,
        reviewed_at="2026-08-25T00:08:18Z",
        decision_basis="current root-accepted C2 reclosure fixture",
    )
    contract.write_text(json.dumps(current), encoding="utf-8")
    return {
        "formal": formal,
        "audit": audit,
        "formal_input": formal_input,
        "receipt": receipt,
        "authorization": authorization,
        "proposal": proposal,
        "contract": contract,
    }


def _validate_current_legacy_reclosure(paths: dict[str, Path]) -> None:
    bridge._validate_legacy_execution_contract(
        paths["formal"],
        paths["proposal"],
        paths["contract"],
        paths["authorization"],
        paths["receipt"],
    )


def _passing_formal_audit(root: str) -> dict[str, object]:
    return {
        "schema_version": "lidousha-review-package-audit.v2",
        "policy_epoch": "2026-07-31.final-artifact-gates.v5",
        "policy_fingerprint": "sha256:" + "a" * 64,
        "auditor_source_sha256": "sha256:" + "b" * 64,
        "passed": True,
        "root": root,
        "audited_inputs": [{"path": "video.mp4", "sha256": "sha256:" + "c" * 64}],
        "issues": [],
        "issue_count": 0,
        "blocking_issue_count": 0,
    }


def _formal_audit_replay_fixture(tmp_path, monkeypatch):
    formal = tmp_path / "formal"
    formal.mkdir()
    saved = _passing_formal_audit("/frozen/c2-formal")
    (formal / "package_audit.json").write_text(json.dumps(saved), encoding="utf-8")
    current = json.loads(json.dumps(saved))
    current["root"] = str(formal.resolve())

    import scripts.audit_lidousha_review_package as review_package_audit

    monkeypatch.setattr(review_package_audit, "audit_package", lambda _root: current)
    return formal, current


def _legacy_formal_audit_replay_values(tmp_path: Path):
    formal = tmp_path / "formal"
    formal.mkdir()
    saved = _passing_formal_audit("/frozen/c2-formal")
    current = json.loads(json.dumps(saved))
    current["root"] = str(formal.resolve())
    return formal, saved, current


def _legacy_make_proposal_fixture(tmp_path: Path, monkeypatch):
    formal, saved, current = _legacy_formal_audit_replay_values(tmp_path)
    for role, name in NAMES.items():
        path = formal / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{role}\n", encoding="utf-8")

    rows = [{} for _ in range(21)]
    rows[4] = {
        "cue": 5,
        "time": "00:00:08,720 --> 00:00:11,240",
        "before": "是刚吗？小豆老公不是你老公",
        "after": "小豆老公；； 不是你老公",
        "disposition": "OPERATOR_REPAIR",
    }
    rows[20] = {"after": "小豆哪有好吵"}
    (formal / NAMES["graph"]).write_text(json.dumps({"rows": rows}), encoding="utf-8")
    (formal / "c2.formal.record.v1.json").write_text(
        json.dumps({"candidate_id": bridge.CID, "title": bridge.TITLE}), encoding="utf-8"
    )
    for name in (
        "c2.formal.publish.v1.json",
        "review_manifest.json",
        "cover-reprojection.v1.json",
    ):
        (formal / name).write_text("{}", encoding="utf-8")
    (formal / "package_audit.json").write_text(json.dumps(saved), encoding="utf-8")

    import scripts.audit_lidousha_review_package as review_package_audit

    monkeypatch.setattr(review_package_audit, "audit_package", lambda _root: current)
    authorization = tmp_path / "authorization.json"
    _authorization(authorization)
    proposal_path = formal / "ready-proposal.json"
    proposal_path.write_text("{}", encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"proposal": {"path": proposal_path.name}}), encoding="utf-8")

    def validate_receipt(root, proposal, receipt_data, *, allow_audit_root_relocation):
        assert root == formal
        assert proposal == proposal_path
        assert receipt_data == {"proposal": {"path": proposal_path.name}}
        assert allow_audit_root_relocation is True

    monkeypatch.setattr(legacy, "validate_accepted_receipt", validate_receipt)
    return formal, authorization, receipt, current


def test_c2_legacy_make_proposal_allows_formal_root_and_auditor_identity_churn(
    tmp_path, monkeypatch
):
    formal, authorization, receipt, current = _legacy_make_proposal_fixture(tmp_path, monkeypatch)
    current["policy_fingerprint"] = "sha256:" + "d" * 64
    current["auditor_source_sha256"] = "sha256:" + "e" * 64

    proposal = legacy.make_proposal(formal, authorization, receipt)

    assert proposal["candidate_id"] == bridge.CID
    assert proposal["recording_date"] == bridge.DATE
    assert proposal["formal_inputs"]["package_audit"] == {
        "path": "package_audit.json",
        "bytes": (formal / "package_audit.json").stat().st_size,
        "sha256": "sha256:" + _sha(formal / "package_audit.json"),
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda audit: audit.update(
            issues=[{"code": "SAVED_WARNING", "severity": "WARN"}], issue_count=1
        ),
        lambda audit: audit.update(blocking_issue_count=1),
        lambda audit: audit.update(passed=False),
    ],
    ids=["issues-count", "blocking", "pass"],
)
def test_c2_legacy_formal_audit_replay_requires_saved_and_current_pass_zero(tmp_path, mutate):
    formal, saved, current = _legacy_formal_audit_replay_values(tmp_path)
    mutate(saved)
    current = json.loads(json.dumps(saved))
    current["root"] = str(formal.resolve())

    with pytest.raises(ValueError, match="C2 formal audit replay drift"):
        legacy._validate_current_formal_audit_replay(
            formal=formal,
            saved=saved,
            current=current,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda saved, _current, _formal: saved.update(root="relative/frozen-root"),
        lambda _saved, current, formal: current.update(root=str(formal.parent / "other-formal")),
    ],
    ids=["saved-root-not-absolute", "current-root-not-formal"],
)
def test_c2_legacy_formal_audit_replay_rejects_unsafe_root_relocation(tmp_path, mutate):
    formal, saved, current = _legacy_formal_audit_replay_values(tmp_path)
    mutate(saved, current, formal)

    with pytest.raises(ValueError, match="C2 formal audit replay drift"):
        legacy._validate_current_formal_audit_replay(
            formal=formal,
            saved=saved,
            current=current,
        )


@pytest.mark.parametrize(
    "drift, mutate",
    [
        (
            "inputs",
            lambda audit: audit.update(
                audited_inputs=[{"path": "video.mp4", "sha256": "sha256:" + "f" * 64}]
            ),
        ),
        (
            "issues",
            lambda audit: audit.update(
                issues=[{"code": "NEW_ISSUE", "severity": "WARN"}], issue_count=1
            ),
        ),
        ("schema", lambda audit: audit.update(schema_version="other-audit-schema.v1")),
        ("epoch", lambda audit: audit.update(policy_epoch="other-policy-epoch")),
        ("pass", lambda audit: audit.update(passed=False)),
        ("blocking", lambda audit: audit.update(blocking_issue_count=1)),
        ("count", lambda audit: audit.update(issue_count=1)),
    ],
)
def test_c2_legacy_formal_audit_replay_rejects_content_or_verdict_drift(
    tmp_path, drift, mutate
):
    formal, saved, current = _legacy_formal_audit_replay_values(tmp_path)
    current["policy_fingerprint"] = "sha256:" + "d" * 64
    current["auditor_source_sha256"] = "sha256:" + "e" * 64
    mutate(current)

    with pytest.raises(ValueError, match="C2 formal audit replay drift"):
        legacy._validate_current_formal_audit_replay(
            formal=formal,
            saved=saved,
            current=current,
        )


def test_c2_formal_audit_allows_root_and_auditor_identity_churn(tmp_path, monkeypatch):
    formal, current = _formal_audit_replay_fixture(tmp_path, monkeypatch)
    current["policy_fingerprint"] = "sha256:" + "d" * 64
    current["auditor_source_sha256"] = "sha256:" + "e" * 64

    bridge._validate_formal_audit(formal)


@pytest.mark.parametrize(
    "drift, mutate",
    [
        (
            "inputs",
            lambda audit: audit.update(
                audited_inputs=[{"path": "video.mp4", "sha256": "sha256:" + "f" * 64}]
            ),
        ),
        (
            "issues",
            lambda audit: audit.update(
                issues=[{"code": "NEW_ISSUE", "severity": "WARN"}], issue_count=1
            ),
        ),
        ("schema", lambda audit: audit.update(schema_version="other-audit-schema.v1")),
        ("epoch", lambda audit: audit.update(policy_epoch="other-policy-epoch")),
        ("pass", lambda audit: audit.update(passed=False)),
        ("blocking", lambda audit: audit.update(blocking_issue_count=1)),
        ("count", lambda audit: audit.update(issue_count=1)),
    ],
)
def test_c2_formal_audit_rejects_content_or_verdict_drift(
    tmp_path, monkeypatch, drift, mutate
):
    formal, current = _formal_audit_replay_fixture(tmp_path, monkeypatch)
    current["policy_fingerprint"] = "sha256:" + "d" * 64
    current["auditor_source_sha256"] = "sha256:" + "e" * 64
    mutate(current)

    with pytest.raises(bridge.C2ReleaseBridgeError, match="current passing replay"):
        bridge._validate_formal_audit(formal)


def test_c2_bridge_does_not_accept_non_c2_manifest(tmp_path):
    root = tmp_path / "ordinary-package"
    root.mkdir()
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": bridge.SCHEMA,
                "candidate_id": "ordinary-candidate",
                "recording_date": bridge.DATE,
            }
        ),
        encoding="utf-8",
    )

    assert bridge.audit_fastlane_c2_release_package(root) == [
        {"code": "C2_RELEASE_MANIFEST_INVALID", "severity": "BLOCK", "detail": ""}
    ]


def test_c2_receipt_requires_its_exact_self_bound_proposal(tmp_path):
    formal = tmp_path / "formal"
    formal.mkdir()
    proposal = formal / "exact-proposal.json"
    proposal.write_text("{}", encoding="utf-8")
    other = formal / "other-proposal.json"
    other.write_text("{}", encoding="utf-8")
    receipt = tmp_path / bridge.ROOT_RECEIPT_NAME
    _receipt_binding(receipt, proposal)
    assert bridge._receipt_bound_proposal(formal, proposal, receipt) == proposal
    with pytest.raises(bridge.C2ReleaseBridgeError, match="not receipt-bound"):
        bridge._receipt_bound_proposal(formal, other, receipt)
    proposal.write_text('{"drift":true}', encoding="utf-8")
    with pytest.raises(bridge.C2ReleaseBridgeError, match="binding drift"):
        bridge._receipt_bound_proposal(formal, proposal, receipt)


def test_c2_authorization_asset_is_self_sealed_and_registry_hash_bound():
    repo = Path(__file__).resolve().parents[1]
    authorization = repo / "assets/lidousha/fastlane_c2_private/auto_203011_328_389.release-authorization.v1.json"
    bridge._validate_authorization(json.loads(authorization.read_text(encoding="utf-8")))
    registry = json.loads((repo / "assets/lidousha/publication_registry.v1.json").read_text(encoding="utf-8"))
    row = next(item for item in registry["entries"] if item.get("candidate_id") == bridge.CID)
    assert row["status"] == "released_for_upload"
    assert row["release_authorization"] == {
        "path": "assets/lidousha/fastlane_c2_private/auto_203011_328_389.release-authorization.v1.json",
        "bytes": authorization.stat().st_size,
        "sha256": "sha256:" + _sha(authorization),
        "direct_ivan_lines": [947, 1643, 1745],
        "remaining_machine_gates": [
            "accepted C2 root technical receipt bound to current formal audit and artifacts",
            "current C2 release-package audit",
            "CPA title-cover joint QC for exact final title and cover",
            "authorized-upload manifest verify",
            "single serialized upload and public Creator section reconciliation",
        ],
    }


def test_c2_legacy_execution_envelope_rejects_any_signature_surface_drift(tmp_path):
    proposal = tmp_path / "proposal.json"
    proposal.write_text("{}", encoding="utf-8")
    accepted = make_accepted_execution_contract(proposal=proposal, reviewed_at="2026-08-24T20:00:00+00:00", decision_basis="root acceptance fixture")
    validate_accepted_execution_contract(accepted, proposal=proposal)
    for key, value in (("reviewed_by", "other"), ("reviewed_at", "2026-08-24T20:00:00"), ("decision_basis", "")):
        drift = dict(accepted)
        drift[key] = value
        with pytest.raises(ValueError):
            validate_accepted_execution_contract(drift, proposal=proposal)
    proposal.write_text('{"drift":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="proposal binding"):
        validate_accepted_execution_contract(accepted, proposal=proposal)


def test_c2_bridge_accepts_only_current_reclosure_not_historical_envelope(tmp_path, monkeypatch):
    paths = _current_legacy_reclosure_fixture(tmp_path, monkeypatch)
    _validate_current_legacy_reclosure(paths)

    old_proposal = tmp_path / "old-c2.legacy-recovery.proposal.v1.json"
    old_proposal.write_text('{"proposal":"before-reclosure"}', encoding="utf-8")
    stale = make_accepted_execution_contract(
        proposal=old_proposal,
        reviewed_at="2026-08-24T22:57:54Z",
        decision_basis="historical envelope fixture",
    )
    paths["contract"].write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(bridge.C2ReleaseBridgeError, match="legacy execution contract rejected"):
        _validate_current_legacy_reclosure(paths)

    current = make_accepted_execution_contract(
        proposal=paths["proposal"],
        reviewed_at="2026-08-25T00:08:18Z",
        decision_basis="current root-accepted C2 reclosure fixture",
    )
    paths["contract"].write_text(json.dumps(current), encoding="utf-8")
    _validate_current_legacy_reclosure(paths)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("reviewed_at", "2026-08-25T00:09:18Z"),
        lambda value: value.__setitem__("decision_basis", "tampered basis"),
        lambda value: value["proposal"].__setitem__("sha256", "sha256:" + "0" * 64),
        lambda value: value.__setitem__("unexpected", True),
    ],
    ids=["timestamp", "decision-basis", "proposal", "extra-key"],
)
def test_c2_bridge_rejects_tampered_current_legacy_contract(tmp_path, monkeypatch, mutate):
    paths = _current_legacy_reclosure_fixture(tmp_path, monkeypatch)
    value = json.loads(paths["contract"].read_text(encoding="utf-8"))
    mutate(value)
    paths["contract"].write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(bridge.C2ReleaseBridgeError, match="legacy execution contract rejected"):
        _validate_current_legacy_reclosure(paths)


@pytest.mark.parametrize("drift", ["receipt", "audit", "authorization", "formal_input"])
def test_c2_bridge_rejects_current_reclosure_source_binding_drift(tmp_path, monkeypatch, drift):
    paths = _current_legacy_reclosure_fixture(tmp_path, monkeypatch)
    paths[drift].write_text('{"drift":true}', encoding="utf-8")
    with pytest.raises(bridge.C2ReleaseBridgeError, match="legacy execution contract rejected"):
        _validate_current_legacy_reclosure(paths)


def test_c2_bridge_projects_strict_same_stem_package(monkeypatch, tmp_path):
    formal = _formal(tmp_path)
    receipt, authorization, out = tmp_path / bridge.ROOT_RECEIPT_NAME, tmp_path / bridge.AUTH_NAME, tmp_path / "out"
    proposal = _receipt(receipt, formal)
    _authorization(authorization)
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    monkeypatch.setattr(bridge, "_validate_root_receipt", lambda *_args: None)
    monkeypatch.setattr(bridge, "_validate_legacy_execution_contract", lambda *_args: None)
    bridge.build_release_package(
        formal_package=formal, ready_proposal=proposal, root_receipt=receipt, authorization=authorization,
        legacy_proposal=receipt, legacy_execution_contract=receipt,
        out=out, tag_generator=_tags,
    )
    record = json.loads((out / bridge.RECORD_NAME).read_text())
    review = json.loads((out / "review_manifest.json").read_text())
    assert record["upload_tags"]["final_tags"] == _tags()["final_tags"]
    assert review["items"] == [bridge._release_item()]
    assert (out / bridge.VIDEO_NAME).read_bytes() == (formal / NAMES["video"]).read_bytes()
    assert bridge.audit_fastlane_c2_release_package(out) == []


@pytest.mark.parametrize("target", [bridge.VIDEO_NAME, bridge.SRT_NAME, bridge.COVER_NAME])
def test_c2_bridge_rejects_final_artifact_drift(monkeypatch, tmp_path, target):
    formal = _formal(tmp_path)
    receipt, authorization, out = tmp_path / bridge.ROOT_RECEIPT_NAME, tmp_path / bridge.AUTH_NAME, tmp_path / "out"
    proposal = _receipt(receipt, formal)
    _authorization(authorization)
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    monkeypatch.setattr(bridge, "_validate_root_receipt", lambda *_args: None)
    monkeypatch.setattr(bridge, "_validate_legacy_execution_contract", lambda *_args: None)
    bridge.build_release_package(formal_package=formal, ready_proposal=proposal, root_receipt=receipt, authorization=authorization, legacy_proposal=receipt, legacy_execution_contract=receipt, out=out, tag_generator=_tags)
    (out / target).write_bytes(b"drift")
    assert bridge.audit_fastlane_c2_release_package(out)[0]["code"] == "C2_RELEASE_CLOSURE_DRIFT"


def test_c2_bridge_rejects_authority_receipt_and_tag_drift(monkeypatch, tmp_path):
    formal = _formal(tmp_path)
    receipt, authorization, out = tmp_path / bridge.ROOT_RECEIPT_NAME, tmp_path / bridge.AUTH_NAME, tmp_path / "out"
    proposal = _receipt(receipt, formal)
    _authorization(authorization)
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    monkeypatch.setattr(bridge, "_validate_root_receipt", lambda *_args: None)
    monkeypatch.setattr(bridge, "_validate_legacy_execution_contract", lambda *_args: None)
    bridge.build_release_package(formal_package=formal, ready_proposal=proposal, root_receipt=receipt, authorization=authorization, legacy_proposal=receipt, legacy_execution_contract=receipt, out=out, tag_generator=_tags)
    record_path = out / bridge.RECORD_NAME
    record = json.loads(record_path.read_text())
    record["legacy_execution_contract"]["sha256"] = "sha256:" + "0" * 64
    record_path.write_text(json.dumps(record), encoding="utf-8")
    assert bridge.audit_fastlane_c2_release_package(out)[0]["code"] == "C2_RELEASE_CLOSURE_DRIFT"


@pytest.mark.parametrize("path_name, mutate", [
    (bridge.ROOT_RECEIPT_NAME, lambda value: value.__setitem__("accepted", False)),
    (bridge.AUTH_NAME, lambda value: value["direct_ivan_lines"][0].__setitem__("quote", "drift")),
    (bridge.TAG_RECEIPT_NAME, lambda value: value.__setitem__("final_tags", ["drift"])),
])
def test_c2_bridge_rejects_receipt_authority_and_tags_drift(monkeypatch, tmp_path, path_name, mutate):
    formal = _formal(tmp_path)
    receipt, authorization, out = tmp_path / bridge.ROOT_RECEIPT_NAME, tmp_path / bridge.AUTH_NAME, tmp_path / "out"
    proposal = _receipt(receipt, formal)
    _authorization(authorization)
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    monkeypatch.setattr(bridge, "_validate_root_receipt", lambda *_args: None)
    monkeypatch.setattr(bridge, "_validate_legacy_execution_contract", lambda *_args: None)
    bridge.build_release_package(formal_package=formal, ready_proposal=proposal, root_receipt=receipt, authorization=authorization, legacy_proposal=receipt, legacy_execution_contract=receipt, out=out, tag_generator=_tags)
    path = out / path_name
    value = json.loads(path.read_text())
    mutate(value)
    path.write_text(json.dumps(value), encoding="utf-8")
    assert bridge.audit_fastlane_c2_release_package(out)[0]["code"] == "C2_RELEASE_CLOSURE_DRIFT"


def test_c2_bridge_rejects_non_c2_authorization(monkeypatch, tmp_path):
    formal = _formal(tmp_path)
    receipt, authorization = tmp_path / bridge.ROOT_RECEIPT_NAME, tmp_path / bridge.AUTH_NAME
    proposal = _receipt(receipt, formal)
    _authorization(authorization)
    payload = json.loads(authorization.read_text())
    payload["candidate_id"] = "other"
    authorization.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(bridge, "audit_fastlane_c2_formal_package", lambda _root: [])
    monkeypatch.setattr(bridge, "_validate_formal_audit", lambda _root: None)
    monkeypatch.setattr(bridge, "_validate_root_receipt", lambda *_args: None)
    monkeypatch.setattr(bridge, "_validate_legacy_execution_contract", lambda *_args: None)
    with pytest.raises(bridge.C2ReleaseBridgeError, match="authorization identity drift"):
        bridge.build_release_package(formal_package=formal, ready_proposal=proposal, root_receipt=receipt, authorization=authorization, legacy_proposal=receipt, legacy_execution_contract=receipt, out=tmp_path / "out", tag_generator=_tags)
