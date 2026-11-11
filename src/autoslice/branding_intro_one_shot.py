"""Validation of recording-scoped, non-rotating intro bindings."""

from collections.abc import Mapping
from datetime import date
import re
from typing import TypedDict


class BrandingIntroError(RuntimeError):
    """A branding condition that must block delivery."""


class IntroClipIdentity(TypedDict):
    recording_date: str
    candidate_id: str


def clip_identity(value: object) -> tuple[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"recording_date", "candidate_id"}:
        raise BrandingIntroError("branding intro clip identity needs recording_date and candidate_id")
    day, candidate = value["recording_date"], value["candidate_id"]
    if not isinstance(day, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None:
        raise BrandingIntroError("branding intro recording_date must be YYYY-MM-DD")
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise BrandingIntroError("branding intro recording_date is invalid") from exc
    if not isinstance(candidate, str) or not candidate or candidate.strip() != candidate:
        raise BrandingIntroError("branding intro candidate_id is invalid")
    return day, candidate


def one_shot_bindings(entry: object) -> list[tuple[str, str]]:
    if not isinstance(entry, Mapping):
        raise BrandingIntroError("branding intro entry must be an object")
    if "one_shot" not in entry:
        return []
    value = entry["one_shot"]
    if not isinstance(value, Mapping) or set(value) != {"clips"}:
        raise BrandingIntroError("branding intro one_shot must contain clips")
    clips = value["clips"]
    if not isinstance(clips, list) or not clips:
        raise BrandingIntroError("branding intro one_shot clips must be a nonempty list")
    return [clip_identity(clip) for clip in clips]
