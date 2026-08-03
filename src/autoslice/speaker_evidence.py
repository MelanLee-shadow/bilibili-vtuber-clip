"""Hash-bound speaker review and source-session evidence validation."""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path
from typing import Mapping, Sequence

from scripts.apply_speaker_turn_overrides import sha256_file
from scripts.apply_subtitle_text_overrides import TextCue
from src.autoslice.speaker_common import (
    CHANNEL_PROFILE,
    MIXED_OVERLAP_EVIDENCE_SCHEMA,
    REVIEW_SHA256_RE,
    REVIEW_TIMESTAMP_RE,
    SHA256_RE,
    SOURCE_SESSION_ANCHOR_SCHEMA,
    SpeakerFinalizationError,
)

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
