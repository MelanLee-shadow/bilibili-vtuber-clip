"""Repair-first song completeness: try to *prove* a full song before blocking.

Goal steering (维护者,): the pipeline must spend a repair budget before
any BLOCK.  For song candidates the missing evidence is almost always the
lyrics-alignment proof demanded by ``_verify_lyrics_alignment_proof`` in the
shadow pipeline.  This module attempts to *earn* that proof honestly:

1. discover an external LRC for the performed song (pluggable provider,
   e.g. the NetEase public lyric API);
2. fuzzy-align the LRC lines against the ASR cues of the performance;
3. check the alignment actually covers the whole song (head and tail);
4. on success, write an alignment report artifact and return
   ``song_boundary``/``lyrics_alignment`` payloads that pass the existing
   hash-bound proof gate — no score is raised on trust.

Every attempt (including failures) is recorded in a repair report so a final
BLOCK can say what was tried instead of silently giving up.
"""

from __future__ import annotations

from bisect import bisect_left
import copy
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.song_lrc_metadata import is_lrc_section_metadata

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
HOST_LYRIC_SUBJECT = CHANNEL_PROFILE.decision("lyric_vocal_subject")
HOST_NOT_SINGING_REASON = CHANNEL_PROFILE.decision("host_not_singing_reason")

SONG_REPAIR_SCHEMA_VERSION = "song-repair-report.v1"

SHIFT_TOLERANCE_MS = 6_000
MAX_AUDIO_LRC_VARIANT_ATTEMPTS = 3

_NON_LYRIC_CHARS = re.compile(r"[\s，。！？、,.!?…~〜\-—:：;；\"'“”‘’()（）\[\]【】]")
_LRC_VARIANT_MARKERS = re.compile(
    r"(?i)(?:\bremix\b|\bmix\b|\bdj\b|\bpiano\b|\bacoustic\b|"
    r"\binstrumental\b|\bkaraoke\b|\blive\b|\bcover\b|\bver(?:sion)?\b|"
    r"\bsped\s*up\b|\bslowed\b|\bnightcore\b|伴奏|现场|翻唱|钢琴)"
)

LIVE_PERFORMANCE_READY_MODE = "LIVE_STREAMER_SINGING"
LIVE_PERFORMANCE_MODES = {
    LIVE_PERFORMANCE_READY_MODE,
    "ORIGINAL_OR_BACKGROUND_PLAYBACK",
    "OTHER_SINGER",
    "STREAMER_TALKING_OVER_MUSIC",
    "AMBIGUOUS",
}

AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION = "agy-audio-lrc-observation.v5"
AGY_AUDIO_LRC_RUN_SCHEMA_VERSION = "agy-audio-lrc-run.v3"
AGY_AUDIO_LRC_CANONICALIZATION_STRATEGY = "canonical-lrc-by-exact-index.v1"
AGY_AUDIO_LRC_PROVIDER = "agy"
AGY_AUDIO_LRC_MODEL = "Gemini 3.6 Flash (High)"
GEMINI_API_AUDIO_LRC_PROVIDER = "gemini_api"
# 失败转移用的 Gemini API 型号。env 覆盖位与另两个 Gemini 调用点同款惯例
# （ENTITY_AUDIO_GEMINI_API_MODEL / JINGTING_GEMINI_MODEL）；该串会写进 run
# 元数据并被 gate 回验，改 env 必须整个部署一致，历史工件回验按当时取值。
GEMINI_API_AUDIO_LRC_MODEL = os.environ.get(
    "SONG_LRC_GEMINI_API_MODEL", "gemini-3.6-flash"
)
AGY_AUDIO_LRC_FALLBACK_FAILURE_CATEGORIES = frozenset(
    {
        "AGY_UNAVAILABLE",
        "AGY_QUOTA_EXHAUSTED",
        "AGY_TIMEOUT",
        "AGY_FAILED_RC",
        "AGY_EMPTY_OUTPUT",
        "AGY_INVALID_OUTPUT",
        "AGY_LRC_INDEX_INVALID",
    }
)
LYRIC_VOCAL_SUBJECTS = {
    HOST_LYRIC_SUBJECT,
    "OTHER_OR_MIXED_SINGER",
    "RECORDED_OR_PLAYBACK_SINGER",
    "NO_AUDIBLE_LYRIC_VOCAL",
    "AMBIGUOUS",
}
HOST_LYRIC_ROLES = {
    "SINGING_THIS_LYRIC",
    "PERFORMING_THIS_LYRIC_SPOKEN",
    "SPEAKING_NOT_SINGING",
    "SILENT_OR_NOT_AUDIBLE",
    "AMBIGUOUS",
}
MIN_READY_SUNG_LYRIC_ROWS = 7
MIN_READY_SUNG_LYRIC_RATIO = 0.80
MAX_READY_CONSECUTIVE_SPOKEN_LYRIC_ROWS = 6
MAX_READY_SPOKEN_LYRIC_DURATION_MS = 12_000
MAX_READY_SPOKEN_BLOCK_SPAN_MS = 15_000
MAX_READY_SPOKEN_BLOCKS = 1
LYRIC_VOCAL_ASSERTION_KEYS = {
    "lyric_vocal_subject",
    "lidousha_role",
    "same_live_vocal_source_as_lidousha",
    "other_singer_or_harmony_audible",
    "recorded_or_playback_vocal_audible",
}

