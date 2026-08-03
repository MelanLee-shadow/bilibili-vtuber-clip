"""Shared speaker-finalization policy, labels, and error types."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

from src.autoslice.channel_profile import load_channel_profile

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id
HOST_SPEAKER = CHANNEL_PROFILE.host_speaker_label
GUEST_SPEAKER = CHANNEL_PROFILE.guest_speaker_label
SPEAKERS = {HOST_SPEAKER, GUEST_SPEAKER}
SPEAKER_FINALIZATION_SCHEMA = f"{PROFILE_ID}-speaker-finalization.v1"
FAST_FRESH_DERIVATION_SCHEMA = f"{PROFILE_ID}-speaker-fast-fresh-derivation.v1"
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


class SpeakerFinalizationError(RuntimeError):
    pass


def milliseconds(value: str) -> int:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000 + int(millis)


def speaker_policy(profile: Mapping[str, object]) -> dict[str, float | int]:
    result = dict(DEFAULT_POLICY)
    configured = profile.get("talk_speaker_policy")
    if isinstance(configured, Mapping):
        for key in result:
            value = configured.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = value
    return result
