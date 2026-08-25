from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import replay_reviewed_subtitle_baseline as cli
from src.autoslice import published_recovery_package as recovery
from src.autoslice import published_recovery_package_contract as recovery_contract
from src.autoslice import reviewed_baseline_replay as replay
from src.autoslice import producer_package_finalization as producer_finalization
from src.autoslice.recovery_title_authority import build_recovery_publication_authorities
from src.autoslice.repository_asset_authority import _canonical_sha256
from src.autoslice.reviewed_baseline_replay import (
    PrivateReplayPackage,
    ReplayPlan,
    ReviewedBaselineReplayError,
    regular_binding,
)
from src.autoslice.reviewed_baseline_replay_setup import (
    build_reviewed_baseline_replay_spec,
)

CID = "auto_113028_1271_1328"
DATE = "2026-08-14"
BVID = "BV1Sahj65ExM"
TITLE = "【李豆沙】一次点歌巧合被小李解释成量子纠缠，kmx和李豆沙只要产生关系就会量子纠缠"
ROOT = Path(__file__).resolve().parents[1]


def test_canonical_auditor_binds_published_recovery_preflight(tmp_path: Path) -> None:
    from scripts.audit_lidousha_review_package import _audited_inputs

    package = tmp_path / "package"
    package.mkdir()
    preflight_name = f"{CID}.published-recovery-preflight.json"
    receipt_name = f"{CID}.published-recovery-package-receipt.json"
    (package / preflight_name).write_text("{}\n")
    (package / receipt_name).write_text("{}\n")
    (package / "unrelated.json").write_text("{}\n")
    paths = {row["path"] for row in _audited_inputs(package)}
    assert paths == {preflight_name, receipt_name}


def test_committed_c4_c5_recovery_registry_is_hash_bound() -> None:
    authorities = build_recovery_publication_authorities(
        candidate_ids={CID, "auto_113028_1602_1698"},
        registry_path=ROOT / recovery.REGISTRY_REPO_PATH,
        expected_registry_sha256=recovery.REGISTRY_SHA256,
        repo_root=ROOT,
    )
    assert authorities[CID]["bvid"] == BVID
    c4 = "auto_113028_1602_1698"
    assert authorities[c4]["bvid"] == "BV1h7hg68E8Y"
    c5_state = recovery_contract.load_published_recovery_state_authority(
        repo_root=ROOT, candidate_id=CID
    )
    c4_state = recovery_contract.load_published_recovery_state_authority(
        repo_root=ROOT, candidate_id=c4
    )
    assert c5_state["published_cid"] == 41264349440
    assert c4_state["published_cid"] == 41264153653
    assert c4_state["publication_authority_cid"] == 41244820167


def _tree_snapshot(root: Path) -> list[tuple[str, str, int, str | None]]:
    rows = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir() and not path.is_symlink():
            rows.append((relative, "dir", path.stat().st_mode, None))
        elif path.is_file() and not path.is_symlink():
            rows.append((
                relative,
                "file",
                path.stat().st_mode,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            ))
        else:
            rows.append((relative, "unsafe", path.lstat().st_mode, None))
    return rows


