"""Consume a completed full-song recut through the ordinary review path."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from .auto_review import DecisionAction
from .producer_text_checkpoint import _frozen_bytes
from .song_completion import song_completion_evidence
from .source_context_executor import SourceContextExecutionResult

SCHEMA = "materialized-song-successor.v1"
PROOF_KEYS = (
    "song_boundary",
    "lyrics_alignment",
    "host_vocal_proof",
    "song_repair_gate",
    "song_context_subtitle_fallback",
    "final_song_lyrics_cpa",
)
MEDIA_HASH_KEYS = ("video_sha256", "subtitle_sha256", "ass_sha256", "burned_video_sha256")


def completion(record):
    claim = record["source_context_job"].get("host_vocal_proof") or {}
    path = claim.get("proof_path")
    if not isinstance(path, str):
        raise ValueError("SONG_MATERIALIZED_SUCCESSOR_HOST_BINDING_MISSING")
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != str(claim.get("proof_sha256") or "").removeprefix(
        "sha256:"
    ):
        raise ValueError("SONG_MATERIALIZED_SUCCESSOR_HOST_BINDING_DRIFT")
    proof = json.loads(raw)
    profile = claim.get("profile_path") or (proof.get("reference_profile") or {}).get("path")
    if not isinstance(profile, str):
        raise ValueError("SONG_MATERIALIZED_SUCCESSOR_HOST_BINDING_MISSING")
    return song_completion_evidence(record, host_vocal_profile=Path(profile))


def completed_boundary(job, recut, resolution):
    """Resolve the current physical window only after full output verification."""
    if (
        resolution is None
        or resolution.action not in {DecisionAction.AUTO_RECUT, DecisionAction.AUTO_UPLOAD}
        or resolution.reason_codes != ("SONG_FULL_BOUNDARY_READY",)
    ):
        return None
    record = {
        "candidate_id": resolution.candidate_id,
        "source_context_job": job,
        "materialized_recut": recut,
    }
    try:
        proof = completion(record)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    if not proof.get("ready"):
        return None
    if (
        recut.get("start_ms") != resolution.resolved_start_ms
        or recut.get("end_ms") != resolution.resolved_end_ms
    ):
        return None
    receipt = {
        "schema_version": SCHEMA,
        "candidate_id": resolution.candidate_id,
        "original_anchor": {
            key: (job.get("timeline") or {}).get(key)
            for key in ("anchor_start_ms", "anchor_end_ms")
        },
        "predecessor_boundary": {
            "action": resolution.action.value,
            "reason_codes": list(resolution.reason_codes),
            "resolved_start_ms": resolution.resolved_start_ms,
            "resolved_end_ms": resolution.resolved_end_ms,
        },
        "source_binding": copy.deepcopy(recut["source_binding"]),
        "artifact_hashes": copy.deepcopy(recut["artifact_hashes"]),
        "manifest_path": recut["manifest_path"],
        "manifest_sha256": recut["manifest_sha256"],
        "song_completion": proof,
    }
    return replace(
        resolution,
        action=DecisionAction.AUTO_UPLOAD,
        reason_codes=(),
        next_start_ms=None,
        next_end_ms=None,
    ), receipt


def load_materialized_successor(*, source_video, job, output_dir):
    """Read only the current ordinary candidate's completed Song record.

    Invalid existing media is a held input, never a request to overwrite it.
    A new source or anchor cannot borrow this candidate's old render.
    """
    summary_path = output_dir / "summary.json"
    if not summary_path.exists():
        return None
    if summary_path.is_symlink():
        raise ValueError("SONG_MATERIALIZED_SUCCESSOR_SUMMARY_INVALID")
    raw = summary_path.read_bytes()
    summary = json.loads(raw)
    rows = [
        r for r in summary.get("records", []) if r.get("candidate_id") == job.get("candidate_id")
    ]
    if len(rows) != 1:
        raise ValueError("SONG_MATERIALIZED_SUCCESSOR_CANDIDATE_AMBIGUOUS")
    record = rows[0]
    previous = record.get("materialized_song_successor")
    if previous is not None and (
        not isinstance(previous, dict)
        or previous.get("schema_version") != SCHEMA
        or previous.get("candidate_id") != job.get("candidate_id")
        or any(
            previous.get(key) != (record.get("materialized_recut") or {}).get(key)
            for key in ("source_binding", "manifest_path", "manifest_sha256")
        )
        or any(
            (previous.get("artifact_hashes") or {}).get(key)
            != (record.get("materialized_recut", {}).get("artifact_hashes") or {}).get(key)
            for key in MEDIA_HASH_KEYS
        )
    ):
        raise ValueError("SONG_MATERIALIZED_SUCCESSOR_HISTORY_DRIFT")
    if not previous and not (
        record.get("decision_action") == "AUTO_RECUT"
        and record.get("reason_codes") == ["SONG_FULL_BOUNDARY_READY"]
        and (record.get("source_context_job", {}).get("song_boundary") or {}).get("status")
        == "FULL_SONG_READY"
    ):
        if (record.get("materialized_recut") or {}).get("status") == "MATERIALIZED":
            raise ValueError("SONG_MATERIALIZED_SUCCESSOR_PREDECESSOR_UNSUPPORTED")
        return None
    old_job = record["source_context_job"]
    if job.get("timeline") != old_job.get("timeline") or str(source_video.resolve()) != (
        record.get("materialized_recut", {}).get("source_binding") or {}
    ).get("canonical_path"):
        raise ValueError("SONG_MATERIALIZED_SUCCESSOR_IDENTITY_DRIFT")
    proof = completion(record)
    if not proof.get("ready"):
        raise ValueError(
            "SONG_MATERIALIZED_SUCCESSOR_PROOF_INVALID:" + ",".join(proof.get("reason_codes", []))
        )
    # Preserve the actual predecessor before the ordinary emitter replaces its
    # summary. Re-entry keeps the first predecessor instead of growing a chain.
    predecessor = previous.get("predecessor_summary") if isinstance(previous, dict) else None
    if predecessor:
        p = Path(predecessor["path"])
        if p.is_symlink() or hashlib.sha256(p.read_bytes()).hexdigest() != predecessor[
            "sha256"
        ].removeprefix("sha256:"):
            raise ValueError("SONG_MATERIALIZED_SUCCESSOR_HISTORY_DRIFT")
        origins = [
            r
            for r in json.loads(p.read_bytes()).get("records", [])
            if r.get("candidate_id") == job.get("candidate_id")
        ]
        if (
            len(origins) != 1
            or origins[0]["source_context_job"].get("timeline") != job.get("timeline")
            or origins[0]["materialized_recut"].get("source_binding")
            != record["materialized_recut"].get("source_binding")
            or any(
                origins[0]["materialized_recut"]["artifact_hashes"].get(key)
                != record["materialized_recut"]["artifact_hashes"].get(key)
                for key in MEDIA_HASH_KEYS
            )
        ):
            raise ValueError("SONG_MATERIALIZED_SUCCESSOR_HISTORY_DRIFT")
    else:
        predecessor = _frozen_bytes(output_dir / "song-materialized-input", "summary", raw, ".json")
    current_job = {
        **dict(job),
        **{k: copy.deepcopy(old_job[k]) for k in PROOF_KEYS if k in old_job},
    }
    context = dict(record["source_context"])
    context["reason_codes"] = tuple(context["reason_codes"])
    context["review_required_path"] = context.get("review_required_path") or ""
    return (
        current_job,
        SourceContextExecutionResult(**context),
        copy.deepcopy(record["materialized_recut"]),
        predecessor,
    )
