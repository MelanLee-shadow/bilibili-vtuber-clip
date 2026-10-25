from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from src.autoslice import overflow_bundle as overflow_bundle_module
from src.autoslice.overflow_bundle import (
    OverflowBundleError,
    export_bundle,
    import_bundle,
)
from src.autoslice.producer_delivery_transaction import (
    DeliveryArtifact,
    ProducerDeliveryTransactionError,
    _read_document,
    load_prepared_delivery,
    preflight_prepared_delivery,
    prepare_delivery,
)
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
from src.autoslice.repository_asset_authority import _canonical_sha256


def _seal(root: Path, commit: str = "a" * 40) -> None:
    repo = root / "repo"
    repo.mkdir(exist_ok=True)
    profile_path = repo / "profiles/lidousha/profile.json"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).resolve().parents[1] / "profiles/lidousha/profile.json", profile_path)
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
    manifest = dict(body)
    manifest["manifest_sha256"] = _canonical_sha256(body)
    (repo / "DEPLOYED_COMMIT").write_text(commit + "\n", encoding="utf-8")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _fixture(
    tmp_path: Path, *, status: str = "delivery_prepared_no_target",
    versioned: bool = False, lane: str = "talk", profile_id: str = "lidousha",
    date: str = "2026-10-05", candidate_id: str = "cid-1",
    target_root: str | None = None,
) -> dict[str, object]:
    root = tmp_path / "runtime"
    root.mkdir()
    _seal(root)
    repo = root / "repo"
    if profile_id != "lidousha":
        original = repo / "profiles/lidousha/profile.json"
        value = json.loads(original.read_text())
        value["profile_id"] = profile_id
        value["identity"]["output_directory"] = profile_id
        manifest = repo / "profiles" / profile_id / "profile.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps(value))
    if versioned:
        deployed = root / "versions" / ("a" * 40) / "repo"
        deployed.parent.mkdir(parents=True)
        shutil.copytree(repo, deployed)
        (deployed / "DEPLOYED_MANIFEST.json").write_text("{}")
        (root / "current").mkdir()
        (root / "current/repo").symlink_to(deployed)
        repo = deployed
    from src.autoslice.channel_profile import binding_for_profile, load_channel_profile
    profile = load_channel_profile(repo_root=repo, profile_id=profile_id, environ={})
    profile_binding = binding_for_profile(profile)
    source = root / "out" / date / candidate_id / "video.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"candidate-video")
    evidence = source.parent / "evidence.json"
    media = source.parent / "evidence.pcm"
    media.write_bytes(b"pcm-evidence")
    evidence.write_text(json.dumps({"media_path": str(media)}), encoding="utf-8")
    delivery_root = root / target_root if target_root else profile.delivery_root
    target = delivery_root / date / ("hook__" + candidate_id + ".mp4")
    target.parent.mkdir(parents=True)
    digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    handle = prepare_delivery(
        runtime_root=root,
        lane=lane,
        candidate_id=candidate_id,
        artifacts=[DeliveryArtifact("video", source, target, digest)],
    )
    result: dict[str, object] = {
        "job_id": "job-1",
        "attempt_id": "attempt-1",
        "release_sha256": "sha256:" + "1" * 64,
        "input_sha256": "sha256:" + "2" * 64,
        "candidate_id": candidate_id,
        "status": status,
        "evidence_path": str(evidence),
        "profile_binding": profile_binding,
    }
    if status == "delivery_prepared_no_target":
        result["prepared_delivery"] = {
            "manifest_path": str(handle.manifest_path),
            "prepared_sha256": f"sha256:{handle.prepared_sha256}",
            "upload_enabled": False,
        }
    return {
        "root": root,
        "date": date,
        "candidate_id": candidate_id,
        "source": source,
        "evidence": evidence,
        "media": media,
        "target": target,
        "handle": handle,
        "result": result,
        "profile_binding": profile_binding,
        "lane": lane,
    }


def _export(
    fixture: dict[str, object], bundle: Path, *, source_files: object = None,
) -> Path:
    return export_bundle(
        fixture["result"],
        fixture["root"],
        bundle_dir=bundle,
        job_id="job-1",
        attempt_id="attempt-1",
        release_sha256="sha256:" + "1" * 64,
        input_sha256="sha256:" + "2" * 64,
        source_files=source_files,
        date=fixture["date"],
        lane=fixture["lane"],
        expected_profile_binding=fixture["profile_binding"],
    )