def test_c4_published_state_remains_compatible_with_predecessor_repair(
    tmp_path: Path,
) -> None:
    candidate = "auto_113028_1602_1698"
    bvid = "BV1h7hg68E8Y"
    title = "【李豆沙】线上直播间老公不可能是女生，李姐开始男女身份排列组合，竟敢磕男的女的？违背直播间世界观了"
    runtime = tmp_path / "runtime"
    repo = runtime / "repo"
    _write_deployment_authority(repo)
    registry = repo / recovery.REGISTRY_REPO_PATH
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_bytes((ROOT / recovery.REGISTRY_REPO_PATH).read_bytes())
    state_authority = repo / recovery_contract.STATE_AUTHORITY_REPO_PATH
    state_authority.write_bytes(
        (ROOT / recovery_contract.STATE_AUTHORITY_REPO_PATH).read_bytes()
    )
    predecessor_relative = Path(
        "reports/authorized_uploads/2026-08-25-c4-timeaxis-repair/"
        "same-bv-repair-verify-live-20260825.json"
    )
    predecessor = repo / predecessor_relative
    predecessor.parent.mkdir(parents=True, exist_ok=True)
    predecessor.write_bytes((ROOT / predecessor_relative).read_bytes())
    record = tmp_path / "c4.record.json"
    record.write_text("{}\n")
    _write_json(
        runtime / "state" / f"{DATE}.json",
        {
            "picks": [{
                "candidate_id": candidate,
                "status": "published",
                "bvid": bvid,
                "aid": 117154678638617,
                "published_cid": 41264153653,
                "publication_reconciliation": {
                    "schema_version": "publication-reconciliation.v1",
                    "status": "VERIFIED_SAME_BV",
                    "recording_date": DATE,
                    "candidate_id": candidate,
                    "bvid": bvid,
                    "aid": 117154678638617,
                    "cid": 41264153653,
                    "title": title,
                },
            }]
        },
    )
    preflight = recovery.published_recovery_preflight(
        SimpleNamespace(date=DATE, candidate_id=candidate, record_path=record),
        runtime_root=runtime,
        expected_bvid=bvid,
    )
    assert preflight.current_cid == 41264153653
    assert preflight.publication_authority["cid"] == 41244820167
    assert preflight.published_state_authority["predecessor_completed"]["new_cid"] == 41264153653
    assert preflight.publication_authority["bvid"] == bvid
    predecessor.write_text("{}\n")
    with pytest.raises(
        ReviewedBaselineReplayError,
        match="PUBLISHED_RECOVERY_STATE_AUTHORITY_INVALID",
    ):
        recovery.published_recovery_preflight(
            SimpleNamespace(date=DATE, candidate_id=candidate, record_path=record),
            runtime_root=runtime,
            expected_bvid=bvid,
        )


def _write_deployment_authority(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    commit = "a" * 40
    body = {
        "schema_version": "deployed-authority-manifest.v1",
        "deployed_commit": commit,
        "entries": {
            "docs/pipeline/80-package-delivery.md": {
                "bytes": 1,
                "sha256": "sha256:" + "b" * 64,
            }
        },
    }
    _write_json(
        repo / "DEPLOYED_AUTHORITY_MANIFEST.json",
        {**body, "manifest_sha256": _canonical_sha256(body)},
    )
    (repo / "DEPLOYED_COMMIT").write_text(commit + "\n", encoding="utf-8")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _plan(tmp_path: Path) -> ReplayPlan:
    record = tmp_path / "old-record.json"
    record.write_text("{}\n", encoding="utf-8")
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"source")
    old_outer = tmp_path / "runtime" / "out" / DATE / CID
    (old_outer / "replacement_recuts").mkdir(parents=True, mode=0o700)
    return ReplayPlan(
        date=DATE,
        candidate_id=CID,
        package_root=old_outer,
        record_path=record,
        padded_path=padded,
        local_start_ms=9750,
        local_end_ms=67524,
        expected_video_sha256="0" * 64,
        baseline=SimpleNamespace(config={"sha256": "1" * 64}),
        matrix=(),
    )


def _published_state(*, status: str = "published", bvid: str = BVID) -> dict[str, object]:
    return {
        "picks": [
            {
                "candidate_id": CID,
                "status": status,
                "bvid": bvid,
                "aid": 117157295950921,
                "published_cid": 41264349440,
                "publication_reconciliation": {
                    "schema_version": "publication-reconciliation.v1",
                    "status": "VERIFIED_PUBLIC",
                    "recording_date": DATE,
                    "candidate_id": CID,
                    "bvid": bvid,
                    "aid": 117157295950921,
                    "cid": 41264349440,
                    "title": TITLE,
                },
            }
        ]
    }


