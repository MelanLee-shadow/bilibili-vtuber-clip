from __future__ import annotations

import argparse
import base64
import difflib
import re
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
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
# _editorial_score is deliberately shared with the analyzer so the semantic
# authority override cannot drift from the canonical editorial formula.
from src.autoslice.content_evidence import _editorial_score, analyze_content_evidence
from src.autoslice.cpa_semantic_qa import (
    apply_cpa_semantic_qa_to_review_evidence,
    evaluate_cpa_semantic_response_artifact,
    load_request_artifact,
)
from src.autoslice.render_qa import RenderRequest, RenderedTimelineMetadata, evaluate_render_pts
from src.autoslice.review_evidence import ReviewEvidence, SourceCue, to_candidate_review
from src.autoslice.llm_client import LlmCall, LlmConfig, build_llm_call, extract_json_object
from src.autoslice.song_repair import LrcProvider, SongRepairResult, attempt_song_repair, build_netease_lrc_provider
from src.autoslice.source_integrity import MediaSegmentObservation, build_source_range_ledger, plan_bilibili_replay_compensation
from src.autoslice.source_context_executor import AgyExecutionResult, SourceContextExecutionResult, execute_source_context_job
from src.autoslice.source_context_planner import JingtingJobProvenance, plan_source_context_jingting_jobs
from src.autoslice.subtitle_timing_qa import SpeechSpansProvider, sanitize_cue_timing
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
    lrc_provider: LrcProvider | None = None,
    song_hint_llm_call: LlmCall | None = None,
    burn_preview: bool = False,
    publish_staging: bool = False,
    title_llm_call: LlmCall | None = None,
    art_direction_llm_call: LlmCall | None = None,
    speech_spans_provider: SpeechSpansProvider | None = None,
    fresh_talk_transcriber: Callable[[Path], str] | None = None,
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
            lrc_provider=lrc_provider,
            song_hint_llm_call=song_hint_llm_call,
            burn_preview=burn_preview,
            publish_staging=publish_staging,
            title_llm_call=title_llm_call,
            art_direction_llm_call=art_direction_llm_call,
            speech_spans_provider=speech_spans_provider,
            fresh_talk_transcriber=fresh_talk_transcriber,
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
    lrc_provider: LrcProvider | None = None,
    song_hint_llm_call: LlmCall | None = None,
    burn_preview: bool = False,
    publish_staging: bool = False,
    title_llm_call: LlmCall | None = None,
    art_direction_llm_call: LlmCall | None = None,
    speech_spans_provider: SpeechSpansProvider | None = None,
    fresh_talk_transcriber: Callable[[Path], str] | None = None,
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
    job_manifest, song_repair_result = _attempt_song_repair_stage(
        job_manifest,
        cues,
        candidate_id=candidate_id,
        output_dir=output_dir,
        lrc_provider=lrc_provider,
        hint_llm_call=song_hint_llm_call,
    )
    boundary_resolution = _resolve_live_source_boundary(job_manifest, cues, output_dir=output_dir)
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
        output_dir=output_dir,
    )
    if song_repair_result is not None:
        evidence = replace(
            evidence,
            metadata={**dict(evidence.metadata), "song_repair": song_repair_result.to_manifest()},
        )
    duration_seconds = _duration_seconds(cues, context_duration_ms / 1000.0)
    evidence = apply_style_profile(evidence, _default_lidousha_profile(), title=title, duration_seconds=duration_seconds)
    counts["candidates_with_style_profile_evidence"] = 1
    lyric_timeline_loaded = _load_lyric_timeline(job_manifest, output_dir=output_dir)
    materialized_recut = _materialize_recut_record(
        source_video=source_video,
        candidate_id=candidate_id,
        boundary_resolution=boundary_resolution,
        output_dir=output_dir,
        cues=cues,
        run_ffmpeg=source_context_run_ffmpeg,
        lyric_timeline=lyric_timeline_loaded[0] if lyric_timeline_loaded else None,
        lyric_offset_ms=lyric_timeline_loaded[1] if lyric_timeline_loaded else None,
        speech_spans_provider=speech_spans_provider,
        fresh_talk_transcriber=fresh_talk_transcriber,
    )
    if burn_preview:
        materialized_recut = _burn_preview_subtitles(materialized_recut, run_ffmpeg=source_context_run_ffmpeg)
    if publish_staging:
        materialized_recut = _stage_publish_draft(
            materialized_recut,
            candidate_id=candidate_id,
            title=title,
            cues=cues,
            run_ffmpeg=source_context_run_ffmpeg,
            title_llm_call=title_llm_call,
            art_direction_llm_call=art_direction_llm_call,
        )
    evidence = _apply_materialized_recut_render_qa(evidence, materialized_recut)
    evidence = _apply_cpa_semantic_review_from_job(evidence, job_manifest, output_dir=output_dir)
    evidence = _apply_semantic_authority_evidence(evidence, job_manifest)
    provenance = _read_jingting_provenance_path(Path(source_context.jingting_manifest_path) if source_context.jingting_manifest_path else None)
    live_review_required = _read_review_required_marker(
        Path(source_context.review_required_path) if source_context.review_required_path else None
    )
    decision = review_candidate(
        to_candidate_review(
            evidence,
            provenance,
            jingting_done=source_context.jingting_done,
            review_required=live_review_required,
        )
    )
    decision = _merge_cpa_semantic_review_into_decision(decision, evidence)
    decision = _merge_boundary_resolution_into_decision(decision, boundary_resolution)
    # Last: an unverified full-song claim must surface as BLOCK regardless of how
    # the talk-boundary fallback would otherwise dispose of the candidate.
    decision = _merge_song_proof_into_decision(decision, evidence)
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
    *,
    output_dir: Path,
) -> BoundaryResolution | None:
    anchor = _job_anchor_candidate(job_manifest)
    if anchor is None or not cues:
        return None
    song_boundary_resolution = _resolve_song_boundary(job_manifest, anchor, output_dir=output_dir)
    if song_boundary_resolution is not None:
        return song_boundary_resolution
    if all(cue.kind == "singing" for cue in cues):
        return None
    talk_cues = [_to_talk_cue(cue, index, cues) for index, cue in enumerate(cues)]
    resolution = resolve_talk_boundary(anchor, talk_cues)
    if str(job_manifest.get("boundary_authority") or "") == "semantic":
        # Semantic lanes (LLM recall / viewer-context expansion) own their
        # bounds: interestingness and context completeness are judged
        # semantically by CPA QA + the viewer-context check, not by closure
        # keywords.  The keyword verdict stays visible as ADVISORY_* codes but
        # can no longer DROP the candidate or rewrite its window.
        return BoundaryResolution(
            candidate_id=anchor.candidate_id,
            action=DecisionAction.AUTO_RECUT,
            resolved_start_ms=anchor.anchor_start_ms,
            resolved_end_ms=anchor.anchor_end_ms,
            start_boundary_score=resolution.start_boundary_score,
            end_boundary_score=resolution.end_boundary_score,
            reason_codes=("BOUNDARY_SEMANTIC_AUTHORITY",)
            + tuple(f"ADVISORY_{code}" for code in resolution.reason_codes),
            next_start_ms=anchor.anchor_start_ms,
            next_end_ms=anchor.anchor_end_ms,
        )
    return resolution


def _job_anchor_candidate(job_manifest: Mapping[str, object]) -> AnchorCandidate | None:
    timeline = _mapping(job_manifest.get("timeline"))
    anchor_start_ms = _first_int(timeline.get("anchor_start_ms"), job_manifest.get("anchor_start_ms"))
    anchor_end_ms = _first_int(timeline.get("anchor_end_ms"), job_manifest.get("anchor_end_ms"))
    if anchor_start_ms is None or anchor_end_ms is None or anchor_end_ms < anchor_start_ms:
        return None
    candidate_id = str(job_manifest.get("candidate_id") or "source-context")
    return AnchorCandidate(candidate_id=candidate_id, anchor_start_ms=anchor_start_ms, anchor_end_ms=anchor_end_ms)


def _resolve_song_boundary(
    job_manifest: Mapping[str, object],
    anchor: AnchorCandidate,
    *,
    output_dir: Path,
) -> BoundaryResolution | None:
    song_boundary = _mapping(job_manifest.get("song_boundary"))
    lyrics_alignment = _mapping(job_manifest.get("lyrics_alignment"))
    if not _song_boundary_ready(song_boundary):
        return None
    if _verify_lyrics_alignment_proof(lyrics_alignment, output_dir=output_dir) is not None:
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


def _verify_lyrics_alignment_proof(lyrics_alignment: Mapping[str, object], *, output_dir: Path) -> str | None:
    """Fail-closed proof check for a READY lyrics-alignment claim.

    A ``READY`` status string alone must never raise scores; the claim has to
    carry provider/model/source provenance plus an alignment report that exists
    on disk and matches its declared sha256.  Returns ``None`` when the proof
    verifies, else a human-readable error.
    """

    if lyrics_alignment.get("status") != "READY":
        return "lyrics_alignment.status is not READY"
    for field in ("provider", "model"):
        value = lyrics_alignment.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"lyrics_alignment.{field} is missing"
    source = lyrics_alignment.get("source")
    if not isinstance(source, str) or not source.strip():
        source = lyrics_alignment.get("external_lrc")
    if not isinstance(source, str) or not source.strip():
        return "lyrics_alignment.source/external_lrc is missing"
    report_value = lyrics_alignment.get("alignment_report_path")
    if not isinstance(report_value, str) or not report_value:
        return "lyrics_alignment.alignment_report_path is missing"
    report_path = Path(report_value)
    if not report_path.is_absolute() and not report_path.is_file():
        report_path = output_dir / report_path
    if not report_path.is_file():
        return f"alignment report does not exist: {report_path}"
    declared_sha = lyrics_alignment.get("alignment_report_sha256")
    if not isinstance(declared_sha, str) or not declared_sha:
        return "lyrics_alignment.alignment_report_sha256 is missing"
    normalized_sha = declared_sha.lower().removeprefix("sha256:")
    if len(normalized_sha) != 64 or any(ch not in "0123456789abcdef" for ch in normalized_sha):
        return "lyrics_alignment.alignment_report_sha256 is malformed"
    actual_sha = _sha256(report_path)
    if actual_sha != normalized_sha:
        return "alignment report sha256 mismatch"
    return None


def _apply_live_source_machine_evidence(
    evidence: ReviewEvidence,
    *,
    source_context_job: Mapping[str, object],
    source_context: SourceContextExecutionResult,
    title: str,
    output_dir: Path,
) -> ReviewEvidence:
    checks = list(evidence.checks)
    updates: dict[str, object] = {}

    song_boundary = _mapping(source_context_job.get("song_boundary"))
    lyrics_alignment = _mapping(source_context_job.get("lyrics_alignment"))
    song_boundary_claimed = _song_boundary_ready(song_boundary)
    lyrics_proof_error = _verify_lyrics_alignment_proof(lyrics_alignment, output_dir=output_dir)
    full_song_evidence_ready = song_boundary_claimed and lyrics_proof_error is None
    if song_boundary_claimed and lyrics_proof_error is not None:
        checks.append(
            {
                "code": "SONG_PROOF_UNVERIFIED",
                "pass": False,
                "severity": "BLOCK",
                "evidence": {
                    "error": lyrics_proof_error,
                    "song_boundary_status": song_boundary.get("status"),
                    "lyrics_alignment": dict(lyrics_alignment),
                },
            }
        )
        updates["evidence_gaps"] = tuple(dict.fromkeys(tuple(evidence.evidence_gaps) + ("SONG_PROOF_UNVERIFIED",)))
        updates["metadata"] = {
            **dict(evidence.metadata),
            "song_proof": {"verified": False, "error": lyrics_proof_error},
        }
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