def _import(
    fixture: dict[str, object],
    bundle: Path,
    *,
    job_id: str = "job-1",
    attempt_id: str = "attempt-1",
    release_sha256: str = "sha256:" + "1" * 64,
    input_sha256: str = "sha256:" + "2" * 64,
) -> dict[str, object]:
    return import_bundle(
        bundle,
        fixture["root"],
        job_id,
        attempt_id,
        release_sha256,
        input_sha256,
        expected_profile_binding=fixture["profile_binding"],
    )


@pytest.mark.parametrize('tamper', [False, True])
def test_legacy_failed_prepared_log_uses_verified_attempt_payload_without_overwriting_prior(tmp_path, tamper):
    fixture = _fixture(tmp_path, status='failed')
    log = fixture['source'].parent/'.prepared-resumes'/('a'*64)/'producer.log'
    log.parent.mkdir(parents=True)
    log.write_bytes(b'first attempt\nsecond attempt\n')
    fixture['result']['log'] = str(log)
    manifest = _export(fixture, tmp_path/'bundle')
    raw_manifest = manifest.read_bytes()
    doc = json.loads(raw_manifest)
    row = next(r for r in doc['files'] if r['path'] == log.relative_to(fixture['root']).as_posix())
    payload = manifest.parent/row['payload']
    if tamper:
        payload.write_bytes(b'altered attempt evidence')
    log.write_bytes(b'first attempt\n')
    original = log.read_bytes(), log.stat().st_ino, log.stat().st_mtime_ns
    if tamper:
        with pytest.raises(OverflowBundleError):
            _import(fixture, manifest.parent)
    else:
        imported = _import(fixture, manifest.parent)
        assert imported['status'] == 'failed'
        assert Path(imported['log']) == payload
        assert Path(imported['log']).read_bytes() == b'first attempt\nsecond attempt\n'
        assert imported['imported_producer_log']['original_locator'] == str(log)
        assert imported['imported_producer_log']['sha256'] == row['sha256']
        assert _import(fixture, manifest.parent) == imported
    assert (log.read_bytes(), log.stat().st_ino, log.stat().st_mtime_ns) == original
    assert manifest.read_bytes() == raw_manifest


