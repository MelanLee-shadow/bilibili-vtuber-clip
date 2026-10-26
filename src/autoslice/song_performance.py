"""Hash-bound audio/LRC artifacts and live-performance validation."""

from __future__ import annotations

from src.autoslice.cpa_gemini_client import valid_lrc_transport

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.gemini_backup_policy import validate_key_acceptance_metadata
from src.autoslice.review_evidence import SourceCue
from src.autoslice.song_common import (
    AGY_AUDIO_LRC_CANONICALIZATION_STRATEGY,
    AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION,
    AGY_AUDIO_LRC_PROVIDER,
    AGY_AUDIO_LRC_RUN_SCHEMA_VERSION,
    AudioLrcAlignmentRun,
    CHANNEL_PROFILE,
    GEMINI_API_AUDIO_LRC_PROVIDER,
    HOST_LYRIC_ROLES,
    HOST_LYRIC_SUBJECT,
    LIVE_ARRANGEMENT_CLASSIFICATIONS,
    LIVE_ARRANGEMENT_TRANSITIONS,
    LIVE_PERFORMANCE_MODES,
    LIVE_PERFORMANCE_READY_MODE,
    LYRIC_VOCAL_ASSERTION_KEYS,
    LYRIC_VOCAL_SUBJECTS,
    LivePerformanceRejected,
    LrcLine,
    LrcResult,
    MAX_LIVE_ARRANGEMENT_INTERLINE_GAP_MS,
    MAX_READY_CONSECUTIVE_SPOKEN_LYRIC_ROWS,
    MAX_READY_SPOKEN_BLOCKS,
    MAX_READY_SPOKEN_BLOCK_SPAN_MS,
    MAX_READY_SPOKEN_LYRIC_DURATION_MS,
    MAX_READY_SPOKEN_LYRIC_DURATION_RATIO,
    MIN_LIVE_ARRANGEMENT_DURATION_MS,
    MIN_LIVE_ARRANGEMENT_HEARD_RATIO,
    MIN_LIVE_ARRANGEMENT_HEARD_ROWS,
    MIN_READY_SUNG_LYRIC_RATIO,
    MIN_READY_SUNG_LYRIC_ROWS,
    SelectedSong,
    is_lrc_non_lyric_metadata,
    validate_audio_lrc_execution_metadata,
)
from src.autoslice.post_song_talk_witness import (
    validate_post_song_transition_pair,
    witness_post_song_talk_start,
)
from src.autoslice.song_instrumental_proof import prove_long_instrumental_spans
from src.autoslice.song_lrc_provider import parse_lrc_text
from src.autoslice.song_alignment import validate_audio_lrc_canonical_projection
from src.autoslice.song_alignment import (
    TIMING_MODEL,
    AsrAnchoredTimeline,
    asr_cues_sha256,
    serialize_asr_cue,
    build_asr_anchored_timeline,
    AsrTimelinePending,
    performance_rows_on_asr_clock,
)
from src.autoslice.song_clip_bounds import audio_onset_evidence, song_clip_start
from src.autoslice.song_omission_shape import derive_omission_shape
from src.autoslice.song_spoken_interlude import lyric_texts

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_bound_artifact(path_value: str, expected_sha256: str, label: str) -> Path:
    path = Path(path_value)
    if not path.is_file():
        raise ValueError(f"{label} artifact is missing: {path}")
    actual = _sha256_file(path)
    if actual != str(expected_sha256).lower().removeprefix("sha256:"):
        raise ValueError(f"{label} sha256 mismatch")
    return path


def load_audio_lrc_json_artifact(path: Path, label: str) -> object:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"{label} cannot be read: {exc}") from exc
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().casefold() in {"```", "```json"}:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON: {exc}") from exc


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def untimed_lrc_text(lrc: LrcResult) -> str:
    """Only exact row identity and lyric text cross the observation boundary."""
    return "\n".join(json.dumps({"lrc_index": i, "text": line.text}, ensure_ascii=False)
                     for i, line in enumerate(lrc.lines)) + "\n"


def validate_untimed_audio_tool_trace(trace: str, *, audio_path: Path,
                                    lyrics_path: Path, prompt_path: Path,
                                    output_path: Path) -> None:
    """Validate actual AGY tool completion, not the model's listening claim."""
    allowed = {str(p.resolve()) for p in (audio_path, lyrics_path, prompt_path, output_path)}
    completed_reads: set[str] = set()
    wrote_output = False
    succeeded = False
    try:
        events = [json.loads(line) for line in trace.splitlines() if line.strip()]
    except (ValueError, TypeError) as exc:
        raise ValueError("AUDIO_TOOL_TRACE_UNAVAILABLE") from exc
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("AUDIO_TOOL_TRACE_UNAVAILABLE")
        if event.get("event") == "result":
            succeeded = event.get("result", {}).get("status") == "SUCCESS"
        step = event.get("step_update", {})
        if step.get("step_type") != "tool":
            continue
        info = step.get("tool_info", {})
        name = info.get("name", step.get("tool_name"))
        params = info.get("parameters", {})
        path = params.get("AbsolutePath") if name == "view_file" else params.get("TargetFile")
        if name not in {"view_file", "write_to_file"} or not isinstance(path, str):
            raise ValueError("REFERENCE_TIMING_EXPOSURE_UNKNOWN")
        path = str(Path(path).resolve())
        if path not in allowed:
            raise ValueError("REFERENCE_TIMING_EXPOSED")
        if step.get("state") != "DONE" or info.get("error"):
            continue
        if name == "view_file":
            completed_reads.add(path)
        elif path == str(output_path.resolve()):
            if str(audio_path.resolve()) not in completed_reads:
                raise ValueError("AUDIO_NOT_CONSUMED_BEFORE_OBSERVATION")
            wrote_output = True
    if not succeeded or not wrote_output or not {
        str(audio_path.resolve()), str(lyrics_path.resolve())
    }.issubset(completed_reads):
        raise ValueError("AUDIO_TOOL_CONSUMPTION_UNPROVEN")