LIVE_ARRANGEMENT_CLASSIFICATIONS = {
    "FULL_STUDIO_SEQUENCE",
    "COMPLETE_LIVE_ARRANGEMENT",
    "INCOMPLETE_OR_FRAGMENT",
}
LIVE_ARRANGEMENT_TRANSITIONS = {
    "HOST_TALK",
    "INSTRUMENTAL_OUTRO_END",
    "NONE_OR_UNKNOWN",
}
MIN_LIVE_ARRANGEMENT_HEARD_ROWS = 8
MIN_LIVE_ARRANGEMENT_HEARD_RATIO = 0.70
MIN_LIVE_ARRANGEMENT_DURATION_MS = 30_000
MAX_LIVE_ARRANGEMENT_OMITTED_ROWS = 12
MAX_LIVE_ARRANGEMENT_OMITTED_RATIO = 0.30
MAX_LIVE_ARRANGEMENT_OMITTED_BLOCKS = 1
MAX_LIVE_ARRANGEMENT_INTERLINE_GAP_MS = 45_000
MAX_PROVEN_LIVE_INSTRUMENTAL_GAP_MS = 120_000
MAX_LIVE_ARRANGEMENT_OUTRO_MS = 45_000


class LivePerformanceRejected(ValueError):
    """A structurally valid AGY observation that proves this is not a live song.

    Keep this distinct from malformed/unbound AGY output.  The caller still
    fails closed in both cases, but a valid background/original-playback verdict
    must survive song repair so the orchestration layer cannot forget it and
    fall back to treating the seeded song as an ordinary talk candidate.
    """

    def __init__(self, detail: str, performance: Mapping[str, object]) -> None:
        super().__init__(detail)
        self.performance = dict(performance)
        self.reason_codes = live_performance_failure_reason_codes(performance)


def live_performance_failure_reason_codes(performance: object) -> tuple[str, ...]:
    """Map an observed non-live mode to honest user-facing block reasons."""

    mode = performance.get("mode") if isinstance(performance, Mapping) else None
    if mode in {"ORIGINAL_OR_BACKGROUND_PLAYBACK", "STREAMER_TALKING_OVER_MUSIC"}:
        return ("SONG_BACKGROUND_PLAYBACK_ONLY", HOST_NOT_SINGING_REASON)
    if mode == "OTHER_SINGER":
        return (HOST_NOT_SINGING_REASON,)
    return ("SONG_LIVE_PERFORMANCE_UNPROVEN",)

