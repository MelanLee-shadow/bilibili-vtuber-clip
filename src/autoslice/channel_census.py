"""Channel census: coarse per-window scorecard grades -> where-to-look clues.

Offline generator and optional recall input (design: ``docs/design/channel-knowledge.md``).
Transcription/window labeling is available through the offline CLI. Only recall
consumes the generated asset; scoring and release gates do not read it.

The census measures recorded streams with the *same* dimensions that
``selection_scorecard`` uses for candidates, so "what makes a good slice" keeps
exactly one definition.  A census never produces a Tier, a score, or a
``candidate_quality`` verdict.  Its only product besides a report is a
``CensusClueSet``: observation questions telling the recall prompt where to
look and how to recognize a payoff.  维护者 (H47) forbids turning the
census into category weights, penalties, exclusions or quotas; that ban is
structural here: no clue type has a field able to carry such a number.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Literal, Protocol, TypeAlias, cast, get_args

from src.autoslice.selection_scorecard import DIMENSION_WEIGHTS

CENSUS_WINDOW_SCHEMA = "channel-census-window.v1"
CLUE_SET_SCHEMA = "channel-census-clues.v1"
GENERATOR_VERSION = "channel-census-clues.v1"
CENSUS_CLUES_ASSET_KEY = "selection_census_clues"
CENSUS_CONFIG_ASSET_KEY = "channel_census_config"
CENSUS_CONFIG_SCHEMA = "channel-census-config.v1"
MAX_CENSUS_INPUT_CHARS = 2500
CensusJSON: TypeAlias = None | bool | int | float | str | list["CensusJSON"] | dict[str, "CensusJSON"]

# A five-minute window is not a candidate: whether a stranger can follow a
# clip depends on the final boundary, so ``self_contained`` is never graded.
WINDOW_UNGRADABLE_DIMENSIONS: frozenset[str] = frozenset({"self_contained"})
# Derived from the scorecard, never restated (one definition of the dimensions).
CENSUS_WINDOW_DIMENSIONS: tuple[str, ...] = tuple(
    name for name in DIMENSION_WEIGHTS if name not in WINDOW_UNGRADABLE_DIMENSIONS
)
# "Something lands in this window" == the metric's own payoff level 2
# ("明确但普通的收口") or above.  No second notion of a good slice.
PAYOFF_DIMENSION = "comedic_payoff"
PAYOFF_MIN_GRADE = 2
# Interaction/interest dimensions whose signals the cross-activity clue tracks;
# host-intrinsic dimensions get their own look-for clues.
INTERACTION_DIMENSIONS: tuple[str, ...] = (
    "relationship_interaction",
    "audience_salience",
    "persona_reversal",
)

# Emission thresholds decide only whether a clue is written at all.  They are
# generator constants, not weights, and never reach a scorecard.
MIN_SUPPORT_WINDOWS = 30
MIN_ACTIVITY_SUPPORT_WINDOWS = 10
EMBEDDED_SHARE_NUMERATOR = 1
EMBEDDED_SHARE_DENOMINATOR = 2
LOOKFOR_TOP_RECOGNITION_CUES = 5

# Window grade == the metric's 0..3 wording verbatim.  Level 4 needs a
# candidate-level complete evidence chain and cannot be given to a window.
CoarseGrade = Literal[0, 1, 2, 3]
GradeBasis = Literal["CENSUS_LABEL", "LEGACY_LABEL_PROXY"]
# Where in the stream the host is (location axis).  Value lives only in the
# dimensions; legacy content-value labels (ADDRESS/RELATION/YURI) are retired.
HostActivity = Literal[
    "SINGING",
    "GAMEPLAY",
    "MEDIA",
    "VIEWER_QA",
    "FREE_TALK",
    "COLLAB",
    "STREAM_OPERATIONS",
    "NO_SPEECH",
    "OTHER",
    "UNSPECIFIED",  # legacy adapter only: old primary was a value label
]
TriggerKind = Literal[
    "VIEWER_CHAT",
    "PAID_MESSAGE",
    "GUEST",
    "HOST_SELF",
    "MEDIA_OR_GAME_EVENT",
    "NONE",
    "UNKNOWN",
]
# Closed set of "where to look / how to recognize" clue kinds.  There is
# deliberately no member that excludes, demotes, caps or prioritizes anything.
ClueKind = Literal[
    "CROSS_ACTIVITY_EMBEDDED",
    "DIMENSION_LOOKFOR",
    "ENTITY_RECOGNITION",
    "ACTIVITY_COVERAGE",
]
CensusCluesStatus = Literal["ABSENT", "INVALID", "PROFILE_MISMATCH", "VALID"]

# A later test walks ``dataclasses.fields`` of every type below and fails if a
# field name contains one of these fragments or if any field is a float.
FORBIDDEN_FIELD_NAME_FRAGMENTS: tuple[str, ...] = (
    "weight",
    "score",
    "bonus",
    "penalty",
    "quota",
    "exclude",
    "limit",
    "rank",
    "priority",
    "tier",
)
# ``lint_clue_text`` rejects clue text carrying a selection directive.  The
# fixed preamble is stored separately because it must say "不加减分".
FORBIDDEN_DIRECTIVE_PHRASES: tuple[str, ...] = (
    "加分",
    "扣分",
    "减分",
    "排除",
    "不选",
    "少选",
    "配额",
    "限额",
    "最多",
    "降档",
    "降级",
    "优先选",
)
CLUE_PREAMBLE_ZH = (
    "以下只是去哪找、怎样认出看点的线索，不进入评分算术，不按活动或题材加减分、"
    "排除或限额；普查频率不能代替本次候选的片内证据。"
)
DUPLICATE_HANDWRITTEN_SECTION_REASON = "DUPLICATE_HANDWRITTEN_CLUE_SECTION"
HANDWRITTEN_CLUE_SECTION_HEADING = "## 去哪找：李豆沙的看点线索（2026-10-25 录播普查）"

# Declaration order is descriptive and stable, never a value ordering.
ACTIVITY_ORDER: tuple[HostActivity, ...] = get_args(HostActivity)
ACTIVITY_LABELS_ZH: dict[HostActivity, str] = {
    "SINGING": "唱歌", "GAMEPLAY": "游戏", "MEDIA": "看作品或聊作品",
    "VIEWER_QA": "读弹幕问答", "FREE_TALK": "日常闲聊", "COLLAB": "联动",
    "STREAM_OPERATIONS": "直播事务", "NO_SPEECH": "无话语或纯音乐",
    "OTHER": "其他活动", "UNSPECIFIED": "活动未标明",
}
LEGACY_PRIMARY_ACTIVITIES: dict[str, HostActivity] = {
    "SING": "SINGING", "GAME": "GAMEPLAY", "MEDIA": "MEDIA", "VIEWER_QA": "VIEWER_QA",
    "DAILY": "FREE_TALK", "COLLAB": "COLLAB", "STREAM_OPS": "STREAM_OPERATIONS",
    "SILENCE_OR_MUSIC": "NO_SPEECH", "OTHER": "OTHER", "ADDRESS": "UNSPECIFIED",
    "RELATION": "UNSPECIFIED", "YURI": "UNSPECIFIED",
}
CLUE_RENDER_ORDER = (
    "cross_activity_embedded", "lookfor_stance_intensity", "lookfor_lidousha_centrality",
    "lookfor_audience_salience", "entity_recognition", "lookfor_relationship_interaction",
    "lookfor_persona_reversal", "activity_coverage",
)
CLUE_TEMPLATES: dict[str, str] = {'CROSS_ACTIVITY_EMBEDDED': '在{acts}中，人物互动、受众关心的题材和反差这类信号多半不是该段的主内容：留意什么触发了主播转入自己的故事或互动、主播怎样回应、片内怎样兑现，沿这一事件找完整上下文。活动名称和关系词只帮助定位，不决定价值；同一段里的其他独立事件也照常比较，普查窗口不是候选边界。',
 'stance_intensity': '主播讲经历、做选择或评论作品时，找主播明确喜欢、拒绝、坚持或改口的地方，并说清具体对象、理由或后果；一次清晰观点也可以兑现题目。情绪强或出现“我”不等于本人立场，朗读、引述和角色台词按原主语区分。',
 'audience_salience': '遇到{interests}相关内容时，找主播偏爱什么、为什么这样解释、怎样回应观众或当场对象，并分清看点来自作品设定、主播的观点还是本次互动；作品里的关系、假设的配对和亲昵称呼保持原语境，不写成现实中已经发生的事。',
 'relationship_interaction': '出现「{terms}」等称呼或来回互动时，看是谁提出、主播怎样接住或拒绝、之后有没有新的回应与收束；单个称呼、普通撒娇和只有口头动作不替代完整证据；若有反差，指出片内前后的依据。',
 'lidousha_centrality': '找这一段里只有主播本人会这样做、这样说的选择和反应，以及它们对事件结果的作用；表层身份符号或自我玩梗本身不够。',
 'persona_reversal': '找主播前后表现或人设出现落差的地方，并指出片内前后的依据；普通可爱、一句惊讶或口头说法不算。',
 'ENTITY_RECOGNITION': '用当前输入里实际出现的人名和称呼辨认谁在对谁说什么，找具体的相处事件、误会、选择和回应；只读到名字或谢礼物不能补出关系链，同名、近音或称呼对应的身份未定就保留未知，理解主事件所需的身份和规则要在片内交代。',
 'ACTIVITY_COVERAGE': '{acts}里都出现过有兑现的段落：设备、排期、答谢、上下播等直播事务和日常里若有误会、立场、完整故事或有收束的互动，按实际事件判断；游戏里的失败、协作和结果同样看主播的具体选择与反应；没有人物、关系或题材信号的段落也有完整故事。活动类别本身不给价值结论。'}


@dataclass(frozen=True)
class RecordingRef:
    """One input recording, identified by bytes rather than by path."""

    basename: str
    sha256: str
    duration_ms: int
    recording_date: str | None  # ISO date; None for externally sourced VODs


@dataclass(frozen=True)
class CensusInputManifest:
    """What was measured, how, and where (the host is data, not a default)."""

    census_id: str
    profile_id: str
    recordings: tuple[RecordingRef, ...]
    transcript_algorithm: str
    window_seconds: int
    label_prompt_sha256: str
    label_model: str
    executed_on_host: str
    created_at: str
    # Legacy pilot identity was the canonical sorted list of source-file digests.
    # Only legacy-proxy windows may use this compatibility provenance.
    legacy_label_file_sha256: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class DimensionGrade:
    """``grade=None`` means not measured; it is never coerced to 0."""

    dimension: str
    grade: CoarseGrade | None
    basis: GradeBasis


@dataclass(frozen=True)
class CensusWindow:
    """One labeled five-minute window (``CENSUS_WINDOW_SCHEMA``)."""

    window_id: str
    recording_group: str
    start_s: int
    end_s: int
    asr_missing: bool
    input_char_count: int
    host_activity: HostActivity  # the activity taking most of the window
    secondary_activities: tuple[HostActivity, ...]  # other activities present
    topic: str
    grades: tuple[DimensionGrade, ...]
    audience_interest_hits: tuple[str, ...]
    persons: tuple[str, ...]
    address_terms: tuple[str, ...]
    trigger_kinds: tuple[TriggerKind, ...]
    quoted_or_roleplay: bool | None
    summary: str


@dataclass(frozen=True)
class AudienceInterest:
    """Machine index of an interest the channel metric already describes.

    ``label_zh`` must occur verbatim in the metric's audience-salience
    paragraph; the metric stays the editorial authority.
    """

    interest_id: str
    label_zh: str
    description_zh: str


@dataclass(frozen=True)
class CensusConfig:
    """``assets/<profile>/channel_census_config.v1.json``."""

    profile_id: str
    audience_interests: tuple[AudienceInterest, ...]
    metric_dimension_section_heading: str
    metric_audience_paragraph_marker: str


@dataclass(frozen=True)
class EvidenceRatio:
    """Descriptive window count only; rendered in the report, never the prompt."""

    numerator: int
    denominator: int
    definition_zh: str


@dataclass(frozen=True)
class CensusClue:
    """A where-to-look prior.  No field can express a weight or an exclusion."""

    clue_id: str
    kind: ClueKind
    dimension_pointers: tuple[str, ...]
    host_activities: tuple[HostActivity, ...]
    observation_question: str
    recognition_cues: tuple[str, ...]
    evidence: EvidenceRatio
    example_window_ids: tuple[str, ...]
    measurement_basis: GradeBasis

    def __post_init__(self) -> None:
        if self.kind not in get_args(ClueKind):
            raise ValueError("INVALID_CLUE_KIND")
        if self.measurement_basis not in get_args(GradeBasis):
            raise ValueError("INVALID_MEASUREMENT_BASIS")
        if any(d not in CENSUS_WINDOW_DIMENSIONS for d in self.dimension_pointers):
            raise ValueError("INVALID_DIMENSION_POINTER")
        if any(a not in get_args(HostActivity) for a in self.host_activities):
            raise ValueError("INVALID_HOST_ACTIVITY")
        if any(type(n) is not int or n < 0 for n in
               (self.evidence.numerator, self.evidence.denominator)):
            raise ValueError("INVALID_EVIDENCE_COUNT")
        for text in (self.observation_question, *self.recognition_cues):
            _validate_prompt_text(text)


@dataclass(frozen=True)
class ClueProvenance:
    census_id: str
    census_date: str
    input_sha256: str
    generator_version: str
    label_prompt_sha256: str
    window_count: int
    valid_window_count: int


@dataclass(frozen=True)
class CensusClueSet:
    """``assets/<profile>/selection_census_clues.v1.json`` (whole-file replace).

    Not registered in ``pipeline_fingerprint_files`` nor in the Song proof
    closure: regenerating it must not drift any proof fingerprint.
    """

    schema_version: str
    profile_id: str
    provenance: ClueProvenance
    preamble_zh: str
    clues: tuple[CensusClue, ...]


@dataclass(frozen=True)
class RecallClueProvenance:
    """Stored on new scorecards like ``semantic_recall_chat_evidence``; no gate."""

    schema_version: str
    census_id: str
    asset_sha256: str
    status: CensusCluesStatus


@dataclass(frozen=True)
class CensusTranscriptCue:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class CensusWindowInput:
    window_id: str
    recording_group: str
    start_s: int
    end_s: int
    cues: tuple[CensusTranscriptCue, ...]
    danmaku_count: int
    asr_missing: bool


class CensusTranscriber(Protocol):
    """Coarse ASR adapter (BCUT today); the executing host is the caller's choice."""

    def transcribe(self, recording: RecordingRef, media_path: Path) -> Sequence[CensusTranscriptCue]:
        ...


