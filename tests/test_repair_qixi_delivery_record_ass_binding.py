import ast
import hashlib
import json
from pathlib import Path

import pytest

import scripts.repair_qixi_delivery_record_ass_binding as repair

_REAL_VALIDATE_PREDECESSOR = repair._validate_predecessor


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _desc(path: Path) -> dict[str, object]:
    return {"path": str(path), "sha256": _sha(path), "bytes": path.stat().st_size}


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(repair, "PACKAGE_ROOT", tmp_path)
    subtitle = _write(tmp_path / "clip.srt", b"srt")
    main = _write(tmp_path / "clip.mp4", b"main")
    burned = _write(tmp_path / "clip.burned.mp4", b"burned")
    ass = _write(tmp_path / "clip.ass", b"final ass")
    cover = _write(tmp_path / "cover.png", b"cover")
    publish = _write(tmp_path / "clip.publish.json", b"publish")
    z2 = {"status": "PREPENDED", "intro_id": "z2", "intro_media_sha256": "sha256:" + "a" * 64, "intro_offset_ms": 6183}
    correction = _write(tmp_path / "correction.json", repair._json_bytes({
        "schema_version": "human-subtitle-correction.v2", "candidate_id": "auto_113022_354_496",
        "after_srt_sha256": _sha(subtitle).removeprefix("sha256:"),
        "burned_media_sha256": _sha(burned).removeprefix("sha256:"), "upload_enabled": False,
        "delivery_branding_authority": {"branding_intro": z2, "authority_sha256": "sha256:" + "b" * 64,
            "authority_repository_seal": {"mode": "DEPLOYED_MANIFEST", "deployed_commit": "c" * 40,
                "relative_path": "assets/x.json", "sha256": "sha256:" + "b" * 64}},
    }))
    hashes = {key: "sha256:" + "0" * 64 for key in {
        "ai_background_sha256", "ass_sha256", "burned_video_sha256", "chat_authority_audit_sha256",
        "clip_context_file_sha256", "cover_reference_sha256", "cover_sha256", "publish_draft_sha256",
        "redelivery_baseline_audit_sha256", "subtitle_sha256", "video_sha256",
    }}
    hashes.update({"subtitle_sha256": _sha(subtitle), "video_sha256": _sha(main), "burned_video_sha256": _sha(burned), "cover_sha256": _sha(cover), "publish_draft_sha256": _sha(publish)})
    boundary, story = {"status": "PASS"}, {"schema": "story"}
    staging = {"title": "title", "cover_path": str(cover), "cover_status": "AI_COVER_READY", "publish_json_path": str(publish), "story_contract": story}
    record = {"artifact_hashes": hashes, "subtitle_ass_path": None, "subtitle_style": "lidousha-final-sapphire72", "speaker_mode": "uniform_host", "human_text_correction_manifest_path": str(correction), "human_text_correction_manifest_sha256": _sha(correction), "subtitle_path": str(subtitle), "media_path": str(main), "burned_preview": {"path": str(burned)}, "boundary_audit": boundary, "publish_staging": staging}
    primary = _write(tmp_path / "primary.record.json", repair._json_bytes(record))
    mirror = _write(tmp_path / "mirror.record.json", primary.read_bytes())
    authority = {
        "schema_version": repair.SCHEMA, "candidate_id": "auto_113022_354_496", "package_root": str(tmp_path),
        "receipt_path": str(tmp_path / "receipt.json"),
        "predecessor_v2_evidence": {"relative_path": "assets/old.json", "authority_sha256": "sha256:" + "d" * 64,
            "successor_correction_manifest_sha256": "sha256:" + "e" * 64, "successor_record_sha256": "sha256:" + "f" * 64, "successor_burned_video_sha256": "sha256:" + "1" * 64},
        "primary_record": _desc(primary), "mirror_record": _desc(mirror),
        "artifacts": {"subtitle": _desc(subtitle), "main": _desc(main), "burned": _desc(burned), "ass": _desc(ass), "correction": _desc(correction), "cover": _desc(cover), "publish": _desc(publish)},
        "record_invariants": {"subtitle_ass_path": None, "subtitle_style": record["subtitle_style"], "speaker_mode": record["speaker_mode"], "human_text_correction_manifest_path": str(correction), "human_text_correction_manifest_sha256": _sha(correction), "artifact_hashes": hashes, "burned_preview_sha256": repair._canonical_sha(record["burned_preview"]), "boundary_audit_sha256": repair._canonical_sha(boundary), "publish_staging_sha256": repair._canonical_sha(staging), "story_contract_sha256": repair._canonical_sha(story), "title": "title", "cover_path": str(cover), "cover_status": "AI_COVER_READY"},
        "correction_invariants": {"schema_version": "human-subtitle-correction.v2", "candidate_id": "auto_113022_354_496", "after_srt_sha256": _sha(subtitle), "burned_media_sha256": _sha(burned), "upload_enabled": False, "branding_intro": z2, "authority_sha256": "sha256:" + "b" * 64, "authority_repository_seal": {"mode": "DEPLOYED_MANIFEST", "deployed_commit": "c" * 40, "relative_path": "assets/x.json", "sha256": "sha256:" + "b" * 64}},
    }
    state = {"raw": repair._json_bytes(authority), "seal": ("DEPLOYED_MANIFEST", "a" * 40, str(repair.ASSET_RELATIVE), "sha256:" + "9" * 64)}

    def load():
        return state["raw"], json.loads(state["raw"]), state["seal"]

    monkeypatch.setattr(repair, "_load_authority", load)
    monkeypatch.setattr(repair, "_validate_predecessor", lambda _value: None)
    return {"authority": authority, "state": state, "primary": primary, "mirror": mirror, "ass": ass, "receipt": tmp_path / "receipt.json", "correction": correction}


