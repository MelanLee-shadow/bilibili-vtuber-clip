"""Portable mechanical-review regression cases, using synthetic package bytes.

The production canonical auditor is only replaced in explicit unit seams.
The unpatched negative case verifies that fake media cannot pass real audit.
No private review contract, real candidate, media, or publication authority is
required. The paired private suite retains its historical coverage.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import wave

import pytest

from scripts import build_final_human_review as builder
from src.autoslice import final_human_review as human_review
from src.autoslice.final_media_review_inputs import (
    ASSESSMENT_SCHEMA_VERSION,
    JOB_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    canonical_sha256,
)
from src.autoslice.final_media_review_raw_av import (
    PACKAGE_BINDING_SCHEMA_VERSION,
    RUNTIME_CAPABILITY_FILENAME,
    RUNTIME_CAPABILITY_SCHEMA_VERSION,
)
from tests.final_media_review_test_support import (
    package_model_capability_fields,
    write_model_capability_attestation,
)

CANDIDATE_ID = "auto_193450_1000_1020"
STEM = "reviewed-clip"
TITLE = "【主播】测试片完整标题"

def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _wav(path: Path, *, duration_us: int) -> None:
    frames = round(duration_us * 16_000 / 1_000_000)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\0\0" * frames)


def _transport_capability(
    root: Path, *, provider_audio: bool, provider_video: bool
) -> dict[str, object]:
    document = {
        "schema_version": PACKAGE_BINDING_SCHEMA_VERSION,
        "capability_id": "synthetic-test-capability",
        "provider": "cpa",
        "transport": "content_bound_command",
        "model": "synthetic-raw-av-model",
        "endpoint_family": "synthetic_raw_av",
        "accepts": {
            "raw_audio": provider_audio,
            "continuous_source_video": provider_video,
        },
        "runtime_capability_sha256": "1" * 64,
        "executable_sha256": "2" * 64,
        "command_contract_sha256": "3" * 64,
        **package_model_capability_fields(),
        "result_schema_version": RESULT_SCHEMA_VERSION,
    }
    path = root / "verification" / (
        f"transport-capability-{int(provider_audio)}-{int(provider_video)}.json"
    )
    _write_json(path, document)
    return {
        "path": str(path.resolve()),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _raw_av_runtime(root: Path) -> Path:
    runtime = root / "synthetic-runtime"
    adapter = runtime / "repo" / "scripts" / "synthetic_raw_av_adapter.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text(
        r'''#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--request", required=True)
parser.add_argument("--response", required=True)
args = parser.parse_args()
request_path = Path(args.request)
response_path = Path(args.response)
request_bytes = request_path.read_bytes()
request = json.loads(request_bytes)
runtime = Path(os.environ["AUTOSLICE_BASE"])
capability = json.loads(
    (runtime / "final-media-review-raw-av-capability.json").read_text()
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


if sha(request["source_video"]["path"]) != request["source_video"]["sha256"]:
    raise SystemExit(31)
if sha(request["raw_audio"]["path"]) != request["raw_audio"]["sha256"]:
    raise SystemExit(32)
(runtime / "adapter-calls.txt").open("a", encoding="utf-8").write("call\n")
request_sha = hashlib.sha256(request_bytes).hexdigest()
result = {
    "schema_version": "final-media-perceptual-review-result.v1",
    "candidate_id": request["candidate_id"],
    "source_video_sha256": request["source_video"]["sha256"],
    "asset_manifest_sha256": request["asset_manifest_sha256"],
    "status": "PASS",
    "content_review_status": "PASS",
    "observations": [
        {
            "scope": "synthetic-normal-entry-only",
            "verdict": "exact request binding observed",
        }
    ],
}
receipt = {
    "schema_version": "final-media-review-raw-av-adapter-receipt.v3",
    "provider": "cpa",
    "model": request["model"],
    "endpoint_family": request["endpoint_family"],
    "capability_id": request["capability_id"],
    "runtime_capability_sha256": request["runtime_capability_sha256"],
    "executable_sha256": capability["executable"]["sha256"],
    "model_capability_seal_receipt_sha256": request[
        "model_capability_seal_receipt_sha256"
    ],
    "model_capability_attestation_sha256": request[
        "model_capability_attestation_sha256"
    ],
    "model_capability_contract_sha256": request[
        "model_capability_contract_sha256"
    ],
    "model_capability_sentinel_result_sha256": request[
        "model_capability_sentinel_result_sha256"
    ],
    "model_capability_sentinel_result_self_sha256": request[
        "model_capability_sentinel_result_self_sha256"
    ],
    "model_capability_sentinel_runner_sha256": request[
        "model_capability_sentinel_runner_sha256"
    ],
    "request_sha256": request_sha,
    "candidate_id": request["candidate_id"],
    "source_video_sha256": request["source_video"]["sha256"],
    "raw_audio_sha256": request["raw_audio"]["sha256"],
    "consumed_continuous_source_video": True,
    "consumed_raw_audio": True,
}
response_path.write_text(
    json.dumps(
        {
            "schema_version": "final-media-review-raw-av-adapter-response.v3",
            "request_sha256": request_sha,
            "transport_receipt": receipt,
            "result": result,
        },
        sort_keys=True,
    )
    + "\n"
)
''',
        encoding="utf-8",
    )
    adapter.chmod(0o700)
    seal_binding = write_model_capability_attestation(runtime, adapter)
    capability = {
        "schema_version": RUNTIME_CAPABILITY_SCHEMA_VERSION,
        "capability_id": "synthetic-mechanical-raw-av",
        "provider": "cpa",
        "transport": "content_bound_command",
        "model": "synthetic-raw-av-model",
        "endpoint_family": "synthetic_raw_av",
        "accepts": {
            "raw_audio": True,
            "continuous_source_video": True,
        },
        "executable": {
            "path": "repo/scripts/synthetic_raw_av_adapter.py",
            "sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
        },
        "argv": [
            "{executable}",
            "--request",
            "{request_json}",
            "--response",
            "{response_json}",
        ],
        "timeout_seconds": 30,
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "model_capability_seal": seal_binding,
    }
    capability_path = runtime / RUNTIME_CAPABILITY_FILENAME
    _write_json(capability_path, capability)
    capability_path.chmod(0o600)
    return runtime


@pytest.fixture
def receipt_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    root = tmp_path / "package"
    root.mkdir()
    verification = root / "verification"
    verification.mkdir()
    paths = {
        "video": root / f"{STEM}.mp4",
        "subtitle": root / f"{STEM}.srt",
        "cover": root / f"{STEM}.cover.png",
        "record": root / f"{STEM}.record.json",
    }
    for kind in ("video", "subtitle", "cover"):
        paths[kind].write_bytes(f"final {kind} bytes".encode())

    source_claims = [
        "最终源帧左侧清楚可见主播",
        "最终源帧右侧清楚可见乙乙",
    ]
    narrative = "封面文字表达两人围绕测试问题争论的故事"
    authority = {
        "candidate_id": CANDIDATE_ID,
        "observed_public_title": TITLE,
        "bvid": "BV1234567890",
        "aid": 123,
        "cid": 456,
        "authority_sha256": "sha256:" + "a" * 64,
    }
    record = {
        "burned_preview": {"branding_intro": {"verification": {"duration_ms": 20_000}}},
        "story_contract": {
            "candidate_id": CANDIDATE_ID,
            "cover_reference_authority": {
                "source_visible_claims": source_claims,
                "narrative_presentation": narrative,
            },
        },
        "publish_staging": {"title": TITLE},
        "recovery_publication_authority": authority,
    }
    _write_json(paths["record"], record)
    manifest = {
        "status": ("finished_review_package_no_upload_pending_human_review"),
        "upload_allowed": False,
        "exact_candidate_ids": [CANDIDATE_ID],
        "selection_contract": {
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "candidate_ids": [CANDIDATE_ID],
        },
        "items": [
            {
                "candidate_id": CANDIDATE_ID,
                "title": TITLE,
                "video": paths["video"].name,
                "mp4": paths["video"].name,
                "subtitle_srt": paths["subtitle"].name,
                "cover": paths["cover"].name,
                "record": paths["record"].name,
                "recovery_publication_authority": authority,
            }
        ],
    }
    review_path = root / "review_manifest.json"
    _write_json(review_path, manifest)

    audit = {
        "schema_version": builder.AUDIT_SCHEMA_VERSION,
        "policy_epoch": builder.AUDIT_POLICY_EPOCH,
        "policy_fingerprint": "sha256:" + "b" * 64,
        "auditor_source_sha256": "sha256:" + "c" * 64,
        "passed": True,
        "root": str(root.resolve()),
        "audited_inputs": [],
        "issues": [],
        "issue_count": 0,
        "blocking_issue_count": 0,
    }
    audit_path = verification / "package-audit.json"
    _write_json(audit_path, audit)
    monkeypatch.setattr(builder, "audit_package", lambda _root: copy.deepcopy(audit))

    # Empty placeholders test that the mechanical route needs no manual
    # contract or human statements. They are never passed off as real reviews.
    contract_path = tmp_path / "unused-human-contract.json"
    evidence_path = verification / "unused-human-evidence.json"
    _write_json(contract_path, {})
    _write_json(evidence_path, {})
    monkeypatch.setattr(human_review, "FINAL_MEDIA_REVIEW_CONTRACT_PATH", contract_path)
    return {
        "root": root, "paths": paths, "manifest": manifest,
        "review_path": review_path, "audit": audit, "audit_path": audit_path,
        "contract_path": contract_path, "evidence_path": evidence_path,
        "output": verification / "mechanical-review.json", "authority": authority,
    }

@pytest.fixture
def mechanical_package(receipt_package, monkeypatch):
    from src.autoslice import mechanical_delivery_review as mechanical
    # Unit seam only: production always invokes the actual canonical auditor.
    # All real paths, bytes, target identities and create-only writes stay real.
    monkeypatch.setattr(mechanical, "audit_package", lambda root: copy.deepcopy(receipt_package["audit"]))
    ass = receipt_package["root"] / "burned.ass"
    ass.write_bytes(b"synthetic ASS for identity-only unit test")
    receipt_package["paths"]["ass"] = ass
    path = receipt_package["paths"]["record"]
    record = json.loads(path.read_text())
    video = receipt_package["paths"]["video"]
    record["burned_preview"].update(
        status="BURNED", path=str(video), ass_path=str(ass), burned_sha256=_sha256(video),
    )
    record["artifact_hashes"] = {
        "burned_video_sha256": _sha256(video), "ass_sha256": _sha256(ass),
    }
    _write_json(path, record)
    return receipt_package, mechanical


def test_mechanical_receipt_does_not_require_or_claim_human_viewing(mechanical_package):
    fixture, mechanical = mechanical_package
    fixture["evidence_path"].unlink()
    fixture["contract_path"].unlink()  # no new mandatory manual point checklist
    value = mechanical.build_mechanical_receipt(fixture["root"], fixture["audit_path"])
    assert value["fresh_human_full_playback_claimed"] is False
    assert value["new_upload_authorized"] is False
    assert "reviewed_by" not in value and "checks" not in value
    assert mechanical.validate_mechanical_receipt(
        value, fixture["root"], fixture["audit_path"],
        publication_authority=fixture["authority"],
    ) == value


def test_mechanical_cli_create_only_and_existing_uploader_replay(mechanical_package):
    fixture, _ = mechanical_package
    path = fixture["output"].with_name("mechanical-review.json")
    args = [str(fixture["root"]), "--package-audit", str(fixture["audit_path"]),
            "--mechanical", "--out", str(path)]
    assert builder.main(args) == 0
    before = path.read_bytes()
    assert builder.main(args) == 2
    assert path.read_bytes() == before

    manifest = {
        "package_attestation": {
            "package_root": str(fixture["root"].resolve()),
            "review_manifest": builder._absolute_attestation(fixture["review_path"]),
            "package_audit": builder._absolute_attestation(fixture["audit_path"]),
        },
        "recovery_publication_authority": fixture["authority"],
        "season": {"lane": "talk"},
    }
    assert human_review.attach_final_human_review(
        manifest, path, season_ids={"talk": {"season_id": 123, "section_id": 456}},
    ) == []
    assert human_review.final_human_review_attestation_problems(manifest) == []
    assert human_review.ordinary_upload_problems(manifest)  # never a new BV permission
    manifest["recovery_publication_authority"] = {**fixture["authority"], "cid": 999}
    assert human_review.final_human_review_attestation_problems(manifest)


@pytest.mark.parametrize("kind", ["video", "subtitle", "cover", "record", "ass"])
def test_mechanical_receipt_rejects_artifact_drift(mechanical_package, kind):
    fixture, mechanical = mechanical_package
    value = mechanical.build_mechanical_receipt(fixture["root"], fixture["audit_path"])
    with fixture["paths"][kind].open("ab") as handle:
        handle.write(b"\n ")
    with pytest.raises((ValueError, human_review.FinalHumanReviewError)):
        mechanical.validate_mechanical_receipt(value, fixture["root"], fixture["audit_path"])


def test_mechanical_receipt_never_overrides_a_fresh_audit_failure(mechanical_package):
    fixture, mechanical = mechanical_package
    value = mechanical.build_mechanical_receipt(fixture["root"], fixture["audit_path"])
    fixture["audit"]["passed"] = False
    fixture["audit"]["blocking_issue_count"] = 1
    with pytest.raises(ValueError, match="current canonical audit rejected"):
        mechanical.validate_mechanical_receipt(value, fixture["root"], fixture["audit_path"])


@pytest.mark.parametrize("key", ["fresh_human_full_playback_claimed", "new_upload_authorized"])
def test_mechanical_receipt_rejects_false_claims(mechanical_package, key):
    fixture, mechanical = mechanical_package
    value = mechanical.build_mechanical_receipt(fixture["root"], fixture["audit_path"])
    value[key] = True
    with pytest.raises(ValueError, match="overclaims"):
        mechanical.validate_mechanical_receipt(value, fixture["root"], fixture["audit_path"])


def test_mechanical_path_runs_real_canonical_auditor_for_invalid_package(receipt_package):
    from src.autoslice import mechanical_delivery_review as mechanical
    # Deliberately do NOT patch mechanical.audit_package: synthetic MP4 bytes
    # and a self-reported PASS cannot authorize a real delivery.
    with pytest.raises((ValueError, human_review.FinalHumanReviewError)):
        mechanical.build_mechanical_receipt(receipt_package["root"], receipt_package["audit_path"])


def test_mechanical_writer_rechecks_inputs_before_final_link(mechanical_package, monkeypatch):
    fixture, _ = mechanical_package
    original = builder._secure_json_create_only

    def drift(**kwargs):
        fixture["paths"]["video"].write_bytes(b"replaced while preparing output")
        return original(**kwargs)

    monkeypatch.setattr(builder, "_secure_json_create_only", drift)
    path = fixture["output"].with_name("mechanical-race.json")
    assert builder.main([str(fixture["root"]), "--package-audit", str(fixture["audit_path"]),
                         "--mechanical", "--out", str(path)]) == 2
    assert not path.exists()


@pytest.mark.parametrize("defect", ["not_burned", "video_hash", "ass_hash"])
def test_mechanical_review_reuses_producer_burn_identity_gate(mechanical_package, defect):
    fixture, mechanical = mechanical_package
    path = fixture["paths"]["record"]
    record = json.loads(path.read_text())
    if defect == "not_burned":
        record["burned_preview"]["status"] = "PLANNED"
    elif defect == "video_hash":
        record["artifact_hashes"]["burned_video_sha256"] = "sha256:" + "0" * 64
    else:
        record["artifact_hashes"]["ass_sha256"] = "sha256:" + "0" * 64
    _write_json(path, record)
    with pytest.raises(ValueError, match="burn output binding invalid"):
        mechanical.build_mechanical_receipt(fixture["root"], fixture["audit_path"])

def _write_final_media_review_state(
    fixture: dict[str, object],
    *,
    status: str,
    result_status: str | None = None,
    result_asset_sha256: str | None = None,
) -> Path:
    video_sha256 = _sha256(fixture["paths"]["video"])[7:]
    asset_manifest_sha256 = "d" * 64
    assessment = {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "candidate_id": CANDIDATE_ID,
        "content_review_status": "UNASSESSED",
        "asset_manifest": {"sha256": asset_manifest_sha256},
        "exact_media_clock": {"source_video_sha256": video_sha256},
    }
    result = None
    if result_status is not None:
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "candidate_id": CANDIDATE_ID,
            "source_video_sha256": video_sha256,
            "asset_manifest_sha256": (
                result_asset_sha256 or asset_manifest_sha256
            ),
            "status": result_status,
            "content_review_status": result_status,
            "observations": ["synthetic bound perceptual result"],
        }
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "candidate_id": CANDIDATE_ID,
        "binding_sha256": "a" * 64,
        "job_path": "/synthetic/final-media-review-job.json",
        "job_sha256": "b" * 64,
        "source_video_sha256": video_sha256,
        "asset_manifest_sha256": asset_manifest_sha256,
        "content_review_required": True,
        "attempt_count": 1 if result is not None else 0,
        "updated_at": "2026-09-30T12:00:00Z",
        "next_attempt_at": None,
        "assessment": assessment,
        "runtime_binding": None,
        "provider_diagnostics": {},
        "result": result,
        "reason_codes": (
            []
            if result_status == "PASS"
            else ["FINAL_MEDIA_REVIEW_INPUT_OR_CONTENT_BLOCKED"]
        ),
        "status": status,
    }
    state["state_sha256"] = canonical_sha256(state)
    path = (
        fixture["root"]
        / "verification"
        / f"{CANDIDATE_ID}.final-media-review-state.json"
    )
    _write_json(path, state)
    return path


def test_mechanical_review_refuses_present_unresolved_perceptual_state(
    mechanical_package,
):
    fixture, mechanical = mechanical_package
    _write_final_media_review_state(fixture, status="BLOCKED_INPUT")

    with pytest.raises(
        ValueError, match="final media perceptual review unresolved"
    ):
        mechanical.build_mechanical_receipt(
            fixture["root"], fixture["audit_path"]
        )


def test_mechanical_review_binds_complete_perceptual_pass_and_replays_it(
    mechanical_package,
):
    fixture, mechanical = mechanical_package
    state_path = _write_final_media_review_state(
        fixture, status="COMPLETE", result_status="PASS"
    )

    receipt = mechanical.build_mechanical_receipt(
        fixture["root"], fixture["audit_path"]
    )
    item = receipt["bindings"]["items"][0]
    assert item["final_media_review_state"] == {
        "path": f"verification/{CANDIDATE_ID}.final-media-review-state.json",
        "sha256": _sha256(state_path),
        "bytes": state_path.stat().st_size,
    }
    assert mechanical.validate_mechanical_receipt(
        receipt, fixture["root"], fixture["audit_path"]
    ) == receipt

    state_path.write_text(state_path.read_text() + " ", encoding="utf-8")
    with pytest.raises((ValueError, human_review.FinalHumanReviewError)):
        mechanical.validate_mechanical_receipt(
            receipt, fixture["root"], fixture["audit_path"]
        )


def test_mechanical_review_rejects_self_hashed_result_binding_drift(
    mechanical_package,
):
    fixture, mechanical = mechanical_package
    _write_final_media_review_state(
        fixture,
        status="COMPLETE",
        result_status="PASS",
        result_asset_sha256="e" * 64,
    )

    with pytest.raises(ValueError, match="final media review state invalid"):
        mechanical.build_mechanical_receipt(
            fixture["root"], fixture["audit_path"]
        )

def test_mechanical_consumer_discovers_job_and_persists_input_block(
    mechanical_package,
):
    fixture, mechanical = mechanical_package
    video = fixture["paths"]["video"]
    manifest_path = fixture["root"] / "verification" / "review-assets.json"
    _write_json(
        manifest_path,
        {
            "schema": "synthetic-review-assets.v1",
            "source_video": {
                "path": str(video.resolve()),
                "bytes": video.stat().st_size,
                "sha256": _sha256(video),
            },
            "artifacts": [],
        },
    )
    job_path = (
        fixture["root"]
        / "verification"
        / f"{CANDIDATE_ID}.final-media-review-job.json"
    )
    _write_json(
        job_path,
        {
            "schema_version": JOB_SCHEMA_VERSION,
            "candidate_id": CANDIDATE_ID,
            "asset_manifest": {
                "path": str(manifest_path.resolve()),
                "sha256": _sha256(manifest_path),
            },
            "media_clock": {
                "source_video_sha256": _sha256(video),
                "source_video_bytes": video.stat().st_size,
                "duration_us": 20_000_000,
            },
            "requirements": {
                "continuous_audio_required": False,
                "continuous_visual_required": True,
                "content_review_required": True,
                "exact_media_clock_evidence_required": False,
                "provider_accepts_bound_source_video": False,
                "provider_accepts_bound_audio": True,
            },
            "transport_capability": _transport_capability(
                fixture["root"], provider_audio=True, provider_video=False
            ),
        },
    )

    with pytest.raises(
        ValueError, match="final media perceptual review unresolved"
    ):
        mechanical.build_mechanical_receipt(
            fixture["root"], fixture["audit_path"]
        )

    state_path = (
        fixture["root"]
        / "verification"
        / f"{CANDIDATE_ID}.final-media-review-state.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "BLOCKED_INPUT"
    assert state["result"] is None
    assert state["attempt_count"] == 0
    assert "FINAL_MEDIA_REVIEW_VISUAL_INPUT_NOT_CONTINUOUS" in state["reason_codes"]



def test_mechanical_consumer_rejects_job_manifest_outside_package_root(
    mechanical_package,
):
    fixture, mechanical = mechanical_package
    video = fixture["paths"]["video"]
    external_manifest = fixture["root"].parent / "external-review-assets.json"
    _write_json(
        external_manifest,
        {
            "schema": "synthetic-review-assets.v1",
            "source_video": {
                "path": str(video.resolve()),
                "bytes": video.stat().st_size,
                "sha256": _sha256(video),
            },
            "artifacts": [],
        },
    )
    job_path = (
        fixture["root"]
        / "verification"
        / f"{CANDIDATE_ID}.final-media-review-job.json"
    )
    _write_json(
        job_path,
        {
            "schema_version": JOB_SCHEMA_VERSION,
            "candidate_id": CANDIDATE_ID,
            "asset_manifest": {
                "path": str(external_manifest.resolve()),
                "sha256": _sha256(external_manifest),
            },
            "media_clock": {
                "source_video_sha256": _sha256(video),
                "source_video_bytes": video.stat().st_size,
                "duration_us": 20_000_000,
            },
            "requirements": {
                "continuous_audio_required": True,
                "continuous_visual_required": True,
                "content_review_required": True,
                "exact_media_clock_evidence_required": False,
                "provider_accepts_bound_source_video": True,
                "provider_accepts_bound_audio": True,
            },
            "transport_capability": _transport_capability(
                fixture["root"], provider_audio=True, provider_video=True
            ),
        },
    )

    with pytest.raises(ValueError, match="final media review job invalid"):
        mechanical.build_mechanical_receipt(
            fixture["root"], fixture["audit_path"]
        )

    state_path = (
        fixture["root"]
        / "verification"
        / f"{CANDIDATE_ID}.final-media-review-state.json"
    )
    assert not state_path.exists()

def test_mechanical_consumer_materializes_audio_before_capability_wait(
    mechanical_package, monkeypatch: pytest.MonkeyPatch
):
    fixture, mechanical = mechanical_package
    from src.autoslice.final_media_review_materialization import (
        resolve_or_materialize_review_job as real_resolver,
    )

    video = fixture["paths"]["video"]
    verification = fixture["root"] / "verification"
    partial = verification / "partial-review-audio.wav"
    _wav(partial, duration_us=10_000_000)
    frame = verification / "review-frame.jpg"
    frame.write_bytes(b"diagnostic frame bytes")
    manifest = {
        "schema": "synthetic-review-assets.v1",
        "source_video": {
            "path": str(video.resolve()),
            "bytes": video.stat().st_size,
            "sha256": _sha256(video),
        },
        "artifacts": [
            {
                "path": str(partial.resolve()),
                "bytes": partial.stat().st_size,
                "sha256": _sha256(partial),
                "kind": "diagnostic_wav",
                "window": "first-half",
                "range_seconds": [0.0, 10.0],
            },
            {
                "path": str(frame.resolve()),
                "bytes": frame.stat().st_size,
                "sha256": _sha256(frame),
                "kind": "frame",
                "window": "sample",
                "timestamp_seconds": 5.0,
            },
        ],
    }
    manifest_path = verification / "review-assets.json"
    _write_json(manifest_path, manifest)
    job_path = verification / f"{CANDIDATE_ID}.final-media-review-job.json"
    _write_json(
        job_path,
        {
            "schema_version": JOB_SCHEMA_VERSION,
            "candidate_id": CANDIDATE_ID,
            "asset_manifest": {
                "path": str(manifest_path.resolve()),
                "sha256": _sha256(manifest_path),
            },
            "media_clock": {
                "source_video_sha256": _sha256(video),
                "source_video_bytes": video.stat().st_size,
                "duration_us": 20_000_000,
            },
            "requirements": {
                "continuous_audio_required": True,
                "continuous_visual_required": True,
                "content_review_required": True,
                "exact_media_clock_evidence_required": False,
                "provider_accepts_bound_source_video": True,
                "provider_accepts_bound_audio": True,
            },
            "transport_capability": _transport_capability(
                fixture["root"], provider_audio=True, provider_video=True
            ),
        },
    )
    calls = {"extract": 0}

    def fake_extract(_source: Path, output: Path, *, duration_us: int) -> None:
        calls["extract"] += 1
        _wav(output, duration_us=duration_us)

    def resolver(job, *, allowed_root):
        return real_resolver(
            job, allowed_root=allowed_root, extract_full_audio=fake_extract
        )

    monkeypatch.setattr(mechanical, "resolve_or_materialize_review_job", resolver)

    with pytest.raises(
        ValueError, match="final media perceptual review unresolved"
    ):
        mechanical.build_mechanical_receipt(
            fixture["root"], fixture["audit_path"]
        )

    assert calls["extract"] == 1
    state_path = (
        verification / f"{CANDIDATE_ID}.final-media-review-state.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "WAITING_CAPABILITY"
    assert state["reason_codes"] == ["FINAL_MEDIA_REVIEW_CPA_RUNTIME_UNBOUND"]
    assert state["attempt_count"] == 0
    assert state["assessment"]["audio"]["actual_gaps"] == []
    assert state["assessment"]["audio"]["exact_full_audio_wav_count"] == 1
    assert state["job_path"] != str(job_path.resolve())
    assert list(
        verification.rglob("materialization-receipt.json")
    )

    # Restart resolves the same successor and does not re-extract or call CPA.
    with pytest.raises(
        ValueError, match="final media perceptual review unresolved"
    ):
        mechanical.build_mechanical_receipt(
            fixture["root"], fixture["audit_path"]
        )
    assert calls["extract"] == 1
    restarted = json.loads(state_path.read_text(encoding="utf-8"))
    assert restarted["state_sha256"] == state["state_sha256"]



def test_mechanical_normal_entry_binds_runtime_raw_av_and_deduplicates(
    mechanical_package, monkeypatch: pytest.MonkeyPatch
):
    fixture, mechanical = mechanical_package
    video = fixture["paths"]["video"]
    verification = fixture["root"] / "verification"
    audio = verification / "full-final-media.wav"
    _wav(audio, duration_us=20_000_000)
    manifest = {
        "schema": "synthetic-review-assets.v1",
        "source_video": {
            "path": str(video.resolve()),
            "bytes": video.stat().st_size,
            "sha256": _sha256(video),
        },
        "artifacts": [
            {
                "path": str(audio.resolve()),
                "bytes": audio.stat().st_size,
                "sha256": _sha256(audio),
                "kind": "exact_full_audio_wav",
                "window": "full-final-media",
                "range_seconds": [0.0, 20.0],
                "actual_duration_us": 20_000_000,
                "sample_rate": 16_000,
                "sample_frames": 320_000,
                "source_video_sha256": _sha256(video),
            }
        ],
    }
    manifest_path = verification / "review-assets.json"
    _write_json(manifest_path, manifest)
    job_path = verification / f"{CANDIDATE_ID}.final-media-review-job.json"
    _write_json(
        job_path,
        {
            "schema_version": JOB_SCHEMA_VERSION,
            "candidate_id": CANDIDATE_ID,
            "asset_manifest": {
                "path": str(manifest_path.resolve()),
                "sha256": _sha256(manifest_path),
            },
            "media_clock": {
                "source_video_sha256": _sha256(video),
                "source_video_bytes": video.stat().st_size,
                "duration_us": 20_000_000,
            },
            "requirements": {
                "continuous_audio_required": True,
                "continuous_visual_required": True,
                "content_review_required": True,
                "exact_media_clock_evidence_required": False,
                "provider_accepts_bound_source_video": False,
                "provider_accepts_bound_audio": False,
            },
            "review_plan": {
                "authority": "SYNTHETIC_NORMAL_ENTRY_TEST_ONLY",
                "review_points": [
                    {
                        "point_id": "whole-final-media",
                        "final_video_start_ms": 0,
                        "final_video_end_ms": 20_000,
                        "expectation": "inspect exact final audio and video",
                    }
                ],
            },
        },
    )
    runtime = _raw_av_runtime(fixture["root"])
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    monkeypatch.setenv("AUTOSLICE_PROVIDER_WAIT_SECONDS", "1")

    first = mechanical.build_mechanical_receipt(
        fixture["root"], fixture["audit_path"]
    )

    state_path = (
        verification / f"{CANDIDATE_ID}.final-media-review-state.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "COMPLETE"
    assert state["attempt_count"] == 1
    assert state["result"]["status"] == "PASS"
    assert state["result"]["transport_evidence"]["consumed_raw_audio"] is True
    assert (
        state["result"]["transport_evidence"][
            "consumed_continuous_source_video"
        ]
        is True
    )
    item = first["bindings"]["items"][0]
    assert "final_media_review_active_job" in item
    assert "final_media_review_transport_capability" in item
    assert "final_media_review_transport_binding" in item
    assert "final_media_review_state" in item
    assert (runtime / "adapter-calls.txt").read_text().splitlines() == ["call"]

    second = mechanical.build_mechanical_receipt(
        fixture["root"], fixture["audit_path"]
    )
    restarted = json.loads(state_path.read_text(encoding="utf-8"))
    assert restarted["state_sha256"] == state["state_sha256"]
    assert second["bindings"] == first["bindings"]
    assert (runtime / "adapter-calls.txt").read_text().splitlines() == ["call"]


def test_mechanical_consumer_bootstraps_stale_duration_witness_then_materializes(
    mechanical_package, monkeypatch: pytest.MonkeyPatch
):
    fixture, mechanical = mechanical_package
    from src.autoslice.final_media_review_bootstrap import (
        bootstrap_final_media_review_job as real_bootstrap,
    )
    from src.autoslice.final_media_review_materialization import (
        resolve_or_materialize_review_job as real_resolver,
    )

    record_path = fixture["paths"]["record"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["burned_preview"]["verification"] = {"duration_ms": 20_000}
    record["burned_preview"]["branding_intro"]["verification"] = {
        "duration_ms": 37_546
    }
    _write_json(record_path, record)
    contract_sha = "sha256:" + "a" * 64
    points = [
        {
            "point_id": "whole-final-media",
            "final_video_start_ms": 0,
            "final_video_end_ms": 20_000,
            "expectation": "inspect exact final audio/video and retain UNASSESSED until CPA",
        }
    ]
    monkeypatch.setattr(
        human_review,
        "_review_contracts",
        lambda: (contract_sha, {CANDIDATE_ID: points}),
    )
    calls = {"probe": 0, "extract": 0}

    def fake_probe(_video: Path) -> dict[str, object]:
        calls["probe"] += 1
        return {
            "duration_us": 20_000_000,
            "first_video_pts_us": 0,
            "last_video_pts_us": 19_960_000,
            "video_frame_count": 500,
            "format_duration_seconds": "20.000000",
            "stream_start_seconds": "0.000000",
            "stream_duration_seconds": "20.000000",
        }

    def bootstrap(**kwargs):
        return real_bootstrap(**kwargs, probe=fake_probe)

    def fake_extract(_source: Path, output: Path, *, duration_us: int) -> None:
        calls["extract"] += 1
        _wav(output, duration_us=duration_us)

    def resolver(job, *, allowed_root):
        return real_resolver(
            job, allowed_root=allowed_root, extract_full_audio=fake_extract
        )

    monkeypatch.setattr(mechanical, "bootstrap_final_media_review_job", bootstrap)
    monkeypatch.setattr(mechanical, "resolve_or_materialize_review_job", resolver)

    with pytest.raises(
        ValueError, match="final media perceptual review unresolved"
    ):
        mechanical.build_mechanical_receipt(
            fixture["root"], fixture["audit_path"]
        )

    assert calls == {"probe": 1, "extract": 1}
    verification = fixture["root"] / "verification"
    bootstrap_path = (
        verification / f"{CANDIDATE_ID}.final-media-review-bootstrap.json"
    )
    job_path = verification / f"{CANDIDATE_ID}.final-media-review-job.json"
    state_path = verification / f"{CANDIDATE_ID}.final-media-review-state.json"
    assert bootstrap_path.is_file() and job_path.is_file() and state_path.is_file()
    bootstrap_receipt = json.loads(bootstrap_path.read_text(encoding="utf-8"))
    assert bootstrap_receipt["trigger_reason_codes"] == [
        "FINAL_MEDIA_REVIEW_LEGACY_DURATION_WITNESS_MISMATCH"
    ]
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["review_plan"]["duration_binding"] == {
        "final_duration_ms": 20_000,
        "final_duration_source": "burned_preview.verification.duration_ms",
        "current_final_duration_ms": 20_000,
        "legacy_branding_intro_duration_ms": 37_546,
        "duration_witness_mismatch_ms": 17_546,
        "duration_witness_mismatch": True,
    }
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "BLOCKED_INPUT"
    assert state["reason_codes"] == [
        "FINAL_MEDIA_REVIEW_AUDIO_TRANSPORT_MISSING",
        "FINAL_MEDIA_REVIEW_VISUAL_INPUT_NOT_CONTINUOUS",
    ]
    assert state["assessment"]["audio"]["actual_gaps"] == []
    assert state["assessment"]["content_review_status"] == "UNASSESSED"

    with pytest.raises(
        ValueError, match="final media perceptual review unresolved"
    ):
        mechanical.build_mechanical_receipt(
            fixture["root"], fixture["audit_path"]
        )
    assert calls == {"probe": 1, "extract": 1}
    restarted = json.loads(state_path.read_text(encoding="utf-8"))
    assert restarted["state_sha256"] == state["state_sha256"]

def test_manifest_closure_prefers_current_burn_duration_and_discloses_stale_intro_witness(
    receipt_package: dict[str, object],
) -> None:
    record_path = receipt_package["paths"]["record"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["burned_preview"]["verification"] = {"duration_ms": 20_000}
    record["burned_preview"]["branding_intro"]["verification"] = {
        "duration_ms": 37_546
    }
    _write_json(record_path, record)

    order, closures = human_review._manifest_items(  # noqa: SLF001
        receipt_package["manifest"],
        package_root=receipt_package["root"],
    )

    assert order == [CANDIDATE_ID]
    closure = closures[CANDIDATE_ID]
    assert closure["final_duration_ms"] == 20_000
    assert closure["final_duration_source"] == (
        "burned_preview.verification.duration_ms"
    )
    assert closure["current_final_duration_ms"] == 20_000
    assert closure["legacy_branding_intro_duration_ms"] == 37_546
    assert closure["duration_witness_mismatch_ms"] == 17_546
    assert closure["duration_witness_mismatch"] is True


def test_manifest_closure_keeps_legacy_duration_only_as_compatibility_fallback(
    receipt_package: dict[str, object],
) -> None:
    order, closures = human_review._manifest_items(  # noqa: SLF001
        receipt_package["manifest"],
        package_root=receipt_package["root"],
    )

    assert order == [CANDIDATE_ID]
    closure = closures[CANDIDATE_ID]
    assert closure["final_duration_ms"] == 20_000
    assert closure["final_duration_source"] == (
        "burned_preview.branding_intro.verification.duration_ms:legacy_fallback"
    )
    assert closure["current_final_duration_ms"] is None
    assert closure["legacy_branding_intro_duration_ms"] == 20_000
    assert closure["duration_witness_mismatch_ms"] is None
    assert closure["duration_witness_mismatch"] is False



def test_saved_mechanical_receipt_replays_without_running_auditor(
    mechanical_package, monkeypatch: pytest.MonkeyPatch
):
    fixture, mechanical = mechanical_package
    receipt = mechanical.build_mechanical_receipt(
        fixture["root"], fixture["audit_path"]
    )

    monkeypatch.setattr(
        mechanical,
        "audit_package",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("saved replay must not run the canonical auditor")
        ),
    )

    assert mechanical.validate_saved_mechanical_receipt(
        receipt, fixture["root"]
    ) == receipt


def test_saved_mechanical_receipt_rejects_current_artifact_drift(
    mechanical_package,
):
    fixture, mechanical = mechanical_package
    receipt = mechanical.build_mechanical_receipt(
        fixture["root"], fixture["audit_path"]
    )
    fixture["paths"]["video"].write_bytes(
        fixture["paths"]["video"].read_bytes() + b"drift"
    )

    with pytest.raises(ValueError, match="binding drift|burn output binding"):
        mechanical.validate_saved_mechanical_receipt(receipt, fixture["root"])


def test_saved_mechanical_receipt_rejects_later_incomplete_final_media_state(
    mechanical_package,
):
    fixture, mechanical = mechanical_package
    receipt = mechanical.build_mechanical_receipt(
        fixture["root"], fixture["audit_path"]
    )
    _write_final_media_review_state(fixture, status="BLOCKED_INPUT")

    with pytest.raises(ValueError, match="sidecars are incomplete"):
        mechanical.validate_saved_mechanical_receipt(receipt, fixture["root"])


@pytest.mark.parametrize(
    ("bootstrap_status", "provider_calls"),
    [("RETRY_WAIT", 1), ("SENTINEL_RUNTIME_ABSENT", 0)],
)
def test_mechanical_stops_before_package_state_when_capability_bootstrap_is_not_ready(
    mechanical_package,
    monkeypatch: pytest.MonkeyPatch,
    bootstrap_status: str,
    provider_calls: int,
):
    fixture, mechanical = mechanical_package
    video = fixture["paths"]["video"]
    verification = fixture["root"] / "verification"
    audio = verification / "bootstrap-wait-full-final-media.wav"
    _wav(audio, duration_us=20_000_000)
    manifest = {
        "schema": "synthetic-review-assets.v1",
        "source_video": {
            "path": str(video.resolve()),
            "bytes": video.stat().st_size,
            "sha256": _sha256(video),
        },
        "artifacts": [
            {
                "path": str(audio.resolve()),
                "bytes": audio.stat().st_size,
                "sha256": _sha256(audio),
                "kind": "exact_full_audio_wav",
                "window": "full-final-media",
                "range_seconds": [0.0, 20.0],
                "actual_duration_us": 20_000_000,
                "sample_rate": 16_000,
                "sample_frames": 320_000,
                "source_video_sha256": _sha256(video),
            }
        ],
    }
    manifest_path = verification / "bootstrap-wait-review-assets.json"
    _write_json(manifest_path, manifest)
    job_path = verification / f"{CANDIDATE_ID}.final-media-review-job.json"
    _write_json(
        job_path,
        {
            "schema_version": JOB_SCHEMA_VERSION,
            "candidate_id": CANDIDATE_ID,
            "asset_manifest": {
                "path": str(manifest_path.resolve()),
                "sha256": _sha256(manifest_path),
            },
            "media_clock": {
                "source_video_sha256": _sha256(video),
                "source_video_bytes": video.stat().st_size,
                "duration_us": 20_000_000,
            },
            "requirements": {
                "continuous_audio_required": True,
                "continuous_visual_required": True,
                "content_review_required": True,
                "exact_media_clock_evidence_required": False,
                "provider_accepts_bound_source_video": False,
                "provider_accepts_bound_audio": False,
            },
            "review_plan": {
                "authority": "SYNTHETIC_BOOTSTRAP_WAIT_TEST_ONLY",
                "review_points": [
                    {
                        "point_id": "whole-final-media",
                        "final_video_start_ms": 0,
                        "final_video_end_ms": 20_000,
                        "expectation": "inspect exact final audio and video",
                    }
                ],
            },
        },
    )
    runtime = fixture["root"] / "bootstrap-wait-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", str(runtime))
    monkeypatch.setattr(
        mechanical,
        "ensure_runtime_raw_av_capability",
        lambda _root: {
            "status": bootstrap_status,
            "provider_calls": provider_calls,
            "reason_code": "SYNTHETIC_BOOTSTRAP_STATUS",
        },
    )

    def unexpected_bind(*_args, **_kwargs):
        raise AssertionError("mechanical review must wait before package binding")

    monkeypatch.setattr(
        mechanical, "bind_review_job_to_runtime_capability", unexpected_bind
    )
    state_path = verification / f"{CANDIDATE_ID}.final-media-review-state.json"

    with pytest.raises(ValueError, match="final media review job invalid"):
        mechanical.build_mechanical_receipt(
            fixture["root"], fixture["audit_path"]
        )

    assert not state_path.exists()
    assert not (
        verification / "final-media-review-capabilities"
    ).exists()
