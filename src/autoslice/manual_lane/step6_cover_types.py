"""The typed records of step 6's cover (moved from step6_cover.py unchanged; step6_cover re-exports every name)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

    from .cover_guests import CoverRoster
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
    repeat: str | None      # why this frame repeats a cover of this stream (pick_frame.repeat_of), None when it does not
    fallback_from: str
    fallback_reason: str
    fallback_note: str


class FrameAction(TypedDict, total=False):
    t: float | None
    what: str
    why: str


class BaseFrame(TypedDict, total=False):
    """The live frame a cover is made on, as the next covers of the stream avoid it (step6_cover.base_avoid)."""
    cid: str
    source: str | None          # the recording it is cut from
    t_source: float | None      # its time on that recording's clock (None: a cut pieced together has none)
    dhash: str | None           # her face's 16x16 dHash in it (pick_frame.face_hash); None when her face was not read


class BaseAvoid(TypedDict):
    source: str | None          # this clip's recording
    offset_s: float | None      # the cut's start on that recording's clock (None: a cut pieced together)
    covers: list[BaseFrame]     # the base frames of this stream's covers so far


class FrameDocument(TypedDict, total=False):
    action: FrameAction
    times: list[float]
    question: str
    model: str | None
    chosen: float | None
    boxes: dict[str, list[JsonValue]] | None
    reused: bool
    calls: list[FrameCall]
    checked: list[FrameCall]
    avoid: BaseAvoid
    base: dict[str, JsonValue]  # the chosen frame: t, face, dhash (repeat_forced when every frame repeated a cover)


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
    template_history: list[str | None]           # the composition template of every earlier cover, oldest first
    template_session_history: list[str | None]   # the same for this stream's covers
    route_run: str | None                        # the route the last covers all used (cover_repeats)
    session_compositions: list[list[str]]
    guest_asset_history: dict[str, list[str]]


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
    template_id: str | None


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
    guests_on_cover: list[dict[str, JsonValue]]       # the pack's guests the tool put on this candidate (cover_pack_guests.py)
    guest_assets_dropped: list[dict[str, str]]        # pack pictures the media host could not use: name, why, code, src
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
    pixel_audit: dict[str, JsonValue] | None     # local glyph/mask/background replay; not portable-package acceptance
    punch_floor_px: int
    punch_below_floor: bool
    text_plan: dict[str, JsonValue] | None
    crop_plan: dict[str, JsonValue] | None
    render_s: float
    phases_s: dict[str, float]      # where this route's seconds went inside the tool (cover_clock.py)
    thumb: ThumbVerdict | None
    repeat: str | None              # why it repeats the covers before it (step6_cover.cover_repeats), or None
    look_repeat: bool               # it looks like a cover this stream already has (step6_cover.cover_looks_repeated)
    cover_layout: dict[str, JsonValue] | None   # the media-host tool's template selection (cover_layout_rotation.select)
    cover_layout_note: str | None
    on_action: bool             # a screenshot at the host's signature move (step6_cover.on_action)


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
    fallback_from: str
    fallback_reason: str
    fallback_note: str


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
    template_id: str | None                  # the composition template (cover_layout_rotation)
    cover_layout: dict[str, JsonValue] | None   # the selection: the template taken, the ones passed over and why


class CoverRoute(TypedDict):
    method: str
    route_schema: str
    at: str
    recording_date: str | None
    chosen: CoverChoice
    peers: CoverPeers | None
    host_side_reason: str
    guests: dict[str, object]                         # cover_guests' four sets, who is missing, the pack pictures on the cover
    base_frame: NotRequired[BaseFrame]               # the live frame the cover is made on (the stream's next covers avoid it)
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
    template_id: str | None
    base_frame: BaseFrame | None
    guest_assets: dict[str, str]


class CoverAction(TypedDict):
    t: float                    # the cover frame, seconds into the cut
    what: str                   # the move, as the vision reading put it


class CoverPlan(TypedDict, total=False):
    recent: RecentCovers
    emote: CoverEmote | None
    fans_emote: CoverEmote | None
    line: str
    participants: list[str]
    brief: CoverBrief
    rotation: CoverRotation
    peers: CoverPeers
    action_frame: CoverAction | None   # the cover frame is the host's signature move (step6_cover.chosen_action)
    roster: CoverRoster         # the guests this clip's cover must show and where their pictures come from (cover_guests.roster)
    wordy: bool                 # three or more people and words that could not be written short (cover_layout_rotation.punch_floor)


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
