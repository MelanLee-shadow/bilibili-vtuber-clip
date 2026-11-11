"""The lane's rule switches and numbers (assets/manual_lane/lane_rules.json, TASK-28: the e05 coach advice written into
the generator).  One reader: rules(section) returns that section as a dict; MANUAL_LANE_RULES=<path> reads another file
(an experiment, a test).  The file is read once per path."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias, TypedDict, cast, overload

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

RULES_FILE = Path(__file__).resolve().parents[3] / "assets" / "manual_lane" / "lane_rules.json"
SCHEMA = "manual-lane.rules.v1"


class _RuleMetadata(TypedDict, total=False):
    _doc: str
    source: str


class DetectionRules(_RuleMetadata, total=False):
    content_key: bool
    small_words: str
    pronouns_one_class: str
    one_side_empty: bool
    near_name: bool
    latin_unknown: bool
    latin_min_letters: int
    latin_common_words: list[str]
    latin_source: str


class ListeningRules(_RuleMetadata, total=False):
    ladder: str
    whole_witness: str
    escalate_without_reading: bool
    change_needs_listening: bool
    change_source: str


class CandidateRules(_RuleMetadata, total=False):
    gemini_min_overlap: float
    single_source_min_chars: int
    repeat_variants_join_current: bool


class OrphanRules(_RuleMetadata, total=False):
    enabled: bool
    min_content_chars: int
    neighbour_s: float
    repeat_coverage: float


class SplitRules(_RuleMetadata, total=False):
    enabled: bool
    min_content_chars: int


class SpeakerRules(_RuleMetadata, total=False):
    two_person_host_min: float
    multi_person_host_min: float
    multi_person_from: int
    two_person_mid: list[float]
    review_mid: bool


class OtherVoicesRules(_RuleMetadata, total=False):
    screen_frames: int
    screen_min_hits: int
    cue_frame_every_s: float
    cue_frames_max: int


class EffortPurpose(TypedDict, total=False):
    model: str
    effort: str
    max_output_tokens: int
    reason: str
    source: str


class EffortRules(_RuleMetadata, total=False):
    default: str
    purposes: dict[str, EffortPurpose]


class NameConsistencyRules(_RuleMetadata, total=False):
    enabled: bool
    min_syllables: int
    max_syllables: int


class ProviderLimit(TypedDict, total=False):
    limit: int
    source: str


class ConcurrencyRules(_RuleMetadata, total=False):
    cpa: ProviderLimit
    lookup: ProviderLimit
    host_encode: ProviderLimit


class TrimThanksWordsRules(_RuleMetadata, total=False):
    words: list[str]


class ScreenWorkRules(_RuleMetadata, total=False):
    enabled: bool


class GraphContextRules(_RuleMetadata, total=False):
    neighbours_per_entity: int
    lines_per_clip: int
    neighbour_kinds: list[str]
    limits_source: str


if TYPE_CHECKING:
    RuleSection: TypeAlias = (DetectionRules | ListeningRules | CandidateRules | OrphanRules | SplitRules
                              | SpeakerRules | OtherVoicesRules | EffortRules | NameConsistencyRules | ConcurrencyRules
                              | TrimThanksWordsRules | ScreenWorkRules | GraphContextRules | dict[str, JsonValue])


@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, JsonValue]:
    doc = cast("dict[str, JsonValue]", json.loads(Path(path).read_text(encoding="utf-8")))
    if doc.get("schema") != SCHEMA:
        raise ValueError(f"{path}: schema must be {SCHEMA!r}")
    return doc


@overload
def rules(section: Literal["detection"]) -> DetectionRules: ...
@overload
def rules(section: Literal["listening"]) -> ListeningRules: ...
@overload
def rules(section: Literal["candidates"]) -> CandidateRules: ...
@overload
def rules(section: Literal["orphans"]) -> OrphanRules: ...
@overload
def rules(section: Literal["split"]) -> SplitRules: ...
@overload
def rules(section: Literal["speaker"]) -> SpeakerRules: ...
@overload
def rules(section: Literal["other_voices"]) -> OtherVoicesRules: ...
@overload
def rules(section: Literal["effort"]) -> EffortRules: ...
@overload
def rules(section: Literal["name_consistency"]) -> NameConsistencyRules: ...
@overload
def rules(section: Literal["concurrency"]) -> ConcurrencyRules: ...
@overload
def rules(section: Literal["trim_thanks_words"]) -> TrimThanksWordsRules: ...
@overload
def rules(section: Literal["screen_work"]) -> ScreenWorkRules: ...
@overload
def rules(section: Literal["graph_context"]) -> GraphContextRules: ...
@overload
def rules(section: str) -> RuleSection: ...


def rules(section: str) -> RuleSection:
    raw = _load(os.environ.get("MANUAL_LANE_RULES") or str(RULES_FILE))[section]
    return cast("RuleSection", dict(cast("Mapping[str, JsonValue]", raw)))
