"""Production host/guest speaker finalization for talk subtitles.

Contract:

1. input SRT is already text-final (ASR, terminology, pronouns, and any human
   corrections are complete);
2. CAM++ supplies acoustic evidence, whole-conversation context resolves short
   or boundary-band cues, and optional hash-bound human decisions are applied;
3. a clean text SRT remains the wording authority, while a review-labelled SRT,
   colour ASS, and evidence manifest are emitted before burn.

The ML imports are lazy so ordinary unit tests do not need the production venv.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shlex
import statistics
import stat
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Mapping, Sequence

from scripts.apply_speaker_turn_overrides import (
    Cue,
    SPEAKER_SUBTITLE_STYLE_ID,
    apply_overrides,
    atomic_write_text,
    sha256_file,
    validate_bound_speaker_override_document,
    write_ass,
    write_srt,
)
from scripts.apply_subtitle_text_overrides import TextCue, parse_srt
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.host_vocal_proof import (
    _extract_checkpoint,
    _load_campplus_pipeline,
    _sha256_directory,
    _validate_profile,
)
from src.autoslice.llm_client import extract_json_object

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id
HOST_SPEAKER = CHANNEL_PROFILE.host_speaker_label
GUEST_SPEAKER = CHANNEL_PROFILE.guest_speaker_label
SPEAKERS = {HOST_SPEAKER, GUEST_SPEAKER}
SPEAKER_FINALIZATION_SCHEMA = f"{PROFILE_ID}-speaker-finalization.v1"
FAST_FRESH_DERIVATION_SCHEMA = f"{PROFILE_ID}-speaker-fast-fresh-derivation.v1"

class SpeakerFinalizationError(RuntimeError):
    pass


SOURCE_SESSION_ANCHOR_SCHEMA = f"{PROFILE_ID}-speaker-source-session-anchors.v1"
MIXED_OVERLAP_EVIDENCE_SCHEMA = f"{PROFILE_ID}-speaker-mixed-overlap-evidence.v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
CAMPP_EMBEDDING_CACHE_SCHEMA = f"{PROFILE_ID}-campp-embedding-cache.v3"
CAMPP_EMBEDDING_DIMENSION = 192
CAMPP_MIN_EMBEDDING_NORM = 1e-3
CAMPP_COSINE_EPSILON = 1e-6
CAMPP_SCORE_ROUNDING_TOLERANCE = 1e-5
SINGLETON_CONTEXT_HOST_MIN_CONFIDENCE = 0.90
SINGLETON_NONLEXICAL_RESIDUALS = {"", "我", "我这", "这", "那", "这个", "那个"}
REVIEW_SHA256_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
REVIEW_TIMESTAMP_RE = re.compile(r"\d{2}:\d{2}:\d{2},\d{3}\Z")


DEFAULT_POLICY: dict[str, float | int] = {
    "host_session_seed_min": 0.68,
    "host_session_anchor_count": 4,
    "guest_seed_max": 0.42,
    "guest_min_duration_ms": 1_800,
    "guest_session_similarity_max": 0.45,
    "guest_cluster_similarity_min": 0.45,
    "ambiguity_band": 0.10,
    "short_cue_ms": 1_500,
    "single_host_median_seed_min": 0.55,
}


def _review_sha256(value: object, *, field: str) -> str:
    matched = REVIEW_SHA256_RE.fullmatch(value) if isinstance(value, str) else None
    if not matched:
        raise SpeakerFinalizationError(f"{field} must be a SHA-256 digest")
    return matched.group(1)


def _validate_review_audio_binding(
    *,
    audio_path_value: object,
    expected_sha256: object,
    field: str,
    expected_root: Path | None = None,
) -> Path:
    if not isinstance(audio_path_value, str) or not audio_path_value:
        raise SpeakerFinalizationError(f"{field} audio_path is missing")
    raw_path = Path(audio_path_value)
    if not raw_path.is_absolute():
        raise SpeakerFinalizationError(f"{field} audio_path must be absolute")
    absolute = raw_path.absolute()
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise SpeakerFinalizationError(f"{field} audio_path is missing: {exc}") from exc
    if absolute.is_symlink() or resolved != absolute or not absolute.is_file():
        raise SpeakerFinalizationError(
            f"{field} audio_path must be a regular non-symlink path"
        )
    if expected_root is not None:
        root = expected_root.absolute()
        try:
            resolved.relative_to(root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise SpeakerFinalizationError(
                f"{field} audio_path escapes the evidence root"
            ) from exc
    expected = _review_sha256(expected_sha256, field=f"{field} audio_sha256")
    if sha256_file(resolved) != expected:
        raise SpeakerFinalizationError(f"{field} audio bytes drifted")
    return resolved


def validate_speaker_review_manifest_document(
    document: object,
    *,
    expected_media_sha256: str | None = None,
    expected_text_sha256: str | None = None,
    cues: Sequence[TextCue] | None = None,
) -> list[dict[str, object]]:
    """Validate that a terminal speaker-review state is self-contained and bound."""

    if not isinstance(document, Mapping):
        raise SpeakerFinalizationError("speaker review manifest must be an object")
    if (
        document.get("status") != "SPEAKER_REVIEW_REQUIRED"
        or document.get("production_ready") is not False
    ):
        raise SpeakerFinalizationError("speaker review manifest status is invalid")
    if not str(document.get("reason") or "").strip():
        raise SpeakerFinalizationError("speaker review manifest reason is missing")
    media_sha256 = _review_sha256(
        document.get("source_media_sha256"), field="source_media_sha256"
    )
    text_sha256 = _review_sha256(
        document.get("text_final_srt_sha256"), field="text_final_srt_sha256"
    )
    if expected_media_sha256 is not None and media_sha256 != _review_sha256(
        expected_media_sha256, field="expected source_media_sha256"
    ):
        raise SpeakerFinalizationError("speaker review media hash mismatch")
    if expected_text_sha256 is not None and text_sha256 != _review_sha256(
        expected_text_sha256, field="expected text_final_srt_sha256"
    ):
        raise SpeakerFinalizationError("speaker review text hash mismatch")

    unresolved_raw = document.get("context_unresolved_cues")
    if not isinstance(unresolved_raw, list) or not unresolved_raw:
        raise SpeakerFinalizationError("speaker review unresolved cues must be non-empty")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in unresolved_raw):
        raise SpeakerFinalizationError("speaker review unresolved cue numbers must be integers")
    unresolved = list(unresolved_raw)
    if len(set(unresolved)) != len(unresolved) or any(value < 1 for value in unresolved):
        raise SpeakerFinalizationError("speaker review unresolved cue numbers are invalid")

    rows_raw = document.get("review_required_cues")
    if not isinstance(rows_raw, list) or not rows_raw:
        raise SpeakerFinalizationError("speaker review evidence rows must be non-empty")
    rows: list[dict[str, object]] = []
    seen: set[int] = set()
    for raw in rows_raw:
        if not isinstance(raw, Mapping):
            raise SpeakerFinalizationError("speaker review evidence row must be an object")
        source_cue = raw.get("source_cue")
        zero_based = raw.get("zero_based_index")
        if (
            isinstance(source_cue, bool)
            or not isinstance(source_cue, int)
            or isinstance(zero_based, bool)
            or not isinstance(zero_based, int)
            or zero_based != source_cue - 1
            or source_cue in seen
        ):
            raise SpeakerFinalizationError("speaker review evidence cue indexes are invalid")
        _review_sha256(raw.get("audio_sha256"), field=f"cue {source_cue} audio_sha256")
        provider_details = raw.get("provider_details")
        if isinstance(provider_details, Mapping) and provider_details.get("audio_path"):
            _validate_review_audio_binding(
                audio_path_value=provider_details.get("audio_path"),
                expected_sha256=raw.get("audio_sha256"),
                field=f"cue {source_cue}",
            )
        if not REVIEW_TIMESTAMP_RE.fullmatch(str(raw.get("start") or "")) or not REVIEW_TIMESTAMP_RE.fullmatch(
            str(raw.get("end") or "")
        ):
            raise SpeakerFinalizationError(f"speaker review cue {source_cue} timestamps are invalid")
        if not isinstance(raw.get("text"), str) or not str(raw.get("text")).strip():
            raise SpeakerFinalizationError(f"speaker review cue {source_cue} text is invalid")
        if cues is not None:
            if source_cue > len(cues):
                raise SpeakerFinalizationError("speaker review evidence cue is out of range")
            cue = cues[source_cue - 1]
            if (raw.get("start"), raw.get("end"), raw.get("text")) != (
                cue.start,
                cue.end,
                cue.text,
            ):
                raise SpeakerFinalizationError(
                    f"speaker review cue {source_cue} text/timeline binding mismatch"
                )
        seen.add(source_cue)
        rows.append(dict(raw))
    if seen != set(unresolved):
        raise SpeakerFinalizationError("speaker review evidence does not exactly cover unresolved cues")
    return rows


def validate_mixed_overlap_evidence_document(
    document: object,
    *,
    expected_media_sha256: str,
    expected_text_sha256: str,
    cues: Sequence[TextCue],
    expected_audio_root: Path,
) -> list[dict[str, object]]:
    """Validate provider evidence that one subtitle cue is not one clean speaker.

    This is deliberately a review gate, not a diarization implementation.  A
    provider may report multiple speaker clusters inside one subtitle cue or
    overlapping speech; the finalizer then refuses to render that cue as one
    colour unless a hash-bound human override covers it.
    """

    if not isinstance(document, Mapping):
        raise SpeakerFinalizationError("mixed/overlap evidence must be an object")
    if document.get("schema_version") != MIXED_OVERLAP_EVIDENCE_SCHEMA:
        raise SpeakerFinalizationError(
            f"mixed/overlap evidence schema must be {MIXED_OVERLAP_EVIDENCE_SCHEMA}"
        )
    status = document.get("status")
    if status not in {"CLEAR", "REVIEW_REQUIRED"}:
        raise SpeakerFinalizationError("mixed/overlap evidence status is invalid")
    media_sha256 = _review_sha256(
        document.get("source_media_sha256"), field="mixed/overlap source_media_sha256"
    )
    text_sha256 = _review_sha256(
        document.get("text_final_srt_sha256"), field="mixed/overlap text_final_srt_sha256"
    )
    if media_sha256 != _review_sha256(
        expected_media_sha256, field="expected mixed/overlap source_media_sha256"
    ):
        raise SpeakerFinalizationError("mixed/overlap evidence media hash mismatch")
    if text_sha256 != _review_sha256(
        expected_text_sha256, field="expected mixed/overlap text_final_srt_sha256"
    ):
        raise SpeakerFinalizationError("mixed/overlap evidence text hash mismatch")

    provider = document.get("provider")
    if not isinstance(provider, Mapping) or not str(provider.get("name") or "").strip():
        raise SpeakerFinalizationError("mixed/overlap evidence provider is missing")
    _review_sha256(
        provider.get("config_sha256"), field="mixed/overlap provider config_sha256"
    )

    rows_raw = document.get("review_required_cues")
    if not isinstance(rows_raw, list):
        raise SpeakerFinalizationError("mixed/overlap review_required_cues must be a list")
    if status == "CLEAR":
        if rows_raw:
            raise SpeakerFinalizationError("CLEAR mixed/overlap evidence contains review cues")
        return []
    if not rows_raw:
        raise SpeakerFinalizationError("mixed/overlap REVIEW_REQUIRED evidence has no cues")

    allowed_reasons = {
        "CUE_MULTI_CLUSTER",
        "CUE_MIXED_SPEAKER",
        "CUE_OVERLAPPING_SPEECH",
    }
    rows: list[dict[str, object]] = []
    seen: set[int] = set()
    for raw in rows_raw:
        if not isinstance(raw, Mapping):
            raise SpeakerFinalizationError("mixed/overlap evidence row must be an object")
        source_cue = raw.get("source_cue")
        zero_based = raw.get("zero_based_index")
        if (
            isinstance(source_cue, bool)
            or not isinstance(source_cue, int)
            or isinstance(zero_based, bool)
            or not isinstance(zero_based, int)
            or zero_based != source_cue - 1
            or source_cue < 1
            or source_cue > len(cues)
            or source_cue in seen
        ):
            raise SpeakerFinalizationError("mixed/overlap evidence cue indexes are invalid")
        cue = cues[source_cue - 1]
        if (raw.get("start"), raw.get("end"), raw.get("text")) != (
            cue.start,
            cue.end,
            cue.text,
        ):
            raise SpeakerFinalizationError(
                f"mixed/overlap cue {source_cue} text/timeline binding mismatch"
            )
        _review_sha256(
            raw.get("audio_sha256"), field=f"mixed/overlap cue {source_cue} audio_sha256"
        )
        reasons_raw = raw.get("reason_codes")
        if (
            not isinstance(reasons_raw, list)
            or not reasons_raw
            or any(not isinstance(reason, str) or reason not in allowed_reasons for reason in reasons_raw)
            or len(set(reasons_raw)) != len(reasons_raw)
        ):
            raise SpeakerFinalizationError(
                f"mixed/overlap cue {source_cue} reason codes are invalid"
            )
        details = raw.get("provider_details")
        if not isinstance(details, Mapping):
            raise SpeakerFinalizationError(
                f"mixed/overlap cue {source_cue} provider details are missing"
            )
        _validate_review_audio_binding(
            audio_path_value=details.get("audio_path"),
            expected_sha256=raw.get("audio_sha256"),
            field=f"mixed/overlap cue {source_cue}",
            expected_root=expected_audio_root,
        )
        if any(reason in {"CUE_MULTI_CLUSTER", "CUE_MIXED_SPEAKER"} for reason in reasons_raw):
            cluster_count = details.get("cluster_count")
            if isinstance(cluster_count, bool) or not isinstance(cluster_count, int) or cluster_count < 2:
                raise SpeakerFinalizationError(
                    f"mixed/overlap cue {source_cue} multi-cluster evidence is invalid"
                )
        if "CUE_OVERLAPPING_SPEECH" in reasons_raw and details.get("overlap_detected") is not True:
            raise SpeakerFinalizationError(
                f"mixed/overlap cue {source_cue} overlap evidence is invalid"
            )
        seen.add(source_cue)
        rows.append(dict(raw))
    return rows


def _require_sha256(value: object, *, field: str) -> str:
    digest = str(value or "")
    if not SHA256_RE.fullmatch(digest):
        raise SpeakerFinalizationError(f"{field} must be a SHA-256 digest")
    return digest


def _snapshot_bound_input(source: Path, target: Path) -> tuple[Path, str]:
    """Freeze one small control-plane input as the exact bytes we hash/use."""

    resolved = source.resolve(strict=True)
    payload = resolved.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target.resolve(), hashlib.sha256(payload).hexdigest()


def _validate_source_session_anchor_document(
    document: object,
    *,
    target_media_sha256: str,
    target_media_path: Path | None = None,
    profile_sha256: str,
    model_tree_sha256: str,
    reference_hashes: Mapping[str, str],
    host_seed_min: float,
) -> dict[str, object]:
    """Validate the immutable contract before any donor media is decoded.

    A source-session anchor is not a relaxed threshold.  Every donor cue must
    already pass the same static enrollment gate used for clip-local anchors,
    and the manifest explicitly allowlists the exact target recut hash.  This
    lets a short clip reuse trusted speech from the same source recording while
    keeping cross-session or drifted anchors fail-closed.
    """

    if not isinstance(document, dict) or document.get("schema_version") != SOURCE_SESSION_ANCHOR_SCHEMA:
        raise SpeakerFinalizationError(
            f"source-session anchor schema must be {SOURCE_SESSION_ANCHOR_SCHEMA}"
        )
    if (
        document.get("status") != "READY"
        or document.get("subject") != CHANNEL_PROFILE.display_name
    ):
        raise SpeakerFinalizationError(
            "source-session anchor manifest is not READY for the selected host"
        )
    session_id = str(document.get("source_session_id") or "").strip()
    if not session_id:
        raise SpeakerFinalizationError("source-session anchor manifest is missing source_session_id")
    if _require_sha256(document.get("profile_sha256"), field="source-session profile_sha256") != profile_sha256:
        raise SpeakerFinalizationError("source-session anchor profile hash drift")
    if _require_sha256(document.get("model_tree_sha256"), field="source-session model_tree_sha256") != model_tree_sha256:
        raise SpeakerFinalizationError("source-session anchor model hash drift")
    declared_references = document.get("reference_hashes")
    if not isinstance(declared_references, Mapping) or dict(declared_references) != dict(reference_hashes):
        raise SpeakerFinalizationError("source-session anchor reference hashes drift")
    source_recording = str(document.get("source_recording") or "").strip()
    if not source_recording or not Path(source_recording).is_absolute():
        raise SpeakerFinalizationError(
            "source-session anchor source_recording must be an absolute path"
        )
    allowed_targets = document.get("allowed_targets")
    if not isinstance(allowed_targets, list) or not allowed_targets:
        raise SpeakerFinalizationError("source-session anchor target allowlist is missing")
    normalized_target_hashes: list[str] = []
    normalized_target_ids: list[str] = []
    matching_target: Mapping[str, object] | None = None
    for position, target in enumerate(allowed_targets, start=1):
        if not isinstance(target, Mapping):
            raise SpeakerFinalizationError(
                f"source-session allowed target {position} must be an object"
            )
        candidate_id = str(target.get("candidate_id") or "").strip()
        media_path = str(target.get("media_path") or "").strip()
        provenance_path = str(target.get("provenance_path") or "").strip()
        if not candidate_id or not media_path or not provenance_path:
            raise SpeakerFinalizationError(
                f"source-session allowed target {position} is missing provenance fields"
            )
        if not Path(media_path).is_absolute() or not Path(provenance_path).is_absolute():
            raise SpeakerFinalizationError(
                f"source-session allowed target {position} paths must be absolute"
            )
        media_digest = _require_sha256(
            target.get("media_sha256"),
            field=f"source-session allowed target {position} media_sha256",
        )
        _require_sha256(
            target.get("provenance_sha256"),
            field=f"source-session allowed target {position} provenance_sha256",
        )
        normalized_target_hashes.append(media_digest)
        normalized_target_ids.append(candidate_id)
        if media_digest == target_media_sha256:
            matching_target = target
    if len(set(normalized_target_hashes)) != len(normalized_target_hashes):
        raise SpeakerFinalizationError("source-session anchor target allowlist contains duplicates")
    if len(set(normalized_target_ids)) != len(normalized_target_ids):
        raise SpeakerFinalizationError("source-session anchor target candidate IDs contain duplicates")
    if matching_target is None:
        raise SpeakerFinalizationError("target media is not allowlisted for source-session anchors")
    # The runtime may consume a byte-identical temporary copy (the remote
    # wrapper deliberately scps to /tmp).  Identity is therefore the frozen
    # media SHA; the canonical path remains provenance, not execution state.
    if target_media_path is not None:
        target_media_path.resolve(strict=True)

    anchors = document.get("anchors")
    if not isinstance(anchors, list) or len(anchors) < 2:
        raise SpeakerFinalizationError("source-session anchor manifest requires at least two anchors")
    reference_ids = set(reference_hashes)
    seen_sources: set[tuple[object, ...]] = set()
    needs_cue_donor = False
    for position, anchor in enumerate(anchors, start=1):
        if not isinstance(anchor, Mapping):
            raise SpeakerFinalizationError(f"source-session anchor {position} must be an object")
        anchor_type = str(anchor.get("anchor_type") or "donor_cue")
        if anchor_type == "donor_cue":
            needs_cue_donor = True
            cue_index = anchor.get("source_cue")
            if not isinstance(cue_index, int) or isinstance(cue_index, bool) or cue_index < 1:
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} has invalid source_cue"
                )
            source_key = (anchor_type, cue_index)
            if not str(anchor.get("start") or "") or not str(anchor.get("end") or ""):
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} is missing timing"
                )
        elif anchor_type == "source_recording_segment":
            start_ms = anchor.get("source_start_ms")
            end_ms = anchor.get("source_end_ms")
            if (
                not isinstance(start_ms, int)
                or isinstance(start_ms, bool)
                or not isinstance(end_ms, int)
                or isinstance(end_ms, bool)
                or start_ms < 0
                or end_ms - start_ms < 1_000
                or end_ms - start_ms > 30_000
            ):
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} has invalid source recording segment"
                )
            source_key = (anchor_type, start_ms, end_ms)
        else:
            raise SpeakerFinalizationError(
                f"source-session anchor {position} has invalid anchor_type"
            )
        if source_key in seen_sources:
            raise SpeakerFinalizationError("source-session anchor sources must be unique")
        seen_sources.add(source_key)
        if not isinstance(anchor.get("text"), str):
            raise SpeakerFinalizationError(f"source-session anchor {position} is missing text")
        _require_sha256(anchor.get("sample_sha256"), field=f"source-session anchor {position} sample")
        scores = anchor.get("reference_scores")
        if not isinstance(scores, Mapping) or set(scores) != reference_ids:
            raise SpeakerFinalizationError(f"source-session anchor {position} reference scores drift")
        normalized_scores: list[float] = []
        for reference_id in sorted(reference_ids):
            score = scores.get(reference_id)
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0.0 <= float(score) <= 1.0:
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} has invalid score for {reference_id}"
                )
            normalized_scores.append(float(score))
        declared_median = anchor.get("enroll_median_score")
        if not isinstance(declared_median, (int, float)) or isinstance(declared_median, bool):
            raise SpeakerFinalizationError(f"source-session anchor {position} median is missing")
        actual_declared_median = float(statistics.median(normalized_scores))
        if abs(float(declared_median) - actual_declared_median) > 1e-6:
            raise SpeakerFinalizationError(f"source-session anchor {position} median drift")
        if actual_declared_median < host_seed_min:
            raise SpeakerFinalizationError(
                f"source-session anchor {position} does not pass the unchanged host seed gate"
            )
    donor = document.get("donor")
    if needs_cue_donor and not isinstance(donor, Mapping):
        raise SpeakerFinalizationError("source-session cue anchors require a donor")
    if donor is not None:
        if not isinstance(donor, Mapping):
            raise SpeakerFinalizationError("source-session anchor donor must be an object")
        if not str(donor.get("candidate_id") or "").strip():
            raise SpeakerFinalizationError("source-session anchor donor candidate_id is missing")
        for key in ("media_path", "text_srt_path", "provenance_path"):
            if not str(donor.get(key) or "").strip():
                raise SpeakerFinalizationError(f"source-session anchor donor {key} is missing")
            if not Path(str(donor[key])).is_absolute():
                raise SpeakerFinalizationError(
                    f"source-session anchor donor {key} must be absolute"
                )
        _require_sha256(donor.get("media_sha256"), field="source-session donor media_sha256")
        _require_sha256(
            donor.get("text_srt_sha256"), field="source-session donor text_srt_sha256"
        )
        _require_sha256(
            donor.get("provenance_sha256"), field="source-session donor provenance_sha256"
        )
    return dict(document)


def _validate_source_session_provenance(
    document: Mapping[str, object],
    *,
    target_media_path: Path,
    target_media_sha256: str,
) -> dict[str, object]:
    """Verify hash-bound donor/target specs name the same source recording."""

    source_recording = str(document["source_recording"])
    donor = document.get("donor")
    targets = document["allowed_targets"]
    assert isinstance(targets, list)
    target = next(
        (
            item
            for item in targets
            if isinstance(item, Mapping) and item.get("media_sha256") == target_media_sha256
        ),
        None,
    )
    if not isinstance(target, Mapping):
        raise SpeakerFinalizationError("source-session target provenance is missing")

    def load_spec(entry: Mapping[str, object], *, role: str) -> tuple[Path, dict[str, object]]:
        spec_path = Path(str(entry["provenance_path"])).resolve(strict=True)
        if sha256_file(spec_path) != entry["provenance_sha256"]:
            raise SpeakerFinalizationError(f"source-session {role} provenance hash drift")
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SpeakerFinalizationError(
                f"cannot read source-session {role} provenance: {exc}"
            ) from exc
        if not isinstance(spec, dict) or spec.get("candidate_id") != entry["candidate_id"]:
            raise SpeakerFinalizationError(
                f"source-session {role} provenance candidate drift"
            )
        pieces = spec.get("pieces")
        if not isinstance(pieces, list) or not pieces:
            raise SpeakerFinalizationError(
                f"source-session {role} provenance has no source pieces"
            )
        remote_media = {
            str(piece.get("remote_media") or "")
            for piece in pieces
            if isinstance(piece, Mapping)
        }
        if remote_media != {source_recording}:
            raise SpeakerFinalizationError(
                f"source-session {role} provenance names a different source recording"
            )
        return spec_path, spec

    donor_spec_path: Path | None = None
    if isinstance(donor, Mapping):
        donor_spec_path, _donor_spec = load_spec(donor, role="donor")
    target_spec_path, _target_spec = load_spec(target, role="target")
    canonical_target_media = Path(str(target["media_path"])).resolve(strict=True)
    if sha256_file(canonical_target_media) != target_media_sha256:
        raise SpeakerFinalizationError("source-session canonical target media hash drift")
    if sha256_file(target_media_path) != target_media_sha256:
        raise SpeakerFinalizationError("source-session target media hash drift")
    return {
        "source_recording": source_recording,
        "donor_provenance": str(donor_spec_path) if donor_spec_path is not None else None,
        "donor_provenance_sha256": (
            donor["provenance_sha256"] if isinstance(donor, Mapping) else None
        ),
        "target_candidate_id": target["candidate_id"],
        "canonical_target_media": str(canonical_target_media),
        "canonical_target_media_sha256": target_media_sha256,
        "target_provenance": str(target_spec_path),
        "target_provenance_sha256": target["provenance_sha256"],
    }


def _ms(value: str) -> int:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000 + int(millis)


def _policy(profile: Mapping[str, object]) -> dict[str, float | int]:
    result = dict(DEFAULT_POLICY)
    configured = profile.get("talk_speaker_policy")
    if isinstance(configured, Mapping):
        for key in result:
            value = configured.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = value
    return result


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Dependency-free equivalent of Torch cosine for test/runtime fallbacks.

    ``torch.nn.CosineSimilarity`` clamps each vector norm independently, not
    their product. Production still delegates to the loaded ModelScope
    pipeline's own scorer so float32 rounding stays identical to the original
    pair path.
    """

    if not left or not right:
        raise SpeakerFinalizationError("CAM++ embedding must not be empty")
    if len(left) != len(right):
        raise SpeakerFinalizationError("CAM++ embedding dimensions do not match")
    dot = left_norm_sq = right_norm_sq = 0.0
    for raw_a, raw_b in zip(left, right, strict=True):
        a, b = float(raw_a), float(raw_b)
        if not math.isfinite(a) or not math.isfinite(b):
            raise SpeakerFinalizationError("CAM++ embedding values must be finite")
        dot += a * b
        left_norm_sq += a * a
        right_norm_sq += b * b
    if not all(math.isfinite(value) for value in (dot, left_norm_sq, right_norm_sq)):
        raise SpeakerFinalizationError("CAM++ embedding arithmetic must be finite")
    denominator = max(math.sqrt(left_norm_sq), CAMPP_COSINE_EPSILON) * max(
        math.sqrt(right_norm_sq), CAMPP_COSINE_EPSILON
    )
    score = dot / denominator
    if not math.isfinite(score):
        raise SpeakerFinalizationError("CAM++ similarity must be finite")
    if not -1.0 - CAMPP_SCORE_ROUNDING_TOLERANCE <= score <= 1.0 + CAMPP_SCORE_ROUNDING_TOLERANCE:
        raise SpeakerFinalizationError("CAM++ similarity must be finite and within [-1, 1]")
    return min(1.0, max(-1.0, score))


