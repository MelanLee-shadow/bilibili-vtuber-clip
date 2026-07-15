"""Live-source evidence, boundary, and song-proof policy for shadow review.

Network/media execution stays in the CLI orchestrator. This module converts
already supplied source observations into fail-closed review evidence.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .auto_review import DecisionAction
from .boundary_resolver import AnchorCandidate, BoundaryResolution, TalkCue, resolve_talk_boundary
from .channel_profile import load_channel_profile
from .content_evidence import _editorial_score
from .cpa_semantic_qa import (
    apply_cpa_semantic_qa_to_review_evidence,
    evaluate_cpa_semantic_response_artifact,
    load_request_artifact,
)
from .host_vocal_proof import verify_host_vocal_proof_claim
from .llm_client import LlmCall
from .review_evidence import ReviewEvidence, SourceCue
from .shadow_review import _gap_summary, _sha256
from .song_repair import (
    AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION,
    AudioLrcAligner,
    LYRIC_VOCAL_ASSERTION_KEYS,
    LrcProvider,
    LrcResult,
    SongRepairResult,
    attempt_song_repair,
    derive_live_arrangement_completeness,
    fetch_lrclib_lrc,
    fetch_netease_lrc,
    live_performance_failure_reason_codes,
    load_audio_lrc_json_artifact,
    normalize_lyric_text,
    validate_audio_lrc_canonical_projection,
    validate_audio_lrc_execution_metadata,
    validate_live_performance_observation,
)
from .source_context_executor import SourceContextExecutionResult
from .source_context_planner import JingtingJobProvenance, plan_source_context_jingting_jobs
from .source_integrity import (
    MediaSegmentObservation,
    build_source_range_ledger,
    plan_bilibili_replay_compensation,
)
from .subtitle_rendering import _parse_srt

ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)


def profile_asset_file(key: str) -> Path:
    return CHANNEL_PROFILE.asset_file(key, repo_root=ROOT)


HostVocalProver = Callable[
    [Path, str, Mapping[str, object], Mapping[str, object], Path],
    Mapping[str, object],
]


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
    # Planning fills in the missing context window; it must not replace the
    # upstream candidate identity.  In particular, dropping these song-lane
    # fields turns a seeded song into an ordinary talk job, which can then take
    # the talk boundary fallback and materialize before the final review gate.
    # Keep planner-owned execution fields/times while retaining every upstream
    # extension and the proof/guard claims that make the job fail closed.
    merged = {**requested, **planned}
    merged["timeline"] = {
        **timeline,
        **dict(_mapping(planned.get("timeline"))),
    }
    merged["provenance"] = {
        **dict(_mapping(requested.get("provenance"))),
        **dict(_mapping(planned.get("provenance"))),
    }
    for guard_field in (
        "content_type_hint",
        "song_candidate",
        "requires_full_source_song_boundary_redo",
        "song_boundary",
        "lyrics_alignment",
        "live_performance_proof",
        "host_vocal_proof",
        "song_repair_gate",
    ):
        if guard_field in requested:
            merged[guard_field] = requested[guard_field]
    if room_id:
        merged["room_id"] = room_id
    return merged

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
    if _job_is_song_candidate(job_manifest):
        # A seeded/upstream song is never eligible for the ordinary talk
        # fallback.  In particular, a structurally valid AGY background-mode
        # rejection makes song repair return no READY boundary; falling through
        # here used to re-label that same window as talk and materialize it
        # before the outer delivery gate could object.
        return BoundaryResolution(
            candidate_id=anchor.candidate_id,
            action=DecisionAction.BLOCK,
            resolved_start_ms=anchor.anchor_start_ms,
            resolved_end_ms=anchor.anchor_end_ms,
            start_boundary_score=0.0,
            end_boundary_score=0.0,
            reason_codes=_song_candidate_gate_reason_codes(job_manifest, output_dir=output_dir),
        )
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

def _song_candidate_gate_reason_codes(
    job_manifest: Mapping[str, object], *, output_dir: Path
) -> tuple[str, ...]:
    """Return the strongest fail-closed reason retained by song repair."""

    repair_gate = _mapping(job_manifest.get("song_repair_gate"))
    raw_reasons = repair_gate.get("reason_codes")
    if isinstance(raw_reasons, Sequence) and not isinstance(raw_reasons, (str, bytes)):
        reasons = tuple(
            dict.fromkeys(
                reason
                for reason in raw_reasons
                if isinstance(reason, str) and reason.startswith("SONG_")
            )
        )
        if reasons:
            return reasons
    song_boundary = _mapping(job_manifest.get("song_boundary"))
    lyrics_alignment = _mapping(job_manifest.get("lyrics_alignment"))
    if _song_boundary_ready(song_boundary):
        if _verify_lyrics_alignment_proof(lyrics_alignment, output_dir=output_dir) is not None:
            return ("SONG_PROOF_UNVERIFIED",)
        performance_error = _verify_live_performance_observation(lyrics_alignment, output_dir=output_dir)
        if performance_error is not None:
            return _live_performance_block_reasons(lyrics_alignment, output_dir=output_dir)
    return ("SONG_LIVE_PERFORMANCE_UNPROVEN",)

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
    performance_error = _verify_live_performance_observation(lyrics_alignment, output_dir=output_dir)
    if performance_error is not None:
        performance_reasons = _live_performance_block_reasons(lyrics_alignment, output_dir=output_dir)
        return BoundaryResolution(
            candidate_id=anchor.candidate_id,
            action=DecisionAction.BLOCK,
            resolved_start_ms=anchor.anchor_start_ms,
            resolved_end_ms=anchor.anchor_end_ms,
            start_boundary_score=0.0,
            end_boundary_score=0.0,
            reason_codes=performance_reasons,
        )
    host_vocal_claim = _mapping(job_manifest.get("host_vocal_proof"))
    host_vocal_error, host_vocal_reason = _verify_host_vocal_claim(
        host_vocal_claim,
        expected_candidate_id=anchor.candidate_id,
        lyrics_alignment=lyrics_alignment,
    )
    if host_vocal_error is not None:
        return BoundaryResolution(
            candidate_id=anchor.candidate_id,
            action=DecisionAction.BLOCK,
            resolved_start_ms=anchor.anchor_start_ms,
            resolved_end_ms=anchor.anchor_end_ms,
            start_boundary_score=0.0,
            end_boundary_score=0.0,
            reason_codes=(host_vocal_reason,),
        )

    clip_start_ms = _first_int(song_boundary.get("clip_start_ms"), song_boundary.get("source_start_ms"), song_boundary.get("song_start_ms"))
    clip_end_ms = _first_int(
        song_boundary.get("clip_end_ms"),
        song_boundary.get("source_end_ms"),
        song_boundary.get("post_song_reaction_end_ms"),
        song_boundary.get("last_lyric_end_ms"),
    )
    # The CAM++ session sample starts at the first verified post-song host
    # speech.  That speech is evidence input, not song output.  Resolve against
    # the verified proof's current anchor as well as the earlier AGY boundary:
    # a proof/report refresh may legitimately tighten the end after repair.
    host_anchor_start_ms = _host_vocal_post_song_anchor_start(host_vocal_claim)
    if host_anchor_start_ms is None:
        return BoundaryResolution(
            candidate_id=anchor.candidate_id,
            action=DecisionAction.BLOCK,
            resolved_start_ms=anchor.anchor_start_ms,
            resolved_end_ms=anchor.anchor_end_ms,
            start_boundary_score=0.0,
            end_boundary_score=0.0,
            reason_codes=("SONG_HOST_VOCAL_PROOF_INVALID",),
        )
    if clip_end_ms is not None:
        clip_end_ms = min(clip_end_ms, host_anchor_start_ms)
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

def _verify_live_performance_observation(
    lyrics_alignment: Mapping[str, object], *, output_dir: Path
) -> str | None:
    proof_error = _verify_lyrics_alignment_proof(lyrics_alignment, output_dir=output_dir)
    if proof_error is not None:
        return proof_error
    report_value = lyrics_alignment.get("alignment_report_path")
    assert isinstance(report_value, str)
    report_path = Path(report_value)
    if not report_path.is_absolute() and not report_path.is_file():
        report_path = output_dir / report_path
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"cannot read live-performance alignment report: {exc}"
    if not isinstance(report, Mapping):
        return "live-performance alignment report is not a JSON object"
    first_ms = report.get("first_lyric_start_ms")
    last_ms = report.get("last_lyric_end_ms")
    report_rows = report.get("alignment")
    if not isinstance(first_ms, int) or isinstance(first_ms, bool) or not isinstance(last_ms, int) or isinstance(last_ms, bool):
        return "live-performance lyric span is missing"
    performance_error = validate_live_performance_observation(
        report.get("live_performance"),
        first_lyric_start_ms=first_ms,
        last_lyric_end_ms=last_ms,
        observations=report_rows,
        require_ready=True,
    )
    if performance_error is not None:
        return performance_error
    report_provider = report.get("audio_alignment_provider")
    report_agy_rc = report.get("audio_alignment_agy_rc", 0 if report_provider == "agy" else None)
    report_fallback_used = report.get(
        "audio_alignment_provider_fallback_used",
        False if report_provider == "agy" else None,
    )
    report_execution_error = validate_audio_lrc_execution_metadata(
        provider=report_provider,
        model=report.get("audio_alignment_model"),
        agy_rc=report_agy_rc,
        provider_fallback_used=report_fallback_used,
        agy_failure_category=report.get("audio_alignment_agy_failure_category"),
        sandbox=True if report_provider == "agy" else False,
    )
    if (
        report.get("evidence_source") != "agy_audio_lrc"
        or report_execution_error is not None
        or not str(lyrics_alignment.get("model") or "").endswith("-agy-audio-lrc-global-shift-v1")
    ):
        return "live-performance proof is not an approved production audio alignment"

    artifacts = report.get("audio_alignment_artifacts")
    if not isinstance(artifacts, Mapping):
        return "live-performance audio artifacts are missing"

    def bound_artifact(path_key: str, sha_key: str) -> tuple[Path | None, str | None]:
        path_value = artifacts.get(path_key)
        sha_value = artifacts.get(sha_key)
        if not isinstance(path_value, str) or not path_value or not isinstance(sha_value, str):
            return None, f"live-performance {path_key}/{sha_key} binding is missing"
        normalized_sha = sha_value.lower().removeprefix("sha256:")
        if len(normalized_sha) != 64 or any(character not in "0123456789abcdef" for character in normalized_sha):
            return None, f"live-performance {sha_key} is malformed"
        path = Path(path_value)
        if not path.is_absolute() and not path.is_file():
            path = report_path.parent / path
        try:
            if path.is_symlink() or not path.is_file() or _sha256(path) != normalized_sha:
                return None, f"live-performance {path_key} hash mismatch"
        except OSError as exc:
            return None, f"cannot read live-performance {path_key}: {exc}"
        return path, None

    artifact_paths: dict[str, Path] = {}
    for path_key, sha_key in (
        ("source_path", "source_sha256"),
        ("lrc_path", "lrc_sha256"),
        ("prompt_path", "prompt_sha256"),
        ("raw_output_path", "raw_output_sha256"),
        ("run_manifest_path", "run_manifest_sha256"),
    ):
        path, error = bound_artifact(path_key, sha_key)
        if error is not None or path is None:
            return error or f"live-performance {path_key} is invalid"
        artifact_paths[path_key] = path

    try:
        manifest = json.loads(artifact_paths["run_manifest_path"].read_text(encoding="utf-8"))
        raw = json.loads(artifact_paths["raw_output_path"].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"cannot read live-performance raw proof: {exc}"
    candidate_id = report.get("candidate_id")
    manifest_artifacts = manifest.get("artifacts") if isinstance(manifest, Mapping) else None
    manifest_execution_error = (
        validate_audio_lrc_execution_metadata(
            provider=manifest.get("provider"),
            model=manifest.get("model"),
            agy_rc=manifest.get("agy_rc"),
            provider_fallback_used=manifest.get("provider_fallback_used"),
            agy_failure_category=manifest.get("agy_failure_category"),
            sandbox=manifest.get("sandbox"),
        )
        if isinstance(manifest, Mapping)
        else "manifest is not a mapping"
    )
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema_version") not in {
            "agy-audio-lrc-run.v1",
            "agy-audio-lrc-run.v2",
            "agy-audio-lrc-run.v3",
        }
        or manifest.get("candidate_id") != candidate_id
        or (manifest.get("provider") == "gemini_api" and manifest.get("schema_version") != "agy-audio-lrc-run.v3")
        or manifest.get("provider") != report_provider
        or manifest.get("model") != report.get("audio_alignment_model")
        or manifest.get("agy_rc") != report_agy_rc
        or manifest.get("provider_fallback_used") is not report_fallback_used
        or manifest.get("agy_failure_category") != report.get("audio_alignment_agy_failure_category")
        or manifest_execution_error is not None
        or not isinstance(manifest_artifacts, Mapping)
    ):
        return "live-performance audio run manifest is invalid"
    for manifest_key, report_key in (
        ("source_origin_path", "source_origin_path"),
        ("source_path", "source_path"),
        ("source_sha256", "source_sha256"),
        ("source_duration_ms", "source_duration_ms"),
        ("lrc_path", "lrc_path"),
        ("lrc_sha256", "lrc_sha256"),
        ("prompt_path", "prompt_path"),
        ("prompt_sha256", "prompt_sha256"),
        ("output_path", "raw_output_path"),
        ("output_sha256", "raw_output_sha256"),
    ):
        if manifest_artifacts.get(manifest_key) != artifacts.get(report_key):
            return "live-performance audio manifest/artifact binding mismatch"

    if manifest.get("provider") == "gemini_api":
        api_audio_path, error = bound_artifact("api_audio_path", "api_audio_sha256")
        if error is not None or api_audio_path is None:
            return error or "live-performance Gemini API audio is invalid"
        api_audio_duration_ms = artifacts.get("api_audio_duration_ms")
        source_duration_ms = artifacts.get("source_duration_ms")
        configured_key_count = manifest.get("configured_key_count")
        accepted_key_ordinal = manifest.get("accepted_key_ordinal")
        if (
            manifest.get("direct_audio_input") is not True
            or not isinstance(api_audio_duration_ms, int)
            or isinstance(api_audio_duration_ms, bool)
            or not isinstance(source_duration_ms, int)
            or isinstance(source_duration_ms, bool)
            or abs(api_audio_duration_ms - source_duration_ms) > 1_000
            or manifest_artifacts.get("api_audio_path") != artifacts.get("api_audio_path")
            or manifest_artifacts.get("api_audio_sha256") != artifacts.get("api_audio_sha256")
            or manifest_artifacts.get("api_audio_duration_ms") != api_audio_duration_ms
            or not isinstance(configured_key_count, int)
            or isinstance(configured_key_count, bool)
            or not 1 <= configured_key_count <= 3
            or not isinstance(accepted_key_ordinal, int)
            or isinstance(accepted_key_ordinal, bool)
            or not 1 <= accepted_key_ordinal <= configured_key_count
        ):
            return "live-performance Gemini API complete-audio binding is invalid"

    if manifest.get("schema_version") in {"agy-audio-lrc-run.v2", "agy-audio-lrc-run.v3"}:
        provider_raw_path, error = bound_artifact(
            "provider_raw_output_path",
            "provider_raw_output_sha256",
        )
        if error is not None or provider_raw_path is None:
            return error or "live-performance provider raw output is invalid"
        if (
            artifacts.get("canonicalized_output_path") != artifacts.get("raw_output_path")
            or artifacts.get("canonicalized_output_sha256") != artifacts.get("raw_output_sha256")
            or manifest_artifacts.get("provider_raw_output_path")
            != artifacts.get("provider_raw_output_path")
            or manifest_artifacts.get("provider_raw_output_sha256")
            != artifacts.get("provider_raw_output_sha256")
        ):
            return "live-performance AGY raw/canonical artifact binding mismatch"
        canonicalization = manifest.get("canonicalization")
        canonical_lines_for_binding = report.get("canonical_lyric_lines")
        if canonicalization != {
            "strategy": "canonical-lrc-by-exact-index.v1",
            "row_identity": "strict_zero_based_lrc_index",
            "restored_fields": ["lrc_time_ms", "text"],
            "row_count": len(canonical_lines_for_binding) if isinstance(canonical_lines_for_binding, list) else -1,
            "canonical_lrc_sha256": artifacts.get("lrc_sha256"),
            "provider_raw_output_sha256": artifacts.get("provider_raw_output_sha256"),
            "canonicalized_output_sha256": artifacts.get("canonicalized_output_sha256"),
        }:
            return "live-performance AGY canonicalization manifest is invalid"
        try:
            provider_raw = load_audio_lrc_json_artifact(
                provider_raw_path,
                "live-performance provider raw proof",
            )
            validate_audio_lrc_canonical_projection(
                provider_payload=provider_raw,
                canonical_payload=raw,
                lrc_path=artifact_paths["lrc_path"],
            )
        except ValueError as exc:
            return f"live-performance provider/canonical projection is invalid: {exc}"

    raw_record = raw.get("record") if isinstance(raw, Mapping) else None
    raw_rows = raw.get("observations") if isinstance(raw, Mapping) else None
    if (
        not isinstance(raw, Mapping)
        or raw.get("schema_version") != AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION
        or not isinstance(raw_record, Mapping)
        or raw_record.get("candidate_id") != candidate_id
        or raw_record.get("source_sha256") != artifacts.get("source_sha256")
        or raw_record.get("lrc_sha256") != artifacts.get("lrc_sha256")
        or raw_record.get("source_duration_ms") != artifacts.get("source_duration_ms")
        or not isinstance(raw_rows, list)
    ):
        return "live-performance raw AGY observation schema is invalid"
    if raw.get("live_performance") != report.get("live_performance"):
        return "live-performance raw/report observation mismatch"
    if raw.get("live_arrangement") != report.get("live_arrangement_observation"):
        return "live-arrangement raw/report observation mismatch"
    try:
        arrangement_completeness = derive_live_arrangement_completeness(
            observations=raw_rows,
            live_arrangement=raw.get("live_arrangement"),
            post_song_talk_start_ms=raw.get("post_song_talk_start_ms"),
            source_duration_ms=int(artifacts.get("source_duration_ms")),
        )
    except (TypeError, ValueError) as exc:
        return f"live-arrangement structure is invalid: {exc}"
    if report.get("arrangement_completeness") != arrangement_completeness:
        return "live-arrangement code-derived report mismatch"
    if lyrics_alignment.get("completion_basis") != arrangement_completeness.get("classification"):
        return "live-arrangement completion basis mismatch"
    canonical_lyrics = report.get("canonical_lyric_lines")
    if (
        report.get("canonical_line_count") != len(raw_rows)
        or not isinstance(canonical_lyrics, list)
        or len(canonical_lyrics) != len(raw_rows)
        or any(
            not isinstance(raw_row, Mapping)
            or not isinstance(lyric, Mapping)
            or lyric.get("lrc_index") != raw_row.get("lrc_index")
            or lyric.get("lrc_time_ms") != raw_row.get("lrc_time_ms")
            or lyric.get("text") != raw_row.get("text")
            for raw_row, lyric in zip(raw_rows, canonical_lyrics, strict=True)
        )
    ):
        return "live-arrangement canonical raw/report rows mismatch"
    lyric_lines = report.get("lyric_lines")
    raw_heard_rows = [
        row for row in raw_rows
        if isinstance(row, Mapping) and row.get("heard") is True
    ]
    if (
        not isinstance(report_rows, list)
        or not isinstance(lyric_lines, list)
        or len(raw_heard_rows) != len(report_rows)
        or len(report_rows) != len(lyric_lines)
        or len(raw_heard_rows) < 8
    ):
        return "live-performance raw/report lyric rows are incomplete"
    expected_raw_sha = str(artifacts.get("raw_output_sha256") or "").lower().removeprefix("sha256:")
    previous_start: int | None = None
    for report_index, (raw_row, report_row, lyric) in enumerate(
        zip(raw_heard_rows, report_rows, lyric_lines, strict=True)
    ):
        if not isinstance(raw_row, Mapping) or not isinstance(report_row, Mapping) or not isinstance(lyric, Mapping):
            return f"live-performance raw/report lyric row {report_index} is invalid"
        canonical_index = raw_row.get("lrc_index")
        start_ms = raw_row.get("live_start_ms")
        end_ms = raw_row.get("live_end_ms")
        confidence = raw_row.get("confidence")
        if (
            set(raw_row) != {
                "lrc_index",
                "lrc_time_ms",
                "text",
                "heard",
                "live_start_ms",
                "live_end_ms",
                "confidence",
                *LYRIC_VOCAL_ASSERTION_KEYS,
            }
            or not LYRIC_VOCAL_ASSERTION_KEYS.issubset(report_row)
            or not isinstance(canonical_index, int)
            or isinstance(canonical_index, bool)
            or report_row.get("canonical_lrc_index") != canonical_index
            or ("lrc_index" in lyric and lyric.get("lrc_index") != canonical_index)
            or raw_row.get("lrc_time_ms") != lyric.get("lrc_time_ms")
            or raw_row.get("text") != lyric.get("text")
            or raw_row.get("heard") is not True
            or not isinstance(start_ms, int)
            or isinstance(start_ms, bool)
            or not isinstance(end_ms, int)
            or isinstance(end_ms, bool)
            or not 0 <= start_ms < end_ms
            or (previous_start is not None and start_ms <= previous_start)
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.8 <= float(confidence) <= 1.0
            or report_row.get("lrc_time_ms") != lyric.get("lrc_time_ms")
            or report_row.get("lrc_text") != lyric.get("text")
            or report_row.get("cue_start_ms") != start_ms
            or report_row.get("cue_end_ms") != end_ms
            or report_row.get("match_ratio") != round(float(confidence), 4)
            or report_row.get("evidence_source") != "agy_audio_lrc"
            or report_row.get("matched_cue_id") != f"agy-audio:{expected_raw_sha[:12]}:line-{canonical_index}"
            or any(report_row.get(key) != raw_row.get(key) for key in LYRIC_VOCAL_ASSERTION_KEYS)
        ):
            return f"live-performance raw/report lyric row {canonical_index} mismatch"
        previous_start = start_ms
    if (
        report_rows[0].get("cue_start_ms") != first_ms
        or report_rows[-1].get("cue_end_ms") != last_ms
        or len({row.get("matched_cue_id") for row in report_rows if isinstance(row, Mapping)}) != len(report_rows)
    ):
        return "live-performance raw/report lyric boundary mismatch"
    return None

def _live_performance_block_reasons(
    lyrics_alignment: Mapping[str, object], *, output_dir: Path
) -> tuple[str, ...]:
    """Preserve a valid non-live AGY mode in state/summary reason codes."""

    if _verify_lyrics_alignment_proof(lyrics_alignment, output_dir=output_dir) is not None:
        return ("SONG_LIVE_PERFORMANCE_UNPROVEN",)
    report_value = lyrics_alignment.get("alignment_report_path")
    if not isinstance(report_value, str):
        return ("SONG_LIVE_PERFORMANCE_UNPROVEN",)
    report_path = Path(report_value)
    if not report_path.is_absolute() and not report_path.is_file():
        report_path = output_dir / report_path
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ("SONG_LIVE_PERFORMANCE_UNPROVEN",)
    return live_performance_failure_reason_codes(
        report.get("live_performance") if isinstance(report, Mapping) else None
    )

def _verify_host_vocal_claim(
    claim: Mapping[str, object],
    *,
    expected_candidate_id: str,
    lyrics_alignment: Mapping[str, object],
) -> tuple[str | None, str]:
    """Validate the CAM++ proof and classify failure without trusting labels."""

    if not claim:
        return "host-vocal proof claim is missing", "SONG_HOST_VOCAL_UNPROVEN"
    proof_value = claim.get("proof_path")
    if not isinstance(proof_value, str) or not proof_value:
        reason = str(claim.get("reason_code") or "SONG_HOST_VOCAL_UNPROVEN")
        return str(claim.get("error") or "host-vocal proof artifact is missing"), reason
    try:
        proof = json.loads(Path(proof_value).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"cannot read host-vocal proof: {exc}", "SONG_HOST_VOCAL_PROOF_INVALID"
    if not isinstance(proof, Mapping):
        return "host-vocal proof is not a JSON object", "SONG_HOST_VOCAL_PROOF_INVALID"

    source_binding = _mapping(proof.get("source_media"))
    profile_binding = _mapping(proof.get("reference_profile"))
    source_value = claim.get("source_media_path") or source_binding.get("path")
    alignment_value = lyrics_alignment.get("alignment_report_path")
    profile_value = claim.get("profile_path") or profile_binding.get("path")
    if not all(isinstance(value, str) and value for value in (source_value, alignment_value, profile_value)):
        return "host-vocal input bindings are incomplete", "SONG_HOST_VOCAL_PROOF_INVALID"
    error = verify_host_vocal_proof_claim(
        claim,
        expected_candidate_id,
        Path(str(source_value)),
        Path(str(alignment_value)),
        Path(str(profile_value)),
    )
    if error is not None:
        return error, "SONG_HOST_VOCAL_PROOF_INVALID"
    if (
        claim.get("status") != "READY"
        or claim.get("decision") != CHANNEL_PROFILE.decision("host_vocal_present")
    ):
        if (
            claim.get("status") == "BLOCKED"
            and claim.get("decision") == CHANNEL_PROFILE.decision("host_vocal_absent")
        ):
            return (
                f"no {CHANNEL_PROFILE.prompt_name} vocal was detected across the lyric span",
                CHANNEL_PROFILE.decision("host_not_singing_reason"),
            )
        return "host-vocal proof did not reach READY", "SONG_HOST_VOCAL_UNPROVEN"
    return None, "SONG_HOST_VOCAL_VERIFIED"

def _host_vocal_post_song_anchor_start(claim: Mapping[str, object]) -> int | None:
    """Read the post-song speech start from an already verified proof claim."""

    proof_value = claim.get("proof_path")
    if not isinstance(proof_value, str) or not proof_value:
        return None
    try:
        proof = json.loads(Path(proof_value).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    session_anchor = _mapping(proof.get("session_host_anchor")) if isinstance(proof, Mapping) else {}
    start_ms = session_anchor.get("start_ms")
    if not isinstance(start_ms, int) or isinstance(start_ms, bool) or start_ms < 0:
        return None
    return start_ms

def _tighten_song_boundary_to_verified_host_anchor(
    job_manifest: Mapping[str, object],
    *,
    candidate_id: str,
    alignment: Mapping[str, object],
    claim: Mapping[str, object],
) -> Mapping[str, object]:
    """Persist the proof-tightened end so runner and renderer share one range."""

    error, _reason = _verify_host_vocal_claim(
        claim,
        expected_candidate_id=candidate_id,
        lyrics_alignment=alignment,
    )
    anchor_start_ms = _host_vocal_post_song_anchor_start(claim) if error is None else None
    boundary = dict(_mapping(job_manifest.get("song_boundary")))
    clip_start_ms = _first_int(
        boundary.get("clip_start_ms"),
        boundary.get("source_start_ms"),
        boundary.get("song_start_ms"),
    )
    clip_end_ms = _first_int(
        boundary.get("clip_end_ms"),
        boundary.get("source_end_ms"),
        boundary.get("post_song_reaction_end_ms"),
        boundary.get("last_lyric_end_ms"),
    )
    if (
        anchor_start_ms is None
        or clip_start_ms is None
        or clip_end_ms is None
        or anchor_start_ms <= clip_start_ms
        or anchor_start_ms >= clip_end_ms
    ):
        return dict(job_manifest)
    boundary["clip_end_ms"] = anchor_start_ms
    return {**dict(job_manifest), "song_boundary": boundary}

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
    host_vocal_claim = _mapping(source_context_job.get("host_vocal_proof"))
    song_candidate = _job_is_song_candidate(source_context_job)
    song_boundary_claimed = _song_boundary_ready(song_boundary)
    lyrics_proof_error = _verify_lyrics_alignment_proof(lyrics_alignment, output_dir=output_dir)
    live_performance_error = _verify_live_performance_observation(lyrics_alignment, output_dir=output_dir)
    host_vocal_error, host_vocal_reason = _verify_host_vocal_claim(
        host_vocal_claim,
        expected_candidate_id=evidence.candidate_id,
        lyrics_alignment=lyrics_alignment,
    )
    full_song_evidence_ready = (
        song_boundary_claimed
        and lyrics_proof_error is None
        and live_performance_error is None
        and host_vocal_error is None
    )
    if song_candidate and not song_boundary_claimed:
        unproven_reasons = _song_candidate_gate_reason_codes(source_context_job, output_dir=output_dir)
        checks.append(
            {
                "code": "SONG_JOINT_SINGING_GATE",
                "pass": False,
                "severity": "BLOCK",
                "evidence": {
                    "reason_codes": list(unproven_reasons),
                    "song_repair_gate": dict(_mapping(source_context_job.get("song_repair_gate"))),
                },
            }
        )
        updates["evidence_gaps"] = tuple(
            dict.fromkeys(tuple(evidence.evidence_gaps) + unproven_reasons)
        )
        updates["metadata"] = {
            **dict(evidence.metadata),
            "song_joint_singing_gate": {
                "verified": False,
                "reason_codes": list(unproven_reasons),
                "song_repair_gate": dict(_mapping(source_context_job.get("song_repair_gate"))),
            },
        }
        updates["foreground_song_overlap_seconds"] = 0.0
        updates["song_complete"] = False
        updates["lyrics_alignment_ready"] = False
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
    if song_boundary_claimed and live_performance_error is not None:
        performance_reasons = _live_performance_block_reasons(lyrics_alignment, output_dir=output_dir)
        checks.append(
            {
                "code": "SONG_LIVE_PERFORMANCE_PROOF",
                "pass": False,
                "severity": "BLOCK",
                "evidence": {
                    "error": live_performance_error,
                    "reason_codes": list(performance_reasons),
                    "lyrics_alignment": dict(lyrics_alignment),
                },
            }
        )
        current_gaps = tuple(updates.get("evidence_gaps", evidence.evidence_gaps))
        updates["evidence_gaps"] = tuple(
            dict.fromkeys(current_gaps + performance_reasons)
        )
        updates["metadata"] = {
            **dict(updates.get("metadata", evidence.metadata)),
            "live_performance_proof": {"verified": False, "error": live_performance_error},
        }
        updates["foreground_song_overlap_seconds"] = 0.0
        updates["song_complete"] = False
        updates["lyrics_alignment_ready"] = lyrics_proof_error is None
    if song_boundary_claimed and host_vocal_error is not None:
        checks.append(
            {
                "code": "SONG_HOST_VOCAL_PROOF",
                "pass": False,
                "severity": "BLOCK",
                "evidence": {
                    "error": host_vocal_error,
                    "reason_code": host_vocal_reason,
                    "claim": dict(host_vocal_claim),
                },
            }
        )
        current_gaps = tuple(updates.get("evidence_gaps", evidence.evidence_gaps))
        updates["evidence_gaps"] = tuple(dict.fromkeys(current_gaps + (host_vocal_reason,)))
        updates["metadata"] = {
            **dict(updates.get("metadata", evidence.metadata)),
            "host_vocal_proof": {"verified": False, "error": host_vocal_error, "claim": dict(host_vocal_claim)},
        }
        # Never inherit content-evidence's text-overlap estimate as a claim of
        # foreground singing.  Until performer identity verifies, this is only
        # "a song is audible" and must remain non-complete/non-foreground.
        updates["foreground_song_overlap_seconds"] = 0.0
        updates["song_complete"] = False
        updates["lyrics_alignment_ready"] = lyrics_proof_error is None
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
                    "live_performance_proof": {"verified": True, "mode": "LIVE_STREAMER_SINGING"},
                    "host_vocal_proof": {"verified": True, "claim": dict(host_vocal_claim)},
                    "song_duration_seconds": song_duration_seconds,
                },
            }
        )
        checks.append(
            {
                "code": "SONG_FULL_BOUNDARY_READY",
                "pass": True,
                "severity": "PASS",
                "evidence": {
                    "song_boundary": dict(song_boundary),
                    "lyrics_alignment": dict(lyrics_alignment),
                    "live_performance": "LIVE_STREAMER_SINGING",
                    "host_vocal_proof": dict(host_vocal_claim),
                },
            }
        )
        checks.append(
            {
                "code": "SONG_HOST_VOCAL_VERIFIED",
                "pass": True,
                "severity": "PASS",
                "evidence": {"decision": host_vocal_claim.get("decision")},
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
    blocking = tuple(
        reason
        for reason in evidence.evidence_gaps
        if reason == "SONG_PROOF_UNVERIFIED"
        or reason.startswith("SONG_HOST_VOCAL_")
        or reason.startswith("SONG_LIVE_PERFORMANCE_")
        or reason == "SONG_BACKGROUND_PLAYBACK_ONLY"
        or reason == CHANNEL_PROFILE.decision("host_not_singing_reason")
    )
    if not blocking:
        return decision
    merged_reasons = tuple(dict.fromkeys(tuple(decision.reason_codes) + blocking))
    return replace(decision, action=DecisionAction.BLOCK, reason_codes=merged_reasons)

def _merge_boundary_resolution_into_decision(decision, boundary_resolution: BoundaryResolution | None):
    if boundary_resolution is None:
        return decision
    merged_reasons = tuple(dict.fromkeys(tuple(decision.reason_codes) + tuple(boundary_resolution.reason_codes)))
    if boundary_resolution.action == DecisionAction.DROP:
        return replace(decision, action=DecisionAction.DROP, reason_codes=merged_reasons)
    if boundary_resolution.action == DecisionAction.BLOCK:
        return replace(decision, action=DecisionAction.BLOCK, reason_codes=merged_reasons)
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
        "schema_version": job_manifest.get("schema_version"),
        "job_id": job_manifest.get("job_id"),
        "candidate_id": job_manifest.get("candidate_id"),
        "job_kind": job_manifest.get("job_kind"),
        "provider": job_manifest.get("provider"),
        "content_type_hint": job_manifest.get("content_type_hint"),
        "song_candidate": job_manifest.get("song_candidate") is True,
        "requires_full_source_song_boundary_redo": job_manifest.get("requires_full_source_song_boundary_redo") is True,
        "timeline": dict(_mapping(job_manifest.get("timeline"))),
        "song_boundary": dict(_mapping(job_manifest.get("song_boundary"))),
        "lyrics_alignment": dict(_mapping(job_manifest.get("lyrics_alignment"))),
        "host_vocal_proof": dict(_mapping(job_manifest.get("host_vocal_proof"))),
        "song_repair_gate": dict(_mapping(job_manifest.get("song_repair_gate"))),
        "provenance": dict(_mapping(job_manifest.get("provenance"))),
        "cpa_semantic_request_path": job_manifest.get("cpa_semantic_request_path"),
        "cpa_semantic_response_path": job_manifest.get("cpa_semantic_response_path"),
        "semantic_qa": dict(_mapping(job_manifest.get("semantic_qa"))),
        "selector_stage": job_manifest.get("selector_stage"),
        "boundary_authority": job_manifest.get("boundary_authority"),
        "viewer_context_expansion": dict(_mapping(job_manifest.get("viewer_context_expansion"))) or None,
        "song_context_subtitle_fallback": dict(
            _mapping(job_manifest.get("song_context_subtitle_fallback"))
        )
        or None,
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
    strict_song_streams: bool = False,
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
        *(
            [
                "-map", "0:v:0",
                "-map", "0:a:0",
                "-sn",
                "-dn",
                "-map_metadata", "-1",
                "-map_chapters", "-1",
            ]
            if strict_song_streams
            else []
        ),
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

def _load_known_songs() -> list[Mapping[str, object]]:
    """Curated recurring-song table from the selected channel profile."""
    path = profile_asset_file("known_songs")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    songs = payload.get("songs") if isinstance(payload, Mapping) else None
    return [s for s in songs if isinstance(s, Mapping)] if isinstance(songs, list) else []

def _pinned_lrc_for_song(
    cues: Sequence[SourceCue],
    *,
    fetch_lrclib: Callable[[str], LrcResult | None] = fetch_lrclib_lrc,
    fetch_netease: Callable[[str], LrcResult | None] = fetch_netease_lrc,
) -> list[LrcResult]:
    """Deterministically pin a known song's LRC when its fingerprint lines appear
    in the window ASR — so song identification never depends on the flaky LLM
    hint + text search (which lost 《屑屑》 entirely, 2026-07-07).  Pins only ADD
    candidates; alignment ranking still proves them, so a wrong pin is harmless.
    """
    table = _load_known_songs()
    if not table:
        return []
    asr = normalize_lyric_text(" ".join(cue.text for cue in cues))
    if not asr:
        return []
    pinned: list[LrcResult] = []
    seen: set[str] = set()
    for song in table:
        fingerprints = [normalize_lyric_text(str(f)) for f in (song.get("fingerprint") or [])]
        hits = sum(1 for fp in fingerprints if fp and fp in asr)
        if hits < 2:
            continue
        lrc_ref = str(song.get("lrc_ref") or "").strip()
        netease_id = str(song.get("netease_id") or "").strip()
        lookup_ref = lrc_ref or (f"netease://song/{netease_id}" if netease_id else "")
        if not lookup_ref or lookup_ref in seen:
            continue
        if lookup_ref.lower().startswith(("lrclib://", "https://lrclib.net/", "http://lrclib.net/")):
            lrc = fetch_lrclib(lookup_ref)
        else:
            lrc = fetch_netease(lookup_ref)
        if lrc is not None and lrc.lines:
            seen.add(lookup_ref)
            pinned.append(lrc)
    return pinned

def _attempt_song_repair_stage(
    job_manifest: Mapping[str, object],
    cues: Sequence[SourceCue],
    *,
    candidate_id: str,
    output_dir: Path,
    lrc_provider: LrcProvider | None,
    hint_llm_call: LlmCall | None = None,
    extra_queries: Sequence[str] = (),
    source_media_path: Path | None = None,
    audio_lrc_aligner: AudioLrcAligner | None = None,
    asr_anchor_cues: Sequence[SourceCue] = (),
    pinned_lrc_resolver: Callable[[Sequence[SourceCue]], list[LrcResult]] = _pinned_lrc_for_song,
) -> tuple[Mapping[str, object], SongRepairResult | None]:
    """Repair-first: try to earn the full-song proof before review can BLOCK.

    Returns the (possibly repaired) job manifest plus the repair result for
    evidence.  A repaired manifest carries a song_boundary/lyrics_alignment
    pair that passes the hash-bound proof gate on its own merits.

    ``asr_anchor_cues`` is the fresh (non-AGY) full-source ASR transcript of
    the same source media — see ``attempt_song_repair``'s docstring for why
    the AGY-audio path needs it as an independent anchor for the lyric global
    shift.
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
        extra_queries=extra_queries,
        pinned_lrc_results=pinned_lrc_resolver(cues),
        source_media_path=source_media_path,
        audio_lrc_aligner=audio_lrc_aligner,
        asr_anchor_cues=asr_anchor_cues,
    )
    if result.repaired and result.song_boundary and result.lyrics_alignment:
        repaired_job = {
            **dict(job_manifest),
            "song_boundary": dict(result.song_boundary),
            "lyrics_alignment": dict(result.lyrics_alignment),
        }
        repaired_job.pop("song_repair_gate", None)
        return repaired_job, result
    reason_codes = result.reason_codes or ("SONG_LIVE_PERFORMANCE_UNPROVEN",)
    repair_gate = {
        "status": "BLOCKED",
        "reason_codes": list(reason_codes),
        "live_performance": dict(result.live_performance) if result.live_performance else None,
        "repair_report_path": result.report_path,
    }
    return {**dict(job_manifest), "song_repair_gate": repair_gate}, result