def _apply_semantic_authority_evidence(
    evidence: ReviewEvidence,
    job_manifest: Mapping[str, object],
) -> ReviewEvidence:
    """For semantic-lane candidates, the CPA judge owns the semantic scores.

    Keyword-derived payoff/standalone/boundary/editorial scores structurally
    miss reactive humor (danmaku banter, on-screen reactions) — Ivan's rule is
    that interestingness and context completeness are semantic judgments.  So
    when the passing CPA verdict affirms a dimension, it supersedes the
    keyword score for that dimension.  Machine-verifiable gates (timing, cut
    error, duplicates, song proof, jingting provenance) keep full authority,
    and a failing CPA verdict still blocks via its own reason codes.
    """

    if str(job_manifest.get("boundary_authority") or "") != "semantic":
        return evidence
    cpa_check = next(
        (check for check in evidence.checks if _mapping(check).get("code") == "CPA_SEMANTIC_QA"),
        None,
    )
    if cpa_check is None:
        return evidence
    check_evidence = _mapping(_mapping(cpa_check).get("evidence"))

    semantic_complete = check_evidence.get("semantic_complete") is True
    hook_score = check_evidence.get("title_hook_score")
    hook_score = float(hook_score) if isinstance(hook_score, (int, float)) and not isinstance(hook_score, bool) else None
    viewer_context = _mapping(check_evidence.get("viewer_context"))
    viewer_context_ok = viewer_context.get("viewer_context_ok")
    if not isinstance(viewer_context_ok, bool):
        dependency = check_evidence.get("context_dependency_score")
        viewer_context_ok = (
            isinstance(dependency, (int, float)) and not isinstance(dependency, bool) and float(dependency) <= 0.45
        )

    updates: dict[str, object] = {}
    if semantic_complete:
        updates["start_boundary_score"] = max(evidence.start_boundary_score or 0.0, 0.97)
        updates["end_boundary_score"] = max(evidence.end_boundary_score or 0.0, 0.98)
        updates["open_loop_count"] = 0
    if viewer_context_ok:
        updates["standalone_score"] = max(evidence.standalone_score or 0.0, 0.94)
    if hook_score is not None and hook_score >= 0.60:
        updates["payoff_score"] = max(evidence.payoff_score or 0.0, 0.96)
    if not updates:
        return evidence

    updates["editorial_score"] = max(
        evidence.editorial_score or 0.0,
        _editorial_score(
            start_score=float(updates.get("start_boundary_score", evidence.start_boundary_score or 0.0)),
            end_score=float(updates.get("end_boundary_score", evidence.end_boundary_score or 0.0)),
            standalone_score=float(updates.get("standalone_score", evidence.standalone_score or 0.0)),
            payoff_score=float(updates.get("payoff_score", evidence.payoff_score or 0.0)),
            song_overlap=evidence.foreground_song_overlap_seconds or 0.0,
            song_complete=bool(evidence.song_complete),
            lyrics_ready=bool(evidence.lyrics_alignment_ready),
        ),
    )
    updates["metadata"] = {
        **dict(evidence.metadata),
        "semantic_authority": {
            "applied": True,
            "source": "cpa_semantic_qa",
            "semantic_complete": semantic_complete,
            "viewer_context_ok": bool(viewer_context_ok),
            "title_hook_score": hook_score,
            "overridden_fields": sorted(key for key in updates if key != "metadata"),
        },
    }
    return replace(evidence, **updates)


def _apply_cpa_semantic_review_from_job(
    evidence: ReviewEvidence,
    job_manifest: Mapping[str, object],
    *,
    output_dir: Path,
) -> ReviewEvidence:
    request_path = _cpa_semantic_request_path(job_manifest, output_dir=output_dir)
    response_path = _cpa_semantic_response_path(job_manifest, output_dir=output_dir)
    if response_path is None:
        if _cpa_semantic_optional(job_manifest):
            return evidence
        return _apply_cpa_semantic_failure(
            evidence,
            reason_code="CPA_SEMANTIC_QA_REQUIRED",
            response_path=None,
            request_path=request_path,
            error="cpa_semantic_response_path is required unless cpa_optional=true",
        )
    if request_path is None:
        # A response without its request artifact cannot be hash-bound to what
        # the selector actually asked; the legacy response-only loader is gone.
        return _apply_cpa_semantic_failure(
            evidence,
            reason_code="CPA_SEMANTIC_QA_REQUEST_REQUIRED",
            response_path=response_path,
            request_path=None,
            error="cpa_semantic_request_path is required so the response can be verified against the request hash",
        )
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


def _apply_cpa_semantic_failure(
    evidence: ReviewEvidence,
    *,
    reason_code: str,
    response_path: Path | None,
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
            "response_path": str(response_path) if response_path is not None else None,
            "error": error,
        },
    }
    metadata = {
        **dict(evidence.metadata),
        "cpa_semantic_qa": {
            "request_path": str(request_path) if request_path is not None else None,
            "response_path": str(response_path) if response_path is not None else None,
            "response_reason_codes": [reason_code],
            "error": error,
        },
    }
    gaps = list(evidence.evidence_gaps)
    if reason_code not in gaps:
        gaps.append(reason_code)
    return replace(evidence, checks=tuple(list(evidence.checks) + [check]), metadata=metadata, evidence_gaps=tuple(gaps))


def _cpa_semantic_optional(job_manifest: Mapping[str, object]) -> bool:
    value = job_manifest.get("cpa_optional")
    if isinstance(value, bool):
        return value
    qa = _mapping(job_manifest.get("semantic_qa"))
    nested = qa.get("cpa_optional")
    return nested if isinstance(nested, bool) else False


def _cpa_semantic_request_path(job_manifest: Mapping[str, object], *, output_dir: Path) -> Path | None:
    value = job_manifest.get("cpa_semantic_request_path")
    if not isinstance(value, str) or not value:
        qa = _mapping(job_manifest.get("semantic_qa"))
        value = qa.get("cpa_request_path") if isinstance(qa.get("cpa_request_path"), str) else None
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() or path.is_file() else output_dir / path


def _cpa_semantic_response_path(job_manifest: Mapping[str, object], *, output_dir: Path) -> Path | None:
    value = job_manifest.get("cpa_semantic_response_path")
    if not isinstance(value, str) or not value:
        qa = _mapping(job_manifest.get("semantic_qa"))
        value = qa.get("cpa_response_path") if isinstance(qa.get("cpa_response_path"), str) else None
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() or path.is_file() else output_dir / path


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


def _merge_song_proof_into_decision(decision, evidence: ReviewEvidence):
    if "SONG_PROOF_UNVERIFIED" not in evidence.evidence_gaps:
        return decision
    merged_reasons = tuple(dict.fromkeys(tuple(decision.reason_codes) + ("SONG_PROOF_UNVERIFIED",)))
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
        "selector_stage": job_manifest.get("selector_stage"),
        "boundary_authority": job_manifest.get("boundary_authority"),
        "viewer_context_expansion": dict(_mapping(job_manifest.get("viewer_context_expansion"))) or None,
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


def _attempt_song_repair_stage(
    job_manifest: Mapping[str, object],
    cues: Sequence[SourceCue],
    *,
    candidate_id: str,
    output_dir: Path,
    lrc_provider: LrcProvider | None,
    hint_llm_call: LlmCall | None = None,
) -> tuple[Mapping[str, object], SongRepairResult | None]:
    """Repair-first: try to earn the full-song proof before review can BLOCK.

    Returns the (possibly repaired) job manifest plus the repair result for
    evidence.  A repaired manifest carries a song_boundary/lyrics_alignment
    pair that passes the hash-bound proof gate on its own merits.
    """

    if not _job_is_song_candidate(job_manifest):
        return job_manifest, None
    song_boundary = _mapping(job_manifest.get("song_boundary"))
    lyrics_alignment = _mapping(job_manifest.get("lyrics_alignment"))
    if _song_boundary_ready(song_boundary) and _verify_lyrics_alignment_proof(lyrics_alignment, output_dir=output_dir) is None:
        return job_manifest, None
    anchor = _job_anchor_candidate(job_manifest)
    timeline = _mapping(job_manifest.get("timeline"))
    source_duration_ms = (
        _int(timeline.get("source_duration_ms"), 0)
        or _int(timeline.get("context_end_ms"), 0)
        or max((cue.source_end_ms for cue in cues), default=0)
    )
    result = attempt_song_repair(
        candidate_id=candidate_id,
        cues=cues,
        anchor_start_ms=anchor.anchor_start_ms if anchor else 0,
        anchor_end_ms=anchor.anchor_end_ms if anchor else source_duration_ms,
        source_duration_ms=source_duration_ms,
        output_dir=output_dir / "song_repair",
        lrc_provider=lrc_provider,
        hint_llm_call=hint_llm_call,
    )
    if result.repaired and result.song_boundary and result.lyrics_alignment:
        repaired_job = {
            **dict(job_manifest),
            "song_boundary": dict(result.song_boundary),
            "lyrics_alignment": dict(result.lyrics_alignment),
        }
        return repaired_job, result
    return job_manifest, result


def _job_is_song_candidate(job_manifest: Mapping[str, object]) -> bool:
    if job_manifest.get("song_candidate") is True:
        return True
    if job_manifest.get("requires_full_source_song_boundary_redo") is True:
        return True
    if job_manifest.get("content_type_hint") == "song":
        return True
    # a claimed-but-unproven song boundary also deserves a repair attempt
    return _song_boundary_ready(_mapping(job_manifest.get("song_boundary")))


LIDOUSHA_COVER_WORKFLOW = "cpa-openai-compatible-image-edit-cover-plus-approved-local-title-overlay"


def _video_dimensions(path: Path) -> tuple[int | None, int | None]:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    parts = completed.stdout.strip().split(",")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None, None


def _burn_preview_subtitles(materialized_recut: dict[str, object] | None, *, run_ffmpeg: bool) -> dict[str, object] | None:
    """Burn the recut subtitles into a preview render (shadow artifact, never published)."""

    if not materialized_recut or materialized_recut.get("status") != "MATERIALIZED":
        return materialized_recut
    media_path = Path(str(materialized_recut["media_path"]))
    subtitle_path = Path(str(materialized_recut["subtitle_path"]))
    ass_path = media_path.with_suffix(".final-sapphire72.ass")
    burned_path = media_path.with_suffix(".burned-final-sapphire72.mp4")
    record = dict(materialized_recut)
    _write_lidousha_sapphire_ass_from_srt(subtitle_path, ass_path)
    if not run_ffmpeg:
        burned_path.write_bytes(b"dry-run burned preview placeholder\n")
        record["burned_preview"] = {"status": "DRY_RUN", "path": str(burned_path), "ass_path": str(ass_path)}
        return record
    escaped_subtitle = _escape_ffmpeg_filter_path(ass_path)
    fontsdir = _lidousha_fontsdir(media_path)
    sub = f"subtitles='{escaped_subtitle}'"
    if fontsdir is not None:
        sub += f":fontsdir='{_escape_ffmpeg_filter_path(fontsdir)}'"
    w, h = _video_dimensions(media_path)
    vertical = w is not None and h is not None and h > w
    if vertical:
        # Vertical stream (e.g. 1080x1920) → horizontal 1920x1080 for Bilibili:
        # center the vertical video, fill the sides with a scaled + blurred
        # copy of itself (pillarbox), then burn subtitles on the 16:9 frame so
        # the subtitle is sized/positioned for 1920x1080.
        fc = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,gblur=sigma=24,eq=brightness=-0.06[bgb];"
            "[fg]scale=-2:1080[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:0[pad];"
            f"[pad]{sub}[v]"
        )
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media_path),
            "-filter_complex", fc, "-map", "[v]", "-map", "0:a?", "-c:a", "copy", str(burned_path),
        ]
    else:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media_path),
            "-vf", sub, "-c:a", "copy", str(burned_path),
        ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0 or not burned_path.is_file():
        record["burned_preview"] = {
            "status": "FAILED",
            "path": str(burned_path),
            "ass_path": str(ass_path),
            "stderr_tail": completed.stderr[-500:],
        }
        return record
    burned_sha = "sha256:" + _sha256(burned_path)
    record["burned_preview"] = {
        "status": "BURNED",
        "path": str(burned_path),
        "ass_path": str(ass_path),
        "burned_sha256": burned_sha,
        "subtitle_style": "lidousha-final-sapphire72",
        "pillarbox_16_9": bool(vertical),
    }
    hashes = dict(record.get("artifact_hashes") or {})
    hashes["burned_video_sha256"] = burned_sha
    hashes["ass_sha256"] = "sha256:" + _sha256(ass_path)
    record["artifact_hashes"] = hashes
    return record


def _write_lidousha_sapphire_ass_from_srt(srt_path: Path, ass_path: Path) -> None:
    cues = _parse_srt(srt_path)
    event_rows = []
    for cue in cues:
        for sub_start_ms, sub_end_ms, sub_text in _layout_cue_for_display(
            cue.source_start_ms, cue.source_end_ms, cue.text
        ):
            text = _ass_escape_text(sub_text)
            event_rows.append(
                f"Dialogue: 0,{_format_ass_time(sub_start_ms)},{_format_ass_time(sub_end_ms)},Default,,0,0,0,,{text}"
            )
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    # header must byte-match the approved sapphire72 spec emitted by
    # .agent/skills/song-lyrics-timeline-aligner/scripts/align_timed_lyrics.py
    # write_ass at --play-res 1920x1080: Fontsize 72 belongs to the 1080p
    # PlayRes with margins 60,60,40, Shadow 2, BackColour &H70000000
    ass_path.write_text(
        "\n".join(
            [
                "[Script Info]",
                "ScriptType: v4.00+",
                "PlayResX: 1920",
                "PlayResY: 1080",
                "WrapStyle: 0",
                "ScaledBorderAndShadow: yes",
                "",
                "[V4+ Styles]",
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
                "Style: Default,Microsoft YaHei,72,&H00FFFFFF,&H000000FF,&H00BA520F,&H70000000,0,0,0,0,100,100,0,0,1,3,2,2,60,60,40,1",
                "",
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                *event_rows,
                "",
            ]
        ),
        encoding="utf-8",
    )