def _validate_campp_embedding(values: Sequence[float]) -> list[float]:
    vector = [float(value) for value in values]
    if len(vector) != CAMPP_EMBEDDING_DIMENSION:
        raise SpeakerFinalizationError(
            f"CAM++ embedding must have {CAMPP_EMBEDDING_DIMENSION} values"
        )
    if any(not math.isfinite(value) for value in vector):
        raise SpeakerFinalizationError("CAM++ embedding values must be finite")
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm < CAMPP_MIN_EMBEDDING_NORM:
        raise SpeakerFinalizationError("CAM++ embedding norm is degenerate")
    return vector


def _campp_runtime_fingerprint(verifier: Callable[..., object]) -> str:
    components: dict[str, str] = {
        "pipeline_class": (
            f"{verifier.__class__.__module__}.{verifier.__class__.__qualname__}"
        )
    }
    for package in ("modelscope", "torch", "numpy"):
        try:
            components[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            components[package] = "unavailable"
    payload = json.dumps(components, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _embedding_binding_sha256(
    *,
    vector: Sequence[float],
    model_hash: str,
    runtime_fingerprint: str,
    audio_sha256: str,
) -> str:
    payload = json.dumps(
        {
            "schema_version": CAMPP_EMBEDDING_CACHE_SCHEMA,
            "model_sha256": model_hash,
            "runtime_fingerprint": runtime_fingerprint,
            "audio_sha256": audio_sha256,
            "dimension": CAMPP_EMBEDDING_DIMENSION,
            "embedding": list(vector),
        },
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_cached_embedding(
    path: Path,
    *,
    model_hash: str,
    runtime_fingerprint: str,
    audio_sha256: str,
) -> list[float] | None:
    """Return a fully bound cache entry, or require a trusted re-embedding."""

    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            return None
        if document.get("schema_version") != CAMPP_EMBEDDING_CACHE_SCHEMA:
            return None
        if document.get("model_sha256") != model_hash:
            return None
        if document.get("runtime_fingerprint") != runtime_fingerprint:
            return None
        if document.get("audio_sha256") != audio_sha256:
            return None
        if document.get("dimension") != CAMPP_EMBEDDING_DIMENSION:
            return None
        raw_vector = document.get("embedding")
        if not isinstance(raw_vector, list):
            return None
        vector = _validate_campp_embedding(raw_vector)
        if document.get("binding_sha256") != _embedding_binding_sha256(
            vector=vector,
            model_hash=model_hash,
            runtime_fingerprint=runtime_fingerprint,
            audio_sha256=audio_sha256,
        ):
            return None
        return vector
    except (OSError, TypeError, ValueError, SpeakerFinalizationError):
        return None


def _write_cached_embedding(
    path: Path,
    *,
    vector: Sequence[float],
    model_hash: str,
    runtime_fingerprint: str,
    audio_sha256: str,
) -> None:
    validated = _validate_campp_embedding(vector)
    document = {
        "schema_version": CAMPP_EMBEDDING_CACHE_SCHEMA,
        "model_sha256": model_hash,
        "runtime_fingerprint": runtime_fingerprint,
        "audio_sha256": audio_sha256,
        "dimension": CAMPP_EMBEDDING_DIMENSION,
        "embedding": validated,
    }
    document["binding_sha256"] = _embedding_binding_sha256(
        vector=validated,
        model_hash=model_hash,
        runtime_fingerprint=runtime_fingerprint,
        audio_sha256=audio_sha256,
    )
    atomic_write_text(
        path,
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
    )


def _campp_similarity_score(
    verifier: Callable[..., object],
    left: Sequence[float],
    right: Sequence[float],
) -> float:
    """Score cached embeddings through ModelScope's exact float32 code path."""

    # Validate structure and model-specific magnitude before handing data to
    # Torch; do not pre-compute cosine in Python because its float64 rounding
    # is not the production authority.
    validated_left = _validate_campp_embedding(left)
    validated_right = _validate_campp_embedding(right)
    compute = getattr(verifier, "compute_cos_similarity", None)
    if not callable(compute):
        raise SpeakerFinalizationError("CAM++ runtime has no compute_cos_similarity method")
    try:
        try:
            import torch  # type: ignore[import-not-found]
        except ImportError:
            # Lightweight unit-test fakes can accept validated Python lists;
            # the production speaker venv always has Torch.
            raw_score = compute(validated_left, validated_right)
        else:  # pragma: no cover - exercised by the production ML runtime
            raw_score = compute(
                torch.tensor(validated_left, dtype=torch.float32),
                torch.tensor(validated_right, dtype=torch.float32),
            )
        score = float(raw_score)
    except SpeakerFinalizationError:
        raise
    except Exception as exc:
        raise SpeakerFinalizationError(
            f"CAM++ cached-embedding similarity failed: {type(exc).__name__}: {exc}"
        ) from exc
    if not math.isfinite(score):
        raise SpeakerFinalizationError("CAM++ similarity must be finite")
    if not -1.0 - CAMPP_SCORE_ROUNDING_TOLERANCE <= score <= 1.0 + CAMPP_SCORE_ROUNDING_TOLERANCE:
        raise SpeakerFinalizationError("CAM++ similarity must be finite and within [-1, 1]")
    return min(1.0, max(-1.0, score))


def _campp_embedding(verifier: Callable[..., object], path: Path) -> list[float]:
    """Return the CAM++ speaker embedding for one wav.

    ModelScope's speaker-verification pipeline embeds every input inside
    ``forward`` and only derives a pairwise score when exactly two inputs are
    passed (``postprocess`` returns no score otherwise), so a single-input
    ``output_emb`` call yields that clip's embedding with one inference.
    """
    result = verifier([str(path)], output_emb=True)
    embeddings = result["embs"] if isinstance(result, Mapping) and "embs" in result else result
    row = embeddings[0]
    values = row.tolist() if hasattr(row, "tolist") else list(row)
    return _validate_campp_embedding(values)


def _build_embedding_similarity(
    *, verifier: Callable[..., object], model_hash: str, work_dir: Path
) -> Callable[[Path, Path], float]:
    """Embed each cue once, then score pairs by ModelScope cosine.

    The previous implementation asked the pipeline for every (cue, anchor) pair
    and re-embedded both wavs each time, so a talk clip paid O(cues x anchors)
    CAM++ inferences and long clips blew past the finalizer timeout.  A CAM++
    pair score is the cosine of the two per-clip embeddings, so embedding each
    wav a single time and caching the vector is exactly score-preserving while
    collapsing the cost to O(cues) inferences.
    """
    cache_dir = work_dir / "embedding-cache-v3"
    embeddings: dict[str, list[float]] = {}
    runtime_fingerprint = _campp_runtime_fingerprint(verifier)
    fingerprints: dict[Path, str] = {}

    def fingerprint(path: Path) -> str:
        resolved = path.resolve()
        if resolved not in fingerprints:
            fingerprints[resolved] = sha256_file(resolved)
        return fingerprints[resolved]

    def embedding(path: Path) -> list[float]:
        # Cue basenames are reused after text/timing corrections. Content-bound
        # keys keep a persistent work dir from serving stale voice vectors.
        audio_sha256 = fingerprint(path)
        key = model_hash + "|" + runtime_fingerprint + "|" + audio_sha256
        vector = embeddings.get(key)
        if vector is None:
            cache_name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
            cache_path = cache_dir / cache_name
            vector = _load_cached_embedding(
                cache_path,
                model_hash=model_hash,
                runtime_fingerprint=runtime_fingerprint,
                audio_sha256=audio_sha256,
            )
        if vector is None:
            vector = _campp_embedding(verifier, path)
            _write_cached_embedding(
                cache_path,
                vector=vector,
                model_hash=model_hash,
                runtime_fingerprint=runtime_fingerprint,
                audio_sha256=audio_sha256,
            )
            embeddings[key] = vector
        else:
            embeddings[key] = vector
        return vector

    def similarity(left: Path, right: Path) -> float:
        return round(
            _campp_similarity_score(verifier, embedding(left), embedding(right)),
            5,
        )

    return similarity


def _two_means(values: Sequence[float]) -> tuple[float, float, float]:
    if len(values) < 2:
        raise SpeakerFinalizationError("not enough cue margins for two-speaker clustering")
    low, high = min(values), max(values)
    if low == high:
        raise SpeakerFinalizationError("speaker margin distribution has no separation")
    for _ in range(40):
        high_side = [value for value in values if abs(value - high) < abs(value - low)]
        low_side = [value for value in values if abs(value - high) >= abs(value - low)]
        if not high_side or not low_side:
            break
        next_low = statistics.mean(low_side)
        next_high = statistics.mean(high_side)
        if abs(next_low - low) < 1e-8 and abs(next_high - high) < 1e-8:
            low, high = next_low, next_high
            break
        low, high = next_low, next_high
    if low > high:
        low, high = high, low
    return float(low), float(high), float((low + high) / 2)


def resolve_ambiguous_labels(
    labels: Sequence[str | None],
    margins: Sequence[float],
    threshold: float,
    context_votes: Mapping[int, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Resolve ambiguous cue labels and record the evidence source.

    ``context_votes`` uses zero-based cue indices.  Missing votes fall back to
    matching neighbours when possible, then to the acoustic side of the
    threshold.  This is the best-effort policy for short interjections; it
    never silently claims that fallback evidence was a confident voiceprint.
    """

    if len(labels) != len(margins):
        raise ValueError("labels and margins must have equal length")
    result = list(labels)
    sources = ["campp_audio" if label is not None else "unresolved" for label in labels]
    votes = context_votes or {}
    for index, vote in votes.items():
        if 0 <= index < len(result) and result[index] is None and vote in SPEAKERS:
            result[index] = vote
            sources[index] = "whole_clip_context"
    for index, label in enumerate(result):
        if label is not None:
            continue
        previous = next((result[j] for j in range(index - 1, -1, -1) if result[j]), None)
        following = next((result[j] for j in range(index + 1, len(result)) if result[j]), None)
        if previous is not None and previous == following:
            result[index] = previous
            sources[index] = "neighbour_context_fallback"
        else:
            result[index] = HOST_SPEAKER if margins[index] >= threshold else GUEST_SPEAKER
            sources[index] = "acoustic_threshold_fallback"
    return [str(label) for label in result], sources


def _reviewed_context_votes(
    override_document: Mapping[str, object],
    *,
    cue_count: int,
) -> dict[int, str]:
    """Load hash-bound, human-accepted context votes from an override asset.

    The JSON uses one-based cue numbers; the analyzer uses zero-based indices.
    These votes stabilize only an already reviewed clip.  New clips continue to
    use the normal whole-clip context judge.
    """

    raw = override_document.get("reviewed_context_votes")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SpeakerFinalizationError("reviewed_context_votes must be an object")
    labels = raw.get("labels")
    if not isinstance(labels, Mapping) or not labels:
        raise SpeakerFinalizationError("reviewed_context_votes.labels must be a non-empty object")
    if not str(raw.get("authority") or "").strip():
        raise SpeakerFinalizationError("reviewed_context_votes.authority must be non-empty")
    expected_source = str(override_document.get("source_srt_sha256") or "")
    bound_source = str(raw.get("source_automatic_srt_sha256") or "")
    if not expected_source or not bound_source:
        raise SpeakerFinalizationError(
            "reviewed context votes require source_srt_sha256 and source_automatic_srt_sha256"
        )
    if expected_source != bound_source:
        raise SpeakerFinalizationError("reviewed context vote source hash does not match source_srt_sha256")

    votes: dict[int, str] = {}
    for cue_number_raw, speaker_raw in labels.items():
        try:
            cue_number = int(str(cue_number_raw))
        except ValueError as exc:
            raise SpeakerFinalizationError(
                f"reviewed context cue number is invalid: {cue_number_raw!r}"
            ) from exc
        speaker = str(speaker_raw)
        if not 1 <= cue_number <= cue_count:
            raise SpeakerFinalizationError(f"reviewed context cue is out of range: {cue_number}")
        if speaker not in SPEAKERS:
            raise SpeakerFinalizationError(
                f"reviewed context speaker is invalid for cue {cue_number}: {speaker!r}"
            )
        votes[cue_number - 1] = speaker
    return votes


_SINGLETON_PUNCT_RX = re.compile(r"[\s，。！？!?、,.…~～—\-]+")
_SINGLETON_LAUGHTER_RX = re.compile(r"(?:哈{2,}|嘿{2,}|呵{2,}|嘻{2,}|(?:ha){2,}|笑死|笑)", re.IGNORECASE)
_SINGLETON_INTERJECTION_RX = re.compile(r"(?:啊|呀|哎|唉|诶|欸|嗯|呃|额|哦|噢|哼|嘛|呢|吧)+")


def _singleton_nonlexical_dominant(text: str) -> bool:
    compact = _SINGLETON_PUNCT_RX.sub("", str(text)).lower()
    without_laughter, laughter_count = _SINGLETON_LAUGHTER_RX.subn("", compact)
    residual, interjection_count = _SINGLETON_INTERJECTION_RX.subn("", without_laughter)
    return bool(laughter_count or interjection_count) and residual in SINGLETON_NONLEXICAL_RESIDUALS


def _resolve_singleton_outlier(
    *,
    cues: Sequence[TextCue],
    singleton_index: int,
    seed_scores: Sequence[float],
    host_bank_scores: Mapping[int, float],
    clip_host_indices: Sequence[int],
    policy: Mapping[str, float | int],
    cue_audio_sha256: Sequence[str],
    context_call: Callable[[str], str] | None,
    reviewed_context_votes: Mapping[int, str] | None = None,
) -> dict[str, object]:
    """Resolve one low outlier through the existing whole-clip context judge."""

    host_min = float(policy["single_host_median_seed_min"])
    bank_min = float(policy["guest_session_similarity_max"])

    def cue_evidence(index: int) -> dict[str, object]:
        return {
            "source_cue": index + 1,
            "zero_based_index": index,
            "start": cues[index].start,
            "end": cues[index].end,
            "text": cues[index].text,
            "seed_score": round(float(seed_scores[index]), 8),
            "host_bank_score": round(float(host_bank_scores[index]), 8),
            "audio_sha256": cue_audio_sha256[index],
        }

    neighbours = [
        {
            **cue_evidence(index),
            "host_supported": seed_scores[index] >= host_min
            and host_bank_scores[index] >= bank_min,
        }
        for index in (singleton_index - 1, singleton_index + 1)
        if 0 <= index < len(cues)
    ]
    gates = {
        "nonlexical_dominant": _singleton_nonlexical_dominant(cues[singleton_index].text),
        "strong_host_majority": statistics.median(seed_scores) >= host_min,
        "strong_host_anchors": len(clip_host_indices)
        >= int(policy["host_session_anchor_count"]),
        "adjacent_host": len(neighbours) == 2
        and all(bool(row["host_supported"]) for row in neighbours),
    }
    evidence = {
        **cue_evidence(singleton_index),
        "duration_ms": _ms(cues[singleton_index].end) - _ms(cues[singleton_index].start),
        "nonlexical_dominant": gates["nonlexical_dominant"],
        "clip_median_seed_score": round(float(statistics.median(seed_scores)), 8),
        "strong_host_anchor_cues": [index + 1 for index in clip_host_indices],
        "neighbours": neighbours,
    }
    reviewed = (reviewed_context_votes or {}).get(singleton_index)
    labels: list[str | None] = [HOST_SPEAKER] * len(cues)
    labels[singleton_index] = None
    context_votes, context_attempts, context_errors = _whole_clip_context_votes(
        cues,
        labels,
        [singleton_index],
        context_call,
        initial_speakers=(
            {singleton_index: str(reviewed)} if reviewed in SPEAKERS else None
        ),
        allow_review=True,
        require_confidence=True,
    )
    context_decision = context_votes.get(singleton_index)

    reviewed_ready = reviewed in SPEAKERS
    automatic_host_ready = bool(
        all(gates.values())
        and context_decision
        and context_decision.get("speaker") == HOST_SPEAKER
        and float(context_decision.get("confidence") or 0.0)
        >= SINGLETON_CONTEXT_HOST_MIN_CONFIDENCE
    )
    gate_failures = {
        "nonlexical_dominant": "SINGLETON_LEXICAL_CONTENT",
        "strong_host_majority": "HOST_MAJORITY_INSUFFICIENT",
        "strong_host_anchors": "HOST_ANCHORS_INSUFFICIENT",
        "adjacent_host": "NEIGHBOR_HOST_EVIDENCE_INCOMPLETE",
    }
    reason_codes = [] if reviewed_ready else [
        gate_failures[name] for name, passed in gates.items() if not passed
    ]
    if not reviewed_ready:
        if context_decision is None:
            reason_codes.append("CONTEXT_INCOMPLETE")
        elif context_decision["speaker"] == GUEST_SPEAKER:
            reason_codes.append("CONTEXT_GUEST")
        elif context_decision["speaker"] == "REVIEW":
            reason_codes.append("CONTEXT_REVIEW")
        elif float(context_decision["confidence"]) < SINGLETON_CONTEXT_HOST_MIN_CONFIDENCE:
            reason_codes.append("CONTEXT_HOST_CONFIDENCE_LOW")
        if (
            context_decision
            and context_decision["speaker"] == HOST_SPEAKER
            and not all(gates.values())
        ):
            reason_codes.append("CONTEXT_ACOUSTIC_CONFLICT")
    singleton_speaker = (
        str(reviewed)
        if reviewed_ready
        else HOST_SPEAKER
        if automatic_host_ready
        else str((context_decision or {}).get("speaker"))
        if (context_decision or {}).get("speaker") in SPEAKERS
        else GUEST_SPEAKER
    )
    singleton_source = (
        "accepted_context_baseline"
        if reviewed_ready
        else "whole_clip_context_singleton"
        if automatic_host_ready
        else "speaker_review_required_singleton"
    )
    review_required = not (reviewed_ready or automatic_host_ready)
    evidence.update(
        {
            "gates": gates,
            "context_decision": context_decision,
            "context_errors": context_errors,
            "review_reason_codes": reason_codes,
        }
    )
    return {
        "mode": "singleton_outlier",
        "multi_speaker_detected": singleton_speaker == GUEST_SPEAKER and not review_required,
        "context_attempts": context_attempts,
        "context_errors": context_errors,
        "context_required_cues": [singleton_index + 1],
        "context_unresolved_cues": [singleton_index + 1] if review_required else [],
        "review_required": review_required,
        "review_reason_codes": reason_codes,
        "singleton_evidence": [evidence],
        "decisions": [
            {
                "source_index": index + 1,
                "speaker": singleton_speaker if index == singleton_index else HOST_SPEAKER,
                "decision_source": singleton_source if index == singleton_index else "campp_single_host_majority",
                "seed_score": round(float(seed_scores[index]), 8),
                "host_score": round(float(host_bank_scores[index]), 8),
                "guest_score": None,
                "margin": None,
            }
            for index in range(len(cues))
        ],
    }


def _context_prompt(cues: Sequence[TextCue], labels: Sequence[str | None], ambiguous: Sequence[int]) -> str:
    rows = [
        f"{index}. [{label or '待定'}] {cue.text}"
        for index, (cue, label) in enumerate(zip(cues, labels, strict=True), start=1)
    ]
    return (
        f"这是{HOST_SPEAKER}（直播间主人）与{GUEST_SPEAKER}主播的完整切片字幕，文本、专名和代词已经最终定稿。"
        f"大部分行已经由声纹标为[{HOST_SPEAKER}]/[{GUEST_SPEAKER}]；只有[待定]行因太短或处于声纹分界带，需要根据整段问答、称呼方向和上下文判断。\n"
        f"规则：别人评价{HOST_SPEAKER}后，她的反问/自辩通常是{HOST_SPEAKER}；对{HOST_SPEAKER}使用第三人称评价的通常是{GUEST_SPEAKER}；"
        f"对话中作为名字出现的精确词 {CHANNEL_PROFILE.speaker_identity_aliases[-1]} 是{HOST_SPEAKER}的自称之一，不是第四位说话人或{GUEST_SPEAKER}嘉宾；"
        "不要修改文字，不要把相邻两个人的连续短句合成同一说话人。"
        "单个声纹离群点不能独立建立嘉宾簇；若上下文仍可能是真实嘉宾、证据冲突或无法确定，返回 REVIEW。\n"
        f"待定行号（1-based）：{[index + 1 for index in ambiguous]}\n\n"
        + "\n".join(rows)
        + f'\n\n只输出 JSON：{{"labels":[{{"n":1,"speaker":"{HOST_SPEAKER}","confidence":0.95,'
        f'"reason":"具体上下文依据"}}]}}，且只列待定行。speaker 只能是{HOST_SPEAKER}、{GUEST_SPEAKER}或 REVIEW；confidence 为 0..1。'
    )


def _whole_clip_context_votes(
    cues: Sequence[TextCue],
    labels: Sequence[str | None],
    ambiguous: Sequence[int],
    context_call: Callable[[str], str] | None,
    *,
    initial_speakers: Mapping[int, str] | None = None,
    allow_review: bool = False,
    require_confidence: bool = False,
) -> tuple[dict[int, dict[str, object]], int, list[str]]:
    """Use the one whole-clip judge/retry/schema path for all ambiguous cues."""

    votes = {
        index: {
            "n": index + 1,
            "speaker": speaker,
            "confidence": 1.0,
            "reason": "hash-bound reviewed context vote",
            "source": "hash_bound_reviewed_context",
        }
        for index, speaker in (initial_speakers or {}).items()
        if index in ambiguous and speaker in SPEAKERS
    }
    attempts = 0
    errors: list[str] = []
    if context_call is None:
        return votes, attempts, errors
    allowed = SPEAKERS | ({"REVIEW"} if allow_review else set())
    for _attempt in range(3):
        pending = [index for index in ambiguous if index not in votes]
        if not pending:
            break
        attempts += 1
        try:
            payload = extract_json_object(context_call(_context_prompt(cues, labels, pending)))
            rows = payload.get("labels", [])
            if not isinstance(rows, list):
                raise ValueError("labels must be a list")
            attempt_votes: dict[int, dict[str, object]] = {}
            for row in rows:
                cue_index = int(row["n"]) - 1
                speaker = str(row["speaker"])
                if cue_index not in pending or speaker not in allowed:
                    continue
                if cue_index in attempt_votes:
                    raise ValueError(f"duplicate context label for cue {cue_index + 1}")
                confidence: object = row.get("confidence")
                reason = str(row.get("reason") or "").strip()
                if require_confidence:
                    if isinstance(confidence, bool) or not isinstance(
                        confidence, (int, float)
                    ):
                        raise ValueError("context confidence must be a JSON number")
                    confidence = float(confidence)
                    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                        raise ValueError("context confidence must be finite within 0..1")
                    if not reason:
                        raise ValueError("context reason must be non-empty")
                attempt_votes[cue_index] = {
                    "n": cue_index + 1,
                    "speaker": speaker,
                    "confidence": confidence,
                    "reason": reason,
                    "source": "whole_clip_context",
                }
            votes.update(attempt_votes)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    return votes, attempts, errors


def _speaker_context_env() -> dict[str, str]:
    """Load the fixed runtime CPA env without echoing or shell-evaluating it."""

    env = dict(os.environ)
    if env.get("CPA_BASE_URL") and env.get("CPA_API_KEY"):
        return env
    env_path = Path(os.environ.get("AUTOSLICE_CPA_ENV", "/opt/bilive/autoslice/cpa.env"))
    if not env_path.is_file():
        return env
    mode = stat.S_IMODE(env_path.stat().st_mode)
    if mode & 0o077:
        raise SpeakerFinalizationError(f"CPA env permissions are too broad: {oct(mode)}")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"CPA_[A-Z0-9_]+", key):
            continue
        values = shlex.split(raw_value, comments=True, posix=True)
        if len(values) != 1:
            raise SpeakerFinalizationError(f"invalid {key} entry in CPA env")
        env.setdefault(key, values[0])
    return env


def _call_context_via_cpa(prompt: str, *, repo_root: Path, work_dir: Path) -> str:
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", dir=work_dir, delete=False) as handle:
        prompt_path = Path(handle.name)
        handle.write(prompt)
    output_path = prompt_path.with_suffix(".out")
    try:
        completed = subprocess.run(
            [
                "bash",
                str(repo_root / "scripts" / "llm_via_cpa.sh"),
                str(prompt_path),
                str(output_path),
                "gpt-5.6-sol gpt-5.5 gpt-5.4",
                "medium",
            ],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=_speaker_context_env(),
        )
        if completed.returncode != 0 or not output_path.is_file():
            raise SpeakerFinalizationError(
                "speaker context judge failed: " + (completed.stderr or completed.stdout)[-800:]
            )
        return output_path.read_text(encoding="utf-8")
    finally:
        prompt_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)


def _extract_cue_wavs(media_path: Path, cues: Sequence[TextCue], work_dir: Path) -> tuple[object, int, list[Path]]:
    try:
        import soundfile as sf  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - production ML runtime
        raise SpeakerFinalizationError(f"soundfile unavailable: {exc}") from exc
    wav_path = work_dir / "clip-16k-mono.wav"
    completed = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media_path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if completed.returncode != 0 or not wav_path.is_file():
        raise SpeakerFinalizationError("failed to extract 16k mono audio: " + completed.stderr[-800:])
    audio, sample_rate = sf.read(str(wav_path))
    cue_dir = work_dir / "cue-wavs"
    cue_dir.mkdir(parents=True, exist_ok=True)
    cue_paths: list[Path] = []
    audio_end_ms = int(len(audio) * 1000 / sample_rate)
    for index, cue in enumerate(cues):
        start_ms, end_ms = _ms(cue.start), _ms(cue.end)
        lower = max(start_ms - 150, _ms(cues[index - 1].end) if index else 0)
        upper = min(end_ms + 150, _ms(cues[index + 1].start) if index + 1 < len(cues) else audio_end_ms)
        if upper <= lower:
            lower, upper = start_ms, end_ms
        cue_path = cue_dir / f"cue-{index + 1:04d}.wav"
        sf.write(
            str(cue_path),
            audio[int(lower * sample_rate / 1000): int(upper * sample_rate / 1000)],
            sample_rate,
        )
        cue_paths.append(cue_path)
    return audio, sample_rate, cue_paths


def _load_source_session_anchor_samples(
    manifest_path: Path,
    *,
    target_media_path: Path,
    profile_path: Path,
    references: Sequence[Mapping[str, object]],
    model_tree_sha256: str,
    host_seed_min: float,
    work_dir: Path,
    similarity: Callable[[Path, Path], float],
) -> tuple[list[Path], dict[str, object]]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpeakerFinalizationError(f"cannot read source-session anchor manifest: {exc}") from exc
    reference_hashes = {
        str(reference["id"]): str(reference["sha256"]) for reference in references
    }
    target_media_sha256 = sha256_file(target_media_path)
    document = _validate_source_session_anchor_document(
        raw,
        target_media_sha256=target_media_sha256,
        target_media_path=target_media_path,
        profile_sha256=sha256_file(profile_path),
        model_tree_sha256=model_tree_sha256,
        reference_hashes=reference_hashes,
        host_seed_min=host_seed_min,
    )
    provenance = _validate_source_session_provenance(
        document,
        target_media_path=target_media_path,
        target_media_sha256=target_media_sha256,
    )
    donor = document.get("donor")
    donor_media: Path | None = None
    donor_srt: Path | None = None
    donor_cues: list[TextCue] = []
    donor_wavs: list[Path] = []
    if isinstance(donor, Mapping):
        donor_media = Path(str(donor["media_path"])).resolve(strict=True)
        donor_srt = Path(str(donor["text_srt_path"])).resolve(strict=True)
        if sha256_file(donor_media) != donor["media_sha256"]:
            raise SpeakerFinalizationError("source-session donor media hash drift")
        if sha256_file(donor_srt) != donor["text_srt_sha256"]:
            raise SpeakerFinalizationError("source-session donor text SRT hash drift")
        donor_cues = parse_srt(donor_srt)
        donor_work_dir = work_dir / "source-session-donor"
        donor_work_dir.mkdir(parents=True, exist_ok=True)
        _audio, _sample_rate, donor_wavs = _extract_cue_wavs(
            donor_media, donor_cues, donor_work_dir
        )
    source_recording = Path(str(document["source_recording"])).resolve(strict=True)
    segment_work_dir = work_dir / "source-session-recording"
    segment_work_dir.mkdir(parents=True, exist_ok=True)
    anchor_paths: list[Path] = []
    evidence_rows: list[dict[str, object]] = []
    references_by_id = {str(reference["id"]): reference for reference in references}
    anchors = document["anchors"]
    assert isinstance(anchors, list)
    for position, raw_anchor in enumerate(anchors, start=1):
        assert isinstance(raw_anchor, Mapping)
        anchor_type = str(raw_anchor.get("anchor_type") or "donor_cue")
        if anchor_type == "donor_cue":
            cue_index = int(raw_anchor["source_cue"])
            if cue_index > len(donor_cues):
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} source_cue exceeds donor SRT"
                )
            cue = donor_cues[cue_index - 1]
            expected_cue = (
                str(raw_anchor["start"]),
                str(raw_anchor["end"]),
                str(raw_anchor["text"]),
            )
            if (cue.start, cue.end, cue.text) != expected_cue:
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} donor cue drift"
                )
            sample_path = donor_wavs[cue_index - 1]
            evidence_source: dict[str, object] = {
                "anchor_type": anchor_type,
                "source_cue": cue_index,
                "start": cue.start,
                "end": cue.end,
            }
        else:
            start_ms = int(raw_anchor["source_start_ms"])
            end_ms = int(raw_anchor["source_end_ms"])
            sample_path = segment_work_dir / f"anchor-{position:04d}.wav"
            try:
                _extract_checkpoint(
                    source_recording,
                    start_ms=start_ms,
                    expected_duration_ms=end_ms - start_ms,
                    output_path=sample_path,
                )
            except Exception as exc:
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} source extraction failed: {exc}"
                ) from exc
            evidence_source = {
                "anchor_type": anchor_type,
                "source_start_ms": start_ms,
                "source_end_ms": end_ms,
            }
        sample_sha256 = sha256_file(sample_path)
        if sample_sha256 != raw_anchor["sample_sha256"]:
            raise SpeakerFinalizationError(f"source-session anchor {position} sample hash drift")
        actual_scores = {
            reference_id: float(
                similarity(Path(str(references_by_id[reference_id]["path"])), sample_path)
            )
            for reference_id in sorted(references_by_id)
        }
        expected_scores = raw_anchor["reference_scores"]
        assert isinstance(expected_scores, Mapping)
        for reference_id, actual_score in actual_scores.items():
            if abs(actual_score - float(expected_scores[reference_id])) > 1e-5:
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} runtime score drift for {reference_id}"
                )
        enroll_median = float(statistics.median(actual_scores.values()))
        if enroll_median < host_seed_min:
            raise SpeakerFinalizationError(
                f"source-session anchor {position} fails the unchanged host seed gate at runtime"
            )
        anchor_paths.append(sample_path)
        evidence_rows.append(
            {
                **evidence_source,
                "text": str(raw_anchor["text"]),
                "sample_sha256": sample_sha256,
                "reference_scores": actual_scores,
                "enroll_median_score": enroll_median,
            }
        )
    evidence: dict[str, object] = {
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": sha256_file(manifest_path),
        "source_session_id": document["source_session_id"],
        "authority": document.get("authority"),
        "donor_candidate_id": donor["candidate_id"] if isinstance(donor, Mapping) else None,
        "donor_media_sha256": donor["media_sha256"] if isinstance(donor, Mapping) else None,
        "donor_text_srt_sha256": (
            donor["text_srt_sha256"] if isinstance(donor, Mapping) else None
        ),
        **provenance,
        "anchors": evidence_rows,
    }
    if isinstance(donor, Mapping):
        assert donor_media is not None and donor_srt is not None
        if sha256_file(donor_media) != donor["media_sha256"]:
            raise SpeakerFinalizationError("source-session donor media drifted during analysis")
        if sha256_file(donor_srt) != donor["text_srt_sha256"]:
            raise SpeakerFinalizationError("source-session donor text SRT drifted during analysis")
        if sha256_file(Path(str(provenance["donor_provenance"]))) != provenance[
            "donor_provenance_sha256"
        ]:
            raise SpeakerFinalizationError("source-session donor provenance drifted during analysis")
    if sha256_file(Path(str(provenance["canonical_target_media"]))) != provenance[
        "canonical_target_media_sha256"
    ]:
        raise SpeakerFinalizationError(
            "source-session canonical target media drifted during analysis"
        )
    if sha256_file(Path(str(provenance["target_provenance"]))) != provenance[
        "target_provenance_sha256"
    ]:
        raise SpeakerFinalizationError("source-session target provenance drifted during analysis")
    return anchor_paths, evidence


def _load_runtime(profile_path: Path, reference_dir: Path, model_dir: Path):
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    model_info, expected_references = _validate_profile(profile)
    actual_model_hash = _sha256_directory(model_dir)
    if actual_model_hash != model_info["tree_sha256"]:
        raise SpeakerFinalizationError("CAM++ model tree hash does not match profile")
    references = []
    for expected in expected_references:
        path = (reference_dir / expected["filename"]).resolve(strict=True)
        if sha256_file(path) != expected["sha256"]:
            raise SpeakerFinalizationError(f"voiceprint reference hash mismatch: {expected['id']}")
        references.append({**expected, "path": path})
    return profile, references, actual_model_hash, _load_campplus_pipeline(model_dir)


def _assert_runtime_assets_stable(
    *,
    model_dir: Path,
    model_tree_sha256: str,
    references: Sequence[Mapping[str, object]],
) -> None:
    if _sha256_directory(model_dir) != model_tree_sha256:
        raise SpeakerFinalizationError("CAM++ model tree drifted during speaker analysis")
    for reference in references:
        path = Path(str(reference["path"]))
        if sha256_file(path) != reference["sha256"]:
            raise SpeakerFinalizationError(
                f"voiceprint reference drifted during speaker analysis: {reference['id']}"
            )


def _run_campplus_analysis(
    *,
    media_path: Path,
    cues: Sequence[TextCue],
    profile_path: Path,
    reference_dir: Path,
    model_dir: Path,
    work_dir: Path,
    context_call: Callable[[str], str] | None,
    reviewed_context_votes: Mapping[int, str] | None = None,
    source_session_anchor_path: Path | None = None,
) -> dict[str, object]:
    work_dir.mkdir(parents=True, exist_ok=True)
    profile, references, model_hash, verifier = _load_runtime(profile_path, reference_dir, model_dir)
    policy = _policy(profile)
    _audio, _sample_rate, cue_paths = _extract_cue_wavs(media_path, cues, work_dir)
    similarity = _build_embedding_similarity(
        verifier=verifier, model_hash=model_hash, work_dir=work_dir
    )

    seed_scores = [
        float(statistics.median(similarity(Path(reference["path"]), cue_path) for reference in references))
        for cue_path in cue_paths
    ]
    anchor_count = int(policy["host_session_anchor_count"])
    clip_host_indices = [
        index for index in sorted(range(len(cues)), key=seed_scores.__getitem__, reverse=True)
        if seed_scores[index] >= float(policy["host_session_seed_min"])
    ][:anchor_count]
    source_session_evidence: dict[str, object] | None = None
    if source_session_anchor_path is not None:
        host_prints, source_session_evidence = _load_source_session_anchor_samples(
            source_session_anchor_path.resolve(strict=True),
            target_media_path=media_path,
            profile_path=profile_path,
            references=references,
            model_tree_sha256=model_hash,
            host_seed_min=float(policy["host_session_seed_min"]),
            work_dir=work_dir,
            similarity=similarity,
        )
        host_indices: list[int] = []
        host_anchor_scope = "source_session"
    else:
        host_indices = clip_host_indices
        if len(host_indices) < 2:
            raise SpeakerFinalizationError(
                f"not enough {CHANNEL_PROFILE.prompt_name} clip anchors: {host_indices}"
            )
        host_prints = [cue_paths[index] for index in host_indices]
        host_anchor_scope = "clip"

    host_bank_score_cache: dict[int, float] = {}

    def host_bank_similarity(index: int) -> float:
        # A trusted host bank contains complementary speaking styles.  A cue
        # that strongly matches any unchanged-high-gate host anchor is host-
        # explained; averaging would dilute the one matching style and create
        # false guest evidence (notably excited/farewell delivery).
        if index in host_bank_score_cache:
            return host_bank_score_cache[index]
        values = [similarity(host, cue_paths[index]) for host in host_prints]
        if host_anchor_scope == "source_session":
            score = float(max(values))
            host_bank_score_cache[index] = score
            return score
        # Preserve the established clip-local classifier exactly.  The
        # any-anchor veto is authorized only by an explicit, hash-bound source
        # session bank; it must not silently relax every historical clip.
        score = float(statistics.mean(values))
        host_bank_score_cache[index] = score
        return score

    guest_candidates: list[int] = []
    for index in sorted(range(len(cues)), key=seed_scores.__getitem__):
        if seed_scores[index] > float(policy["guest_seed_max"]) or index in host_indices:
            continue
        if _ms(cues[index].end) - _ms(cues[index].start) < int(policy["guest_min_duration_ms"]):
            continue
        session_similarity = host_bank_similarity(index)
        if session_similarity >= float(policy["guest_session_similarity_max"]):
            continue
        guest_candidates.append(index)
        if len(guest_candidates) >= 8:
            break

    if len(guest_candidates) < 2:
        median_seed = statistics.median(seed_scores)
        raw_long_low = [
            index for index, cue in enumerate(cues)
            if _ms(cue.end) - _ms(cue.start) >= int(policy["guest_min_duration_ms"])
            and seed_scores[index] < float(policy["guest_seed_max"])
        ]
        host_explained_low = (
            [
                index
                for index in raw_long_low
                if host_bank_similarity(index)
                >= float(policy["guest_session_similarity_max"])
            ]
            if host_anchor_scope == "source_session"
            else []
        )
        unexplained_long_low = [
            index for index in raw_long_low if index not in host_explained_low
        ]
        if len(guest_candidates) == 1 and unexplained_long_low == guest_candidates:
            singleton_index = guest_candidates[0]
            singleton = _resolve_singleton_outlier(
                cues=cues,
                singleton_index=singleton_index,
                seed_scores=seed_scores,
                host_bank_scores={
                    index: host_bank_similarity(index) for index in range(len(cues))
                },
                clip_host_indices=clip_host_indices,
                policy=policy,
                cue_audio_sha256=[sha256_file(path) for path in cue_paths],
                context_call=context_call,
                reviewed_context_votes=reviewed_context_votes,
            )
            _assert_runtime_assets_stable(
                model_dir=model_dir,
                model_tree_sha256=model_hash,
                references=references,
            )
            return {
                **singleton,
                "host_anchor_scope": host_anchor_scope,
                "source_session_anchor": source_session_evidence,
                "policy": policy,
                "model_tree_sha256": model_hash,
                "reference_hashes": {
                    str(reference["id"]): str(reference["sha256"])
                    for reference in references
                },
                "host_anchor_cues": [index + 1 for index in host_indices],
                "clip_host_anchor_candidates": [index + 1 for index in clip_host_indices],
                "host_explained_low_cues": [index + 1 for index in host_explained_low],
                "guest_anchor_groups": [],
                "threshold": None,
            }
        if median_seed < float(policy["single_host_median_seed_min"]) or unexplained_long_low:
            raise SpeakerFinalizationError(
                f"guest evidence exists but purified guest anchors are insufficient: {guest_candidates}"
            )
        _assert_runtime_assets_stable(
            model_dir=model_dir,
            model_tree_sha256=model_hash,
            references=references,
        )
        return {
            "mode": "single_host",
            "multi_speaker_detected": False,
            "host_anchor_scope": host_anchor_scope,
            "source_session_anchor": source_session_evidence,
            "policy": policy,
            "model_tree_sha256": model_hash,
            "reference_hashes": {str(reference["id"]): str(reference["sha256"]) for reference in references},
            "host_anchor_cues": [index + 1 for index in host_indices],
            "clip_host_anchor_candidates": [index + 1 for index in clip_host_indices],
            "host_explained_low_cues": [index + 1 for index in host_explained_low],
            "guest_anchor_groups": [],
            "threshold": None,
            "decisions": [
                {
                    "source_index": index + 1,
                    "speaker": HOST_SPEAKER,
                    "decision_source": "campp_single_host",
                    "seed_score": round(seed_scores[index], 8),
                    "margin": None,
                }
                for index in range(len(cues))
            ],
        }

    seed_guest = guest_candidates[0]
    group_a, group_b = [seed_guest], []
    for index in guest_candidates[1:]:
        target = group_a if similarity(cue_paths[seed_guest], cue_paths[index]) >= float(policy["guest_cluster_similarity_min"]) else group_b
        target.append(index)
    guest_groups = [group[:4] for group in (group_a, group_b) if group]

    host_scores: list[float] = []
    guest_scores: list[float] = []
    margins: list[float] = []
    for index, cue_path in enumerate(cue_paths):
        host_values = [similarity(path, cue_path) for path in host_prints if path != cue_path]
        host_score = statistics.mean(host_values or [1.0])
        guest_score = max(
            statistics.mean(
                [similarity(cue_paths[guest_index], cue_path) for guest_index in group if guest_index != index]
                or [0.0]
            )
            for group in guest_groups
        )
        host_scores.append(float(host_score))
        guest_scores.append(float(guest_score))
        margins.append(float(host_score - guest_score))
    low_center, high_center, threshold = _two_means(margins)
    band = float(policy["ambiguity_band"])
    short_ms = int(policy["short_cue_ms"])
    labels: list[str | None] = []
    ambiguous: list[int] = []
    for index, (cue, margin) in enumerate(zip(cues, margins, strict=True)):
        short = _ms(cue.end) - _ms(cue.start) < short_ms
        if short or abs(margin - threshold) < band:
            labels.append(None)
            ambiguous.append(index)
        else:
            labels.append(HOST_SPEAKER if margin >= threshold else GUEST_SPEAKER)

    reviewed_votes = {
        index: speaker
        for index, speaker in (reviewed_context_votes or {}).items()
        if index in ambiguous and speaker in SPEAKERS
    }
    context_vote_rows, context_attempts, context_errors = _whole_clip_context_votes(
        cues,
        labels,
        ambiguous,
        context_call,
        initial_speakers=reviewed_votes,
    )
    votes = {
        index: str(row["speaker"]) for index, row in context_vote_rows.items()
    }
    unresolved_context = [index for index in ambiguous if index not in votes]
    resolved, sources = resolve_ambiguous_labels(labels, margins, threshold, votes)
    for index in reviewed_votes:
        if index in ambiguous:
            sources[index] = "accepted_context_baseline"

    # Smooth only acoustically ambiguous one-cue islands; never override a
    # whole-clip context judgement or confident audio label.
    for index in range(1, len(resolved) - 1):
        if (
            resolved[index - 1] == resolved[index + 1] != resolved[index]
            and sources[index] in {"neighbour_context_fallback", "acoustic_threshold_fallback"}
            and abs(margins[index] - threshold) < band
        ):
            resolved[index] = resolved[index - 1]
            sources[index] = "ambiguous_island_smoothing"

    _assert_runtime_assets_stable(
        model_dir=model_dir,
        model_tree_sha256=model_hash,
        references=references,
    )
    return {
        "mode": "multi_speaker",
        "multi_speaker_detected": True,
        "host_anchor_scope": host_anchor_scope,
        "source_session_anchor": source_session_evidence,
        "policy": policy,
        "model_tree_sha256": model_hash,
        "reference_hashes": {str(reference["id"]): str(reference["sha256"]) for reference in references},
        "host_anchor_cues": [index + 1 for index in host_indices],
        "clip_host_anchor_candidates": [index + 1 for index in clip_host_indices],
        "guest_anchor_groups": [[index + 1 for index in group] for group in guest_groups],
        "cluster_centers": {"guest": low_center, PROFILE_ID: high_center},
        "threshold": threshold,
        "context_attempts": context_attempts,
        "context_errors": context_errors,
        "context_votes": {str(index + 1): speaker for index, speaker in votes.items()},
        "reviewed_context_votes": {
            str(index + 1): speaker for index, speaker in reviewed_votes.items()
        },
        "context_required_cues": [index + 1 for index in ambiguous],
        "context_unresolved_cues": [index + 1 for index in unresolved_context],
        "decisions": [
            {
                "source_index": index + 1,
                "speaker": resolved[index],
                "decision_source": sources[index],
                "seed_score": round(seed_scores[index], 8),
                "host_score": round(host_scores[index], 8),
                "guest_score": round(guest_scores[index], 8),
                "margin": round(margins[index], 8),
            }
            for index in range(len(cues))
        ],
    }


def finalize_speaker_subtitles(
    *,
    media_path: Path,
    text_srt_path: Path,
    profile_path: Path,
    reference_dir: Path,
    model_dir: Path,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
    work_dir: Path,
    candidate_id: str | None = None,
    override_path: Path | None = None,
    source_session_anchor_path: Path | None = None,
    mixed_overlap_evidence_path: Path | None = None,
    analyzer: Callable[..., dict[str, object]] = _run_campplus_analysis,
    context_call: Callable[[str], str] | None = None,
) -> dict[str, object]:
    media_path = media_path.resolve(strict=True)
    text_srt_path = text_srt_path.resolve(strict=True)
    profile_path = profile_path.resolve(strict=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    profile_snapshot, profile_sha256 = _snapshot_bound_input(
        profile_path, work_dir / "bound-inputs" / "voiceprint-profile.json"
    )
    source_session_anchor_original: Path | None = None
    source_session_anchor_snapshot: Path | None = None
    source_session_anchor_sha256: str | None = None
    if source_session_anchor_path is not None:
        source_session_anchor_original = source_session_anchor_path.resolve(strict=True)
        source_session_anchor_snapshot, source_session_anchor_sha256 = _snapshot_bound_input(
            source_session_anchor_original,
            work_dir / "bound-inputs" / "source-session-anchors.json",
        )
    mixed_overlap_evidence_original: Path | None = None
    mixed_overlap_evidence_snapshot: Path | None = None
    mixed_overlap_evidence_sha256: str | None = None
    if mixed_overlap_evidence_path is not None:
        mixed_overlap_evidence_original = mixed_overlap_evidence_path.resolve(strict=True)
        mixed_overlap_evidence_snapshot, mixed_overlap_evidence_sha256 = _snapshot_bound_input(
            mixed_overlap_evidence_original,
            work_dir / "bound-inputs" / "mixed-overlap-evidence.json",
        )
    cues = parse_srt(text_srt_path)
    override_document: dict[str, object] | None = None
    reviewed_votes: dict[int, str] = {}
    expected_automatic = ""
    if override_path is not None:
        loaded = json.loads(override_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise SpeakerFinalizationError("speaker override document must be an object")
        override_document = loaded
        expected_text = str(override_document.get("text_final_srt_sha256") or "")
        expected_automatic = str(override_document.get("source_srt_sha256") or "")
        expected_media = str(override_document.get("source_media_sha256") or "")
        actual_text = sha256_file(text_srt_path)
        actual_media = sha256_file(media_path)
        if not expected_media:
            raise SpeakerFinalizationError("speaker override is missing source_media_sha256")
        if expected_media != actual_media:
            raise SpeakerFinalizationError(
                f"speaker override media hash mismatch: expected {expected_media!r}, got {actual_media!r}"
            )
        if expected_text and expected_text != actual_text:
            raise SpeakerFinalizationError(
                f"speaker override text-final hash mismatch: expected {expected_text!r}, got {actual_text!r}"
            )
        if not str(candidate_id or "").strip():
            raise SpeakerFinalizationError(
                "candidate_id is required when a speaker override is present"
            )
        try:
            validate_bound_speaker_override_document(
                override_path,
                candidate_id=str(candidate_id),
                expected_source_media_sha256=actual_media,
                expected_text_final_srt_sha256=actual_text,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SpeakerFinalizationError(
                f"speaker override authority binding failed: {exc}"
            ) from exc
        reviewed_votes = _reviewed_context_votes(override_document, cue_count=len(cues))
    mixed_overlap_document: Mapping[str, object] | None = None
    if mixed_overlap_evidence_snapshot is not None:
        try:
            mixed_overlap_document = json.loads(
                mixed_overlap_evidence_snapshot.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise SpeakerFinalizationError(f"mixed/overlap evidence is invalid JSON: {exc}") from exc
        media_sha256 = sha256_file(media_path)
        text_sha256 = sha256_file(text_srt_path)
        mixed_rows = validate_mixed_overlap_evidence_document(
            mixed_overlap_document,
            expected_media_sha256=media_sha256,
            expected_text_sha256=text_sha256,
            cues=cues,
            expected_audio_root=mixed_overlap_evidence_original.parent,
        )
        override_sources = {
            int(item.get("source_cue", 0))
            for item in ((override_document or {}).get("overrides") or [])
            if isinstance(item, Mapping)
        }
        remaining_rows = [
            row for row in mixed_rows if int(row["source_cue"]) not in override_sources
        ]
        if remaining_rows:
            if sha256_file(mixed_overlap_evidence_original) != mixed_overlap_evidence_sha256:
                raise SpeakerFinalizationError("mixed/overlap evidence drifted during validation")
            output_srt_path.unlink(missing_ok=True)
            output_ass_path.unlink(missing_ok=True)
            reason_codes = sorted(
                {
                    str(reason)
                    for row in remaining_rows
                    for reason in row.get("reason_codes", [])
                }
            )
            unresolved = [int(row["source_cue"]) for row in remaining_rows]
            review_manifest: dict[str, object] = {
                "schema_version": SPEAKER_FINALIZATION_SCHEMA,
                "status": "SPEAKER_REVIEW_REQUIRED",
                "production_ready": False,
                "reason_code": "SPEAKER_REVIEW_REQUIRED",
                "reason": "mixed/overlap speaker evidence requires review: " + ",".join(reason_codes),
                "stage_order": "text_final_then_speaker_then_ass_then_burn",
                "source_media": str(media_path),
                "source_media_sha256": media_sha256,
                "text_final_srt": str(text_srt_path),
                "text_final_srt_sha256": text_sha256,
                "profile": str(profile_path.resolve()),
                "profile_sha256": profile_sha256,
                "speaker_override": str(override_path.resolve()) if override_path is not None else None,
                "speaker_override_sha256": sha256_file(override_path) if override_path is not None else None,
                "source_session_anchor_manifest": (
                    str(source_session_anchor_original)
                    if source_session_anchor_original is not None
                    else None
                ),
                "source_session_anchor_manifest_sha256": source_session_anchor_sha256,
                "mixed_overlap_evidence": str(mixed_overlap_evidence_original),
                "mixed_overlap_evidence_sha256": mixed_overlap_evidence_sha256,
                "source_cue_count": len(cues),
                "context_unresolved_cues": unresolved,
                "review_reason_codes": reason_codes,
                "review_required_cues": remaining_rows,
                "analysis": {
                    "mode": "provider_mixed_overlap_gate",
                    "provider": mixed_overlap_document.get("provider"),
                },
            }
            validate_speaker_review_manifest_document(
                review_manifest,
                expected_media_sha256=media_sha256,
                expected_text_sha256=text_sha256,
                cues=cues,
            )
            atomic_write_text(
                output_manifest_path,
                json.dumps(review_manifest, ensure_ascii=False, indent=2) + "\n",
            )
            return review_manifest
    analysis = analyzer(
        media_path=media_path,
        cues=cues,
        profile_path=profile_snapshot,
        reference_dir=reference_dir,
        model_dir=model_dir,
        work_dir=work_dir,
        context_call=context_call,
        reviewed_context_votes=reviewed_votes,
        source_session_anchor_path=source_session_anchor_snapshot,
    )
    if sha256_file(profile_path) != profile_sha256:
        raise SpeakerFinalizationError("voiceprint profile drifted during speaker analysis")
    if (
        source_session_anchor_original is not None
        and sha256_file(source_session_anchor_original) != source_session_anchor_sha256
    ):
        raise SpeakerFinalizationError(
            "source-session anchor manifest drifted during speaker analysis"
        )
    if (
        mixed_overlap_evidence_original is not None
        and sha256_file(mixed_overlap_evidence_original) != mixed_overlap_evidence_sha256
    ):
        raise SpeakerFinalizationError(
            "mixed/overlap evidence drifted during speaker analysis"
        )
    if mixed_overlap_document is not None:
        # Recheck the actual extracted review audio after analyzer/override
        # work so a concurrent byte change cannot be blessed by a prior hash.
        validate_mixed_overlap_evidence_document(
            mixed_overlap_document,
            expected_media_sha256=sha256_file(media_path),
            expected_text_sha256=sha256_file(text_srt_path),
            cues=cues,
            expected_audio_root=mixed_overlap_evidence_original.parent,
        )
    decisions = analysis.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != len(cues):
        raise SpeakerFinalizationError("speaker analyzer returned incomplete decisions")
    automatic: list[Cue] = []
    for index, (text_cue, decision) in enumerate(zip(cues, decisions, strict=True), start=1):
        if not isinstance(decision, Mapping) or decision.get("speaker") not in SPEAKERS:
            raise SpeakerFinalizationError(f"speaker decision {index} is invalid")
        automatic.append(
            Cue(
                source_index=index,
                start=text_cue.start,
                end=text_cue.end,
                speaker=str(decision["speaker"]),
                text=text_cue.text,
                decision_source=str(decision.get("decision_source") or "campp_audio"),
                note=(f"margin={decision.get('margin')}" if decision.get("margin") is not None else None),
            )
        )
    automatic_srt = work_dir / "automatic-labelled.srt"
    write_srt(automatic, automatic_srt)
    final_cues = automatic
    if override_document is not None:
        actual_automatic = sha256_file(automatic_srt)
        if expected_automatic and expected_automatic != actual_automatic:
            raise SpeakerFinalizationError(
                f"speaker override source hash mismatch: expected {expected_automatic!r}, got {actual_automatic!r}"
            )
        final_cues = apply_overrides(automatic, override_document)
    unresolved_raw = analysis.get("context_unresolved_cues") or []
    if not isinstance(unresolved_raw, list) or any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in unresolved_raw
    ):
        raise SpeakerFinalizationError(
            "speaker analyzer returned invalid unresolved-context evidence"
        )
    unresolved = set(unresolved_raw)
    override_sources = {
        int(item.get("source_cue", 0))
        for item in ((override_document or {}).get("overrides") or [])
        if isinstance(item, Mapping)
    }
    remaining_unresolved = sorted(unresolved - override_sources)
    if remaining_unresolved:
        if analysis.get("review_required") is True:
            output_srt_path.unlink(missing_ok=True)
            output_ass_path.unlink(missing_ok=True)
            reason_codes_raw = analysis.get("review_reason_codes") or []
            if not isinstance(reason_codes_raw, list) or any(
                not isinstance(value, str) or not value.strip() for value in reason_codes_raw
            ):
                raise SpeakerFinalizationError("speaker analyzer returned invalid review reason codes")
            reason_codes = list(dict.fromkeys(reason_codes_raw))
            review_rows = analysis.get("review_required_cues")
            if review_rows is None:
                review_rows = analysis.get("singleton_evidence") or []
            media_sha256 = sha256_file(media_path)
            text_sha256 = sha256_file(text_srt_path)
            review_manifest: dict[str, object] = {
                "schema_version": SPEAKER_FINALIZATION_SCHEMA,
                "status": "SPEAKER_REVIEW_REQUIRED",
                "production_ready": False,
                "reason_code": "SPEAKER_REVIEW_REQUIRED",
                "reason": "speaker evidence requires review: "
                + ",".join(reason_codes or ["UNRESOLVED_SPEAKER_EVIDENCE"]),
                "stage_order": "text_final_then_speaker_then_ass_then_burn",
                "source_media": str(media_path),
                "source_media_sha256": media_sha256,
                "text_final_srt": str(text_srt_path),
                "text_final_srt_sha256": text_sha256,
                "profile": str(profile_path.resolve()),
                "profile_sha256": profile_sha256,
                "automatic_labelled_srt": str(automatic_srt.resolve()),
                "automatic_labelled_srt_sha256": sha256_file(automatic_srt),
                "speaker_override": (
                    str(override_path.resolve()) if override_path is not None else None
                ),
                "speaker_override_sha256": (
                    sha256_file(override_path) if override_path is not None else None
                ),
                "source_session_anchor_manifest": (
                    str(source_session_anchor_original)
                    if source_session_anchor_original is not None
                    else None
                ),
                "source_session_anchor_manifest_sha256": source_session_anchor_sha256,
                "mixed_overlap_evidence": (
                    str(mixed_overlap_evidence_original)
                    if mixed_overlap_evidence_original is not None
                    else None
                ),
                "mixed_overlap_evidence_sha256": mixed_overlap_evidence_sha256,
                "host_anchor_scope": analysis.get("host_anchor_scope", "clip"),
                "source_cue_count": len(cues),
                "context_unresolved_cues": remaining_unresolved,
                "review_reason_codes": reason_codes,
                "review_required_cues": review_rows,
                "analysis": analysis,
            }
            validate_speaker_review_manifest_document(
                review_manifest,
                expected_media_sha256=media_sha256,
                expected_text_sha256=text_sha256,
                cues=cues,
            )
            atomic_write_text(
                output_manifest_path,
                json.dumps(review_manifest, ensure_ascii=False, indent=2) + "\n",
            )
            return review_manifest
        raise SpeakerFinalizationError(
            "whole-clip context did not resolve ambiguous speaker cues: "
            + ",".join(str(value) for value in remaining_unresolved)
        )
    write_srt(final_cues, output_srt_path)
    write_ass(final_cues, output_ass_path, show_speaker_labels=False)
    manifest: dict[str, object] = {
        "schema_version": SPEAKER_FINALIZATION_SCHEMA,
        "status": "READY",
        "production_ready": True,
        "stage_order": "text_final_then_speaker_then_ass_then_burn",
        "source_media": str(media_path),
        "source_media_sha256": sha256_file(media_path),
        "text_final_srt": str(text_srt_path),
        "text_final_srt_sha256": sha256_file(text_srt_path),
        "profile": str(profile_path.resolve()),
        "profile_sha256": profile_sha256,
        "automatic_labelled_srt_sha256": sha256_file(automatic_srt),
        "speaker_override": str(override_path.resolve()) if override_path is not None else None,
        "speaker_override_sha256": sha256_file(override_path) if override_path is not None else None,
        "source_session_anchor_manifest": (
            str(source_session_anchor_original)
            if source_session_anchor_original is not None
            else None
        ),
        "source_session_anchor_manifest_sha256": source_session_anchor_sha256,
        "mixed_overlap_evidence": (
            str(mixed_overlap_evidence_original)
            if mixed_overlap_evidence_original is not None
            else None
        ),
        "mixed_overlap_evidence_sha256": mixed_overlap_evidence_sha256,
        "host_anchor_scope": analysis.get("host_anchor_scope", "clip"),
        "source_session_id": (
            (analysis.get("source_session_anchor") or {}).get("source_session_id")
            if isinstance(analysis.get("source_session_anchor"), Mapping)
            else None
        ),
        "output_review_srt": str(output_srt_path.resolve()),
        "output_review_srt_sha256": sha256_file(output_srt_path),
        "output_ass": str(output_ass_path.resolve()),
        "output_ass_sha256": sha256_file(output_ass_path),
        "visible_speaker_prefixes": False,
        "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID,
        "speaker_taxonomy": "binary_visual_host_vs_guest",
        "host_identity_aliases": list(CHANNEL_PROFILE.speaker_identity_aliases),
        "source_cue_count": len(cues),
        "output_cue_count": len(final_cues),
        "reviewed_output_cue_count": sum(cue.decision_source.startswith("reviewed_") for cue in final_cues),
        "accepted_context_output_cue_count": sum(
            cue.decision_source == "accepted_context_baseline" for cue in final_cues
        ),
        "overlap_output_cue_count": sum(cue.placement == "above" for cue in final_cues),
        "analysis": analysis,
        "final_decisions": [asdict(cue) for cue in final_cues],
    }
    atomic_write_text(output_manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def finalize_fast_solo_subtitles(
    *,
    media_path: Path,
    text_srt_path: Path,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
    candidate_id: str,
    routing_claim_path: Path,
    verified_route: object,
    fresh_derivation: Mapping[str, object],
) -> dict[str, object]:
    """Render an all-host result from an already verified session authority.

    This function intentionally has no profile, reference, model, analyzer, or
    context arguments.  The opaque ``VerifiedFastSoloRoute`` value is produced
    only by the current-state verifier in ``speaker_session_router``.
    """

    from src.autoslice.speaker_session_router import VerifiedFastSoloRoute

    if not isinstance(verified_route, VerifiedFastSoloRoute):
        raise SpeakerFinalizationError("FAST_SOLO renderer requires a verified route")
    if verified_route.candidate_id != candidate_id:
        raise SpeakerFinalizationError("FAST_SOLO candidate authority mismatch")
    media_path = media_path.resolve(strict=True)
    text_srt_path = text_srt_path.resolve(strict=True)
    routing_claim_path = routing_claim_path.resolve(strict=True)
    source_media_sha256 = sha256_file(media_path)
    text_final_srt_sha256 = sha256_file(text_srt_path)
    expected_derivation_keys = {
        "schema_version",
        "method",
        "cache_reused",
        "source_path",
        "source_sha256",
        "absolute_source_start_ms",
        "absolute_source_end_ms",
        "expected_duration_ms",
        "actual_duration_ms",
        "output_path",
        "output_sha256",
    }
    if not isinstance(fresh_derivation, Mapping) or set(fresh_derivation) != expected_derivation_keys:
        raise SpeakerFinalizationError("FAST_SOLO fresh derivation schema is incomplete")
    if (
        fresh_derivation.get("schema_version") != FAST_FRESH_DERIVATION_SCHEMA
        or fresh_derivation.get("method")
        != "canonical_accurate_recut_direct_from_claimed_segment"
        or fresh_derivation.get("cache_reused") is not False
    ):
        raise SpeakerFinalizationError("FAST_SOLO fresh derivation method is invalid")
    derivation_source = Path(str(fresh_derivation.get("source_path") or "")).resolve(
        strict=True
    )
    derivation_start = fresh_derivation.get("absolute_source_start_ms")
    derivation_end = fresh_derivation.get("absolute_source_end_ms")
    expected_duration = fresh_derivation.get("expected_duration_ms")
    actual_duration = fresh_derivation.get("actual_duration_ms")
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (
            derivation_start,
            derivation_end,
            expected_duration,
            actual_duration,
        )
    ):
        raise SpeakerFinalizationError("FAST_SOLO fresh derivation timing is invalid")
    if (
        str(derivation_source) != verified_route.segment_path
        or fresh_derivation.get("source_sha256")
        != verified_route.segment_binding_sha256
        or sha256_file(derivation_source) != verified_route.segment_binding_sha256
        or not (
            verified_route.start_ms
            <= derivation_start
            < derivation_end
            <= verified_route.end_ms
        )
    ):
        raise SpeakerFinalizationError("FAST_SOLO fresh derivation source binding mismatch")
    derived_duration = derivation_end - derivation_start
    if (
        expected_duration != derived_duration
        or actual_duration <= 0
        or str(media_path) != fresh_derivation.get("output_path")
        or source_media_sha256 != fresh_derivation.get("output_sha256")
    ):
        raise SpeakerFinalizationError("FAST_SOLO fresh derivation output binding mismatch")
    if sha256_file(routing_claim_path) != verified_route.claim_sha256:
        raise SpeakerFinalizationError("FAST_SOLO routing claim drifted before render")
    cues = parse_srt(text_srt_path)
    if not cues:
        raise SpeakerFinalizationError("FAST_SOLO text-final SRT has no cues")
    final_cues = [
        Cue(
            source_index=index,
            start=cue.start,
            end=cue.end,
            speaker=HOST_SPEAKER,
            text=cue.text,
            decision_source="verified_session_fast_solo",
        )
        for index, cue in enumerate(cues, start=1)
    ]
    output_srt_path.unlink(missing_ok=True)
    output_ass_path.unlink(missing_ok=True)
    write_srt(final_cues, output_srt_path)
    write_ass(final_cues, output_ass_path, show_speaker_labels=False)
    if (
        sha256_file(routing_claim_path) != verified_route.claim_sha256
        or sha256_file(media_path) != source_media_sha256
        or sha256_file(text_srt_path) != text_final_srt_sha256
        or sha256_file(derivation_source) != verified_route.segment_binding_sha256
    ):
        output_srt_path.unlink(missing_ok=True)
        output_ass_path.unlink(missing_ok=True)
        raise SpeakerFinalizationError("FAST_SOLO authority inputs drifted during render")
    manifest: dict[str, object] = {
        "schema_version": SPEAKER_FINALIZATION_SCHEMA,
        "status": "READY",
        "production_ready": True,
        "stage_order": "text_final_then_speaker_then_ass_then_burn",
        "source_media": str(media_path),
        "source_media_sha256": source_media_sha256,
        "text_final_srt": str(text_srt_path),
        "text_final_srt_sha256": text_final_srt_sha256,
        "speaker_routing_claim": str(routing_claim_path),
        "speaker_routing_claim_sha256": verified_route.claim_sha256,
        "speaker_routing_request_sha256": verified_route.request_sha256,
        "speaker_routing_provider_evidence_sha256": (
            verified_route.provider_evidence_sha256
        ),
        "pipeline_fingerprint": verified_route.pipeline_fingerprint,
        "fresh_fast_derivation": dict(fresh_derivation),
        "output_review_srt": str(output_srt_path.resolve()),
        "output_review_srt_sha256": sha256_file(output_srt_path),
        "output_ass": str(output_ass_path.resolve()),
        "output_ass_sha256": sha256_file(output_ass_path),
        "visible_speaker_prefixes": False,
        "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID,
        "speaker_taxonomy": "binary_visual_host_vs_guest",
        "host_identity_aliases": list(CHANNEL_PROFILE.speaker_identity_aliases),
        "host_anchor_scope": "verified_session_fast_solo",
        "source_cue_count": len(cues),
        "output_cue_count": len(final_cues),
        "reviewed_output_cue_count": 0,
        "accepted_context_output_cue_count": 0,
        "overlap_output_cue_count": 0,
        "analysis": {
            "mode": "speaker_session_fast_solo_v1",
            "campp_invoked": False,
            "context_invoked": False,
        },
        "final_decisions": [asdict(cue) for cue in final_cues],
    }
    atomic_write_text(
        output_manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--text-srt", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output-srt", type=Path, required=True)
    parser.add_argument("--output-ass", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--source-session-anchors", type=Path)
    parser.add_argument("--mixed-overlap-evidence", type=Path)
    parser.add_argument("--no-context-judge", action="store_true")
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    context_call = None if args.no_context_judge else (
        lambda prompt: _call_context_via_cpa(prompt, repo_root=repo_root, work_dir=args.work_dir)
    )
    try:
        manifest = finalize_speaker_subtitles(
            media_path=args.media,
            text_srt_path=args.text_srt,
            profile_path=args.profile,
            reference_dir=args.reference_dir,
            model_dir=args.model_dir,
            output_srt_path=args.output_srt,
            output_ass_path=args.output_ass,
            output_manifest_path=args.output_manifest,
            work_dir=args.work_dir,
            candidate_id=args.candidate_id,
            override_path=args.overrides,
            source_session_anchor_path=args.source_session_anchors,
            mixed_overlap_evidence_path=args.mixed_overlap_evidence,
            context_call=context_call,
        )
    except Exception as exc:
        blocked = {
            "schema_version": SPEAKER_FINALIZATION_SCHEMA,
            "status": "BLOCKED",
            "production_ready": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "text_final_srt": str(args.text_srt),
            "text_final_srt_sha256": sha256_file(args.text_srt) if args.text_srt.is_file() else None,
        }
        atomic_write_text(args.output_manifest, json.dumps(blocked, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(blocked, ensure_ascii=False))
        return 3
    if manifest.get("status") == "SPEAKER_REVIEW_REQUIRED":
        print(json.dumps(manifest, ensure_ascii=False))
        return 4
    print(json.dumps({"status": manifest["status"], "manifest": str(args.output_manifest)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
