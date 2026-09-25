"""Hash-bound planning for a supplemental CPA visual batch."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path


PLAN_SCHEMA = "cpa-visual-batch-resume-plan.v1"
PLAN_STATUS = "READY_FOR_SUPPLEMENTAL_VISUAL_REVIEW"
_SUCCESS = frozenset({"OBSERVED", "REUSED_EXACT_RECEIPT"})
_RETRYABLE_REASONS = frozenset({"VISION_CALL_FAILED", "VISION_PROVIDER_CAPACITY"})


class CpaVisualBatchResumeError(ValueError):
    """The batch sources, plan, image bytes, or supplemental outputs are unsafe."""


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _normal_hash(value: object, *, code: str) -> str:
    if not isinstance(value, str):
        raise CpaVisualBatchResumeError(code)
    normalized = value.removeprefix("sha256:").lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise CpaVisualBatchResumeError(code)
    return normalized


def _read_regular(path: str | Path, *, code: str) -> tuple[Path, bytes]:
    candidate = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (candidate, *candidate.parents)):
        raise CpaVisualBatchResumeError(code)
    try:
        resolved = candidate.resolve(strict=True)
        before = resolved.stat()
        raw = resolved.read_bytes()
        after = resolved.stat()
    except OSError as exc:
        raise CpaVisualBatchResumeError(code) from exc
    if not resolved.is_file() or before.st_nlink != 1:
        raise CpaVisualBatchResumeError(code)
    if (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_ino,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_ino,
    ) or len(raw) != before.st_size:
        raise CpaVisualBatchResumeError(code)
    return resolved, raw


def _load_bound_json(
    path: str | Path,
    *,
    expected_sha256: str,
    unavailable_code: str,
    drift_code: str,
    invalid_code: str,
) -> tuple[Path, bytes, dict[str, object]]:
    resolved, raw = _read_regular(path, code=unavailable_code)
    if hashlib.sha256(raw).hexdigest() != _normal_hash(expected_sha256, code=drift_code):
        raise CpaVisualBatchResumeError(drift_code)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CpaVisualBatchResumeError(invalid_code) from exc
    if not isinstance(value, dict):
        raise CpaVisualBatchResumeError(invalid_code)
    return resolved, raw, value


def _source_descriptor(path: Path, raw: bytes) -> dict[str, object]:
    if path.name in {"", ".", ".."} or Path(path.name).name != path.name:
        raise CpaVisualBatchResumeError("VISUAL_RESUME_SOURCE_NAME_INVALID")
    return {
        "file_name": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def _entry_map(manifest: Mapping[str, object]) -> dict[int, dict[str, object]]:
    if not (
        manifest.get("provider") == "cpa"
        and isinstance(manifest.get("model"), str)
        and bool(manifest.get("model"))
        and manifest.get("allow_fallback") is False
        and manifest.get("agy_allowed") is False
        and manifest.get("audio_heard_by_this_batch") is False
        and isinstance(manifest.get("candidate_id"), str)
        and bool(manifest.get("candidate_id"))
    ):
        raise CpaVisualBatchResumeError("VISUAL_RESUME_MANIFEST_POLICY_INVALID")
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise CpaVisualBatchResumeError("VISUAL_RESUME_MANIFEST_ENTRIES_INVALID")
    entries: dict[int, dict[str, object]] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            raise CpaVisualBatchResumeError("VISUAL_RESUME_MANIFEST_ENTRY_INVALID")
        entry = dict(raw_entry)
        index = entry.get("sheet_index")
        image_name = entry.get("image_name")
        image_bytes = entry.get("image_bytes")
        cue_range = entry.get("cue_range")
        cues = entry.get("cues")
        if not (
            type(index) is int
            and index >= 0
            and index not in entries
            and isinstance(image_name, str)
            and image_name
            and Path(image_name).name == image_name
            and type(image_bytes) is int
            and image_bytes > 0
            and isinstance(cue_range, list)
            and len(cue_range) == 2
            and all(type(value) is int for value in cue_range)
            and cue_range[0] <= cue_range[1]
            and isinstance(cues, list)
            and cues
            and all(isinstance(cue, Mapping) for cue in cues)
        ):
            raise CpaVisualBatchResumeError("VISUAL_RESUME_MANIFEST_ENTRY_INVALID")
        entry["image_sha256"] = _normal_hash(
            entry.get("image_sha256"), code="VISUAL_RESUME_IMAGE_HASH_INVALID"
        )
        entries[index] = entry
    if sorted(entries) != list(range(len(entries))):
        raise CpaVisualBatchResumeError("VISUAL_RESUME_MANIFEST_INDEX_SET_INVALID")
    return entries


def _prior_result_map(
    prior: Mapping[str, object],
    *,
    manifest: Mapping[str, object],
    manifest_sha256: str,
    entries: Mapping[int, Mapping[str, object]],
) -> dict[int, dict[str, object]]:
    if not (
        prior.get("candidate_id") == manifest.get("candidate_id")
        and prior.get("provider") == "cpa"
        and prior.get("model") == manifest.get("model")
        and prior.get("allow_fallback") is False
        and prior.get("agy_used") is False
        and prior.get("complete") is False
        and _normal_hash(
            prior.get("manifest_sha256"), code="VISUAL_RESUME_PRIOR_MANIFEST_HASH_INVALID"
        )
        == manifest_sha256
    ):
        raise CpaVisualBatchResumeError("VISUAL_RESUME_PRIOR_BATCH_BINDING_INVALID")
    raw_results = prior.get("results")
    if not isinstance(raw_results, list):
        raise CpaVisualBatchResumeError("VISUAL_RESUME_PRIOR_RESULTS_INVALID")
    results: dict[int, dict[str, object]] = {}
    for raw_result in raw_results:
        if not isinstance(raw_result, Mapping):
            raise CpaVisualBatchResumeError("VISUAL_RESUME_PRIOR_RESULT_INVALID")
        result = dict(raw_result)
        index = result.get("sheet_index")
        status = result.get("status")
        if type(index) is not int or index not in entries or index in results:
            raise CpaVisualBatchResumeError("VISUAL_RESUME_PRIOR_RESULT_INVALID")
        _normal_hash(result.get("receipt_sha256"), code="VISUAL_RESUME_RECEIPT_HASH_INVALID")
        if status in _SUCCESS:
            _normal_hash(
                result.get("response_sha256"), code="VISUAL_RESUME_RESPONSE_HASH_INVALID"
            )
        elif status == "UNAVAILABLE":
            if result.get("reason_code") not in _RETRYABLE_REASONS:
                raise CpaVisualBatchResumeError("VISUAL_RESUME_FAILURE_NOT_RETRYABLE")
        else:
            raise CpaVisualBatchResumeError("VISUAL_RESUME_PRIOR_STATUS_INVALID")
        results[index] = result
    return results


def _question(entry: Mapping[str, object]) -> str:
    cue_lines = "\n".join(
        "cue {cue}: delivery {start}–{end} ms, current subtitle={text}".format(
            cue=cue["cue"],
            start=cue["delivery_start_ms"],
            end=cue["delivery_end_ms"],
            text=json.dumps(cue["current_text"], ensure_ascii=False),
        )
        for cue in entry["cues"]
    )
    cue_range = entry["cue_range"]
    return f"""你是直播切片的像素证据审核员。输入是原始未烧主播字幕的视频密帧 contact sheet，覆盖 cue {cue_range[0]}–{cue_range[1]}。每个 panel 已标出实际时间；必须直接看图，不调用OCR工具，不依据旧分类自证。