def test_applies_only_the_sealed_ass_pointers_and_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fixture(tmp_path, monkeypatch)
    plan = repair.recover(apply=False)
    assert plan["mode"] == "DRY_RUN" and not data["receipt"].exists()
    repair.recover(apply=True)
    assert data["primary"].read_bytes() == data["mirror"].read_bytes()
    updated = json.loads(data["primary"].read_text())
    assert updated["subtitle_ass_path"] == str(data["ass"])
    assert updated["artifact_hashes"]["ass_sha256"] == _sha(data["ass"])
    receipt = json.loads(data["receipt"].read_text())
    assert receipt["allowed_json_pointers"] == ["/artifact_hashes/ass_sha256", "/subtitle_ass_path"]
    assert receipt["authority"]["relative_path"] == str(repair.ASSET_RELATIVE)
    assert set(receipt["evidence_descriptors"]) == {"primary_record", "mirror_record", "subtitle", "main", "burned", "ass", "correction", "cover", "publish"}


@pytest.mark.parametrize("drift", ["mirror", "artifact", "receipt", "nested-key", "receipt-symlink"])
def test_preflight_refuses_drift_without_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str) -> None:
    data = _fixture(tmp_path, monkeypatch)
    if drift == "mirror":
        data["mirror"].write_text("{}")
    elif drift == "artifact":
        data["ass"].write_text("drift")
    elif drift == "receipt":
        data["receipt"].write_text("collision")
    elif drift == "receipt-symlink":
        data["receipt"].symlink_to(data["primary"])
    else:
        data["authority"]["record_invariants"].pop("title")
        data["state"]["raw"] = repair._json_bytes(data["authority"])
    before = {path: path.read_bytes() for path in (data["primary"], data["mirror"])}
    with pytest.raises(repair.AssBindingRecoveryError):
        repair.recover(apply=True)
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("nested", ["predecessor", "correction", "descriptor"])
def test_rejects_extra_nested_authority_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: str
) -> None:
    data = _fixture(tmp_path, monkeypatch)
    if nested == "predecessor":
        data["authority"]["predecessor_v2_evidence"]["extra"] = True
        with pytest.raises(repair.AssBindingRecoveryError):
            _REAL_VALIDATE_PREDECESSOR(data["authority"]["predecessor_v2_evidence"])
        return
    elif nested == "correction":
        data["authority"]["correction_invariants"]["extra"] = True
    else:
        data["authority"]["artifacts"]["ass"]["extra"] = True
    data["state"]["raw"] = repair._json_bytes(data["authority"])
    with pytest.raises(repair.AssBindingRecoveryError):
        repair.recover(apply=False)


def test_rejects_symlink_in_a_fixed_package_ancestor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fixture(tmp_path, monkeypatch)
    nested = tmp_path / "nested"
    nested.mkdir()
    nested_ass = nested / "clip.ass"
    nested_ass.write_bytes(data["ass"].read_bytes())
    data["authority"]["artifacts"]["ass"] = _desc(nested_ass)
    nested_ass.unlink()
    nested.rmdir()
    nested.symlink_to(tmp_path, target_is_directory=True)
    data["state"]["raw"] = repair._json_bytes(data["authority"])
    with pytest.raises(repair.AssBindingRecoveryError, match="ancestor"):
        repair.recover(apply=False)