def _authority_fixture(runtime: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = runtime / "repo"
    _write_deployment_authority(repo)
    state_authority = repo / recovery_contract.STATE_AUTHORITY_REPO_PATH
    state_authority.parent.mkdir(parents=True, exist_ok=True)
    state_authority.write_bytes(
        (ROOT / recovery_contract.STATE_AUTHORITY_REPO_PATH).read_bytes()
    )
    source = repo / "assets/lidousha/source.json"
    receipt = {
        "schema_version": "authorized-upload-public-verify.v2",
        "status": "VERIFIED_PUBLIC",
        "bvid": BVID,
        "manifest_title": TITLE,
        "expected": {"title": TITLE},
        "public_view": {
            "code": 0,
            "state": 0,
            "aid": 117157295950921,
            "cid": 41264349440,
            "title": TITLE,
        },
        "member_archive": {
            "aid": 117157295950921,
            "bvid": BVID,
            "title": TITLE,
        },
    }
    _write_json(source, receipt)
    source_sha = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    registry = repo / "assets/lidousha/registry.json"
    _write_json(
        registry,
        {
            "schema_version": "lidousha-recovery-publication-authority.v1",
            "authority": "Ivan directed an in-place same-BV recovery.",
            "entries": [
                {
                    "candidate_id": CID,
                    "title_mode": "verified_public_exact",
                    "observed_public_title": TITLE,
                    "required_given_end_ms": 67524,
                    "boundary_end_mode": "semantic_lower_bound",
                    "bvid": BVID,
                    "aid": 117157295950921,
                    "cid": 41264349440,
                    "source_public_verify_repo_path": "assets/lidousha/source.json",
                    "source_public_verify_sha256": source_sha,
                    "source_public_verify_schema_version": "authorized-upload-public-verify.v2",
                    "source_public_verify_status": "VERIFIED_PUBLIC",
                }
            ],
        },
    )
    monkeypatch.setattr(recovery, "REGISTRY_REPO_PATH", "assets/lidousha/registry.json")
    monkeypatch.setattr(
        recovery,
        "REGISTRY_SHA256",
        "sha256:" + hashlib.sha256(registry.read_bytes()).hexdigest(),
    )


def test_published_recovery_preflight_binds_exact_state_and_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    runtime = tmp_path / "runtime"
    _authority_fixture(runtime, monkeypatch)
    _write_json(runtime / "state" / f"{DATE}.json", _published_state())

    result = recovery.published_recovery_preflight(
        plan, runtime_root=runtime, expected_bvid=BVID
    )

    assert result.bvid == BVID
    assert result.current_cid == 41264349440
    assert result.title == TITLE
    assert result.state_sha256 == "sha256:" + hashlib.sha256(result.state_bytes).hexdigest()
    assert result.publication_authority["authority_sha256"].startswith("sha256:")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (_published_state(status="candidate_rejected"), "PUBLISHED_RECOVERY_STATE_PICK_PREIMAGE_DRIFT"),
        (_published_state(bvid="BV1BadBadBad"), "PUBLISHED_RECOVERY_RECONCILIATION_DRIFT"),
    ],
)
def test_published_recovery_preflight_fails_closed_on_state_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, object],
    expected: str,
) -> None:
    plan = _plan(tmp_path)
    runtime = tmp_path / "runtime"
    _authority_fixture(runtime, monkeypatch)
    _write_json(runtime / "state" / f"{DATE}.json", state)

    with pytest.raises(ReviewedBaselineReplayError, match=expected):
        recovery.published_recovery_preflight(
            plan, runtime_root=runtime, expected_bvid=BVID
        )


