from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import src.autoslice.recovery_publication_authority_migration as migration_module
from src.autoslice.recovery_publication_authority_migration import (
    POLICY_REPO_PATH,
    POLICY_SHA256,
    RecoveryPublicationAuthorityMigrationError,
    validate_successor_authority_migration,
)
from src.autoslice.recovery_title_authority import (
    ROOT,
    build_recovery_publication_authorities,
)


CANDIDATE_ID = "auto_113028_1602_1698"
BVID = "BV1h7hg68E8Y"
PLAN_SHA256 = "eb8aa715d68c70db9d144da9dc8c760295c43f323a221301b80e450f66ab639c"
COMPLETED_SHA256 = (
    "265304913afa62c938a9adf69770d5ec55ae4f0a8fc113b74fc27630f3c6b1dc"
)
OLD_REGISTRY = (
    ROOT
    / "assets/lidousha"
    / "recovery_publication_authority_2026-08-25_c4_timeaxis.v1.json"
)
OLD_REGISTRY_SHA256 = (
    "sha256:08d549fa718b41c6568a8d7577cb2a2d7d918917a0455604c4d6a579c3aeefc7"
)
CURRENT_REGISTRY = (
    ROOT
    / "assets/lidousha"
    / "recovery_publication_authority_2026-08-25_c4_c5_timeaxis.v1.json"
)
CURRENT_REGISTRY_SHA256 = (
    "sha256:25ad60011739fdc661871eea0fc87e0ba61dfd3f034cbe9e5993999a473fe204"
)


def _authorities(*, repo_root: Path = ROOT) -> tuple[dict, dict]:
    old = build_recovery_publication_authorities(
        candidate_ids={CANDIDATE_ID},
        registry_path=(
            repo_root
            / "assets/lidousha"
            / OLD_REGISTRY.name
        ),
        expected_registry_sha256=OLD_REGISTRY_SHA256,
        require_exact_candidate_set=True,
        repo_root=repo_root,
    )[CANDIDATE_ID]
    current = build_recovery_publication_authorities(
        candidate_ids={CANDIDATE_ID},
        registry_path=(
            repo_root
            / "assets/lidousha"
            / CURRENT_REGISTRY.name
        ),
        expected_registry_sha256=CURRENT_REGISTRY_SHA256,
        repo_root=repo_root,
    )[CANDIDATE_ID]
    return old, current


def _validate(
    old: object,
    current: object,
    **overrides,
) -> dict[str, object]:
    arguments = {
        "candidate_id": CANDIDATE_ID,
        "bvid": BVID,
        "predecessor_plan_sha256": PLAN_SHA256,
        "predecessor_completed_sha256": COMPLETED_SHA256,
        "preserve_existing_tags": True,
        "repo_root": ROOT,
    }
    arguments.update(overrides)
    return validate_successor_authority_migration(
        old,
        current,
        **arguments,
    )


def _resign(authority: dict[str, object]) -> None:
    unsigned = {
        key: value
        for key, value in authority.items()
        if key != "authority_sha256"
    }
    raw = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    authority["authority_sha256"] = (
        "sha256:" + hashlib.sha256(raw).hexdigest()
    )


def test_exact_c4_authority_migration_is_directional_and_attested():
    old, current = _authorities()

    attestation = _validate(old, current)

    assert attestation == {
        "schema_version": (
            "recovery-publication-authority-migration-attestation.v1"
        ),
        "migration_id": "c4-2026-08-25-single-to-combined-v1",
        "policy": {
            "repo_path": POLICY_REPO_PATH,
            "sha256": POLICY_SHA256,
        },
        "migration_sha256": (
            "sha256:8e54a7fea971ed067aa15e0d182e1faafd1854434132cec8e020aa99ccc3b858"
        ),
        "from_authority_sha256": (
            "sha256:8f16a9849b0df1be496f2ddd7578563d0f7f63dc5a1b9239a4c3b6652f1b7937"
        ),
        "to_authority_sha256": (
            "sha256:69efe8489cc84bf45cad252281561449b7c5a75b7b70505323a56c20432639fc"
        ),
        "predecessor_plan_sha256": "sha256:" + PLAN_SHA256,
        "predecessor_completed_sha256": "sha256:" + COMPLETED_SHA256,
        "preserve_existing_tags": True,
    }


