from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from src.autoslice import host_vocal_proof as hv
from src.autoslice.song_repair import (
    AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION,
    AudioLrcAlignmentRun,
    LrcResult,
)


READY_LYRIC_VOCAL_ASSERTIONS = {
    "lyric_vocal_subject": "LIDOUSHA",
    "lidousha_role": "SINGING_THIS_LYRIC",
    "same_live_vocal_source_as_lidousha": True,
    "other_singer_or_harmony_audible": False,
    "recorded_or_playback_vocal_audible": False,
}

READY_LIVE_PERFORMANCE_ASSERTIONS = {
    "same_lidousha_live_performer_across_all_lyrics": True,
    "other_singer_or_harmony_present": False,
    "recorded_or_playback_vocal_present": False,
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _ensure_alignment_rows(alignment: dict[str, object]) -> None:
    first_ms = int(alignment["first_lyric_start_ms"])
    last_ms = int(alignment["last_lyric_end_ms"])
    raw_rows = alignment.get("alignment")
    lyric_lines = alignment.get("lyric_lines")
    if not isinstance(raw_rows, list):
        return
    count = len(raw_rows)
    for index, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            continue
        start_ms = row.get("cue_start_ms")
        if not isinstance(start_ms, int) or isinstance(start_ms, bool):
            start_ms = first_ms + round((last_ms - first_ms - 3_000) * index / max(1, count - 1))
            row["cue_start_ms"] = start_ms
        if not isinstance(row.get("cue_end_ms"), int) or isinstance(row.get("cue_end_ms"), bool):
            row["cue_end_ms"] = min(last_ms, start_ms + 3_000)
        if not isinstance(row.get("lrc_text"), str):
            lyric = lyric_lines[index] if isinstance(lyric_lines, list) and index < len(lyric_lines) else None
            row["lrc_text"] = str(lyric.get("text") if isinstance(lyric, dict) else f"lyric-{index}")


def bind_ready_live_performance_report(
    report_path: Path, *, source_media: Path, candidate_id: str
) -> None:
    """Upgrade a synthetic alignment report to the bound AGY-v4 contract."""

    report = json.loads(report_path.read_text(encoding="utf-8"))
    _ensure_alignment_rows(report)
    first_ms = int(report["first_lyric_start_ms"])
    last_ms = int(report["last_lyric_end_ms"])
    span = last_ms - first_ms
    live_performance = {
        "mode": "LIVE_STREAMER_SINGING",
        "confidence": 0.96,
        "continuous_live_song_performance": True,
        "background_recording_likelihood": 0.03,
        **READY_LIVE_PERFORMANCE_ASSERTIONS,
        "evidence": [
            {"time_ms": report["alignment"][1]["cue_start_ms"], "observation": "live vocal head"},
            {
                "time_ms": report["alignment"][len(report["alignment"]) // 2]["cue_start_ms"],
                "observation": "live vocal middle",
            },
            {"time_ms": report["alignment"][-2]["cue_start_ms"], "observation": "live vocal tail"},
        ],
        "notes": "synthetic bound live-performance fixture",
    }
    artifact_dir = report_path.parent / "agy-audio-fixture"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    bound_source = artifact_dir / "input.mp4"
    shutil.copy2(source_media, bound_source)
    lrc_path = artifact_dir / "source.lrc"
    lyric_lines = report["lyric_lines"]
    lrc_path.write_text("\n".join(str(row["text"]) for row in lyric_lines) + "\n", encoding="utf-8")
    prompt_path = artifact_dir / "prompt.md"
    prompt_path.write_text("strict AGY v4 test prompt\n", encoding="utf-8")
    source_sha = _sha(bound_source)
    lrc_sha = _sha(lrc_path)
    raw_rows = []
    for index, (lyric, proof_row) in enumerate(zip(lyric_lines, report["alignment"], strict=True)):
        raw_rows.append(
            {
                "lrc_index": index,
                "lrc_time_ms": lyric["lrc_time_ms"],
                "text": lyric["text"],
                "heard": True,
                "live_start_ms": proof_row["cue_start_ms"],
                "live_end_ms": proof_row["cue_end_ms"],
                "confidence": 0.95,
                **READY_LYRIC_VOCAL_ASSERTIONS,
            }
        )
    spots = [
        {"name": "first_line", "live_time_ms": first_ms, "result": "OK", "notes": "fixture"},
        {"name": "chorus", "live_time_ms": first_ms + span // 3, "result": "OK", "notes": "fixture"},
        {"name": "repeated_section", "live_time_ms": first_ms + span // 2, "result": "OK", "notes": "fixture"},
        {"name": "longest_instrumental_gap", "live_time_ms": first_ms + span * 2 // 3, "result": "OK", "notes": "fixture"},
        {"name": "tail", "live_time_ms": report["alignment"][-1]["cue_start_ms"], "result": "OK", "notes": "fixture"},
    ]
    raw_output = artifact_dir / "alignment.json"
    raw_payload = {
        "schema_version": AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION,
        "record": {
            "attempt_id": "fixture-attempt",
            "candidate_id": candidate_id,
            "source_sha256": source_sha,
            "lrc_sha256": lrc_sha,
            "source_duration_ms": last_ms + 10_000,
        },
        "observations": raw_rows,
        "spot_checks": spots,
        "live_performance": live_performance,
        "post_song_talk_start_ms": last_ms + 1_000,
    }
    _write_json(raw_output, raw_payload)
    raw_sha = _sha(raw_output)
    for index, row in enumerate(report["alignment"]):
        row["matched_cue_id"] = f"agy-audio:{raw_sha[:12]}:line-{index}"
        row["evidence_source"] = "agy_audio_lrc"
        row["match_ratio"] = 0.95
        row.update(READY_LYRIC_VOCAL_ASSERTIONS)
    manifest = artifact_dir / "run.manifest.json"
    artifacts = {
        "source_origin_path": str(source_media.resolve()),
        "source_path": str(bound_source),
        "source_sha256": source_sha,
        "source_duration_ms": last_ms + 10_000,
        "lrc_path": str(lrc_path),
        "lrc_sha256": lrc_sha,
        "prompt_path": str(prompt_path),
        "prompt_sha256": _sha(prompt_path),
        "output_path": str(raw_output),
        "output_sha256": raw_sha,
    }
    _write_json(
        manifest,
        {
            "schema_version": "agy-audio-lrc-run.v1",
            "candidate_id": candidate_id,
            "provider": "agy",
            "model": "Gemini 3.5 Flash (High)",
            "agy_rc": 0,
            "provider_fallback_used": False,
            "sandbox": True,
            "artifacts": artifacts,
        },
    )
    report.update(
        {
            "evidence_source": "agy_audio_lrc",
            "audio_alignment_provider": "agy",
            "audio_alignment_model": "Gemini 3.5 Flash (High)",
            "spot_checks": spots,
            "live_performance": live_performance,
            "post_song_talk_start_ms": last_ms + 1_000,
            "source_media_path": str(source_media.resolve()),
            "source_media_sha256": source_sha,
            "audio_alignment_artifacts": {
                **artifacts,
                "raw_output_path": artifacts["output_path"],
                "raw_output_sha256": artifacts["output_sha256"],
                "run_manifest_path": str(manifest),
                "run_manifest_sha256": _sha(manifest),
            },
        }
    )
    # Report uses raw_* names, not output_* aliases.
    report["audio_alignment_artifacts"].pop("output_path")
    report["audio_alignment_artifacts"].pop("output_sha256")
    _write_json(report_path, report)


def make_ready_audio_alignment_run(
    *,
    source_media: Path,
    source_duration_ms: int,
    lrc: LrcResult,
    candidate_id: str,
    output_dir: Path,
    offset_ms: int = 50_000,
) -> AudioLrcAlignmentRun:
    """Build a strict, hash-bound AGY-v2 fixture for song-repair tests."""

    output_dir.mkdir(parents=True, exist_ok=True)
    source_sha = _sha(source_media)
    lrc_path = output_dir / "source.lrc"
    lrc_path.write_text(
        "".join(
            f"[{line.time_ms // 60_000:02d}:{(line.time_ms % 60_000) // 1_000:02d}.{line.time_ms % 1_000:03d}]{line.text}\n"
            for line in lrc.lines
        ),
        encoding="utf-8",
    )
    lrc_sha = _sha(lrc_path)
    observations = []
    for index, line in enumerate(lrc.lines):
        start_ms = offset_ms + line.time_ms
        observations.append(
            {
                "lrc_index": index,
                "lrc_time_ms": line.time_ms,
                "text": line.text,
                "heard": True,
                "live_start_ms": start_ms,
                "live_end_ms": start_ms + 3_000,
                "confidence": 0.96,
                **READY_LYRIC_VOCAL_ASSERTIONS,
            }
        )
    first_ms = int(observations[0]["live_start_ms"])
    last_ms = int(observations[-1]["live_end_ms"])
    if last_ms + 6_000 > source_duration_ms:
        raise ValueError("strict audio-alignment fixture leaves no post-song host anchor")
    live_performance = {
        "mode": "LIVE_STREAMER_SINGING",
        "confidence": 0.96,
        "continuous_live_song_performance": True,
        "background_recording_likelihood": 0.03,
        **READY_LIVE_PERFORMANCE_ASSERTIONS,
        "evidence": [
            {"time_ms": observations[1]["live_start_ms"], "observation": "live vocal head"},
            {
                "time_ms": observations[len(observations) // 2]["live_start_ms"],
                "observation": "live vocal middle",
            },
            {"time_ms": observations[-2]["live_start_ms"], "observation": "live vocal tail"},
        ],
        "notes": "strict synthetic live-performance fixture",
    }
    spot_indexes = (0, min(2, len(observations) - 1), min(3, len(observations) - 1), min(4, len(observations) - 1), len(observations) - 1)
    spot_names = ("first_line", "chorus", "repeated_section", "longest_instrumental_gap", "tail")
    spots = [
        {
            "name": name,
            "live_time_ms": observations[index]["live_start_ms"],
            "result": "OK",
            "notes": "fixture",
        }
        for name, index in zip(spot_names, spot_indexes, strict=True)
    ]
    payload = {
        "schema_version": AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION,
        "record": {
            "attempt_id": "fixture-attempt",
            "candidate_id": candidate_id,
            "source_sha256": source_sha,
            "lrc_sha256": lrc_sha,
            "source_duration_ms": source_duration_ms,
        },
        "observations": observations,
        "spot_checks": spots,
        "live_performance": live_performance,
        "post_song_talk_start_ms": last_ms + 2_000,
    }
    prompt_path = output_dir / "prompt.md"
    prompt_path.write_text("strict AGY v4 test prompt\n", encoding="utf-8")
    raw_output = output_dir / "alignment.json"
    _write_json(raw_output, payload)
    raw_sha = _sha(raw_output)
    manifest = output_dir / "run.manifest.json"
    artifacts = {
        "source_origin_path": str(source_media.resolve()),
        "source_path": str(source_media),
        "source_sha256": source_sha,
        "source_duration_ms": source_duration_ms,
        "lrc_path": str(lrc_path),
        "lrc_sha256": lrc_sha,
        "prompt_path": str(prompt_path),
        "prompt_sha256": _sha(prompt_path),
        "output_path": str(raw_output),
        "output_sha256": raw_sha,
    }
    _write_json(
        manifest,
        {
            "schema_version": "agy-audio-lrc-run.v1",
            "candidate_id": candidate_id,
            "provider": "agy",
            "model": "Gemini 3.5 Flash (High)",
            "agy_rc": 0,
            "provider_fallback_used": False,
            "sandbox": True,
            "artifacts": artifacts,
        },
    )
    return AudioLrcAlignmentRun(
        payload=payload,
        provider="agy",
        model="Gemini 3.5 Flash (High)",
        rc=0,
        provider_fallback_used=False,
        source_origin_path=str(source_media.resolve()),
        source_path=str(source_media),
        source_sha256=source_sha,
        source_duration_ms=source_duration_ms,
        lrc_path=str(lrc_path),
        lrc_sha256=lrc_sha,
        prompt_path=str(prompt_path),
        prompt_sha256=_sha(prompt_path),
        output_path=str(raw_output),
        output_sha256=raw_sha,
        manifest_path=str(manifest),
        manifest_sha256=_sha(manifest),
    )


def make_ready_host_vocal_claim(
    output_dir: Path,
    *,
    source_media: Path,
    alignment_report: Path,
    candidate_id: str,
) -> tuple[dict[str, object], Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    alignment = json.loads(alignment_report.read_text(encoding="utf-8"))
    alignment["candidate_id"] = candidate_id
    _ensure_alignment_rows(alignment)
    _write_json(alignment_report, alignment)
    first_ms = int(alignment["first_lyric_start_ms"])
    last_ms = int(alignment["last_lyric_end_ms"])

    model_dir = output_dir / "model"
    model_dir.mkdir(exist_ok=True)
    (model_dir / "configuration.json").write_bytes(b"test-campp-config")
    (model_dir / "model.bin").write_bytes(b"test-campp-model")
    reference_dir = output_dir / "references"
    reference_dir.mkdir(exist_ok=True)
    references = []
    for index in range(3):
        path = reference_dir / f"enroll_lds_{index + 1}.wav"
        path.write_bytes(f"test-reference-{index + 1}".encode())
        references.append(
            {"id": f"lidousha-{index + 1}", "filename": path.name, "sha256": _sha(path), "path": str(path.resolve())}
        )
    profile_path = output_dir / "profile.json"
    profile = {
        "schema_version": hv.PROFILE_SCHEMA_VERSION,
        "profile_id": "test-lidousha-profile",
        "subject": "李豆沙",
        "model": {
            "model_id": "test-campp",
            "tree_sha256": hv._sha256_directory(model_dir),
        },
        "references": [{key: row[key] for key in ("id", "filename", "sha256")} for row in references],
        "policy": hv._canonical_policy(),
    }
    _write_json(profile_path, profile)

    proof_path = output_dir / f"{candidate_id}.host-vocal-proof.json"
    checkpoint_dir = output_dir / f"{candidate_id}.host-vocal-proof.checkpoints"
    checkpoint_dir.mkdir(exist_ok=True)
    session_sample = checkpoint_dir / "session-host-anchor.wav"
    session_sample.write_bytes(b"test-session-host-anchor")
    session_start, session_end = hv._session_host_anchor_position(alignment)
    session_host_anchor = {
        "start_ms": session_start,
        "end_ms": session_end,
        "window_ms": session_end - session_start,
        "sample_path": str(session_sample.resolve()),
        "sample_sha256": _sha(session_sample),
        "reference_scores": [{"reference_id": row["id"], "score": 0.80} for row in references],
        "enroll_median_score": 0.80,
        "passed": True,
    }
    checkpoints = []
    selected_lyrics = hv._selected_lyric_rows(alignment)
    for index, (fraction, bucket, lyric_row) in enumerate(
        zip(hv.CHECKPOINT_FRACTIONS, hv.CHECKPOINT_BUCKETS, selected_lyrics, strict=True)
    ):
        sample = checkpoint_dir / f"checkpoint-{index + 1:02d}.wav"
        sample.write_bytes(f"test-checkpoint-{index + 1}".encode())
        center_ms, start_ms, end_ms = hv._checkpoint_position_for_row(lyric_row)
        checkpoints.append(
            {
                "index": index,
                "fraction": fraction,
                "bucket": bucket,
                "alignment_index": lyric_row["alignment_index"],
                "matched_cue_id": lyric_row["matched_cue_id"],
                "lrc_time_ms": lyric_row["lrc_time_ms"],
                "lrc_text": lyric_row["lrc_text"],
                "lyric_cue_start_ms": lyric_row["cue_start_ms"],
                "lyric_cue_end_ms": lyric_row["cue_end_ms"],
                **{
                    key: lyric_row[key]
                    for key in hv.READY_SINGING_ASSERTIONS
                },
                "center_ms": center_ms,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "window_ms": end_ms - start_ms,
                "sample_path": str(sample.resolve()),
                "sample_sha256": _sha(sample),
                "scores": [{"reference_id": row["id"], "score": 0.40} for row in references],
                "median_score": 0.40,
                "session_anchor_score": 0.40,
                "passed": True,
            }
        )
    status, decision, distribution = hv._decision_from_checkpoints(checkpoints)
    proof = {
        "schema_version": hv.PROOF_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "status": status,
        "decision": decision,
        "source_media": {"path": str(source_media.resolve()), "sha256": _sha(source_media)},
        "lyrics_alignment_report": {
            "path": str(alignment_report.resolve()),
            "sha256": _sha(alignment_report),
            "first_lyric_start_ms": first_ms,
            "last_lyric_end_ms": last_ms,
        },
        "reference_profile": {
            "path": str(profile_path.resolve()),
            "sha256": _sha(profile_path),
            "schema_version": hv.PROFILE_SCHEMA_VERSION,
            "profile_id": profile["profile_id"],
        },
        "speaker_model": {
            "path": str(model_dir.resolve()),
            "model_id": profile["model"]["model_id"],
            "tree_sha256": profile["model"]["tree_sha256"],
        },
        "references": references,
        "session_host_anchor": session_host_anchor,
        "policy": hv._canonical_policy(),
        "checkpoints": checkpoints,
        "distribution": distribution,
    }
    _write_json(proof_path, proof)
    claim = {
        "status": status,
        "decision": decision,
        "proof_path": str(proof_path.resolve()),
        "proof_sha256": "sha256:" + _sha(proof_path),
        "source_media_path": str(source_media.resolve()),
        "alignment_report_path": str(alignment_report.resolve()),
        "profile_path": str(profile_path.resolve()),
    }
    return claim, profile_path