class CensusWindowLabeler(Protocol):
    """Model adapter that answers the window contract for a batch of windows."""

    def label(self, windows: Sequence[CensusWindowInput], *, prompt: str) -> Sequence[CensusWindow]:
        ...


def build_census_label_prompt(
    *,
    host_display_name: str,
    metric_text: str,
    config: CensusConfig,
) -> str:
    """Render the window contract + the metric's seven-dimension section + interests.

    The dimension definitions are extracted from ``metric_text`` by
    ``config.metric_dimension_section_heading``; nothing is restated here.
    """

    lint_census_config(config, metric_text=metric_text)
    section = extract_metric_section(metric_text, heading=config.metric_dimension_section_heading)
    dimensions = json.dumps(dict.fromkeys(CENSUS_WINDOW_DIMENSIONS), ensure_ascii=False)
    return (
        f"## 通用窗口合同\n主播：{host_display_name}\n"
        "输入为 window_id、起止秒、粗转写（最多2500字）、弹幕条数、asr_missing。"
        "粗 ASR 不补造，只评本窗证据；朗读、引述、角色台词和播放内容不算主播本人立场。"
        "缺转写写“转写缺失”；不能评的维度写 null（未测），不写0。无转写证据时所有维度为 null。\n"
        "只返回一个严格 JSON 对象，字段恰为：window_id（照抄）、host_activity、"
        "secondary_activities（数组）、topic（不超过12字）、grades、audience_interest_hits（id数组）、"
        "persons（现实中被提到或在场的具体他人数组）、address_terms（实际称呼数组）、"
        "trigger_kinds（数组）、quoted_or_roleplay（布尔）、summary（不超过20字）。"
        "不产出 Tier、分数、candidate_quality 或综合价值结论。\n"
        "主活动为占时最多的活动，其他活动写 secondary_activities。活动闭集："
        + ", ".join(a for a in ACTIVITY_ORDER if a != "UNSPECIFIED")
        + "。触发来源闭集：" + ", ".join(get_args(TriggerKind)) + "。\n\n"
        + section + "\n"
        "窗口规则：只用0–3档，不给4，不评 self_contained；档义以以上 metric 原文为准。"
        f"grades 的键必须恰为：{dimensions}。每个档位的持久化 basis 为 CENSUS_LABEL。\n\n"
        "## 受众兴趣索引\n"
        + json.dumps([asdict(i) for i in config.audience_interests], ensure_ascii=False)
        + "\n"
    )