@pytest.mark.parametrize(
    ("damage", "error"),
    [
        ("reverse", "MIGRATION_FROM_MISMATCH"),
        ("same_endpoint", "MIGRATION_TO_MISMATCH"),
        ("plan_sha", "MIGRATION_SCOPE_MISMATCH"),
        ("completed_sha", "MIGRATION_SCOPE_MISMATCH"),
        ("bvid", "MIGRATION_SCOPE_MISMATCH"),
        ("candidate", "MIGRATION_ENDPOINT_INVALID"),
        ("no_tag_preservation", "MIGRATION_REQUIRES_TAG_PRESERVATION"),
        ("from_hash", "MIGRATION_ENDPOINT_INVALID"),
        ("to_hash", "MIGRATION_ENDPOINT_INVALID"),
        ("to_candidate", "MIGRATION_ENDPOINT_INVALID"),
        ("to_aid", "MIGRATION_ENDPOINT_INVALID"),
        ("to_cid", "MIGRATION_ENDPOINT_INVALID"),
        ("to_title", "MIGRATION_ENDPOINT_INVALID"),
        ("to_boundary", "MIGRATION_ENDPOINT_INVALID"),
        ("to_registry_authority", "MIGRATION_ENDPOINT_INVALID"),
        ("to_registry_path", "MIGRATION_ENDPOINT_INVALID"),
        ("to_registry_sha", "MIGRATION_ENDPOINT_INVALID"),
        ("to_source_path", "MIGRATION_ENDPOINT_INVALID"),
        ("to_source_schema", "MIGRATION_ENDPOINT_INVALID"),
        ("to_source_sha", "MIGRATION_ENDPOINT_INVALID"),
        ("to_source_status", "MIGRATION_ENDPOINT_INVALID"),
        ("extra_field", "MIGRATION_ENDPOINT_INVALID"),
    ],
)
def test_migration_rejects_every_scope_or_endpoint_drift(
    damage: str,
    error: str,
):
    old, current = _authorities()
    kwargs = {}
    if damage == "reverse":
        old, current = current, old
    elif damage == "same_endpoint":
        current = copy.deepcopy(old)
    elif damage == "plan_sha":
        kwargs["predecessor_plan_sha256"] = "0" * 64
    elif damage == "completed_sha":
        kwargs["predecessor_completed_sha256"] = "0" * 64
    elif damage == "bvid":
        kwargs["bvid"] = "BV1Mug46EEQz"
    elif damage == "candidate":
        kwargs["candidate_id"] = "candidate-other"
    elif damage == "no_tag_preservation":
        kwargs["preserve_existing_tags"] = False
    elif damage == "from_hash":
        old["authority_sha256"] = "sha256:" + "0" * 64
    elif damage == "to_hash":
        current["authority_sha256"] = "sha256:" + "0" * 64
    elif damage == "to_candidate":
        current["candidate_id"] = "candidate-other"
        _resign(current)
    elif damage == "to_aid":
        current["aid"] += 1
        _resign(current)
    elif damage == "to_cid":
        current["cid"] += 1
        _resign(current)
    elif damage == "to_title":
        current["observed_public_title"] += "漂移"
        _resign(current)
    elif damage == "to_boundary":
        current["required_given_end_ms"] += 1
        _resign(current)
    elif damage == "to_registry_authority":
        current["registry_authority"] += " drift"
        _resign(current)
    elif damage == "to_registry_path":
        current["registry_repo_path"] = old["registry_repo_path"]
        _resign(current)
    elif damage == "to_registry_sha":
        current["registry_sha256"] = "sha256:" + "0" * 64
        _resign(current)
    elif damage == "to_source_path":
        current["source_public_verify_repo_path"] = old[
            "source_public_verify_repo_path"
        ]
        _resign(current)
    elif damage == "to_source_schema":
        current["source_public_verify_schema_version"] = "unexpected.v1"
        _resign(current)
    elif damage == "to_source_sha":
        current["source_public_verify_sha256"] = "sha256:" + "0" * 64
        _resign(current)
    elif damage == "to_source_status":
        current["source_public_verify_status"] = "UNVERIFIED"
        _resign(current)
    elif damage == "extra_field":
        current["unexpected"] = True
        _resign(current)
    else:
        raise AssertionError(f"unknown damage: {damage}")

    with pytest.raises(
        RecoveryPublicationAuthorityMigrationError,
        match=error,
    ):
        _validate(old, current, **kwargs)