def _format_ass_time(ms: int) -> str:
    # round (not floor) to centiseconds — flooring made every cue start up to
    # 9ms early, which compounds with other sources of "subtitles feel early"
    total_centiseconds = max(0, (int(ms) + 5) // 10)
    centiseconds = total_centiseconds % 100
    total_seconds = total_centiseconds // 100
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


ASS_MAX_CHARS_PER_LINE = 28
ASS_MAX_VISUAL_LINES = 2
ASS_MIN_SUBCUE_MS = 700

_TEXT_BREAK_STRONG = "。！？…；;!?"
_TEXT_BREAK_WEAK = "，、,: ：~〜 "


def _split_text_segments(text: str) -> list[str]:
    """Split cue text into natural phrase segments at punctuation boundaries."""

    normalized = " ".join(text.replace("\r", "\n").split())
    segments: list[str] = []
    current = ""
    for char in normalized:
        current += char
        if char in _TEXT_BREAK_STRONG or char in _TEXT_BREAK_WEAK:
            if current.strip():
                segments.append(current.strip())
            current = ""
    if current.strip():
        segments.append(current.strip())
    # hard-split any single segment that alone exceeds the line limit
    result: list[str] = []
    for segment in segments:
        while len(segment) > ASS_MAX_CHARS_PER_LINE:
            result.append(segment[:ASS_MAX_CHARS_PER_LINE])
            segment = segment[ASS_MAX_CHARS_PER_LINE:]
        if segment:
            result.append(segment)
    return result or ([normalized] if normalized else [])


def _pack_segments(segments: Sequence[str], max_chars: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for segment in segments:
        if current and len(current) + len(segment) > max_chars:
            chunks.append(current)
            current = segment
        else:
            current += segment
    if current:
        chunks.append(current)
    return chunks


def _layout_cue_for_display(
    start_ms: int,
    end_ms: int,
    text: str,
) -> list[tuple[int, int, str]]:
    """Viewability contract (Ivan, 2026-07-03): at most 28 chars per visual
    line, at most 2 lines per dialogue, single line preferred.  Over-long cue
    text is split into sequential sub-cues (time allocated by text share)
    instead of stacking 3-4 lines that cover half the screen."""

    segments = _split_text_segments(text)
    if not segments:
        return []
    duration_ms = max(0, end_ms - start_ms)
    # prefer single-line chunks; fall back to 2-line chunks when the cue is too
    # short to give each single-line sub-cue a readable minimum duration
    chunks = _pack_segments(segments, ASS_MAX_CHARS_PER_LINE)
    if len(chunks) > 1 and duration_ms // len(chunks) < ASS_MIN_SUBCUE_MS:
        chunks = _pack_segments(segments, ASS_MAX_CHARS_PER_LINE * ASS_MAX_VISUAL_LINES)
    total_chars = sum(len(chunk) for chunk in chunks) or 1
    result: list[tuple[int, int, str]] = []
    cursor_ms = start_ms
    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            chunk_end_ms = end_ms
        else:
            chunk_end_ms = min(end_ms, cursor_ms + max(1, (duration_ms * len(chunk)) // total_chars))
        display = _wrap_ass_text(chunk)
        if chunk_end_ms > cursor_ms and display:
            result.append((cursor_ms, chunk_end_ms, display))
        cursor_ms = chunk_end_ms
    return result


def _wrap_ass_text(text: str, *, max_chars: int = ASS_MAX_CHARS_PER_LINE) -> str:
    """Wrap one display chunk to at most 2 visual lines of <= max_chars,
    breaking at a punctuation boundary near the middle when possible."""

    line = " ".join(text.replace("\r", "\n").split())
    if len(line) <= max_chars:
        return line
    # choose the break closest to the middle, preferring natural boundaries
    candidates = [
        index + 1
        for index, char in enumerate(line[:-1])
        if char in _TEXT_BREAK_STRONG or char in _TEXT_BREAK_WEAK
    ]
    valid = [i for i in candidates if 0 < i <= max_chars and len(line) - i <= max_chars]
    if valid:
        break_at = min(valid, key=lambda i: abs(i - len(line) / 2))
    else:
        # no natural boundary: break at the middle, clamped so both halves fit
        break_at = min(max_chars, max(len(line) - max_chars, (len(line) + 1) // 2))
    first, second = line[:break_at].rstrip(), line[break_at:].lstrip()
    return f"{first}\\N{second}" if second else first


def _ass_escape_text(text: str) -> str:
    return text.replace("{", "（").replace("}", "）")


def _escape_ffmpeg_filter_path(path: Path | str) -> str:
    return str(path).replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def _lidousha_fontsdir(media_path: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    env_value = os.environ.get("LIDOUSHA_FONTS_DIR")
    if env_value:
        candidates.append(Path(env_value))
    if media_path is not None:
        for parent in [media_path.parent, *media_path.parents]:
            candidates.append(parent / "fonts")
    candidates.extend(
        [
            ROOT / "assets" / "lidousha" / "fonts",
            ROOT / "assets" / "fonts",
            ROOT / "assets",
            Path("/app/assets/fonts"),
            Path("/opt/bilive/app/assets/fonts"),
            Path("/app/assets"),
            Path("/opt/bilive/app/assets"),
        ]
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


# Title policy (authority: assets/lidousha/title_style.md, itself backfilled
# from .agent/skills/lidousha-title-style/SKILL.md 2026-07-04).  These gates
# apply ONLY to LLM-auto-generated titles — Ivan's manual titles pass through
# untouched (title_llm_call=None; see the iron rule in _stage_publish_draft).
_LIDOUSHA_TITLE_PREFIX = "【李豆沙】"
# Empty hype/clickbait words Ivan bans as STANDALONE words (almost always empty hype).
_TITLE_BANNED_HYPE_WORDS: tuple[str, ...] = ("炸裂", "震惊", "天花板", "绝了", "犯规", "太顶")
# 离谱 is dual-use: descriptive "越看越离谱/越整越离谱" is an Ivan-APPROVED structure
# (title-style §2/§5, real historical titles), so it is banned ONLY in the empty
# "X到离谱" suffix form — never as a standalone word.
_TITLE_SUFFIX_ONLY_HYPE_WORDS: tuple[str, ...] = ("离谱",)
# "X到{banned}" universal hype suffix (哄睡到犯规 / 好听到炸裂 / 哄睡到离谱): the title
# must say concretely what happened instead of slapping a catch-all hype tail on.
_TITLE_BANNED_SUFFIX_RE = re.compile(
    "到(?:" + "|".join(_TITLE_BANNED_HYPE_WORDS + _TITLE_SUFFIX_ONLY_HYPE_WORDS) + ")"
)
# Filler/machine-flavored words Ivan banned outright (2026-07-06): none of these
# ever appear in his real historical titles.  直呼打咩 >> 直接打咩; 当场/秒X are
# auto-title tics, not his voice.  The word bank in title_style.md may only
# contain words verified against Ivan's own titles (machine-generated legacy
# production titles are NOT corpus).
_TITLE_BANNED_FILLER_WORDS: tuple[str, ...] = ("直接", "当场")
_TITLE_BANNED_MIAO_RE = re.compile(r"秒[一-鿿]")  # 秒懂/秒回/秒怼… instant-X tic
_TITLE_MIN_LEN = 12  # counted WITH the 【李豆沙】 prefix
_TITLE_MAX_LEN = 30
_TITLE_MAX_ATTEMPTS = 3  # 1 initial generation + up to 2 bounded retries


def _title_policy_violations(title: str) -> list[str]:
    """Policy codes an auto-generated title trips (empty list == clean).

    Applied only to LLM-auto-generated titles; Ivan's manual titles pass
    through untouched per the iron rule in ``_stage_publish_draft``.
    """

    violations: list[str] = []
    if _TITLE_BANNED_SUFFIX_RE.search(title):
        violations.append("banned_universal_suffix")
    if any(word in title for word in _TITLE_BANNED_HYPE_WORDS):
        violations.append("banned_hype_word")
    if any(word in title for word in _TITLE_BANNED_FILLER_WORDS):
        violations.append("banned_filler_word")
    if _TITLE_BANNED_MIAO_RE.search(title):
        violations.append("banned_filler_word")
    return violations


def _ensure_lidousha_prefix(title: str) -> str:
    """Guarantee the 【李豆沙】 publish prefix on an auto-generated title.

    Song titles already carry the fuller ``【李豆沙】豆沙歌，`` prefix, which
    itself starts with ``【李豆沙】``, so the ``startswith`` check avoids
    double-prefixing.
    """

    stripped = title.strip()
    return stripped if stripped.startswith(_LIDOUSHA_TITLE_PREFIX) else _LIDOUSHA_TITLE_PREFIX + stripped


def _stage_publish_draft(
    materialized_recut: dict[str, object] | None,
    *,
    candidate_id: str,
    title: str,
    cues: Sequence[SourceCue],
    run_ffmpeg: bool,
    title_llm_call: LlmCall | None,
    art_direction_llm_call: LlmCall | None = None,
) -> dict[str, object] | None:
    """Mirror production local_prepare: AI title + cover + publish.json draft.

    Always writes ``upload_enabled: false`` — publishing stays behind the
    AUTO_UPLOAD manifest/hash gate and is out of scope for the shadow lane.
    """

    if not materialized_recut or materialized_recut.get("status") != "MATERIALIZED":
        return materialized_recut
    record = dict(materialized_recut)
    media_path = Path(str(record["media_path"]))
    publish_json_path = media_path.with_suffix(".publish.json")

    # Iron rule: Ivan's manual title (title_llm_call=None) is final and passes
    # through a字不改 — no prefix forcing, no length gate, no policy check.
    # Prefix / length / banned-word enforcement applies ONLY to auto titles.
    staged_title = title
    title_source = "job_title"
    title_policy_violations: list[str] = []
    if title_llm_call is not None:
        transcript_sample = _staged_transcript_sample(record, cues)
        style_asset = _load_lidousha_asset("title_style.md")
        persona_asset = _load_lidousha_asset("persona.md")
        base_prompt = (
            "为一条李豆沙(B站虚拟主播)的直播切片起中文标题。\n"
            "最重要的原则：观众是因为'这是李豆沙'才点进来的,不是因为内容——标题必须围绕李豆沙本人"
            "(她的反应、气质、口癖、梗、名字谐音),切片内容只是辅助素材。引人注目为先。\n"
            f"\n李豆沙特质:\n{persona_asset}\n"
            f"\n标题风格规范与历史标题范例(严格模仿这个风格):\n{style_asset}\n"
            f"\n本切片转写内容节选(辅助素材): {transcript_sample}\n"
            "硬性要求：含【李豆沙】前缀后 12–30 字；"
            "禁用空洞夸张词(炸裂/震惊/天花板/绝了/犯规/太顶),"
            "更不许用'X到犯规/炸裂/离谱'这种万能后缀——标题必须具体到这条切片里到底发生了什么"
            "(描述性的'越看越离谱/越整越离谱'这类是可以的,禁的是空洞的'X到离谱'后缀)。\n"
            '只输出一个 JSON 对象：{"title": "标题"}'
        )
        llm_title = ""
        llm_error: str | None = None
        # Bounded retry: regenerate up to _TITLE_MAX_ATTEMPTS times, calling out
        # the banned-word violation each retry so the model rewrites concretely.
        for attempt in range(_TITLE_MAX_ATTEMPTS):
            prompt = base_prompt
            if attempt > 0:
                prompt = (
                    base_prompt
                    + "\n注意：上一次生成的标题命中了违禁词（夸张词/'X到{违禁词}'万能后缀/机器味弱化词\"直接/当场/秒X\"），已被否决。"
                    "这些词 Ivan 的真实历史标题里从来没有——别用任何万能强调词，"
                    "直接写她具体做了/说了什么（引她的原话、用梗词，如\"直呼打咩\"\"大大方方承认\"），重新只输出 JSON。"
                )
            try:
                payload = extract_json_object(title_llm_call(prompt))
            except Exception as exc:
                llm_error = type(exc).__name__
                break
            candidate = str(payload.get("title") or "").strip()
            if not candidate:
                llm_error = "empty_title"
                break
            llm_title = candidate
            title_policy_violations = _title_policy_violations(candidate)
            if not title_policy_violations:
                break

        if llm_title:
            prefixed = _ensure_lidousha_prefix(llm_title)
            if _TITLE_MIN_LEN <= len(prefixed) <= _TITLE_MAX_LEN:
                staged_title = prefixed
                title_source = "llm+lidousha_style_asset"
                # Retries exhausted but still violating → keep it, flag the draft.
                if title_policy_violations:
                    title_source = "llm+lidousha_style_asset(title_policy_violation)"
            else:
                # Length gate rejects the auto title → fall back to the job title
                # untouched (prefix forcing never touches non-LLM titles).
                title_policy_violations = []
                title_source = f"job_title(llm_length_out_of_bounds:{len(prefixed)})"
        elif llm_error is not None:
            title_source = f"job_title(llm_failed: {llm_error})"

    cover_text = _lidousha_cover_text(staged_title)
    cover_result = _stage_lidousha_ai_cover(
        record,
        media_path=media_path,
        candidate_id=candidate_id,
        title=staged_title,
        cover_text=cover_text,
        run_ffmpeg=run_ffmpeg,
        art_direction_llm_call=art_direction_llm_call,
    )
    cover_status = str(cover_result["status"])
    cover_path_value = cover_result.get("cover_path") if cover_status == "AI_COVER_READY" else None
    cover_generation = cover_result["cover_generation"]
    raw_reason_codes = cover_result.get("reason_codes")
    reason_codes = [str(value) for value in raw_reason_codes] if isinstance(raw_reason_codes, list) else []
    artifact_hashes = {str(k): str(v) for k, v in dict(record.get("artifact_hashes") or {}).items()}
    for key in ("cover_sha256", "ai_background_sha256", "cover_reference_sha256"):
        value = cover_result.get(key)
        if isinstance(value, str) and value:
            artifact_hashes[key] = value

    publish_draft = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": candidate_id,
        "upload_enabled": False,
        "title": staged_title,
        "title_source": title_source,
        "title_policy_violations": title_policy_violations,
        "video_path": str(media_path),
        "cover_text": cover_text,
        "cover_path": cover_path_value,
        "cover_status": cover_status,
        "cover_generation": cover_generation,
        "reason_codes": reason_codes,
        "artifact_hashes": artifact_hashes,
    }
    publish_json_path.write_text(json.dumps(publish_draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record["artifact_hashes"] = artifact_hashes
    record["publish_staging"] = {
        "status": "STAGED",
        "title": staged_title,
        "title_source": title_source,
        "title_policy_violations": title_policy_violations,
        "cover_status": cover_status,
        "cover_path": cover_path_value,
        "cover_text": cover_text,
        "cover_generation": cover_generation,
        "reason_codes": reason_codes,
        "publish_json_path": str(publish_json_path),
        "upload_enabled": False,
    }
    return record


def _stage_lidousha_ai_cover(
    materialized_recut: Mapping[str, object],
    *,
    media_path: Path,
    candidate_id: str,
    title: str,
    cover_text: str,
    run_ffmpeg: bool,
    art_direction_llm_call: LlmCall | None = None,
) -> dict[str, object]:
    cover_generation: dict[str, object] = {
        "workflow": LIDOUSHA_COVER_WORKFLOW,
        "method": "images.edit",
        "model": "gpt-image-2",
        "image_gen_model": "cpa",
        "fallback_used": False,
        "cover_text": cover_text,
        "title": title,
    }
    base_url = os.environ.get("CPA_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("CPA_API_KEY", "").strip()
    if not base_url or not api_key:
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "CPA_CREDENTIALS_MISSING"],
            "CPA_BASE_URL/CPA_API_KEY missing; deterministic frame covers are not publish-grade",
        )
    if not run_ffmpeg:
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "COVER_REFERENCE_EXTRACTION_DISABLED"],
            "ffmpeg disabled, so no identity/reference frame can be extracted for CPA images.edit",
        )

    artifact_root = _materialized_artifact_root(materialized_recut, media_path)
    cover_refs_dir = artifact_root / "cover_refs"
    ai_dir = artifact_root / "covers_ai_original"
    covers_dir = artifact_root / "covers"
    evidence_dir = artifact_root / "evidence"
    for directory in (cover_refs_dir, ai_dir, covers_dir, evidence_dir):
        directory.mkdir(parents=True, exist_ok=True)

    reference_path = cover_refs_dir / f"{candidate_id}.cover-ref.png"
    ref_command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(media_path),
        "-vf",
        "thumbnail=120,scale=1920:-2",
        "-frames:v",
        "1",
        str(reference_path),
    ]
    completed = subprocess.run(ref_command, check=False, capture_output=True, text=True)
    if completed.returncode != 0 or not reference_path.is_file():
        cover_generation["reference_command"] = ref_command
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "COVER_REFERENCE_EXTRACTION_FAILED"],
            completed.stderr[-500:] or "reference frame extraction failed",
        )

    # Art direction is picked AFTER the fail-closed gates (creds/ffmpeg/ref frame)
    # so a blocked cover never spends an LLM call. It is fail-OPEN (deterministic
    # baseline) while the cover IMAGE stays fail-closed.
    art_direction = _lidousha_cover_art_direction(
        candidate_id=candidate_id,
        title=title,
        cover_text=cover_text,
        art_direction_llm_call=art_direction_llm_call,
    )
    cover_generation["art_direction"] = asdict(art_direction)

    ai_background_path = ai_dir / f"{candidate_id}.ai-bg.cpa-gpt-image-2.png"
    request_path = evidence_dir / f"{candidate_id}.cover-cpa-request.redacted.json"
    response_path = evidence_dir / f"{candidate_id}.cover-cpa-response.redacted.json"
    cpa_result = _call_cpa_image_edit(
        base_url=base_url,
        api_key=api_key,
        reference_path=reference_path,
        output_path=ai_background_path,
        prompt=_lidousha_cover_prompt(title=title, cover_text=cover_text, art_direction=art_direction),
        request_path=request_path,
        response_path=response_path,
    )
    cover_generation.update(
        {
            "reference_image": str(reference_path),
            "reference_sha256": "sha256:" + _sha256(reference_path),
            "request_path": str(request_path),
            "response_path": str(response_path),
        }
    )
    if cpa_result.get("status") != "AI_BACKGROUND_READY" or not ai_background_path.is_file():
        cover_generation["cpa_status"] = cpa_result.get("status")
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", str(cpa_result.get("reason_code") or "CPA_IMAGE_EDIT_FAILED")],
            str(cpa_result.get("detail") or "CPA image edit did not return an image"),
        )

    final_cover_path = covers_dir / f"{candidate_id}.ai-title.cover.png"
    overlay = _overlay_lidousha_cover_title(ai_background_path, final_cover_path, cover_text=cover_text, art_direction=art_direction)
    cover_generation.update(
        {
            "ai_background": str(ai_background_path),
            "ai_background_sha256": "sha256:" + _sha256(ai_background_path),
            "final_cover": str(final_cover_path),
            "final_cover_sha256": "sha256:" + _sha256(final_cover_path),
            **overlay,
        }
    )
    return {
        "status": "AI_COVER_READY",
        "reason_codes": [],
        "cover_path": str(final_cover_path),
        "cover_generation": cover_generation,
        "cover_sha256": "sha256:" + _sha256(final_cover_path),
        "ai_background_sha256": "sha256:" + _sha256(ai_background_path),
        "cover_reference_sha256": "sha256:" + _sha256(reference_path),
    }


def _blocked_ai_cover_result(cover_generation: Mapping[str, object], reason_codes: Sequence[str], detail: str) -> dict[str, object]:
    generation = {**dict(cover_generation), "status": "BLOCKED", "detail": detail}
    return {
        "status": "BLOCKED_AI_COVER_REQUIRED",
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "cover_path": None,
        "cover_generation": generation,
    }


def _materialized_artifact_root(materialized_recut: Mapping[str, object], media_path: Path) -> Path:
    manifest_value = materialized_recut.get("manifest_path")
    if isinstance(manifest_value, str) and manifest_value:
        manifest_path = Path(manifest_value)
        if manifest_path.parent.name in {"recuts", "media"}:
            return manifest_path.parent.parent
        return manifest_path.parent
    if media_path.parent.name in {"recuts", "media"}:
        return media_path.parent.parent
    return media_path.parent


def _staged_transcript_sample(record: Mapping[str, object], cues: Sequence[SourceCue]) -> str:
    """Title/cover text sample: prefer the FINAL subtitle (fresh transcription
    with glossary corrections) over the context cues, so the title uses the
    corrected names (Ado, 小室) rather than the coarse-ASR spellings."""

    subtitle_path = record.get("subtitle_path")
    if isinstance(subtitle_path, str) and Path(subtitle_path).is_file():
        try:
            from src.autoslice.jingting_chunker import parse_srt_cues

            parsed = parse_srt_cues(Path(subtitle_path).read_text(encoding="utf-8"))
            sample = " ".join(" ".join(cue.text.split()) for cue in parsed if cue.text.strip())[:600]
            if sample:
                return sample
        except OSError:
            pass
    return " ".join(cue.text.strip() for cue in cues if cue.text.strip())[:600]


# --------------------------------------------------------------------------
# Persona-driven cover art-direction (Ivan 2026-07-04 redesign).
# Old covers were "wallpaper + a single-color bottom title bar", all alike.
# The new system rotates layouts, matches the FACE to the clip's in-character
# role, varies the background, highlights a hook word, and backs the text with a
# soft dark card so any fill color reads on a bright pop background.  CPA still
# makes only a text-free background; the title is overlaid locally (fail-closed).
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LidoushaCoverArtDirection:
    """One cover's art direction: chosen deterministically per candidate_id (so
    covers differ but are reproducible) and optionally refined by a CPA judge."""

    role: str            # persona archetype key (see persona.md 封面表情/角色映射)
    expression_en: str   # in-character English face phrase injected into the CPA prompt
    background_style: str  # a key in _COVER_BG_BUSY (talk) or _COVER_BG_CALM (song/tender)
    layout: str          # left-split | right-split | banner | song-clean
    hook_color: str      # key in _COVER_HOOK_COLORS
    is_song: bool
    hook_word: str = ""  # verbatim substring of cover_text to highlight ("" = none)


_COVER_TALK_LAYOUTS = ("left-split", "right-split", "banner")
_COVER_SONG_LAYOUT = "song-clean"
_COVER_BASE_FILL = (255, 246, 214)  # cream #FFF6D6 — approved base fill
_COVER_STROKE = (18, 36, 79)        # navy  #12244F — approved outer stroke
_COVER_WHITE = (255, 255, 255)
# Hook/accent colors rotate — NOT only yellow/pink (Ivan 2026-07-04).  All are
# SATURATED (never the cream base fill, else the hook word would be invisible).
_COVER_HOOK_COLORS = {
    "yellow": (255, 198, 41),
    "pink": (255, 92, 138),
    "purple": (150, 106, 245),
    "blue": (58, 141, 237),
    "orange": (255, 140, 60),
    "red": (233, 69, 69),
}
_COVER_BG_BUSY = ("pop-art-burst", "halftone-dots", "speed-lines")  # talk
_COVER_BG_CALM = ("soft-radial", "clean-scenic")                    # song / tender
_COVER_BG_PHRASES = {
    "pop-art-burst": "an energetic pop-art comic background — radiating burst/speed lines, halftone dots, scattered sparkles and little stars, filling the frame",
    "halftone-dots": "a vivid halftone dot-pattern background with a few bold stars and soft sparkles, filling the frame",
    "speed-lines": "a dynamic comic speed-line / radial motion background with halftone shading and sparkles, filling the frame",
    "soft-radial": "a soft radial glow background with gentle bokeh and a few sparkles, calm and uncluttered",
    "clean-scenic": "a clean dreamy scene — a starry night sky with a crescent moon, soft bokeh and a few floating music notes, low-detail and uncluttered",
}
# Expression guardrail (Ivan — 表情永不吐舌头, never 油滑/挑衅/sexy).  Match bad
# PHRASES, not bare "tongue" (else a benign "no tongue" would be rejected); the
# global no-tongue rule is enforced unconditionally in _lidousha_cover_prompt.
_COVER_FORBIDDEN_EXPR = (
    "tongue out", "tongue-out", "tongue sticking", "sticking tongue", "stick out her tongue",
    "licking", "sexy", "seductive", "挑衅", "provocative", "油滑", "媚", "cleavage", "flirt", "吐舌",
)

# Title-keyword → in-character role/expression/background.  DEFAULT is soft/cute
# 清纯邻家女同学; 机灵/得意 is SECONDARY (only when the clip role calls for it).
_COVER_ROLE_LEXICON: tuple[tuple[tuple[str, ...], str, str, str | None], ...] = (
    (("破防", "害怕", "好可怕", "吓", "怕", "惊", "傻眼", "？！", "!？", "遇到"), "shocked_bites_back",
     "wide-eyed startled gasp, mouth open in surprise, flushed cheeks, hands drawn up near her face, scared-but-cute", "speed-lines"),
    (("哭", "眼泪", "又哭", "哭哭"), "teary_cute",
     "big welling teary eyes, a cute comedic about-to-cry frown, blush, sniffly", "speed-lines"),
    (("拆台", "反杀", "反怼", "玩梗", "一眼AI", "得意", "整活", "谐音", "反沙", "嘴瓢", "掏兜", "买弹幕", "自封"), "witty_smug",
     "clever pleased closed-mouth grin, one eyebrow slightly raised, a little smug but cute", "halftone-dots"),
    (("嘴硬", "澄清", "不是", "嘴犟", "才不"), "stubborn_pout",
     "pouty defiant frown, puffed cheeks, cute-stubborn hmph, arms-crossed energy", "halftone-dots"),
    (("吃醋", "你只能", "占有", "醋"), "jealous_pout",
     "jealous puffed-cheek pout, small knit brows, clingy-cute possessive look", "halftone-dots"),
    (("一本正经", "犯傻", "歪理", "认真", "讲道理"), "earnest_silly",
     "earnest deadpan serious face, flat calm eyes, taking herself absurdly seriously", "pop-art-burst"),
    (("看傻", "离谱", "越看越", "当场看", "越整越", "奇遇", "猴群", "见猴", "第一次见"), "dumbstruck",
     "dumbstruck frozen face, wide round sparkly eyes, small O-shaped open mouth, hands near chin", "pop-art-burst"),
    (("哄睡", "晚安", "温柔", "细声"), "tender_soft",
     "tender warm soft-smiling face, gentle half-lidded caring eyes, soothing", "soft-radial"),
)
_COVER_HOOK_LEXICON = (
    "反沙", "反杀", "拆台", "一群猴", "翻车", "破防", "看傻", "清唱", "一眼AI", "嘴硬", "吃醋", "哄睡",
    "犯傻", "离谱", "掏兜", "买弹幕", "回扣", "反李豆沙", "海王", "认输", "自封", "妈妈", "宝宝", "破大防",
    "猴群", "奇遇", "熊猫头",
)


def _cover_stable_hash(seed: str) -> int:
    return int(hashlib.sha256((seed or "lidousha").encode("utf-8")).hexdigest(), 16)


def _cover_role_from_title(title: str, cover_text: str) -> tuple[str, str, str | None]:
    hay = f"{title} {cover_text}"
    for keywords, role, expr, bg in _COVER_ROLE_LEXICON:
        if any(keyword in hay for keyword in keywords):
            return role, expr, bg
    return (
        "shy_cute_default",
        "soft cute girl-next-door, shy but spirited, small closed-mouth smile, gentle blush",
        None,
    )


def _cover_default_hook_word(cover_text: str) -> str:
    # A 《song name》is the strongest hook and must stay whole on its own line
    # (Ivan 2026-07-05: 歌名不能换行). Highlight the whole 《...》.
    song = re.search(r"《[^》]*》", cover_text)
    if song:
        return song.group()
    flat = cover_text.replace("\n", "")
    for word in _COVER_HOOK_LEXICON:
        if word in flat:
            return word
    # explicit clause break (colon→newline) → highlight the last clause; a single
    # unbroken line with no lexicon hook gets NO forced highlight (don't paint the
    # whole title, which would collide with wrapping and read as monochrome).
    lines = [line.strip() for line in cover_text.splitlines() if line.strip()]
    return lines[-1] if len(lines) >= 2 else ""


def _lidousha_is_song_title(title: str) -> bool:
    return title.strip().startswith("【李豆沙】豆沙歌")


def _lidousha_cover_art_direction(
    *,
    candidate_id: str,
    title: str,
    cover_text: str,
    art_direction_llm_call: LlmCall | None = None,
) -> LidoushaCoverArtDirection:
    """Pick the cover's role/expression/background/layout/hook color.

    Deterministic baseline first (persona keyword lexicon + a stable per-clip
    hash for anti-monotony rotation), then optional CPA-judge refinement that is
    fail-OPEN and guard-railed (never tongue-out/油滑/sexy).  The cover IMAGE
    stays fail-closed elsewhere; only art-direction degrades gracefully.
    """

    is_song = _lidousha_is_song_title(title)
    digest = _cover_stable_hash(candidate_id or title)
    hook_keys = list(_COVER_HOOK_COLORS)
    hook_color = hook_keys[(digest // 31) % len(hook_keys)]
    if is_song:
        layout = _COVER_SONG_LAYOUT
        role = "gentle_song"
        expression_en = "gentle serene face, eyes softly closed or half-lidded, singing calmly with a faint tender smile"
        background_style = _COVER_BG_CALM[digest % len(_COVER_BG_CALM)]
    else:
        layout = _COVER_TALK_LAYOUTS[digest % len(_COVER_TALK_LAYOUTS)]
        role, expression_en, forced_bg = _cover_role_from_title(title, cover_text)
        background_style = forced_bg if forced_bg is not None else _COVER_BG_BUSY[(digest // 7) % len(_COVER_BG_BUSY)]
    baseline = LidoushaCoverArtDirection(
        role=role,
        expression_en=expression_en,
        background_style=background_style,
        layout=layout,
        hook_color=hook_color,
        is_song=is_song,
        hook_word=_cover_default_hook_word(cover_text),
    )
    if art_direction_llm_call is None:
        return baseline
    try:
        payload = extract_json_object(art_direction_llm_call(_cover_art_direction_prompt(title=title, cover_text=cover_text)))
        return _normalize_cover_art_direction(payload, baseline, cover_text)
    except Exception:
        return baseline


def _cover_art_direction_prompt(*, title: str, cover_text: str) -> str:
    persona = _load_lidousha_asset("persona.md")
    return (
        "你在为一条李豆沙(B站虚拟主播)切片的封面挑选'艺术指导'。只依据人设与本条切片语义选择。\n"
        f"\n李豆沙人设(权威):\n{persona}\n"
        "\n硬护栏:表情要贴这条切片里她扮演的角色;默认是软糯清纯邻家女同学(被欺负又软软反击);"
        "机灵鬼怪/得意只在角色需要时用(次要);**永远不要吐舌头**,不要油滑/挑衅/性感/媚。"
        "外观由参考帧决定,你不描述服装。\n"
        f"\n本切片标题: {title}\n封面文案(分行): {cover_text}\n"
        "\n从下列集合里各选一个:\n"
        f"- layout(谈话三选一,歌切固定 song-clean): {list(_COVER_TALK_LAYOUTS)} 或 song-clean\n"
        f"- background_style(谈话用忙: {list(_COVER_BG_BUSY)};歌/温柔用净: {list(_COVER_BG_CALM)})\n"
        f"- hook_color: {list(_COVER_HOOK_COLORS)}\n"
        "- role: 一个简短英文角色键(如 shy_cute_default/shocked_bites_back/witty_smug/tender_soft/gentle_song)\n"
        "- expression_en: 一句英文脸部表情(贴角色,不吐舌)\n"
        "- hook_word: 封面文案里最该高亮的一个词(必须是文案里出现的原词)\n"
        '只输出一个 JSON 对象: {"role":"...","expression_en":"...","background_style":"...","layout":"...","hook_color":"...","hook_word":"..."}'
    )


def _normalize_cover_art_direction(
    payload: Mapping[str, object],
    baseline: LidoushaCoverArtDirection,
    cover_text: str,
) -> LidoushaCoverArtDirection:
    def pick(key: str, allowed, default: str) -> str:
        value = payload.get(key)
        return value if isinstance(value, str) and value in allowed else default

    # song layout/background pools are fixed by is_song; only talk can rotate.
    layout = baseline.layout if baseline.is_song else pick("layout", set(_COVER_TALK_LAYOUTS), baseline.layout)
    hook_color = pick("hook_color", set(_COVER_HOOK_COLORS), baseline.hook_color)
    bg_pool = _COVER_BG_CALM if baseline.is_song else _COVER_BG_BUSY
    background_style = pick("background_style", set(bg_pool), baseline.background_style)

    expression_en = payload.get("expression_en")
    if not (isinstance(expression_en, str) and expression_en.strip()) or any(
        bad in expression_en.lower() for bad in _COVER_FORBIDDEN_EXPR
    ):
        expression_en = baseline.expression_en  # guardrail: reject tongue/油滑/sexy → safe baseline

    role = payload.get("role")
    role = role.strip() if isinstance(role, str) and role.strip() else baseline.role

    hook_word = payload.get("hook_word")
    if not (isinstance(hook_word, str) and hook_word and hook_word in cover_text.replace("\n", "")):
        hook_word = baseline.hook_word

    return LidoushaCoverArtDirection(
        role=role,
        expression_en=expression_en,
        background_style=background_style,
        layout=layout,
        hook_color=hook_color,
        is_song=baseline.is_song,
        hook_word=hook_word,
    )


def _lidousha_cover_text(title: str) -> str:
    # Cover title NEVER uses a colon (Ivan 2026-07-04): the archive/video title
    # may use "引语：反应", but on the cover the clause break is a LINE BREAK,
    # not punctuation.  Strip the 【李豆沙】/豆沙歌 prefix and turn any colon
    # into a newline so the overlay splits clauses by line.
    text = title.strip()
    for prefix in ("【李豆沙】豆沙歌，", "【李豆沙】"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = re.sub(r"\s*[：:]\s*", "\n", text)
    text = "\n".join(line.strip(" ，,") for line in text.split("\n") if line.strip(" ，,"))
    return text or title.strip()


def _lidousha_identity_descriptor() -> str:
    """Pull Li Dousha's visual identity descriptors from persona.md so the AI
    cover keeps her recognizable (熊猫头/熊猫耳/白毛/小李).

    The reference frame anchors identity, but CPA images.edit drifts without an
    explicit character description, so we inject the persona 身份/形象 lines
    verbatim (authoritative Chinese descriptors) alongside an English gloss.
    """

    persona = _load_lidousha_asset("persona.md")
    descriptors: list[str] = []
    for line in persona.splitlines():
        stripped = line.strip().lstrip("-").strip()
        if stripped.startswith(("身份", "形象")):
            descriptors.append(stripped)
    return " ".join(descriptors)


def _lidousha_cover_prompt(*, title: str, cover_text: str, art_direction: LidoushaCoverArtDirection | None = None) -> str:
    """Text-free CPA background prompt, ART-DIRECTED per clip (Ivan 2026-07-04).

    Identity stays anchored (panda/小李/熊猫, from persona.md) but the OUTFIT/skin
    is deferred to the per-clip reference frame — she wears different costumes on
    different streams.  Layout/expression/background follow ``art_direction``; the
    title is overlaid locally so this prompt forbids any rendered text.  She must
    NEVER stick her tongue out.
    """
    if art_direction is None:
        art_direction = _lidousha_cover_art_direction(candidate_id="", title=title, cover_text=cover_text)
    identity_descriptor = _lidousha_identity_descriptor()
    background = _COVER_BG_PHRASES.get(art_direction.background_style, _COVER_BG_PHRASES["pop-art-burst"])
    identity_block = (
        "Create a bold 16:9 (1920x1080) anime VTuber livestream cover thumbnail for Li Dousha. "
        "Use the supplied image ONLY as identity/style reference. "
        "IDENTITY (keep her instantly recognizable): Li Dousha is a cute anime VTuber whose signature look is "
        "a white PANDA hood with PANDA EARS over WHITE hair; her chibi/derivative form is '小李' (little Li). "
        "Faithfully preserve her panda-hood/panda-ear and white-hair face features from the reference image. "
        f"Persona identity descriptors (Chinese, authoritative): {identity_descriptor} "
        "PRESERVE THE EXACT OUTFIT, skin tone, hairstyle and accessories shown in the reference frame — she wears "
        "DIFFERENT costumes on different streams, so do NOT invent or lock a fixed costume; copy what the reference shows. "
        f"EXPRESSION (must fit her in-character role for this clip): {art_direction.expression_en}. "
        "Her mouth may be open for a gasp/shout/laugh but she must NEVER stick her tongue out — no tongue showing; "
        "never look sly beyond cute, never provocative or sexy. "
        "FEED-SAFE FRAMING: keep her FACE and all key features within the central 4:3 portion of the frame — feed "
        "thumbnails crop the outer ~13% of the width on EACH side, so place nothing important (face, hands, key props) "
        "in the far-left or far-right edges; those edges may hold only background. "
    )
    layout = art_direction.layout
    if layout == "left-split":
        composition = (
            "COMPOSITION: draw her as a LARGE chest-up bust filling the LEFT ~55% of the frame, close to the camera, "
            "big and expressive, with a clean white sticker-style outline so she pops off the background. "
            f"The RIGHT ~45% is an empty graphic zone reserved for a title (keep her body out of it): fill it and the "
            f"whole frame with {background}. Minimal empty space, high energy. "
        )
    elif layout == "right-split":
        composition = (
            "COMPOSITION: draw her as a LARGE chest-up bust filling the RIGHT ~55% of the frame, close to the camera, "
            "big and expressive, with a clean white sticker-style outline so she pops off the background. "
            f"The LEFT ~45% is an empty graphic zone reserved for a title (keep her body out of it): fill it and the "
            f"whole frame with {background}. Minimal empty space, high energy. "
        )
    elif layout == "banner":
        composition = (
            "COMPOSITION: place her as a LARGE chest-up bust in the LOWER-CENTER, head around the middle of the frame, "
            "with a clean white sticker outline. Keep the TOP ~40% a clear vibrant band reserved for a big title. "
            f"Fill the whole frame with {background}. Minimal empty space. "
        )
    else:  # song-clean
        composition = (
            "COMPOSITION: draw her as a soft chest-up portrait on the RIGHT ~55%, optionally holding a microphone, "
            "with a clean gentle look. Keep the LEFT ~45% a CLEAN calm zone reserved for a title: fill it with "
            f"{background}. Cohesive blue / navy / cream palette, tasteful and pretty rather than loud. "
        )
    return (
        identity_block
        + composition
        + "CRITICAL — render ABSOLUTELY NO text of any kind: no letters, words, Chinese/Japanese/English "
        + "characters, numbers, watermark, logos, UI, subtitles, or comic 'POW'/speech-bubble text anywhere. "
        + "The title is added separately afterwards, so the reserved title area must be a graphic background that is "
        + "COMPLETELY EMPTY of any glyphs or symbols. Keep the whole composition energetic, cute and eye-catching."
    )


def _load_lidousha_asset(name: str) -> str:
    path = ROOT / "assets" / "lidousha" / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return "(资产文件缺失)"


def _call_cpa_image_edit(
    *,
    base_url: str,
    api_key: str,
    reference_path: Path,
    output_path: Path,
    prompt: str,
    request_path: Path,
    response_path: Path,
    timeout_seconds: float = 180.0,
) -> dict[str, object]:
    endpoint = f"{base_url}/images/edits"
    request_path.write_text(
        json.dumps(
            {
                "endpoint": endpoint,
                "model": "gpt-image-2",
                "method": "images.edit",
                "image_gen_model": "cpa",
                "prompt": prompt,
                "reference_image": str(reference_path),
                "reference_sha256": "sha256:" + _sha256(reference_path),
                "api_key": "<redacted>",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        body, content_type = _multipart_form_data(
            fields={"model": "gpt-image-2", "prompt": prompt, "size": "1920x1080"},
            files={"image": (reference_path.name, reference_path.read_bytes(), "image/png")},
        )
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": content_type,
                # the CPA endpoint sits behind Cloudflare, which 403s (error
                # 1010) the default Python-urllib user agent
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status_code = response.status
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        response_path.write_text(
            json.dumps({"status_code": exc.code, "body_tail": raw[-4000:]}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {"status": "FAILED", "reason_code": "CPA_IMAGE_EDIT_HTTP_ERROR", "detail": f"HTTP {exc.code}: {raw[-500:]}"}
    except Exception as exc:
        response_path.write_text(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {"status": "FAILED", "reason_code": "CPA_IMAGE_EDIT_REQUEST_FAILED", "detail": f"{type(exc).__name__}: {exc}"}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        response_path.write_text(
            json.dumps({"status_code": status_code, "body_tail": raw[-4000:]}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {"status": "FAILED", "reason_code": "CPA_IMAGE_EDIT_BAD_JSON", "detail": raw[-500:]}
    image_record = (payload.get("data") or [{}])[0] if isinstance(payload.get("data"), list) else {}
    if not isinstance(image_record, Mapping):
        image_record = {}
    redacted_response: dict[str, object] = {"status_code": status_code, "keys": sorted(payload.keys()), "data_keys": sorted(image_record.keys())}
    b64_json = image_record.get("b64_json")
    image_url = image_record.get("url")
    if isinstance(b64_json, str) and b64_json:
        output_path.write_bytes(base64.b64decode(b64_json))
        redacted_response["b64_json_bytes"] = len(b64_json)
    elif isinstance(image_url, str) and image_url:
        with urllib.request.urlopen(image_url, timeout=timeout_seconds) as image_response:
            output_path.write_bytes(image_response.read())
        redacted_response["url"] = image_url
    else:
        response_path.write_text(json.dumps(redacted_response, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"status": "FAILED", "reason_code": "CPA_IMAGE_EDIT_NO_IMAGE", "detail": "response had no b64_json/url image"}
    redacted_response["output_path"] = str(output_path)
    redacted_response["output_sha256"] = "sha256:" + _sha256(output_path)
    response_path.write_text(json.dumps(redacted_response, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": "AI_BACKGROUND_READY", "output_path": str(output_path)}


def _multipart_form_data(*, fields: Mapping[str, str], files: Mapping[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = "----HermesVtuberSliceCoverBoundary"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    for name, (filename, content, content_type) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


_COVER_SCRIM_SIDE = {"color": (8, 16, 44), "alpha": 172, "pad": 48, "feather": 26}
_COVER_SCRIM_BAR = {"color": (8, 16, 44), "alpha": 168, "pad": 44, "feather": 24}
_COVER_SCRIM_SOFT = {"color": (6, 12, 34), "alpha": 140, "pad": 58, "feather": 34}
# per-layout render spec: text-block zone box, tilt, dark card, outline stack.
# song-clean stays at -4.0 (the approved default tilt; keeps the song-cover test
# deterministic) while talk layouts each get their own slight tilt for variety.
# FEED-CROP SAFE ZONE (Ivan 2026-07-05): Bilibili's feed/首页/推荐 center-crops the
# 16:9 cover to ~4:3 (height kept, width 1920→1440, cutting 240px each side); some
# surfaces go to 1:1. Text near the L/R edges gets cut ("下播" was lost). So ALL
# title text must stay inside the central ~1280-wide safe band x∈[320,1600]
# (matches Bilibili's recommended 中央 1280×720 safe area, with buffer over the
# 240px 4:3 crop). Every layout's text zone is clamped to that band.
_COVER_SAFE_X0, _COVER_SAFE_X1 = 260, 1660  # feed 4:3 crop = 240px/side; +20 buffer
_COVER_LAYOUT_RENDER = {
    # zone the text block fills, tilt, dark-card params (unused when backing=outline),
    # max_lines (wrap budget — MORE lines ⇒ shorter lines ⇒ BIGGER font in the narrow
    # half, Ivan's trick), max_size (font cap). Outer edge = feed-safe band 260/1660;
    # inner edge kept off the character; zone made tall so many big lines fit.
    "left-split": {"zone": (960, 66, 1660, 1014), "angle": -4.0, "scrim": _COVER_SCRIM_SIDE, "max_lines": 5, "max_size": 360},
    "right-split": {"zone": (260, 66, 960, 1014), "angle": -3.0, "scrim": _COVER_SCRIM_SIDE, "max_lines": 5, "max_size": 360},
    "banner": {"zone": (260, 16, 1660, 486), "angle": -2.0, "scrim": _COVER_SCRIM_BAR, "max_lines": 3, "max_size": 360},
    "song-clean": {"zone": (260, 110, 1000, 940), "angle": -4.0, "scrim": _COVER_SCRIM_SOFT, "max_lines": 5, "max_size": 320},
}
_COVER_OUTLINE_NAVY_RATIO = 0.085   # outer stroke ≈ 8.5% of font size (chunky, scales up)
_COVER_OUTLINE_WHITE_RATIO = 0.042


def _cover_outlines_for(size):
    """Outline thickness scales WITH the font so big text keeps a chunky border
    (a fixed 20px outline looks thin under 260px text)."""
    outer = max(8, int(round(size * _COVER_OUTLINE_NAVY_RATIO)))
    inner = max(4, int(round(size * _COVER_OUTLINE_WHITE_RATIO)))
    return ((outer, _COVER_STROKE), (inner, _COVER_WHITE))
# Text backing behind the title. Ivan 2026-07-04: the reference covers use NO
# box — the thick navy+white outline alone separates the text from a bright pop
# background (the earlier dark "card" looked like an ugly rectangle and was only
# needed before the white-glyph outline bug was fixed).  "outline" = default,
# clean, reference-accurate.  "glow" = a soft dark halo hugging the glyphs (a
# sticker-shadow, NOT a box) for extra depth.  "card" = the old rounded panel.
_COVER_TEXT_BACKING = "outline"


_COVER_MISSING_CHECKERS: dict = {}


def _cover_fallback_font_path():
    """A cute + COMPLETE CJK font used for the WHOLE cover when ZCOOL is missing a
    glyph (Ivan 2026-07-05: one cover = one uniform font; if ZCOOL can't render a
    char like 镚, swap the ENTIRE cover to this font — never mix fonts in a cover).
    得意黑/SmileySans (cute, complete) preferred; then plain complete fallbacks."""
    for candidate in (ROOT / "assets/lidousha/fonts/SmileySans-Oblique.ttf",
                      Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
                      ROOT / "lidousha/2026-06-29/redone_fullsong_433_travel_meaning/fonts/msyh.ttf"):
        if candidate.is_file():
            return candidate
    return None


def _cover_font_for_text(cover_text):
    """Choose ONE font for the whole cover: default ZCOOL; if ZCOOL is MISSING any
    glyph in the title, use the complete fallback for the ENTIRE cover (uniform)."""
    zcool = _find_cover_font()
    missing = _cover_missing_checker(zcool)
    if any((not ch.isspace()) and missing(ch) for ch in cover_text):
        fallback = _cover_fallback_font_path()
        if fallback is not None:
            return fallback
    return zcool


def _cover_missing_checker(font_path):
    """Cached ch->bool: True if ZCOOL is MISSING the glyph (renders .notdef tofu).
    Detect by rendering the char and comparing to a known-missing PUA char."""
    key=str(font_path)
    cache_all=_COVER_MISSING_CHECKERS
    if key not in cache_all:
        from PIL import Image, ImageDraw, ImageFont
        probe=ImageFont.truetype(str(font_path),100)
        def render(ch):
            img=Image.new("L",(160,180),0)
            ImageDraw.Draw(img).text((12,12),ch,font=probe,fill=255)
            return img.tobytes()
        notdef=render("\ue000")
        seen={}
        def missing(ch):
            if ch not in seen:
                seen[ch]=(render(ch)==notdef)
            return seen[ch]
        cache_all[key]=missing
    return cache_all[key]


def _cover_fonts(font_path, size):
    """The single whole-cover font at `size` (no per-glyph mixing — the font is
    chosen once per cover by _cover_font_for_text)."""
    from PIL import ImageFont
    return (ImageFont.truetype(str(font_path), size), None, None)


def _cover_char_font(ch, fonts):
    return fonts[0]


def _cover_line_width(draw, segs, fonts, pad) -> float:
    total = 0.0
    for i, (text, _color) in enumerate(segs):
        total += sum(draw.textlength(ch, font=_cover_char_font(ch, fonts)) for ch in text)
        if i < len(segs) - 1:
            total += pad
    return total


def _cover_seg_outlines(fill, outlines):
    """Inner stroke MUST contrast the fill: a light/cream fill with a WHITE inner
    stroke merges adjacent glyphs into a blob, so light fills get the dark outer
    stroke ONLY; saturated fills get dark-outer + white-inner for pop."""
    luminance = 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]
    return [outlines[0]] if luminance > 200 else list(outlines)


def _cover_draw_layered(draw, x, y, segs, fonts, outlines, pad) -> None:
    """Two passes so a later segment's outline never occludes an earlier segment's
    fill: draw ALL outlines for the line first, then ALL fills on top.  ``pad`` is
    inserted between different-color segments so thick outlines don't bleed.  Draws
    char-by-char so a per-glyph fallback font (for chars ZCOOL lacks, e.g. 镚) can
    be used without breaking the cute look of the rest."""

    def seg_width(text):
        return sum(draw.textlength(ch, font=_cover_char_font(ch, fonts)) for ch in text)

    xx = x
    for i, (text, fill) in enumerate(segs):
        for width, color in _cover_seg_outlines(fill, outlines):
            cx = xx
            for ch in text:
                fnt = _cover_char_font(ch, fonts)
                draw.text((cx, y), ch, font=fnt, fill=fill, stroke_width=width, stroke_fill=color)
                cx += draw.textlength(ch, font=fnt)
        xx += seg_width(text) + (0 if i == len(segs) - 1 else pad)
    xx = x
    for i, (text, fill) in enumerate(segs):
        cx = xx
        for ch in text:
            fnt = _cover_char_font(ch, fonts)
            draw.text((cx, y), ch, font=fnt, fill=fill)
            cx += draw.textlength(ch, font=fnt)
        xx += seg_width(text) + (0 if i == len(segs) - 1 else pad)


def _build_cover_panel(size, text_bbox, *, color, alpha, pad, feather):
    """Soft-edged dark CARD sized to the text block: solid interior (so any fill,
    incl. pure white, reads on a bright pop background) with only the edges
    feathered.  Built in the text layer's coordinate space so it rotates with the
    text and stays aligned."""
    from PIL import Image, ImageDraw, ImageFilter

    left, top, right, bottom = text_bbox
    panel = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    box = [left - pad, top - pad * 0.7, right + pad, bottom + pad * 0.7]
    radius = int(min(box[2] - box[0], box[3] - box[1]) * 0.18)
    draw.rounded_rectangle(box, radius=max(1, radius), fill=(color[0], color[1], color[2], alpha))
    return panel.filter(ImageFilter.GaussianBlur(feather))


def _build_cover_glow(layer, *, color=(6, 12, 30), grow=13, blur=13, opacity=0.9):
    """Optional soft dark glow derived from the TEXT's own alpha — a shadow that
    hugs the glyph contour (never a rectangle). Dilate (MaxFilter) + blur so it
    reads as depth, not a box."""
    from PIL import Image, ImageFilter

    alpha = layer.split()[3].filter(ImageFilter.MaxFilter(grow)).filter(ImageFilter.GaussianBlur(blur))
    alpha = alpha.point(lambda v: int(min(255, v) * opacity))
    dark = Image.new("RGBA", layer.size, (*color, 255))
    dark.putalpha(alpha)
    return dark


def _cover_segment_line(line, hook_word, base_fill, hook_rgb):
    """Split one line into colored segments, highlighting hook_word if present."""
    if hook_word and hook_word == line:
        return [(line, hook_rgb)]
    if hook_word and hook_word in line:
        before, _sep, after = line.partition(hook_word)
        segs = []
        if before:
            segs.append((before, base_fill))
        segs.append((hook_word, hook_rgb))
        if after:
            segs.append((after, base_fill))
        return segs
    return [(line, base_fill)]


def _wrap_even(text, n, keep=()):
    """Wrap text into n balanced lines: prefer punctuation-delimited clauses when
    there are exactly n of them, else pack 'atoms' greedily into n length-balanced
    lines.  Atoms kept WHOLE (never split across lines): each ASCII run
    (kmx/TPL/AI/0.5), any 《song name》, and any phrase in ``keep`` (the highlighted
    hook word, so its color stays intact).  Shorter lines ⇒ bigger font."""
    text = text.strip("，,、；;！!？? ")
    if n <= 1 or len(text) <= 1:
        return [text]
    # A 《song name》never wraps and gets its own complete line (Ivan 2026-07-05);
    # the prefix/suffix DO wrap across the remaining lines so a long tail stays big.
    song = re.search(r"《[^》]*》", text)
    if song:
        pre = text[:song.start()].strip("，,、；;！!？? ")
        suf = text[song.end():].strip("，,、；;！!？? ")
        name = song.group()
        total = len(pre) + len(suf)
        if total == 0:
            return [name]
        remaining = max(1, n - 1)
        pre_n = max(1, round(remaining * len(pre) / total)) if pre else 0
        suf_n = max(1, remaining - pre_n) if suf else 0
        lines = []
        if pre:
            lines += _wrap_even(pre, pre_n)
        lines.append(name)
        if suf:
            lines += _wrap_even(suf, suf_n)
        return [ln for ln in lines if ln]
    parts = [p.strip() for p in re.split(r"[，,、；;]", text) if p.strip()]
    if len(parts) == n:
        return parts
    keeps = sorted((re.escape(k) for k in keep if k), key=len, reverse=True)
    pattern = "|".join([*keeps, r"《[^》]*》", r"[A-Za-z0-9]+", r"[^A-Za-z0-9]"])
    atoms = re.findall(pattern, text)
    n = min(n, len(atoms))
    if n <= 1:
        return ["".join(atoms)]
    # BALANCED partition: break at the atom boundaries nearest the even split
    # positions, so every line is ~equal length (no long tail line that would cap
    # the font). Balanced lines ⇒ bigger font (Ivan 2026-07-05).
    cum = [0]
    for atom in atoms:
        cum.append(cum[-1] + len(atom))
    total = cum[-1]
    cuts = []
    for k in range(1, n):
        ideal = total * k / n
        best = None
        for i in range(len(atoms) - 1):
            if (cuts and i <= cuts[-1]) or i in cuts:
                continue
            dist = abs(cum[i + 1] - ideal)
            if best is None or dist < best[0]:
                best = (dist, i)
        if best is not None:
            cuts.append(best[1])
    lines, start = [], 0
    for cut in cuts:
        lines.append("".join(atoms[start:cut + 1]))
        start = cut + 1
    lines.append("".join(atoms[start:]))
    return [line for line in lines if line]


def _fit_cover_lines(cover_text, *, hook_word, base_fill, hook_rgb, zone, font_path, max_lines=3, max_size=300):
    """Choose the line-wrap + font size that makes the title as BIG as possible
    while filling the zone: try 1..max_lines wraps, and for each binary-search the
    largest emphasized size that fits (width AND height); keep the wrap that yields
    the biggest font.  The hook line is emphasized; outlines scale with the font."""
    from PIL import Image, ImageDraw, ImageFont

    x0, y0, x1, y1 = zone
    zone_w = (x1 - x0) * 0.98
    zone_h = (y1 - y0) * 0.96
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    explicit = [line.strip() for line in cover_text.splitlines() if line.strip()] if "\n" in cover_text else None

    def evaluate(line_texts, emph):
        emph_idx = 0
        for i, line in enumerate(line_texts):
            if hook_word and hook_word in line:
                emph_idx = i
                break
        seg_lines = [_cover_segment_line(line, hook_word, base_fill, hook_rgb) for line in line_texts]
        connector = max(46, int(round(emph / 1.25)))
        sizes = [emph if i == emph_idx else connector for i in range(len(line_texts))]
        fonts = [_cover_fonts(font_path, s) for s in sizes]
        pads = [_cover_outlines_for(s)[0][0] for s in sizes]
        widths = [_cover_line_width(scratch, seg_lines[i], fonts[i], pads[i]) for i in range(len(line_texts))]
        gaps = [max(6, int(s * 0.08)) for s in sizes]
        total_h = sum(sum(f[0].getmetrics()) for f in fonts) + sum(gaps[:-1] or [0])
        fits = (max(widths) <= zone_w) and (total_h <= zone_h)
        return fits, sizes, seg_lines, gaps

    keep = (hook_word,) if hook_word else ()
    candidates = [explicit] if explicit else [_wrap_even(cover_text, n, keep=keep) for n in range(1, max_lines + 1)]
    best = None
    for line_texts in candidates:
        line_texts = [line for line in line_texts if line]
        if not line_texts:
            continue
        lo, hi, best_emph = 46, max_size, 46
        while lo <= hi:
            mid = (lo + hi) // 2
            if evaluate(line_texts, mid)[0]:
                best_emph = mid
                lo = mid + 1
            else:
                hi = mid - 1
        _, sizes, seg_lines, gaps = evaluate(line_texts, best_emph)
        if best is None or best_emph > best[0]:
            best = (best_emph, seg_lines, sizes, gaps)
    _, seg_lines, sizes, gaps = best
    return [{"segs": seg_lines[i], "size": sizes[i], "gap": gaps[i]} for i in range(len(seg_lines))]


def _overlay_lidousha_cover_title(
    ai_background_path: Path,
    final_cover_path: Path,
    *,
    cover_text: str,
    art_direction: LidoushaCoverArtDirection | None = None,
) -> dict[str, object]:
    """Overlay the multi-color artistic title onto the text-free CPA background.

    Layout-aware (side split / banner / song), with a highlighted hook word and
    the approved cream/navy palette.  Default backing is "outline" — the thick
    navy(+white) stroke alone lifts the text off bright pop backgrounds like the
    reference covers (the dark card/glow modes exist but Ivan rejected the card
    box look).  Font is fail-closed ZCOOLKuaiLe (whole-cover swap to 得意黑 only
    when ZCOOL lacks a glyph).
    """
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    if art_direction is None:
        art_direction = _lidousha_cover_art_direction(candidate_id="", title=cover_text, cover_text=cover_text)
    render = _COVER_LAYOUT_RENDER.get(art_direction.layout, _COVER_LAYOUT_RENDER["left-split"])
    zone = render["zone"]
    angle = render["angle"]
    scrim = render["scrim"]
    hook_rgb = _COVER_HOOK_COLORS.get(art_direction.hook_color, _COVER_HOOK_COLORS["yellow"])

    font_path = _cover_font_for_text(cover_text)  # ZCOOL, or a complete font if ZCOOL lacks a glyph
    image = ImageOps.fit(Image.open(ai_background_path).convert("RGB"), (1920, 1080), method=Image.Resampling.LANCZOS)
    lines = _fit_cover_lines(
        cover_text,
        hook_word=art_direction.hook_word,
        base_fill=_COVER_BASE_FILL,
        hook_rgb=hook_rgb,
        zone=zone,
        font_path=font_path,
        max_lines=render["max_lines"],
        max_size=render["max_size"],
    )

    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    meta = []
    total_h = 0
    max_w = 0
    for line in lines:
        fonts = _cover_fonts(font_path, line["size"])
        outlines = _cover_outlines_for(line["size"])
        width = _cover_line_width(scratch, line["segs"], fonts, outlines[0][0])
        height = sum(fonts[0].getmetrics())
        meta.append((fonts, outlines, width, height))
        max_w = max(max_w, width)
        total_h += height + line["gap"]
    pad = 90
    layer = Image.new("RGBA", (int(max(1, max_w + pad * 2)), int(max(1, total_h + pad))), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    y = pad // 2
    for line, (fonts, outlines, width, height) in zip(lines, meta):
        x = (layer.width - width) / 2
        _cover_draw_layered(layer_draw, x, y, line["segs"], fonts, outlines, outlines[0][0])
        y += height + line["gap"]
    font_size = max(line["size"] for line in lines)

    # Text backing: default "outline" (none) — the thick navy+white outline alone
    # separates the text from a bright pop background, like the reference covers.
    backing = None
    bbox = layer.split()[3].getbbox()
    if _COVER_TEXT_BACKING == "card" and scrim and bbox:
        backing = _build_cover_panel(
            layer.size, bbox, color=scrim["color"], alpha=scrim["alpha"], pad=scrim["pad"], feather=scrim["feather"]
        )
    elif _COVER_TEXT_BACKING == "glow" and bbox:
        backing = _build_cover_glow(layer)
    if angle:
        layer = layer.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
        if backing is not None:
            backing = backing.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)

    x0, y0, x1, y1 = zone
    paste_x = int(x0 + (x1 - x0 - layer.width) / 2)
    paste_y = int(y0 + (y1 - y0 - layer.height) / 2)
    if backing is not None:
        canvas = Image.new("RGBA", image.size, (0, 0, 0, 0))
        canvas.paste(backing, (paste_x, paste_y), backing)
        image = Image.alpha_composite(image.convert("RGBA"), canvas).convert("RGB")
    image.paste(layer, (paste_x, paste_y), layer)
    final_cover_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(final_cover_path)
    return {
        "font": font_path.name if font_path is not None else "PIL-default",
        "font_size": font_size,
        "angle_degrees": angle,
        "overlay_position": {"x": paste_x, "y": paste_y},
        "title_band": art_direction.layout,
        "layout": art_direction.layout,
        "background_style": art_direction.background_style,
        "hook_color": art_direction.hook_color,
        "hook_word": art_direction.hook_word,
        "role": art_direction.role,
        "expression_en": art_direction.expression_en,
        "text_backing": _COVER_TEXT_BACKING,
        "scrim": backing is not None,
    }


def _find_cover_font() -> Path:
    """Cover title font is ZCOOL KuaiLe (站酷快乐体) — the established Li Dousha
    cover look, deliberately different from the subtitle font.  Fail closed:
    a silently substituted default font shipped wrong-font covers once
    (2026-07-04); a missing font must block the cover, not degrade it."""

    candidates = [
        ROOT / "assets" / "lidousha" / "fonts" / "ZCOOLKuaiLe-Regular.ttf",
        Path("/opt/bilive/app/assets/fonts/ZCOOLKuaiLe-Regular.ttf"),
        Path("/app/assets/fonts/ZCOOLKuaiLe-Regular.ttf"),
    ]
    fontsdir = _lidousha_fontsdir(None)
    if fontsdir is not None:
        candidates.insert(0, fontsdir / "ZCOOLKuaiLe-Regular.ttf")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("COVER_FONT_MISSING: ZCOOLKuaiLe-Regular.ttf not found (assets/lidousha/fonts/)")


def _materialize_recut_record(
    *,
    source_video: Path,
    candidate_id: str,
    boundary_resolution: BoundaryResolution | None,
    output_dir: Path,
    cues: Sequence[SourceCue],
    run_ffmpeg: bool,
    lyric_timeline: Sequence[tuple[int, str]] | None = None,
    lyric_offset_ms: int | None = None,
    speech_spans_provider: SpeechSpansProvider | None = None,
    fresh_talk_transcriber: Callable[[Path], str] | None = None,
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
    timing_qa_path = media_path.with_suffix(".timing_qa.json")
    media_path.parent.mkdir(parents=True, exist_ok=True)

    timing_qa_record: dict[str, object] | None = None
    speech_spans_cache: list | None = None
    if lyric_timeline is not None and lyric_offset_ms is not None:
        # Strict song process: burned lyric timing comes from the external LRC
        # timeline shifted by the proven global offset, never from ASR cues.
        subtitle_source = "external_lrc_global_shift"
        _write_lyric_timeline_srt(lyric_timeline, lyric_offset_ms, start_ms, end_ms, subtitle_path)
    else:
        subtitle_source = "asr_cues"
        # ASR cue timing is coarse and hallucination-prone over BGM; sanitize
        # against real speech evidence before it becomes burned subtitles.
        # Best-effort: a VAD outage is recorded, never silently ignored.
        if speech_spans_provider is not None:
            try:
                speech_spans_cache = list(speech_spans_provider(source_video, start_ms, end_ms))
                cues, timing_qa_record = sanitize_cue_timing(
                    cues, speech_spans_cache, window_start_ms=start_ms, window_end_ms=end_ms
                )
                timing_qa_path.write_text(
                    json.dumps(timing_qa_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
            except Exception as exc:
                timing_qa_record = {
                    "status": "SUBTITLE_TIMING_QA_UNAVAILABLE",
                    "error": f"{type(exc).__name__}: {exc}",
                }
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
    # Repair-first: a copy-cut that landed on a keyframe seconds away must be
    # re-rendered precisely for ANY materialized recut, not only AUTO_UPLOAD —
    # otherwise review blocks on ACTUAL_CUT_ERROR_HIGH that we know how to fix.
    # Song clips ALWAYS re-render: copy-cut leaves audio/video stream starts
    # quantized to packet/keyframe boundaries (measured 20-90ms skew), which is
    # exactly the "lyrics show ~20ms early" class of bug — the LRC subtitle
    # timeline is only valid against a sample-accurate audio start.
    cut_error_ms = _render_qa_actual_cut_error_ms(render_qa)
    needs_accurate_rerender = (cut_error_ms is not None and cut_error_ms > 100) or subtitle_source == "external_lrc_global_shift"
    if run_ffmpeg and needs_accurate_rerender:
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

    # Fresh whole-window transcription (talk only): the coarse integer-second
    # production ASR is fine for recall but repeatedly shipped text/timing
    # mismatches in finals — re-transcribing the finished clip media gives
    # cue timing and text that actually correspond to the audio.  Runs after
    # the accurate re-render so the subtitle matches the final media exactly.
    # Fail-open with a recorded fallback: a transcriber outage must not kill
    # materialization, but it must be visible in the evidence.
    fresh_transcription_record: dict[str, object] | None = None
    if fresh_talk_transcriber is not None and subtitle_source == "asr_cues":
        try:
            import inspect

            clip_speech_spans = (
                [(span.start_ms - start_ms, span.end_ms - start_ms) for span in speech_spans_cache]
                if speech_spans_cache
                else None
            )
            if len(inspect.signature(fresh_talk_transcriber).parameters) >= 2:
                fresh_srt_text = fresh_talk_transcriber(media_path, clip_speech_spans)
            else:
                fresh_srt_text = fresh_talk_transcriber(media_path)
            fresh_cues = _fresh_srt_to_source_cues(fresh_srt_text, window_start_ms=start_ms, duration_ms=duration_ms)
            sanitized_cues = fresh_cues
            if speech_spans_cache is not None:
                sanitized_cues, timing_qa_record = sanitize_cue_timing(
                    fresh_cues, speech_spans_cache, window_start_ms=start_ms, window_end_ms=end_ms
                )
                timing_qa_path.write_text(
                    json.dumps(timing_qa_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
            _write_source_range_srt(sanitized_cues, start_ms, end_ms, subtitle_path)
            subtitle_source = "fresh_agy_transcription"
            artifact_hashes["subtitle_sha256"] = "sha256:" + _sha256(subtitle_path)
            fresh_transcription_record = {
                "status": "USED",
                "cue_count": len(fresh_cues),
                "replaced_subtitle_source": "asr_cues",
            }
        except Exception as exc:
            fresh_transcription_record = {
                "status": "FAILED_FALLBACK_ASR_CUES",
                "error": f"{type(exc).__name__}: {exc}",
            }
    manifest = {
        "schema_version": "materialized-recut.v1",
        "status": "MATERIALIZED",
        "reason_codes": reason_codes,
        "candidate_id": candidate_id,
        "source_video_path": str(source_video),
        "requested_range": {"start_ms": start_ms, "end_ms": end_ms, "duration_ms": duration_ms},
        "media_path": str(media_path),
        "subtitle_path": str(subtitle_path),
        "subtitle_source": subtitle_source,
        "lyric_offset_ms": lyric_offset_ms if subtitle_source == "external_lrc_global_shift" else None,
        "command": plan["command"],
        "accurate_command": accurate_command,
        "dry_run_placeholder": not run_ffmpeg,
        "accurate_rerender_used": accurate_rerender_used,
        "artifact_hashes": artifact_hashes,
        "render_qa": render_qa,
        "render_qa_path": str(render_qa_path) if render_qa is not None else None,
        "subtitle_timing_qa": timing_qa_record,
        "fresh_transcription": fresh_transcription_record,
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
        "subtitle_source": subtitle_source,
        "lyric_offset_ms": lyric_offset_ms if subtitle_source == "external_lrc_global_shift" else None,
        "manifest_path": str(manifest_path),
        "dry_run_placeholder": not run_ffmpeg,
        "accurate_rerender_used": accurate_rerender_used,
        "accurate_command": accurate_command,
        "artifact_hashes": artifact_hashes,
        "render_qa": render_qa,
        "render_qa_path": str(render_qa_path) if render_qa is not None else None,
        "subtitle_timing_qa": timing_qa_record,
        "timing_qa_path": str(timing_qa_path) if timing_qa_record is not None and "counts" in timing_qa_record else None,
        "fresh_transcription": fresh_transcription_record,
    }


def _fresh_srt_to_source_cues(srt_text: str, *, window_start_ms: int, duration_ms: int) -> list[SourceCue]:
    """Validate a fresh clip-relative transcription and lift it onto the
    source timeline.  Fail loudly on garbage — the caller falls back to the
    ASR-cue subtitle and records why."""

    from src.autoslice.jingting_chunker import parse_srt_cues

    parsed = parse_srt_cues(srt_text)
    if len(parsed) < 3:
        raise ValueError(f"fresh transcription has too few cues ({len(parsed)})")
    previous_end = 0
    lifted: list[SourceCue] = []
    for index, cue in enumerate(parsed, start=1):
        if cue.start_ms < 0 or cue.end_ms <= cue.start_ms:
            raise ValueError(f"fresh transcription cue {index} has invalid timing {cue.start_ms}-{cue.end_ms}")
        if cue.start_ms < previous_end - 1_000:
            raise ValueError(f"fresh transcription cue {index} overlaps the previous cue by >1s")
        previous_end = max(previous_end, cue.end_ms)
        # Gemini timestamps drift slightly long near the clip tail: cues that
        # START past the clip are dropped, ends are clamped — one overrunning
        # tail cue must not discard an otherwise-good transcription.
        if cue.start_ms >= duration_ms:
            continue
        if not cue.text.strip():
            continue
        lifted.append(
            SourceCue(
                cue_id=f"fresh_{index:04d}",
                source_start_ms=window_start_ms + cue.start_ms,
                source_end_ms=window_start_ms + min(cue.end_ms, duration_ms),
                text=cue.text.strip(),
                language="zh",
                kind="speech",
                confidence=1.0,
            )
        )
    if len(lifted) < 3:
        raise ValueError("fresh transcription has too few non-empty cues")
    return lifted


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
    coarse_preroll_ms: int = 10_000,
) -> list[str]:
    # Two-stage seek: live-captured MPEG-TS has no reliable seek index, so a
    # pure input-side -ss can land *after* the requested point (byte-position
    # estimation) and the head goes missing.  Coarse input seek well before the
    # target, then decode-and-drop precisely on the output side.
    coarse_ms = max(0, start_ms - coarse_preroll_ms)
    fine_ms = start_ms - coarse_ms
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{coarse_ms / 1000:.3f}",
        "-i",
        str(source_video),
        "-ss",
        f"{fine_ms / 1000:.3f}",
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


def _load_lyric_timeline(
    job_manifest: Mapping[str, object],
    *,
    output_dir: Path,
) -> tuple[list[tuple[int, str]], int] | None:
    """Load the proven external-LRC timeline + global shift for a song job.

    The strict song process burns lyrics from ``lrc_time + offset``, never from
    raw ASR cue timings (ASR onsets are systematically early/noisy).  Only a
    verified lyrics-alignment proof may supply this timeline.
    """

    lyrics_alignment = _mapping(job_manifest.get("lyrics_alignment"))
    if _verify_lyrics_alignment_proof(lyrics_alignment, output_dir=output_dir) is not None:
        return None
    report_path = Path(str(lyrics_alignment["alignment_report_path"]))
    if not report_path.is_absolute() and not report_path.is_file():
        report_path = output_dir / report_path
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(report, Mapping):
        return None

    timeline: list[tuple[int, str]] = []
    lyric_lines = report.get("lyric_lines")
    if isinstance(lyric_lines, list):
        for line in lyric_lines:
            if isinstance(line, Mapping) and isinstance(line.get("lrc_time_ms"), int) and str(line.get("text") or "").strip():
                timeline.append((int(line["lrc_time_ms"]), str(line["text"]).strip()))
    if not timeline:
        alignment = report.get("alignment")
        if isinstance(alignment, list):
            for entry in alignment:
                if isinstance(entry, Mapping) and isinstance(entry.get("lrc_time_ms"), int) and str(entry.get("lrc_text") or "").strip():
                    timeline.append((int(entry["lrc_time_ms"]), str(entry["lrc_text"]).strip()))
    if not timeline:
        return None
    timeline.sort(key=lambda item: item[0])

    offset_value = lyrics_alignment.get("offset_ms")
    if not isinstance(offset_value, int) or isinstance(offset_value, bool):
        offset_value = report.get("offset_ms")
    if not isinstance(offset_value, int) or isinstance(offset_value, bool):
        return None
    return timeline, offset_value


def _write_lyric_timeline_srt(
    timeline: Sequence[tuple[int, str]],
    offset_ms: int,
    start_ms: int,
    end_ms: int,
    output_path: Path,
    *,
    max_duration_ms: int = 6_000,
    min_duration_ms: int = 800,
    tail_pad_ms: int = 6_500,
) -> int:
    """Write clip-local lyric SRT from the external LRC global-shift model.

    Mirrors ``build_cues`` in the song-lyrics-timeline-aligner skill script:
    cue end = next lyric start capped at +6s (no line hangs through a long
    instrumental gap), minimum display 0.8s, final line padded 6.5s.
    """

    duration_ms = max(0, end_ms - start_ms)
    mapped = [(lrc_time_ms + offset_ms - start_ms, text) for lrc_time_ms, text in timeline]
    rows: list[str] = []
    index = 1
    for position, (cue_start_ms, text) in enumerate(mapped):
        if cue_start_ms < 0 or cue_start_ms >= duration_ms:
            continue
        next_start_ms = mapped[position + 1][0] if position + 1 < len(mapped) else None
        if next_start_ms is None:
            cue_end_ms = cue_start_ms + tail_pad_ms
        else:
            cue_end_ms = min(next_start_ms, cue_start_ms + max_duration_ms)
        if cue_end_ms - cue_start_ms < min_duration_ms:
            cue_end_ms = cue_start_ms + min_duration_ms
            if next_start_ms is not None and cue_end_ms > next_start_ms:
                cue_end_ms = max(cue_start_ms + 100, next_start_ms)
        cue_end_ms = min(cue_end_ms, duration_ms)
        if cue_end_ms <= cue_start_ms:
            continue
        rows.append(
            f"{index}\n{_format_srt_time(cue_start_ms)} --> {_format_srt_time(cue_end_ms)}\n{text.strip()}\n"
        )
        index += 1
    output_path.write_text("\n".join(rows).rstrip() + ("\n" if rows else ""), encoding="utf-8")
    return index - 1


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
    parser.add_argument("--lrc-provider", choices=("none", "netease"), default="none", help="External LRC discovery provider for repair-first song completeness.")
    parser.add_argument("--burn-preview", action="store_true", help="Burn recut subtitles into a shadow preview render.")
    parser.add_argument("--song-hint-llm-command", help="LLM command template ({prompt_file} {completion_file}) for song-name guessing from garbled ASR.")
    parser.add_argument("--publish-staging", action="store_true", help="Stage AI title + cover + publish.json draft (upload_enabled always false).")
    parser.add_argument("--title-llm-command", help="LLM command template for title generation; falls back to the job title.")
    parser.add_argument("--cover-art-direction-llm-command", help="LLM command template ({prompt_file} {completion_file}) that picks cover art direction (role/expression/background/layout/hook); falls back to the deterministic persona baseline.")
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
        lrc_provider=build_netease_lrc_provider() if args.lrc_provider == "netease" else None,
        song_hint_llm_call=build_llm_call(LlmConfig(transport="command", command_template=args.song_hint_llm_command))
        if args.song_hint_llm_command
        else None,
        burn_preview=args.burn_preview,
        publish_staging=args.publish_staging,
        title_llm_call=build_llm_call(LlmConfig(transport="command", command_template=args.title_llm_command))
        if args.title_llm_command
        else None,
        art_direction_llm_call=build_llm_call(LlmConfig(transport="command", command_template=args.cover_art_direction_llm_command))
        if args.cover_art_direction_llm_command
        else None,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