待核 cue：
{cue_lines}

区分：画面中央/左侧“被观看视频”的内嵌字幕，和外层直播聊天、歌单、UI、主播虚拟形象。逐 cue 检查整个 sheet 中其开始、中间、结束及相邻帧：
1. 精确抄录能看清的被观看视频内嵌字幕，并列出对应 panel 时间；看不清就写 unknown，不猜字。
2. 说明被观看视频是否正在播放、切换、黑屏；主播头像可见不证明声音来自主播。
3. 仅由像素给 visual_verdict：source_media_direct（同时间内嵌字幕完整或足够直接匹配当前 cue）、overlap_possible（只覆盖 cue 一部分/可能跟读）、visually_unresolved。禁止因无内嵌字幕而给 host。
4. 标出此前4秒采样可能漏掉的新字幕证据。
5. 只输出一个 JSON 对象，键严格为 sheet_index、cue_results、new_dense_evidence、limitations、confidence。cue_results 每项必须含 cue、visible_embedded_caption_exact、panel_times_sec、scene_state、visual_verdict、reason。不要输出 markdown。"""


def build_visual_batch_resume_plan(
    *,
    manifest_path: str | Path,
    manifest_sha256: str,
    prior_batch_path: str | Path,
    prior_batch_sha256: str,
    requested_sheet_indices: Sequence[int],
) -> dict[str, object]:
    """Freeze every outstanding sheet; successful prior sheets are ineligible."""

    manifest_path, manifest_raw, manifest = _load_bound_json(
        manifest_path,
        expected_sha256=manifest_sha256,
        unavailable_code="VISUAL_RESUME_MANIFEST_UNAVAILABLE",
        drift_code="VISUAL_RESUME_MANIFEST_HASH_DRIFT",
        invalid_code="VISUAL_RESUME_MANIFEST_INVALID",
    )
    prior_path, prior_raw, prior = _load_bound_json(
        prior_batch_path,
        expected_sha256=prior_batch_sha256,
        unavailable_code="VISUAL_RESUME_PRIOR_BATCH_UNAVAILABLE",
        drift_code="VISUAL_RESUME_PRIOR_BATCH_HASH_DRIFT",
        invalid_code="VISUAL_RESUME_PRIOR_BATCH_INVALID",
    )
    observed_manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    entries = _entry_map(manifest)
    results = _prior_result_map(
        prior,
        manifest=manifest,
        manifest_sha256=observed_manifest_sha,
        entries=entries,
    )
    successes = sorted(index for index, row in results.items() if row["status"] in _SUCCESS)
    failed = sorted(index for index, row in results.items() if row["status"] == "UNAVAILABLE")
    unattempted = sorted(set(entries) - set(results))
    outstanding = set(failed) | set(unattempted)
    requested = list(requested_sheet_indices)
    if (
        not requested
        or any(type(index) is not int for index in requested)
        or len(set(requested)) != len(requested)
        or set(requested) != outstanding
    ):
        raise CpaVisualBatchResumeError("VISUAL_RESUME_MUST_COVER_ALL_OUTSTANDING")
    ordered = [*unattempted, *failed]
    tasks: list[dict[str, object]] = []
    for index in ordered:
        entry = entries[index]
        question = _question(entry)
        task: dict[str, object] = {
            "sheet_index": index,
            "attempt_class": "UNATTEMPTED" if index in unattempted else "FAILED_RETRY",
            "image_name": entry["image_name"],
            "image_sha256": entry["image_sha256"],
            "image_bytes": entry["image_bytes"],
            "cue_range": list(entry["cue_range"]),
            "cues": [dict(cue) for cue in entry["cues"]],
            "entry_sha256": _canonical_sha256(entry),
            "question": question,
            "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        }
        if index in failed:
            task["prior_failure"] = results[index]
        tasks.append(task)
    body: dict[str, object] = {
        "schema_version": PLAN_SCHEMA,
        "status": PLAN_STATUS,
        "candidate_id": manifest["candidate_id"],
        "provider": "cpa",
        "model": manifest["model"],
        "allow_fallback": False,
        "agy_used": False,
        "source_manifest": _source_descriptor(manifest_path, manifest_raw),
        "prior_batch": _source_descriptor(prior_path, prior_raw),
        "manifest_entry_count": len(entries),
        "immutable_success_sheet_indices": successes,
        "failed_retry_sheet_indices": failed,
        "unattempted_sheet_indices": unattempted,
        "tasks": tasks,
        "policy": {
            "all_outstanding_required": True,
            "task_order": "UNATTEMPTED_THEN_FAILED_RETRY",
            "stop_on_task_failure": False,
            "one_probe_invocation_per_task": True,
            "receipt_namespace": "CREATE_ONLY_SUPPLEMENTAL",
            "prior_receipts_overwritten": False,
        },
        "planned_probe_invocations": len(tasks),
        "mutation_authorized": False,
        "publication_authority": False,
    }
    return {**body, "plan_sha256": _canonical_sha256(body)}


def validate_visual_batch_resume_plan(
    plan: object,
    *,
    evidence_root: str | Path,
) -> dict[str, object]:
    """Rebuild the plan from the exact packaged manifest and prior batch."""

    if not isinstance(plan, Mapping):
        raise CpaVisualBatchResumeError("VISUAL_RESUME_PLAN_INVALID")
    value = dict(plan)
    body = dict(value)
    declared = body.pop("plan_sha256", None)
    if declared != _canonical_sha256(body):
        raise CpaVisualBatchResumeError("VISUAL_RESUME_PLAN_DIGEST_MISMATCH")
    if not (
        body.get("schema_version") == PLAN_SCHEMA
        and body.get("status") == PLAN_STATUS
        and body.get("allow_fallback") is False
        and body.get("agy_used") is False
        and body.get("mutation_authorized") is False
        and body.get("publication_authority") is False
    ):
        raise CpaVisualBatchResumeError("VISUAL_RESUME_PLAN_INVALID")
    root = Path(evidence_root).expanduser().absolute()
    if any(part.is_symlink() for part in (root, *root.parents)) or not root.is_dir():
        raise CpaVisualBatchResumeError("VISUAL_RESUME_EVIDENCE_ROOT_UNSAFE")
    manifest = body.get("source_manifest")
    prior = body.get("prior_batch")
    tasks = body.get("tasks")
    if not isinstance(manifest, Mapping) or not isinstance(prior, Mapping) or not isinstance(tasks, list):
        raise CpaVisualBatchResumeError("VISUAL_RESUME_PLAN_INVALID")
    requested = [task.get("sheet_index") for task in tasks if isinstance(task, Mapping)]
    expected = build_visual_batch_resume_plan(
        manifest_path=root / str(manifest.get("file_name") or ""),
        manifest_sha256=str(manifest.get("sha256") or ""),
        prior_batch_path=root / str(prior.get("file_name") or ""),
        prior_batch_sha256=str(prior.get("sha256") or ""),
        requested_sheet_indices=requested,
    )
    if expected != value:
        raise CpaVisualBatchResumeError("VISUAL_RESUME_PLAN_RECOMPUTE_MISMATCH")
    return expected
