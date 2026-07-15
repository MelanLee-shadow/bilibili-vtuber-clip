"""Review-package evidence, decision, and marker records for shadow runs.

The CLI module re-exports these names for historical callers. This module is
side-effect free apart from the explicitly requested local evidence writes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .auto_review import (
    AutoReviewManifest,
    DecisionAction,
    JingtingProvenance,
    REQUIRED_PUBLISH_ARTIFACT_KEYS,
    auto_review_manifest_sha256,
    evaluate_required_evidence,
    is_publish_gate_satisfied,
    review_candidate,
)
from .channel_profile import load_channel_profile
from .content_evidence import analyze_content_evidence
from .review_evidence import to_candidate_review
from .style_profile import ManualStyleProfile, apply_style_profile
from .subtitle_rendering import _parse_srt

ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id


def _run_review_package(
    *,
    review_package: Path,
    output_dir: Path,
    no_upload: bool,
    analyze_content=analyze_content_evidence,
    apply_style=apply_style_profile,
) -> dict[str, object]:
    manifest_path = review_package / "review_manifest.json"
    manifest = _read_json(manifest_path)
    items = manifest.get("items") if isinstance(manifest, Mapping) else None
    if not isinstance(items, list):
        items = []

    profile = _default_lidousha_profile()
    records: list[dict[str, object]] = []
    counts = _empty_counts()
    room_id = manifest.get("room_id") if isinstance(manifest.get("room_id"), str) else None
    date = manifest.get("date") if isinstance(manifest.get("date"), str) else None
    shadow_run_id = output_dir.name

    for item in items:
        if not isinstance(item, Mapping):
            continue
        counts["candidates_evaluated"] += 1
        stem = str(item.get("stem") or item.get("candidate_id") or f"candidate-{counts['candidates_evaluated']}")
        title = str(item.get("title") or stem)
        duration_seconds = _float(item.get("duration_sec"), 60.0)
        jingting_done = _package_path_exists(review_package, item.get("jingting_done"))
        srt_path = _package_path(review_package, item.get("jingting_srt") or item.get("srt"))
        cues = _parse_srt(srt_path) if srt_path and srt_path.is_file() else []
        evidence = analyze_content(candidate_id=stem, cues=cues, title=title)
        counts["candidates_with_content_evidence"] += 1
        evidence = apply_style(evidence, profile, title=title, duration_seconds=duration_seconds)
        counts["candidates_with_style_profile_evidence"] += 1
        provenance = _read_jingting_provenance(review_package, item)
        review_required = _read_review_required(review_package, item)
        candidate = to_candidate_review(evidence, provenance, jingting_done=jingting_done, review_required=review_required)
        decision = review_candidate(candidate)

        evidence_path = output_dir / "evidence" / f"{stem}.evidence.json"
        evidence_path.write_text(json.dumps(evidence.to_manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        artifact_hashes = _artifact_hashes(review_package, item)
        shadow_paths = _shadow_item_paths(output_dir, stem)
        shadow_inputs = _build_shadow_inputs_record(
            review_package=review_package,
            output_dir=output_dir,
            item=item,
            stem=stem,
            title=title,
            room_id=room_id,
            date=date,
            shadow_run_id=shadow_run_id,
            artifact_hashes=artifact_hashes,
            review_required=review_required,
            provenance=provenance,
            no_upload=no_upload,
        )
        _write_json_file(shadow_paths["shadow"], shadow_inputs)
        resolved_decision, decision_data = _build_shadow_decision_record(
            review_package=review_package,
            item=item,
            candidate=candidate,
            decision=decision,
            provenance=provenance,
            artifact_hashes=artifact_hashes,
            stem=stem,
            title=title,
            room_id=room_id,
            date=date,
            shadow_run_id=shadow_run_id,
            jingting_done=jingting_done,
            review_required=review_required,
            evidence_path=evidence_path,
            shadow_path=shadow_paths["shadow"],
            output_dir=output_dir,
            no_upload=no_upload,
        )
        _increment_action_count(counts, resolved_decision.action.value)
        _write_json_file(shadow_paths["decision"], decision_data)
        marker_paths = _write_shadow_markers(
            output_dir=output_dir,
            stem=stem,
            decision=resolved_decision,
            decision_path=shadow_paths["decision"],
            shadow_path=shadow_paths["shadow"],
            room_id=room_id,
            date=date,
            shadow_run_id=shadow_run_id,
            no_upload=no_upload,
        )
        records.append(
            {
                "candidate_id": stem,
                "title": title,
                "decision_action": resolved_decision.action.value,
                "reason_codes": list(resolved_decision.reason_codes),
                "score": resolved_decision.score,
                "evidence_path": str(evidence_path),
                "decision_path": str(shadow_paths["decision"].relative_to(output_dir)),
                "shadow_inputs_path": str(shadow_paths["shadow"].relative_to(output_dir)),
                "marker_paths": marker_paths,
                "subtitle_source": "review_package_jingting_srt" if _package_path_exists(review_package, item.get("jingting_srt")) else "review_package_draft_srt",
                "subtitle_path": str(srt_path) if srt_path else None,
            }
        )

    summary = {
        "schema_version": "auto-review-shadow-summary.v1",
        "mode": "review_package",
        "input": {
            "review_package": str(review_package),
            "review_manifest_exists": manifest_path.is_file(),
            "room_id": room_id,
            "date": date,
        },
        "counts": counts,
        "records": records,
        "validations": {
            "no_upload": no_upload,
            "no_upload_or_free_deploy_performed": no_upload,
            "generated_real_evidence": counts["candidates_with_content_evidence"] == counts["candidates_evaluated"],
            "marker_refs_valid": all(_record_paths_exist(output_dir, record) for record in records),
        },
        "gap_summary": _gap_summary(records),
    }
    return summary

def _default_lidousha_profile() -> ManualStyleProfile:
    return ManualStyleProfile(
        profile_id=f"{PROFILE_ID}-manual-shadow-v1",
        sample_count=5,
        preferred_duration_seconds={"p25": 20.0, "p50": 65.0, "p75": 120.0},
        title_hook_patterns=("？", "也太", "突然", "笑", "小皇帝", "离谱"),
        kept_content_types=("funny_talk", "interaction", "mistake", "callback", "song"),
        intro_outro_tolerance="low",
        subtitle_density_range=(0.18, 0.70),
        danmaku_density_range=(0.05, 0.90),
        negative_patterns=("contextless_fragment", "song_cut", "no_payoff", "duplicate", "pure_noise"),
    )

def _read_jingting_provenance(review_package: Path, item: Mapping[str, object]) -> JingtingProvenance:
    manifest_path = _package_path(review_package, item.get("jingting_manifest"))
    data = _read_json(manifest_path) if manifest_path and manifest_path.is_file() else None
    return JingtingProvenance.from_manifest(data if isinstance(data, Mapping) else None)

def _package_path(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else root / path

def _package_path_exists(root: Path, value: object) -> bool:
    path = _package_path(root, value)
    return bool(path and path.is_file())

def _read_json(path: Path | None) -> object:
    if path is None or not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

def _read_jingting_provenance_path(path: Path | None) -> JingtingProvenance:
    data = _read_json(path) if path and path.is_file() else None
    return JingtingProvenance.from_manifest(data if isinstance(data, Mapping) else None)

def _read_review_required(review_package: Path, item: Mapping[str, object]) -> Mapping[str, object] | None:
    review_required_path = _package_path(review_package, item.get("jingting_review_required"))
    data = _read_json(review_required_path) if review_required_path and review_required_path.is_file() else None
    return data if isinstance(data, Mapping) else None

def _read_review_required_marker(path: Path | None) -> Mapping[str, object] | None:
    data = _read_json(path) if path and path.is_file() else None
    return data if isinstance(data, Mapping) else None

def _shadow_item_paths(output_dir: Path, stem: str) -> dict[str, Path]:
    return {
        "shadow": output_dir / f"{stem}.auto_review.shadow.json",
        "decision": output_dir / f"{stem}.auto_review.decision.json",
    }

def _build_shadow_inputs_record(
    *,
    review_package: Path,
    output_dir: Path,
    item: Mapping[str, object],
    stem: str,
    title: str,
    room_id: str | None,
    date: str | None,
    shadow_run_id: str,
    artifact_hashes: Mapping[str, str],
    review_required: Mapping[str, object] | None,
    provenance: JingtingProvenance,
    no_upload: bool,
) -> dict[str, object]:
    relpaths = {
        "publish_json": item.get("publish_json") or None,
        "mp4": item.get("mp4") or None,
        "flv": item.get("flv") or None,
        "cover": item.get("cover") or None,
        "srt": item.get("srt") or None,
        "evidence": item.get("evidence") or None,
        "jingting_srt": item.get("jingting_srt") or None,
        "jingting_done": item.get("jingting_done") or None,
        "jingting_manifest": item.get("jingting_manifest") or None,
        "jingting_review_required": item.get("jingting_review_required") or None,
    }
    inputs_present = {key: bool(value and _package_path(review_package, value) and _package_path(review_package, value).is_file()) for key, value in relpaths.items()}
    fingerprints = {
        key: _stat_fingerprint(_package_path(review_package, value))
        for key, value in relpaths.items()
        if isinstance(value, str) and value
    }
    hash_status = {key: ("present" if key in artifact_hashes else "missing") for key in REQUIRED_PUBLISH_ARTIFACT_KEYS}
    return {
        "schema_version": "auto-review-shadow-candidate.v1",
        "candidate_id": stem,
        "stem": stem,
        "title": title,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "run_auto_review_shadow_pipeline.v2",
        "room_id": room_id,
        "date": date,
        "shadow_run_id": shadow_run_id,
        "source": {
            "review_package": str(review_package),
            "review_manifest": str(review_package / "review_manifest.json"),
            "summary_output_dir": str(output_dir),
        },
        "inputs_present": inputs_present,
        "inputs_relpaths": relpaths,
        "input_hashes": dict(artifact_hashes),
        "input_hash_status": hash_status,
        "input_fingerprints": fingerprints,
        "jingting_review_required_snapshot": {
            "path": relpaths["jingting_review_required"],
            "release_ready": review_required.get("release_ready") if review_required else None,
            "findings": review_required.get("findings", []) if review_required else [],
        },
        "jingting_provenance_snapshot": provenance.to_metadata(),
        "no_upload": no_upload,
    }

def _build_shadow_decision_record(
    *,
    review_package: Path,
    item: Mapping[str, object],
    candidate,
    decision,
    provenance: JingtingProvenance,
    artifact_hashes: Mapping[str, str],
    stem: str,
    title: str,
    room_id: str | None,
    date: str | None,
    shadow_run_id: str,
    jingting_done: bool,
    review_required: Mapping[str, object] | None,
    evidence_path: Path,
    shadow_path: Path,
    output_dir: Path,
    no_upload: bool,
) -> tuple[object, dict[str, object]]:
    artifact_hashes = dict(artifact_hashes)
    base_checks = _base_checks(item, jingting_done, review_required, _read_upload_enabled(review_package, stem))
    required_checks = [check.to_manifest_check() for check in evaluate_required_evidence(candidate)]
    preliminary_input_hash_status = _required_artifact_hash_status(artifact_hashes)
    artifact_hash_checks = _required_artifact_hash_checks(artifact_hashes, preliminary_input_hash_status)
    all_checks = base_checks + required_checks + artifact_hash_checks
    failed_block_reason_codes = _failed_block_reason_codes(all_checks)
    resolved_decision = replace(
        decision,
        reason_codes=tuple(dict.fromkeys(decision.reason_codes + tuple(failed_block_reason_codes))),
    )
    if failed_block_reason_codes:
        resolved_decision = replace(resolved_decision, action=DecisionAction.BLOCK)
    manifest = AutoReviewManifest(
        candidate_id=stem,
        decision=resolved_decision,
        artifacts=artifact_hashes,
        jingting_provenance=provenance,
        checks=all_checks,
        metadata={
            "title": title,
            "duration_sec": item.get("duration_sec"),
            "jingting_done": jingting_done,
            "jingting_manifest": item.get("jingting_manifest") or None,
            "jingting_review_required": item.get("jingting_review_required") or None,
            "jingting_review_required_release_ready": review_required.get("release_ready") if review_required else None,
            "jingting_review_required_findings": review_required.get("findings", []) if review_required else [],
            "jingting_qc": item.get("jingting_qc"),
            "draft_cue_count": item.get("draft_cue_count"),
            "jingting_cue_count": item.get("jingting_cue_count"),
            "subtitle_source": "review_package_jingting_srt" if item.get("jingting_srt") else "review_package_draft_srt",
            "evidence_path": str(evidence_path.relative_to(output_dir)),
            "source_context": None,
        },
    )
    decision_json_sha256 = auto_review_manifest_sha256(manifest)
    if decision_json_sha256:
        artifact_hashes["decision_json_sha256"] = decision_json_sha256
        manifest = replace(manifest, artifacts=artifact_hashes)
    input_hash_status = _required_artifact_hash_status(artifact_hashes)
    missing_required_artifact_hashes = [
        artifact_name for artifact_name, status in input_hash_status.items() if status != "present"
    ]
    publish_gate_satisfied = is_publish_gate_satisfied(
        jingting_done=jingting_done,
        manifest=manifest,
        expected_artifacts=artifact_hashes,
    )
    if resolved_decision.action == DecisionAction.AUTO_UPLOAD and not publish_gate_satisfied:
        fallback_reason_codes = list(resolved_decision.reason_codes)
        if not fallback_reason_codes:
            fallback_reason_codes.append("PUBLISH_GATE_INCOMPLETE")
        resolved_decision = replace(
            resolved_decision,
            action=DecisionAction.BLOCK,
            reason_codes=tuple(dict.fromkeys(fallback_reason_codes)),
        )
        manifest = replace(manifest, decision=resolved_decision)
    manifest_data = manifest.to_dict()
    manifest_data.update(
        {
            "schema_version": "slice-auto-review-shadow-decision.v1",
            "stem": stem,
            "title": title,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_by": "run_auto_review_shadow_pipeline.v2",
            "room_id": room_id,
            "date": date,
            "shadow_run_id": shadow_run_id,
            "shadow_inputs_ref": str(shadow_path.relative_to(output_dir)),
            "no_upload": no_upload,
            "input_hash_status": input_hash_status,
            "missing_required_artifact_hashes": missing_required_artifact_hashes,
        }
    )
    manifest_data["publish_gate_shadow"] = {
        "done_only_gate_satisfied": is_publish_gate_satisfied(
            jingting_done=jingting_done,
            manifest=None,
            expected_artifacts=artifact_hashes,
        ),
        "decision_manifest_gate_satisfied": is_publish_gate_satisfied(
            jingting_done=jingting_done,
            manifest=manifest,
            expected_artifacts=artifact_hashes,
        ),
        "required_artifact_keys": list(REQUIRED_PUBLISH_ARTIFACT_KEYS),
    }
    return resolved_decision, manifest_data

def _write_shadow_markers(
    *,
    output_dir: Path,
    stem: str,
    decision,
    decision_path: Path,
    shadow_path: Path,
    room_id: str | None,
    date: str | None,
    shadow_run_id: str,
    no_upload: bool,
) -> list[str]:
    marker_map = {
        "done": output_dir / f"{stem}.auto_review.done",
        "block": output_dir / f"{stem}.auto_review.block",
        "retry": output_dir / f"{stem}.auto_review.retry",
        "drop": output_dir / f"{stem}.auto_review.drop",
        "would_upload": output_dir / f"{stem}.auto_review.would_upload",
    }
    existing_markers = [path for path in marker_map.values() if path.exists()]
    if existing_markers:
        raise FileExistsError("pre-existing shadow marker(s): " + ", ".join(str(path) for path in existing_markers))
    action = decision.action.value if hasattr(decision.action, "value") else str(decision.action)
    if action == DecisionAction.AUTO_UPLOAD.value:
        selected = ("done", "would_upload")
    elif action == DecisionAction.BLOCK.value:
        selected = ("done", "block")
    elif action == DecisionAction.DROP.value:
        selected = ("done", "drop")
    else:
        selected = ("retry",)
    payload = {
        "schema_version": "slice-auto-review-shadow-marker.v1",
        "candidate_id": stem,
        "action": action,
        "reason_codes": list(decision.reason_codes),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "run_auto_review_shadow_pipeline.v2",
        "decision_path": str(decision_path.relative_to(output_dir)),
        "shadow_inputs_path": str(shadow_path.relative_to(output_dir)),
        "shadow_run_id": shadow_run_id,
        "room_id": room_id,
        "date": date,
        "no_upload": no_upload,
    }
    written: list[str] = []
    for marker_name in selected:
        marker_payload = dict(payload)
        marker_payload["marker"] = marker_name
        _write_json_file(marker_map[marker_name], marker_payload)
        written.append(str(marker_map[marker_name].relative_to(output_dir)))
    return written

def _record_paths_exist(output_dir: Path, record: Mapping[str, object]) -> bool:
    for key in ("decision_path", "shadow_inputs_path"):
        value = record.get(key)
        if not isinstance(value, str) or not (output_dir / value).exists():
            return False
    marker_paths = record.get("marker_paths")
    if isinstance(marker_paths, Sequence) and not isinstance(marker_paths, str):
        for marker_path in marker_paths:
            if not isinstance(marker_path, str) or not (output_dir / marker_path).exists():
                return False
    return True

def _base_checks(item: Mapping[str, object], jingting_done: bool, review_required: Mapping[str, object] | None, upload_enabled: bool | None) -> list[dict[str, object]]:
    review_required_failed = _review_required_failed(review_required)
    timing_qc_ok = item.get("jingting_qc") == "same_timing"
    upload_disabled = upload_enabled is False
    return [
        {
            "code": "JINGTING_DONE_PRESENT",
            "pass": jingting_done,
            "severity": "RETRY",
            "reason_code": "JINGTING_PENDING" if not jingting_done else None,
            "evidence": {"path": item.get("jingting_done") or None},
        },
        {
            "code": "JINGTING_TIMING_QC",
            "pass": timing_qc_ok,
            "severity": "BLOCK",
            "reason_code": None if timing_qc_ok else "JINGTING_TIMING_QC_FAILED",
            "evidence": {
                "qc": item.get("jingting_qc"),
                "draft_cues": item.get("draft_cue_count"),
                "jingting_cues": item.get("jingting_cue_count"),
            },
        },
        {
            "code": "JINGTING_REVIEW_REQUIRED",
            "pass": not review_required_failed,
            "severity": "BLOCK",
            "reason_code": None if not review_required_failed else "JINGTING_REVIEW_REQUIRED",
            "evidence": {
                "path": item.get("jingting_review_required") or None,
                "release_ready": True if review_required is None else review_required.get("release_ready"),
                "findings": [] if review_required is None else review_required.get("findings", []),
            },
        },
        {
            "code": "UPLOAD_DISABLED_IN_SOURCE_PACKAGE",
            "pass": upload_disabled,
            "severity": "BLOCK",
            "reason_code": None if upload_disabled else "UPLOAD_ENABLED_IN_SOURCE_PACKAGE",
            "evidence": {"upload_enabled": upload_enabled},
        },
    ]

def _review_required_failed(review_required: Mapping[str, object] | None) -> bool:
    if review_required is None:
        return False
    release_ready = review_required.get("release_ready")
    findings = review_required.get("findings")
    has_findings = isinstance(findings, Sequence) and not isinstance(findings, str) and any(bool(item) for item in findings)
    return release_ready is False or has_findings

def _required_artifact_hash_status(artifact_hashes: Mapping[str, str]) -> dict[str, str]:
    return {
        artifact_name: ("present" if artifact_hashes.get(artifact_name) else "missing")
        for artifact_name in REQUIRED_PUBLISH_ARTIFACT_KEYS
    }

def _required_artifact_hash_checks(
    artifact_hashes: Mapping[str, str],
    input_hash_status: Mapping[str, str],
) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    for artifact_name in REQUIRED_PUBLISH_ARTIFACT_KEYS:
        if artifact_name == "decision_json_sha256":
            continue
        present = bool(artifact_hashes.get(artifact_name))
        checks.append(
            {
                "code": f"PUBLISH_ARTIFACT_HASH_RECORDED_{artifact_name.upper()}",
                "pass": present,
                "severity": "BLOCK",
                "reason_code": None if present else f"MISSING_REQUIRED_ARTIFACT_HASH_{artifact_name.upper()}",
                "evidence": {
                    "artifact_name": artifact_name,
                    "hash": artifact_hashes.get(artifact_name),
                    "status": input_hash_status.get(artifact_name, "missing"),
                },
            }
        )
    return checks

def _failed_block_reason_codes(checks: Sequence[Mapping[str, object]]) -> list[str]:
    reason_codes: list[str] = []
    for check in checks:
        if check.get("severity") != "BLOCK" or check.get("pass") is not False:
            continue
        reason_code = check.get("reason_code")
        if isinstance(reason_code, str) and reason_code:
            reason_codes.append(reason_code)
    return list(dict.fromkeys(reason_codes))

def _artifact_hashes(root: Path, item: Mapping[str, object]) -> dict[str, str]:
    mapping = {
        "video_sha256": item.get("mp4") or item.get("flv"),
        "cover_sha256": item.get("cover"),
        "draft_subtitle_sha256": item.get("srt"),
        "jingting_subtitle_sha256": item.get("jingting_srt"),
        "jingting_manifest_sha256": item.get("jingting_manifest"),
        "evidence_sha256": item.get("evidence"),
        "publish_json_sha256": item.get("publish_json") or (f"{item.get('stem')}.publish.json" if item.get("stem") else None),
    }
    return {key: "sha256:" + _sha256(root / path) for key, path in mapping.items() if isinstance(path, str) and path and (root / path).exists()}

def _read_upload_enabled(root: Path, stem: str) -> bool | None:
    path = root / f"{stem}.publish.json"
    if not path.exists():
        return None
    data = _read_json(path)
    if not isinstance(data, Mapping):
        return None
    value = data.get("upload_enabled")
    return value if isinstance(value, bool) else None

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _stat_fingerprint(path: Path | None) -> dict[str, object]:
    if path is None:
        return {"path": None, "exists": False}
    if not path.exists():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    return {"path": str(path), "exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

def _write_json_file(path: Path, data: Mapping[str, object]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def _float(value: object, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default

def _empty_counts() -> dict[str, int]:
    return {
        "candidates_evaluated": 0,
        "candidates_with_content_evidence": 0,
        "candidates_with_style_profile_evidence": 0,
        "auto_upload": 0,
        "auto_recut": 0,
        "block": 0,
        "drop": 0,
        "retry": 0,
    }

def _increment_action_count(counts: dict[str, int], action: str) -> None:
    key = {
        "AUTO_UPLOAD": "auto_upload",
        "AUTO_RECUT": "auto_recut",
        "BLOCK": "block",
        "DROP": "drop",
        "RETRY": "retry",
    }.get(action)
    if key:
        counts[key] += 1

def _gap_summary(records: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        reasons = record.get("reason_codes")
        if not isinstance(reasons, Sequence) or isinstance(reasons, str):
            continue
        for reason in reasons:
            if isinstance(reason, str):
                counts[reason] = counts.get(reason, 0) + 1
    return counts
