from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import cpa_visual_batch_resume as cli
from src.autoslice.cpa_visual_batch_resume import (
    CpaVisualBatchResumeError,
    build_visual_batch_resume_plan,
    execute_visual_batch_resume,
    validate_visual_batch_resume_plan,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry(root: Path, index: int) -> dict[str, object]:
    image = root / f"sheet-{index:02d}.jpg"
    image.write_bytes(f"image-{index}".encode())
    return {
        "sheet_index": index,
        "image_name": image.name,
        "image_sha256": _sha(image),
        "image_bytes": image.stat().st_size,
        "cue_range": [index + 1, index + 1],
        "cues": [
            {
                "cue": index + 1,
                "delivery_start_ms": index * 1_000,
                "delivery_end_ms": (index + 1) * 1_000,
                "current_text": f"cue {index + 1}",
            }
        ],
    }


def _sources(tmp_path: Path) -> tuple[Path, Path, Path]:
    package = tmp_path / "evidence"
    images = tmp_path / "images"
    package.mkdir()
    images.mkdir()
    entries = [_entry(images, index) for index in range(3)]
    manifest = package / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "synthetic-visual-batch.v1",
                "candidate_id": "candidate",
                "provider": "cpa",
                "model": "gpt-6-sol",
                "allow_fallback": False,
                "agy_allowed": False,
                "audio_heard_by_this_batch": False,
                "entries": entries,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    prior = package / "prior.json"
    prior.write_text(
        json.dumps(
            {
                "schema_version": "synthetic-visual-batch-result.v1",
                "candidate_id": "candidate",
                "provider": "cpa",
                "model": "gpt-6-sol",
                "allow_fallback": False,
                "agy_used": False,
                "manifest_sha256": _sha(manifest),
                "complete": False,
                "results": [
                    {
                        "sheet_index": 0,
                        "status": "OBSERVED",
                        "receipt_sha256": "0" * 64,
                        "response_sha256": "1" * 64,
                        "reason_code": None,
                    },
                    {
                        "sheet_index": 1,
                        "status": "UNAVAILABLE",
                        "receipt_sha256": "2" * 64,
                        "response_sha256": None,
                        "reason_code": "VISION_CALL_FAILED",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return package, images, manifest


def _plan(tmp_path: Path) -> tuple[dict[str, object], Path, Path]:
    package, images, manifest = _sources(tmp_path)
    prior = package / "prior.json"
    plan = build_visual_batch_resume_plan(
        manifest_path=manifest,
        manifest_sha256=_sha(manifest),
        prior_batch_path=prior,
        prior_batch_sha256=_sha(prior),
        requested_sheet_indices=[1, 2],
    )
    return plan, package, images


def _observed(image_path: Path, _question: str, *, model: str) -> dict[str, object]:
    return {
        "status": "OBSERVED",
        "provider": "cpa",
        "model": model,
        "image_sha256": _sha(image_path),
        "answer": '{"cue_results":[]}',
        "response_sha256": "a" * 64,
    }


def test_plan_schedules_unattempted_before_failed_retry(tmp_path: Path) -> None:
    plan, package, _images = _plan(tmp_path)
    assert [task["sheet_index"] for task in plan["tasks"]] == [2, 1]
    assert [task["attempt_class"] for task in plan["tasks"]] == [
        "UNATTEMPTED",
        "FAILED_RETRY",
    ]
    assert plan["immutable_success_sheet_indices"] == [0]
    assert plan["planned_probe_invocations"] == 2
    assert plan["allow_fallback"] is False
    assert plan["agy_used"] is False
    assert validate_visual_batch_resume_plan(plan, evidence_root=package) == plan


def test_plan_must_cover_every_outstanding_sheet(tmp_path: Path) -> None:
    package, _images, manifest = _sources(tmp_path)
    prior = package / "prior.json"
    with pytest.raises(
        CpaVisualBatchResumeError, match="VISUAL_RESUME_MUST_COVER_ALL_OUTSTANDING"
    ):
        build_visual_batch_resume_plan(
            manifest_path=manifest,
            manifest_sha256=_sha(manifest),
            prior_batch_path=prior,
            prior_batch_sha256=_sha(prior),
            requested_sheet_indices=[1],
        )


def test_observed_sheet_cannot_be_requested(tmp_path: Path) -> None:
    package, _images, manifest = _sources(tmp_path)
    prior = package / "prior.json"
    with pytest.raises(
        CpaVisualBatchResumeError, match="VISUAL_RESUME_MUST_COVER_ALL_OUTSTANDING"
    ):
        build_visual_batch_resume_plan(
            manifest_path=manifest,
            manifest_sha256=_sha(manifest),
            prior_batch_path=prior,
            prior_batch_sha256=_sha(prior),
            requested_sheet_indices=[0, 1, 2],
        )


def test_nonretryable_prior_failure_is_rejected(tmp_path: Path) -> None:
    package, _images, manifest = _sources(tmp_path)
    prior = package / "prior.json"
    payload = json.loads(prior.read_text())
    payload["results"][1]["reason_code"] = "PERMISSION_DENIED"
    prior.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        CpaVisualBatchResumeError, match="VISUAL_RESUME_FAILURE_NOT_RETRYABLE"
    ):
        build_visual_batch_resume_plan(
            manifest_path=manifest,
            manifest_sha256=_sha(manifest),
            prior_batch_path=prior,
            prior_batch_sha256=_sha(prior),
            requested_sheet_indices=[1, 2],
        )


def test_tampered_plan_fails_even_after_self_rehash(tmp_path: Path) -> None:
    plan, package, _images = _plan(tmp_path)
    tampered = copy.deepcopy(plan)
    tampered["tasks"][0]["question"] += " tampered"
    body = dict(tampered)
    body.pop("plan_sha256")
    tampered["plan_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with pytest.raises(
        CpaVisualBatchResumeError, match="VISUAL_RESUME_PLAN_RECOMPUTE_MISMATCH"
    ):
        validate_visual_batch_resume_plan(tampered, evidence_root=package)


def test_task_failure_does_not_starve_later_retry(tmp_path: Path) -> None:
    plan, package, images = _plan(tmp_path)
    calls: list[int] = []

    def probe(image_path: Path, question: str, *, model: str):
        index = int(image_path.stem.split("-")[-1])
        calls.append(index)
        if index == 2:
            return {
                "status": "UNAVAILABLE",
                "reason_code": "VISION_CALL_FAILED",
                "provider": "cpa",
                "model": model,
            }
        return _observed(image_path, question, model=model)

    receipts = tmp_path / "receipts"
    result = execute_visual_batch_resume(
        plan=plan,
        evidence_root=package,
        image_root=images,
        receipt_root=receipts,
        probe=probe,
    )
    assert calls == [2, 1]
    assert result["complete"] is False
    assert [row["status"] for row in result["supplemental_results"]] == [
        "UNAVAILABLE",
        "OBSERVED",
    ]
    assert (receipts / "sheet-02.supplemental.json").is_file()
    assert (receipts / "sheet-01.supplemental.json").is_file()
    assert result["mutation_authorized"] is False


def test_invalid_observed_receipt_does_not_starve_later_retry(tmp_path: Path) -> None:
    plan, package, images = _plan(tmp_path)
    calls: list[int] = []

    def probe(image_path: Path, question: str, *, model: str):
        index = int(image_path.stem.split("-")[-1])
        calls.append(index)
        if index == 2:
            return {
                "status": "OBSERVED",
                "provider": "cpa",
                "model": model,
                "image_sha256": _sha(image_path),
                "answer": "{}",
                # Missing response_sha256: this task is invalid, not fatal to the batch.
            }
        return _observed(image_path, question, model=model)

    result = execute_visual_batch_resume(
        plan=plan,
        evidence_root=package,
        image_root=images,
        receipt_root=tmp_path / "receipts",
        probe=probe,
    )
    assert calls == [2, 1]
    assert [row["status"] for row in result["supplemental_results"]] == [
        "UNAVAILABLE",
        "OBSERVED",
    ]
    assert result["supplemental_results"][0]["reason_code"] == (
        "VISUAL_PROBE_RECEIPT_INVALID"
    )


def test_complete_supplemental_batch_stays_evidence_only(tmp_path: Path) -> None:
    plan, package, images = _plan(tmp_path)
    result = execute_visual_batch_resume(
        plan=plan,
        evidence_root=package,
        image_root=images,
        receipt_root=tmp_path / "receipts",
        probe=_observed,
    )
    assert result["complete"] is True
    assert result["status"] == "COMPLETE_VISUAL_BATCH"
    assert result["prior_success_sheet_indices"] == [0]
    assert result["provider_probe_invocations"] == 2
    assert result["mutation_authorized"] is False
    assert result["publication_authority"] is False


def test_image_drift_stops_before_provider_calls(tmp_path: Path) -> None:
    plan, package, images = _plan(tmp_path)
    (images / "sheet-02.jpg").write_bytes(b"drift")
    calls = 0

    def probe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {}

    with pytest.raises(CpaVisualBatchResumeError, match="VISUAL_RESUME_IMAGE_HASH_DRIFT"):
        execute_visual_batch_resume(
            plan=plan,
            evidence_root=package,
            image_root=images,
            receipt_root=tmp_path / "receipts",
            probe=probe,
        )
    assert calls == 0
    assert not (tmp_path / "receipts").exists()


def test_receipt_root_is_create_only(tmp_path: Path) -> None:
    plan, package, images = _plan(tmp_path)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    with pytest.raises(CpaVisualBatchResumeError, match="VISUAL_RESUME_RECEIPT_ROOT_EXISTS"):
        execute_visual_batch_resume(
            plan=plan,
            evidence_root=package,
            image_root=images,
            receipt_root=receipts,
            probe=_observed,
        )


def test_cli_writes_portable_plan_create_only(tmp_path: Path) -> None:
    package, _images, manifest = _sources(tmp_path)
    prior = package / "prior.json"
    out = tmp_path / "plan.json"
    args = [
        "plan",
        "--manifest",
        str(manifest),
        "--manifest-sha256",
        _sha(manifest),
        "--prior-batch",
        str(prior),
        "--prior-batch-sha256",
        _sha(prior),
        "--sheet-index",
        "1",
        "--sheet-index",
        "2",
        "--out",
        str(out),
    ]
    assert cli.main(args) == 0
    value = json.loads(out.read_text())
    assert [task["sheet_index"] for task in value["tasks"]] == [2, 1]
    assert cli.main(args) == 2