# Timed LRC providers sometimes put a production-credit card in the same timed
# row stream as lyrics.  Keep the classifier intentionally structural: a role
# must occupy the complete short head before a colon (or a conventional
# English ``... by ...`` credit).  Merely mentioning "piano", "作词", or
# "special thanks" inside a sentence is still a lyric.
_LRC_CHINESE_CREDIT_HEADS = {
    "作词", "填词", "词", "歌词", "作曲", "曲", "编曲", "编", "扒谱", "制谱", "调校", "调教", "制作", "制作人", "音乐制作", "制作统筹",
    "监制", "演唱", "原唱", "主唱", "歌手", "艺术家", "表演者", "录音", "录音师", "录音工程", "录音室",
    "混音", "混", "混音师", "混音工程", "混音工程师", "母带", "母带工程", "配唱", "和声", "合声", "吉他", "吉他演奏", "贝斯", "贝斯演奏",
    "鼓", "鼓手", "键盘", "钢琴", "木吉他", "电吉他", "弦乐", "小提琴", "大提琴", "摄影", "封面", "封面设计", "平面设计", "美术", "插画", "设计",
    "出品", "发行", "版权", "统筹", "企划", "策划", "后期", "特别鸣谢", "鸣谢",
}
_LRC_ENGLISH_CREDIT_HEADS = {
    "lyrics", "lyric", "lyricist", "lyricists", "songwriter", "songwriters",
    "composer", "composers", "composition", "music", "arranger", "arrangers", "arrangement",
    "producer", "producers", "production", "music production", "executive producer",
    "vocal", "vocals", "singer", "artist", "performer",
    "recording", "recorded", "recording engineer", "recording engineers", "recording room",
    "recording studio", "studio", "mixing", "mix", "mixing engineer", "mixing engineers",
    "mastering", "master", "mastering engineer", "mastering engineers",
    "guitar", "guitars", "acoustic guitar", "electric guitar", "piano", "keyboard", "keyboards", "bass", "drum", "drums",
    "strings", "violin", "cello", "photography", "photographer", "cover art", "art cover",
    "artwork", "illustration", "illustrator", "design", "designer", "special thanks",
    "acknowledgements", "acknowledgments",
}
_LRC_ENGLISH_BY_CREDIT = re.compile(
    r"^(?:lyrics?|written|songwritten|composed|composition|arranged|produced|performed|recorded|"
    r"mixed|mastered|vocals?|sung|photography|illustration|artwork|cover\s+art)\s+by\s+\S",
    re.IGNORECASE,
)
def _normalized_credit_head(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


def _is_chinese_credit_head(head: str) -> bool:
    compact = re.sub(r"\s+", "", head)
    if compact in _LRC_CHINESE_CREDIT_HEADS:
        return True
    # Multi-role heads such as ``混音、母带`` remain metadata only when
    # every complete component is a known production role.
    parts = [part for part in re.split(r"[/&+、，,]", compact) if part]
    if len(parts) > 1 and all(part in _LRC_CHINESE_CREDIT_HEADS for part in parts):
        return True
    return bool(re.fullmatch(r"母带(?:后期)?混音师", compact))


def is_lrc_credit_metadata(text: str) -> bool:
    """Return true only for a structurally explicit timed production credit.

    The real ``暗恋是一个人的事`` LRC uses bilingual heads such as
    ``录音师 Recording Engineer：...`` and ``钢琴 Piano：...``.  The
    colon/credit-head requirement is the over-filter guard: ordinary lyric
    sentences that merely contain ``guitar``, ``piano``, ``作词``, or ``鸣谢``
    are not classified as metadata.
    """

    normalized = _normalized_credit_head(text)
    if _LRC_ENGLISH_BY_CREDIT.match(normalized):
        return True
    colon_split = re.split(r"[:：]", normalized, maxsplit=1)
    if len(colon_split) != 2 or not colon_split[1].strip():
        return False
    head = colon_split[0].strip()
    if not head or len(head) > 64:
        return False

    chinese = re.sub(r"[^\u3400-\u9fff、，,/&+]+", "", head)
    english = re.sub(r"[^a-z\s]+", " ", head)
    english = re.sub(r"\s+", " ", english).strip()
    chinese_credit = bool(chinese) and _is_chinese_credit_head(chinese)
    english_credit = bool(english) and english in _LRC_ENGLISH_CREDIT_HEADS
    if chinese and english:
        # Bilingual rows are accepted only when both halves independently name
        # a credit role; this avoids filtering a lyric that happens to mix one
        # role word with otherwise unrelated prose.
        return chinese_credit and english_credit
    return chinese_credit or english_credit


def is_lrc_non_lyric_metadata(text: str) -> bool:
    return is_lrc_credit_metadata(text) or is_lrc_section_metadata(text)


@dataclass(frozen=True)
class LrcLine:
    time_ms: int
    text: str


@dataclass(frozen=True)
class LrcResult:
    provider: str
    song_title: str
    artist: str | None
    source_ref: str
    lines: tuple[LrcLine, ...]


LrcProvider = Callable[[str], "LrcResult | Sequence[LrcResult] | None"]


@dataclass(frozen=True)
class AudioLrcAlignmentRun:
    """Audio-bound observation bundle returned by the AGY adapter.

    The model output is deliberately not a READY verdict.  ``attempt_song_repair``
    validates the bundle, recomputes the global shift and mints the standard
    hash-bound proof only when every invariant holds.
    """

    payload: Mapping[str, object]
    provider: str
    model: str
    rc: int | None
    provider_fallback_used: bool
    source_origin_path: str
    source_path: str
    source_sha256: str
    source_duration_ms: int
    lrc_path: str
    lrc_sha256: str
    prompt_path: str
    prompt_sha256: str
    output_path: str
    output_sha256: str
    manifest_path: str
    manifest_sha256: str
    provider_raw_output_path: str | None = None
    provider_raw_output_sha256: str | None = None
    agy_failure_category: str | None = None
    configured_key_count: int | None = None
    accepted_key_ordinal: int | None = None
    accepted_key_tier: str | None = None
    paid_backup_policy: Mapping[str, object] | None = None
    api_audio_path: str | None = None
    api_audio_sha256: str | None = None
    api_audio_duration_ms: int | None = None


def validate_audio_lrc_execution_metadata(
    *,
    provider: object,
    model: object,
    agy_rc: object,
    provider_fallback_used: object,
    agy_failure_category: object,
    sandbox: object,
) -> str | None:
    """Validate the provider lane without weakening the shared v5 proof.

    ``gemini_api`` is accepted only as an explicit AGY failover: bounded failure
    category, no inherited sandbox claim.  ``agy_rc`` says *why AGY could not be
    used*, so every exit shape it carries is a legal trigger and none of them may
    retroactively invalidate the failover's output.  Execution provenance only:
    hashes, v5 rows and live performance are recomputed by the proof validators.
    """

    if provider == AGY_AUDIO_LRC_PROVIDER:
        if (
            model != AGY_AUDIO_LRC_MODEL
            or agy_rc != 0
            or provider_fallback_used is not False
            or agy_failure_category is not None
            or sandbox is not True
        ):
            return "audio aligner AGY execution metadata is invalid"
        return None
    if provider == GEMINI_API_AUDIO_LRC_PROVIDER:
        if (
            model != GEMINI_API_AUDIO_LRC_MODEL
            or provider_fallback_used is not True
            or agy_failure_category not in AGY_AUDIO_LRC_FALLBACK_FAILURE_CATEGORIES
            or sandbox is not False
            or isinstance(agy_rc, bool)
            or not isinstance(agy_rc, (int, type(None)))
            # F1 负数退出=被信号杀死(-9=SIGKILL,即 AGY OOM)、0=AGY 干净退出但输出不可用(AGY_EMPTY_OUTPUT 系列),两者都是合法 failover 触发——被杀本身就是触发条件,不能反过来成为否定 failover 产物的理由(维护者T23:28 逐字: AGY 与 Gemini API 同为 gemini 模型,只差调用顺序)。取代旧 `agy_rc < 0` 的防伪是类别一致性: AGY_FAILED_RC 只由 _classify_agy_nonzero 在非零退出时铸造,配 rc==0 即伪造。见 tests/test_song_repair.py::test_gemini_audio_lrc_failover_accepts_every_way_agy_can_fail 与同处的离线重放。
            or (agy_failure_category == "AGY_FAILED_RC" and agy_rc == 0)
        ):
            return "audio aligner Gemini API failover metadata is invalid"
        return None
    return f"audio aligner provider/model is not approved: {provider} {model}"


def canonicalize_audio_lrc_observation(
    payload: Mapping[str, object],
    lrc: LrcResult,
) -> dict[str, object]:
    """Restore immutable LRC text/timestamps using only exact row indices.

    AGY is an audio-observation provider, not a lyric-text authority.  A model
    may echo Traditional Chinese as Simplified Chinese (or normalize other
    glyphs) while correctly reporting the audio evidence.  That must not
    rewrite the externally sourced LRC or reject an otherwise well-indexed
    observation.  Conversely, text similarity must never be used to guess a
    row: count, uniqueness and strict zero-based order are validated before
    the two canonical display fields are restored.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("provider audio observation is not a JSON object")
    observations = payload.get("observations")
    if not isinstance(observations, list) or len(observations) != len(lrc.lines):
        raise ValueError("provider audio observation must contain exactly one row per canonical LRC line")

    indices: list[int] = []
    for position, row in enumerate(observations):
        if not isinstance(row, Mapping):
            raise ValueError(f"provider audio observation row {position} is not an object")
        index = row.get("lrc_index")
        if not _is_int(index) or not 0 <= int(index) < len(lrc.lines):
            raise ValueError(f"provider audio observation row {position} lrc_index is out of range")
        indices.append(int(index))
    if len(set(indices)) != len(indices):
        raise ValueError("provider audio observation contains duplicate lrc_index values")
    expected_indices = list(range(len(lrc.lines)))
    if indices != expected_indices:
        raise ValueError("provider audio observation lrc_index values are missing or out of strict order")

    canonical = copy.deepcopy(dict(payload))
    canonical_rows = [copy.deepcopy(dict(row)) for row in observations]
    canonical["observations"] = canonical_rows
    for index, row in enumerate(canonical_rows):
        line = lrc.lines[index]
        row["lrc_time_ms"] = line.time_ms
        row["text"] = line.text
    return canonical


AudioLrcAligner = Callable[[Path, LrcResult, str, Path], AudioLrcAlignmentRun]


@dataclass(frozen=True)
class SongRepairAttempt:
    step: str
    status: str  # SUCCESS | FAILED | SKIPPED
    detail: str

    def to_manifest(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SongRepairResult:
    repaired: bool
    attempts: tuple[SongRepairAttempt, ...]
    song_boundary: Mapping[str, object] | None
    lyrics_alignment: Mapping[str, object] | None
    report_path: str | None
    reason_codes: tuple[str, ...] = ()
    live_performance: Mapping[str, object] | None = None

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": SONG_REPAIR_SCHEMA_VERSION,
            "repaired": self.repaired,
            "attempts": [attempt.to_manifest() for attempt in self.attempts],
            "song_boundary": dict(self.song_boundary) if self.song_boundary else None,
            "lyrics_alignment": dict(self.lyrics_alignment) if self.lyrics_alignment else None,
            "report_path": self.report_path,
            "reason_codes": list(self.reason_codes),
            "live_performance": dict(self.live_performance) if self.live_performance else None,
        }


def normalize_lyric_text(text: str) -> str:
    return _NON_LYRIC_CHARS.sub("", text).lower()


def _singable_lrc_result(lrc: LrcResult) -> tuple[LrcResult, int]:
    """Remove timed metadata from provider, cached, pinned, and injected records
    before recall, AGY, proof, and rendering."""

    title_identity = normalize_lyric_text(lrc.song_title)
    singable_rows: list[LrcLine] = []
    for index, line in enumerate(lrc.lines):
        next_line = lrc.lines[index + 1] if index + 1 < len(lrc.lines) else None
        is_timed_title_card = (
            index < 3 and line.time_ms <= 20_000 and bool(title_identity)
            and normalize_lyric_text(line.text) == title_identity
            and next_line is not None and next_line.time_ms - line.time_ms >= 8_000
        )
        if not is_lrc_non_lyric_metadata(line.text) and not is_timed_title_card:
            singable_rows.append(line)
    singable = tuple(singable_rows)
    return (
        LrcResult(
            provider=lrc.provider,
            song_title=lrc.song_title,
            artist=lrc.artist,
            source_ref=lrc.source_ref,
            lines=singable,
        ),
        len(lrc.lines) - len(singable),
    )

SelectedSong = tuple[
    float,
    LrcResult,
    list[dict[str, object]],
    list[dict[str, object]],
    int,
    int,
    int,
    int,
    int,
    Mapping[str, object] | None,
    Mapping[str, object] | None,
]


@dataclass(frozen=True)
class AudioSelectionOutcome:
    selected: SelectedSong
    audio_alignment_run: AudioLrcAlignmentRun
    variant_attempts: list[dict[str, object]]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _lrc_fingerprint(lrc: LrcResult) -> str:
    payload = json.dumps(
        [[line.time_ms, line.text] for line in lrc.lines],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def lrc_latin_letter_ratio(lrc: LrcResult) -> float:
    """Share of alphabetic chars that are ASCII — a romanization (romaji/pinyin)
    row of a CJK song scores near 1.0, the native-script row near 0.0."""

    letters = [ch for line in lrc.lines for ch in line.text if ch.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch.isascii()) / len(letters)


def lrc_timed_structure_agreement(left: LrcResult, right: LrcResult, *, tolerance_ms: int = 800) -> float:
    """Fraction of the shorter LRC's line times reproduced by the longer one.

    Script/arrangement variants of one recording share the synced timeline even
    when their normalized texts share nothing (怪獣の花唄: lrclib
    native-JP vs romaji rows agree within ~150ms/line at text similarity 0.005).
    """

    left_times = sorted(line.time_ms for line in left.lines)
    right_times = sorted(line.time_ms for line in right.lines)
    if not left_times or not right_times:
        return 0.0
    shorter, longer = (
        (left_times, right_times)
        if len(left_times) <= len(right_times)
        else (right_times, left_times)
    )
    hits = 0
    for value in shorter:
        index = bisect_left(longer, value)
        nearest = min(
            (
                abs(longer[position] - value)
                for position in (index - 1, index)
                if 0 <= position < len(longer)
            ),
            default=None,
        )
        if nearest is not None and nearest <= tolerance_ms:
            hits += 1
    return hits / len(shorter)