def test_recovery_replay_spec_pairs_authority_with_exact_given_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    runtime = tmp_path / "runtime"
    _authority_fixture(runtime, monkeypatch)
    _write_json(runtime / "state" / f"{DATE}.json", _published_state())
    preflight = recovery.published_recovery_preflight(
        plan, runtime_root=runtime, expected_bvid=BVID
    )
    spec = build_reviewed_baseline_replay_spec(
        date=DATE,
        candidate_id=CID,
        output_root=tmp_path / "output",
        story={"candidate_id": CID, "selection_hook": "量子纠缠点歌巧合"},
        normalized_piece={},
        spec_piece={},
        clip_context_path=tmp_path / "clip.json",
        clip_context={},
        baseline_config={},
        boundary={},
        error_factory=ReviewedBaselineReplayError,
        recovery_publication_authority=preflight.publication_authority,
    )
    assert spec["given_title"] == TITLE
    assert spec["recovery_publication_authority"] == preflight.publication_authority

    def after_pair_gate(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("AFTER_RECOVERY_AUTHORITY_PAIR_GATE")

    committed_authority = build_recovery_publication_authorities(
        candidate_ids={CID},
        registry_path=ROOT / recovery_contract.REGISTRY_REPO_PATH,
        expected_registry_sha256=recovery_contract.REGISTRY_SHA256,
        repo_root=ROOT,
    )[CID]
    stage_spec = {
        **spec,
        "given_title": TITLE,
        "recovery_publication_authority": committed_authority,
    }
    monkeypatch.setattr(producer_finalization, "build_llm_call", after_pair_gate)
    with pytest.raises(RuntimeError, match="AFTER_RECOVERY_AUTHORITY_PAIR_GATE"):
        producer_finalization._stage_record(
            options=SimpleNamespace(reuse_cover=False),
            spec=stage_spec,
            cid=CID,
            recut=SimpleNamespace(
                recut_dir=tmp_path / "recut",
                subtitle_path=tmp_path / "subtitle.srt",
            ),
            record={},
            adapters=SimpleNamespace(),
        )


def test_normal_replay_state_projection_still_rejects_published_pick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    monkeypatch.setattr(
        replay,
        "_state_document",
        lambda *_a, **_kw: (_published_state(), b"published-state\n"),
    )
    with pytest.raises(ReviewedBaselineReplayError, match="REPLAY_STATE_PICK_PREIMAGE_DRIFT"):
        replay.project_replay_state_after(
            plan,
            runtime_root=tmp_path / "runtime",
            state_path=tmp_path / "runtime/state" / f"{DATE}.json",
            finalization=SimpleNamespace(),
            projection=SimpleNamespace(),
        )


def _preflight_for_materialization(
    plan: ReplayPlan,
    runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> recovery.PublishedRecoveryPreflight:
    _authority_fixture(runtime, monkeypatch)
    _write_json(runtime / "state" / f"{DATE}.json", _published_state())
    return recovery.published_recovery_preflight(
        plan, runtime_root=runtime, expected_bvid=BVID
    )


@pytest.mark.parametrize(
    "surface",
    [
        "state_sha", "bvid", "aid", "cid", "title", "authority_sha",
        "source_sha", "transition", "upload", "state_path",
    ],
)
def test_recovery_preflight_and_receipt_tampering_is_semantically_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    plan = _plan(tmp_path)
    runtime = tmp_path / "runtime"
    preflight = _preflight_for_materialization(plan, runtime, monkeypatch)
    package = tmp_path / "contract-package"
    package.mkdir()
    for suffix, payload in ((".mp4", b"video"), (".srt", b"subtitle"),
                            (".cover.png", b"cover")):
        (package / f"{CID}{suffix}").write_bytes(payload)
    evidence_path = package / f"{CID}.published-recovery-preflight.json"
    _write_json(package / evidence_path.name, recovery._preflight_evidence(plan=plan, preflight=preflight))
    receipt_path = package / f"{CID}.published-recovery-package-receipt.json"
    receipt = recovery_contract.build_published_recovery_package_receipt(
        package_root=package,
        candidate_id=CID,
        recovery_publication_authority=preflight.publication_authority,
    )
    _write_json(receipt_path, receipt)
    item = {
        "candidate_id": CID,
        "published_recovery_package_receipt": receipt_path.name,
        "published_recovery_package_receipt_sha256": (
            "sha256:" + hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        ),
    }
    assert recovery_contract.audit_published_recovery_manifest_binding(
        package, {CID: preflight.publication_authority}, [item]
    ) == []
    evidence = json.loads(evidence_path.read_text())
    if surface == "state_sha":
        evidence["production_state"]["sha256"] = "sha256:" + "0" * 64
    elif surface in {"bvid", "aid", "cid", "title"}:
        field = "current_state_cid" if surface == "cid" else surface
        evidence["target"][field] = "drift" if surface in {"bvid", "title"} else 1
    elif surface == "authority_sha":
        evidence["publication_authority_sha256"] = "sha256:" + "0" * 64
    elif surface == "source_sha":
        evidence["source_record_sha256"] = "sha256:" + "0" * 64
    elif surface == "transition":
        evidence["state_transition"] = "published"
    elif surface == "upload":
        evidence["upload_allowed"] = True
    else:
        evidence["production_state"]["path"] = "/tmp/wrong.json"
    _write_json(evidence_path, evidence)
    assert recovery_contract.audit_published_recovery_manifest_binding(
        package, {CID: preflight.publication_authority}, [item]
    )


def test_recovery_preflight_duplicate_or_missing_receipt_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    runtime = tmp_path / "runtime"
    preflight = _preflight_for_materialization(plan, runtime, monkeypatch)
    package = tmp_path / "ambiguous-package"
    package.mkdir()
    _write_json(
        package / f"{CID}.published-recovery-preflight.json",
        recovery._preflight_evidence(plan=plan, preflight=preflight),
    )
    assert recovery_contract.audit_published_recovery_manifest_binding(
        package, {CID: preflight.publication_authority}, []
    )
    _write_json(
        package / "duplicate.published-recovery-preflight.json",
        recovery._preflight_evidence(plan=plan, preflight=preflight),
    )
    issues = recovery_contract.audit_published_recovery_manifest_binding(
        package, {CID: preflight.publication_authority}, []
    )
    assert issues[0].code == "PUBLISHED_RECOVERY_PREFLIGHT_AMBIGUOUS"


def test_prepare_published_recovery_materializes_private_package_without_state_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    runtime = tmp_path / "runtime"
    preflight = _preflight_for_materialization(plan, runtime, monkeypatch)
    state_before = preflight.state_bytes
    for relative in (
        "out/2026-08-14/sentinel", "delivery/sentinel",
        "ledgers/sentinel", "reports/sentinel",
    ):
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("unchanged\n")
    runtime_before = _tree_snapshot(runtime)
    package = tmp_path / "prepared-package"
    package.mkdir()
    for name, payload in {
        f"{CID}.mp4": b"video",
        f"{CID}.srt": b"subtitle",
        f"{CID}.cover.png": b"cover",
        f"{CID}.chat-authority.json": b"{}\n",
        f"{CID}.clip-context.json": b"{}\n",
        f"{CID}.published-recovery-preflight.json": b"{}\n",
        "review_manifest.json": b"{}\n",
    }.items():
        (package / name).write_bytes(payload)
    audit = {"passed": True, "issue_count": 0, "blocking_issue_count": 0}
    _write_json(package / "package-audit.json", audit)
    private_package = PrivateReplayPackage(
        root=package,
        review_manifest=regular_binding(package / "review_manifest.json", label="TEST"),
        package_audit=regular_binding(package / "package-audit.json", label="TEST"),
        predicate_matrix=(),
    )
    projection = SimpleNamespace(marker="projection")
    monkeypatch.setattr(
        recovery, "project_private_finalization_to_live", lambda *_a, **_kw: projection
    )
    def flatten(*_args: object, **kwargs: object) -> PrivateReplayPackage:
        base = Path(str(kwargs["base_package_root"]))
        (package / f"{CID}.published-recovery-preflight.json").write_bytes(
            (base / f"{CID}.published-recovery-preflight.json").read_bytes()
        )
        kwargs["package_postprocessor"](package)
        package_receipt = package / f"{CID}.published-recovery-package-receipt.json"
        _write_json(
            package / "review_manifest.json",
            {"items": [{
                "candidate_id": CID,
                "published_recovery_package_receipt": package_receipt.name,
                "published_recovery_package_receipt_sha256": (
                    "sha256:" + hashlib.sha256(package_receipt.read_bytes()).hexdigest()
                ),
            }]},
        )
        return private_package

    monkeypatch.setattr(recovery, "flatten_and_audit_private_replay", flatten)
    from scripts import audit_lidousha_review_package

    monkeypatch.setattr(audit_lidousha_review_package, "audit_package", lambda _root: audit)
    destination_parent = tmp_path / "operator-private"
    destination_parent.mkdir(mode=0o700)
    destination = destination_parent / CID

    private_runtime = tmp_path / "private-runtime"
    private_runtime.mkdir()
    result = recovery.prepare_published_recovery_package(
        plan,
        finalization=SimpleNamespace(private_runtime_root=private_runtime),
        runtime_root=runtime,
        preflight=preflight,
        package_outer_root=destination,
        persist=True,
    )

    assert result.package_outer_root == destination
    final_video = destination / "replacement_recuts" / f"{CID}.mp4"
    assert final_video.read_bytes() == b"video"
    assert (final_video.stat().st_dev, final_video.stat().st_ino) != (
        (package / f"{CID}.mp4").stat().st_dev,
        (package / f"{CID}.mp4").stat().st_ino,
    )
    receipt = json.loads((destination / "published-recovery-package.json").read_text())
    evidence = json.loads(
        (destination / f"replacement_recuts/{CID}.published-recovery-preflight.json").read_text()
    )
    assert evidence["production_state"]["sha256"] == preflight.state_sha256
    assert evidence["state_transition"] == "none"
    assert receipt["status"] == "VERIFIED_PRIVATE_PACKAGE"
    assert receipt["state_transition"] == "none"
    assert receipt["same_bv_only"] is True
    assert receipt["upload_allowed"] is False
    bound = recovery_contract.validate_materialized_published_recovery_package(
        destination / "replacement_recuts", live_authority_recheck=False
    )
    assert bound["path"] == str(
        (destination / "published-recovery-package.json").resolve()
    )
    assert preflight.state_path.read_bytes() == state_before
    assert _tree_snapshot(runtime) == runtime_before


def test_failed_final_audit_leaves_destination_absent_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    runtime = tmp_path / "runtime"
    preflight = _preflight_for_materialization(plan, runtime, monkeypatch)
    package = tmp_path / "prepared-package"
    package.mkdir()
    for name in (
        f"{CID}.mp4", f"{CID}.srt", f"{CID}.chat-authority.json",
        f"{CID}.clip-context.json", f"{CID}.published-recovery-preflight.json",
        "review_manifest.json",
    ):
        (package / name).write_text("sealed\n")
    stored = {"passed": True, "issue_count": 0, "blocking_issue_count": 0}
    _write_json(package / "package-audit.json", stored)
    private_package = PrivateReplayPackage(
        root=package,
        review_manifest=regular_binding(package / "review_manifest.json", label="TEST"),
        package_audit=regular_binding(package / "package-audit.json", label="TEST"),
        predicate_matrix=(),
    )
    from scripts import audit_lidousha_review_package

    monkeypatch.setattr(
        audit_lidousha_review_package,
        "audit_package",
        lambda _root: {"passed": False, "issue_count": 1},
    )
    parent = tmp_path / "operator-private"
    parent.mkdir(mode=0o700)
    destination = parent / CID
    with pytest.raises(
        ReviewedBaselineReplayError,
        match="PUBLISHED_RECOVERY_PACKAGE_AUDIT_DRIFT",
    ):
        recovery._materialize(
            plan=plan,
            package=private_package,
            destination_outer=destination,
            preflight=preflight,
            runtime_root=runtime,
        )
    assert not destination.exists()
    assert not list(parent.glob(f".{CID}.published-recovery-*"))


def test_atomic_noreplace_commit_preserves_concurrent_destination(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    source = parent / "source"
    source.mkdir(mode=0o700)
    (source / "payload").write_text("new\n")
    destination = parent / "destination"
    destination.mkdir(mode=0o700)
    sentinel = destination / "sentinel"
    sentinel.write_text("keep\n")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    parent_fd = os.open(parent, flags)
    try:
        with pytest.raises(
            ReviewedBaselineReplayError,
            match="PUBLISHED_RECOVERY_DESTINATION_NOT_CREATE_ONLY",
        ):
            recovery._rename_noreplace(parent_fd, source.name, destination.name)
    finally:
        os.close(parent_fd)
    assert sentinel.read_text() == "keep\n"
    assert (source / "payload").read_text() == "new\n"


def test_published_recovery_destination_never_replaces_existing_package(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    parent = tmp_path / "operator-private"
    parent.mkdir(mode=0o700)
    destination = parent / CID
    destination.mkdir(mode=0o700)
    sentinel = destination / "sentinel"
    sentinel.write_text("keep\n")
    with pytest.raises(
        ReviewedBaselineReplayError,
        match="PUBLISHED_RECOVERY_DESTINATION_NOT_CREATE_ONLY",
    ):
        recovery._private_destination(
            destination, runtime_root=runtime, candidate_id=CID
        )
    assert sentinel.read_text() == "keep\n"
    other_parent = tmp_path / "other-private-parent"
    other_parent.mkdir(mode=0o700)
    alias = tmp_path / "private-parent-symlink"
    alias.symlink_to(other_parent, target_is_directory=True)
    with pytest.raises(ReviewedBaselineReplayError, match="REPLAY_PATH_UNSAFE"):
        recovery._private_destination(
            alias / CID,
            runtime_root=runtime,
            candidate_id=CID,
        )


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("record", "PUBLISHED_RECOVERY_SOURCE_RECORD_DRIFT"),
        ("registry", "PUBLISHED_RECOVERY_AUTHORITY_DRIFT"),
        ("state_authority", "PUBLISHED_RECOVERY_STATE_AUTHORITY_DRIFT"),
        ("deployment", "PUBLISHED_RECOVERY_DEPLOYMENT_AUTHORITY_DRIFT"),
    ],
)
def test_prepare_published_recovery_revalidates_every_authority_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    expected: str,
) -> None:
    plan = _plan(tmp_path)
    runtime = tmp_path / "runtime"
    preflight = _preflight_for_materialization(plan, runtime, monkeypatch)
    if surface == "record":
        plan.record_path.write_text('{"tampered":true}\n')
    elif surface == "registry":
        (runtime / "repo" / recovery.REGISTRY_REPO_PATH).write_text("{}\n")
    elif surface == "state_authority":
        (runtime / "repo" / recovery_contract.STATE_AUTHORITY_REPO_PATH).write_text(
            "{}\n"
        )
    else:
        (runtime / "repo/DEPLOYED_COMMIT").write_text("b" * 40 + "\n")
    called = False

    def project(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(recovery, "project_private_finalization_to_live", project)
    parent = tmp_path / "operator-private"
    parent.mkdir(mode=0o700)
    with pytest.raises(ReviewedBaselineReplayError, match=expected):
        recovery.prepare_published_recovery_package(
            plan,
            finalization=SimpleNamespace(private_runtime_root=tmp_path / "unused"),
            runtime_root=runtime,
            preflight=preflight,
            package_outer_root=parent / CID,
            persist=True,
        )
    assert called is False
    assert not (parent / CID).exists()


def test_prepare_published_recovery_rejects_state_change_before_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    runtime = tmp_path / "runtime"
    preflight = _preflight_for_materialization(plan, runtime, monkeypatch)
    _write_json(preflight.state_path, {"picks": []})
    called = False

    def project(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(recovery, "project_private_finalization_to_live", project)
    destination_parent = tmp_path / "operator-private"
    destination_parent.mkdir(mode=0o700)

    with pytest.raises(ReviewedBaselineReplayError, match="PUBLISHED_RECOVERY_STATE_DRIFT"):
        recovery.prepare_published_recovery_package(
            plan,
            finalization=SimpleNamespace(),
            runtime_root=runtime,
            preflight=preflight,
            package_outer_root=destination_parent / CID,
            persist=True,
        )
    assert called is False
    assert not (destination_parent / CID).exists()


def test_published_recovery_matrix_is_unique_and_upload_false_last() -> None:
    rows = cli._published_recovery_matrix((
        {"predicate": "SEALED_BASELINE", "status": "PASS"},
        {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"},
    ))
    assert rows[-1] == {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"}
    assert {row["predicate"] for row in rows} == {
        "SEALED_BASELINE", "PUBLISHED_STATE_SAME_BV_AUTHORITY",
        "STATE_TRANSITION", "UPLOAD_ALLOWED",
    }


def test_cli_recovery_flags_are_explicit_and_create_only(tmp_path: Path) -> None:
    base = [
        "--runtime-root", str(tmp_path), "--date", DATE,
        "--candidate-id", CID, "--private-stage-parent", str(tmp_path),
        "--published-recovery-bvid", BVID,
    ]
    with pytest.raises(SystemExit):
        cli._args([*base, "--apply"])
    with pytest.raises(SystemExit):
        cli._args([*base, "--full-dry-run", "--recovery-package-root", str(tmp_path / CID)])
    parsed = cli._args(
        [*base, "--apply", "--recovery-package-root", str(tmp_path / CID)]
    )
    assert parsed.published_recovery_bvid == BVID
    assert parsed.recovery_package_root == tmp_path / CID


def test_prepare_recovery_dispatches_without_normal_state_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan(tmp_path)
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    stage = parent / "candidate-stage"
    stage.mkdir()
    (stage / "stage.json").write_text("{}\n", encoding="utf-8")
    finalization = SimpleNamespace(private_runtime_root=stage, prepared_manifest=stage / "p.json")
    published = SimpleNamespace(publication_authority={"authority_sha256": "sha256:" + "a" * 64})
    sentinel = SimpleNamespace(receipt={"state_transition": "none"})
    monkeypatch.setattr(cli, "stage_replay", lambda *_a, **_kw: {"stage": str(stage)})
    monkeypatch.setattr(cli, "_production_llm_call", lambda **_kw: lambda _prompt: "{}")
    captured: dict[str, object] = {}

    def synthesize(*_args: object, **kwargs: object) -> object:
        captured["authority"] = kwargs.get("recovery_publication_authority")
        return finalization

    monkeypatch.setattr(cli, "synthesize_replay_spec_and_finalize_private", synthesize)
    monkeypatch.setattr(cli, "_provider_receipt_sha256s", lambda _root: ())
    monkeypatch.setattr(cli, "_prepared_manifest_sha256", lambda _finalization: "sha256:" + "b" * 64)
    monkeypatch.setattr(
        cli,
        "prepare_published_recovery_package",
        lambda *_a, **_kw: sentinel,
    )
    monkeypatch.setattr(
        cli,
        "prepare_replay_after_image",
        lambda *_a, **_kw: pytest.fail("normal state projection must not run"),
    )

    prepared = cli._prepare(
        plan,
        runtime=tmp_path / "runtime",
        stage_parent=parent,
        published_recovery=published,
        recovery_package_root=tmp_path / "output" / CID,
        persist_recovery=True,
    )

    assert prepared[2] is sentinel
    assert captured["authority"] == published.publication_authority
