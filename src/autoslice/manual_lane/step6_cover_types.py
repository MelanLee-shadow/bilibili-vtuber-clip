"""The typed records of step 6's cover (moved from step6_cover.py unchanged; step6_cover re-exports every name)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

    from .cover_thumb import ThumbVerdict
    from .state import CpaEvent


class FrameCall(TypedDict, total=False):
    kind: str
    t: float | None
    verdict: str
    rc: int | None
    model: str | None
    elapsed_s: float | None
    usage: JsonValue


class FrameDocument(TypedDict, total=False):
    times: list[float]
    question: str
    chosen: float | None
    boxes: dict[str, list[JsonValue]] | None
    reused: bool
    calls: list[FrameCall]
    checked: list[FrameCall]


class CoverBrief(TypedDict, total=False):
    quotes: list[str]
    expression: str
    action: str
    scene_needed: bool
    scene_reason: JsonValue
    setting: JsonValue
    on_fans: bool
    column_rows: JsonValue      # the cover lines broken into a column's rows, as the brief proposed them
    _cpa: CpaEvent
    _failed: str
    _detail: str


class CoverPeers(TypedDict, total=False):
    peers: bool
    names: list[str]
    source: str


class CoverRotation(TypedDict, total=False):
    recent_style_ids: list[str]
    recent_background_ids: list[str]
    session_background_ids: list[str]
    previous_host_side: str | None
    recent_layouts: list[str | None]
    session_compositions: list[list[str]]


class CoverEmote(TypedDict, total=False):
    id: str
    label: str
    hd: str
    mask: str


class CoverRender(TypedDict, total=False):
    style_id: str | None
    layout: str | None
    host_side: str | None
    colors: list[str] | None
    composition: str | None


class CoverPeople(TypedDict, total=False):
    expected: list[str]
    present: list[str]


class CoverVerdict(TypedDict, total=False):
    people: CoverPeople
    face: JsonValue


class CoverScore(TypedDict, total=False):
    status: str
    vetoes: list[str]
    P: int
    T: int
    H: int
    C: int
    V: int
    total: int


class Generation(TypedDict, total=False):
    model: str | None
    reported_model: str | None
    reused: bool


class CoverCandidate(TypedDict, total=False):
    id: str
    route: str
    status: str
    failure: str | None
    judge_error: str | None
    verdict: CoverVerdict | None
    render: CoverRender | None
    punch_font_size: int | None
    main_emote: str | None
    note: str | None
    answer: JsonValue
    face_measured: JsonValue
    guest_face_measured: JsonValue
    guest_faces_measured: JsonValue
    expected_guest_count: int
    face_method: JsonValue
    final_path: str
    score: CoverScore
    generation: dict[str, Generation]
    face_status: str                 # MEASURED | UNMEASURED (cover_pick.face_view)
    face_source: str | None
    face_px: int | None
    face_frac: float | None
    punch_ink_h: int | None
    background: dict[str, JsonValue] | None    # the pastel background chosen for this candidate and why (cover_background.py)
    punch_floor_px: int
    punch_below_floor: bool
    text_plan: dict[str, JsonValue] | None
    crop_plan: dict[str, JsonValue] | None
    render_s: float
    phases_s: dict[str, float]      # where this route's seconds went inside the tool (cover_clock.py)
    thumb: ThumbVerdict | None
    repeat: bool


class CoverCandidateCall(TypedDict, total=False):
    kind: str
    candidate: str
    selected_model: str | None
    attempted_models: list[str] | None
    reported_model: str | None
    model: str | None
    rc: int | None
    elapsed_s: float | None
    status: str | None
    usage: JsonValue
    reason_code: str | None


class CoverCandidateResult(TypedDict, total=False):
    rows: list[CoverCandidate]
    calls: list[CoverCandidateCall]


class CoverSummary(TypedDict):
    id: str
    route: str
    status: str
    score: CoverScore
    note: str | None


class CoverChoice(TypedDict):
    id: str
    route: str
    main_emote: str | None
    score: CoverScore
    style_id: str | None
    layout: str | None
    host_side: str | None
    colors: list[str] | None
    background_id: str | None
    punch_font_size: int | None
    composition_key: list[str]


class CoverRoute(TypedDict):
    method: str
    route_schema: str
    at: str
    recording_date: str | None
    chosen: CoverChoice
    peers: CoverPeers | None
    host_side_reason: str
    image_generation_used: bool
    image_gen_model: str
    reported_model: str | None
    generation: dict[str, Generation]
    candidates: list[CoverSummary]
    choose: str
    sheet: str
    material_flag: NotRequired[dict[str, object]]    # cover_redo.material_flag: chosen although its material was vetoed
    frame_s: NotRequired[float]                      # the frame the chosen cover was made on, when a redo changed it


class CoverProducts(TypedDict, total=False):
    cover: str
    choose: str
    route: CoverRoute


class RecentCovers(TypedDict):
    previous_route: str | None
    last_five_main_emotes: list[str]
    source_session_main_emotes: list[str]


class RecentCover(TypedDict):
    cid: str
    at: str
    route: str
    main_emote: str | None
    recording_date: str | None
    style_id: str | None
    host_side: str | None
    layout: str | None
    composition_key: list[str] | None
    background_id: str | None


class CoverPlan(TypedDict, total=False):
    recent: RecentCovers
    emote: CoverEmote | None
    fans_emote: CoverEmote | None
    line: str
    participants: list[str]
    brief: CoverBrief
    rotation: CoverRotation
    peers: CoverPeers
    text_layout: str            # the text layout this cover takes its turn at (cover_column.next_layout)


@dataclass
class CoverRenderResult:
    """Internal render result; only the caller's final StepOutcome is persisted."""

    ok: bool
    reason: str = ""
    products: CoverProducts = field(default_factory=lambda: cast(CoverProducts, {}))
    cpa: list[CpaEvent] = field(default_factory=list)
    note: str = ""
    process: list[str] = field(default_factory=list)
    rewind: tuple[str, ...] = ()
