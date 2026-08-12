"""Exclusive, candidate-scoped replay of an operator-reviewed source interval.

This authority is deliberately independent of a fresh ASR grid.  A declared
grant either validates completely and fixes the source interval, reviewed SRT,
speaker truth and frozen semantic verdict, or the candidate blocks.  There is
no nearest-cue search and no fallback to the ordinary boundary resolver.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.autoslice.boundary_endpoint_binding import bind_final_semantic_endpoint
from src.autoslice.boundary_semantic_review import (
    SEMANTIC_LLM_INDEPENDENCE_GROUP,
    build_boundary_search_scope,
    cue_grid_sha256,
    semantic_review_sha256,
)
from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues
from src.autoslice.producer_boundary import boundary_audit, syntactic_tail_audit
from src.autoslice.review_evidence import SourceCue


GRANT_SCHEMA_VERSION = "operator-reviewed-exact-source-interval.v1"
REFERENCE_SCHEMA_VERSION = "operator-reviewed-exact-source-interval-ref.v1"
DISPATCH_MODE = "reviewed_exact_source_interval_v1"
POLICY_ID = "reviewed-exact-source-interval-policy.v1"
REFERENCE_CONFIG_KEY = "operator_reviewed_exact_source_interval"
RUNTIME_CONFIG_KEY = "reviewed_exact_source_interval_authority"
CONTRADICTION_CONFIG_KEY = "operator_reviewed_next_topic_source_start"
FROZEN_CONTRACT_KEY = RUNTIME_CONFIG_KEY
BOUNDARY_AUTHORITY = "operator_reviewed_exact_source_interval_plus_frozen_reviewed_timeline"
MAX_TERMINAL_TAIL_MS = 400
_SHA256_RX = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_RECORDING_RX = re.compile(
    r"(?P<room>[0-9]+)_(?P<date>[0-9]{8})-(?P<time>[0-9]{2}-[0-9]{2}-[0-9]{2})\.mp4\Z"
)
_REPO_ROOT = Path(__file__).resolve().parents[2]


class ReviewedExactSourceIntervalError(ValueError):
    """A declared exact interval authority is missing, ambiguous or stale."""


def _fail(code: str) -> "Any":
    raise ReviewedExactSourceIntervalError(f"REVIEWED_EXACT_SOURCE_INTERVAL_{code}")


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _sha(value: object, *, code: str) -> str:
    match = _SHA256_RX.fullmatch(str(value or ""))
    if match is None:
        return _fail(code)
    return "sha256:" + match.group(1)


def _integer(value: object, *, code: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return _fail(code)
    return value


def _mapping(value: object, *, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return _fail(code)
    return value


def _text(value: object, *, code: str) -> str:
    text = str(value or "").strip()
    if not text:
        return _fail(code)
    return text


def authority_sha256(authority: Mapping[str, object]) -> str:
    return canonical_sha256(
        {str(key): value for key, value in authority.items() if key != "authority_sha256"}
    )


def receipt_sha256(receipt: Mapping[str, object]) -> str:
    return canonical_sha256(
        {str(key): value for key, value in receipt.items() if key != "receipt_sha256"}
    )


def reviewed_cue_manifest(cues: Sequence[object]) -> dict[str, object]:
    rows = [
        {
            "cue_index": index,
            "start_ms": int(getattr(cue, "start_ms")),
            "end_ms": int(getattr(cue, "end_ms")),
            "text": str(getattr(cue, "text") or "").strip(),
        }
        for index, cue in enumerate(cues, start=1)
    ]
    if not rows or any(
        not row["text"] or row["start_ms"] < 0 or row["end_ms"] <= row["start_ms"] for row in rows
    ):
        return _fail("REVIEWED_SRT_CUE_MANIFEST_INVALID")
    return {
        "schema_version": "reviewed-subtitle-cue-manifest.v1",
        "cues": rows,
    }


def reviewed_cue_manifest_sha256(cues: Sequence[object]) -> str:
    return canonical_sha256(reviewed_cue_manifest(cues))


def _parse_clock_ms(value: object, *, code: str) -> int:
    match = re.fullmatch(
        r"(?P<h>[0-9]{2}):(?P<m>[0-9]{2}):(?P<s>[0-9]{2})[,.](?P<ms>[0-9]{3})",
        str(value or ""),
    )
    if match is None:
        return _fail(code)
    return (
        int(match.group("h")) * 3_600_000
        + int(match.group("m")) * 60_000
        + int(match.group("s")) * 1_000
        + int(match.group("ms"))
    )


def canonical_speaker_segment_manifest(
    override: Mapping[str, object],
) -> dict[str, object]:
    rows = override.get("overrides")
    if not isinstance(rows, list) or not rows:
        return _fail("SPEAKER_SEGMENTS_INVALID")
    segments: list[dict[str, object]] = []
    prior_end = 0
    for cue_position, raw_row in enumerate(rows, start=1):
        row = _mapping(raw_row, code="SPEAKER_SEGMENTS_INVALID")
        cue = _integer(row.get("source_cue"), code="SPEAKER_SEGMENTS_INVALID", minimum=1)
        if cue != cue_position:
            return _fail("SPEAKER_SEGMENTS_INVALID")
        raw_segments = row.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            return _fail("SPEAKER_SEGMENTS_INVALID")
        for segment_position, raw_segment in enumerate(raw_segments, start=1):
            segment = _mapping(raw_segment, code="SPEAKER_SEGMENTS_INVALID")
            start_ms = _parse_clock_ms(segment.get("start"), code="SPEAKER_SEGMENTS_INVALID")
            end_ms = _parse_clock_ms(segment.get("end"), code="SPEAKER_SEGMENTS_INVALID")
            if end_ms <= start_ms or start_ms < prior_end:
                return _fail("SPEAKER_SEGMENTS_INVALID")
            prior_end = end_ms
            segments.append(
                {
                    "source_cue": cue,
                    "segment_index": segment_position,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "speaker": _text(segment.get("speaker"), code="SPEAKER_SEGMENTS_INVALID"),
                    "text": _text(segment.get("text"), code="SPEAKER_SEGMENTS_INVALID"),
                }
            )
    return {
        "schema_version": "reviewed-speaker-segment-manifest.v1",
        "cue_count": len(rows),
        "segment_count": len(segments),
        "segments": segments,
    }


def canonical_speaker_segment_manifest_sha256(
    override: Mapping[str, object],
) -> str:
    return canonical_sha256(canonical_speaker_segment_manifest(override))


def _logical_timeline_identity(recording_basename: str) -> dict[str, str]:
    match = _RECORDING_RX.fullmatch(recording_basename)
    if match is None:
        return _fail("LOGICAL_TIMELINE_INVALID")
    return {
        "platform": "bilibili-live",
        "room_id": match.group("room"),
        "recording_start_local": (
            match.group("date") + "T" + match.group("time").replace("-", ":")
        ),
    }


def validate_runtime_authority(value: object) -> dict[str, object]:
    authority = dict(_mapping(value, code="AUTHORITY_INVALID"))
    if authority.get("schema_version") != GRANT_SCHEMA_VERSION:
        return _fail("AUTHORITY_SCHEMA_UNSUPPORTED")
    if authority.get("status") != "ACTIVE":
        return _fail("AUTHORITY_NOT_ACTIVE")
    if authority.get("dispatch_mode") != DISPATCH_MODE:
        return _fail("DISPATCH_MODE_UNSUPPORTED")
    if authority.get("policy_id") != POLICY_ID:
        return _fail("POLICY_DRIFT")
    if _sha(authority.get("authority_sha256"), code="AUTHORITY_HASH_INVALID") != authority_sha256(
        authority
    ):
        return _fail("AUTHORITY_HASH_MISMATCH")

    source = _mapping(authority.get("source"), code="SOURCE_BINDING_INVALID")
    start_ms = _integer(source.get("absolute_source_start_ms"), code="INTERVAL_INVALID")
    end_ms = _integer(source.get("absolute_source_end_ms"), code="INTERVAL_INVALID")
    if end_ms <= start_ms:
        return _fail("INTERVAL_INVALID")
    basename = _text(source.get("recording_basename"), code="SOURCE_BINDING_INVALID")
    if Path(basename).name != basename:
        return _fail("SOURCE_BINDING_INVALID")
    _sha(source.get("source_sha256"), code="SOURCE_HASH_INVALID")
    if source.get("logical_timeline") != _logical_timeline_identity(basename):
        return _fail("LOGICAL_TIMELINE_MISMATCH")

    subtitle = _mapping(authority.get("reviewed_subtitle"), code="REVIEWED_SRT_BINDING_INVALID")
    _sha(subtitle.get("srt_sha256"), code="REVIEWED_SRT_HASH_INVALID")
    _sha(
        subtitle.get("cue_manifest_sha256"),
        code="REVIEWED_SRT_CUE_MANIFEST_INVALID",
    )
    cue_count = _integer(
        subtitle.get("cue_count"), code="REVIEWED_SRT_CUE_COUNT_INVALID", minimum=1
    )

    speaker = _mapping(authority.get("speaker_truth"), code="SPEAKER_TRUTH_BINDING_INVALID")
    _sha(speaker.get("truth_diff_sha256"), code="SPEAKER_TRUTH_HASH_INVALID")
    _sha(speaker.get("override_sha256"), code="SPEAKER_OVERRIDE_HASH_INVALID")
    _sha(
        speaker.get("segment_manifest_sha256"),
        code="SPEAKER_SEGMENT_MANIFEST_INVALID",
    )
    if (
        _integer(speaker.get("cue_count"), code="SPEAKER_TRUTH_CUE_COUNT_INVALID", minimum=1)
        != cue_count
    ):
        return _fail("SPEAKER_TRUTH_CUE_COUNT_MISMATCH")
    _integer(
        speaker.get("segment_count"),
        code="SPEAKER_TRUTH_SEGMENT_COUNT_INVALID",
        minimum=1,
    )

    terminal = _mapping(authority.get("terminal"), code="TERMINAL_BINDING_INVALID")
    anchor_ms = _integer(terminal.get("media_end_anchor_ms"), code="TERMINAL_ANCHOR_INVALID")
    last_cue_end_ms = _integer(terminal.get("last_cue_end_ms"), code="TERMINAL_CUE_END_INVALID")
    tail_ms = _integer(terminal.get("tail_ms"), code="TERMINAL_TAIL_INVALID")
    if (
        anchor_ms != end_ms - start_ms
        or last_cue_end_ms > anchor_ms
        or tail_ms != anchor_ms - last_cue_end_ms
        or tail_ms > MAX_TERMINAL_TAIL_MS
        or terminal.get("tail_policy") != "inclusive_0_to_400_ms"
    ):
        return _fail("TERMINAL_BINDING_MISMATCH")

    selection = _mapping(authority.get("selection"), code="SELECTION_BINDING_INVALID")
    for key in (
        "selection_hook_sha256",
        "selection_scorecard_sha256",
        "correction_authority_file_sha256",
        "correction_authority_sha256",
        "rescore_receipt_output_sha256",
        "selection_calibration_sha256",
    ):
        _sha(selection.get(key), code="SELECTION_BINDING_INVALID")
    verdict = _mapping(
        authority.get("frozen_semantic_verdict"),
        code="SEMANTIC_VERDICT_INVALID",
    )
    if not all(
        verdict.get(key) is True
        for key in (
            "syntax_complete",
            "story_closed",
            "next_topic_separated",
            "content_anchor_covered",
        )
    ):
        return _fail("SEMANTIC_VERDICT_NOT_PASS")
    for key in (
        "source_review_sha256",
        "final_review_sha256",
        "frozen_record_sha256",
        "frozen_boundary_audit_sha256",
        "frozen_story_contract_sha256",
        "closure_text_sha256",
    ):
        _sha(verdict.get(key), code="SEMANTIC_VERDICT_INVALID")

    receipt = _mapping(
        authority.get("authenticated_review_receipt"),
        code="AUTHENTICATED_REVIEW_RECEIPT_INVALID",
    )
    if (
        receipt.get("schema_version") != "authenticated-exact-audiovisual-review-receipt.v1"
        or receipt.get("status") != "AUTHENTICATED_REVIEWED_EXACT_AUDIOVISUAL_INTERVAL"
        or _sha(
            receipt.get("receipt_sha256"),
            code="AUTHENTICATED_REVIEW_RECEIPT_INVALID",
        )
        != receipt_sha256(receipt)
    ):
        return _fail("AUTHENTICATED_REVIEW_RECEIPT_INVALID")
    media = _mapping(receipt.get("reviewed_media"), code="AUTHENTICATED_REVIEW_RECEIPT_INVALID")
    if (
        media.get("source_sha256") != source.get("source_sha256")
        or media.get("absolute_source_start_ms") != start_ms
        or media.get("absolute_source_end_ms") != end_ms
    ):
        return _fail("AUTHENTICATED_REVIEW_MEDIA_MISMATCH")
    for key in (
        "readme_sha256",
        "recut_provenance_sha256",
        "recut_sha256",
        "burned_review_video_sha256",
    ):
        _sha(media.get(key), code="AUTHENTICATED_REVIEW_RECEIPT_INVALID")
    messages = receipt.get("messages")
    if not isinstance(messages, list) or len(messages) != 3:
        return _fail("AUTHENTICATED_REVIEW_MESSAGES_INVALID")
    if any(
        not isinstance(row, Mapping)
        or not str(row.get("message_id") or "").strip()
        or not str(row.get("timestamp") or "").strip()
        or _SHA256_RX.fullmatch(str(row.get("text_sha256") or "")) is None
        for row in messages
    ):
        return _fail("AUTHENTICATED_REVIEW_MESSAGES_INVALID")
    return authority


def authority_from_spec(spec: Mapping[str, object]) -> dict[str, object] | None:
    value = spec.get(RUNTIME_CONFIG_KEY)
    if value is None:
        baseline = spec.get("subtitle_redelivery_baseline")
        if isinstance(baseline, Mapping) and baseline.get(REFERENCE_CONFIG_KEY) is not None:
            return _fail("DECLARED_AUTHORITY_NOT_COMPILED")
        return None
    return validate_runtime_authority(value)


def _load_json(path: Path, *, code: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewedExactSourceIntervalError(f"REVIEWED_EXACT_SOURCE_INTERVAL_{code}") from exc
    return dict(_mapping(value, code=code))


def _stable_semantic_spec(spec: Mapping[str, object]) -> dict[str, object]:
    return {
        "candidate_id": spec.get("candidate_id"),
        "semantic_start_ms": spec.get("semantic_start_ms"),
        "semantic_end_ms": spec.get("semantic_end_ms"),
        "given_end_ms": spec.get("given_end_ms"),
        "given_end_mode": spec.get("given_end_mode"),
        "given_end_authority_sha256": canonical_sha256(
            {"authority": str(spec.get("given_end_authority") or "")}
        ),
    }


def compile_authority_from_spec(
    *,
    spec: Mapping[str, object],
    piece_provenance_rows: Sequence[Mapping[str, object]],
) -> dict[str, object] | None:
    """Validate a declared asset against live source/spec/truth bytes."""

    baseline = spec.get("subtitle_redelivery_baseline")
    reference_raw = baseline.get(REFERENCE_CONFIG_KEY) if isinstance(baseline, Mapping) else None
    if reference_raw is None:
        if spec.get(RUNTIME_CONFIG_KEY) is not None:
            return _fail("UNSEALED_RUNTIME_AUTHORITY_INJECTION")
        return None
    if spec.get(RUNTIME_CONFIG_KEY) is not None:
        return _fail("MULTIPLE_ACTIVE_AUTHORITIES")
    reference = _mapping(reference_raw, code="REFERENCE_INVALID")
    if reference.get("schema_version") != REFERENCE_SCHEMA_VERSION:
        return _fail("REFERENCE_SCHEMA_UNSUPPORTED")
    cid = str(spec.get("candidate_id") or "")
    try:
        # The registry owns the canonical candidate path, single-read parse,
        # and active HEAD/deployment seal for both manifest and grant bytes.
        from src.autoslice.reviewed_subtitle_baseline_registry import (
            ReviewedSubtitleBaselineRegistryError,
            load_candidate_reviewed_subtitle_baseline,
        )

        registered = load_candidate_reviewed_subtitle_baseline(
            _REPO_ROOT / "assets/lidousha/reviewed_subtitle_baselines",
            cid,
            repo_root=_REPO_ROOT,
        )
    except ReviewedSubtitleBaselineRegistryError:
        return _fail("AUTHORITY_ASSET_UNSEALED")
    if (
        registered is None
        or registered.exact_interval_authority is None
        or dict(baseline) != registered.config
    ):
        return _fail("REFERENCE_NOT_CANONICAL_REGISTRY_CONFIG")
    authority = validate_runtime_authority(registered.exact_interval_authority)
    if authority.get("candidate_id") != cid:
        return _fail("CANDIDATE_MISMATCH")
    semantic_spec = _mapping(authority.get("semantic_spec"), code="SEMANTIC_SPEC_INVALID")
    if semantic_spec.get("identity") != _stable_semantic_spec(spec):
        return _fail("SEMANTIC_SPEC_MISMATCH")
    if semantic_spec.get("identity_sha256") != canonical_sha256(semantic_spec.get("identity")):
        return _fail("SEMANTIC_SPEC_HASH_MISMATCH")

    pieces = spec.get("pieces")
    if not isinstance(pieces, list) or len(pieces) != 1:
        return _fail("SOURCE_PIECE_AMBIGUOUS")
    piece = _mapping(pieces[0], code="SOURCE_PIECE_INVALID")
    source = _mapping(authority.get("source"), code="SOURCE_BINDING_INVALID")
    if (
        Path(str(piece.get("remote_media") or "")).name != source.get("recording_basename")
        or _sha(piece.get("source_media_sha256"), code="SOURCE_HASH_INVALID")
        != source.get("source_sha256")
        or _integer(piece.get("start_ms"), code="SOURCE_PIECE_INVALID")
        > source.get("absolute_source_start_ms")
        or _integer(piece.get("end_ms"), code="SOURCE_PIECE_INVALID")
        < source.get("absolute_source_end_ms")
    ):
        return _fail("SOURCE_BINDING_MISMATCH")
    if len(piece_provenance_rows) != 1:
        return _fail("SOURCE_PROVENANCE_AMBIGUOUS")
    provenance = _mapping(piece_provenance_rows[0], code="SOURCE_PROVENANCE_INVALID")
    if _sha(provenance.get("source_sha256"), code="SOURCE_PROVENANCE_INVALID") != source.get(
        "source_sha256"
    ):
        return _fail("SOURCE_PROVENANCE_MISMATCH")

    assert isinstance(baseline, Mapping)
    subtitle_path = Path(str(baseline.get("path") or ""))
    expected_subtitle_path = (
        _REPO_ROOT / "assets/lidousha/reviewed_subtitle_baselines" / f"{cid}.reviewed.srt"
    )
    if subtitle_path.absolute() != expected_subtitle_path.absolute():
        return _fail("REVIEWED_SRT_PATH_NOT_CANONICAL")
    if subtitle_path.is_symlink() or not subtitle_path.is_file():
        return _fail("REVIEWED_SRT_UNAVAILABLE")
    subtitle = _mapping(authority.get("reviewed_subtitle"), code="REVIEWED_SRT_BINDING_INVALID")
    if file_sha256(subtitle_path) != subtitle.get("srt_sha256"):
        return _fail("REVIEWED_SRT_HASH_MISMATCH")
    cues = parse_srt_cues(subtitle_path.read_text(encoding="utf-8"))
    if len(cues) != subtitle.get("cue_count") or reviewed_cue_manifest_sha256(cues) != subtitle.get(
        "cue_manifest_sha256"
    ):
        return _fail("REVIEWED_SRT_CUE_MANIFEST_MISMATCH")
    terminal = _mapping(authority.get("terminal"), code="TERMINAL_BINDING_INVALID")
    if not cues or cues[-1].end_ms != terminal.get("last_cue_end_ms"):
        return _fail("TERMINAL_CUE_END_MISMATCH")
    closure_hash = "sha256:" + hashlib.sha256(cues[-1].text.encode("utf-8")).hexdigest()
    verdict = _mapping(
        authority.get("frozen_semantic_verdict"),
        code="SEMANTIC_VERDICT_INVALID",
    )
    if closure_hash != verdict.get("closure_text_sha256"):
        return _fail("SEMANTIC_VERDICT_CLOSURE_MISMATCH")
    if any(cue.end_ms > terminal.get("media_end_anchor_ms") for cue in cues):
        return _fail("REVIEWED_SRT_OVERRUN")

    speaker_path = Path(str(spec.get("speaker_overrides") or ""))
    expected_speaker_path = (
        _REPO_ROOT / "assets/lidousha/speaker_overrides" / f"{cid}.speaker.v1.json"
    )
    if speaker_path.absolute() != expected_speaker_path.absolute():
        return _fail("SPEAKER_OVERRIDE_PATH_NOT_CANONICAL")
    if speaker_path.is_symlink() or not speaker_path.is_file():
        return _fail("SPEAKER_OVERRIDE_UNAVAILABLE")
    speaker = _mapping(authority.get("speaker_truth"), code="SPEAKER_TRUTH_BINDING_INVALID")
    if file_sha256(speaker_path) != speaker.get("override_sha256"):
        return _fail("SPEAKER_OVERRIDE_HASH_MISMATCH")
    override = _load_json(speaker_path, code="SPEAKER_OVERRIDE_INVALID")
    if canonical_speaker_segment_manifest_sha256(override) != speaker.get(
        "segment_manifest_sha256"
    ):
        return _fail("SPEAKER_SEGMENT_MANIFEST_MISMATCH")
    manifest = canonical_speaker_segment_manifest(override)
    if (
        manifest["cue_count"] != speaker.get("cue_count")
        or manifest["segment_count"] != speaker.get("segment_count")
        or any(
            int(row["end_ms"]) > terminal.get("media_end_anchor_ms") for row in manifest["segments"]
        )
    ):
        return _fail("SPEAKER_SEGMENT_MANIFEST_MISMATCH")
    truth_rel = _text(speaker.get("truth_diff_repo_path"), code="SPEAKER_TRUTH_PATH_INVALID")
    truth_path = (_REPO_ROOT / truth_rel).resolve()
    if _REPO_ROOT not in truth_path.parents or not truth_path.is_file() or truth_path.is_symlink():
        return _fail("SPEAKER_TRUTH_UNAVAILABLE")
    if file_sha256(truth_path) != speaker.get("truth_diff_sha256"):
        return _fail("SPEAKER_TRUTH_HASH_MISMATCH")

    scorecard = spec.get("selection_scorecard")
    provenance_raw = spec.get("source_fact_scorecard_rescore_provenance")
    provenance_binding = _mapping(provenance_raw, code="SELECTION_PROVENANCE_MISSING")
    selection = _mapping(authority.get("selection"), code="SELECTION_BINDING_INVALID")
    correction = _mapping(
        provenance_binding.get("correction_authority"),
        code="SELECTION_PROVENANCE_INVALID",
    )
    receipt = _mapping(
        provenance_binding.get("rescore_receipt"),
        code="SELECTION_PROVENANCE_INVALID",
    )
    receipt_inputs = _mapping(receipt.get("input_bindings"), code="SELECTION_PROVENANCE_INVALID")
    actual_selection = {
        "selection_hook_sha256": canonical_sha256(
            {"selection_hook": str(spec.get("selection_hook") or "")}
        ),
        "selection_scorecard_sha256": canonical_sha256(scorecard),
        "correction_authority_file_sha256": _sha(
            provenance_binding.get("correction_authority_file_sha256"),
            code="SELECTION_PROVENANCE_INVALID",
        ),
        "correction_authority_sha256": _sha(
            correction.get("authority_sha256"),
            code="SELECTION_PROVENANCE_INVALID",
        ),
        "rescore_receipt_output_sha256": _sha(
            receipt.get("output_sha256"), code="SELECTION_PROVENANCE_INVALID"
        ),
        "selection_calibration_sha256": _sha(
            receipt_inputs.get("selection_calibration_sha256"),
            code="SELECTION_PROVENANCE_INVALID",
        ),
    }
    if dict(selection) != actual_selection:
        return _fail("SELECTION_BINDING_MISMATCH")
    return authority


def _baseline_cues(spec: Mapping[str, object]) -> list[SrtCue]:
    baseline = _mapping(
        spec.get("subtitle_redelivery_baseline"), code="REVIEWED_SRT_CONFIG_MISSING"
    )
    path = Path(_text(baseline.get("path"), code="REVIEWED_SRT_PATH_INVALID"))
    try:
        return parse_srt_cues(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ReviewedExactSourceIntervalError(
            "REVIEWED_EXACT_SOURCE_INTERVAL_REVIEWED_SRT_UNAVAILABLE"
        ) from exc


def source_timeline_cues(
    spec: Mapping[str, object], authority: Mapping[str, object]
) -> list[SrtCue]:
    source = _mapping(authority.get("source"), code="SOURCE_BINDING_INVALID")
    pieces = spec.get("pieces")
    if not isinstance(pieces, list) or len(pieces) != 1:
        return _fail("SOURCE_PIECE_AMBIGUOUS")
    piece_start = _integer(
        _mapping(pieces[0], code="SOURCE_PIECE_INVALID").get("start_ms"),
        code="SOURCE_PIECE_INVALID",
    )
    offset = int(source["absolute_source_start_ms"]) - piece_start
    return [
        SrtCue(
            index=str(index),
            start_ms=offset + cue.start_ms,
            end_ms=offset + cue.end_ms,
            text=cue.text,
        )
        for index, cue in enumerate(_baseline_cues(spec), start=1)
    ]


def _selector_story_witness(scorecard: object) -> dict[str, object]:
    dimensions = scorecard.get("dimensions") if isinstance(scorecard, Mapping) else {}
    return {
        "independence_group": SEMANTIC_LLM_INDEPENDENCE_GROUP,
        "status": "PASS",
        "self_contained": dimensions.get("self_contained"),
        "comedic_payoff": dimensions.get("comedic_payoff"),
    }


def _source_witness(
    review: Mapping[str, object], *, final_start_ms: int, final_end_ms: int
) -> dict[str, object]:
    return {
        "schema_version": "talk-boundary-source-separation-witness.v1",
        "status": "PASS",
        "source_review_sha256": semantic_review_sha256(review),
        "source_request_sha256": review.get("request_sha256"),
        "source_cue_grid_sha256": review.get("cue_grid_sha256"),
        "source_recommended_end_ms": review.get("recommended_end_ms"),
        "source_final_start_ms": final_start_ms,
        "source_final_end_ms": final_end_ms,
        "reason_codes": [],
    }


def _exact_review(
    *,
    cues: Sequence[SrtCue],
    authority: Mapping[str, object],
    candidate_id: str,
    scorecard: object,
    review_scope: str,
    final_start_ms: int,
    final_end_ms: int,
    boundary_search_scope: Mapping[str, object],
    source_review: Mapping[str, object] | None = None,
) -> dict[str, object]:
    terminal = _mapping(authority.get("terminal"), code="TERMINAL_BINDING_INVALID")
    if not cues:
        return _fail("REVIEWED_SRT_NO_CUES")
    closure = cues[-1]
    request = {
        "schema_version": "reviewed-exact-boundary-semantic-replay-request.v1",
        "authority_sha256": authority.get("authority_sha256"),
        "candidate_id": candidate_id,
        "review_scope": review_scope,
        "cue_grid_sha256": cue_grid_sha256(cues),
        "boundary_search_scope": dict(boundary_search_scope),
        "frozen_semantic_verdict": authority.get("frozen_semantic_verdict"),
    }
    review: dict[str, object] = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "candidate_id": candidate_id,
        "review_scope": review_scope,
        "target_ms": closure.end_ms,
        "target_cue_index": len(cues),
        "syntax_complete": True,
        "story_closed": True,
        "next_topic_separated": True,
        "content_anchor_covered": True,
        "same_topic_continues_after_target": False,
        "needs_more_context": False,
        "next_topic_witness_valid": True,
        "recommended_end_cue_index": len(cues),
        "recommended_end_ms": closure.end_ms,
        "evidence_cue_indexes": list(range(max(1, len(cues) - 2), len(cues) + 1)),
        "reason_codes": [
            "OPERATOR_REVIEWED_EXACT_AUDIOVISUAL_INTERVAL",
            "FROZEN_SEMANTIC_VERDICT_REPLAYED_ON_REVIEWED_TIMELINE",
        ],
        "summary": "Authenticated operator review fixes this exact audiovisual interval.",
        "retry_scope": "none",
        "max_forward_ms": 0,
        "selector_story_witness": _selector_story_witness(scorecard),
        "correlated_reviewer_disclosure": True,
        "reviewer_independence_group": SEMANTIC_LLM_INDEPENDENCE_GROUP,
        "semantic_independence_groups": [SEMANTIC_LLM_INDEPENDENCE_GROUP],
        "independent_semantic_vote_count": 1,
        "cue_grid_sha256": cue_grid_sha256(cues),
        "boundary_search_scope": dict(boundary_search_scope),
        "request_sha256": canonical_sha256(request),
        "reviewed_exact_source_interval_binding": {
            "schema_version": "reviewed-exact-source-interval-binding.v1",
            "authority_sha256": authority.get("authority_sha256"),
            "dispatch_mode": DISPATCH_MODE,
            "fresh_asr_role": "diagnostic_witness_only",
        },
        # Portable package audit and the post-mutation delivery gate need the
        # complete self-hashed grant; it contains no host/path identity.
        RUNTIME_CONFIG_KEY: dict(authority),
    }
    if source_review is not None:
        review["source_separation_witness"] = _source_witness(
            source_review,
            final_start_ms=final_start_ms,
            final_end_ms=final_end_ms,
        )
    review, reasons = bind_final_semantic_endpoint(
        semantic_review=review,
        cues=cues,
        closure_cue=closure,
        snapped_end_ms=closure.end_ms,
        final_start_ms=final_start_ms,
        final_end_ms=final_end_ms,
    )
    if reasons:
        return _fail("SEMANTIC_ENDPOINT_BINDING_FAILED")
    if final_end_ms - final_start_ms != terminal.get("media_end_anchor_ms"):
        return _fail("TERMINAL_ANCHOR_MISMATCH")
    return review


def build_source_semantic_review(
    *,
    spec: Mapping[str, object],
    authority: Mapping[str, object],
    boundary_search_scope: Mapping[str, object],
) -> dict[str, object]:
    authority = validate_runtime_authority(authority)
    source = _mapping(authority.get("source"), code="SOURCE_BINDING_INVALID")
    pieces = spec.get("pieces")
    if not isinstance(pieces, list) or len(pieces) != 1:
        return _fail("SOURCE_PIECE_AMBIGUOUS")
    piece_start = int(_mapping(pieces[0], code="SOURCE_PIECE_INVALID")["start_ms"])
    final_start = int(source["absolute_source_start_ms"]) - piece_start
    final_end = int(source["absolute_source_end_ms"]) - piece_start
    return _exact_review(
        cues=source_timeline_cues(spec, authority),
        authority=authority,
        candidate_id=str(spec.get("candidate_id") or ""),
        scorecard=spec.get("selection_scorecard"),
        review_scope="source_full_window",
        final_start_ms=final_start,
        final_end_ms=final_end,
        boundary_search_scope=boundary_search_scope,
    )


def build_final_semantic_review(
    *,
    cues: Sequence[object],
    source_review: Mapping[str, object],
    source_final_start_ms: int,
    source_final_end_ms: int,
    candidate_id: str,
    selection_scorecard: object,
    authority: Mapping[str, object],
) -> dict[str, object]:
    authority = validate_runtime_authority(authority)
    final_cues = [
        SrtCue(
            index=str(index),
            start_ms=int(getattr(cue, "start_ms")),
            end_ms=int(getattr(cue, "end_ms")),
            text=str(getattr(cue, "text") or "").strip(),
        )
        for index, cue in enumerate(cues, start=1)
        if str(getattr(cue, "text", "") or "").strip()
    ]
    subtitle = _mapping(authority.get("reviewed_subtitle"), code="REVIEWED_SRT_BINDING_INVALID")
    if len(final_cues) != subtitle.get("cue_count") or reviewed_cue_manifest_sha256(
        final_cues
    ) != subtitle.get("cue_manifest_sha256"):
        return _fail("FINAL_REVIEWED_SRT_MISMATCH")
    closure_end = final_cues[-1].end_ms
    scope = build_boundary_search_scope(
        semantic_target_ms=closure_end,
        repair_cap_ms=30_000,
        last_piece_start_ms=0,
        prior_piece_duration_ms=0,
    )
    return _exact_review(
        cues=final_cues,
        authority=authority,
        candidate_id=candidate_id,
        scorecard=selection_scorecard,
        review_scope="final_delivery",
        final_start_ms=0,
        final_end_ms=source_final_end_ms - source_final_start_ms,
        boundary_search_scope=scope,
        source_review=source_review,
    )


def prepare_exact_delivery_review(
    *,
    cues: Sequence[object],
    source_boundary_review: Mapping[str, object] | None,
    source_final_start_ms: int,
    source_final_end_ms: int,
    candidate_id: str,
    selection_scorecard: object,
) -> tuple[list[object], dict[str, object] | None]:
    """Normalize final cues and consume the exclusive exact authority, if any."""

    final_cues = [cue for cue in cues if str(getattr(cue, "text", "") or "").strip()]
    if not final_cues:
        return final_cues, _fail("FINAL_DELIVERY_BOUNDARY_NO_CUES")
    authority = (
        source_boundary_review.get(RUNTIME_CONFIG_KEY)
        if isinstance(source_boundary_review, Mapping)
        else None
    )
    if authority is None:
        return final_cues, None
    try:
        review = build_final_semantic_review(
            cues=final_cues,
            source_review=source_boundary_review,
            source_final_start_ms=source_final_start_ms,
            source_final_end_ms=source_final_end_ms,
            candidate_id=candidate_id,
            selection_scorecard=selection_scorecard,
            authority=authority,
        )
    except ReviewedExactSourceIntervalError as exc:
        review = _fail(str(exc))
    review.update(
        {
            "review_scope": "final_delivery",
            "candidate_id": candidate_id,
        }
    )
    return final_cues, review


def exact_source_cues(
    spec: Mapping[str, object], authority: Mapping[str, object]
) -> list[SourceCue]:
    return [
        SourceCue(
            cue_id=f"reviewed_exact_{index:04d}",
            source_start_ms=cue.start_ms,
            source_end_ms=cue.end_ms,
            text=cue.text,
            language="zh",
            kind="speech",
            confidence=1.0,
        )
        for index, cue in enumerate(source_timeline_cues(spec, authority), start=1)
    ]


def exact_boundary_search_scope(
    *,
    spec: Mapping[str, object],
    authority: Mapping[str, object],
    required_owner_end_ms: int | None,
) -> dict[str, object]:
    source = _mapping(authority.get("source"), code="SOURCE_BINDING_INVALID")
    pieces = spec.get("pieces")
    if not isinstance(pieces, list) or len(pieces) != 1:
        return _fail("SOURCE_PIECE_AMBIGUOUS")
    piece_start = int(_mapping(pieces[0], code="SOURCE_PIECE_INVALID")["start_ms"])
    final_end = int(source["absolute_source_end_ms"]) - piece_start
    if required_owner_end_ms is not None and required_owner_end_ms > final_end:
        return _fail("REQUIRED_OWNER_OUTSIDE_INTERVAL")
    return build_boundary_search_scope(
        semantic_target_ms=final_end,
        manual_lower_bound_ms=final_end,
        required_owner_end_ms=required_owner_end_ms,
        repair_cap_ms=int(spec.get("boundary_repair_extend_cap_ms", 30_000)),
        last_piece_start_ms=piece_start,
        prior_piece_duration_ms=0,
        boundary_end_mode="exact_source_pin",
    )


def resolve_exact_boundary(
    *,
    spec: Mapping[str, object],
    durations: Sequence[int],
    padded_duration_ms: int,
    fresh_cues: Sequence[object],
    speech_spans: Sequence[object],
) -> dict[str, object] | None:
    """Resolve the exclusive branch without consulting any fresh-grid rule."""

    authority = authority_from_spec(spec)
    if authority is None:
        return None
    pieces = spec.get("pieces")
    if not isinstance(pieces, list) or len(pieces) != 1 or len(durations) != 1:
        return _fail("SOURCE_PIECE_AMBIGUOUS")
    piece = _mapping(pieces[0], code="SOURCE_PIECE_INVALID")
    piece_start = _integer(piece.get("start_ms"), code="SOURCE_PIECE_INVALID")
    source = _mapping(authority.get("source"), code="SOURCE_BINDING_INVALID")
    final_start = int(source["absolute_source_start_ms"]) - piece_start
    final_end = int(source["absolute_source_end_ms"]) - piece_start
    if (
        final_start < 0
        or final_end <= final_start
        or final_end > int(durations[0])
        or final_end > padded_duration_ms
    ):
        return _fail("OUTPUT_SOURCE_SPAN_OVERRUN")
    terminal = _mapping(authority.get("terminal"), code="TERMINAL_BINDING_INVALID")
    if final_end - final_start != terminal.get("media_end_anchor_ms"):
        return _fail("TERMINAL_ANCHOR_MISMATCH")

    contradiction_raw = spec.get(CONTRADICTION_CONFIG_KEY)
    if contradiction_raw is not None:
        contradiction = dict(_mapping(contradiction_raw, code="NEXT_TOPIC_AUTHORITY_INVALID"))
        observed_receipt = _sha(
            contradiction.get("receipt_sha256"),
            code="NEXT_TOPIC_AUTHORITY_INVALID",
        )
        expected_receipt = canonical_sha256(
            {key: value for key, value in contradiction.items() if key != "receipt_sha256"}
        )
        if (
            contradiction.get("schema_version") != "operator-reviewed-next-topic-source-start.v1"
            or contradiction.get("status") != "CONFIRMED_NEXT_TOPIC"
            or contradiction.get("candidate_id") != authority.get("candidate_id")
            or contradiction.get("source_sha256") != source.get("source_sha256")
            or observed_receipt != expected_receipt
            or not str(contradiction.get("operator_authority") or "").strip()
        ):
            return _fail("NEXT_TOPIC_AUTHORITY_INVALID")
        next_topic_start = _integer(
            contradiction.get("absolute_source_start_ms"),
            code="NEXT_TOPIC_AUTHORITY_INVALID",
        )
        # Half-open interval semantics: an authoritative next topic at the
        # exact endpoint is excluded and therefore compatible; one millisecond
        # before it is a real contradiction and blocks.
        if next_topic_start < int(source["absolute_source_end_ms"]):
            return _fail("AUTHORITATIVE_NEXT_TOPIC_CONTRADICTION")

    owners_raw = spec.get("required_boundary_owners") or []
    if not isinstance(owners_raw, list):
        return _fail("REQUIRED_OWNER_INVALID")
    owner_failures: list[dict[str, object]] = []
    for owner_raw in owners_raw:
        owner = _mapping(owner_raw, code="REQUIRED_OWNER_INVALID")
        windows = owner.get("local_windows")
        if not isinstance(windows, list):
            return _fail("REQUIRED_OWNER_INVALID")
        for window_raw in windows:
            window = _mapping(window_raw, code="REQUIRED_OWNER_INVALID")
            start = _integer(window.get("start_ms"), code="REQUIRED_OWNER_INVALID")
            end = _integer(window.get("end_ms"), code="REQUIRED_OWNER_INVALID")
            if start < final_start or end > final_end or end <= start:
                owner_failures.append(
                    {
                        "owner_kind": owner.get("owner_kind"),
                        "owner_id": owner.get("owner_id"),
                        "window": dict(window),
                    }
                )
    if owner_failures:
        return _fail("REQUIRED_OWNER_OUTSIDE_INTERVAL")

    source_cues = source_timeline_cues(spec, authority)
    closure = source_cues[-1]
    if closure.end_ms != final_start + int(
        terminal["last_cue_end_ms"]
    ) or final_end - closure.end_ms != terminal.get("tail_ms"):
        return _fail("TERMINAL_BINDING_MISMATCH")
    scope = spec.get("boundary_search_scope")
    if not isinstance(scope, Mapping):
        return _fail("BOUNDARY_SEARCH_SCOPE_MISSING")
    expected_review = build_source_semantic_review(
        spec=spec, authority=authority, boundary_search_scope=scope
    )
    observed_review = spec.get("boundary_semantic_review")
    if observed_review != expected_review:
        return _fail("SOURCE_SEMANTIC_REVIEW_MISMATCH")

    audit = boundary_audit(
        speech_spans,
        start_ms=final_start,
        cut_ms=final_end,
        start_snapped=True,
        end_snapped=True,
    )
    audit.update(
        {
            "semantic_start_target_rel_ms": final_start,
            "snapped_sentence_start_ms": source_cues[0].start_ms,
            "final_start_ms": final_start,
            "opening_sentence": source_cues[0].text,
            "semantic_target_rel_ms": final_end,
            "boundary_selection_lower_bound_ms": closure.end_ms,
            "delivery_coverage_lower_bound_ms": final_end,
            "tail_pad_coverage_bridge": {
                "status": "USED" if closure.end_ms < final_end else "NOT_NEEDED",
                "closure_lower_bound_ms": closure.end_ms,
                "delivery_lower_bound_ms": final_end,
                "maximum_tail_pad_ms": MAX_TERMINAL_TAIL_MS,
            },
            "snapped_sentence_end_ms": closure.end_ms,
            "final_end_ms": final_end,
            "closure_sentence": closure.text,
            "syntactic_tail_audit": syntactic_tail_audit(closure.text),
            "tail_adjustment": {
                "nominal_end_ms": min(padded_duration_ms, closure.end_ms + 400),
                "final_end_ms": final_end,
                "next_speech_island_start_ms": None,
                "next_speech_island_guard_ms": 100,
                "reason": "operator_reviewed_exact_source_interval",
            },
            "tail_refinement_used": False,
            "boundary_repair_search_origin_ms": final_end,
            "boundary_repair_extend_cap_ms": 0,
            "boundary_repair_max_end_ms": final_end,
            "required_boundary_owner_start_ms": min(
                (
                    int(window["start_ms"])
                    for owner in owners_raw
                    for window in owner.get("local_windows") or []
                ),
                default=None,
            ),
            "required_boundary_owner_end_ms": max(
                (
                    int(window["end_ms"])
                    for owner in owners_raw
                    for window in owner.get("local_windows") or []
                ),
                default=None,
            ),
            "boundary_repairs": [],
            "frozen_required_boundary_owner_count": len(owners_raw),
            "frozen_required_boundary_owners": list(owners_raw),
            "required_boundary_owner_verification": {
                "status": "PASS",
                "failures": [],
            },
            "delivery_coverage_verification": {
                "status": "PASS",
                "failure": None,
            },
            "boundary_authority": BOUNDARY_AUTHORITY,
            "manual_end_authority": str(spec.get("given_end_authority") or ""),
            "manual_end_mode": DISPATCH_MODE,
            "boundary_semantic_review": expected_review,
            RUNTIME_CONFIG_KEY: authority,
            "fresh_asr_diagnostic_only": {
                "role": "diagnostic_witness_only",
                "cue_count": len(fresh_cues),
                "cue_grid_sha256": (cue_grid_sha256(fresh_cues) if fresh_cues else None),
                "may_move_boundary": False,
            },
            "red_flags": [],
        }
    )
    normalized = exact_source_cues(spec, authority)
    timing_qa = {
        "schema_version": "subtitle-timing-qa.v3",
        "window": {"start_ms": final_start, "end_ms": final_end},
        "policy": {"mode": DISPATCH_MODE},
        "speech_span_count": len(speech_spans),
        "speech_total_ms": sum(
            int(getattr(span, "end_ms")) - int(getattr(span, "start_ms")) for span in speech_spans
        ),
        "vad_evidence_contract": "diagnostic_only_for_reviewed_exact_timeline",
        "actions": [],
        "counts": {"dropped": 0, "retimed": 0, "output": len(normalized)},
    }
    return {
        "final_start": final_start,
        "final_end": final_end,
        "audit": audit,
        "sanitized_cues": normalized,
        "timing_qa": timing_qa,
    }
