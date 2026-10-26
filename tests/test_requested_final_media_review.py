"""Explicit local media / complete stub protocol checks, never real AV approval."""
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.autoslice.requested_final_media_review import build_request, validate_request, inspect_runtime
from src.autoslice.production_final_media_review_admission import (
    bootstrap_production_final_media_review_job, inspect_production_final_media_review_admission,
)
from tests.test_first_publication_boundary import published_fixture, recall_authority
from tests.test_production_final_media_review_admission import _package, _sha, _write_json
from tests.test_final_media_review_gemini_api_runtime import _runtime_tree
from src.autoslice.final_media_review_gemini_api_runtime import ensure_gemini_api_sentinel_runtime


def _request(tmp_path):
    published = tmp_path / "publication"
    published.mkdir()
    fixture = published_fixture(published)
    spec = dict(fixture["spec"], recovery_publication_authority=recall_authority(fixture))
    path = tmp_path / "normal-spec.json"
    _write_json(path, spec)
    return build_request(path)


def _requested_package(tmp_path, *, real_media=False, manual=False):
    request = _request(tmp_path)
    root, files = _package(tmp_path, mismatch=False)
    record = json.loads(files["record"].read_bytes())
    record["candidate_id"] = request["candidate_id"]
    source = json.loads(Path(request["source_spec"]["path"]).read_bytes())
    record["recovery_publication_authority"] = source["recovery_publication_authority"]
    _write_json(files["record"], record)
    if real_media:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            pytest.skip("local ffmpeg unavailable")
        completed = subprocess.run([ffmpeg, "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=25:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(files["video"])], capture_output=True, timeout=30)
        assert completed.returncode == 0, completed.stderr.decode()
    manifest_path = root / "review_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["candidate_id"] = request["candidate_id"]
    manifest["items"][0]["candidate_id"] = request["candidate_id"]
    manifest["items"][0]["sha256"]["evidence_json"] = _sha(files["record"])
    manifest["items"][0]["sha256"]["video"] = _sha(files["video"])
    if manual:
        # Synthetic normal manual-manifest shape; no human or AV receipt.
        cid, title = request["candidate_id"], source["given_title"]
        record.update(story_contract={"candidate_id": cid, "cover_reference_authority": {
            "source_visible_claims": ["synthetic visible subject"],
            "narrative_presentation": "synthetic cover text"}},
            publish_staging={"title": title})
        _write_json(files["record"], record)
        manifest.update(schema_version="lidousha-manual-review-manifest.v1",
            generated_by="build_manual_review_manifest.v1",
            run_mode="MANUAL_PRODUCE_REVIEW",
            status="finished_review_package_no_upload_pending_human_review",
            exact_candidate_ids=[cid], selection_contract={
                "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL", "candidate_ids": [cid]},
            manual_attestation={"operator": "explicit-fixture", "note": "protocol test only"})
        item = manifest["items"][0]
        item.pop("final_media_duration_binding")
        item.update(title=title, recovery_publication_authority=record["recovery_publication_authority"])
        item["sha256"]["evidence_json"] = _sha(files["record"])
    _write_json(manifest_path, manifest)
    return request, root, files


def test_explicit_request_creates_full_media_job_without_anomaly_or_candidate_whitelist(tmp_path):
    request, root, files = _requested_package(tmp_path, real_media=True)
    ordinary = inspect_production_final_media_review_admission(root, request["candidate_id"])
    assert ordinary["status"] == "NOT_REQUIRED"
    result = bootstrap_production_final_media_review_job(root, request["candidate_id"],
                                                         requested_review=request)
    assert result["created"] is True and result["provider_calls"] == 0
    job = json.loads(Path(result["job_path"]).read_bytes())
    plan = job["review_plan"]
    assert plan["trigger_reason_codes"] == ["OPERATOR_REQUESTED_WHOLE_FINAL_MEDIA"]
    assert plan["duration_binding"]["duration_witness_mismatch"] is False
    assert plan["requested_review"]["bvid"] == request["bvid"]
    assert plan["content_review_status"] == "UNASSESSED"
    point = plan["subtitle_review_points"][0]
    assert point["final_video_start_ms"] == 0
    assert point["final_video_end_ms"] == 2000
    assert job["requirements"]["continuous_audio_required"] is True
    assert job["requirements"]["continuous_visual_required"] is True
    second = bootstrap_production_final_media_review_job(root, request["candidate_id"],
                                                         requested_review=request)
    assert second["created"] is False


def test_normal_cli_prepares_completed_manual_package_with_real_small_media(tmp_path, capsys):
    from scripts.admit_final_media_review import main
    request, root, files = _requested_package(tmp_path, real_media=True, manual=True)
    assert inspect_production_final_media_review_admission(root, request["candidate_id"])["status"] == "NOT_REQUIRED"
    assert main(["prepare", "--spec", request["source_spec"]["path"],
                 "--package-root", str(root)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["created"] is True and result["provider_calls"] == 0
    assert result["content_review_status"] == "UNASSESSED"
    job = json.loads(Path(result["job_path"]).read_bytes())
    assert job["review_plan"]["requested_review"]["bvid"] == request["bvid"]
    assert job["review_plan"]["subtitle_review_points"][0]["final_video_end_ms"] == 2000


@pytest.mark.parametrize("damage", ["candidate_set", "selection", "status", "title"])
def test_manual_requested_admission_keeps_existing_closure_gate(tmp_path, damage):
    request, root, files = _requested_package(tmp_path, manual=True)
    path = root / "review_manifest.json"
    manifest = json.loads(path.read_bytes())
    if damage == "candidate_set":
        manifest["exact_candidate_ids"] = ["other"]
    elif damage == "selection":
        manifest["selection_contract"]["mode"] = "BACKFILL"
    elif damage == "status":
        manifest["status"] = "review_ready"
    else:
        manifest["items"][0]["title"] = "unbound title"
    _write_json(path, manifest)
    with pytest.raises(ValueError, match="MANIFEST|RECORD_IDENTITY"):
        inspect_production_final_media_review_admission(root, request["candidate_id"],
                                                        requested_review=request)


@pytest.mark.parametrize("field", ["bvid", "candidate_id", "scope", "publication_authority_sha256",
                                   "content_review_status", "release_authorized"])
def test_request_does_not_accept_identity_or_completion_claim_drift(tmp_path, field):
    request = _request(tmp_path)
    candidate = request["candidate_id"]
    request[field] = True if field == "release_authorized" else "forged"
    with pytest.raises(ValueError, match="FINAL_MEDIA_REVIEW_REQUEST"):
        validate_request(request, candidate_id=candidate)


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("artifact", ["video", "subtitle", "record"])
def test_requested_admission_rejects_final_bytes_mismatch(tmp_path, artifact, manual):
    request, root, files = _requested_package(tmp_path, manual=manual)
    files[artifact].write_bytes(files[artifact].read_bytes() + b"changed")
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        bootstrap_production_final_media_review_job(root, request["candidate_id"],
                                                    requested_review=request)


@pytest.mark.parametrize("damage", ["candidate", "bvid", "authority_hash", "missing"])
def test_requested_admission_binds_record_to_same_publication(tmp_path, damage):
    request, root, files = _requested_package(tmp_path)
    record = json.loads(files["record"].read_bytes())
    if damage == "candidate":
        record["candidate_id"] = "other"
    elif damage == "missing":
        record.pop("recovery_publication_authority")
    else:
        key = "bvid" if damage == "bvid" else "authority_sha256"
        record["recovery_publication_authority"][key] = "forged"
    _write_json(files["record"], record)
    manifest = json.loads((root / "review_manifest.json").read_bytes())
    manifest["items"][0]["sha256"]["evidence_json"] = _sha(files["record"])
    _write_json(root / "review_manifest.json", manifest)
    with pytest.raises(ValueError, match="REQUEST_RECORD_AUTHORITY_DRIFT"):
        inspect_production_final_media_review_admission(root, request["candidate_id"],
                                                        requested_review=request)


@pytest.mark.parametrize("damage", ["coverage", "contract_hash"])
def test_generic_bootstrap_cannot_replace_requested_full_contract(tmp_path, damage):
    from src.autoslice.final_media_review_bootstrap import bootstrap_final_media_review_job
    request, root, files = _requested_package(tmp_path)
    admission = inspect_production_final_media_review_admission(root, request["candidate_id"],
                                                                requested_review=request)
    points = copy.deepcopy(admission["review_points"])
    contract = admission["review_contract_sha256"]
    if damage == "coverage":
        points[0]["final_video_end_ms"] = 1000
    else:
        contract = "0" * 64
    with pytest.raises(ValueError, match="FULL_COVERAGE_REQUIRED"):
        bootstrap_final_media_review_job(package_root=root, candidate_id=request["candidate_id"],
            closure=admission["closure"], review_contract_sha256=contract,
            review_points=points, requested_review=request)


def test_missing_runtime_and_configured_runtime_are_not_content_review(tmp_path):
    runtime, environment = _runtime_tree(tmp_path)
    first = inspect_runtime(runtime)
    assert first["status"] == "SENTINEL_RUNTIME_ABSENT"
    assert first["provider_calls"] == 0 and first["runner_called"] is False
    # Explicit dummy keys and inert tool fixtures only create configuration.
    provisioned = ensure_gemini_api_sentinel_runtime(runtime, environment=environment)
    assert provisioned["provider_calls"] == 0
    second = inspect_runtime(runtime)
    assert second["status"] == "SENTINEL_REQUIRED"
    assert second["identity"]["provider"] == provisioned["provider"]
    assert second["content_review_status"] == "NOT_PERFORMED"
    assert second["release_authorized"] is False
