from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from src.autoslice.review_evidence import ReviewEvidence


@dataclass(frozen=True)
class ManualStyleProfile:
    profile_id: str
    sample_count: int
    preferred_duration_seconds: Mapping[str, float]
    title_hook_patterns: Sequence[str]
    kept_content_types: Sequence[str]
    intro_outro_tolerance: str
    subtitle_density_range: tuple[float, float]
    danmaku_density_range: tuple[float, float]
    negative_patterns: Sequence[str]


@dataclass(frozen=True)
class StyleMatchScore:
    profile_id: str
    style_match_score: float
    reasons: tuple[str, ...]


def score_style_match(
    evidence: ReviewEvidence,
    profile: ManualStyleProfile,
    *,
    title: str,
    duration_seconds: float,
) -> StyleMatchScore:
    score = 58.0
    reasons: list[str] = []
    full_song_ready = (
        evidence.foreground_song_overlap_seconds is not None
        and evidence.foreground_song_overlap_seconds > 5.0
        and evidence.song_complete is True
        and evidence.lyrics_alignment_ready is True
    )

    if any(pattern in title for pattern in profile.title_hook_patterns):
        score += 14.0
        reasons.append("hook_title_pattern")
    else:
        score -= 5.0
        reasons.append("generic_title")

    preferred = profile.preferred_duration_seconds
    p25 = float(preferred.get("p25", 20.0))
    p75 = float(preferred.get("p75", 120.0))
    if full_song_ready:
        score += 10.0
        reasons.append("complete_song_duration_accepted")
    elif p25 <= duration_seconds <= p75:
        score += 10.0
        reasons.append("duration_in_manual_iqr")
    else:
        distance = min(abs(duration_seconds - p25), abs(duration_seconds - p75))
        score -= min(14.0, distance / 10.0)
        reasons.append("duration_outside_manual_iqr")

    payoff = evidence.payoff_score if evidence.payoff_score is not None else 0.0
    if payoff >= 0.90:
        score += 12.0
        reasons.append("clear_payoff")
    else:
        score -= 22.0
        reasons.append("missing_payoff")

    standalone = evidence.standalone_score if evidence.standalone_score is not None else 0.0
    if standalone >= 0.90:
        score += 6.0
        reasons.append("standalone_context")
    else:
        score -= 10.0
        reasons.append("contextless_fragment")

    if evidence.start_boundary_score is not None:
        score += max(-8.0, (evidence.start_boundary_score - 0.92) * 40.0)
    if evidence.end_boundary_score is not None:
        score += max(-8.0, (evidence.end_boundary_score - 0.95) * 40.0)
    if evidence.foreground_song_overlap_seconds and evidence.foreground_song_overlap_seconds > 5.0 and evidence.song_complete is False:
        score -= 25.0
        reasons.append("song_cut")

    return StyleMatchScore(
        profile_id=profile.profile_id,
        style_match_score=round(max(0.0, min(100.0, score)), 2),
        reasons=tuple(dict.fromkeys(reasons)),
    )


def apply_style_profile(
    evidence: ReviewEvidence,
    profile: ManualStyleProfile | None,
    *,
    title: str,
    duration_seconds: float,
) -> ReviewEvidence:
    if profile is None:
        return replace(
            evidence,
            editorial_score=None,
            evidence_gaps=tuple(dict.fromkeys(tuple(evidence.evidence_gaps) + ("STYLE_PROFILE_MISSING",))),
            metadata={**dict(evidence.metadata), "style_profile": None},
        )

    style_score = score_style_match(evidence, profile, title=title, duration_seconds=duration_seconds)
    checks = tuple(evidence.checks) + (
        {
            "code": "MANUAL_STYLE_PROFILE_MATCH",
            "pass": style_score.style_match_score >= 82.0,
            "style_match_score": style_score.style_match_score,
            "reasons": list(style_score.reasons),
            "profile_id": profile.profile_id,
        },
    )
    return replace(
        evidence,
        editorial_score=style_score.style_match_score,
        checks=checks,
        metadata={
            **dict(evidence.metadata),
            "style_profile": {
                "profile_id": profile.profile_id,
                "sample_count": profile.sample_count,
                "score_reasons": list(style_score.reasons),
            },
        },
    )