def extract_metric_section(metric_text: str, *, heading: str) -> str:
    """Return an exact Markdown section, through subheadings but not its next peer."""

    lines = metric_text.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.strip() == heading]
    if len(matches) != 1 or not re.match(r"^#{1,6} ", heading):
        raise ValueError("CENSUS_METRIC_SECTION_NOT_UNIQUE")
    start = matches[0]
    level = len(heading.split(" ", 1)[0])
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if match := re.match(r"^(#{1,6})\s", lines[i]):
            if len(match[1]) <= level:
                end = i
                break
    return "".join(lines[start:end])


def lint_census_config(config: CensusConfig, *, metric_text: str) -> None:
    section = extract_metric_section(metric_text, heading=config.metric_dimension_section_heading)
    lines = section.splitlines()
    starts = [i for i, line in enumerate(lines)
              if line.startswith(config.metric_audience_paragraph_marker)]
    if not config.metric_audience_paragraph_marker or len(starts) != 1:
        raise ValueError("CENSUS_AUDIENCE_PARAGRAPH_NOT_UNIQUE")
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^(?:\d+[.)]\s|#{1,6}\s)", lines[i])), len(lines))
    paragraph = "\n".join(lines[start:end])
    ids: set[str] = set()
    for interest in config.audience_interests:
        if (not interest.interest_id.strip() or interest.interest_id in ids or
                not interest.label_zh.strip() or not interest.description_zh.strip()):
            raise ValueError("INVALID_CENSUS_AUDIENCE_INTEREST")
        ids.add(interest.interest_id)
        if interest.label_zh not in paragraph:
            raise ValueError(f"CENSUS_INTEREST_NOT_IN_METRIC: {interest.label_zh}")