def test_revalidates_authority_epoch_and_bytes_before_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fixture(tmp_path, monkeypatch)
    context = repair._prepare()
    data["state"]["seal"] = ("DEPLOYED_MANIFEST", "b" * 40, str(repair.ASSET_RELATIVE), "sha256:" + "9" * 64)
    with pytest.raises(repair.AssBindingRecoveryError, match="authority, epoch"):
        repair._commit(context)
    assert not data["receipt"].exists()


def test_revalidates_authority_bytes_before_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fixture(tmp_path, monkeypatch)
    context = repair._prepare()
    data["state"]["raw"] = b"\n" + data["state"]["raw"]
    with pytest.raises(repair.AssBindingRecoveryError, match="authority, epoch"):
        repair._commit(context)
    assert not data["receipt"].exists()


@pytest.mark.parametrize("drift", ["artifact", "primary"])
def test_revalidates_artifact_and_target_preimage_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    data = _fixture(tmp_path, monkeypatch)
    context = repair._prepare()
    if drift == "artifact":
        data["ass"].write_text("different final ASS")
    else:
        data["primary"].write_text("different record preimage")
    before = {data["primary"]: data["primary"].read_bytes(), data["mirror"]: data["mirror"].read_bytes()}
    with pytest.raises(repair.AssBindingRecoveryError):
        repair._commit(context)
    assert {path: path.read_bytes() for path in before} == before


def test_revalidates_again_after_private_staging_before_any_target_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _fixture(tmp_path, monkeypatch)
    before = {data["primary"]: data["primary"].read_bytes(), data["mirror"]: data["mirror"].read_bytes()}
    real_write = repair._write_private

    def drift_after_staging(path: Path, payload: bytes) -> tuple[int, int]:
        owner = real_write(path, payload)
        if path.name == "install-2":
            data["ass"].write_bytes(b"ASS changed during private staging")
        return owner

    monkeypatch.setattr(repair, "_write_private", drift_after_staging)
    with pytest.raises(repair.AssBindingRecoveryError, match="commit failed; restored preimages"):
        repair.recover(apply=True)
    assert {path: path.read_bytes() for path in before} == before
    assert not data["receipt"].exists()
    assert not list(tmp_path.glob(".qixi-ass-binding-*"))


def test_rejects_target_symlink_between_plan_and_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fixture(tmp_path, monkeypatch)
    context = repair._prepare()
    original = data["primary"].read_bytes()
    data["primary"].unlink()
    data["primary"].symlink_to(data["mirror"])
    with pytest.raises(repair.AssBindingRecoveryError):
        repair._commit(context)
    assert data["mirror"].read_bytes() == original
    assert not data["receipt"].exists()


@pytest.mark.parametrize("failed_install", [1, 2])
def test_second_or_third_install_failure_restores_all_preimages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_install: int) -> None:
    data = _fixture(tmp_path, monkeypatch)
    before = {data["primary"]: data["primary"].read_bytes(), data["mirror"]: data["mirror"].read_bytes()}
    real_replace = repair.os.replace

    def fail_install(source: str | Path, target: str | Path) -> None:
        if Path(source).name == f"install-{failed_install}":
            raise OSError("injected install failure")
        real_replace(source, target)

    monkeypatch.setattr(repair.os, "replace", fail_install)
    with pytest.raises(repair.AssBindingRecoveryError, match="restored preimages"):
        repair.recover(apply=True)
    assert {path: path.read_bytes() for path in before} == before
    assert not data["receipt"].exists()
    assert not list(tmp_path.glob(".qixi-ass-binding-*"))


def test_rollback_never_unlinks_a_foreign_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fixture(tmp_path, monkeypatch)
    real_replace = repair.os.replace

    def lose_primary_ownership(source: str | Path, target: str | Path) -> None:
        real_replace(source, target)
        if Path(source).name == "install-0":
            data["primary"].write_bytes(b"foreign replacement")

    monkeypatch.setattr(repair.os, "replace", lose_primary_ownership)
    with pytest.raises(repair.AssBindingRecoveryError, match="foreign replacement preserved"):
        repair.recover(apply=True)
    assert data["primary"].read_bytes() == b"foreign replacement"
    assert list(tmp_path.glob(".qixi-ass-binding-*"))