def _attempt_host_vocal_proof_stage(
    job_manifest: Mapping[str, object],
    *,
    candidate_id: str,
    source_media_path: Path | None,
    output_dir: Path,
    host_vocal_prover: HostVocalProver | None,
) -> Mapping[str, object]:
    """Mint the independent performer-identity proof after lyric repair.

    LRC alignment proves which song is present, not who is singing it.  Every
    song path (ASR-rich and audio+LRC fallback alike) passes through this stage.
    Missing runtime/model/reference inputs are recorded and later block; they
    never silently fall back to the old LRC-only completion rule.
    """

    if not _job_is_song_candidate(job_manifest):
        return job_manifest
    boundary = _mapping(job_manifest.get("song_boundary"))
    alignment = _mapping(job_manifest.get("lyrics_alignment"))
    if not _song_boundary_ready(boundary) or _verify_lyrics_alignment_proof(alignment, output_dir=output_dir) is not None:
        return job_manifest
    performance_error = _verify_live_performance_observation(alignment, output_dir=output_dir)
    performance_reasons = (
        () if performance_error is None else _live_performance_block_reasons(alignment, output_dir=output_dir)
    )
    performance_claim: dict[str, object] = {
        "status": "READY" if performance_error is None else "BLOCKED",
        "mode": "LIVE_STREAMER_SINGING" if performance_error is None else "UNPROVEN",
        "reason_code": None if performance_error is None else performance_reasons[0],
        "reason_codes": list(performance_reasons),
        "error": performance_error,
        "alignment_report_path": alignment.get("alignment_report_path"),
        "alignment_report_sha256": alignment.get("alignment_report_sha256"),
    }
    with_performance = {**dict(job_manifest), "live_performance_proof": performance_claim}
    if performance_error is not None:
        stale_host = with_performance.pop("host_vocal_proof", None)
        if stale_host:
            with_performance["superseded_host_vocal_proof"] = stale_host
        return with_performance
    existing = _mapping(job_manifest.get("host_vocal_proof"))
    if existing.get("status") == "READY":
        return _tighten_song_boundary_to_verified_host_anchor(
            with_performance,
            candidate_id=candidate_id,
            alignment=alignment,
            claim=existing,
        )
    if source_media_path is None or not source_media_path.is_file():
        claim: Mapping[str, object] = {
            "status": "ERROR",
            "decision": "UNKNOWN",
            "reason_code": "SONG_HOST_VOCAL_VERIFIER_UNAVAILABLE",
            "error": "source media for host-vocal verification is unavailable",
        }
    elif host_vocal_prover is None:
        claim = {
            "status": "MISSING",
            "decision": "UNKNOWN",
            "reason_code": "SONG_HOST_VOCAL_UNPROVEN",
            "error": "host-vocal prover is not configured",
        }
    else:
        try:
            claim = host_vocal_prover(
                source_media_path,
                candidate_id,
                boundary,
                alignment,
                output_dir / "host_vocal_proof",
            )
        except Exception as exc:  # fail closed at the orchestration boundary
            claim = {
                "status": "ERROR",
                "decision": "UNKNOWN",
                "reason_code": "SONG_HOST_VOCAL_VERIFIER_UNAVAILABLE",
                "error": f"{type(exc).__name__}: {exc}",
            }
    with_host_claim = {**with_performance, "host_vocal_proof": dict(claim)}
    return _tighten_song_boundary_to_verified_host_anchor(
        with_host_claim,
        candidate_id=candidate_id,
        alignment=alignment,
        claim=_mapping(claim),
    )

def _job_is_song_candidate(job_manifest: Mapping[str, object]) -> bool:
    if job_manifest.get("song_candidate") is True:
        return True
    if job_manifest.get("requires_full_source_song_boundary_redo") is True:
        return True
    if job_manifest.get("content_type_hint") == "song":
        return True
    # a claimed-but-unproven song boundary also deserves a repair attempt
    return _song_boundary_ready(_mapping(job_manifest.get("song_boundary")))

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