@pytest.mark.parametrize("damage", [None, "wrong_digest", "sibling", "parent", "spec"])
def test_ordinary_canonical_resume_stays_in_cid_scope_through_export_import(tmp_path, damage):
    from src.autoslice.producer_prepared_recovery import (
        canonical_prepared_resume_output_root, validate_resume_options,
    )
    from src.autoslice.producer_request import parse_producer_args
    fixture = _fixture(tmp_path)
    root, cid, date, old = fixture["root"], fixture["candidate_id"], fixture["date"], fixture["handle"]
    spec = root / "out" / date / f"spec_{cid}.json"
    spec.write_text(json.dumps({"candidate_id": cid, "date": date,
                                "output_root": str(root / "out" / date)}))
    original = spec.read_bytes(), fixture["source"].read_bytes(), old.manifest_path.read_bytes()
    sha = hashlib.sha256(spec.read_bytes()).hexdigest()
    successor = canonical_prepared_resume_output_root(spec, sha, old.manifest_path)
    assert successor.is_relative_to(root / "out" / date / cid / ".prepared-resumes")
    output = successor
    if damage == "wrong_digest":
        output = successor.with_name("f" * 64)
    elif damage == "sibling":
        output = root / "out" / date / "other-cid" / ".prepared-resumes" / successor.name
    elif damage == "parent":
        output = root / "out" / date
    elif damage == "spec":
        sha = "f" * 64
    args = parse_producer_args([
        "--spec", str(spec), "--resume-prepared", str(old.manifest_path),
        "--resume-spec-sha256", sha, "--resume-output-root", str(output),
        "--prepare-only", "--correct", "cpa", "--speaker-mode", "uniform_host",
    ], description="", speaker_display_name="host", default_speaker_mode="uniform_host")
    if damage:
        with pytest.raises(ValueError, match="PREPARED_RESUME_"):
            validate_resume_options(args)
        return
    validate_resume_options(args)
    source = successor / cid / "replacement_recuts" / "video.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(fixture["source"].read_bytes())
    # Use the actual normal producer's prepare primitive to mint this new
    # source member, then the unmodified shared export/import consumers.
    fresh = prepare_delivery(
        runtime_root=root, lane="talk", candidate_id=cid,
        artifacts=[DeliveryArtifact("video", source, fixture["target"],
                                    "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest())])
    fixture["result"]["prepared_delivery"] = {
        "manifest_path": str(fresh.manifest_path), "prepared_sha256": "sha256:" + fresh.prepared_sha256,
        "upload_enabled": False,
    }
    bundle = tmp_path / "ordinary-resume-bundle"
    _export(fixture, bundle)
    imported = _import(fixture, bundle)
    assert imported["status"] == "delivery_prepared_no_target"
    controller = load_prepared_delivery(runtime_root=root,
                                       manifest_path=Path(imported["prepared_delivery"]["manifest_path"]))
    with exclusive_runner_commit(root) as lease:
        preflight_prepared_delivery(handle=controller, lease=lease)
    assert not fixture["target"].exists()
    assert (spec.read_bytes(), fixture["source"].read_bytes(), old.manifest_path.read_bytes()) == original


def test_import_reprepares_after_physical_prepared_manifest_is_unavailable(tmp_path: Path):
    fixture = _fixture(tmp_path)
    old_handle = fixture["handle"]
    bundle = tmp_path / "bundle"
    _export(fixture, bundle)

    # A physical copy of the worker's private directory preserves the
    # manifest's old device/inode observations, but its staged inode is new.
    # The normal commit preflight rejects that copied handle; the importer
    # must validate the bytes from the bundle and fresh-prepare them.
    old_manifest = old_handle.manifest_path
    stale_copy = tmp_path / "stale-private-copy"
    shutil.copytree(old_manifest.parent, stale_copy)
    shutil.rmtree(old_manifest.parent)
    shutil.copytree(stale_copy, old_manifest.parent)
    replacement = fixture["source"].with_name(".video.replacement")
    replacement.write_bytes(fixture["source"].read_bytes())
    replacement.chmod(fixture["source"].stat().st_mode & 0o777)
    old_source_inode = fixture["source"].stat().st_ino
    replacement.replace(fixture["source"])
    assert fixture["source"].stat().st_ino != old_source_inode
    copied_handle = load_prepared_delivery(runtime_root=fixture["root"], manifest_path=old_manifest)
    with exclusive_runner_commit(fixture["root"]) as lease:
        with pytest.raises(ProducerDeliveryTransactionError):
            preflight_prepared_delivery(handle=copied_handle, lease=lease)

    imported = _import(fixture, bundle)
    new_ref = imported["prepared_delivery"]
    assert new_ref["manifest_path"] != str(old_manifest)
    new_handle = load_prepared_delivery(
        runtime_root=fixture["root"], manifest_path=Path(new_ref["manifest_path"])
    )
    entry = _read_document(new_handle)["artifacts"][0]
    assert Path(entry["target_path"]) == fixture["target"]
    assert not fixture["target"].exists()
    assert fixture["source"].read_bytes() == b"candidate-video"


def test_bundle_dependency_closure_keeps_referenced_binary_and_omits_unreferenced_pcm(tmp_path: Path):
    fixture = _fixture(tmp_path, status="rejected")
    unrelated = fixture["source"].parent / "unreferenced.pcm"
    unrelated.write_bytes(b"large-but-unreferenced")
    manifest = _export(fixture, tmp_path / "bundle")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in document["files"]}
    assert "out/2026-10-05/cid-1/evidence.json" in paths
    assert "out/2026-10-05/cid-1/evidence.pcm" in paths
    assert "out/2026-10-05/cid-1/unreferenced.pcm" not in paths
    assert _import(fixture, manifest.parent)["status"] == "rejected"


def test_hash_corruption_is_rejected_before_runtime_write(tmp_path: Path):
    fixture = _fixture(tmp_path)
    bundle = tmp_path / "bundle"
    _export(fixture, bundle)
    payload = bundle / "payload" / "out" / fixture["date"] / fixture["candidate_id"] / "video.mp4"
    payload.write_bytes(b"corrupt")
    with pytest.raises(OverflowBundleError, match="(hash drifts|byte count drifts)"):
        _import(fixture, bundle)
    assert not fixture["target"].exists()


def test_symlink_payload_and_stale_identity_are_rejected(tmp_path: Path):
    fixture = _fixture(tmp_path)
    bundle = tmp_path / "bundle"
    _export(fixture, bundle)
    payload = bundle / "payload" / "out" / fixture["date"] / fixture["candidate_id"] / "video.mp4"
    payload.unlink()
    payload.symlink_to(fixture["source"])
    with pytest.raises(OverflowBundleError, match="(symlink|regular file)"):
        _import(fixture, bundle)

    # Use a clean export for the identity check so the symlink does not mask
    # the expected stale-attempt diagnostic.
    clean_parent = tmp_path / "clean"
    clean_parent.mkdir()
    clean = _fixture(clean_parent)
    _export(clean, tmp_path / "clean-bundle")
    with pytest.raises(OverflowBundleError, match="attempt"):
        _import(clean, tmp_path / "clean-bundle", attempt_id="attempt-stale")


def test_result_path_escape_is_rejected_during_export(tmp_path: Path):
    fixture = _fixture(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    fixture["result"]["evidence_path"] = str(outside)
    with pytest.raises(OverflowBundleError, match="escapes runtime_root"):
        _export(fixture, tmp_path / "bundle")


def test_declared_external_source_is_provenance_and_result_bytes_stay_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    fixture = _fixture(tmp_path)
    external_root = tmp_path / "external-source-root"
    monkeypatch.setattr(overflow_bundle_module, "_EXTERNAL_SOURCE_ROOTS", (external_root,))
    external = external_root / "recording.mp4"
    external.parent.mkdir()
    external.write_bytes(b"original-recording")
    fixture["result"]["segment_path"] = str(external)
    source_files = [{
        "path": str(external),
        "bytes": external.stat().st_size,
        "sha256": hashlib.sha256(external.read_bytes()).hexdigest(),
    }]

    manifest = _export(fixture, tmp_path / "bundle", source_files=source_files)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["source_files"] == [{
        "path": str(external),
        "bytes": external.stat().st_size,
        "sha256": "sha256:" + hashlib.sha256(external.read_bytes()).hexdigest(),
    }]
    assert all(not entry["path"].endswith("recording.mp4") for entry in document["files"])

    imported = _import(fixture, manifest.parent)
    assert imported["segment_path"] == str(external)


def _attach_failure_surface_evidence(fixture: dict[str, object]) -> dict[str, Path]:
    candidate = fixture["source"].parent
    review = candidate / "talk_cid-1.review-flags.json"
    chat = candidate / "talk_cid-1.chat-authority.json"
    nested = candidate / "talk_cid-1.chat-detail.json"
    review.write_bytes(b'{"status":"failed"}\n')
    nested.write_bytes(b'{"detail":"preserved"}\n')
    chat.write_text(json.dumps({"detail_path": str(nested)}) + "\n", encoding="utf-8")
    fixture["result"]["failure_evidence"] = {
        "artifact_file": chat.name,
        "surface_files": [
            {
                "file": review.name,
                "role": "review_flags",
                "sha256": "sha256:" + hashlib.sha256(review.read_bytes()).hexdigest(),
            },
            {
                "file": chat.name,
                "role": "chat_authority",
                "sha256": "sha256:" + hashlib.sha256(chat.read_bytes()).hexdigest(),
            },
        ],
    }
    return {"review": review, "chat": chat, "nested": nested}


def test_failed_relative_surface_evidence_roundtrips_with_json_closure(tmp_path: Path):
    fixture = _fixture(tmp_path, status="rejected")
    surfaces = _attach_failure_surface_evidence(fixture)
    manifest = _export(fixture, tmp_path / "bundle")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in document["files"]}
    assert f"out/{fixture['date']}/{fixture['candidate_id']}/{surfaces['review'].name}" in paths
    assert f"out/{fixture['date']}/{fixture['candidate_id']}/{surfaces['chat'].name}" in paths
    assert f"out/{fixture['date']}/{fixture['candidate_id']}/{surfaces['nested'].name}" in paths

    shutil.rmtree(fixture["source"].parent)
    imported = _import(fixture, manifest.parent)
    assert imported == fixture["result"]
    for path in surfaces.values():
        assert path.is_file()
    assert surfaces["chat"].read_bytes() == json.dumps(
        {"detail_path": str(surfaces["nested"])}, ensure_ascii=False
    ).encode() + b"\n"


@pytest.mark.parametrize("mutation", ["hash", "path"])
def test_failed_surface_hash_or_path_drift_is_rejected(
    tmp_path: Path, mutation: str,
):
    fixture = _fixture(tmp_path, status="rejected")
    surfaces = _attach_failure_surface_evidence(fixture)
    if mutation == "hash":
        surfaces["chat"].write_bytes(b"drifted-chat-authority")
    else:
        fixture["result"]["failure_evidence"]["surface_files"][0]["file"] = "../escape.json"
    with pytest.raises(OverflowBundleError, match="(failure evidence|unsafe|escapes candidate)"):
        _export(fixture, tmp_path / "bundle")


def test_failed_missing_native_audit_remains_missing_and_original_result_is_preserved(tmp_path: Path):
    fixture = _fixture(tmp_path, status="failed")
    fixture["result"]["failure_evidence"] = {
        "schema_version": "talk-final-review-failure-evidence.v1",
        "audit_loaded": False,
        "artifact_file": "missing.chat-authority.json",
        "surface_files": [],
    }
    manifest = _export(fixture, tmp_path / "bundle")
    imported = _import(fixture, manifest.parent)
    assert imported == fixture["result"]
    assert not (fixture["source"].parent / "missing.chat-authority.json").exists()
    assert "prepared_delivery" not in imported


def test_external_source_drift_blocks_import_before_candidate_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    fixture = _fixture(tmp_path)
    external_root = tmp_path / "external-source-root"
    monkeypatch.setattr(overflow_bundle_module, "_EXTERNAL_SOURCE_ROOTS", (external_root,))
    external = external_root / "recording.mp4"
    external.parent.mkdir()
    external.write_bytes(b"original-recording")
    fixture["result"]["segment_path"] = str(external)
    source_files = [{
        "path": str(external),
        "bytes": external.stat().st_size,
        "sha256": hashlib.sha256(external.read_bytes()).hexdigest(),
    }]
    bundle = tmp_path / "bundle"
    _export(fixture, bundle, source_files=source_files)

    fixture["source"].unlink()
    external.write_bytes(b"drifted-recording")
    with pytest.raises(OverflowBundleError, match="source preimage drifts"):
        _import(fixture, bundle)
    assert not fixture["source"].exists()
    assert not fixture["target"].exists()


def test_candidate_and_target_conflicts_are_create_only(tmp_path: Path):
    fixture = _fixture(tmp_path)
    bundle = tmp_path / "bundle"
    _export(fixture, bundle)

    fixture["source"].write_bytes(b"different-existing-source")
    with pytest.raises(OverflowBundleError, match="conflicts"):
        _import(fixture, bundle)
    assert not fixture["target"].exists()

    clean_parent = tmp_path / "target-clean"
    clean_parent.mkdir()
    clean = _fixture(clean_parent)
    _export(clean, tmp_path / "target-bundle")
    clean["target"].write_bytes(b"owned-by-another-attempt")
    with pytest.raises(OverflowBundleError, match="target already exists"):
        _import(clean, tmp_path / "target-bundle")
    assert clean["target"].read_bytes() == b"owned-by-another-attempt"


def test_failed_result_exports_evidence_without_fabricating_handle(tmp_path: Path):
    fixture = _fixture(tmp_path, status="rejected")
    # A rejected result intentionally has no prepared handle.  The bounded
    # evidence file is still transported and the result remains unchanged.
    bundle = tmp_path / "bundle"
    _export(fixture, bundle)
    document = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    assert document["prepared"] is None
    imported = _import(fixture, bundle)
    assert imported == fixture["result"]
    assert "prepared_delivery" not in imported


def test_bundle_requires_explicit_profile_binding_before_payload_write(tmp_path: Path):
    fixture = _fixture(tmp_path)
    with pytest.raises(OverflowBundleError, match="expected_profile_binding"):
        export_bundle(
            fixture["result"], fixture["root"], bundle_dir=tmp_path / "bundle",
            job_id="job-1", attempt_id="attempt-1",
            release_sha256="sha256:" + "1" * 64,
            input_sha256="sha256:" + "2" * 64,
            date=fixture["date"], lane="talk",
        )
    assert not (tmp_path / "bundle").exists()


def test_bundle_import_requires_explicit_profile_binding_before_materialization(tmp_path: Path):
    fixture = _fixture(tmp_path)
    manifest = _export(fixture, tmp_path / "bundle")
    fixture["source"].unlink()
    with pytest.raises(OverflowBundleError, match="expected_profile_binding"):
        import_bundle(
            manifest.parent, fixture["root"], "job-1", "attempt-1",
            "sha256:" + "1" * 64, "sha256:" + "2" * 64,
        )
    assert not fixture["source"].exists()


def test_bundle_same_id_manifest_byte_drift_is_rejected_before_materialization(tmp_path: Path):
    fixture = _fixture(tmp_path)
    manifest = _export(fixture, tmp_path / "bundle")
    profile_path = fixture["root"] / "repo/profiles/lidousha/profile.json"
    profile_path.write_text(profile_path.read_text() + "\n", encoding="utf-8")
    fixture["source"].unlink()
    with pytest.raises(OverflowBundleError, match="profile"):
        _import(fixture, manifest.parent)
    assert not fixture["source"].exists()
    assert not fixture["target"].exists()


def test_bundle_rejects_alternate_valid_profile_binding(tmp_path: Path):
    fixture = _fixture(tmp_path)
    manifest = _export(fixture, tmp_path / "bundle")
    default_path = fixture["root"] / "repo/profiles/lidousha/profile.json"
    document = json.loads(default_path.read_text(encoding="utf-8"))
    document["profile_id"] = "audit-other"
    document["identity"]["output_directory"] = "audit-other"
    alternate_path = fixture["root"] / "repo/profiles/audit-other/profile.json"
    alternate_path.parent.mkdir()
    alternate_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    from src.autoslice.channel_profile import binding_for_profile, load_channel_profile
    alternate_binding = binding_for_profile(
        load_channel_profile(repo_root=fixture["root"] / "repo", profile_id="audit-other", environ={})
    )
    fixture["source"].unlink()
    with pytest.raises(OverflowBundleError, match="profile binding"):
        import_bundle(
            manifest.parent, fixture["root"], "job-1", "attempt-1",
            "sha256:" + "1" * 64, "sha256:" + "2" * 64,
            expected_profile_binding=alternate_binding,
        )
    assert not fixture["source"].exists()


def test_bundle_explicit_binding_ignores_ambient_profile_drift(tmp_path: Path, monkeypatch):
    fixture = _fixture(tmp_path)
    manifest = _export(fixture, tmp_path / "bundle")
    default_path = fixture["root"] / "repo/profiles/lidousha/profile.json"
    document = json.loads(default_path.read_text(encoding="utf-8"))
    document["profile_id"] = "audit-other"
    document["identity"]["output_directory"] = "audit-other"
    alternate_path = fixture["root"] / "repo/profiles/audit-other/profile.json"
    alternate_path.parent.mkdir()
    alternate_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("AUTOSLICE_PROFILE", "audit-other")
    monkeypatch.setenv("AUTOSLICE_PROFILE_MANIFEST", str(alternate_path))
    assert _import(fixture, manifest.parent)["profile_binding"] == fixture["profile_binding"]


def test_legacy_v1_bundle_is_rejected_explicitly(tmp_path: Path):
    fixture = _fixture(tmp_path)
    manifest = _export(fixture, tmp_path / "bundle")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["schema_version"] = "autoslice-overflow-bundle.v1"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(OverflowBundleError, match="legacy overflow bundle v1"):
        _import(fixture, manifest.parent)


def test_explicit_external_manifest_binding_is_provenance_not_payload(tmp_path: Path):
    fixture = _fixture(tmp_path)
    from src.autoslice.channel_profile import binding_for_profile, load_channel_profile
    external = tmp_path / "external-profile.json"
    external.write_bytes((fixture["root"] / "repo/profiles/lidousha/profile.json").read_bytes())
    binding = binding_for_profile(load_channel_profile(fixture["root"] / "repo", manifest_path=external, environ={}))
    fixture["profile_binding"] = binding
    fixture["result"]["profile_binding"] = binding
    _export(fixture, tmp_path / "external-bundle")
    result = import_bundle(tmp_path / "external-bundle", fixture["root"], "job-1", "attempt-1", "1" * 64, "2" * 64, expected_profile_binding=binding)
    assert result["profile_binding"] == binding
    manifest = json.loads((tmp_path / "external-bundle/bundle.json").read_text())
    assert not any("external-profile" in entry["path"] for entry in manifest["files"])


@pytest.mark.parametrize("lane,profile_id,date,candidate_id", [
    ("talk", "lidousha", "2026-10-15", "retained-talk-2"),
    ("song", "other-show", "2026-11-03", "song-unseen-7"),
])
def test_versioned_prepare_export_import_keeps_logical_profile_target(
    tmp_path, monkeypatch, lane, profile_id, date, candidate_id,
):
    monkeypatch.delenv("AUTOSLICE_RUNTIME_VERSION_ROOT", raising=False)
    fixture = _fixture(tmp_path, versioned=True, lane=lane, profile_id=profile_id,
                       date=date, candidate_id=candidate_id)
    target = fixture["target"]
    assert target.relative_to(fixture["root"]).parts == (profile_id, date, target.name)
    manifest = _export(fixture, tmp_path / "bundle")
    # The controller must reconstruct sources and a fresh physical handle.
    old_manifest = fixture["handle"].manifest_path
    shutil.rmtree(old_manifest.parent)
    fixture["source"].unlink()
    imported = _import(fixture, manifest.parent)
    fresh = load_prepared_delivery(runtime_root=fixture["root"],
                                  manifest_path=Path(imported["prepared_delivery"]["manifest_path"]))
    assert _read_document(fresh)["artifacts"][0]["target_path"] == str(target)
    assert fixture["source"].read_bytes() == b"candidate-video"
    assert not target.exists()
    assert fresh.lane == lane


@pytest.mark.parametrize("target_root", ["state", "repo/lidousha", "other-show"])
def test_versioned_export_rejects_targets_outside_exact_bound_profile(
    tmp_path, monkeypatch, target_root,
):
    monkeypatch.delenv("AUTOSLICE_RUNTIME_VERSION_ROOT", raising=False)
    fixture = _fixture(tmp_path, versioned=True, target_root=target_root)
    with pytest.raises(OverflowBundleError, match="target.*namespace"):
        _export(fixture, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()
    assert not fixture["target"].exists()


def test_versioned_import_rejects_a_rehashed_wrong_profile_target(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOSLICE_RUNTIME_VERSION_ROOT", raising=False)
    fixture = _fixture(tmp_path, versioned=True)
    manifest = _export(fixture, tmp_path / "bundle")
    document = json.loads(manifest.read_text())
    prepared_row = next(row for row in document["files"] if row["kind"] == "prepared_manifest")
    prepared_payload = manifest.parent / prepared_row["payload"]
    prepared = json.loads(prepared_payload.read_text())
    wrong_target = fixture["root"] / "other-show" / fixture["date"] / "wrong.mp4"
    prepared["artifacts"][0]["target_path"] = str(wrong_target)
    unsigned = dict(prepared)
    unsigned.pop("prepared_sha256")
    digest = overflow_bundle_module._sha256_bytes(overflow_bundle_module._canonical_bytes(unsigned))
    prepared["prepared_sha256"] = digest
    payload = overflow_bundle_module._canonical_bytes(prepared)
    prepared_payload.write_bytes(payload)
    document["payload_bytes"] += len(payload) - prepared_row["bytes"]
    prepared_row.update(bytes=len(payload), sha256=overflow_bundle_module._sha256_bytes(payload))
    document["prepared"]["prepared_sha256"] = digest
    document["prepared"]["artifacts"][0]["target_path"] = wrong_target.relative_to(fixture["root"]).as_posix()
    document["result"]["prepared_delivery"]["prepared_sha256"] = digest
    document["manifest_sha256"] = overflow_bundle_module._manifest_digest(document)
    manifest.write_bytes(overflow_bundle_module._canonical_bytes(document))
    fixture["source"].unlink()
    with pytest.raises(OverflowBundleError, match="target escapes delivery namespace"):
        _import(fixture, manifest.parent)
    assert not fixture["source"].exists()
    assert not wrong_target.exists()


def test_versioned_bundle_reads_current_profile_and_preserves_legacy_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOSLICE_RUNTIME_VERSION_ROOT", raising=False)
    fixture = _fixture(tmp_path, versioned=True)
    legacy = fixture["root"] / "repo/profiles/lidousha/profile.json"
    legacy.write_text(legacy.read_text() + "\n")
    before = legacy.read_bytes()
    manifest = _export(fixture, tmp_path / "bundle")
    assert _import(fixture, manifest.parent)["status"] == "delivery_prepared_no_target"
    assert legacy.read_bytes() == before


@pytest.mark.parametrize("versioned", [False, True])
def test_provider_diagnostic_paths_are_preserved_without_file_capabilities(
    tmp_path, monkeypatch, versioned,
):
    monkeypatch.delenv("AUTOSLICE_RUNTIME_VERSION_ROOT", raising=False)
    fixture = _fixture(tmp_path, versioned=versioned)
    from src.autoslice.llm_client import sanitize_provider_diagnostics
    diagnostics = sanitize_provider_diagnostics({
        "provider_endpoint_path": "/api/v9/generate",
        "provider_credential_source": "/service/provider.env",
    })
    fixture["result"]["provider_runtime_binding"] = diagnostics
    # The same producer receipt is nested in persisted candidate evidence.
    fixture["evidence"].write_text(json.dumps({"cpa_request": {
        "provider_runtime_binding": diagnostics}}))
    before = fixture["evidence"].read_bytes()
    manifest = _export(fixture, tmp_path / "bundle")
    doc = json.loads(manifest.read_text())
    assert all(row["path"].startswith(("out/", ".prepared-deliveries/")) for row in doc["files"])
    fixture["evidence"].unlink()
    imported = _import(fixture, manifest.parent)
    assert imported["provider_runtime_binding"] == diagnostics
    assert fixture["evidence"].read_bytes() == before
    assert not fixture["target"].exists()


def test_diagnostic_mapping_does_not_hide_an_external_evidence_file(tmp_path):
    fixture = _fixture(tmp_path)
    fixture["evidence"].write_text(json.dumps({"provider_runtime_binding": {
        "provider_endpoint_path": "/api/v9",
        "evidence_path": str(tmp_path / "outside.json")}}))
    with pytest.raises(OverflowBundleError, match="JSON dependency escapes runtime_root"):
        _export(fixture, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("field", ["output_dir", "output_root"])
def test_directory_metadata_preserves_reference_without_recursive_copy(tmp_path, field):
    fixture = _fixture(tmp_path)
    directory = fixture["source"].parent / "replacement_recuts"
    directory.mkdir()
    unrelated = directory / "unreferenced.json"
    unrelated.write_text("{}")
    fixture["evidence"].write_text(json.dumps({"subtitle_audio_correspondence": {field: str(directory)}}))
    before = fixture["evidence"].read_bytes()
    manifest = _export(fixture, tmp_path / "bundle")
    document = json.loads(manifest.read_text())
    assert not any(row["path"].endswith("unreferenced.json") for row in document["files"])
    assert _import(fixture, manifest.parent)["status"] == "delivery_prepared_no_target"
    assert fixture["evidence"].read_bytes() == before
    # The same directory presented as a file locator must still fail.
    fixture["evidence"].write_text(json.dumps({"evidence_path": str(directory)}))
    with pytest.raises(OverflowBundleError, match="not a regular file"):
        _export(fixture, tmp_path / "second-bundle")
    assert not (tmp_path / "second-bundle").exists()


def test_frozen_truth_ledger_is_opaque_hash_bound_authority_not_historical_file_graph(tmp_path):
    fixture = _fixture(tmp_path)
    from src.autoslice.producer_source_truth_checkpoint import freeze_ledger
    source = Path(__file__).parents[1] / "assets/lidousha/subtitle_truth_ledger.v1.json"
    ledger = freeze_ledger(source, fixture["source"].parent)
    fixture["evidence"].write_text(json.dumps({"ledger_path": str(ledger)}))
    original = ledger.read_bytes()
    manifest = _export(fixture, tmp_path / "bundle")
    document = json.loads(manifest.read_text())
    copied = next(row for row in document["files"] if row["path"].endswith(ledger.name))
    assert (manifest.parent / copied["payload"]).read_bytes() == original
    assert _import(fixture, manifest.parent)["status"] == "delivery_prepared_no_target"
    assert ledger.read_bytes() == original
    ledger.write_bytes(original + b"\n")
    with pytest.raises(OverflowBundleError, match="frozen source truth ledger binding drifts"):
        _export(fixture, tmp_path / "damaged-bundle")
    assert not (tmp_path / "damaged-bundle").exists()
