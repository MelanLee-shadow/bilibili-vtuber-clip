"""Fail-closed nested-media caption/source attribution.

Embedded captions are evidence that watched media contains a text surface.  They
are not proof that the host stayed silent.  This module therefore aligns caption
observations to the final SRT but permits deletion only when a separate,
hash-bound acoustic receipt proves source-media speech PRESENT, host speech
ABSENT, and overlap speech ABSENT.  ``uniform_host`` remains a rendering policy,
never speaker authority.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from src.autoslice.subtitle_audio_correspondence import (
    TimedCue,
    normalize_text,
    parse_timed_srt,
)

ATTRIBUTION_SCHEMA = "nested-media-caption-source-attribution.v1"
OBSERVATION_SCHEMA = "embedded-media-caption-observation.v1"
ACOUSTIC_SCHEMA = "nested-media-acoustic-attribution.v1"
PRIVATE_RECEIPT_SCHEMA = "nested-media-private-host-track.v1"

_LABELS = frozenset({"source_media", "host", "overlap", "unknown"})
_SPEECH_STATES = frozenset({"PRESENT", "ABSENT", "UNKNOWN"})
_CAPTION_ROLES = frozenset(
    {"WATCHED_MEDIA_CAPTION", "NO_EMBEDDED_CAPTION_OBSERVED"}
)


class AttributionInputError(ValueError):
    """An attribution input is malformed, drifted, or unsafe to consume."""


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normal_hash(value: object, *, code: str) -> str:
    if not isinstance(value, str):
        raise AttributionInputError(code)
    raw = value.removeprefix("sha256:").lower()
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise AttributionInputError(code)
    return "sha256:" + raw


def _regular_file(path: str | Path, *, code: str) -> Path:
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AttributionInputError(code) from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise AttributionInputError(code)
    return resolved


def _sha256(path: str | Path, *, code: str = "INPUT_FILE_INVALID") -> str:
    resolved = _regular_file(path, code=code)
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AttributionInputError(code) from exc
    return "sha256:" + digest.hexdigest()


def _verified_file(
    path: str | Path, expected_sha256: object, *, unavailable: str, mismatch: str
) -> Path:
    resolved = _regular_file(path, code=unavailable)
    expected = _normal_hash(expected_sha256, code=mismatch)
    if _sha256(resolved, code=unavailable) != expected:
        raise AttributionInputError(mismatch)
    return resolved


def _cue_id(cue: TimedCue) -> int | str:
    return int(cue.index) if cue.index.isdigit() else cue.index


def _compact_visible(text: str) -> str:
    return "".join(text.split())


def _match_kind(
    visible: str, cue_text: str, *, cue_count: int, threshold: float
) -> tuple[str, float] | None:
    visible_norm = normalize_text(visible)
    cue_norm = normalize_text(cue_text)
    if not visible_norm or not cue_norm:
        return None
    if visible_norm == cue_norm:
        if cue_count > 1:
            return "cross_cue_segmentation", 1.0
        if _compact_visible(visible) == _compact_visible(cue_text):
            return "exact", 1.0
        return "normalized_exact", 1.0
    similarity = SequenceMatcher(None, visible_norm, cue_norm).ratio()
    if similarity < threshold:
        return None
    return ("cross_cue_segmentation" if cue_count > 1 else "fuzzy", similarity)


def _nearby_spans(
    cues: Sequence[TimedCue], *, frame_ms: int, lead_lag_ms: int, max_span_cues: int
) -> list[tuple[TimedCue, ...]]:
    spans: list[tuple[TimedCue, ...]] = []
    for start in range(len(cues)):
        for size in range(1, max_span_cues + 1):
            selected = tuple(cues[start : start + size])
            if len(selected) != size:
                break
            if (
                selected[0].start_ms <= frame_ms + lead_lag_ms
                and selected[-1].end_ms >= frame_ms - lead_lag_ms
            ):
                spans.append(selected)
    return spans


def _nearest_single_cue(
    cues: Sequence[TimedCue], *, frame_ms: int, lead_lag_ms: int
) -> tuple[TimedCue, ...]:
    nearby = _nearby_spans(
        cues, frame_ms=frame_ms, lead_lag_ms=lead_lag_ms, max_span_cues=1
    )
    if not nearby:
        return ()
    return min(
        nearby,
        key=lambda span: abs(((span[0].start_ms + span[0].end_ms) // 2) - frame_ms),
    )


def _validate_observation(
    value: object, *, candidate_id: str
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise AttributionInputError("CAPTION_OBSERVATION_INVALID")
    observation_id = value.get("observation_id")
    frame_ms = value.get("frame_ms")
    caption_present = value.get("caption_present")
    visible_text = value.get("visible_text")
    role = value.get("role")
    if not (
        value.get("schema_version") == OBSERVATION_SCHEMA
        and value.get("candidate_id") == candidate_id
        and isinstance(observation_id, str)
        and observation_id.strip()
        and isinstance(frame_ms, int)
        and not isinstance(frame_ms, bool)
        and frame_ms >= 0
        and isinstance(caption_present, bool)
        and isinstance(visible_text, str)
        and role in _CAPTION_ROLES
        and value.get("pipeline_burned_subtitle") is False
    ):
        raise AttributionInputError("CAPTION_OBSERVATION_INVALID")
    if caption_present != bool(visible_text.strip()):
        raise AttributionInputError("CAPTION_OBSERVATION_TEXT_INVALID")
    expected_role = (
        "WATCHED_MEDIA_CAPTION"
        if caption_present
        else "NO_EMBEDDED_CAPTION_OBSERVED"
    )
    if role != expected_role:
        raise AttributionInputError("CAPTION_OBSERVATION_ROLE_INVALID")
    frame = _verified_file(
        value.get("frame_path", ""),
        value.get("frame_sha256"),
        unavailable="CAPTION_FRAME_UNAVAILABLE",
        mismatch="CAPTION_FRAME_HASH_MISMATCH",
    )
    return {
        "observation_id": observation_id,
        "frame_ms": frame_ms,
        "caption_present": caption_present,
        "visible_text": visible_text.strip(),
        "role": role,
        "frame_path": str(frame),
        "frame_sha256": _sha256(frame),
    }


def _load_acoustic_receipts(
    bindings: Sequence[Mapping[str, object]],
    *,
    candidate_id: str,
    source_media_sha256: str,
    final_srt_sha256: str,
) -> dict[str, dict[str, object]]:
    receipts: dict[str, dict[str, object]] = {}
    for binding in bindings:
        if not isinstance(binding, Mapping):
            raise AttributionInputError("ACOUSTIC_BINDING_INVALID")
        path = _verified_file(
            binding.get("path", ""),
            binding.get("sha256"),
            unavailable="ACOUSTIC_RECEIPT_UNAVAILABLE",
            mismatch="ACOUSTIC_RECEIPT_HASH_MISMATCH",
        )
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AttributionInputError("ACOUSTIC_RECEIPT_INVALID") from exc
        interval = receipt.get("interval_ms") if isinstance(receipt, Mapping) else None
        observation_id = receipt.get("observation_id") if isinstance(receipt, Mapping) else None
        source_state = receipt.get("source_media_speech") if isinstance(receipt, Mapping) else None
        host_state = receipt.get("host_speech") if isinstance(receipt, Mapping) else None
        overlap_state = receipt.get("overlap_speech") if isinstance(receipt, Mapping) else None
        input_bindings = receipt.get("bindings") if isinstance(receipt, Mapping) else None
        if not (
            isinstance(receipt, Mapping)
            and receipt.get("schema_version") == ACOUSTIC_SCHEMA
            and receipt.get("status") == "PASS"
            and receipt.get("candidate_id") == candidate_id
            and isinstance(observation_id, str)
            and observation_id
            and isinstance(interval, list)
            and len(interval) == 2
            and all(isinstance(item, int) and not isinstance(item, bool) for item in interval)
            and 0 <= interval[0] < interval[1]
            and source_state in _SPEECH_STATES
            and host_state in _SPEECH_STATES
            and overlap_state in _SPEECH_STATES
            and isinstance(receipt.get("method"), str)
            and str(receipt.get("method")).startswith("HASH_BOUND_")
            and isinstance(receipt.get("accepted_by"), str)
            and bool(str(receipt.get("accepted_by")).strip())
            and isinstance(receipt.get("accepted_at"), str)
            and bool(str(receipt.get("accepted_at")).strip())
            and isinstance(input_bindings, Mapping)
            and input_bindings.get("source_media_sha256") == source_media_sha256
            and input_bindings.get("final_srt_sha256") == final_srt_sha256
            and isinstance(input_bindings.get("caption_frame_sha256"), str)
        ):
            raise AttributionInputError("ACOUSTIC_RECEIPT_INVALID")
        if observation_id in receipts:
            raise AttributionInputError("ACOUSTIC_RECEIPT_DUPLICATE")
        receipts[observation_id] = {
            **dict(receipt),
            "path": str(path),
            "sha256": _sha256(path),
            "receipt_payload_sha256": _canonical_hash(receipt),
        }
    return receipts


def _classify(acoustic: Mapping[str, object] | None) -> tuple[str, str, list[str]]:
    if acoustic is None:
        return "unknown", "KEEP", ["CAPTION_ONLY_CANNOT_EXCLUDE_HOST"]
    source = acoustic.get("source_media_speech")
    host = acoustic.get("host_speech")
    overlap = acoustic.get("overlap_speech")
    if source == "PRESENT" and host == "ABSENT" and overlap == "ABSENT":
        return "source_media", "DROP_SOURCE_MEDIA", ["SOURCE_PRESENT_HOST_AND_OVERLAP_ABSENT"]
    if source == "PRESENT" and (host == "PRESENT" or overlap == "PRESENT"):
        return "overlap", "KEEP", ["SOURCE_AND_HOST_OR_OVERLAP_PRESENT"]
    if source == "ABSENT" and host == "PRESENT" and overlap == "ABSENT":
        return "host", "KEEP", ["SOURCE_ABSENT_HOST_PRESENT_OVERLAP_ABSENT"]
    return "unknown", "KEEP", ["ACOUSTIC_EVIDENCE_INSUFFICIENT_OR_CONFLICTING"]


def build_caption_source_attribution(
    *,
    candidate_id: str,
    source_media_path: str | Path,
    source_media_sha256: str,
    final_srt_path: str | Path,
    final_srt_sha256: str,
    observations: Sequence[Mapping[str, object]],
    acoustic_receipt_bindings: Sequence[Mapping[str, object]] = (),
    lead_lag_ms: int = 1_800,
    max_span_cues: int = 3,
    fuzzy_threshold: float = 0.72,
) -> dict[str, object]:
    """Align visual captions to SRT and classify only from typed acoustic proof."""

    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise AttributionInputError("CANDIDATE_ID_INVALID")
    if lead_lag_ms < 0 or max_span_cues < 1 or not 0.0 <= fuzzy_threshold <= 1.0:
        raise AttributionInputError("ATTRIBUTION_POLICY_INVALID")
    media = _verified_file(
        source_media_path,
        source_media_sha256,
        unavailable="SOURCE_MEDIA_UNAVAILABLE",
        mismatch="SOURCE_MEDIA_HASH_MISMATCH",
    )
    final_srt = _verified_file(
        final_srt_path,
        final_srt_sha256,
        unavailable="FINAL_SRT_UNAVAILABLE",
        mismatch="FINAL_SRT_HASH_MISMATCH",
    )
    try:
        cues = parse_timed_srt(final_srt.read_text(encoding="utf-8-sig"), label="final SRT")
    except (OSError, UnicodeError, ValueError) as exc:
        raise AttributionInputError("FINAL_SRT_INVALID") from exc
    actual_source_media_sha256 = _sha256(media)
    actual_final_srt_sha256 = _sha256(final_srt)
    acoustic = _load_acoustic_receipts(
        acoustic_receipt_bindings,
        candidate_id=candidate_id,
        source_media_sha256=actual_source_media_sha256,
        final_srt_sha256=actual_final_srt_sha256,
    )
    normalized_observations: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in observations:
        observation = _validate_observation(raw, candidate_id=candidate_id)
        observation_id = str(observation["observation_id"])
        if observation_id in seen:
            raise AttributionInputError("CAPTION_OBSERVATION_DUPLICATE")
        seen.add(observation_id)
        normalized_observations.append(observation)
    if set(acoustic) - seen:
        raise AttributionInputError("ACOUSTIC_RECEIPT_ORPHANED")

    rows: list[dict[str, object]] = []
    for observation in normalized_observations:
        frame_ms = int(observation["frame_ms"])
        matched: tuple[TimedCue, ...] = ()
        match_kind = "no_caption_control"
        similarity = 0.0
        if observation["caption_present"]:
            ranked: list[tuple[float, int, int, tuple[TimedCue, ...], str]] = []
            for span in _nearby_spans(
                cues,
                frame_ms=frame_ms,
                lead_lag_ms=lead_lag_ms,
                max_span_cues=max_span_cues,
            ):
                cue_text = "".join(cue.text for cue in span)
                match = _match_kind(
                    str(observation["visible_text"]),
                    cue_text,
                    cue_count=len(span),
                    threshold=fuzzy_threshold,
                )
                if match is None:
                    continue
                kind, score = match
                midpoint = (span[0].start_ms + span[-1].end_ms) // 2
                ranked.append((score, -abs(midpoint - frame_ms), -len(span), span, kind))
            if ranked:
                similarity, _, _, matched, match_kind = max(
                    ranked, key=lambda item: item[:3]
                )
        else:
            matched = _nearest_single_cue(
                cues, frame_ms=frame_ms, lead_lag_ms=lead_lag_ms
            )

        acoustic_receipt = acoustic.get(str(observation["observation_id"]))
        if acoustic_receipt is not None:
            acoustic_bindings = acoustic_receipt["bindings"]
            assert isinstance(acoustic_bindings, Mapping)
            if acoustic_bindings.get("caption_frame_sha256") != observation["frame_sha256"]:
                raise AttributionInputError("ACOUSTIC_CAPTION_FRAME_BINDING_MISMATCH")
            interval = acoustic_receipt["interval_ms"]
            assert isinstance(interval, list)
            if not interval[0] <= frame_ms < interval[1]:
                raise AttributionInputError("ACOUSTIC_INTERVAL_MISMATCH")
        label, action, reason_codes = _classify(acoustic_receipt)
        if not observation["caption_present"] and acoustic_receipt is None:
            reason_codes = ["NO_CAPTION_IS_NOT_HOST_EVIDENCE"]
        if not matched:
            label, action = "unknown", "KEEP"
            reason_codes = [*reason_codes, "NO_SRT_CAPTION_ALIGNMENT"]
        rows.append(
            {
                **observation,
                "cue_indexes": [_cue_id(cue) for cue in matched],
                "cue_interval_ms": (
                    [matched[0].start_ms, matched[-1].end_ms] if matched else None
                ),
                "final_text": "｜".join(cue.text for cue in matched),
                "visual_match": match_kind if matched else "unmatched",
                "similarity": round(float(similarity), 6),
                "acoustic_receipt": acoustic_receipt,
                "label": label,
                "action": action,
                "reason_codes": reason_codes,
            }
        )

    cue_votes: dict[int | str, list[str]] = {}
    for row in rows:
        for cue_index in row["cue_indexes"]:
            cue_votes.setdefault(cue_index, []).append(str(row["action"]))
    cue_actions = [
        {
            "cue_index": cue_index,
            "action": (
                "DROP_SOURCE_MEDIA"
                if actions and all(action == "DROP_SOURCE_MEDIA" for action in actions)
                else "KEEP"
            ),
            "evidence_actions": actions,
        }
        for cue_index, actions in sorted(cue_votes.items(), key=lambda item: str(item[0]))
    ]
    safe_to_drop = [
        row["cue_index"] for row in cue_actions if row["action"] == "DROP_SOURCE_MEDIA"
    ]
    counts = {label: sum(row["label"] == label for row in rows) for label in _LABELS}
    return {
        "schema_version": ATTRIBUTION_SCHEMA,
        "status": "PASS_CONSERVATIVE_ATTRIBUTION",
        "candidate_id": candidate_id,
        "source_media_path": str(media),
        "source_media_sha256": actual_source_media_sha256,
        "final_srt_path": str(final_srt),
        "final_srt_sha256": actual_final_srt_sha256,
        "uniform_host_display_only": True,
        "policy": {
            "lead_lag_ms": lead_lag_ms,
            "max_span_cues": max_span_cues,
            "fuzzy_threshold": fuzzy_threshold,
            "drop_requires": "SOURCE_PRESENT_HOST_ABSENT_AND_OVERLAP_ABSENT_HASH_BOUND_ACOUSTIC_RECEIPT",
            "no_caption_default": "UNKNOWN_KEEP",
        },
        "rows": rows,
        "cue_actions": cue_actions,
        "safe_to_drop_cue_indexes": safe_to_drop,
        "counts": counts,
        "input_binding_sha256": _canonical_hash(
            {
                "source_media_sha256": actual_source_media_sha256,
                "final_srt_sha256": actual_final_srt_sha256,
                "observations": normalized_observations,
                "acoustic_receipts": acoustic,
            }
        ),
    }


def _format_time(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def materialize_private_host_track(
    *,
    attribution: Mapping[str, object],
    final_srt_path: str | Path,
    output_path: str | Path,
) -> dict[str, object]:
    """Create a private-only host-track candidate; never publication authority."""

    if not (
        isinstance(attribution, Mapping)
        and attribution.get("schema_version") == ATTRIBUTION_SCHEMA
        and attribution.get("status") == "PASS_CONSERVATIVE_ATTRIBUTION"
        and attribution.get("uniform_host_display_only") is True
    ):
        raise AttributionInputError("ATTRIBUTION_RESULT_INVALID")
    source = _verified_file(
        final_srt_path,
        attribution.get("final_srt_sha256"),
        unavailable="FINAL_SRT_UNAVAILABLE",
        mismatch="FINAL_SRT_HASH_MISMATCH",
    )
    try:
        cues = parse_timed_srt(source.read_text(encoding="utf-8-sig"), label="final SRT")
    except (OSError, UnicodeError, ValueError) as exc:
        raise AttributionInputError("FINAL_SRT_INVALID") from exc
    drop = set(attribution.get("safe_to_drop_cue_indexes") or [])
    known = {_cue_id(cue) for cue in cues}
    if not drop <= known:
        raise AttributionInputError("DROP_CUE_UNKNOWN")
    if drop:
        blocks = [
            f"{cue.index}\n{_format_time(cue.start_ms)} --> {_format_time(cue.end_ms)}\n{cue.text}"
            for cue in cues
            if _cue_id(cue) not in drop
        ]
        payload = ("\n\n".join(blocks) + ("\n" if blocks else "")).encode("utf-8")
        status = "PRIVATE_DIAGNOSTIC_HOST_TRACK_CANDIDATE"
    else:
        payload = source.read_bytes()
        status = "PRIVATE_DIAGNOSTIC_NO_MUTATION_PENDING_ACOUSTIC"
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise AttributionInputError("PRIVATE_OUTPUT_EXISTS") from exc
    return {
        "schema_version": PRIVATE_RECEIPT_SCHEMA,
        "status": status,
        "candidate_id": attribution.get("candidate_id"),
        "source_srt_path": str(source),
        "source_srt_sha256": _sha256(source),
        "output_srt_path": str(target.resolve(strict=True)),
        "output_srt_sha256": _sha256(target),
        "output_bytes": target.stat().st_size,
        "dropped_cue_indexes": sorted(drop, key=str),
        "uniform_host_display_only": True,
        "upload_allowed": False,
        "publication_authority": False,
        "attribution_input_binding_sha256": attribution.get("input_binding_sha256"),
    }