def _independent_timing_input(run: AudioLrcAlignmentRun, lrc: LrcResult) -> bool:
    """Optional new receipt; legacy equal timestamps remain unproven, not false."""
    if not getattr(run, "manifest_path", None):
        return False
    manifest_path = _require_bound_artifact(run.manifest_path, run.manifest_sha256, "audio manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    evidence = manifest.get("observation_input")
    if evidence is None:
        return False
    if not isinstance(evidence, dict) or evidence.get("protocol") != "untimed-lyrics-full-audio.v1":
        raise ValueError("independent timing input protocol is invalid")
    if evidence.get("source_sha256") != run.source_sha256 or evidence.get("source_offset_ms") != 0:
        raise ValueError("independent timing input source clock is invalid")
    audio = _require_bound_artifact(evidence.get("audio_path"), evidence.get("audio_sha256"), "observation audio")
    lyrics = _require_bound_artifact(evidence.get("lyrics_path"), evidence.get("lyrics_sha256"), "untimed lyrics")
    duration = evidence.get("audio_duration_ms")
    if not _is_int(duration) or abs(duration - run.source_duration_ms) > 1000:
        raise ValueError("independent timing input duration is invalid")
    if lyrics.read_text(encoding="utf-8") != untimed_lrc_text(lrc):
        raise ValueError("observation lyrics contain reference timing or differ from canonical text")
    if run.provider == AGY_AUDIO_LRC_PROVIDER:
        if 'cpa_transport_receipt' in manifest:
            from .cpa_gemini_client import valid_lrc_transport
            if not valid_lrc_transport(manifest):
                raise ValueError('CPA_GEMINI_INPUT_RECEIPT_MISMATCH')
        else:
            trace = _require_bound_artifact(evidence.get("tool_trace_path"), evidence.get("tool_trace_sha256"), "audio tool trace")
            validate_untimed_audio_tool_trace(trace.read_text(encoding="utf-8"), audio_path=audio,
                lyrics_path=lyrics, prompt_path=Path(run.prompt_path),
                output_path=Path(run.provider_raw_output_path))
    elif run.provider == GEMINI_API_AUDIO_LRC_PROVIDER:
        if str(audio) != run.api_audio_path or evidence.get("audio_sha256") != run.api_audio_sha256:
            raise ValueError("independent timing input differs from submitted API audio")
        prompt = _require_bound_artifact(run.prompt_path, run.prompt_sha256, "API prompt").read_text(encoding="utf-8")
        if untimed_lrc_text(lrc).strip() not in prompt:
            raise ValueError("API prompt does not contain the bound untimed lyrics")
    else:
        raise ValueError("independent timing input provider is invalid")
    return True


def derive_live_arrangement_completeness(
    *,
    observations: object,
    live_arrangement: object,
    post_song_talk_start_ms: object,
    source_duration_ms: int,
    spot_checks: object = None,
    live_performance: object = None,
    timing_alignment: Sequence[Mapping[str, object]] | None = None,
    asr_anchor_cues: Sequence[SourceCue] | None = None,
) -> dict[str, object]:
    """Derive whether the *performed live arrangement* is complete.

    A synchronized studio LRC is a canonical text/timing reference, not a
    command to reproduce every studio repeat.  Unheard lyric rows are judged
    only by ``song_omission_shape`` (small skips, repeated sections, spoken
    interludes and their whole-song budgets).  The model's classification is
    only an observation, recorded when it disagrees: code recomputes coverage,
    omission shape, duration, ordering, and the post-song boundary before
    returning a READY-shaped result.  ``asr_anchor_cues=None`` re-derives a
    legacy report without its (absent) bound transcript.
    """

    if not _is_int(source_duration_ms) or source_duration_ms <= 0:
        raise ValueError("live arrangement source duration is invalid")
    if not isinstance(live_arrangement, Mapping) or set(live_arrangement) != {
        "classification",
        "observed_live_song_opening",
        "observed_live_song_ending",
        "post_song_transition_kind",
        "post_song_transition_ms",
        "notes",
    }:
        raise ValueError("live arrangement observation schema is invalid")
    claimed_classification = live_arrangement.get("classification")
    transition_kind = live_arrangement.get("post_song_transition_kind")
    transition_ms = live_arrangement.get("post_song_transition_ms")
    if (
        claimed_classification not in LIVE_ARRANGEMENT_CLASSIFICATIONS
        or transition_kind not in LIVE_ARRANGEMENT_TRANSITIONS
        or not isinstance(live_arrangement.get("observed_live_song_opening"), bool)
        or not isinstance(live_arrangement.get("observed_live_song_ending"), bool)
        or not isinstance(live_arrangement.get("notes"), str)
        or not str(live_arrangement.get("notes")).strip()
    ):
        raise ValueError("live arrangement observation values are invalid")
    if (
        not isinstance(observations, Sequence)
        or isinstance(observations, (str, bytes, bytearray))
        or len(observations) < MIN_LIVE_ARRANGEMENT_HEARD_ROWS
    ):
        raise ValueError("live arrangement canonical observations are missing")

    # All proof modes use one metadata definition. Preserve raw row identities
    # at the artifact boundary, and count only lyrics inside this calculation.
    for index, row in enumerate(observations):
        if not isinstance(row, Mapping) or row.get("lrc_index") != index:
            raise ValueError(f"live arrangement canonical row {index} is invalid")
    keep = [i for i, r in enumerate(observations) if not is_lrc_non_lyric_metadata(str(r.get("text", "")))]
    renumber = {old: new for new, old in enumerate(keep)}
    observations = [dict(observations[i], lrc_index=renumber[i]) for i in keep]
    if timing_alignment is not None:
        timing_alignment = [dict(r, canonical_lrc_index=renumber[int(r["canonical_lrc_index"])])
                            for r in timing_alignment if int(r["canonical_lrc_index"]) in renumber]
    canonical_count = len(observations)
    heard_rows: list[tuple[int, Mapping[str, object], int, int]] = []
    omitted_indices: list[int] = []
    previous_start_ms: int | None = None
    previous_end_ms: int | None = None
    max_interline_gap_ms = 0
    interline_gap_bounds: list[tuple[int, int, int, int]] = []
    for index, row in enumerate(observations):
        if not isinstance(row, Mapping) or row.get("lrc_index") != index:
            raise ValueError(f"live arrangement canonical row {index} is invalid")
        heard = row.get("heard")
        confidence = row.get("confidence")
        if (
            not isinstance(heard, bool)
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.8 <= float(confidence) <= 1.0
        ):
            raise ValueError(f"live arrangement canonical row {index} confidence/heard is invalid")
        if not heard:
            if (
                row.get("live_start_ms") is not None
                or row.get("live_end_ms") is not None
                or row.get("lyric_vocal_subject") != "NO_AUDIBLE_LYRIC_VOCAL"
                or row.get("lidousha_role") != "SILENT_OR_NOT_AUDIBLE"
                or row.get("same_live_vocal_source_as_lidousha") is not False
                or row.get("other_singer_or_harmony_audible") is not False
                or row.get("recorded_or_playback_vocal_audible") is not False
            ):
                raise ValueError(f"live arrangement omitted row {index} is not an honest no-audible-lyric row")
            omitted_indices.append(index)
            continue
        if timing_alignment is not None:
            heard_rows.append((index, row, 0, 0))
            continue
        start_ms = row.get("live_start_ms")
        end_ms = row.get("live_end_ms")
        if not (
            _is_int(start_ms)
            and _is_int(end_ms)
            and 0 <= start_ms < end_ms <= source_duration_ms
        ):
            raise ValueError(f"live arrangement heard row {index} timing is invalid")
        if previous_start_ms is not None and start_ms <= previous_start_ms:
            raise ValueError("live arrangement heard lyric starts are not strictly monotonic")
        if previous_end_ms is not None and previous_end_ms - start_ms > 250:
            raise ValueError("live arrangement adjacent heard lyrics overlap by more than 250ms")
        if previous_end_ms is not None:
            current_gap_ms = start_ms - previous_end_ms
            if current_gap_ms > MAX_LIVE_ARRANGEMENT_INTERLINE_GAP_MS:
                interline_gap_bounds.append(
                    (
                        heard_rows[-1][0],
                        index,
                        int(previous_end_ms),
                        int(start_ms),
                    )
                )
            if current_gap_ms > max_interline_gap_ms:
                max_interline_gap_ms = current_gap_ms
        heard_rows.append((index, row, int(start_ms), int(end_ms)))
        previous_start_ms = int(start_ms)
        previous_end_ms = int(end_ms)

    heard_count = len(heard_rows)
    heard_ratio = heard_count / max(1, canonical_count)
    if heard_count < MIN_LIVE_ARRANGEMENT_HEARD_ROWS or heard_ratio < MIN_LIVE_ARRANGEMENT_HEARD_RATIO:
        raise ValueError(
            "live arrangement has too little canonical lyric evidence: "
            f"{heard_count}/{canonical_count} rows"
        )
    if timing_alignment is not None:
        heard_rows = [
            (int(r["canonical_lrc_index"]), observations[int(r["canonical_lrc_index"])],
             int(r["live_time_ms"]), int(r["live_end_ms"])) for r in timing_alignment
        ]
        if not heard_rows:
            raise AsrTimelinePending("alignment pending: no performed rows")
        for previous, current in zip(heard_rows, heard_rows[1:]):
            if current[2] <= previous[2] or current[2] < previous[3]:
                raise AsrTimelinePending("alignment pending: performed rows overlap")
            gap = current[2] - previous[3]
            max_interline_gap_ms = max(max_interline_gap_ms, gap)
            if gap > MAX_LIVE_ARRANGEMENT_INTERLINE_GAP_MS:
                interline_gap_bounds.append((previous[0], current[0], previous[3], current[2]))
    first_index, _first_row, first_start_ms, _first_end_ms = heard_rows[0]
    last_index, _last_row, _last_start_ms, last_end_ms = heard_rows[-1]
    performed_duration_ms = last_end_ms - first_start_ms
    if performed_duration_ms < MIN_LIVE_ARRANGEMENT_DURATION_MS:
        raise ValueError(
            f"live arrangement performed span is too short: {performed_duration_ms}ms"
        )
    middle_start = canonical_count // 3
    tail_start = (canonical_count * 2) // 3
    heard_indices = {index for index, _row, _start, _end in heard_rows}
    if (
        first_index > (1 if timing_alignment is not None else 0)
        or not any(middle_start <= index < tail_start for index in heard_indices)
        or (max(heard_indices) if timing_alignment is not None else last_index) < tail_start
    ):
        raise ValueError("live arrangement does not prove canonical head, middle, and performed tail coverage")
    if (
        live_arrangement.get("observed_live_song_opening") is not True
        or live_arrangement.get("observed_live_song_ending") is not True
    ):
        raise ValueError("live arrangement opening or actual live ending was not observed")

    if timing_alignment is not None:
        # Anchored rows bound a hole's live window by their actual source cue;
        # unanchored rows by their section-shifted reference time.
        performed = [
            (int(r["canonical_lrc_index"]),
             int(r["cue_start_ms"]) if r.get("matched_cue_id") is not None else int(r["live_time_ms"]),
             int(r["cue_end_ms"]) if r.get("matched_cue_id") is not None else int(r["live_end_ms"]))
            for r in timing_alignment
        ]
    else:
        performed = [(index, start, end) for index, _row, start, end in heard_rows]
    shape = derive_omission_shape(
        lyric_rows=observations, omitted_indices=omitted_indices, performed=performed,
        raw_index=keep, asr_anchor_cues=asr_anchor_cues,
        trailing_window_end_ms=transition_ms if _is_int(transition_ms) else None,
    )
    if shape.speech_cues:
        # Unlabelled source speech is attributed to the host only inside an
        # independently proven continuous, solo-host live performance.
        error = validate_live_performance_observation(
            live_performance, first_lyric_start_ms=0, last_lyric_end_ms=source_duration_ms,
            observations=[r for r in observations if r["heard"]],
            require_ready=True, check_timing=False,
        )
        if error is not None:
            raise ValueError(error)
    derived_ranges = shape.ranges
    derived_classification = shape.classification

    validate_post_song_transition_pair(
        transition_kind=transition_kind,
        transition_ms=transition_ms,
        post_song_talk_start_ms=post_song_talk_start_ms,
        source_duration_ms=source_duration_ms,
    )
    assert _is_int(transition_ms)
    if not last_end_ms <= transition_ms <= source_duration_ms:
        raise ValueError("live arrangement post-song transition is outside the actual ending boundary")
    instrumental_gap_proof = prove_long_instrumental_spans(
        observations=observations,
        heard_rows=heard_rows,
        live_arrangement=live_arrangement,
        live_performance=live_performance,
        spot_checks=spot_checks,
        interline_gap_bounds=interline_gap_bounds,
        last_index=last_index,
        last_end_ms=last_end_ms,
        transition_ms=int(transition_ms),
    )

    result = {
        "classification": derived_classification,
        "canonical_line_count": canonical_count,
        "heard_line_count": heard_count,
        "heard_line_ratio": round(heard_ratio, 4),
        "performed_duration_ms": performed_duration_ms,
        "first_heard_lrc_index": first_index,
        "last_heard_lrc_index": last_index,
        "max_interline_gap_ms": max_interline_gap_ms,
        "omitted_ranges": derived_ranges,
        "observed_live_song_opening": True,
        "observed_live_song_ending": True,
        "post_song_transition_kind": transition_kind,
        "post_song_transition_ms": transition_ms,
    }
    if instrumental_gap_proof is not None:
        result["instrumental_gap_proof"] = instrumental_gap_proof
    if claimed_classification != derived_classification:
        # The provider label is an observation; record disagreement honestly.
        result["model_classification_disagreement"] = claimed_classification
    return result


def _validate_audio_lrc_artifact_bindings(
    *,
    run: AudioLrcAlignmentRun,
    lrc: LrcResult,
    candidate_id: str,
    source_media_path: Path,
    source_duration_ms: int,
) -> int:
    if run.provider == AGY_AUDIO_LRC_PROVIDER:
        preliminary_sandbox = True
    elif run.provider == GEMINI_API_AUDIO_LRC_PROVIDER:
        preliminary_sandbox = False
    else:
        preliminary_sandbox = None
    execution_error = validate_audio_lrc_execution_metadata(
        provider=run.provider,
        model=run.model,
        agy_rc=run.rc,
        provider_fallback_used=run.provider_fallback_used,
        agy_failure_category=run.agy_failure_category,
        sandbox=preliminary_sandbox,
    )
    if execution_error is not None:
        raise ValueError(execution_error)
    if not source_media_path.is_file():
        raise ValueError(f"current source media is missing: {source_media_path}")
    try:
        current_source_origin = str(source_media_path.resolve(strict=True))
        declared_source_origin = str(Path(run.source_origin_path).resolve(strict=True))
    except OSError as exc:
        raise ValueError(f"audio source origin cannot be resolved: {exc}") from exc
    if declared_source_origin != current_source_origin:
        raise ValueError("audio observation source origin is not the current source media")
    source_path = _require_bound_artifact(run.source_path, run.source_sha256, "audio source")
    if _sha256_file(source_media_path) != run.source_sha256 or _sha256_file(source_path) != run.source_sha256:
        raise ValueError("audio observation is not bound to the current source media")
    lrc_path = _require_bound_artifact(run.lrc_path, run.lrc_sha256, "canonical LRC")
    _require_bound_artifact(run.prompt_path, run.prompt_sha256, "audio prompt")
    if not run.provider_raw_output_path or not run.provider_raw_output_sha256:
        raise ValueError("provider raw audio alignment binding is missing")
    provider_raw_path = _require_bound_artifact(
        run.provider_raw_output_path,
        run.provider_raw_output_sha256,
        "provider raw audio alignment",
    )
    canonical_output_path = _require_bound_artifact(
        run.output_path,
        run.output_sha256,
        "canonicalized audio alignment",
    )
    manifest_path = _require_bound_artifact(run.manifest_path, run.manifest_sha256, "audio run manifest")
    try:
        run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"audio run manifest is invalid JSON: {exc}") from exc
    manifest_artifacts = run_manifest.get("artifacts") if isinstance(run_manifest, Mapping) else None
    manifest_execution_error = (
        validate_audio_lrc_execution_metadata(
            provider=run_manifest.get("provider"),
            model=run_manifest.get("model"),
            agy_rc=run_manifest.get("agy_rc"),
            provider_fallback_used=run_manifest.get("provider_fallback_used"),
            agy_failure_category=run_manifest.get("agy_failure_category"),
            sandbox=run_manifest.get("sandbox"),
            cpa_transport_valid=valid_lrc_transport(run_manifest),
        )
        if isinstance(run_manifest, Mapping)
        else "audio run manifest is not a JSON object"
    )
    if (
        not isinstance(run_manifest, Mapping)
        or run_manifest.get("schema_version") != AGY_AUDIO_LRC_RUN_SCHEMA_VERSION
        or run_manifest.get("candidate_id") != candidate_id
        or run_manifest.get("provider") != run.provider
        or run_manifest.get("model") != run.model
        or run_manifest.get("agy_rc") != run.rc
        or run_manifest.get("provider_fallback_used") is not run.provider_fallback_used
        or run_manifest.get("agy_failure_category") != run.agy_failure_category
        or manifest_execution_error is not None
        or not isinstance(manifest_artifacts, Mapping)
        or any(
            manifest_artifacts.get(key) != value
            for key, value in (
                ("source_path", run.source_path),
                ("source_origin_path", run.source_origin_path),
                ("source_sha256", run.source_sha256),
                ("source_duration_ms", run.source_duration_ms),
                ("lrc_path", run.lrc_path),
                ("lrc_sha256", run.lrc_sha256),
                ("prompt_path", run.prompt_path),
                ("prompt_sha256", run.prompt_sha256),
                ("provider_raw_output_path", run.provider_raw_output_path),
                ("provider_raw_output_sha256", run.provider_raw_output_sha256),
                ("output_path", run.output_path),
                ("output_sha256", run.output_sha256),
            )
        )
    ):
        raise ValueError("audio run manifest is not bound to the current run artifacts")
    if run.provider == GEMINI_API_AUDIO_LRC_PROVIDER:
        acceptance_error = validate_key_acceptance_metadata(
            configured_key_count=run.configured_key_count,
            accepted_key_ordinal=run.accepted_key_ordinal,
            accepted_key_tier=run.accepted_key_tier,
            paid_backup_policy=run.paid_backup_policy,
        )
        if (
            acceptance_error is not None
            or run_manifest.get("configured_key_count") != run.configured_key_count
            or run_manifest.get("accepted_key_ordinal") != run.accepted_key_ordinal
            or run_manifest.get("direct_audio_input") is not True
            or not run.api_audio_path
            or not run.api_audio_sha256
            or not _is_int(run.api_audio_duration_ms)
        ):
            raise ValueError(acceptance_error or "Gemini API audio failover metadata is incomplete")
        key_tier = run.accepted_key_tier or "free"
        if key_tier == "free":
            # Legacy manifests predate accepted_key_tier; a free acceptance
            # must stay inside the configured free-key range and must not
            # carry any paid-policy stamp.
            if (
                not 1 <= int(run.accepted_key_ordinal) <= int(run.configured_key_count)
                or run_manifest.get("accepted_key_tier") not in (None, "free")
                or run.paid_backup_policy is not None
                or run_manifest.get("paid_backup_policy") is not None
            ):
                raise ValueError("Gemini API audio failover metadata is incomplete")
        elif key_tier == "paid_backup":





            policy = run.paid_backup_policy
            if (
                int(run.accepted_key_ordinal) != int(run.configured_key_count) + 1
                or run_manifest.get("accepted_key_tier") != "paid_backup"
                or not isinstance(policy, Mapping)
                or run_manifest.get("paid_backup_policy") != dict(policy)
            ):
                raise ValueError("paid Gemini backup acceptance violates the usage gate")
        else:
            raise ValueError("Gemini API audio failover key tier is unknown")
        api_audio_path = _require_bound_artifact(
            run.api_audio_path,
            run.api_audio_sha256,
            "Gemini API complete audio",
        )
        if (
            abs(int(run.api_audio_duration_ms) - run.source_duration_ms) > 1_000
            or manifest_artifacts.get("api_audio_path") != run.api_audio_path
            or manifest_artifacts.get("api_audio_sha256") != run.api_audio_sha256
            or manifest_artifacts.get("api_audio_duration_ms") != run.api_audio_duration_ms
            or _sha256_file(api_audio_path) != run.api_audio_sha256
        ):
            raise ValueError("Gemini API audio failover is not bound to the complete current audio")
    elif any(
        value is not None
        for value in (
            run.configured_key_count,
            run.accepted_key_ordinal,
            run.accepted_key_tier,
            run.paid_backup_policy,
            run.api_audio_path,
            run.api_audio_sha256,
            run.api_audio_duration_ms,
        )
    ):
        raise ValueError("clean AGY run unexpectedly contains Gemini API fallback metadata")
    canonicalization = run_manifest.get("canonicalization")
    expected_canonicalization = {
        "strategy": AGY_AUDIO_LRC_CANONICALIZATION_STRATEGY,
        "row_identity": "strict_zero_based_lrc_index",
        "restored_fields": ["lrc_time_ms", "text"],
        "row_count": len(lrc.lines),
        "canonical_lrc_sha256": run.lrc_sha256,
        "provider_raw_output_sha256": run.provider_raw_output_sha256,
        "canonicalized_output_sha256": run.output_sha256,
    }
    if canonicalization != expected_canonicalization:
        raise ValueError("audio run canonicalization manifest is invalid")
    parsed_lrc = parse_lrc_text(lrc_path.read_text(encoding="utf-8"), exclude_metadata=False)
    if [(line.time_ms, line.text) for line in parsed_lrc] != [(line.time_ms, line.text) for line in lrc.lines]:
        raise ValueError("bound LRC artifact does not equal the selected canonical LRC")
    if not _is_int(run.source_duration_ms) or abs(run.source_duration_ms - source_duration_ms) > 1_000:
        raise ValueError(
            f"audio source duration {run.source_duration_ms}ms does not match job duration {source_duration_ms}ms"
        )
    effective_duration_ms = min(run.source_duration_ms, source_duration_ms)

    provider_payload = load_audio_lrc_json_artifact(
        provider_raw_path,
        "provider raw audio alignment",
    )
    canonical_artifact_payload = load_audio_lrc_json_artifact(
        canonical_output_path,
        "canonicalized audio alignment",
    )
    expected_payload = validate_audio_lrc_canonical_projection(
        provider_payload=provider_payload,
        canonical_payload=canonical_artifact_payload,
        lrc_path=lrc_path,
    )
    if run.payload != expected_payload:
        raise ValueError("in-memory audio alignment differs from the bound canonical projection")
    return effective_duration_ms