@pytest.mark.parametrize("target_kind", ["record-regular", "record-broken-symlink", "receipt-regular", "receipt-broken-symlink"])
def test_rollback_preserves_regular_and_broken_symlink_foreign_replacements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str
) -> None:
    data = _fixture(tmp_path, monkeypatch)
    before = {data["primary"]: data["primary"].read_bytes(), data["mirror"]: data["mirror"].read_bytes()}
    real_replace = repair.os.replace
    foreign = tmp_path / "foreign-missing"

    def replace_with_foreign(source: str | Path, target: str | Path) -> None:
        real_replace(source, target)
        source_name = Path(source).name
        wants_record = target_kind.startswith("record") and source_name == "install-0"
        wants_receipt = target_kind.startswith("receipt") and source_name == "install-2"
        if not (wants_record or wants_receipt):
            return
        changed = data["primary"] if wants_record else data["receipt"]
        changed.unlink()
        if target_kind.endswith("broken-symlink"):
            changed.symlink_to(foreign)
        else:
            changed.write_bytes(b"foreign replacement")

    monkeypatch.setattr(repair.os, "replace", replace_with_foreign)
    with pytest.raises(repair.AssBindingRecoveryError, match="foreign replacement preserved"):
        repair.recover(apply=True)
    changed = data["primary"] if target_kind.startswith("record") else data["receipt"]
    if target_kind.endswith("broken-symlink"):
        assert changed.is_symlink() and not changed.exists()
    else:
        assert changed.read_bytes() == b"foreign replacement"
    if target_kind.startswith("receipt"):
        assert {path: path.read_bytes() for path in before} == before
    else:
        assert data["mirror"].read_bytes() == before[data["mirror"]]
    assert list(tmp_path.glob(".qixi-ass-binding-*"))


def test_dry_run_creates_no_transaction_residue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _fixture(tmp_path, monkeypatch)
    before = sorted(path.name for path in tmp_path.iterdir())
    repair.recover(apply=False)
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert not data["receipt"].exists()


def test_private_backup_names_do_not_collide_with_package_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixture(tmp_path, monkeypatch)
    collision = _write(tmp_path / ".receipt.json.ass-binding-backup", b"unowned residue")
    repair.recover(apply=True)
    assert collision.read_bytes() == b"unowned residue"


@pytest.mark.parametrize("failed_backup", ["backup-0", "backup-1"])
def test_cleanup_backup_failure_keeps_applied_targets_and_explicit_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_backup: str
) -> None:
    data = _fixture(tmp_path, monkeypatch)
    real_unlink = repair.os.unlink

    def fail_backup_unlink(path: str | Path, *args, **kwargs) -> None:
        if Path(path).name == failed_backup:
            raise OSError("injected backup cleanup failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(repair.os, "unlink", fail_backup_unlink)
    result = repair.recover(apply=True)
    assert result["mode"] == "APPLIED_WITH_CLEANUP_RESIDUE"
    assert data["primary"].read_bytes() == data["mirror"].read_bytes()
    assert data["receipt"].is_file()
    assert list(tmp_path.glob(".qixi-ass-binding-*"))
    with pytest.raises(repair.AssBindingRecoveryError):
        repair.recover(apply=True)


def test_cleanup_stage_rmdir_failure_keeps_applied_targets_and_explicit_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _fixture(tmp_path, monkeypatch)
    real_rmdir = repair.Path.rmdir

    def fail_stage_rmdir(path: Path) -> None:
        if path.name.startswith(".qixi-ass-binding-"):
            raise OSError("injected stage cleanup failure")
        real_rmdir(path)

    monkeypatch.setattr(repair.Path, "rmdir", fail_stage_rmdir)
    result = repair.recover(apply=True)
    assert result["mode"] == "APPLIED_WITH_CLEANUP_RESIDUE"
    assert data["primary"].read_bytes() == data["mirror"].read_bytes()
    assert data["receipt"].is_file()
    assert list(tmp_path.glob(".qixi-ass-binding-*"))


def test_postcheck_foreign_drift_preserves_backup_and_never_deletes_foreign_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _fixture(tmp_path, monkeypatch)
    real_postcheck = repair._postcommit_snapshot
    calls = 0

    def drift_after_global_postcheck(entry: dict[str, object], expected: bytes) -> None:
        nonlocal calls
        real_postcheck(entry, expected)
        calls += 1
        if calls == 3:
            data["primary"].write_bytes(b"foreign after commit point")

    monkeypatch.setattr(repair, "_postcommit_snapshot", drift_after_global_postcheck)
    result = repair.recover(apply=True)
    assert result["mode"] == "APPLIED_WITH_CLEANUP_RESIDUE"
    assert data["primary"].read_bytes() == b"foreign after commit point"
    stage = next(tmp_path.glob(".qixi-ass-binding-*"))
    assert (stage / "backup-0").is_file()
    assert data["receipt"].is_file()


def test_tool_has_no_media_pipeline_import_or_call() -> None:
    source = Path(repair.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names}
    assert not any("apply_subtitle_correction" in name or "producer" in name for name in imports)
    assert "ffmpeg" not in source.lower()
