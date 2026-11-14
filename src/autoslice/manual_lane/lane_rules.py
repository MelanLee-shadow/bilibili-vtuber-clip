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
    models: dict[str, list[str]]
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
    own_subtitles_share: float
    screen_window_s: float
    on_screen_min: float
    static_text_share: float
    unshown_min_letters: int
    own_time_reach_s: float
    unshown_max_shown: float


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
    subtitle_agent: ProviderLimit


class SubtitleAgentRules(_RuleMetadata, total=False):
    samples: int


class TrimThanksWordsRules(_RuleMetadata, total=False):
    words: list[str]


class ScreenWorkRules(_RuleMetadata, total=False):
    enabled: bool


class GraphContextRules(_RuleMetadata, total=False):
    neighbours_per_entity: int
    lines_per_clip: int
    neighbour_kinds: list[str]
    limits_source: str


class CoverRules(_RuleMetadata, total=False):
    face_target_frac: float
    face_floor_frac: float
    punch_floor_px: int
    face_center_x_frac: float
    chin_margin_frac: float
    text_zone_top_px: int
    text_zone_gap_px: int
    text_line_share: list[float]
    text_safe_width_px: int
    text_stagger_px: int
    first_line_max_px: int
    first_line_min_px: int
    text_layouts: list[str]
    text_layout_recent_covers: int
    column_face_center_x_frac: float
    column_rows_max: int
    column_punch_row_max_chars: int
    column_context_row_max_chars: int
    punch_start_px: int
    punch_line_max_chars: int
    first_line_max_chars: int
    thumb_width_px: int
    layout_order: list[str]
    face_top_min_px: int
    face_top_default_px: int
    layer_top_min_px: int
    sticker_face_center_x_frac: float
    kmx_main_center_x_frac: float
    kmx_x_px: int
    kmx_h_px: int
    kmx_bottom_bleed_px: int
    backgrounds: list[dict[str, JsonValue]]
    background_layer_near_distance: int
    background_layer_max_fused_frac: float
    background_stroke_min_distance: int
    background_recent_covers: int
    background_route_order: list[str]


class AudioUnderSubtitleRules(_RuleMetadata, total=False):
    noise_db: float
    min_subtitle_s: float
    edge_tolerance_s: float


class AutoRules(_RuleMetadata, total=False):
    media_min_bytes: int
    final_keep_days: int


class DiscoverRules(_RuleMetadata, total=False):
    stub_max_bytes: int
    settle_minutes: int
    lone_danmaku_hours: int
    direct_keep_days: int
    auto_not_before: str
    auto_recent_dates: int
    recall_max_candidates: int
    min_confidence: float
    per_segment_soft_cap: int
    base_talk_seats: int


if TYPE_CHECKING:
    RuleSection: TypeAlias = (DetectionRules | ListeningRules | CandidateRules | OrphanRules | SplitRules
                              | SpeakerRules | OtherVoicesRules | EffortRules | NameConsistencyRules | ConcurrencyRules
                              | TrimThanksWordsRules | ScreenWorkRules | SubtitleAgentRules | GraphContextRules | CoverRules | AudioUnderSubtitleRules | DiscoverRules | AutoRules | dict[str, JsonValue])


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
def rules(section: Literal["subtitle_agent"]) -> SubtitleAgentRules: ...
@overload
def rules(section: Literal["graph_context"]) -> GraphContextRules: ...
@overload
def rules(section: Literal["cover"]) -> CoverRules: ...
@overload
def rules(section: Literal["audio_under_subtitle"]) -> AudioUnderSubtitleRules: ...
@overload
def rules(section: Literal["discover"]) -> DiscoverRules: ...
@overload
def rules(section: str) -> RuleSection: ...


def rules(section: str) -> RuleSection:
    raw = _load(os.environ.get("MANUAL_LANE_RULES") or str(RULES_FILE))[section]
    return cast("RuleSection", dict(cast("Mapping[str, JsonValue]", raw)))
