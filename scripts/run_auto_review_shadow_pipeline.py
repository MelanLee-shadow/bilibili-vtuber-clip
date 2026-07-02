from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.auto_review import (
    AutoReviewManifest,
    DecisionAction,
    JingtingProvenance,
    REQUIRED_PUBLISH_ARTIFACT_KEYS,
    auto_review_manifest_sha256,
    evaluate_required_evidence,
    is_publish_gate_satisfied,
    review_candidate,
)
from src.autoslice.boundary_resolver import AnchorCandidate, BoundaryResolution, TalkCue, resolve_talk_boundary
from src.autoslice.content_evidence import analyze_content_evidence
from src.autoslice.cpa_semantic_qa import (
    apply_cpa_semantic_qa_to_review_evidence,
    evaluate_cpa_semantic_response_artifact,
    load_request_artifact,
)
from src.autoslice.cpa_semantic_review import (
    CPASemanticReviewError,
    apply_cpa_semantic_review,
    load_cpa_semantic_review_response,
)
from src.autoslice.render_qa import RenderRequest, RenderedTimelineMetadata, evaluate_render_pts
from src.autoslice.review_evidence import ReviewEvidence, SourceCue, to_candidate_review
from src.autoslice.source_integrity import MediaSegmentObservation, build_source_range_ledger, plan_bilibili_replay_compensation
from src.autoslice.source_context_executor import AgyExecutionResult, SourceContextExecutionResult, execute_source_context_job
from src.autoslice.source_context_planner import JingtingJobProvenance, plan_source_context_jingting_jobs
from src.autoslice.style_profile import ManualStyleProfile, apply_style_profile
from src.autoslice.term_lexicon import load_discovered_term_lexicon, normalize_text


def run_shadow_pipeline(
    *,
    review_package: Path | None = None,
    source_video: Path | None = None,
    source_srt: Path | None = None,
    refined_srt: Path | None = None,
    source_context_job: Mapping[str, object] | None = None,
    agy_result: AgyExecutionResult | None = None,
    source_context_agy_runner: Callable[[Path, Path, Path], AgyExecutionResult] | None = None,
    room_id: str | None = None,
    title: str | None = None,
    output_dir: Path,
    no_upload: bool = True,
    source_context_run_ffmpeg: bool = True,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "evidence").mkdir(exist_ok=True)

    if review_package is not None:
        summary = _run_review_package(review_package=review_package, output_dir=output_dir, no_upload=no_upload)
    elif source_video is not None:
        summary = _run_live_source(
            source_video=source_video,
            source_srt=source_srt,
            refined_srt=refined_srt,
            source_context_job=source_context_job,
            agy_result=agy_result,
            source_context_agy_runner=source_context_agy_runner,
            room_id=room_id,
            title=title,
            output_dir=output_dir,
            no_upload=no_upload,
            source_context_run_ffmpeg=source_context_run_ffmpeg,
        )
    else:
        raise ValueError("review_package or source_video is required")

    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "README.md").write_text(_readme(summary), encoding="utf-8")
    return summary