def _copy_runtime_authority_tree(repo: Path) -> None:
    for source in (
        OLD_REGISTRY,
        CURRENT_REGISTRY,
        ROOT
        / "reports/authorized_uploads/2026-08-25-c4-timeaxis-repair"
        / "auto_113028_1602_1698.public_verify.json",
        ROOT
        / "assets/lidousha/published_recovery_evidence"
        / "c4-original-public-verify.json",
        ROOT
        / "assets/lidousha/published_recovery_evidence"
        / "c5-original-public-verify.json",
    ):
        relative = source.relative_to(ROOT)
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())


def test_clean_copied_deployment_tree_replays_from_captured_bytes(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    _copy_runtime_authority_tree(repo)
    policy = repo / POLICY_REPO_PATH
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_bytes((ROOT / POLICY_REPO_PATH).read_bytes())
    old, current = _authorities(repo_root=repo)

    attestation = validate_successor_authority_migration(
        old,
        current,
        candidate_id=CANDIDATE_ID,
        bvid=BVID,
        predecessor_plan_sha256=PLAN_SHA256,
        predecessor_completed_sha256=COMPLETED_SHA256,
        preserve_existing_tags=True,
        repo_root=repo,
    )

    assert attestation["migration_id"] == (
        "c4-2026-08-25-single-to-combined-v1"
    )


@pytest.mark.parametrize(
    ("damage", "error"),
    [
        ("missing", "MIGRATION_POLICY_MISSING"),
        ("single_byte", "MIGRATION_POLICY_HASH_DRIFT"),
        ("symlink", "MIGRATION_POLICY_UNSAFE"),
        ("policy_parent_symlink", "MIGRATION_POLICY_UNSAFE"),
        ("old_registry_missing", "MIGRATION_ENDPOINT_REGISTRY_MISSING"),
        ("old_registry_symlink", "MIGRATION_ENDPOINT_REGISTRY_UNSAFE"),
        ("old_source_missing", "MIGRATION_ENDPOINT_SOURCE_MISSING"),
        ("old_source_symlink", "MIGRATION_ENDPOINT_SOURCE_UNSAFE"),
        ("old_source_parent_symlink", "MIGRATION_ENDPOINT_SOURCE_UNSAFE"),
        ("current_source_parent_symlink", "MIGRATION_ENDPOINT_SOURCE_UNSAFE"),
    ],
)
def test_migration_policy_and_historical_endpoint_fail_closed(
    tmp_path: Path,
    damage: str,
    error: str,
):
    repo = tmp_path / "repo"
    _copy_runtime_authority_tree(repo)
    policy = repo / POLICY_REPO_PATH
    policy.parent.mkdir(parents=True, exist_ok=True)
    if damage != "missing":
        policy.write_bytes((ROOT / POLICY_REPO_PATH).read_bytes())
    if damage == "single_byte":
        policy.write_bytes(policy.read_bytes() + b" ")
    elif damage == "symlink":
        target = policy.with_name("migration-target.json")
        policy.replace(target)
        policy.symlink_to(target)
    old, current = _authorities(repo_root=repo)
    if damage == "policy_parent_symlink":
        parent = policy.parent
        target = parent.with_name("lidousha-real")
        parent.replace(target)
        parent.symlink_to(target, target_is_directory=True)
    elif damage == "old_registry_missing":
        (repo / OLD_REGISTRY.relative_to(ROOT)).unlink()
    elif damage == "old_registry_symlink":
        registry = repo / OLD_REGISTRY.relative_to(ROOT)
        target = registry.with_name("old-registry-target.json")
        registry.replace(target)
        registry.symlink_to(target)
    elif damage in {
        "old_source_missing",
        "old_source_symlink",
        "old_source_parent_symlink",
    }:
        source = (
            repo
            / "reports/authorized_uploads/2026-08-25-c4-timeaxis-repair"
            / "auto_113028_1602_1698.public_verify.json"
        )
        if damage == "old_source_missing":
            source.unlink()
        elif damage == "old_source_symlink":
            target = source.with_name("public-verify-target.json")
            source.replace(target)
            source.symlink_to(target)
        else:
            parent = source.parent
            target = parent.with_name(parent.name + "-real")
            parent.replace(target)
            parent.symlink_to(target, target_is_directory=True)
    elif damage == "current_source_parent_symlink":
        parent = (
            repo / "assets/lidousha/published_recovery_evidence"
        )
        target = parent.with_name("published-recovery-evidence-real")
        parent.replace(target)
        parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(
        RecoveryPublicationAuthorityMigrationError,
        match=error,
    ):
        validate_successor_authority_migration(
            old,
            current,
            candidate_id=CANDIDATE_ID,
            bvid=BVID,
            predecessor_plan_sha256=PLAN_SHA256,
            predecessor_completed_sha256=COMPLETED_SHA256,
            preserve_existing_tags=True,
            repo_root=repo,
        )


def test_endpoint_registry_rejects_symlinked_parent(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    _copy_runtime_authority_tree(repo)
    old, _ = _authorities(repo_root=repo)
    parent = repo / "assets/lidousha"
    target = repo / "assets/lidousha-real"
    parent.replace(target)
    parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(
        RecoveryPublicationAuthorityMigrationError,
        match="MIGRATION_ENDPOINT_REGISTRY_UNSAFE",
    ):
        migration_module._validate_captured_endpoint(
            old,
            candidate_id=CANDIDATE_ID,
            expected_title=old["observed_public_title"],
            repo_root=repo,
        )


def test_descriptor_capture_rejects_path_replacement_race(
    tmp_path: Path,
    monkeypatch,
):
    victim = tmp_path / "authority.json"
    replacement = tmp_path / "replacement.json"
    old_bytes = b"original-authority-bytes"
    victim.write_bytes(old_bytes)
    replacement.write_bytes(b"replacement-bytes")
    real_read = migration_module.os.read
    replaced = False

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            victim.replace(tmp_path / "opened-inode.json")
            replacement.replace(victim)
        return real_read(descriptor, count)

    monkeypatch.setattr(migration_module.os, "read", racing_read)
    with pytest.raises(
        RecoveryPublicationAuthorityMigrationError,
        match="CHANGED",
    ):
        migration_module._capture_anchored_file(
            tmp_path,
            Path("authority.json"),
            missing_code="MISSING",
            unsafe_code="UNSAFE",
            changed_code="CHANGED",
        )
    assert victim.read_bytes() == b"replacement-bytes"


@pytest.mark.parametrize(
    ("target_kind", "error"),
    [
        ("plan", "MIGRATION_PREDECESSOR_PLAN_UNSAFE"),
        ("completed", "MIGRATION_PREDECESSOR_COMPLETED_UNSAFE"),
    ],
)
def test_predecessor_binding_rejects_symlinked_parent(
    tmp_path: Path,
    target_kind: str,
    error: str,
):
    predecessor_plan = {
        "recovery_publication_authority": {"authority_sha256": "old"}
    }
    completed = {"status": "VERIFIED_FRESH_LIVE"}
    plan_real_parent = tmp_path / "plan-real"
    completed_real_parent = tmp_path / "completed-real"
    plan_real_parent.mkdir()
    completed_real_parent.mkdir()
    plan_path = plan_real_parent / "plan.json"
    completed_path = completed_real_parent / "completed.json"
    plan_path.write_text(json.dumps(predecessor_plan), encoding="utf-8")
    completed_path.write_text(json.dumps(completed), encoding="utf-8")
    if target_kind == "plan":
        linked_parent = tmp_path / "plan-linked"
        linked_parent.symlink_to(plan_real_parent, target_is_directory=True)
        plan_path = linked_parent / "plan.json"
    else:
        linked_parent = tmp_path / "completed-linked"
        linked_parent.symlink_to(completed_real_parent, target_is_directory=True)
        completed_path = linked_parent / "completed.json"

    binding, failure = migration_module.predecessor_authority_migration_binding(
        predecessor_plan,
        {"authority_sha256": "new"},
        {"path": str(plan_path), "sha256": "0" * 64},
        completed,
        completed_path,
        BVID,
        True,
    )

    assert binding == {}
    assert failure is not None and error in failure


def test_descriptor_capture_rejects_in_place_write_race(
    tmp_path: Path,
    monkeypatch,
):
    victim = tmp_path / "authority.json"
    victim.write_bytes(b"original-authority-bytes")
    real_read = migration_module.os.read
    changed = False

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, count)
        if not changed:
            changed = True
            victim.write_bytes(b"changed")
        return chunk

    monkeypatch.setattr(migration_module.os, "read", racing_read)
    with pytest.raises(
        RecoveryPublicationAuthorityMigrationError,
        match="CHANGED",
    ):
        migration_module._capture_anchored_file(
            tmp_path,
            Path("authority.json"),
            missing_code="MISSING",
            unsafe_code="UNSAFE",
            changed_code="CHANGED",
        )
