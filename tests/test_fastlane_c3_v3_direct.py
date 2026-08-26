from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import src.autoslice.fastlane_c3_v3_direct as c3


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    v2_roles = {
        "plain_srt": ("plain.srt", 5, _sha(b"plain")),
        "record": ("record.json", 6, _sha(b"record")),
    }
    auxiliary = {
        "cover_reference": ("cover-ref.png", "cover_reference", 3, _sha(b"ref"), "record.ref"),
        "speaker_override": ("speaker.json", "speaker_override", 7, _sha(b"speaker"), "record.speaker"),
    }
    monkeypatch.setattr(c3, "V2_ROLES", v2_roles)
    monkeypatch.setattr(c3, "AUXILIARY_ROLES", auxiliary)
    v2 = tmp_path / "v2"
    v2.mkdir(mode=0o700)
    for name, _size, digest in v2_roles.values():
        data = {"plain.srt": b"plain", "record.json": b"record"}[name]
        (v2 / name).write_bytes(data)
        (v2 / name).chmod(0o600)
    roles = {
        role: {"locator": name, "mode": "0600", "size": size, "sha256": digest}
        for role, (name, size, digest) in v2_roles.items()
    }
    unsigned = {
        "schema_version": c3.V2_SCHEMA,
        "candidate_id": c3.CANDIDATE_ID,
        "recording_date": c3.RECORDING_DATE,
        "authority_scope": "private_exact_accepted_byte_carry_only",
        "forbidden_operations": list(c3._FORBIDDEN),
        "roles": roles,
    }
    manifest = dict(unsigned)
    manifest["manifest_sha256"] = _sha(c3._canonical(unsigned))
    (v2 / "manifest.json").write_bytes(c3._canonical(manifest))
    (v2 / "manifest.json").chmod(0o600)
    repo = tmp_path / "repo"
    baseline_root = repo / "assets/lidousha/reviewed_subtitle_baselines"
    baseline_root.mkdir(parents=True, mode=0o700)
    (baseline_root / f"{c3.CANDIDATE_ID}.subtitle-baseline.v1.json").write_text(json.dumps({
        "candidate_id": c3.CANDIDATE_ID,
        "schema_version": "subtitle-redelivery-baseline.v2",
        "exact_interval_replay": True,
        "sha256": "d70a96c402df8313264ef6ca145d69d5dbb74e7a2c48eb512e78f3f417e8eecc",
        "operator_text_full_ownership": {"speaker_authority": "IVAN_LINE947_EXHAUSTIVE"},
    }))
    aux = tmp_path / "aux"
    aux.mkdir(mode=0o700)
    for name, _kind, _size, _digest, _pointer in auxiliary.values():
        data = {"cover-ref.png": b"ref", "speaker.json": b"speaker"}[name]
        (aux / name).write_bytes(data)
        (aux / name).chmod(0o644)
    return v2, aux, repo


def test_builder_and_loader_seal_exact_role_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v2, aux, repo = _fixture(tmp_path, monkeypatch)
    authority = c3.build_c3_v3_closure(
        v2_root=v2, auxiliary_root=aux,
        destination=tmp_path / "v3", repo_root=repo,
    )
    assert len(authority.roles) == 4
    assert authority.manifest_raw_sha256.startswith("sha256:")
    assert authority.manifest_self_seal.startswith("sha256:")
    assert authority.root_tree_sha256.startswith("sha256:")
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in authority.roles.values())

    (authority.root / "unexpected").write_bytes(b"x")
    with pytest.raises(c3.C3V3ClosureError, match="EXTRA_OR_MISSING_FILE"):
        c3.load_c3_v3_authority(root=authority.root)


def test_builder_refuses_existing_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v2, aux, repo = _fixture(tmp_path, monkeypatch)
    destination = tmp_path / "v3"
    destination.mkdir(mode=0o700)
    with pytest.raises(c3.C3V3ClosureError, match="DESTINATION_EXISTS"):
        c3.build_c3_v3_closure(v2_root=v2, auxiliary_root=aux, destination=destination, repo_root=repo)


def test_direct_pass0_is_provider_free_and_cleans_private_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    v2, aux, repo = _fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime"
    destination = runtime / c3.V3_RELATIVE_ROOT
    destination.parent.mkdir(parents=True, mode=0o700)
    c3.build_c3_v3_closure(v2_root=v2, auxiliary_root=aux, destination=destination, repo_root=repo)
    stage_parent = tmp_path / "stage-parent"
    stage_parent.mkdir(mode=0o700)
    calls = 0

    def audit(_package: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"passed": True, "issue_count": 0, "blocking_issue_count": 0}

    result = c3.consume_c3_successor_pass0(
        runtime_root=runtime, private_stage_parent=stage_parent, audit=audit,
    )
    assert result.upload_allowed is False
    assert result.provider_attempted is False
    assert calls == 1
    assert not list(stage_parent.iterdir())


def test_direct_pass0_rejects_canonical_audit_block_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    v2, aux, repo = _fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime"
    destination = runtime / c3.V3_RELATIVE_ROOT
    destination.parent.mkdir(parents=True, mode=0o700)
    c3.build_c3_v3_closure(v2_root=v2, auxiliary_root=aux, destination=destination, repo_root=repo)
    stage_parent = tmp_path / "stage-parent"
    stage_parent.mkdir(mode=0o700)
    with pytest.raises(c3.C3V3ClosureError, match="CANONICAL_AUDIT_BLOCKED"):
        c3.consume_c3_successor_pass0(
            runtime_root=runtime, private_stage_parent=stage_parent,
            audit=lambda _path: {"passed": False, "issue_count": 1, "blocking_issue_count": 1},
        )
    assert not list(stage_parent.iterdir())