@dataclass(frozen=True)
class AudioObservationAlignment:
    """Validated AGY performance evidence plus the ASR-anchored lyric timeline.

    ``alignment`` rows are the performed lines (repeats included) on the
    source clock from ``timeline``. AGY supplies the vocalist assertions;
    its original timestamps are retained only as diagnostics.
    """

    payload: Mapping[str, object]
    alignment: list[dict[str, object]]
    performed_lines: list[LrcLine]
    performed_observations: list[Mapping[str, object]]
    arrangement_completeness: Mapping[str, object]
    offset_ms: int
    agy_alignment: list[dict[str, object]]
    timeline: AsrAnchoredTimeline
    timing_basis: str
    agy_offset_ms: int
    asr_cues_sha256: str | None


def _build_audio_observation_alignment(
    *,
    run: AudioLrcAlignmentRun,
    lrc: LrcResult,
    candidate_id: str,
    effective_duration_ms: int,
    asr_anchor_cues: Sequence[SourceCue] = (),
) -> AudioObservationAlignment:
    # Both fresh observations and reloaded receipts need an independent input
    # clock. Timestamp jitter or a short row list cannot establish its origin.
    if not _independent_timing_input(run, lrc):
        raise ValueError(
            "AUDIO_TOOL_CONSUMPTION_UNPROVEN: independent timing input is required "
            "(alignment pending)"
        )
    payload = run.payload
    required_top = {
        "schema_version",
        "record",
        "observations",
        "spot_checks",
        "live_performance",
        "live_arrangement",
        "post_song_talk_start_ms",
    }
    if not isinstance(payload, Mapping) or set(payload) != required_top:
        raise ValueError("audio observation top-level schema/keys are invalid")
    if payload.get("schema_version") != AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION:
        raise ValueError("audio observation schema_version is invalid")
    record = payload.get("record")
    if not isinstance(record, Mapping) or set(record) != {
        "attempt_id",
        "candidate_id",
        "source_sha256",
        "lrc_sha256",
        "source_duration_ms",
    }:
        raise ValueError("audio observation record is invalid")
    if record.get("candidate_id") != candidate_id:
        raise ValueError("audio observation candidate_id mismatch")
    if record.get("source_sha256") != run.source_sha256 or record.get("lrc_sha256") != run.lrc_sha256:
        raise ValueError("audio observation echoed artifact hash mismatch")
    if record.get("source_duration_ms") != run.source_duration_ms:
        raise ValueError("audio observation echoed duration mismatch")
    attempt_id = record.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("audio observation attempt_id is missing")

    observations = payload.get("observations")
    if not isinstance(observations, list) or len(observations) != len(lrc.lines) or len(observations) < 8:
        raise ValueError("audio observation must contain exactly one row per canonical LRC line")
    agy_alignment: list[dict[str, object]] = []
    performed_lines: list[LrcLine] = []
    performed_observations: list[Mapping[str, object]] = []
    residuals: list[int] = []
    for index, (row, line) in enumerate(zip(observations, lrc.lines)):
        if not isinstance(row, Mapping) or set(row) != {
            "lrc_index",
            "lrc_time_ms",
            "text",
            "heard",
            "live_start_ms",
            "live_end_ms",
            "confidence",
            *LYRIC_VOCAL_ASSERTION_KEYS,
        }:
            raise ValueError(f"audio observation row {index} has invalid keys")
        if row.get("lrc_index") != index or row.get("lrc_time_ms") != line.time_ms or row.get("text") != line.text:
            raise ValueError(f"audio observation row {index} does not exactly echo the canonical LRC")
        if not isinstance(row.get("heard"), bool):
            raise ValueError(f"audio observation row {index} heard value is invalid")
        if is_lrc_non_lyric_metadata(line.text):
            continue
        if row.get("heard") is not True:
            # Omission authority comes from the shared structural proof below,
            # never a provider's classification label.
            continue
        start_ms = row.get("live_start_ms")
        end_ms = row.get("live_end_ms")
        confidence = row.get("confidence")
        if not (_is_int(start_ms) and _is_int(end_ms) and 0 <= start_ms < end_ms <= effective_duration_ms):
            raise ValueError(f"audio observation row {index} timing is invalid")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.8 <= confidence <= 1.0:
            raise ValueError(f"audio observation row {index} confidence is below 0.8")
        residuals.append(start_ms - line.time_ms)
        agy_alignment.append(
            {
                "lrc_time_ms": line.time_ms,
                "lrc_text": line.text,
                "canonical_lrc_index": index,
                "matched_cue_id": f"agy-audio:{run.output_sha256[:12]}:line-{index}",
                "cue_start_ms": start_ms,
                "cue_end_ms": end_ms,
                "match_ratio": round(float(confidence), 4),
                "evidence_source": "agy_audio_lrc",
                "lyric_vocal_subject": row.get("lyric_vocal_subject"),
                "lidousha_role": row.get("lidousha_role"),
                "same_live_vocal_source_as_lidousha": row.get(
                    "same_live_vocal_source_as_lidousha"
                ),
                "other_singer_or_harmony_audible": row.get(
                    "other_singer_or_harmony_audible"
                ),
                "recorded_or_playback_vocal_audible": row.get(
                    "recorded_or_playback_vocal_audible"
                ),
            }
        )
        performed_lines.append(line)
        performed_observations.append(row)

    if not agy_alignment:
        raise ValueError("live arrangement has no heard canonical lyrics")
    live_performance = payload.get("live_performance")
    performance_schema_error = validate_live_performance_observation(
        live_performance, first_lyric_start_ms=0, last_lyric_end_ms=effective_duration_ms,
        observations=performed_observations, require_ready=False, check_timing=False,
    )
    if performance_schema_error is not None:
        raise ValueError(performance_schema_error)
    performance_error = validate_live_performance_observation(
        live_performance, first_lyric_start_ms=0, last_lyric_end_ms=effective_duration_ms,
        observations=performed_observations, require_ready=True, check_timing=False,
    )
    if performance_error is not None:
        assert isinstance(live_performance, Mapping)
        raise LivePerformanceRejected(performance_error, live_performance)

    agy_offset_ms = sorted(residuals)[len(residuals) // 2]
    timeline, basis, cues_sha, sung_rows = _asr_anchored_timeline(
        run=run,
        agy_alignment=agy_alignment,
        performed_lines=performed_lines,
        asr_anchor_cues=asr_anchor_cues,
        effective_duration_ms=effective_duration_ms,
    )
    if not 0 <= timeline.offset_ms < effective_duration_ms:
        raise ValueError(f"nominal LRC zero {timeline.offset_ms}ms is outside the current source")
    rows: list[dict[str, object]] = []
    for placed in timeline.lines:
        agy_row = sung_rows[placed.position]
        index = int(agy_row["canonical_lrc_index"])
        anchor = placed.anchor
        rows.append(
            {
                "lrc_time_ms": placed.line.time_ms,
                "lrc_text": placed.line.text,
                "canonical_lrc_index": index,
                "matched_cue_id": anchor.cue.cue_id if anchor is not None else None,
                "cue_start_ms": anchor.cue.source_start_ms if anchor is not None else placed.live_start_ms,
                "cue_end_ms": anchor.cue.source_end_ms if anchor is not None else placed.live_end_ms,
                "match_ratio": anchor.ratio if anchor is not None else 0.0,
                "evidence_source": "agy_audio_lrc",
                "timing_source": "asr_anchor" if anchor is not None else "section_shift",
                "live_time_ms": placed.live_start_ms,
                "live_end_ms": placed.live_end_ms,
                "section_index": placed.section_index,
                "section_shift_ms": placed.shift_ms,
                "agy_live_start_ms": agy_row["cue_start_ms"],
                "agy_live_end_ms": agy_row["cue_end_ms"],
                **{key: agy_row[key] for key in LYRIC_VOCAL_ASSERTION_KEYS},
            }
        )
    timed_observations = performance_rows_on_asr_clock(rows, observations)
    performance_error = validate_live_performance_observation(
        live_performance, first_lyric_start_ms=0, last_lyric_end_ms=effective_duration_ms,
        observations=timed_observations, require_ready=True, check_timing=False,
        check_lyric_timing=True,
    )
    if performance_error is not None:
        raise LivePerformanceRejected(performance_error, live_performance)
    arrangement_completeness = derive_live_arrangement_completeness(
        observations=observations, live_arrangement=payload.get("live_arrangement"),
        post_song_talk_start_ms=payload.get("post_song_talk_start_ms"),
        source_duration_ms=effective_duration_ms, spot_checks=payload.get("spot_checks"),
        live_performance=live_performance, timing_alignment=rows,
        asr_anchor_cues=asr_anchor_cues,
    )
    observation_by_index = {
        row["canonical_lrc_index"]: observation
        for row, observation in zip(agy_alignment, performed_observations)
    }
    return AudioObservationAlignment(
        payload=payload,
        alignment=rows,
        performed_lines=[placed.line for placed in timeline.lines],
        performed_observations=[observation_by_index[row["canonical_lrc_index"]] for row in rows],
        arrangement_completeness=arrangement_completeness,
        offset_ms=timeline.offset_ms,
        agy_alignment=agy_alignment,
        timeline=timeline,
        timing_basis=basis,
        agy_offset_ms=agy_offset_ms,
        asr_cues_sha256=cues_sha,
    )


def _asr_anchored_timeline(
    *,
    run: AudioLrcAlignmentRun,
    agy_alignment: Sequence[Mapping[str, object]],
    performed_lines: Sequence[LrcLine],
    asr_anchor_cues: Sequence[SourceCue],
    effective_duration_ms: int,
) -> tuple[AsrAnchoredTimeline, str, str | None, list[Mapping[str, object]]]:
    """Place affirmatively heard lyrics on the independent BCUT clock."""

    sung = [
        (line, row)
        for line, row in zip(performed_lines, agy_alignment)
        if not is_lrc_non_lyric_metadata(line.text)
    ]
    sung_lines = [line for line, _row in sung]
    sung_rows = [row for _line, row in sung]
    if not asr_anchor_cues:
        raise AsrTimelinePending("alignment pending: source BCUT ASR cues are required")
    timeline = build_asr_anchored_timeline(
        sung_lines, asr_anchor_cues, source_duration_ms=effective_duration_ms
    )
    return timeline, "asr_anchor", asr_cues_sha256(asr_anchor_cues), sung_rows


def _bound_observation_audio(run: AudioLrcAlignmentRun) -> tuple[Path, str] | None:
    """The hash-bound complete audio the observation consumed (source clock)."""
    if getattr(run, "provider", None) == GEMINI_API_AUDIO_LRC_PROVIDER:
        path, sha = getattr(run, "api_audio_path", None), getattr(run, "api_audio_sha256", None)
    else:
        try:
            manifest = json.loads(Path(run.manifest_path).read_text(encoding="utf-8"))
            evidence = manifest.get("observation_input") or {}
            path, sha = evidence.get("audio_path"), evidence.get("audio_sha256")
        except (AttributeError, OSError, TypeError, ValueError):
            return None
    return (Path(path), str(sha)) if isinstance(path, str) and isinstance(sha, str) else None


def _finalize_audio_lrc_selection(
    *,
    run: AudioLrcAlignmentRun,
    lrc: LrcResult,
    asr_anchor_cues: Sequence[SourceCue],
    effective_duration_ms: int,
    min_matched_ratio: float,
    observation: AudioObservationAlignment,
) -> SelectedSong:
    payload = observation.payload
    alignment = observation.alignment
    performed_lines = observation.performed_lines
    arrangement_completeness = observation.arrangement_completeness
    timeline = observation.timeline
    offset_ms = observation.offset_ms
    asr_based = observation.timing_basis == "asr_anchor"
    offset_provenance: dict[str, object] = {
        "offset_basis": observation.timing_basis,
        "agy_offset_ms": observation.agy_offset_ms,
        "asr_offset_ms": offset_ms if asr_based else None,
        "asr_matched_line_count": timeline.anchor_count if asr_based else 0,
        "timing_model": TIMING_MODEL,
        "anchor_line_count": timeline.anchor_count,
        "dropped_anchor_count": timeline.dropped_anchor_count,
        "asr_cues_sha256": observation.asr_cues_sha256,
        "asr_cue_count": len(asr_anchor_cues),
        "asr_source_sha256": run.source_sha256,
        "asr_cues": [serialize_asr_cue(c) for c in asr_anchor_cues],
        "sections": [
            {
                "index": section.index,
                "shift_ms": section.shift_ms,
                "anchor_count": len(section.anchors),
                "max_residual_ms": section.max_residual_ms,
            }
            for section in timeline.sections
        ],
    }

    # Bound the actual performed sequence using the independent source clock.
    first_lyric_start_ms = min(
        min(int(row["cue_start_ms"]), int(row["live_time_ms"])) for row in alignment
    )
    last_lyric_end_ms = max(max(int(row["cue_end_ms"]), int(row["live_end_ms"])) for row in alignment)
    clip_end_ms = int(arrangement_completeness["post_song_transition_ms"])
    # The only *independent* check this pair ever gets.  It lives here, not in
    # derive_live_arrangement_completeness, because that function also re-derives
    # stored inventory in song_completion/live_source_review where no fresh-ASR
    # transcript exists; adding the dependency there would retroactively break
    # every published proof.  Here the fresh transcript is already in scope.
    witness_post_song_talk_start(
        post_song_talk_start_ms=payload.get("post_song_talk_start_ms"),
        asr_cues=asr_anchor_cues,
        require_witness=(
            arrangement_completeness["post_song_transition_kind"] != "HOST_TALK"
        ),
    )

    spot_checks = payload.get("spot_checks")
    required_spots = {"first_line", "chorus", "repeated_section", "longest_instrumental_gap", "tail"}
    if not isinstance(spot_checks, list) or len(spot_checks) != 5:
        raise ValueError("audio observation must include exactly five spot checks")
    spots: dict[str, Mapping[str, object]] = {}
    for spot in spot_checks:
        if not isinstance(spot, Mapping) or set(spot) != {"name", "live_time_ms", "result", "notes"}:
            raise ValueError("audio spot-check schema is invalid")
        name = spot.get("name")
        time_ms = spot.get("live_time_ms")
        if name not in required_spots or name in spots or spot.get("result") != "OK":
            raise ValueError("audio spot-check names/results are invalid")
        if not _is_int(time_ms):
            raise ValueError(f"audio spot-check {name} time is outside the performed song")
        if not 0 <= time_ms <= effective_duration_ms:
            raise ValueError(f"audio spot-check {name} time is outside the source")
        spots[str(name)] = spot
    if set(spots) != required_spots:
        raise ValueError("audio spot-check set is incomplete")
    # One start rule (song_clip_bounds): the music start, intro speech and the
    # recognition tolerance; ``offset_ms`` keeps its meaning (LRC zero on the
    # live clock) and need not precede a coarse ASR cue boundary.
    clip_start_ms, clip_start_evidence = song_clip_start(
        lrc_zero_ms=offset_ms, first_lyric_start_ms=first_lyric_start_ms, cues=asr_anchor_cues,
        lyrics=lyric_texts(payload["observations"]),
        audio_onset=audio_onset_evidence(_bound_observation_audio(run),
                                         estimate_ms=min(offset_ms, first_lyric_start_ms)),
    )
    offset_provenance["clip_start_evidence"] = clip_start_evidence
    if not 0 <= clip_start_ms <= first_lyric_start_ms <= last_lyric_end_ms <= clip_end_ms <= effective_duration_ms:
        raise ValueError("derived full-song boundary ordering is invalid")
    matched = [dict(entry) for entry in alignment if entry["matched_cue_id"] is not None]
    matched_ratio = len(matched) / len(alignment)
    if matched_ratio < min_matched_ratio:
        raise ValueError(f"audio alignment ratio {matched_ratio:.0%} is below {min_matched_ratio:.0%}")
    performed_lrc = LrcResult(
        provider=lrc.provider,
        song_title=lrc.song_title,
        artist=lrc.artist,
        source_ref=lrc.source_ref,
        lines=tuple(performed_lines),
    )
    return (
        matched_ratio,
        performed_lrc,
        alignment,
        matched,
        first_lyric_start_ms,
        last_lyric_end_ms,
        clip_start_ms,
        clip_end_ms,
        offset_ms,
        arrangement_completeness,
        offset_provenance,
    )


def _validated_audio_lrc_selection(
    *,
    run: AudioLrcAlignmentRun,
    lrc: LrcResult,
    candidate_id: str,
    source_media_path: Path,
    source_duration_ms: int,
    min_matched_ratio: float,
    asr_anchor_cues: Sequence[SourceCue] = (),
) -> SelectedSong:
    """Validate bound audio evidence and convert it into standard selection."""
    # A current metadata filter may remove credits from an older reference.
    # Validate that receipt against its unchanged canonical input; only the
    # performed timeline drops metadata, retaining original canonical indices.
    bound_lrc = _require_bound_artifact(run.lrc_path, run.lrc_sha256, "canonical LRC")
    original_lines = tuple(parse_lrc_text(bound_lrc.read_text(encoding="utf-8"), exclude_metadata=False))
    if original_lines != lrc.lines and tuple(
        line for line in original_lines if not is_lrc_non_lyric_metadata(line.text)
    ) == lrc.lines:
        lrc = LrcResult(lrc.provider, lrc.song_title, lrc.artist, lrc.source_ref, original_lines)
    effective_duration_ms = _validate_audio_lrc_artifact_bindings(
        run=run,
        lrc=lrc,
        candidate_id=candidate_id,
        source_media_path=source_media_path,
        source_duration_ms=source_duration_ms,
    )
    observation = _build_audio_observation_alignment(
        run=run,
        lrc=lrc,
        candidate_id=candidate_id,
        effective_duration_ms=effective_duration_ms,
        asr_anchor_cues=asr_anchor_cues,
    )
    return _finalize_audio_lrc_selection(
        run=run,
        lrc=lrc,
        asr_anchor_cues=asr_anchor_cues,
        effective_duration_ms=effective_duration_ms,
        min_matched_ratio=min_matched_ratio,
        observation=observation,
    )


def _lyric_row_interval(row: Mapping[str, object], index: int) -> tuple[int, int]:
    """Read one strict raw-AGY or projected-report lyric interval."""

    if row.get("timing_source") in {"asr_anchor", "section_shift"}:
        start, end = row.get("live_time_ms"), row.get("live_end_ms")
        if not (_is_int(start) and _is_int(end) and 0 <= start < end):
            raise ValueError(f"live performance lyric row {index} ASR interval is invalid")
        return start, end
    has_live = "live_start_ms" in row or "live_end_ms" in row
    has_cue = "cue_start_ms" in row or "cue_end_ms" in row
    if has_live and not {"live_start_ms", "live_end_ms"}.issubset(row):
        raise ValueError(f"live performance lyric row {index} has a partial raw interval")
    if has_cue and not {"cue_start_ms", "cue_end_ms"}.issubset(row):
        raise ValueError(f"live performance lyric row {index} has a partial report interval")
    if not has_live and not has_cue:
        raise ValueError(f"live performance lyric row {index} timing is missing")
    live_pair = (row.get("live_start_ms"), row.get("live_end_ms")) if has_live else None
    cue_pair = (row.get("cue_start_ms"), row.get("cue_end_ms")) if has_cue else None
    if live_pair is not None and cue_pair is not None and live_pair != cue_pair:
        raise ValueError(f"live performance lyric row {index} raw/report intervals conflict")
    selected_pair = live_pair if live_pair is not None else cue_pair
    assert selected_pair is not None
    start_ms, end_ms = selected_pair
    if not (_is_int(start_ms) and _is_int(end_ms) and 0 <= start_ms < end_ms):
        raise ValueError(f"live performance lyric row {index} timing is invalid")
    return int(start_ms), int(end_ms)


def _validate_lyric_vocal_observations(
    observations: object,
    *,
    require_ready: bool,
    check_timing: bool = True,
) -> tuple[bool, bool, bool]:
    """Recompute the singer/role aggregates from every canonical lyric row.

    The AGY top-level summary is never trusted as a substitute for the rows.
    A READY result requires each line to say that the same live lyric source is
    the selected host, with no guest/duet/harmony or recorded vocal audible.  A
    narrowly labelled canonical spoken passage is allowed only inside an
    otherwise predominantly sung performance; ordinary speech over music is
    not.  CAM++ remains an independent speaker-similarity subclaim and is not
    treated here (or elsewhere) as a singing classifier.
    """

    if (
        not isinstance(observations, Sequence)
        or isinstance(observations, (str, bytes, bytearray))
        or not observations
    ):
        raise ValueError("live performance lyric-source observations are missing")
    all_same_lidousha = True
    any_other_singer = False
    any_recorded_vocal = False
    singing_rows = 0
    consecutive_spoken_rows = 0
    longest_spoken_run = 0
    spoken_blocks = 0
    spoken_duration_ms = 0
    total_lyric_vocal_duration_ms = 0
    spoken_block_start_ms: int | None = None
    longest_spoken_block_span_ms = 0
    previous_start_ms: int | None = None
    previous_end_ms: int | None = None
    roles: list[object] = []
    for index, row in enumerate(observations):
        if not isinstance(row, Mapping) or not LYRIC_VOCAL_ASSERTION_KEYS.issubset(row):
            raise ValueError(f"live performance lyric row {index} singer schema is invalid")
        subject = row.get("lyric_vocal_subject")
        role = row.get("lidousha_role")
        same_lidousha = row.get("same_live_vocal_source_as_lidousha")
        other_singer = row.get("other_singer_or_harmony_audible")
        recorded_vocal = row.get("recorded_or_playback_vocal_audible")
        if subject not in LYRIC_VOCAL_SUBJECTS or role not in HOST_LYRIC_ROLES:
            raise ValueError(f"live performance lyric row {index} singer enum is invalid")
        if not all(isinstance(value, bool) for value in (same_lidousha, other_singer, recorded_vocal)):
            raise ValueError(f"live performance lyric row {index} singer assertions are invalid")

        live_lidousha_lyric = (
            subject == HOST_LYRIC_SUBJECT
            and role in {"SINGING_THIS_LYRIC", "PERFORMING_THIS_LYRIC_SPOKEN"}
            and other_singer is False
            and recorded_vocal is False
        )
        if same_lidousha is not live_lidousha_lyric:
            raise ValueError(f"live performance lyric row {index} same-subject assertion is inconsistent")
        if subject == HOST_LYRIC_SUBJECT and role not in {
            "SINGING_THIS_LYRIC",
            "PERFORMING_THIS_LYRIC_SPOKEN",
        }:
            raise ValueError(
                f"live performance lyric row {index} host role contradicts its subject"
            )
        if (
            role in {"SINGING_THIS_LYRIC", "PERFORMING_THIS_LYRIC_SPOKEN"}
            and subject != HOST_LYRIC_SUBJECT
        ):
            raise ValueError(f"live performance lyric row {index} performance role contradicts its subject")
        if subject == "OTHER_OR_MIXED_SINGER" and other_singer is not True:
            raise ValueError(f"live performance lyric row {index} other-singer assertion is inconsistent")
        if subject == "RECORDED_OR_PLAYBACK_SINGER" and recorded_vocal is not True:
            raise ValueError(f"live performance lyric row {index} recorded-vocal assertion is inconsistent")
        if subject == "NO_AUDIBLE_LYRIC_VOCAL" and (other_singer or recorded_vocal):
            raise ValueError(f"live performance lyric row {index} no-vocal assertion is inconsistent")
        if require_ready and not live_lidousha_lyric:
            raise ValueError(
                f"live performance lyric row {index} does not affirm the same live host lyric source"
            )
        roles.append(role)
        row_interval: tuple[int, int] | None = None
        if require_ready and check_timing:
            start_ms, end_ms = _lyric_row_interval(row, index)
            row_interval = (start_ms, end_ms)
            if previous_start_ms is not None and start_ms <= previous_start_ms:
                raise ValueError("live performance lyric starts are not strictly monotonic")
            if previous_end_ms is not None and previous_end_ms - start_ms > 250:
                raise ValueError("live performance adjacent lyric rows overlap by more than 250ms")
            total_lyric_vocal_duration_ms += end_ms - start_ms
            previous_start_ms = start_ms
            previous_end_ms = end_ms
        if role == "SINGING_THIS_LYRIC":
            singing_rows += 1
            consecutive_spoken_rows = 0
            spoken_block_start_ms = None
        elif role == "PERFORMING_THIS_LYRIC_SPOKEN":
            if require_ready and check_timing and consecutive_spoken_rows == 0:
                spoken_blocks += 1
                assert row_interval is not None
                spoken_block_start_ms = row_interval[0]
            consecutive_spoken_rows += 1
            longest_spoken_run = max(longest_spoken_run, consecutive_spoken_rows)
            if require_ready and check_timing:
                assert row_interval is not None
                spoken_duration_ms += row_interval[1] - row_interval[0]
                assert spoken_block_start_ms is not None
                longest_spoken_block_span_ms = max(
                    longest_spoken_block_span_ms,
                    row_interval[1] - spoken_block_start_ms,
                )
        else:
            consecutive_spoken_rows = 0
            spoken_block_start_ms = None
        all_same_lidousha = all_same_lidousha and bool(same_lidousha)
        any_other_singer = any_other_singer or bool(other_singer)
        any_recorded_vocal = any_recorded_vocal or bool(recorded_vocal)
    if require_ready:
        if roles[0] != "SINGING_THIS_LYRIC" or roles[-1] != "SINGING_THIS_LYRIC":
            raise ValueError("live performance first and final canonical lyric rows must be sung")
        if singing_rows < MIN_READY_SUNG_LYRIC_ROWS or singing_rows / len(roles) < MIN_READY_SUNG_LYRIC_RATIO:
            raise ValueError(
                f"live performance is not predominantly sung by {CHANNEL_PROFILE.prompt_name}: "
                f"{singing_rows}/{len(roles)} canonical lyric rows are sung"
            )
        if longest_spoken_run > MAX_READY_CONSECUTIVE_SPOKEN_LYRIC_ROWS:
            raise ValueError(
                "live performance canonical spoken passage is too long: "
                f"{longest_spoken_run} consecutive rows"
            )
        if spoken_blocks > MAX_READY_SPOKEN_BLOCKS:
            raise ValueError(
                "live performance has multiple canonical spoken passages: "
                f"{spoken_blocks} blocks"
            )
        if (
            spoken_duration_ms > MAX_READY_SPOKEN_LYRIC_DURATION_MS
            # Keep the shared rational limit exact at the acceptance boundary.
            or spoken_duration_ms * MAX_READY_SPOKEN_LYRIC_DURATION_RATIO.denominator
            > total_lyric_vocal_duration_ms * MAX_READY_SPOKEN_LYRIC_DURATION_RATIO.numerator
        ):
            raise ValueError(
                "live performance canonical spoken passage is too long by voiced duration: "
                f"{spoken_duration_ms}/{total_lyric_vocal_duration_ms}ms"
            )
        if longest_spoken_block_span_ms > MAX_READY_SPOKEN_BLOCK_SPAN_MS:
            raise ValueError(
                "live performance canonical spoken block span is too long: "
                f"{longest_spoken_block_span_ms}ms"
            )
    return all_same_lidousha, any_other_singer, any_recorded_vocal


def validate_live_performance_observation(
    performance: object,
    *,
    first_lyric_start_ms: int,
    last_lyric_end_ms: int,
    observations: object,
    require_ready: bool,
    check_timing: bool = True,
    check_lyric_timing: bool | None = None,
) -> str | None:
    """Validate AGY's anti-background and same-subject singing observation.

    AGY must assert the active lyric vocalist and the selected host's role on every
    canonical line.  Final delivery additionally combines this with the
    independently generated host voiceprint claim on the same lyric rows.
    """

    try:
        if not isinstance(performance, Mapping) or set(performance) != {
            "mode",
            "confidence",
            "continuous_live_song_performance",
            "background_recording_likelihood",
            "same_lidousha_live_performer_across_all_lyrics",
            "other_singer_or_harmony_present",
            "recorded_or_playback_vocal_present",
            "evidence",
            "notes",
        }:
            raise ValueError("live performance observation schema is invalid")
        mode = performance.get("mode")
        if mode not in LIVE_PERFORMANCE_MODES:
            raise ValueError("live performance observation mode is invalid")
        confidence = performance.get("confidence")
        background = performance.get("background_recording_likelihood")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= float(confidence) <= 1.0
            or isinstance(background, bool)
            or not isinstance(background, (int, float))
            or not 0.0 <= float(background) <= 1.0
            or not isinstance(performance.get("continuous_live_song_performance"), bool)
            or not isinstance(performance.get("same_lidousha_live_performer_across_all_lyrics"), bool)
            or not isinstance(performance.get("other_singer_or_harmony_present"), bool)
            or not isinstance(performance.get("recorded_or_playback_vocal_present"), bool)
            or not isinstance(performance.get("notes"), str)
            or not str(performance.get("notes")).strip()
        ):
            raise ValueError("live performance observation values are invalid")
        all_same_lidousha, any_other_singer, any_recorded_vocal = _validate_lyric_vocal_observations(
            observations,
            require_ready=require_ready,
            check_timing=check_timing if check_lyric_timing is None else check_lyric_timing,
        )
        if (
            performance.get("same_lidousha_live_performer_across_all_lyrics") is not all_same_lidousha
            or performance.get("other_singer_or_harmony_present") is not any_other_singer
            or performance.get("recorded_or_playback_vocal_present") is not any_recorded_vocal
        ):
            raise ValueError("live performance top-level performer assertions do not match lyric rows")
        evidence = performance.get("evidence")
        if not isinstance(evidence, list) or len(evidence) != 3:
            raise ValueError("live performance observation needs exactly three evidence timestamps")
        if not 0 <= first_lyric_start_ms < last_lyric_end_ms:
            raise ValueError("live performance lyric span is invalid")
        span = last_lyric_end_ms - first_lyric_start_ms
        sung_intervals: list[tuple[int, int]] = []
        if require_ready and check_timing:
            sung_intervals = [
                _lyric_row_interval(row, index)
                for index, row in enumerate(observations)
                if isinstance(row, Mapping) and row.get("lidousha_role") == "SINGING_THIS_LYRIC"
            ]
        buckets: set[int] = set()
        previous_time = -1
        for index, row in enumerate(evidence):
            if not isinstance(row, Mapping) or set(row) != {"time_ms", "observation"}:
                raise ValueError(f"live performance evidence[{index}] schema is invalid")
            time_ms = row.get("time_ms")
            observation = row.get("observation")
            if (
                not _is_int(time_ms)
                or not first_lyric_start_ms <= time_ms <= last_lyric_end_ms
                or int(time_ms) <= previous_time
                or not isinstance(observation, str)
                or not observation.strip()
            ):
                raise ValueError(f"live performance evidence[{index}] is invalid")
            if require_ready and check_timing and not any(
                start_ms <= int(time_ms) < end_ms
                for start_ms, end_ms in sung_intervals
            ):
                raise ValueError(
                    f"live performance evidence[{index}] does not bind a sung canonical lyric row"
                )
            previous_time = int(time_ms)
            buckets.add(min(2, ((int(time_ms) - first_lyric_start_ms) * 3) // max(1, span)))
        if check_timing and buckets != {0, 1, 2}:
            raise ValueError("live performance evidence must cover lyric head, middle, and tail")
        if require_ready and (
            mode != LIVE_PERFORMANCE_READY_MODE
            or performance.get("continuous_live_song_performance") is not True
            or performance.get("same_lidousha_live_performer_across_all_lyrics") is not True
            or performance.get("other_singer_or_harmony_present") is not False
            or performance.get("recorded_or_playback_vocal_present") is not False
            or float(confidence) < 0.85
            or float(background) > 0.20
        ):
            raise ValueError(f"live performance not proven: mode={mode}")
    except ValueError as exc:
        return str(exc)
    return None
