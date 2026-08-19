"""Session-stratified centroid--medoid speaker verification in shadow mode.

This module implements the score-only architecture selected in the 
ChatGPT Pro review.  It is deliberately isolated from production speaker,
centrality, selection, and release code.  A missing threshold manifest means
every real-data decision is UNKNOWN even when diagnostic scores are available.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from src.autoslice.speaker_common import CAMPP_EMBEDDING_DIMENSION


SCHEMA_VERSION = "speaker-scmc-shadow.v0"
PURPOSE = "DEVELOPMENT_SCORE_ONLY_NOT_PREDICTION_OR_RELEASE_AUTHORITY"
SAMPLE_RATE_HZ = 16_000
SHORT_MIN_MS = 300
LONG_MIN_MS = 1_500
MIN_HOST_SESSIONS = 3
MIN_HOST_CLIPS_PER_SESSION_STRATUM = 3
MIN_OTHER_CLIPS_PER_STRATUM = 12
OTHER_MEDOIDS_PER_STRATUM = 8
DEFAULT_ADJACENCY_GUARD_SAMPLES = 2 * SAMPLE_RATE_HZ

LABEL_HOST = "HOST"
LABEL_OTHER = "OTHER"
LABEL_UNKNOWN = "UNKNOWN"
STRATUM_SHORT = "SHORT"
STRATUM_LONG = "LONG"
STRATA = (STRATUM_SHORT, STRATUM_LONG)

BANK_SPLIT_ROLES = frozenset(
    {
        "DEVELOPMENT_BANK_REVIEWED",
        "LEGACY_REFERENCE_PROVENANCE_RESOLVED",
    }
)


class ScmcShadowError(ValueError):
    pass


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_sha256(value: str, *, label: str) -> str:
    normalized = value.lower().removeprefix("sha256:")
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ScmcShadowError(f"{label} sha256 is malformed")
    return "sha256:" + normalized


def _normalize(values: Sequence[float]) -> tuple[float, ...]:
    vector = tuple(float(value) for value in values)
    if len(vector) != CAMPP_EMBEDDING_DIMENSION:
        raise ScmcShadowError(
            f"CAM++ embedding must have {CAMPP_EMBEDDING_DIMENSION} values"
        )
    if any(not math.isfinite(value) for value in vector):
        raise ScmcShadowError("CAM++ embedding values must be finite")
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ScmcShadowError("CAM++ embedding norm is degenerate")
    return tuple(value / norm for value in vector)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ScmcShadowError("embedding dimensions do not match")
    score = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    if not math.isfinite(score):
        raise ScmcShadowError("cosine score is not finite")
    return min(1.0, max(-1.0, score))


def duration_stratum(duration_ms: int) -> str | None:
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms <= 0:
        raise ScmcShadowError("duration_ms must be a positive integer")
    if duration_ms < SHORT_MIN_MS:
        return None
    return STRATUM_SHORT if duration_ms < LONG_MIN_MS else STRATUM_LONG


@dataclass(frozen=True)
class EvidenceClip:
    clip_id: str
    speaker: str
    session_group_id: str
    split_role: str
    source_media_sha256: str
    canonical_session_pcm_sha256: str
    canonical_pcm_sha256: str
    embedding_binding_sha256: str
    start_sample: int
    end_sample: int
    duration_ms: int
    embedding: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.clip_id.strip() or not self.session_group_id.strip():
            raise ScmcShadowError("clip and session identities are required")
        if self.speaker not in {LABEL_HOST, LABEL_OTHER}:
            raise ScmcShadowError("bank clips must be clear reviewed HOST or OTHER")
        if self.split_role not in BANK_SPLIT_ROLES:
            raise ScmcShadowError("holdout or unresolved material cannot enter a bank")
        for field_name in (
            "source_media_sha256",
            "canonical_session_pcm_sha256",
            "canonical_pcm_sha256",
            "embedding_binding_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _validate_sha256(getattr(self, field_name), label=field_name),
            )
        if (
            isinstance(self.start_sample, bool)
            or not isinstance(self.start_sample, int)
            or isinstance(self.end_sample, bool)
            or not isinstance(self.end_sample, int)
            or self.start_sample < 0
            or self.end_sample <= self.start_sample
        ):
            raise ScmcShadowError("clip sample bounds are invalid")
        duration_stratum(self.duration_ms)
        decoded_duration_ms = (self.end_sample - self.start_sample) * 1000 // SAMPLE_RATE_HZ
        if abs(decoded_duration_ms - self.duration_ms) > 1:
            raise ScmcShadowError("clip duration does not match sample bounds")
        object.__setattr__(self, "embedding", _normalize(self.embedding))

    @property
    def stratum(self) -> str | None:
        return duration_stratum(self.duration_ms)


@dataclass(frozen=True)
class ViewEvidence:
    view_id: str
    session_group_id: str
    source_media_sha256: str
    canonical_pcm_sha256: str
    start_sample: int
    end_sample: int
    duration_ms: int
    embedding: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.view_id.strip() or not self.session_group_id.strip():
            raise ScmcShadowError("view and session identities are required")
        object.__setattr__(
            self,
            "source_media_sha256",
            _validate_sha256(self.source_media_sha256, label="view source media"),
        )
        object.__setattr__(
            self,
            "canonical_pcm_sha256",
            _validate_sha256(self.canonical_pcm_sha256, label="view canonical PCM"),
        )
        if (
            isinstance(self.start_sample, bool)
            or not isinstance(self.start_sample, int)
            or isinstance(self.end_sample, bool)
            or not isinstance(self.end_sample, int)
            or self.start_sample < 0
            or self.end_sample <= self.start_sample
        ):
            raise ScmcShadowError("view sample bounds are invalid")
        duration_stratum(self.duration_ms)
        decoded_duration_ms = (self.end_sample - self.start_sample) * 1000 // SAMPLE_RATE_HZ
        if abs(decoded_duration_ms - self.duration_ms) > 1:
            raise ScmcShadowError("view duration does not match sample bounds")
        object.__setattr__(self, "embedding", _normalize(self.embedding))

    @property
    def stratum(self) -> str | None:
        return duration_stratum(self.duration_ms)


@dataclass(frozen=True)
class HostSessionRepresentative:
    session_group_id: str
    clip_ids: tuple[str, ...]
    clip_pcm_sha256s: tuple[str, ...]
    centroid: tuple[float, ...]
    medoid: EvidenceClip


@dataclass(frozen=True)
class StratumBank:
    stratum: str
    status: str
    blockers: tuple[str, ...]
    host_sessions: tuple[HostSessionRepresentative, ...]
    other_medoids: tuple[EvidenceClip, ...]


@dataclass(frozen=True)
class ScmcBank:
    clips: tuple[EvidenceClip, ...]
    strata: Mapping[str, StratumBank]

    def manifest(self) -> dict[str, object]:
        strata_payload: dict[str, object] = {}
        for stratum in STRATA:
            bank = self.strata[stratum]
            strata_payload[stratum] = {
                "status": bank.status,
                "blockers": list(bank.blockers),
                "host_sessions": [
                    {
                        "session_group_id": row.session_group_id,
                        "clip_ids": list(row.clip_ids),
                        "clip_pcm_sha256s": list(row.clip_pcm_sha256s),
                        "centroid": [round(value, 12) for value in row.centroid],
                        "medoid_clip_id": row.medoid.clip_id,
                        "medoid_pcm_sha256": row.medoid.canonical_pcm_sha256,
                    }
                    for row in bank.host_sessions
                ],
                "other_medoids": [
                    {
                        "clip_id": clip.clip_id,
                        "session_group_id": clip.session_group_id,
                        "canonical_pcm_sha256": clip.canonical_pcm_sha256,
                    }
                    for clip in bank.other_medoids
                ],
            }
        payload = {
            "schema_version": SCHEMA_VERSION,
            "purpose": PURPOSE,
            "policy": {
                "duration_strata_ms": {"SHORT": [300, 1499], "LONG": [1500, None]},
                "minimum_host_sessions": MIN_HOST_SESSIONS,
                "minimum_host_clips_per_session_stratum": (
                    MIN_HOST_CLIPS_PER_SESSION_STRATUM
                ),
                "minimum_other_clips_per_stratum": MIN_OTHER_CLIPS_PER_STRATUM,
                "other_medoids_per_stratum": OTHER_MEDOIDS_PER_STRATUM,
                "cross_session_support": "strict_majority_order_statistic",
                "other_bank_authority": "VETO_ONLY_NEVER_PROMOTES_HOST",
                "threshold_state": "NULL",
                "hard_labels_emitted": False,
                "promotion_authority": False,
            },
            "strata": strata_payload,
        }
        return {**payload, "deterministic_payload_sha256": _canonical_sha256(payload)}


@dataclass(frozen=True)
class Thresholds:
    tau_other: float
    tau_host: float
    delta_host: float

    def __post_init__(self) -> None:
        values = (float(self.tau_other), float(self.tau_host), float(self.delta_host))
        if any(not math.isfinite(value) for value in values):
            raise ScmcShadowError("thresholds must be finite")
        if not -1.0 <= self.tau_other < self.tau_host <= 1.0:
            raise ScmcShadowError("thresholds require -1 <= tau_other < tau_host <= 1")
        if self.delta_host <= 0.0 or self.delta_host > 2.0:
            raise ScmcShadowError("delta_host must be in (0, 2]")


def _validate_bank_identity(clips: Sequence[EvidenceClip]) -> tuple[EvidenceClip, ...]:
    if not clips:
        raise ScmcShadowError("speaker bank is empty")
    ordered = tuple(
        sorted(
            clips,
            key=lambda clip: (
                clip.session_group_id,
                clip.speaker,
                clip.canonical_pcm_sha256,
                clip.start_sample,
                clip.clip_id,
            ),
        )
    )
    if len({clip.clip_id for clip in ordered}) != len(ordered):
        raise ScmcShadowError("bank clip identities are duplicated")
    if len({clip.canonical_pcm_sha256 for clip in ordered}) != len(ordered):
        raise ScmcShadowError("bank contains duplicated or re-encoded clip audio")

    session_by_pcm: dict[str, str] = {}
    session_by_media: dict[str, str] = {}
    session_pcm_by_group: dict[str, str] = {}
    for clip in ordered:
        prior = session_by_pcm.setdefault(
            clip.canonical_session_pcm_sha256, clip.session_group_id
        )
        if prior != clip.session_group_id:
            raise ScmcShadowError("pseudo-independent session PCM was counted twice")
        prior = session_by_media.setdefault(clip.source_media_sha256, clip.session_group_id)
        if prior != clip.session_group_id:
            raise ScmcShadowError("one source media was assigned to multiple sessions")
        prior = session_pcm_by_group.setdefault(
            clip.session_group_id, clip.canonical_session_pcm_sha256
        )
        if prior != clip.canonical_session_pcm_sha256:
            raise ScmcShadowError("one session group has conflicting canonical PCM identities")
    return ordered


def _session_representative(
    session_group_id: str, clips: Sequence[EvidenceClip]
) -> HostSessionRepresentative:
    ordered = tuple(
        sorted(
            clips,
            key=lambda clip: (clip.canonical_pcm_sha256, clip.start_sample, clip.clip_id),
        )
    )
    centroid = _normalize(
        [sum(clip.embedding[index] for clip in ordered) for index in range(CAMPP_EMBEDDING_DIMENSION)]
    )
    medoid = min(
        ordered,
        key=lambda clip: (
            -_cosine(centroid, clip.embedding),
            clip.canonical_pcm_sha256,
            clip.start_sample,
            clip.clip_id,
        ),
    )
    return HostSessionRepresentative(
        session_group_id=session_group_id,
        clip_ids=tuple(clip.clip_id for clip in ordered),
        clip_pcm_sha256s=tuple(clip.canonical_pcm_sha256 for clip in ordered),
        centroid=centroid,
        medoid=medoid,
    )


def _select_other_medoids(clips: Sequence[EvidenceClip]) -> tuple[EvidenceClip, ...]:
    ordered = tuple(
        sorted(
            clips,
            key=lambda clip: (clip.canonical_pcm_sha256, clip.start_sample, clip.clip_id),
        )
    )
    if len(ordered) < MIN_OTHER_CLIPS_PER_STRATUM:
        return ()

    def distance(left: EvidenceClip, right: EvidenceClip) -> float:
        return 1.0 - _cosine(left.embedding, right.embedding)

    selected: list[EvidenceClip] = []
    while len(selected) < OTHER_MEDOIDS_PER_STRATUM:
        candidate = min(
            (clip for clip in ordered if clip not in selected),
            key=lambda clip: (
                sum(
                    min(
                        [distance(row, clip)]
                        + [distance(row, chosen) for chosen in selected]
                    )
                    for row in ordered
                ),
                clip.canonical_pcm_sha256,
                clip.start_sample,
                clip.clip_id,
            ),
        )
        selected.append(candidate)
    return tuple(selected)


def build_bank(clips: Sequence[EvidenceClip]) -> ScmcBank:
    ordered = _validate_bank_identity(clips)
    strata: dict[str, StratumBank] = {}
    for stratum in STRATA:
        host_by_session: dict[str, list[EvidenceClip]] = {}
        other: list[EvidenceClip] = []
        for clip in ordered:
            if clip.stratum != stratum:
                continue
            if clip.speaker == LABEL_HOST:
                host_by_session.setdefault(clip.session_group_id, []).append(clip)
            else:
                other.append(clip)

        blockers: list[str] = []
        representatives: list[HostSessionRepresentative] = []
        for session_group_id, rows in sorted(host_by_session.items()):
            if len(rows) < MIN_HOST_CLIPS_PER_SESSION_STRATUM:
                blockers.append(f"HOST_SESSION_TOO_SMALL:{session_group_id}")
                continue
            representatives.append(_session_representative(session_group_id, rows))
        if len(representatives) < MIN_HOST_SESSIONS:
            blockers.append("INSUFFICIENT_INDEPENDENT_HOST_SESSIONS")

        other_medoids = _select_other_medoids(other)
        if not other_medoids:
            blockers.append("INSUFFICIENT_REVIEWED_OTHER_CLIPS")
        strata[stratum] = StratumBank(
            stratum=stratum,
            status="READY" if not blockers else "INSUFFICIENT_BANK_SUPPORT",
            blockers=tuple(blockers),
            host_sessions=tuple(representatives),
            other_medoids=other_medoids,
        )
    return ScmcBank(clips=ordered, strata=strata)


def _intervals_too_close(
    *,
    left_start: int,
    left_end: int,
    right_start: int,
    right_end: int,
    guard_samples: int,
) -> bool:
    return left_start < right_end + guard_samples and right_start < left_end + guard_samples


def _reject_view_leakage(
    view: ViewEvidence,
    bank: ScmcBank,
    *,
    allow_same_session_crossfit: bool,
    adjacency_guard_samples: int,
) -> None:
    if isinstance(adjacency_guard_samples, bool) or adjacency_guard_samples < 0:
        raise ScmcShadowError("adjacency guard must be a nonnegative sample count")
    for clip in bank.clips:
        if clip.canonical_pcm_sha256 == view.canonical_pcm_sha256:
            raise ScmcShadowError("evaluated audio is present in the speaker bank")
        if clip.session_group_id == view.session_group_id and not allow_same_session_crossfit:
            raise ScmcShadowError("same-session bank use requires blocked cross-fitting")
        if (
            clip.source_media_sha256 == view.source_media_sha256
            and _intervals_too_close(
                left_start=view.start_sample,
                left_end=view.end_sample,
                right_start=clip.start_sample,
                right_end=clip.end_sample,
                guard_samples=adjacency_guard_samples,
            )
        ):
            raise ScmcShadowError("evaluated or adjacent source audio intersects the bank")


def score_view(
    view: ViewEvidence,
    bank: ScmcBank,
    *,
    thresholds: Thresholds | None = None,
    allow_same_session_crossfit: bool = False,
    adjacency_guard_samples: int = DEFAULT_ADJACENCY_GUARD_SAMPLES,
) -> dict[str, object]:
    """Return diagnostic H/N/M evidence; NULL thresholds always abstain."""

    _reject_view_leakage(
        view,
        bank,
        allow_same_session_crossfit=allow_same_session_crossfit,
        adjacency_guard_samples=adjacency_guard_samples,
    )
    if view.stratum is None:
        return {
            "view_id": view.view_id,
            "stratum": None,
            "status": "CUE_TOO_SHORT",
            "threshold_state": "NULL" if thresholds is None else "SYNTHETIC_TEST_ONLY",
            "decision": LABEL_UNKNOWN,
            "promotion_authority": False,
        }
    stratum_bank = bank.strata[view.stratum]
    if stratum_bank.status != "READY":
        return {
            "view_id": view.view_id,
            "stratum": view.stratum,
            "status": "INSUFFICIENT_BANK_SUPPORT",
            "blockers": list(stratum_bank.blockers),
            "threshold_state": "NULL" if thresholds is None else "SYNTHETIC_TEST_ONLY",
            "decision": LABEL_UNKNOWN,
            "promotion_authority": False,
        }

    session_supports = {
        representative.session_group_id: min(
            _cosine(view.embedding, representative.centroid),
            _cosine(view.embedding, representative.medoid.embedding),
        )
        for representative in stratum_bank.host_sessions
    }
    required_votes = len(session_supports) // 2 + 1
    host_evidence = sorted(session_supports.values(), reverse=True)[required_votes - 1]
    other_veto = max(
        _cosine(view.embedding, clip.embedding) for clip in stratum_bank.other_medoids
    )
    host_margin = host_evidence - other_veto
    decision = LABEL_UNKNOWN
    if thresholds is not None:
        if host_evidence >= thresholds.tau_host and host_margin >= thresholds.delta_host:
            decision = LABEL_HOST
        elif host_evidence <= thresholds.tau_other:
            decision = LABEL_OTHER
    return {
        "view_id": view.view_id,
        "stratum": view.stratum,
        "status": "SCORED_THRESHOLD_NULL" if thresholds is None else "SCORED_SYNTHETIC_TEST",
        "threshold_state": "NULL" if thresholds is None else "SYNTHETIC_TEST_ONLY",
        "session_supports": {
            key: round(value, 12) for key, value in sorted(session_supports.items())
        },
        "required_session_votes": required_votes,
        "host_evidence": round(host_evidence, 12),
        "other_veto": round(other_veto, 12),
        "host_margin": round(host_margin, 12),
        "decision": decision,
        "promotion_authority": False,
    }


def combine_cue_decisions(
    *,
    duration_ms: int,
    mixed: bool,
    whole_view: Mapping[str, object],
    speech_cells: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Combine already-scored views without turning conflict into confidence."""

    stratum = duration_stratum(duration_ms)
    if mixed:
        return {"decision": LABEL_UNKNOWN, "reason": "MIXED_CUE", "promotion_authority": False}
    if stratum is None:
        return {
            "decision": LABEL_UNKNOWN,
            "reason": "CUE_TOO_SHORT",
            "promotion_authority": False,
        }
    whole_decision = str(whole_view.get("decision") or LABEL_UNKNOWN)
    if whole_decision not in {LABEL_HOST, LABEL_OTHER, LABEL_UNKNOWN}:
        raise ScmcShadowError("whole-view decision is invalid")
    if duration_ms < LONG_MIN_MS:
        return {
            "decision": whole_decision,
            "reason": None if whole_decision != LABEL_UNKNOWN else "WHOLE_VIEW_ABSTAINED",
            "promotion_authority": False,
        }

    cell_decisions = [str(cell.get("decision") or LABEL_UNKNOWN) for cell in speech_cells]
    if any(
        decision not in {LABEL_HOST, LABEL_OTHER, LABEL_UNKNOWN}
        for decision in cell_decisions
    ):
        raise ScmcShadowError("speech-cell decision is invalid")
    valid = [decision for decision in cell_decisions if decision in {LABEL_HOST, LABEL_OTHER}]
    if not valid:
        return {
            "decision": LABEL_UNKNOWN,
            "reason": "NO_VALID_SPEECH_CELL",
            "promotion_authority": False,
        }
    host_count = valid.count(LABEL_HOST)
    other_count = valid.count(LABEL_OTHER)
    if whole_decision == LABEL_HOST and other_count == 0 and host_count > len(valid) // 2:
        decision, reason = LABEL_HOST, None
    elif whole_decision == LABEL_OTHER and host_count == 0 and other_count > len(valid) // 2:
        decision, reason = LABEL_OTHER, None
    else:
        decision, reason = LABEL_UNKNOWN, "WHOLE_OR_CELL_CONFLICT"
    return {"decision": decision, "reason": reason, "promotion_authority": False}
