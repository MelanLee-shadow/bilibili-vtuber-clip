"""Portrait-source solo prior for the existing binary speaker finalizer.

The prior is deliberately downstream of CAM++ and the whole-clip judge.  It
only resolves an identity-review hold; it never bypasses provider mixed/
overlap evidence, a significant acoustic two-cluster result, or confirmed
session relationship context.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.segment_scene_context import (
    SegmentSceneContextError,
    validate_segment_scene_context,
)
from src.autoslice.speaker_common import (
    GUEST_SPEAKER,
    HOST_SPEAKER,
    SINGLETON_CONTEXT_HOST_MIN_CONFIDENCE,
)


SESSION_CONTEXT_SCHEMA = "speaker-session-context.v1"
PRIOR_RECEIPT_SCHEMA = "portrait-speaker-solo-prior.v1"


def _canonical_bytes(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def build_speaker_session_context(
    *,
    candidate_id: str,
    session_id: str | None,
    segment_path: str | Path,
    start_ms: int,
    end_ms: int,
    segment_scene_context: object,
    session_relation_authority: object,
    source_piece_count: int,
    source_piece_segments: Sequence[str | Path] | None = None,
) -> dict[str, object] | None:
    """Build the optional context channel; malformed orientation means absent."""

    try:
        scene = validate_segment_scene_context(
            segment_scene_context,
            expected_segment=Path(segment_path).name,
            expected_source_path=segment_path,
        )
    except SegmentSceneContextError:
        return None
    if (
        not candidate_id
        or isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or not 0 <= start_ms < end_ms
        or isinstance(source_piece_count, bool)
        or not isinstance(source_piece_count, int)
        or source_piece_count < 1
    ):
        return None
    source_segment = Path(segment_path).name
    piece_segments = [
        Path(value).name for value in (source_piece_segments or [source_segment])
    ]
    if (
        len(piece_segments) != source_piece_count
        or any(value != source_segment for value in piece_segments)
    ):
        return None
    relation = (
        dict(session_relation_authority)
        if isinstance(session_relation_authority, Mapping)
        else None
    )
    return {
        "schema_version": SESSION_CONTEXT_SCHEMA,
        "candidate_id": candidate_id,
        "session_id": str(session_id or ""),
        "source_segment": source_segment,
        "candidate_interval_ms": {"start": start_ms, "end": end_ms},
        "source_piece_count": source_piece_count,
        "source_piece_segments": piece_segments,
        "segment_scene_context": scene,
        "segment_scene_context_sha256": _sha256_bytes(_canonical_bytes(scene)),
        # A static roster is occurrence-neutral.  Only the already governed,
        # segment-bound relation authority may veto the prior as context proof.
        "session_relation_authority": relation,
    }


def write_speaker_session_context(path: Path, document: Mapping[str, object]) -> None:
    path.write_bytes(_canonical_bytes(document))


def _validated_context(
    payload: object, *, expected_candidate_id: str
) -> dict[str, object] | None:
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != SESSION_CONTEXT_SCHEMA
        or payload.get("candidate_id") != expected_candidate_id
        or not isinstance(payload.get("candidate_interval_ms"), Mapping)
    ):
        return None
    source_piece_count = payload.get("source_piece_count")
    piece_segments = payload.get("source_piece_segments")
    source_segment = str(payload.get("source_segment") or "")
    if (
        isinstance(source_piece_count, bool)
        or not isinstance(source_piece_count, int)
        or source_piece_count < 1
        or not isinstance(piece_segments, list)
        or len(piece_segments) != source_piece_count
        or any(value != source_segment for value in piece_segments)
    ):
        return None
    interval = payload["candidate_interval_ms"]
    start, end = interval.get("start"), interval.get("end")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)) or not 0 <= start < end:
        return None
    try:
        scene = validate_segment_scene_context(
            payload.get("segment_scene_context"),
            expected_segment=source_segment,
        )
    except SegmentSceneContextError:
        return None
    if payload.get("segment_scene_context_sha256") != _sha256_bytes(
        _canonical_bytes(scene)
    ):
        return None
    return {**dict(payload), "segment_scene_context": scene}


def _strong_campp_two_cluster(analysis: Mapping[str, object]) -> dict[str, object]:
    """Measure significance only from the analysis' existing hard margin.

    ``2 * ambiguity_band`` is not a new absolute score: it is exactly the
    minimum center separation needed for the two centers to lie beyond the
    existing ``threshold +/- band`` hard-label boundaries.  Two hard cues on
    each side also prevent a singleton/BGM outlier from becoming the 1% veto.
    The current speaker-work band is 0.10, so this derived gap is 0.20; the
    implementation reads the bound analysis policy instead of hard-coding it.
    """

    policy = analysis.get("policy")
    threshold = analysis.get("threshold")
    centers = analysis.get("cluster_centers")
    decisions = analysis.get("decisions")
    if (
        analysis.get("mode") != "multi_speaker"
        or not isinstance(policy, Mapping)
        or not isinstance(centers, Mapping)
        or not isinstance(decisions, Sequence)
        or isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
    ):
        return {"strong": False, "reason_code": "CAMPP_TWO_CLUSTER_NOT_ESTABLISHED"}
    band = policy.get("ambiguity_band")
    center_values = [
        float(value)
        for value in centers.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if (
        isinstance(band, bool)
        or not isinstance(band, (int, float))
        or not math.isfinite(float(band))
        or float(band) <= 0
        or len(center_values) != 2
        or not all(math.isfinite(value) for value in center_values)
    ):
        return {"strong": False, "reason_code": "CAMPP_MARGIN_GEOMETRY_INVALID"}
    band_value = float(band)
    threshold_value = float(threshold)
    distances = []
    for row in decisions:
        margin = row.get("margin") if isinstance(row, Mapping) else None
        if isinstance(margin, (int, float)) and not isinstance(margin, bool):
            distance = float(margin) - threshold_value
            if math.isfinite(distance):
                distances.append(distance)
    hard_host_count = sum(value >= band_value for value in distances)
    hard_guest_count = sum(value <= -band_value for value in distances)
    center_gap = max(center_values) - min(center_values)
    guest_groups = analysis.get("guest_anchor_groups")
    guest_anchor_count = sum(
        len(group) for group in guest_groups if isinstance(group, list)
    ) if isinstance(guest_groups, list) else 0
    strong = bool(
        center_gap >= 2.0 * band_value
        and hard_host_count >= 2
        and hard_guest_count >= 2
        and guest_anchor_count >= 2
    )
    return {
        "strong": strong,
        "reason_code": (
            "CAMPP_SIGNIFICANT_TWO_CLUSTER"
            if strong
            else "CAMPP_TWO_CLUSTER_BELOW_EXISTING_HARD_MARGIN"
        ),
        "ambiguity_band": band_value,
        "required_center_gap": 2.0 * band_value,
        "observed_center_gap": center_gap,
        "hard_host_cue_count": hard_host_count,
        "hard_guest_cue_count": hard_guest_count,
        "guest_anchor_count": guest_anchor_count,
    }


def _relation_counterevidence(context: Mapping[str, object]) -> dict[str, object]:
    relation = context.get("session_relation_authority")
    participants = relation.get("participants") if isinstance(relation, Mapping) else None
    if (
        not isinstance(relation, Mapping)
        or relation.get("schema_version") != "session-relation-authority.v1"
        or relation.get("bound_recording_basename") != context.get("source_segment")
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(relation.get("ledger_sha256") or "")
        )
        is None
        or not isinstance(participants, list)
        or len(participants) < 2
    ):
        return {"strong": False, "reason_code": "NO_CONFIRMED_RELATION_CONTEXT"}
    state = str(relation.get("state") or "")
    if state == "CONFLICTED":
        return {"strong": True, "reason_code": "RELATION_CONTEXT_CONFLICTED"}
    if state != "CONFIRMED":
        return {"strong": False, "reason_code": "NO_CONFIRMED_RELATION_CONTEXT"}
    interval = context["candidate_interval_ms"]
    start, end = int(interval["start"]), int(interval["end"])
    raw_intervals = relation.get("presence_intervals")
    valid_intervals = [
        row
        for row in raw_intervals
        if isinstance(row, Mapping)
        and isinstance(row.get("start_ms"), int)
        and not isinstance(row.get("start_ms"), bool)
        and isinstance(row.get("end_ms"), int)
        and not isinstance(row.get("end_ms"), bool)
        and int(row["start_ms"]) < int(row["end_ms"])
    ] if isinstance(raw_intervals, list) else []
    overlaps = not valid_intervals or any(
        max(start, int(row["start_ms"])) < min(end, int(row["end_ms"]))
        for row in valid_intervals
    )
    return {
        "strong": overlaps,
        "reason_code": (
            "CONFIRMED_RELATION_PRESENCE_OVERLAP"
            if overlaps
            else "CONFIRMED_RELATION_OUTSIDE_CANDIDATE_INTERVAL"
        ),
        "relation_id": relation.get("relation_id"),
        "participant_count": len(participants),
    }


def _context_guest_counterevidence(analysis: Mapping[str, object]) -> dict[str, object]:
    decisions = analysis.get("decisions")
    if isinstance(decisions, list) and any(
        isinstance(row, Mapping)
        and row.get("speaker") == GUEST_SPEAKER
        and row.get("decision_source") == "accepted_context_baseline"
        for row in decisions
    ):
        return {"strong": True, "reason_code": "REVIEWED_CONTEXT_GUEST"}
    singleton_rows = analysis.get("singleton_evidence")
    for row in singleton_rows if isinstance(singleton_rows, list) else []:
        decision = row.get("context_decision") if isinstance(row, Mapping) else None
        confidence = decision.get("confidence") if isinstance(decision, Mapping) else None
        if (
            isinstance(decision, Mapping)
            and decision.get("speaker") == GUEST_SPEAKER
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and float(confidence) >= SINGLETON_CONTEXT_HOST_MIN_CONFIDENCE
        ):
            return {
                "strong": True,
                "reason_code": "HIGH_CONFIDENCE_WHOLE_CLIP_GUEST_CONTEXT",
                "confidence": float(confidence),
                "minimum_confidence": SINGLETON_CONTEXT_HOST_MIN_CONFIDENCE,
            }
    return {"strong": False, "reason_code": "NO_STRONG_GUEST_CONTEXT"}


def identity_indeterminate_analysis(
    *, cue_count: int, reason: str
) -> dict[str, object]:
    """Project a typed identity-only failure onto the ordinary prior seam."""

    return {
        "mode": "speaker_identity_indeterminate",
        "multi_speaker_detected": False,
        "review_required": True,
        "review_reason_codes": ["SPEAKER_IDENTITY_INDETERMINATE"],
        "context_unresolved_cues": list(range(1, cue_count + 1)),
        "identity_indeterminate_reason": reason,
        "decisions": [
            {
                "source_index": index,
                "speaker": GUEST_SPEAKER,
                "decision_source": "speaker_identity_indeterminate",
                "margin": None,
            }
            for index in range(1, cue_count + 1)
        ],
    }


def apply_portrait_solo_prior(
    analysis: Mapping[str, object],
    *,
    context: Mapping[str, object],
    session_context_sha256: str,
) -> dict[str, object]:
    """Resolve only an existing identity-review hold, never other failures.

    F14(Ivan 2026-08-09 竖屏定律):竖屏直播 ⇒ 99% 单人直播。从录制分辨率纵横比
    直接判 session/场级 solo 先验(比任何音频分析都便宜),再叠 roster/语境佐证。
    横屏或 unknown 不进入本函数的 mutation 分支。
    """

    scene = context["segment_scene_context"]
    probe = scene["media_probe"]
    if probe.get("status") != "PASS" or probe.get("orientation") != "portrait":
        return dict(analysis)
    unresolved = analysis.get("context_unresolved_cues")
    if not isinstance(unresolved, list) or not unresolved:
        return dict(analysis)
    decisions = analysis.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        return dict(analysis)
    acoustic = _strong_campp_two_cluster(analysis)
    relation = _relation_counterevidence(context)
    semantic = _context_guest_counterevidence(analysis)
    counterevidence = {
        "campp": acoustic,
        "session_relation": relation,
        "whole_clip_context": semantic,
    }
    vetoes = [
        str(row["reason_code"])
        for row in counterevidence.values()
        if row.get("strong") is True
    ]
    receipt = {
        "schema_version": PRIOR_RECEIPT_SCHEMA,
        "solo_prior": "portrait",
        "source": "source_segment_ffprobe_orientation",
        "source_segment": context["source_segment"],
        "width": probe["width"],
        "height": probe["height"],
        "segment_scene_context_sha256": context["segment_scene_context_sha256"],
        "speaker_session_context_sha256": session_context_sha256,
        "counterevidence": counterevidence,
        "decision": "VETOED" if vetoes else "APPLIED",
        "veto_reason_codes": vetoes,
    }
    if vetoes:
        return {**dict(analysis), "solo_prior_receipt": receipt}
    all_host = [
        {
            **dict(row),
            "speaker": HOST_SPEAKER,
            "decision_source": "portrait_solo_prior",
        }
        for row in decisions
        if isinstance(row, Mapping)
    ]
    if len(all_host) != len(decisions):
        return dict(analysis)
    return {
        **dict(analysis),
        "mode_before_solo_prior": analysis.get("mode"),
        "mode": "portrait_solo_prior",
        "multi_speaker_detected": False,
        "context_unresolved_cues": [],
        "review_required": False,
        "review_reason_codes": [],
        "decisions": all_host,
        "solo_prior": "portrait",
        "solo_prior_receipt": receipt,
    }


def apply_portrait_solo_prior_from_path(
    analysis: Mapping[str, object],
    *,
    context_path: Path | None,
    candidate_id: str | None,
) -> dict[str, object]:
    """Load a no-symlink context atomically; invalid/drifted input is no prior."""

    if context_path is None or not candidate_id:
        return dict(analysis)
    path = Path(context_path)
    try:
        before = path.stat()
        if path.is_symlink() or not path.is_file():
            return dict(analysis)
        payload = path.read_bytes()
        after = path.stat()
        if (
            (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
            != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
        ):
            return dict(analysis)
        context = _validated_context(
            json.loads(payload), expected_candidate_id=str(candidate_id)
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return dict(analysis)
    if context is None:
        return dict(analysis)
    result = apply_portrait_solo_prior(
        analysis,
        context=context,
        session_context_sha256=_sha256_bytes(payload),
    )
    try:
        if path.read_bytes() != payload:
            return dict(analysis)
    except OSError:
        return dict(analysis)
    return result