def _run_review_package(*, review_package: Path, output_dir: Path, no_upload: bool) -> dict[str, object]:
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
        evidence = analyze_content_evidence(candidate_id=stem, cues=cues, title=title)
        counts["candidates_with_content_evidence"] += 1
        evidence = apply_style_profile(evidence, profile, title=title, duration_seconds=duration_seconds)
        counts["candidates_with_style_profile_evidence"] += 1
        provenance = _read_jingting_provenance(review_package, item)
        review_required = _read_review_required(review_package, item)
        candidate = to_candidate_review(evidence, provenance, jingting_done=jingting_done)
        candidate = _apply_review_required(candidate, review_required)
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
def _run_live_source(
    *,
    source_video: Path,
    source_srt: Path | None,
    refined_srt: Path | None,
    source_context_job: Mapping[str, object] | None,
    agy_result: AgyExecutionResult | None,
    source_context_agy_runner: Callable[[Path, Path, Path], AgyExecutionResult] | None,
    room_id: str | None,
    title: str | None,
    output_dir: Path,
    no_upload: bool,
    source_context_run_ffmpeg: bool,
) -> dict[str, object]:
    candidate_id = str((source_context_job or {}).get("candidate_id") or source_video.stem)
    title = title or candidate_id
    counts = _empty_counts()
    counts["candidates_evaluated"] = 1
    source_integrity = _live_source_integrity_record(source_video, source_context_job, room_id)

    if source_srt is None or not source_srt.is_file():
        record = _write_live_source_gap_record(
            output_dir=output_dir,
            candidate_id=candidate_id,
            reason_codes=("DRAFT_SRT_MISSING",),
            source_context=None,
        )
        counts["retry"] = 1
        return _live_source_summary(
            source_video=source_video,
            source_srt=source_srt,
            refined_srt=refined_srt,
            room_id=room_id,
            counts=counts,
            records=[record],
            no_upload=no_upload,
            source_integrity=source_integrity,
        )

    job_manifest = _prepare_live_source_job(
        source_video=source_video,
        source_srt=source_srt,
        source_context_job=source_context_job,
        room_id=room_id,
    )
    candidate_id = str(job_manifest.get("candidate_id") or candidate_id)
    source_context = execute_source_context_job(
        job_manifest,
        source_video_path=source_video,
        output_dir=output_dir / "source_context" / candidate_id,
        full_source_srt_path=source_srt,
        refined_srt_path=refined_srt,
        agy_result=agy_result,
        agy_runner=source_context_agy_runner or (_run_source_context_agy if refined_srt is None else None),
        run_ffmpeg=source_context_run_ffmpeg,
    )

    if source_context.decision != "READY" or not source_context.context_refined_srt_path:
        record = _write_live_source_gap_record(
            output_dir=output_dir,
            candidate_id=candidate_id,
            reason_codes=source_context.reason_codes,
            source_context=source_context,
        )
        counts["retry"] = 1 if source_context.decision.startswith("RETRY") else 0
        counts["block"] = 0 if source_context.decision.startswith("RETRY") else 1
        return _live_source_summary(
            source_video=source_video,
            source_srt=source_srt,
            refined_srt=refined_srt,
            room_id=room_id,
            counts=counts,
            records=[record],
            no_upload=no_upload,
            source_integrity=source_integrity,
        )

    context_start_ms = _int(_mapping(job_manifest.get("timeline")).get("context_start_ms"), 0)
    context_duration_ms = _int(_mapping(job_manifest.get("timeline")).get("context_duration_ms"), 0)
    cues = _parse_srt(Path(source_context.context_refined_srt_path), source_offset_ms=context_start_ms)
    boundary_resolution = _resolve_live_source_boundary(job_manifest, cues)
    evidence = analyze_content_evidence(candidate_id=candidate_id, cues=cues, title=title)
    counts["candidates_with_content_evidence"] = 1
    evidence = _apply_live_source_context_metadata(
        evidence,
        source_context_job=job_manifest,
        source_context=source_context,
        boundary_resolution=boundary_resolution,
    )
    evidence = _apply_live_source_machine_evidence(
        evidence,
        source_context_job=job_manifest,
        source_context=source_context,
        title=title,
    )
    duration_seconds = _duration_seconds(cues, context_duration_ms / 1000.0)
    evidence = apply_style_profile(evidence, _default_lidousha_profile(), title=title, duration_seconds=duration_seconds)
    counts["candidates_with_style_profile_evidence"] = 1
    materialized_recut = _materialize_recut_record(
        source_video=source_video,
        candidate_id=candidate_id,
        boundary_resolution=boundary_resolution,
        output_dir=output_dir,
        cues=cues,
        run_ffmpeg=source_context_run_ffmpeg,
    )
    evidence = _apply_materialized_recut_render_qa(evidence, materialized_recut)
    evidence = _apply_cpa_semantic_review_from_job(evidence, job_manifest, candidate_id=candidate_id, output_dir=output_dir)
    provenance = _read_jingting_provenance_path(Path(source_context.jingting_manifest_path) if source_context.jingting_manifest_path else None)
    decision = review_candidate(to_candidate_review(evidence, provenance, jingting_done=source_context.jingting_done))
    decision = _merge_cpa_semantic_review_into_decision(decision, evidence)
    decision = _merge_boundary_resolution_into_decision(decision, boundary_resolution)
    _increment_action_count(counts, decision.action.value)

    evidence_path = output_dir / "evidence" / f"{candidate_id}.evidence.json"
    evidence_path.write_text(json.dumps(evidence.to_manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record = {
        "candidate_id": candidate_id,
        "title": title,
        "decision_action": decision.action.value,
        "reason_codes": list(decision.reason_codes),
        "score": decision.score,
        "evidence_path": str(evidence_path),
        "subtitle_source": "source_context_refined_srt",
        "subtitle_path": source_context.context_refined_srt_path,
        "source_context_job": _source_context_job_record(job_manifest),
        "source_context": _source_context_record(source_context),
        "boundary_resolution": _boundary_resolution_record(boundary_resolution),
        "recut_plan": _recut_plan_record(
            source_video=source_video,
            candidate_id=candidate_id,
            boundary_resolution=boundary_resolution,
            output_dir=output_dir,
        ),
        "materialized_recut": materialized_recut,
    }
    return _live_source_summary(
        source_video=source_video,
        source_srt=source_srt,
        refined_srt=refined_srt,
        room_id=room_id,
        counts=counts,
        records=[record],
        no_upload=no_upload,
        source_integrity=source_integrity,
    )


def _default_lidousha_profile() -> ManualStyleProfile:
    return ManualStyleProfile(
        profile_id="lidousha-manual-shadow-v1",
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


def _parse_srt(path: Path, *, source_offset_ms: int = 0) -> list[SourceCue]:
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    lexicon = load_discovered_term_lexicon(path)
    cues: list[SourceCue] = []
    for index, block in enumerate(raw.split("\n\n"), start=1):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        if "-->" in lines[0]:
            timing = lines[0]
            text_lines = lines[1:]
        else:
            timing = lines[1]
            text_lines = lines[2:]
        if "-->" not in timing:
            continue
        start, end = [part.strip() for part in timing.split("-->", 1)]
        text = normalize_text("\n".join(text_lines).strip(), lexicon=lexicon)
        kind = "singing" if any(marker in text for marker in ("《", "啦", "アイドル", "言って")) else "speech"
        cues.append(
            SourceCue(
                cue_id=f"u_{index:06d}",
                source_start_ms=source_offset_ms + _parse_time_ms(start),
                source_end_ms=source_offset_ms + _parse_time_ms(end),
                text=text,
                language="zh",
                kind=kind,
                confidence=1.0,
            )
        )
    return cues


def _parse_time_ms(value: str) -> int:
    hhmmss, millis = value.replace(",", ".").split(".", 1)
    hours, minutes, seconds = [int(part) for part in hhmmss.split(":")]
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(millis[:3].ljust(3, "0"))


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


def _apply_review_required(candidate, review_required: Mapping[str, object] | None):
    if review_required is None:
        return candidate
    findings = review_required.get("findings")
    release_ready = review_required.get("release_ready")
    return replace(
        candidate,
        release_ready=release_ready if isinstance(release_ready, bool) else candidate.release_ready,
        review_required_findings=tuple(str(item) for item in findings) if isinstance(findings, list) else candidate.review_required_findings,
    )


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


def _write_live_source_gap_record(
    *,
    output_dir: Path,
    candidate_id: str,
    reason_codes: Sequence[str],
    source_context: SourceContextExecutionResult | None,
) -> dict[str, object]:
    evidence_path = output_dir / "evidence" / f"{candidate_id}.evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": "slice-review-evidence.v1",
                "candidate_id": candidate_id,
                "evidence_gaps": list(reason_codes),
                "source_cues": [],
                "checks": [
                    {
                        "code": "LIVE_SOURCE_CONTEXT",
                        "pass": False,
                        "reason_codes": list(reason_codes),
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "candidate_id": candidate_id,
        "decision_action": "RETRY" if "DRAFT_SRT_MISSING" in reason_codes or "SOURCE_VIDEO_MISSING" in reason_codes else "BLOCK",
        "reason_codes": list(reason_codes),
        "score": 0.0,
        "evidence_path": str(evidence_path),
        "subtitle_source": None,
        "subtitle_path": None,
        "source_context": _source_context_record(source_context) if source_context else None,
    }


def _live_source_integrity_record(source_video: Path, source_context_job: Mapping[str, object] | None, room_id: str | None) -> dict[str, object] | None:
    job = source_context_job or {}
    integrity = _mapping(job.get("source_integrity"))
    timeline = _mapping(job.get("timeline"))
    if not integrity and not timeline:
        return None

    expected_start_ms = _int(integrity.get("expected_start_ms"), _int(timeline.get("source_start_ms"), 0))
    expected_end_ms = _int(
        integrity.get("expected_end_ms"),
        _int(timeline.get("source_duration_ms"), _int(timeline.get("context_end_ms"), _int(timeline.get("context_duration_ms"), expected_start_ms))),
    )
    if expected_end_ms <= expected_start_ms:
        return None

    segments: list[MediaSegmentObservation] = []
    raw_segments = integrity.get("observed_segments")
    if isinstance(raw_segments, Sequence) and not isinstance(raw_segments, (str, bytes)):
        for raw in raw_segments:
            item = _mapping(raw)
            if not item:
                continue
            segments.append(
                MediaSegmentObservation(
                    path=str(item.get("path") or source_video),
                    start_ms=_int(item.get("start_ms"), expected_start_ms),
                    end_ms=_int(item.get("end_ms"), expected_end_ms),
                    duration_ms=_int(item.get("duration_ms"), max(0, expected_end_ms - expected_start_ms)),
                    size_bytes=_int(item.get("size_bytes"), 0),
                    probed_ok=bool(item.get("probed_ok", True)),
                )
            )
    elif source_video.exists():
        stat = source_video.stat()
        segments.append(
            MediaSegmentObservation(
                path=str(source_video),
                start_ms=expected_start_ms,
                end_ms=expected_end_ms,
                duration_ms=max(0, expected_end_ms - expected_start_ms),
                size_bytes=stat.st_size,
                probed_ok=True,
            )
        )

    replay_available_value = integrity.get("bilibili_replay_available")
    replay_available = replay_available_value if isinstance(replay_available_value, bool) else None
    ledger = build_source_range_ledger(
        room_id=str(room_id or job.get("room_id") or "unknown"),
        session_date=str(integrity.get("session_date") or job.get("date") or "unknown"),
        segments=segments,
        expected_start_ms=expected_start_ms,
        expected_end_ms=expected_end_ms,
        danmaku_latest_ms=_int(integrity.get("danmaku_latest_ms"), None),
        active_media_size_growth_bytes=_int(integrity.get("active_media_size_growth_bytes"), None),
        max_gap_ms=_int(integrity.get("max_gap_ms"), 2_000),
    )
    plan = plan_bilibili_replay_compensation(
        ledger,
        auth_available=bool(integrity.get("bilibili_replay_auth_available", False)),
        replay_available=replay_available,
    )
    return {"ledger": ledger.to_manifest(), "compensation_plan": plan.to_manifest()}


def _live_source_summary(
    *,
    source_video: Path,
    source_srt: Path | None,
    refined_srt: Path | None,
    room_id: str | None,
    counts: Mapping[str, int],
    records: Sequence[Mapping[str, object]],
    no_upload: bool,
    source_integrity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    summary = {
        "schema_version": "auto-review-shadow-summary.v1",
        "mode": "live_source",
        "input": {
            "source_video": str(source_video),
            "source_video_exists": source_video.is_file(),
            "source_srt": str(source_srt) if source_srt else None,
            "source_srt_exists": source_srt.is_file() if source_srt else False,
            "refined_srt": str(refined_srt) if refined_srt else None,
            "refined_srt_exists": refined_srt.is_file() if refined_srt else False,
            "room_id": room_id,
        },
        "counts": dict(counts),
        "records": [dict(record) for record in records],
        "validations": {"no_upload": no_upload, "no_upload_or_free_deploy_performed": no_upload},
        "gap_summary": _gap_summary(records),
    }
    if source_integrity is not None:
        summary["source_integrity"] = dict(source_integrity)
    return summary


def _source_context_record(result: SourceContextExecutionResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    review_required_path = Path(result.review_required_path)
    return {
        "decision": result.decision,
        "reason_codes": list(result.reason_codes),
        "context_media_path": result.context_media_path,
        "context_draft_srt_path": result.context_draft_srt_path,
        "context_refined_srt_path": result.context_refined_srt_path,
        "jingting_manifest_path": result.jingting_manifest_path,
        "review_required_path": result.review_required_path if review_required_path.is_file() else None,
        "source_cues_path": result.source_cues_path,
        "jingting_done": result.jingting_done,
    }


def _prepare_live_source_job(
    *,
    source_video: Path,
    source_srt: Path,
    source_context_job: Mapping[str, object] | None,
    room_id: str | None,
) -> dict[str, object]:
    requested = dict(source_context_job or {})
    anchor = _job_anchor_candidate(requested)
    if anchor is None:
        return requested or _default_source_context_job(source_video=source_video, source_srt=source_srt, room_id=room_id)

    timeline = _mapping(requested.get("timeline"))
    if timeline.get("context_start_ms") is not None and timeline.get("context_duration_ms") is not None:
        return requested

    cues = _parse_srt(source_srt)
    source_duration_ms = max((cue.source_end_ms for cue in cues), default=0)
    provenance = _job_provenance(requested, source_video=source_video, room_id=room_id)
    planned = plan_source_context_jingting_jobs(
        [anchor],
        source_duration_ms=source_duration_ms,
        provenance=provenance,
        pre_ms=_int(requested.get("pre_ms"), 90_000),
        post_ms=_int(requested.get("post_ms"), 150_000),
        provider=str(requested.get("provider") or "agy"),
    )[0].to_manifest()
    if room_id:
        planned["room_id"] = room_id
    return planned


def _job_provenance(requested: Mapping[str, object], *, source_video: Path, room_id: str | None) -> JingtingJobProvenance:
    provenance = _mapping(requested.get("provenance"))
    return JingtingJobProvenance(
        recording_id=_string_or_none(provenance.get("recording_id")) or room_id or source_video.stem,
        source_sha256=_string_or_none(provenance.get("source_sha256")) or f"sha256:{_sha256(source_video)}",
        source_uri=_string_or_none(provenance.get("source_uri")) or source_video.resolve().as_uri(),
        planner_version=_string_or_none(provenance.get("planner_version")) or "source-context-planner.v1",
        code_commit=_string_or_none(provenance.get("code_commit")),
        transcript_provider=_string_or_none(provenance.get("transcript_provider")),
        transcript_model=_string_or_none(provenance.get("transcript_model")),
        prompt_sha256=_string_or_none(provenance.get("prompt_sha256")),
        provider_request_id=_string_or_none(provenance.get("provider_request_id")),
        provider_fallback_used=_optional_bool(provenance.get("provider_fallback_used")),
    )


def _resolve_live_source_boundary(
    job_manifest: Mapping[str, object],
    cues: Sequence[SourceCue],
) -> BoundaryResolution | None:
    anchor = _job_anchor_candidate(job_manifest)
    if anchor is None or not cues:
        return None
    song_boundary_resolution = _resolve_song_boundary(job_manifest, anchor)
    if song_boundary_resolution is not None:
        return song_boundary_resolution
    if all(cue.kind == "singing" for cue in cues):
        return None
    talk_cues = [_to_talk_cue(cue, index, cues) for index, cue in enumerate(cues)]
    return resolve_talk_boundary(anchor, talk_cues)


def _job_anchor_candidate(job_manifest: Mapping[str, object]) -> AnchorCandidate | None:
    timeline = _mapping(job_manifest.get("timeline"))
    anchor_start_ms = _first_int(timeline.get("anchor_start_ms"), job_manifest.get("anchor_start_ms"))
    anchor_end_ms = _first_int(timeline.get("anchor_end_ms"), job_manifest.get("anchor_end_ms"))
    if anchor_start_ms is None or anchor_end_ms is None or anchor_end_ms < anchor_start_ms:
        return None
    candidate_id = str(job_manifest.get("candidate_id") or "source-context")
    return AnchorCandidate(candidate_id=candidate_id, anchor_start_ms=anchor_start_ms, anchor_end_ms=anchor_end_ms)


def _resolve_song_boundary(job_manifest: Mapping[str, object], anchor: AnchorCandidate) -> BoundaryResolution | None:
    song_boundary = _mapping(job_manifest.get("song_boundary"))
    lyrics_alignment = _mapping(job_manifest.get("lyrics_alignment"))
    if not _song_boundary_ready(song_boundary) or not _lyrics_alignment_ready(lyrics_alignment):
        return None

    clip_start_ms = _first_int(song_boundary.get("clip_start_ms"), song_boundary.get("source_start_ms"), song_boundary.get("song_start_ms"))
    clip_end_ms = _first_int(
        song_boundary.get("clip_end_ms"),
        song_boundary.get("source_end_ms"),
        song_boundary.get("post_song_reaction_end_ms"),
        song_boundary.get("last_lyric_end_ms"),
    )
    if clip_start_ms is None or clip_end_ms is None or clip_end_ms <= clip_start_ms:
        return BoundaryResolution(
            candidate_id=anchor.candidate_id,
            action=DecisionAction.BLOCK,
            resolved_start_ms=anchor.anchor_start_ms,
            resolved_end_ms=anchor.anchor_end_ms,
            start_boundary_score=0.0,
            end_boundary_score=0.0,
            reason_codes=("SONG_BOUNDARY_INVALID",),
        )

    resolved_start_ms = max(0, clip_start_ms)
    anchor_already_full_song = anchor.anchor_start_ms <= resolved_start_ms and anchor.anchor_end_ms >= clip_end_ms
    action = DecisionAction.AUTO_UPLOAD if anchor_already_full_song else DecisionAction.AUTO_RECUT
    return BoundaryResolution(
        candidate_id=anchor.candidate_id,
        action=action,
        resolved_start_ms=resolved_start_ms,
        resolved_end_ms=clip_end_ms,
        start_boundary_score=0.98,
        end_boundary_score=0.98,
        reason_codes=("SONG_FULL_BOUNDARY_READY",),
        next_start_ms=resolved_start_ms if action == DecisionAction.AUTO_RECUT else None,
        next_end_ms=clip_end_ms if action == DecisionAction.AUTO_RECUT else None,
    )


def _song_boundary_ready(song_boundary: Mapping[str, object]) -> bool:
    return song_boundary.get("status") == "FULL_SONG_READY"


def _lyrics_alignment_ready(lyrics_alignment: Mapping[str, object]) -> bool:
    return lyrics_alignment.get("status") == "READY"


def _apply_live_source_machine_evidence(
    evidence: ReviewEvidence,
    *,
    source_context_job: Mapping[str, object],
    source_context: SourceContextExecutionResult,
    title: str,
) -> ReviewEvidence:
    checks = list(evidence.checks)
    updates: dict[str, object] = {}

    song_boundary = _mapping(source_context_job.get("song_boundary"))
    lyrics_alignment = _mapping(source_context_job.get("lyrics_alignment"))
    full_song_evidence_ready = _song_boundary_ready(song_boundary) and _lyrics_alignment_ready(lyrics_alignment)
    if full_song_evidence_ready:
        song_duration_seconds = _song_boundary_duration_seconds(song_boundary)
        updates.update(
            {
                "foreground_song_overlap_seconds": song_duration_seconds if song_duration_seconds is not None else evidence.foreground_song_overlap_seconds,
                "song_complete": True,
                "lyrics_alignment_ready": True,
                "start_boundary_score": max(evidence.start_boundary_score or 0.0, 0.98),
                "end_boundary_score": max(evidence.end_boundary_score or 0.0, 0.98),
                "standalone_score": max(evidence.standalone_score or 0.0, 0.94),
                "payoff_score": max(evidence.payoff_score or 0.0, 0.95),
                "editorial_score": max(evidence.editorial_score or 0.0, 90.0),
                "evidence_gaps": tuple(
                    gap
                    for gap in evidence.evidence_gaps
                    if gap not in {"SONG_PARTIAL", "LYRICS_AUTO_ALIGNMENT_UNIMPLEMENTED", "LYRICS_ALIGNMENT_REQUIRED"}
                ),
                "metadata": {
                    **dict(evidence.metadata),
                    "song_boundary": dict(song_boundary),
                    "lyrics_alignment": dict(lyrics_alignment),
                    "song_duration_seconds": song_duration_seconds,
                },
            }
        )
        checks.append(
            {
                "code": "SONG_FULL_BOUNDARY_READY",
                "pass": True,
                "severity": "PASS",
                "evidence": {"song_boundary": dict(song_boundary), "lyrics_alignment": dict(lyrics_alignment)},
            }
        )

    if evidence.subtitle_alignment_p95_ms is None:
        alignment_p95 = _subtitle_alignment_p95_ms(
            Path(source_context.context_draft_srt_path) if source_context.context_draft_srt_path else None,
            Path(source_context.context_refined_srt_path) if source_context.context_refined_srt_path else None,
        )
        if alignment_p95 is not None:
            updates["subtitle_alignment_p95_ms"] = alignment_p95
            checks.append(
                {
                    "code": "SUBTITLE_ALIGNMENT_P95",
                    "pass": alignment_p95 <= 350.0,
                    "severity": "PASS" if alignment_p95 <= 350.0 else "AUTO_RECUT",
                    "evidence": {"subtitle_alignment_p95_ms": alignment_p95, "method": "draft_vs_refined_indexed_cue_timing"},
                }
            )

    if evidence.duplicate_similarity is None:
        duplicate_similarity = _duplicate_similarity_from_job(source_context_job, title=title)
        duplicate_method = "sequence_matcher_title_corpus"
        if duplicate_similarity is None and full_song_evidence_ready:
            duplicate_similarity = 0.0
            duplicate_method = "no_duplicate_corpus_available_assume_unique_for_no_upload_shadow"
        if duplicate_similarity is not None:
            updates["duplicate_similarity"] = duplicate_similarity
            checks.append(
                {
                    "code": "DUPLICATE_SIMILARITY",
                    "pass": duplicate_similarity < 0.90,
                    "severity": "PASS" if duplicate_similarity < 0.90 else "DROP",
                    "evidence": {"duplicate_similarity": duplicate_similarity, "method": duplicate_method},
                }
            )

    if not updates and len(checks) == len(evidence.checks):
        return evidence
    updates["checks"] = tuple(checks)
    return replace(evidence, **updates)


def _song_boundary_duration_seconds(song_boundary: Mapping[str, object]) -> float | None:
    first_lyric_start_ms = _first_int(song_boundary.get("first_lyric_start_ms"), song_boundary.get("song_start_ms"), song_boundary.get("clip_start_ms"))
    last_lyric_end_ms = _first_int(song_boundary.get("last_lyric_end_ms"), song_boundary.get("song_end_ms"), song_boundary.get("clip_end_ms"))
    if first_lyric_start_ms is None or last_lyric_end_ms is None or last_lyric_end_ms <= first_lyric_start_ms:
        return None
    return round((last_lyric_end_ms - first_lyric_start_ms) / 1000.0, 3)


def _subtitle_alignment_p95_ms(draft_srt: Path | None, refined_srt: Path | None) -> float | None:
    if draft_srt is None or refined_srt is None or not draft_srt.is_file() or not refined_srt.is_file():
        return None
    draft = _parse_srt(draft_srt)
    refined = _parse_srt(refined_srt)
    if not draft or not refined:
        return None
    count = min(len(draft), len(refined))
    deltas: list[float] = []
    for index in range(count):
        deltas.append(float(abs(draft[index].source_start_ms - refined[index].source_start_ms)))
        deltas.append(float(abs(draft[index].source_end_ms - refined[index].source_end_ms)))
    if len(draft) != len(refined):
        deltas.extend([10_000.0] * abs(len(draft) - len(refined)))
    return _p95(deltas)


def _duplicate_similarity_from_job(source_context_job: Mapping[str, object], *, title: str) -> float | None:
    corpus_value = source_context_job.get("duplicate_corpus")
    if not isinstance(corpus_value, Sequence) or isinstance(corpus_value, (str, bytes)):
        return None
    corpus: list[str] = []
    for item in corpus_value:
        if isinstance(item, str):
            corpus.append(item)
        elif isinstance(item, Mapping):
            text = item.get("title") or item.get("text") or item.get("candidate_id")
            if isinstance(text, str):
                corpus.append(text)
    if not corpus:
        return 0.0
    normalized_title = _normalize_similarity_text(title)
    if not normalized_title:
        return None
    return max(
        difflib.SequenceMatcher(None, normalized_title, _normalize_similarity_text(other)).ratio()
        for other in corpus
        if _normalize_similarity_text(other)
    )


def _normalize_similarity_text(value: str) -> str:
    return "".join(value.lower().split())


def _p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * 0.95))
    return float(ordered[index])


def _apply_live_source_context_metadata(
    evidence: ReviewEvidence,
    *,
    source_context_job: Mapping[str, object],
    source_context: SourceContextExecutionResult,
    boundary_resolution: BoundaryResolution | None,
) -> ReviewEvidence:
    metadata = {
        **dict(evidence.metadata),
        "source_context_job": _source_context_job_record(source_context_job),
        "source_context": _source_context_record(source_context),
        "boundary_resolution": _boundary_resolution_record(boundary_resolution),
    }
    updates: dict[str, object] = {"metadata": metadata}
    if boundary_resolution is not None:
        updates["start_boundary_score"] = boundary_resolution.start_boundary_score
        updates["end_boundary_score"] = boundary_resolution.end_boundary_score
        if "OPEN_LOOPS_PRESENT" in boundary_resolution.reason_codes:
            updates["open_loop_count"] = max(1, evidence.open_loop_count or 0)
        elif boundary_resolution.end_boundary_score >= 0.95:
            updates["open_loop_count"] = 0
        if boundary_resolution.action == DecisionAction.DROP and "NO_CONTEXT_CUES" in boundary_resolution.reason_codes:
            updates["standalone_score"] = 0.0
    return replace(evidence, **updates)


def _apply_cpa_semantic_review_from_job(
    evidence: ReviewEvidence,
    job_manifest: Mapping[str, object],
    *,
    candidate_id: str,
    output_dir: Path,
) -> ReviewEvidence:
    response_path = _cpa_semantic_response_path(job_manifest, output_dir=output_dir)
    if response_path is None:
        return evidence
    request_path = _cpa_semantic_request_path(job_manifest, output_dir=output_dir)
    if request_path is not None:
        try:
            request = load_request_artifact(request_path)
            evaluation = evaluate_cpa_semantic_response_artifact(request, response_path)
            return apply_cpa_semantic_qa_to_review_evidence(evidence, evaluation)
        except Exception as exc:
            return _apply_cpa_semantic_failure(
                evidence,
                reason_code="CPA_SEMANTIC_QA_REQUEST_INVALID",
                response_path=response_path,
                request_path=request_path,
                error=f"{type(exc).__name__}: {exc}",
            )

    # Legacy fallback for older jobs that only wrote a response artifact.  New
    # unattended jobs should always provide cpa_semantic_request_path too, so the
    # request hash/artifact-path checks in cpa_semantic_qa.py are enforced.
    try:
        response = load_cpa_semantic_review_response(response_path, expected_candidate_id=candidate_id)
        return apply_cpa_semantic_review(evidence, response, response_path=response_path)
    except CPASemanticReviewError as exc:
        return _apply_cpa_semantic_failure(
            evidence,
            reason_code=exc.reason_code,
            response_path=response_path,
            request_path=None,
            error=str(exc),
        )


def _apply_cpa_semantic_failure(
    evidence: ReviewEvidence,
    *,
    reason_code: str,
    response_path: Path,
    request_path: Path | None,
    error: str,
) -> ReviewEvidence:
    check = {
        "code": "CPA_SEMANTIC_QA",
        "pass": False,
        "severity": "BLOCK",
        "reason_codes": [reason_code],
        "evidence": {
            "request_path": str(request_path) if request_path is not None else None,
            "response_path": str(response_path),
            "error": error,
        },
    }
    metadata = {
        **dict(evidence.metadata),
        "cpa_semantic_qa": {
            "request_path": str(request_path) if request_path is not None else None,
            "response_path": str(response_path),
            "response_reason_codes": [reason_code],
            "error": error,
        },
    }
    gaps = list(evidence.evidence_gaps)
    if reason_code not in gaps:
        gaps.append(reason_code)
    return replace(evidence, checks=tuple(list(evidence.checks) + [check]), metadata=metadata, evidence_gaps=tuple(gaps))


def _cpa_semantic_request_path(job_manifest: Mapping[str, object], *, output_dir: Path) -> Path | None:
    value = job_manifest.get("cpa_semantic_request_path")
    if not isinstance(value, str) or not value:
        qa = _mapping(job_manifest.get("semantic_qa"))
        value = qa.get("cpa_request_path") if isinstance(qa.get("cpa_request_path"), str) else None
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else output_dir / path


def _cpa_semantic_response_path(job_manifest: Mapping[str, object], *, output_dir: Path) -> Path | None:
    value = job_manifest.get("cpa_semantic_response_path")
    if not isinstance(value, str) or not value:
        qa = _mapping(job_manifest.get("semantic_qa"))
        value = qa.get("cpa_response_path") if isinstance(qa.get("cpa_response_path"), str) else None
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else output_dir / path


def _merge_cpa_semantic_review_into_decision(decision, evidence: ReviewEvidence):
    cpa_metadata = _mapping(evidence.metadata.get("cpa_semantic_qa")) or _mapping(evidence.metadata.get("cpa_semantic_review"))
    if not cpa_metadata:
        return decision
    cpa_reasons = [
        reason
        for reason in evidence.evidence_gaps
        if isinstance(reason, str)
        and (
            reason.startswith("CPA_")
            or reason.startswith("CPA_SEMANTIC_")
            or reason.startswith("TERMINOLOGY_")
            or reason.startswith("CONTEXT_DEPENDENCY_")
        )
    ]
    metadata_reason = cpa_metadata.get("reason_code")
    if isinstance(metadata_reason, str):
        cpa_reasons.append(metadata_reason)
    for key in ("reason_codes", "response_reason_codes"):
        metadata_reasons = cpa_metadata.get(key)
        if isinstance(metadata_reasons, Sequence) and not isinstance(metadata_reasons, (str, bytes)):
            cpa_reasons.extend(str(reason) for reason in metadata_reasons if isinstance(reason, str))
    cpa_reasons = list(dict.fromkeys(cpa_reasons))
    if not cpa_reasons:
        return decision
    merged_reasons = tuple(dict.fromkeys(tuple(decision.reason_codes) + tuple(cpa_reasons)))
    return replace(decision, action=DecisionAction.BLOCK, reason_codes=merged_reasons)


def _merge_boundary_resolution_into_decision(decision, boundary_resolution: BoundaryResolution | None):
    if boundary_resolution is None:
        return decision
    merged_reasons = tuple(dict.fromkeys(tuple(decision.reason_codes) + tuple(boundary_resolution.reason_codes)))
    if boundary_resolution.action == DecisionAction.DROP:
        return replace(decision, action=DecisionAction.DROP, reason_codes=merged_reasons)
    if boundary_resolution.action == DecisionAction.AUTO_RECUT:
        if decision.action in {DecisionAction.BLOCK, DecisionAction.DROP}:
            return replace(decision, reason_codes=merged_reasons)
        return replace(decision, action=DecisionAction.AUTO_RECUT, reason_codes=merged_reasons)
    return replace(decision, reason_codes=merged_reasons)


_BOUNDARY_CONNECTIVE_PREFIXES = ("然后", "所以", "但是", "因为", "结果", "接着", "后来", "而且", "不过")
_BOUNDARY_PAYOFF_MARKERS = ("哈哈", "笑", "结果", "突然", "离谱", "破防", "绷", "小皇帝", "最后")
_BOUNDARY_CLOSURE_MARKERS = ("结束", "最后", "完了", "就这样", "哈哈", "笑了")
_BOUNDARY_OPEN_LOOP_MARKERS = ("为什么", "到底", "怎么", "咋", "?", "？", "问题")
_BOUNDARY_SETUP_MARKERS = ("我跟你们说一个事", "我跟你说一个事", "我给你们讲", "我跟你们讲", "有个事", "事情是这样的", "我问你们")


def _to_talk_cue(cue: SourceCue, index: int, cues: Sequence[SourceCue]) -> TalkCue:
    text = cue.text.strip()
    previous = cues[index - 1] if index > 0 else None
    has_payoff = any(marker in text for marker in _BOUNDARY_PAYOFF_MARKERS)
    ends_topic = any(marker in text for marker in _BOUNDARY_CLOSURE_MARKERS)
    starts_topic = index == 0 or (
        not text.startswith(_BOUNDARY_CONNECTIVE_PREFIXES)
        and previous is not None
        and any(marker in previous.text for marker in _BOUNDARY_CLOSURE_MARKERS)
    )
    open_loop_delta = 0
    if any(marker in text for marker in _BOUNDARY_OPEN_LOOP_MARKERS + _BOUNDARY_SETUP_MARKERS):
        open_loop_delta += 1
    if has_payoff:
        open_loop_delta -= 1
    if ends_topic:
        open_loop_delta -= 1
    return TalkCue(
        cue_id=cue.cue_id,
        start_ms=cue.source_start_ms,
        end_ms=cue.source_end_ms,
        text=text,
        starts_topic=starts_topic,
        ends_topic=ends_topic,
        has_payoff=has_payoff,
        open_loop_delta=open_loop_delta,
    )


def _source_context_job_record(job_manifest: Mapping[str, object]) -> dict[str, object]:
    return {
        "job_id": job_manifest.get("job_id"),
        "candidate_id": job_manifest.get("candidate_id"),
        "job_kind": job_manifest.get("job_kind"),
        "provider": job_manifest.get("provider"),
        "timeline": dict(_mapping(job_manifest.get("timeline"))),
        "song_boundary": dict(_mapping(job_manifest.get("song_boundary"))),
        "lyrics_alignment": dict(_mapping(job_manifest.get("lyrics_alignment"))),
        "provenance": dict(_mapping(job_manifest.get("provenance"))),
        "cpa_semantic_request_path": job_manifest.get("cpa_semantic_request_path"),
        "cpa_semantic_response_path": job_manifest.get("cpa_semantic_response_path"),
        "semantic_qa": dict(_mapping(job_manifest.get("semantic_qa"))),
    }


def _boundary_resolution_record(boundary_resolution: BoundaryResolution | None) -> dict[str, object] | None:
    if boundary_resolution is None:
        return None
    return {
        "candidate_id": boundary_resolution.candidate_id,
        "action": boundary_resolution.action.value,
        "resolved_start_ms": boundary_resolution.resolved_start_ms,
        "resolved_end_ms": boundary_resolution.resolved_end_ms,
        "start_boundary_score": boundary_resolution.start_boundary_score,
        "end_boundary_score": boundary_resolution.end_boundary_score,
        "reason_codes": list(boundary_resolution.reason_codes),
        "next_start_ms": boundary_resolution.next_start_ms,
        "next_end_ms": boundary_resolution.next_end_ms,
    }


def _recut_plan_record(
    *,
    source_video: Path,
    candidate_id: str,
    boundary_resolution: BoundaryResolution | None,
    output_dir: Path,
) -> dict[str, object] | None:
    if boundary_resolution is None or boundary_resolution.action not in {DecisionAction.AUTO_RECUT, DecisionAction.AUTO_UPLOAD}:
        return None
    if boundary_resolution.action == DecisionAction.AUTO_RECUT:
        start_ms = boundary_resolution.next_start_ms if boundary_resolution.next_start_ms is not None else boundary_resolution.resolved_start_ms
        end_ms = boundary_resolution.next_end_ms if boundary_resolution.next_end_ms is not None else boundary_resolution.resolved_end_ms
    else:
        start_ms = boundary_resolution.resolved_start_ms
        end_ms = boundary_resolution.resolved_end_ms
    output_media = output_dir / "replacement_recuts" / f"{candidate_id}.recut.mp4"
    if end_ms <= start_ms:
        return {
            "status": "BLOCKED",
            "reason_codes": ["INVALID_RECUT_RANGE"],
            "start_ms": start_ms,
            "end_ms": end_ms,
            "output_media_path": str(output_media),
            "command": [],
        }
    duration_ms = end_ms - start_ms
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-i",
        str(source_video),
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-c",
        "copy",
        str(output_media),
    ]
    return {
        "status": "PLANNED",
        "reason_codes": [],
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": duration_ms,
        "output_media_path": str(output_media),
        "command": command,
    }


def _materialize_recut_record(
    *,
    source_video: Path,
    candidate_id: str,
    boundary_resolution: BoundaryResolution | None,
    output_dir: Path,
    cues: Sequence[SourceCue],
    run_ffmpeg: bool,
) -> dict[str, object] | None:
    plan = _recut_plan_record(
        source_video=source_video,
        candidate_id=candidate_id,
        boundary_resolution=boundary_resolution,
        output_dir=output_dir,
    )
    if plan is None:
        return None
    if plan.get("status") != "PLANNED":
        return dict(plan)
    start_ms = _int(plan.get("start_ms"), 0)
    end_ms = _int(plan.get("end_ms"), start_ms)
    duration_ms = max(0, end_ms - start_ms)
    media_path = Path(str(plan["output_media_path"]))
    subtitle_path = media_path.with_suffix(".srt")
    manifest_path = media_path.with_suffix(".manifest.json")
    render_qa_path = media_path.with_suffix(".render_qa.json")
    media_path.parent.mkdir(parents=True, exist_ok=True)

    _write_source_range_srt(cues, start_ms, end_ms, subtitle_path)

    reason_codes: list[str] = []
    if run_ffmpeg:
        completed = subprocess.run([str(part) for part in plan["command"]], check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            reason_codes.append("FFMPEG_RECUT_FAILED")
            manifest = {
                "schema_version": "materialized-recut.v1",
                "status": "RETRY_INFRA",
                "reason_codes": reason_codes,
                "requested_range": {"start_ms": start_ms, "end_ms": end_ms, "duration_ms": duration_ms},
                "command": plan["command"],
                "stderr_tail": completed.stderr[-1000:],
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return {
                "status": "RETRY_INFRA",
                "reason_codes": reason_codes,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": duration_ms,
                "media_path": str(media_path),
                "subtitle_path": str(subtitle_path),
                "manifest_path": str(manifest_path),
                "dry_run_placeholder": False,
                "artifact_hashes": {},
            }
    else:
        media_path.write_bytes(b"dry-run materialized recut placeholder\n")

    artifact_hashes = {
        "video_sha256": "sha256:" + _sha256(media_path),
        "subtitle_sha256": "sha256:" + _sha256(subtitle_path),
    }
    render_qa = _evaluate_materialized_recut_render_qa(
        candidate_id=candidate_id,
        media_path=media_path,
        render_qa_path=render_qa_path,
        requested_start_ms=start_ms,
        requested_end_ms=end_ms,
        enabled=run_ffmpeg,
    )
    accurate_rerender_used = False
    accurate_command: list[str] | None = None
    if (
        run_ffmpeg
        and boundary_resolution.action == DecisionAction.AUTO_UPLOAD
        and _render_qa_actual_cut_error_ms(render_qa) is not None
        and _render_qa_actual_cut_error_ms(render_qa) > 100
    ):
        accurate_command = _accurate_reencode_recut_command(
            source_video=source_video,
            output_media=media_path,
            start_ms=start_ms,
            duration_ms=duration_ms,
        )
        completed = subprocess.run(accurate_command, check=False, capture_output=True, text=True)
        if completed.returncode == 0:
            accurate_rerender_used = True
            artifact_hashes["video_sha256"] = "sha256:" + _sha256(media_path)
            render_qa = _evaluate_materialized_recut_render_qa(
                candidate_id=candidate_id,
                media_path=media_path,
                render_qa_path=render_qa_path,
                requested_start_ms=start_ms,
                requested_end_ms=end_ms,
                enabled=True,
            )
        else:
            reason_codes.append("FFMPEG_ACCURATE_RECUT_FAILED")
    manifest = {
        "schema_version": "materialized-recut.v1",
        "status": "MATERIALIZED",
        "reason_codes": reason_codes,
        "candidate_id": candidate_id,
        "source_video_path": str(source_video),
        "requested_range": {"start_ms": start_ms, "end_ms": end_ms, "duration_ms": duration_ms},
        "media_path": str(media_path),
        "subtitle_path": str(subtitle_path),
        "command": plan["command"],
        "accurate_command": accurate_command,
        "dry_run_placeholder": not run_ffmpeg,
        "accurate_rerender_used": accurate_rerender_used,
        "artifact_hashes": artifact_hashes,
        "render_qa": render_qa,
        "render_qa_path": str(render_qa_path) if render_qa is not None else None,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "MATERIALIZED",
        "reason_codes": reason_codes,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": duration_ms,
        "media_path": str(media_path),
        "subtitle_path": str(subtitle_path),
        "manifest_path": str(manifest_path),
        "dry_run_placeholder": not run_ffmpeg,
        "accurate_rerender_used": accurate_rerender_used,
        "accurate_command": accurate_command,
        "artifact_hashes": artifact_hashes,
        "render_qa": render_qa,
        "render_qa_path": str(render_qa_path) if render_qa is not None else None,
    }


def _render_qa_actual_cut_error_ms(render_qa: Mapping[str, object] | None) -> float | None:
    if not isinstance(render_qa, Mapping):
        return None
    evidence = _mapping(render_qa.get("evidence"))
    value = evidence.get("actual_cut_error_ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _accurate_reencode_recut_command(
    *,
    source_video: Path,
    output_media: Path,
    start_ms: int,
    duration_ms: int,
) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-i",
        str(source_video),
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_media),
    ]


def _evaluate_materialized_recut_render_qa(
    *,
    candidate_id: str,
    media_path: Path,
    render_qa_path: Path,
    requested_start_ms: int,
    requested_end_ms: int,
    enabled: bool,
) -> dict[str, object] | None:
    if not enabled:
        check = {
            "code": "ACTUAL_CUT_ERROR",
            "pass": True,
            "severity": "PASS",
            "evidence": {
                "requested_start_ms": requested_start_ms,
                "requested_end_ms": requested_end_ms,
                "actual_start_ms": requested_start_ms,
                "actual_end_ms": requested_end_ms,
                "start_error_ms": 0,
                "end_error_ms": 0,
                "actual_cut_error_ms": 0,
                "threshold_ms": 100,
                "dry_run_placeholder": True,
            },
        }
        payload = {
            "schema_version": "materialized-render-qa.v1",
            "candidate_id": candidate_id,
            "media_path": str(media_path),
            "check": check,
            "dry_run_placeholder": True,
        }
        render_qa_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return check
    metadata = _probe_rendered_timeline_metadata(
        media_path,
        requested_start_ms=requested_start_ms,
    )
    qa = evaluate_render_pts(
        RenderRequest(
            candidate_id=candidate_id,
            requested_start_ms=requested_start_ms,
            requested_end_ms=requested_end_ms,
        ),
        metadata,
    )
    payload = {
        "schema_version": "materialized-render-qa.v1",
        "candidate_id": candidate_id,
        "media_path": str(media_path),
        "check": qa.to_manifest_check(),
    }
    render_qa_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return qa.to_manifest_check()


def _probe_rendered_timeline_metadata(
    media_path: Path,
    *,
    requested_start_ms: int,
) -> RenderedTimelineMetadata | None:
    if not media_path.is_file():
        return None
    packet_json = _ffprobe_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time,duration_time",
            "-of",
            "json",
            str(media_path),
        ]
    )
    packets = packet_json.get("packets")
    if not isinstance(packets, list) or not packets:
        return None
    first_packet = packets[0] if isinstance(packets[0], Mapping) else None
    last_packet = packets[-1] if isinstance(packets[-1], Mapping) else None
    if first_packet is None or last_packet is None:
        return None
    first_pts_seconds = _float_or_none(first_packet.get("pts_time"))
    last_pts_seconds = _float_or_none(last_packet.get("pts_time"))
    last_duration_seconds = _float_or_none(last_packet.get("duration_time"))
    if first_pts_seconds is None or last_pts_seconds is None:
        return None
    if last_duration_seconds is None:
        last_duration_seconds = 0.0
    return RenderedTimelineMetadata(
        actual_start_ms=requested_start_ms + int(round(first_pts_seconds * 1000.0)),
        actual_end_ms=requested_start_ms + int(round((last_pts_seconds + last_duration_seconds) * 1000.0)),
    )


def _apply_materialized_recut_render_qa(
    evidence: ReviewEvidence,
    materialized_recut: Mapping[str, object] | None,
) -> ReviewEvidence:
    if not isinstance(materialized_recut, Mapping):
        return evidence
    render_qa = materialized_recut.get("render_qa")
    if not isinstance(render_qa, Mapping):
        return evidence
    qa_evidence = _mapping(render_qa.get("evidence"))
    actual_cut_error_ms = qa_evidence.get("actual_cut_error_ms")
    checks = tuple(evidence.checks) + (dict(render_qa),)
    metadata = {
        **dict(evidence.metadata),
        "materialized_recut": {
            "status": materialized_recut.get("status"),
            "media_path": materialized_recut.get("media_path"),
            "subtitle_path": materialized_recut.get("subtitle_path"),
            "manifest_path": materialized_recut.get("manifest_path"),
            "render_qa": dict(render_qa),
            "render_qa_path": materialized_recut.get("render_qa_path"),
        },
    }
    updates: dict[str, object] = {"checks": checks, "metadata": metadata}
    if isinstance(actual_cut_error_ms, (int, float)) and not isinstance(actual_cut_error_ms, bool):
        updates["actual_cut_error_ms"] = float(actual_cut_error_ms)
    return replace(evidence, **updates)


def _write_source_range_srt(cues: Sequence[SourceCue], start_ms: int, end_ms: int, output_path: Path) -> None:
    rows: list[str] = []
    index = 1
    for cue in cues:
        clipped_start_ms = max(cue.source_start_ms, start_ms)
        clipped_end_ms = min(cue.source_end_ms, end_ms)
        if clipped_end_ms <= clipped_start_ms:
            continue
        relative_start_ms = clipped_start_ms - start_ms
        relative_end_ms = clipped_end_ms - start_ms
        rows.append(
            f"{index}\n{_format_srt_time(relative_start_ms)} --> {_format_srt_time(relative_end_ms)}\n{cue.text.strip()}\n"
        )
        index += 1
    output_path.write_text("\n".join(rows).rstrip() + ("\n" if rows else ""), encoding="utf-8")


def _format_srt_time(ms: int) -> str:
    ms = max(0, int(ms))
    millis = ms % 1000
    total_seconds = ms // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _run_source_context_agy(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> AgyExecutionResult:
    from scripts.gemini_slice_jingting import AGY_MODEL, run_agy

    job_dir = run_agy(str(media_path), str(draft_srt_path), str(output_srt_path))
    return AgyExecutionResult(
        provider="agy",
        model=AGY_MODEL,
        agy_rc=0,
        provider_fallback_used=False,
        provider_request_id=job_dir,
    )


def _default_source_context_job(*, source_video: Path, source_srt: Path, room_id: str | None) -> dict[str, object]:
    cues = _parse_srt(source_srt)
    context_end_ms = max((cue.source_end_ms for cue in cues), default=0)
    return {
        "schema_version": "source-context-jingting-job.v1",
        "job_kind": "SOURCE_CONTEXT_JINGTING",
        "job_id": f"scj_{source_video.stem}",
        "candidate_id": source_video.stem,
        "room_id": room_id,
        "timeline": {"context_start_ms": 0, "context_duration_ms": context_end_ms},
        "input": {"source_sha256": None},
    }


def _duration_seconds(cues: Sequence[SourceCue], default: float) -> float:
    if not cues:
        return default
    start_ms = min(cue.source_start_ms for cue in cues)
    end_ms = max(cue.source_end_ms for cue in cues)
    return max(0.001, (end_ms - start_ms) / 1000.0)


def _ffprobe_json(command: Sequence[str]) -> dict[str, object]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        return {}
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _float(value: object, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _first_int(*values: object) -> int | None:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


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


def _readme(summary: Mapping[str, object]) -> str:
    return "# Auto Review Shadow Pipeline\n\n```json\n" + json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n```\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local auto-review shadow evidence pipeline.")
    parser.add_argument("--review-package", type=Path)
    parser.add_argument("--source-video", type=Path)
    parser.add_argument("--source-srt", type=Path, help="Full source-level draft ASR/SRT for live-source shadow integration.")
    parser.add_argument("--refined-srt", type=Path, help="Precomputed agy/jingting refined context SRT for dry-run integration.")
    parser.add_argument("--source-context-job", type=Path, help="Optional source-context job manifest JSON.")
    parser.add_argument("--room-id")
    parser.add_argument("--title")
    parser.add_argument("--agy-model")
    parser.add_argument("--agy-rc", type=int)
    parser.add_argument("--agy-fallback-used", choices=("true", "false", "unknown"), default="unknown")
    parser.add_argument("--skip-ffmpeg", action="store_true", help="Use executor dry-run media placeholder instead of invoking ffmpeg.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-upload", action="store_true", help="Reserved; default shadow mode never uploads.")
    args = parser.parse_args(argv)
    source_context_job = _read_json(args.source_context_job) if args.source_context_job else None
    if source_context_job is not None and not isinstance(source_context_job, Mapping):
        raise ValueError("--source-context-job must contain a JSON object")
    agy_result = None
    if args.agy_model or args.agy_rc is not None or args.agy_fallback_used != "unknown":
        agy_result = AgyExecutionResult(
            provider="agy",
            model=args.agy_model,
            agy_rc=args.agy_rc,
            provider_fallback_used={"true": True, "false": False, "unknown": None}[args.agy_fallback_used],
        )
    summary = run_shadow_pipeline(
        review_package=args.review_package,
        source_video=args.source_video,
        source_srt=args.source_srt,
        refined_srt=args.refined_srt,
        source_context_job=source_context_job if isinstance(source_context_job, Mapping) else None,
        agy_result=agy_result,
        room_id=args.room_id,
        title=args.title,
        output_dir=args.output_dir,
        no_upload=not args.allow_upload,
        source_context_run_ffmpeg=not args.skip_ffmpeg,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
