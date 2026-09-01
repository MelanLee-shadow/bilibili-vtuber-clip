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
        "operator_text_full_ownership": {"speaker_authority": "REVIEWER_LINE947_EXHAUSTIVE"},
    }))
    aux = tmp_path / "aux"
    aux.mkdir(mode=0o700)
    for name, _kind, _size, _digest, _pointer in auxiliary.values():
        data = {"cover-ref.png": b"ref", "speaker.json": b"speaker"}[name]
        (aux / name).write_bytes(data)
        (aux / name).chmod(0o644)
    return v2, aux, repo


def test_builder_rejects_unapproved_fixture_even_when_self_sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    v2, aux, repo = _fixture(tmp_path, monkeypatch)
    with pytest.raises(c3.C3V3ClosureError, match="APPROVED_MANIFEST_DRIFT"):
        c3.build_c3_v3_closure(v2_root=v2, auxiliary_root=aux, destination=tmp_path / "v3", repo_root=repo)


def test_regular_reader_rejects_symlink_and_hardlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"x")
    source.chmod(0o600)
    hardlink = tmp_path / "hardlink"
    hardlink.hardlink_to(source)
    with pytest.raises(c3.C3V3ClosureError, match="NOT_REGULAR"):
        c3._read_regular(hardlink, label="TEST")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(source)
    with pytest.raises(c3.C3V3ClosureError, match="UNAVAILABLE"):
        c3._read_regular(symlink, label="TEST")


def test_loader_rejects_prior_arbitrary_self_sealed_v3() -> None:
    root = Path("/private/tmp/c3-v3-cli-prototype-2-20260826")
    if not root.is_dir():
        pytest.skip("local C3 prototype artifact is unavailable")
    with pytest.raises(c3.C3V3ClosureError, match="APPROVED_MANIFEST_DRIFT"):
        c3.load_c3_v3_authority(root=root)


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
    with pytest.raises(c3.C3V3ClosureError, match="APPROVED_MANIFEST_DRIFT"):
        c3.build_c3_v3_closure(v2_root=v2, auxiliary_root=aux, destination=destination, repo_root=repo)
    assert not destination.exists()


def test_direct_pass0_rejects_canonical_audit_block_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    v2, aux, repo = _fixture(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime"
    destination = runtime / c3.V3_RELATIVE_ROOT
    destination.parent.mkdir(parents=True, mode=0o700)
    with pytest.raises(c3.C3V3ClosureError, match="APPROVED_MANIFEST_DRIFT"):
        c3.build_c3_v3_closure(v2_root=v2, auxiliary_root=aux, destination=destination, repo_root=repo)
    assert not destination.exists()