def parse_census_config(raw: object, *, profile_id: str) -> CensusConfig:
    row = _json_object(raw)
    if row.get("schema_version") != CENSUS_CONFIG_SCHEMA:
        raise ValueError("INVALID_CENSUS_CONFIG_SCHEMA")
    if row.get("profile_id") != profile_id:
        raise ValueError("CENSUS_PROFILE_MISMATCH")
    interests = row.get("audience_interests")
    if not isinstance(interests, list):
        raise ValueError("INVALID_CENSUS_AUDIENCE_INTERESTS")
    return CensusConfig(profile_id, tuple(
        AudienceInterest(_json_text(item, "interest_id"), _json_text(item, "label_zh"),
                         _json_text(item, "description_zh")) for item in interests
    ), _json_text(row, "metric_dimension_section_heading"),
        _json_text(row, "metric_audience_paragraph_marker"))


def build_census_windows(
    cues: Sequence[CensusTranscriptCue],
    *,
    recording_group: str,
    window_seconds: int,
    duration_ms: int | None = None,
    danmaku_starts_ms: Sequence[int] = (),
    asr_missing: bool = False,
) -> tuple[CensusWindowInput, ...]:
    """Assign each cue to the window containing its start (cues are not split)."""

    if type(window_seconds) is not int or window_seconds <= 0:
        raise ValueError("INVALID_CENSUS_WINDOW_SECONDS")
    for cue in cues:
        if (type(cue.start_ms) is not int or type(cue.end_ms) is not int or
                cue.start_ms < 0 or cue.end_ms <= cue.start_ms or not isinstance(cue.text, str)):
            raise ValueError("INVALID_CENSUS_CUE")
    if duration_ms is None:
        duration_ms = max((cue.end_ms for cue in cues), default=0)
    if type(duration_ms) is not int or duration_ms < 0:
        raise ValueError("INVALID_CENSUS_DURATION")
    if any(c.start_ms >= duration_ms for c in cues):
        raise ValueError("CENSUS_CUE_OUTSIDE_RECORDING")
    if any(type(t) is not int or not 0 <= t < duration_ms for t in danmaku_starts_ms):
        raise ValueError("CENSUS_DANMAKU_OUTSIDE_RECORDING")
    width = window_seconds * 1000
    count = (duration_ms + width - 1) // width
    buckets: list[list[CensusTranscriptCue]] = [[] for _ in range(count)]
    for cue in sorted(cues, key=lambda c: (c.start_ms, c.end_ms, c.text)):
        buckets[cue.start_ms // width].append(cue)
    counts = Counter(t // width for t in danmaku_starts_ms)
    return tuple(CensusWindowInput(
        f"{recording_group}:{i:04}", recording_group, i * window_seconds,
        min((i + 1) * window_seconds, (duration_ms + 999) // 1000),
        tuple(bucket), counts[i], asr_missing,
    ) for i, bucket in enumerate(buckets))


def census_window_text(window: CensusWindowInput) -> str:
    return "\n".join(c.text for c in window.cues)[:MAX_CENSUS_INPUT_CHARS]


def parse_census_window_label(
    raw: object, *, window: CensusWindowInput, config: CensusConfig,
) -> CensusWindow:
    """Validate the exact model JSON contract before it can become a completed stage."""

    row = _json_object(raw)
    if set(row) != {"window_id", "host_activity", "secondary_activities", "topic", "grades",
                    "audience_interest_hits", "persons", "address_terms", "trigger_kinds",
                    "quoted_or_roleplay", "summary"}:
        raise ValueError("INVALID_CENSUS_LABEL_FIELDS")
    if row["window_id"] != window.window_id:
        raise ValueError("CENSUS_LABEL_WINDOW_MISMATCH")
    activity = _json_text(row, "host_activity")
    activities = tuple(a for a in ACTIVITY_ORDER if a != "UNSPECIFIED")
    secondary = _json_strings(row, "secondary_activities")
    triggers = _json_strings(row, "trigger_kinds")
    hits = _json_strings(row, "audience_interest_hits")
    if activity not in activities or any(a not in activities or a == activity for a in secondary):
        raise ValueError("INVALID_HOST_ACTIVITY")
    if any(t not in get_args(TriggerKind) for t in triggers):
        raise ValueError("INVALID_CENSUS_TRIGGER")
    if any(h not in {i.interest_id for i in config.audience_interests} for h in hits):
        raise ValueError("INVALID_CENSUS_INTEREST_HIT")
    grades = _json_object(row["grades"])
    if set(grades) != set(CENSUS_WINDOW_DIMENSIONS):
        raise ValueError("INVALID_WINDOW_DIMENSIONS")
    parsed_grades = tuple(DimensionGrade(
        d, None if grades[d] is None else _coarse_grade(cast(int, grades[d])), "CENSUS_LABEL",
    ) for d in CENSUS_WINDOW_DIMENSIONS)
    text = census_window_text(window)
    if (window.asr_missing or not text.strip()) and any(g.grade is not None for g in parsed_grades):
        raise ValueError("CENSUS_GRADE_WITHOUT_TRANSCRIPT")
    topic = _json_text(row, "topic", allow_empty=True)
    summary = _json_text(row, "summary", allow_empty=True)
    if len(topic) > 12 or len(summary) > 20 or type(row["quoted_or_roleplay"]) is not bool:
        raise ValueError("INVALID_CENSUS_LABEL_TEXT")
    return CensusWindow(
        window.window_id, window.recording_group, window.start_s, window.end_s,
        window.asr_missing, len(text), cast(HostActivity, activity),
        cast(tuple[HostActivity, ...], secondary), topic, parsed_grades, hits,
        _json_strings(row, "persons"), _json_strings(row, "address_terms"),
        cast(tuple[TriggerKind, ...], triggers), row["quoted_or_roleplay"], summary,
    )


def _json_object(raw: object) -> dict[str, CensusJSON]:
    if not isinstance(raw, dict) or any(not isinstance(k, str) for k in raw):
        raise ValueError("INVALID_CENSUS_JSON_OBJECT")
    return cast(dict[str, CensusJSON], raw)


def _json_text(raw: object, key: str, *, allow_empty: bool = False) -> str:
    value = _json_object(raw).get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"INVALID_CENSUS_TEXT: {key}")
    return value


def _json_strings(raw: object, key: str) -> tuple[str, ...]:
    value = _json_object(raw).get(key)
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"INVALID_CENSUS_STRING_ARRAY: {key}")
    result = cast(list[str], value)
    if len(set(result)) != len(result):
        raise ValueError(f"DUPLICATE_CENSUS_STRING: {key}")
    return tuple(result)


def adapt_legacy_census_label(
    raw_window_id: str,
    *,
    primary: str,
    labels: Sequence[str],
    stance_emotion: int,
    clip_hook: int,
    persons: Sequence[str],
    address_terms: Sequence[str],
    asr_missing: bool,
    input_char_count: int,
    start_s: int,
    end_s: int,
    summary: str,
) -> CensusWindow:
    """Map one 2026-10 legacy label row onto ``CensusWindow`` (pilot only).

    Every produced grade carries ``basis="LEGACY_LABEL_PROXY"``; dimensions
    the legacy labels never measured stay ``None``.
    """

    if primary not in LEGACY_PRIMARY_ACTIVITIES or any(
        label not in LEGACY_PRIMARY_ACTIVITIES for label in labels
    ):
        raise ValueError("UNKNOWN_LEGACY_LABEL")
    stance = _coarse_grade(stance_emotion)
    hook = _coarse_grade(clip_hook)
    label_set = set(labels)
    relation: CoarseGrade = (3 if primary in {"ADDRESS", "RELATION"} else
                            2 if label_set & {"ADDRESS", "RELATION"} else 0)
    audience: CoarseGrade = 3 if primary == "YURI" else 2 if "YURI" in label_set else 0
    values: dict[str, CoarseGrade | None] = {
        "lidousha_centrality": None, "stance_intensity": stance,
        "audience_salience": audience, "relationship_interaction": relation,
        "persona_reversal": None, "comedic_payoff": hook,
    }
    activity = LEGACY_PRIMARY_ACTIVITIES[primary]
    secondary = {LEGACY_PRIMARY_ACTIVITIES[label] for label in labels}
    return CensusWindow(
        window_id=raw_window_id, recording_group=raw_window_id.rsplit(":", 1)[0],
        start_s=start_s, end_s=end_s, asr_missing=asr_missing, input_char_count=input_char_count,
        host_activity=activity,
        secondary_activities=tuple(a for a in ACTIVITY_ORDER
                                   if a in secondary and a not in {"UNSPECIFIED", activity}),
        topic="", grades=tuple(DimensionGrade(d, values[d], "LEGACY_LABEL_PROXY")
                               for d in CENSUS_WINDOW_DIMENSIONS),
        audience_interest_hits=("gl_yuri",) if "YURI" in label_set or primary == "YURI" else (),
        persons=tuple(sorted(set(persons))), address_terms=tuple(sorted(set(address_terms))),
        trigger_kinds=("UNKNOWN",), quoted_or_roleplay=None, summary=summary,
    )


def generate_census_clues(
    windows: Sequence[CensusWindow],
    *,
    config: CensusConfig,
    manifest: CensusInputManifest,
    metric_text: str = "",
) -> CensusClueSet:
    """Deterministic clue generation (design §2.3); no model call."""

    refuse_if_handwritten_clue_section(metric_text, section_heading=HANDWRITTEN_CLUE_SECTION_HEADING)
    if config.profile_id != manifest.profile_id:
        raise ValueError("CENSUS_PROFILE_MISMATCH")
    if len({w.window_id for w in windows}) != len(windows):
        raise ValueError("DUPLICATE_WINDOW_ID")
    for window in windows:
        if set(g.dimension for g in window.grades) != set(CENSUS_WINDOW_DIMENSIONS):
            raise ValueError("INVALID_WINDOW_DIMENSIONS")
        if len(window.grades) != len(CENSUS_WINDOW_DIMENSIONS):
            raise ValueError("DUPLICATE_WINDOW_DIMENSION")
        if window.host_activity not in ACTIVITY_ORDER:
            raise ValueError("INVALID_HOST_ACTIVITY")
        for g in window.grades:
            if g.grade is not None:
                _coarse_grade(g.grade)
            if g.basis not in get_args(GradeBasis):
                raise ValueError("INVALID_MEASUREMENT_BASIS")
    valid = tuple(sorted((w for w in windows if not w.asr_missing and w.input_char_count > 0),
                         key=lambda w: w.window_id))
    basis: GradeBasis = ("LEGACY_LABEL_PROXY" if any(
        g.basis == "LEGACY_LABEL_PROXY" for w in windows for g in w.grades
    ) else "CENSUS_LABEL")
    provenance = ClueProvenance(
        manifest.census_id, manifest.created_at[:10], _input_sha256(windows, manifest),
        GENERATOR_VERSION, manifest.label_prompt_sha256, len(windows), len(valid),
    )
    clues: list[CensusClue] = []
    measured = tuple(d for d in CENSUS_WINDOW_DIMENSIONS
                     if any(_grade(w, d) is not None for w in valid))
    inter = tuple(d for d in INTERACTION_DIMENSIONS if d in measured)
    support = tuple(w for w in valid if _payoff(w) and any(_signal(w, d) for d in inter))
    embedded = tuple(w for w in support if max((_grade(w, d) or 0 for d in inter), default=0) == 2)
    if (len(support) >= MIN_SUPPORT_WINDOWS and
            len(embedded) * EMBEDDED_SHARE_DENOMINATOR >=
            len(support) * EMBEDDED_SHARE_NUMERATOR):
        activities = _supported_activities(embedded)
        clues.append(CensusClue(
            "cross_activity_embedded", "CROSS_ACTIVITY_EMBEDDED", inter, activities,
            CLUE_TEMPLATES["CROSS_ACTIVITY_EMBEDDED"].format(acts=_activity_text(activities)), (),
            EvidenceRatio(len(embedded), len(support),
                          "有兑现且带互动/兴趣信号的窗中，该信号只是次要（档=2、不是窗口主内容）的窗"),
            _examples(embedded), basis,
        ))
    base_hits = sum(_payoff(w) for w in valid)
    for dimension in measured:
        if dimension == PAYOFF_DIMENSION:
            continue
        signals = tuple(w for w in valid if _signal(w, dimension))
        hits = tuple(w for w in signals if _payoff(w))
        # Compare exact integer cross-products, including the strict inequality.
        if (len(signals) < MIN_SUPPORT_WINDOWS or
                len(hits) * len(valid) <= base_hits * len(signals)):
            continue
        cues: tuple[str, ...] = ()
        text = CLUE_TEMPLATES[dimension]
        if dimension == "audience_salience":
            ids = {i for w in signals for i in w.audience_interest_hits}
            cues = tuple(a.label_zh for a in sorted(config.audience_interests,
                         key=lambda a: a.interest_id) if a.interest_id in ids)
            text = text.format(interests="、".join(cues) or "受众关心的题材")
        elif dimension == "relationship_interaction":
            terms = Counter(t for w in signals for t in set(w.address_terms) if t)
            cues = tuple(t for t, _ in sorted(terms.items(), key=lambda item: (-item[1], item[0]))
                         [:LOOKFOR_TOP_RECOGNITION_CUES])
            text = (text.format(terms="、".join(cues)) if cues else
                    text.replace("出现「{terms}」等称呼或来回互动时", "出现称呼或来回互动时"))
        clues.append(CensusClue(
            f"lookfor_{dimension}", "DIMENSION_LOOKFOR", (dimension,), _supported_activities(hits),
            text, cues, EvidenceRatio(len(hits), len(signals), f"{dimension}>=2 的窗中有兑现的窗"),
            _examples(hits), basis,
        ))
    with_persons = tuple(w for w in valid if any(w.persons))
    if len(with_persons) >= MIN_SUPPORT_WINDOWS:
        hits = tuple(w for w in with_persons if _payoff(w))
        clues.append(CensusClue(
            "entity_recognition", "ENTITY_RECOGNITION", ("relationship_interaction",), (),
            CLUE_TEMPLATES["ENTITY_RECOGNITION"], (),
            EvidenceRatio(len(hits), len(with_persons), "含人物字符串的窗中有兑现的窗"),
            _examples(hits), basis,
        ))
    activities = _supported_activities(valid)
    seen = tuple(a for a in activities if any(w.host_activity == a and _payoff(w) for w in valid))
    listed = _supported_activities(tuple(w for w in valid if _payoff(w)))
    coverage_text = (CLUE_TEMPLATES["ACTIVITY_COVERAGE"].format(acts=_activity_text(listed))
                     if listed else "当前输入尚未观察到达到活动展示口径的兑现段落；活动类别本身不给价值结论，仍按本次候选的片内事件辨认。")
    clues.append(CensusClue(
        "activity_coverage", "ACTIVITY_COVERAGE", (PAYOFF_DIMENSION,), listed, coverage_text, (),
        EvidenceRatio(len(seen), len(activities), "窗数达到下限的活动中出现过兑现窗的活动"),
        _examples(tuple(w for w in valid if w.host_activity == "STREAM_OPERATIONS" and _payoff(w)), 2),
        basis,
    ))
    clues.sort(key=lambda clue: CLUE_RENDER_ORDER.index(clue.clue_id))
    return CensusClueSet(CLUE_SET_SCHEMA, config.profile_id, provenance, CLUE_PREAMBLE_ZH, tuple(clues))


def lint_clue_text(text: str) -> tuple[str, ...]:
    """Return the forbidden directive phrases found in clue text (empty = clean)."""

    normalized = "".join(unicodedata.normalize("NFKC", text).split())
    return tuple(phrase for phrase in FORBIDDEN_DIRECTIVE_PHRASES if phrase in normalized)


def render_census_clue_block(clue_set: CensusClueSet) -> str:
    """Prompt-facing text: preamble + one question per clue; contains no numbers."""

    if clue_set.schema_version != CLUE_SET_SCHEMA or clue_set.preamble_zh != CLUE_PREAMBLE_ZH:
        raise ValueError("INVALID_CLUE_SET")
    lines = ["## 去哪找：频道普查线索（自动生成）", "", CLUE_PREAMBLE_ZH, ""]
    for clue in clue_set.clues:
        for text in (clue.observation_question, *clue.recognition_cues):
            _validate_prompt_text(text)
        lines.append(f"- {clue.observation_question}")
    block = "\n".join(lines) + "\n"
    if any(ch.isdigit() for ch in block):
        raise ValueError("DIGIT_IN_CLUE_BLOCK")
    return block


def render_census_report(windows: Sequence[CensusWindow], clue_set: CensusClueSet) -> str:
    """Human report with counts, activity x dimension table and entity tables."""

    valid = tuple(sorted((w for w in windows if not w.asr_missing and w.input_char_count > 0),
                         key=lambda w: w.window_id))
    lines = ["# 频道普查报告", "", f"普查：{clue_set.provenance.census_id}",
             f"总窗数：{len(windows)}；有效窗数：{len(valid)}；转写缺失：{sum(w.asr_missing for w in windows)}；"
             f"其余空输入：{sum(not w.asr_missing and w.input_char_count <= 0 for w in windows)}。",
             "档位为空表示未测；所有维度档位与评分卡同义；普查不产生候选分数或综合结论。", "",
             "## 活动 × 维度", "", "每格：信号窗数 / 其中兑现窗数；未测单列。", "",
             "| 活动 | 窗数 | " + " | ".join(CENSUS_WINDOW_DIMENSIONS) + " |",
             "| --- | --- | " + " | ".join("---" for _ in CENSUS_WINDOW_DIMENSIONS) + " |"]
    for activity in ACTIVITY_ORDER:
        group = tuple(w for w in valid if w.host_activity == activity)
        cells: list[str] = []
        for dimension in CENSUS_WINDOW_DIMENSIONS:
            signals = tuple(w for w in group if _signal(w, dimension))
            unmeasured = sum(_grade(w, dimension) is None for w in group)
            cells.append(f"{len(signals)}/{sum(_payoff(w) for w in signals)}（未测 {unmeasured}）")
        lines.append(f"| {ACTIVITY_LABELS_ZH[activity]} | {len(group)} | " + " | ".join(cells) + " |")
    lines.extend(["", "## 线索证据", ""])
    for clue in clue_set.clues:
        e = clue.evidence
        lines.extend([f"- {clue.clue_id}：{e.numerator}/{e.denominator}；{e.definition_zh}；"
                      f"口径 {clue.measurement_basis}；例窗 {'、'.join(clue.example_window_ids) or '无'}。"])
    for title, attribute in (("人物原样表", "persons"), ("称呼原样表", "address_terms")):
        counts = Counter(t for w in valid for t in set(getattr(w, attribute)) if t)
        lines.extend(["", f"## {title}", "", "不合并别名，不认定身份；仅作各 owner 的审核材料。", ""])
        for term, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            hits = sum(_payoff(w) and term in getattr(w, attribute) for w in valid)
            # JSON quoting keeps original surfaces, including any literal line breaks.
            lines.append(f"- {json.dumps(term, ensure_ascii=False)}：{hits}/{count}")
    lines.extend(["", "## 月度覆盖", ""])
    months = sorted({w.recording_group[:7] for w in valid})
    for month in months:
        group = tuple(w for w in valid if w.recording_group[:7] == month)
        lines.append(f"- {month}：兑现 {sum(_payoff(w) for w in group)}/{len(group)}")
    no_signal = tuple(w for w in valid if not any(_signal(w, d) for d in INTERACTION_DIMENSIONS)
                      and "SINGING" not in (w.host_activity, *w.secondary_activities))
    lines.extend(["", f"没有互动/兴趣信号且没有唱歌的窗：兑现 {sum(_payoff(w) for w in no_signal)}/{len(no_signal)}。",
                  "", "## 局限", "", "同源标注；五分钟窗口内共现不证明能组成短段，也不证明因果。",
                  "LEGACY_LABEL_PROXY 仅是旧标签折算，未测维度保持未测，不能视作重新标注。"])
    return "\n".join(lines) + "\n"


def load_census_clue_set(path: Path, *, profile_id: str) -> tuple[CensusClueSet | None, CensusCluesStatus]:
    """Fail-open loader: absent/invalid assets yield ``(None, status)``, never raise."""

    return load_census_recall_input(path, profile_id=profile_id)[0:2]


def load_census_recall_input(
    path: Path | None, *, profile_id: str,
) -> tuple[CensusClueSet | None, CensusCluesStatus, str]:
    """Read once: the returned digest identifies exactly the parsed input bytes."""

    if path is None:
        return None, "ABSENT", ""
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None, "ABSENT", ""
    except OSError:
        return None, "INVALID", ""
    digest = hashlib.sha256(payload).hexdigest()
    try:
        row = _json_object(json.loads(payload))
        _clue_keys(row, CensusClueSet)
        if row["schema_version"] != CLUE_SET_SCHEMA:
            raise ValueError("INVALID_CLUE_SET_SCHEMA")
        asset_profile = _json_text(row, "profile_id")
        if asset_profile != profile_id:
            return None, "PROFILE_MISMATCH", digest
        if row["preamble_zh"] != CLUE_PREAMBLE_ZH:
            raise ValueError("INVALID_CLUE_PREAMBLE")
        prov = _json_object(row["provenance"])
        _clue_keys(prov, ClueProvenance)
        provenance = ClueProvenance(
            _json_text(prov, "census_id"), _json_text(prov, "census_date"),
            _clue_sha256(prov, "input_sha256"), _json_text(prov, "generator_version"),
            _clue_sha256(prov, "label_prompt_sha256"), _clue_count(prov, "window_count"),
            _clue_count(prov, "valid_window_count"),
        )
        if provenance.valid_window_count > provenance.window_count:
            raise ValueError("INVALID_CLUE_WINDOW_COUNTS")
        raw_clues = row["clues"]
        if not isinstance(raw_clues, list):
            raise ValueError("INVALID_CLUES")
        clues: list[CensusClue] = []
        for raw_clue in raw_clues:
            clue = _json_object(raw_clue)
            _clue_keys(clue, CensusClue)
            evidence = _json_object(clue["evidence"])
            _clue_keys(evidence, EvidenceRatio)
            ratio = EvidenceRatio(_clue_count(evidence, "numerator"),
                                  _clue_count(evidence, "denominator"),
                                  _json_text(evidence, "definition_zh"))
            if ratio.numerator > ratio.denominator:
                raise ValueError("INVALID_CLUE_RATIO")
            clues.append(CensusClue(
                _json_text(clue, "clue_id"), cast(ClueKind, _json_text(clue, "kind")),
                _json_strings(clue, "dimension_pointers"),
                cast(tuple[HostActivity, ...], _json_strings(clue, "host_activities")),
                _json_text(clue, "observation_question"), _json_strings(clue, "recognition_cues"),
                ratio, _json_strings(clue, "example_window_ids"),
                cast(GradeBasis, _json_text(clue, "measurement_basis")),
            ))
        if len({c.clue_id for c in clues}) != len(clues):
            raise ValueError("DUPLICATE_CLUE_ID")
        result = CensusClueSet(CLUE_SET_SCHEMA, asset_profile, provenance, CLUE_PREAMBLE_ZH, tuple(clues))
        render_census_clue_block(result)
        return result, "VALID", digest
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return None, "INVALID", digest


def _clue_keys(
    row: dict[str, CensusJSON],
    cls: type[CensusClueSet | ClueProvenance | CensusClue | EvidenceRatio],
) -> None:
    # Exact schemas also reject smuggled weight/exclusion fields at every level.
    if set(row) != {field.name for field in fields(cls)}:
        raise ValueError("INVALID_CLUE_FIELDS")


def _clue_count(row: dict[str, CensusJSON], key: str) -> int:
    value = row.get(key)
    if type(value) is not int or value < 0:
        raise ValueError("INVALID_CLUE_COUNT")
    return value


def _clue_sha256(row: dict[str, CensusJSON], key: str) -> str:
    value = _json_text(row, key)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("INVALID_CLUE_SHA256")
    return value


def refuse_if_handwritten_clue_section(metric_text: str, *, section_heading: str) -> None:
    """Raise ``DUPLICATE_HANDWRITTEN_CLUE_SECTION`` while the v0 section still exists."""

    if section_heading and section_heading in metric_text:
        raise ValueError(DUPLICATE_HANDWRITTEN_SECTION_REASON)


def _coarse_grade(value: int) -> CoarseGrade:
    if type(value) is not int or value not in (0, 1, 2, 3):
        raise ValueError("INVALID_COARSE_GRADE")
    return cast(CoarseGrade, value)


def _validate_prompt_text(text: str) -> None:
    if hits := lint_clue_text(text):
        raise ValueError("CLUE_TEXT_DIRECTIVE: " + ",".join(hits))
    if any(ch.isdigit() for ch in text):
        raise ValueError("DIGIT_IN_CLUE_BLOCK")
    if any(ch in text for ch in "\r\n"):
        raise ValueError("MULTILINE_CLUE_TEXT")


def _grade(window: CensusWindow, dimension: str) -> CoarseGrade | None:
    return next((g.grade for g in window.grades if g.dimension == dimension), None)


def _signal(window: CensusWindow, dimension: str) -> bool:
    value = _grade(window, dimension)
    return value is not None and value >= PAYOFF_MIN_GRADE


def _payoff(window: CensusWindow) -> bool:
    return _signal(window, PAYOFF_DIMENSION)


def _supported_activities(windows: Sequence[CensusWindow]) -> tuple[HostActivity, ...]:
    counts = Counter(w.host_activity for w in windows)
    # UNSPECIFIED is an explicitly unmeasured legacy activity, not a real activity.
    return tuple(a for a in ACTIVITY_ORDER if a != "UNSPECIFIED" and
                 counts[a] >= MIN_ACTIVITY_SUPPORT_WINDOWS)


def _activity_text(activities: Sequence[HostActivity]) -> str:
    return "、".join(ACTIVITY_LABELS_ZH[a] for a in activities) or "当前出现的活动"


def _examples(windows: Sequence[CensusWindow], count: int = 3) -> tuple[str, ...]:
    groups: set[str] = set()
    chosen: list[str] = []
    for window in sorted(windows, key=lambda w: (-(_grade(w, PAYOFF_DIMENSION) or 0), w.window_id)):
        if window.recording_group not in groups:
            chosen.append(window.window_id)
            groups.add(window.recording_group)
        if len(chosen) == count:
            break
    return tuple(chosen)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _input_sha256(windows: Sequence[CensusWindow], manifest: CensusInputManifest) -> str:
    if manifest.legacy_label_file_sha256:
        if any(g.basis != "LEGACY_LABEL_PROXY" for w in windows for g in w.grades):
            raise ValueError("LEGACY_INPUT_IDENTITY_REQUIRES_PROXY_LABELS")
        sources = manifest.legacy_label_file_sha256
        if len({name for name, _ in sources}) != len(sources):
            raise ValueError("DUPLICATE_LEGACY_LABEL_SOURCE")
        for name, digest in sources:
            if not name or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise ValueError("INVALID_LEGACY_LABEL_DIGEST")
        return hashlib.sha256(_canonical(sorted(sources))).hexdigest()
    # Sort all unordered input collections so replay order cannot change provenance.
    normalized = []
    for window in sorted(windows, key=lambda w: w.window_id):
        row = asdict(window)
        for key in ("secondary_activities", "audience_interest_hits", "persons", "address_terms", "trigger_kinds"):
            row[key] = sorted(set(row[key]))
        row["grades"] = sorted(row["grades"], key=lambda g: g["dimension"])
        normalized.append(row)
    manifest_row = asdict(manifest)
    manifest_row["recordings"] = sorted(manifest_row["recordings"], key=lambda r: (r["basename"], r["sha256"]))
    return hashlib.sha256(_canonical({"manifest": manifest_row, "windows": normalized})).hexdigest()
