"""Create-only execution for a planned supplemental CPA visual batch."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path

from src.autoslice.cpa_visual_batch_resume_plan import (
    CpaVisualBatchResumeError,
    _canonical_sha256,
    _normal_hash,
    _read_regular,
    build_visual_batch_resume_plan,
    validate_visual_batch_resume_plan,
)


RECEIPT_SCHEMA = "cpa-visual-batch-resume-receipt.v1"
RESULT_SCHEMA = "cpa-visual-batch-resume-result.v1"
RESULT_COMPLETE = "COMPLETE_VISUAL_BATCH"
RESULT_PARTIAL = "PARTIAL_VISUAL_BATCH"

__all__ = [
    "CpaVisualBatchResumeError",
    "build_visual_batch_resume_plan",
    "execute_visual_batch_resume",
    "validate_visual_batch_resume_plan",
    "write_create_only_json",
]


def _read_task_images(
    plan: Mapping[str, object], *, image_root: str | Path
) -> dict[int, Path]:
    root = Path(image_root).expanduser().absolute()
    if any(part.is_symlink() for part in (root, *root.parents)) or not root.is_dir():
        raise CpaVisualBatchResumeError("VISUAL_RESUME_IMAGE_ROOT_UNSAFE")
    paths: dict[int, Path] = {}
    for task in plan["tasks"]:
        if not isinstance(task, Mapping):
            raise CpaVisualBatchResumeError("VISUAL_RESUME_PLAN_INVALID")
        index = int(task["sheet_index"])
        path, raw = _read_regular(
            root / str(task["image_name"]), code="VISUAL_RESUME_IMAGE_UNAVAILABLE"
        )
        if hashlib.sha256(raw).hexdigest() != task["image_sha256"]:
            raise CpaVisualBatchResumeError("VISUAL_RESUME_IMAGE_HASH_DRIFT")
        if len(raw) != task["image_bytes"]:
            raise CpaVisualBatchResumeError("VISUAL_RESUME_IMAGE_SIZE_DRIFT")
        paths[index] = path
    return paths


def write_create_only_json(path: str | Path, value: Mapping[str, object]) -> Path:
    """Persist one owner-only JSON artifact without following symlinks."""

    target = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (target, *target.parents)):
        raise CpaVisualBatchResumeError("VISUAL_RESUME_OUTPUT_UNSAFE")
    target.parent.mkdir(parents=True, exist_ok=True)
    target = target.parent.resolve(strict=True) / target.name
    raw = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise CpaVisualBatchResumeError("VISUAL_RESUME_OUTPUT_EXISTS") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return target.resolve(strict=True)


def _valid_observed_probe(
    receipt: Mapping[str, object], *, task: Mapping[str, object], model: str
) -> bool:
    if not (
        receipt.get("status") == "OBSERVED"
        and receipt.get("provider") == "cpa"
        and receipt.get("model") == model
        and receipt.get("image_sha256") == task["image_sha256"]
        and isinstance(receipt.get("answer"), str)
        and bool(str(receipt.get("answer")).strip())
    ):
        return False
    try:
        _normal_hash(
            receipt.get("response_sha256"),
            code="VISUAL_RESUME_RESPONSE_HASH_INVALID",
        )
    except CpaVisualBatchResumeError:
        return False
    return True


def execute_visual_batch_resume(
    *,
    plan: Mapping[str, object],
    evidence_root: str | Path,
    image_root: str | Path,
    receipt_root: str | Path,
    probe: Callable[..., Mapping[str, object]],
) -> dict[str, object]:
    """Invoke exactly one probe per task and continue after task-level failures."""

    frozen = validate_visual_batch_resume_plan(plan, evidence_root=evidence_root)
    images = _read_task_images(frozen, image_root=image_root)
    receipt_dir = Path(receipt_root).expanduser().absolute()
    if any(part.is_symlink() for part in (receipt_dir, *receipt_dir.parents)):
        raise CpaVisualBatchResumeError("VISUAL_RESUME_RECEIPT_ROOT_UNSAFE")
    try:
        receipt_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError as exc:
        raise CpaVisualBatchResumeError("VISUAL_RESUME_RECEIPT_ROOT_EXISTS") from exc
    rows: list[dict[str, object]] = []
    for task in frozen["tasks"]:
        index = int(task["sheet_index"])
        try:
            raw_probe = probe(
                images[index],
                str(task["question"]),
                model=str(frozen["model"]),
            )
            probe_receipt = dict(raw_probe) if isinstance(raw_probe, Mapping) else {}
        except Exception as exc:  # task isolation is the point of this runner
            probe_receipt = {
                "status": "UNAVAILABLE",
                "reason_code": "VISUAL_PROBE_EXCEPTION",
                "error_type": type(exc).__name__,
                "provider": "cpa",
                "model": frozen["model"],
            }
        raw_observed = probe_receipt.get("status") == "OBSERVED"
        observed = _valid_observed_probe(
            probe_receipt, task=task, model=str(frozen["model"])
        )
        if raw_observed and not observed:
            probe_receipt = {
                "status": "UNAVAILABLE",
                "reason_code": "VISUAL_PROBE_RECEIPT_INVALID",
                "provider": "cpa",
                "model": frozen["model"],
                "raw_status": probe_receipt.get("status"),
            }
        receipt_body: dict[str, object] = {
            "schema_version": RECEIPT_SCHEMA,
            "status": "OBSERVED" if observed else "UNAVAILABLE",
            "candidate_id": frozen["candidate_id"],
            "sheet_index": index,
            "attempt_class": task["attempt_class"],
            "review_plan_sha256": frozen["plan_sha256"],
            "image_name": task["image_name"],
            "image_sha256": task["image_sha256"],
            "image_bytes": task["image_bytes"],
            "question_sha256": task["question_sha256"],
            "provider": "cpa",
            "model": frozen["model"],
            "allow_fallback": False,
            "agy_used": False,
            "probe_receipt": probe_receipt,
            "mutation_authorized": False,
            "publication_authority": False,
        }
        receipt = {**receipt_body, "receipt_sha256": _canonical_sha256(receipt_body)}
        receipt_path = write_create_only_json(
            receipt_dir / f"sheet-{index:02d}.supplemental.json", receipt
        )
        rows.append(
            {
                "sheet_index": index,
                "status": receipt["status"],
                "reason_code": probe_receipt.get("reason_code"),
                "receipt_path": str(receipt_path),
                "receipt_file_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                "response_sha256": probe_receipt.get("response_sha256"),
            }
        )
    observed_new = [row["sheet_index"] for row in rows if row["status"] == "OBSERVED"]
    complete = (
        len(frozen["immutable_success_sheet_indices"]) + len(observed_new)
        == frozen["manifest_entry_count"]
    )
    body = {
        "schema_version": RESULT_SCHEMA,
        "status": RESULT_COMPLETE if complete else RESULT_PARTIAL,
        "complete": complete,
        "candidate_id": frozen["candidate_id"],
        "review_plan_sha256": frozen["plan_sha256"],
        "provider": "cpa",
        "model": frozen["model"],
        "allow_fallback": False,
        "agy_used": False,
        "prior_success_sheet_indices": frozen["immutable_success_sheet_indices"],
        "supplemental_results": rows,
        "provider_probe_invocations": len(rows),
        "stop_on_task_failure": False,
        "mutation_authorized": False,
        "publication_authority": False,
    }
    return {**body, "result_sha256": _canonical_sha256(body)}
