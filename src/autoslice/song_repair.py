"""Repair-first song completeness: try to *prove* a full song before blocking.

Goal steering (Ivan, 2026-07-02): the pipeline must spend a repair budget before
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

import base64
import binascii
import copy
import hashlib
import json
import re
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Mapping, Sequence

from src.autoslice.llm_client import LlmCall, extract_json_object
from src.autoslice.review_evidence import SourceCue

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

AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION = "agy-audio-lrc-observation.v4"
AGY_AUDIO_LRC_RUN_SCHEMA_VERSION = "agy-audio-lrc-run.v2"
AGY_AUDIO_LRC_CANONICALIZATION_STRATEGY = "canonical-lrc-by-exact-index.v1"
LYRIC_VOCAL_SUBJECTS = {
    "LIDOUSHA",
    "OTHER_OR_MIXED_SINGER",
    "RECORDED_OR_PLAYBACK_SINGER",
    "NO_AUDIBLE_LYRIC_VOCAL",
    "AMBIGUOUS",
}
LIDOUSHA_LYRIC_ROLES = {
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
        return ("SONG_BACKGROUND_PLAYBACK_ONLY", "SONG_NOT_LIDOUSHA_SINGING")
    if mode == "OTHER_SINGER":
        return ("SONG_NOT_LIDOUSHA_SINGING",)
    return ("SONG_LIVE_PERFORMANCE_UNPROVEN",)

# Timed LRC providers sometimes put a production-credit card in the same timed
# row stream as lyrics.  Keep the classifier intentionally structural: a role
# must occupy the complete short head before a colon (or a conventional
# English ``... by ...`` credit).  Merely mentioning "piano", "作词", or
# "special thanks" inside a sentence is still a lyric.
_LRC_CHINESE_CREDIT_HEADS = {
    "作词", "填词", "词", "歌词", "作曲", "曲", "编曲", "制作", "制作人", "音乐制作", "制作统筹",
    "监制", "演唱", "原唱", "主唱", "歌手", "艺术家", "表演者", "录音", "录音师", "录音工程", "录音室",
    "混音", "混音师", "母带", "母带工程", "配唱", "和声", "合声", "吉他", "吉他演奏", "贝斯", "贝斯演奏",
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
    rc: int
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
    """Remove explicit timed credits before recall, AGY, proof, and rendering.

    This is deliberately applied to caller-supplied/pinned ``LrcResult`` values
    too, not only to provider text parsed in this process.  Cached or injected
    canonical records must not reintroduce credit rows into the all-lines-heard
    gate or its matched-ratio denominator.
    """

    singable = tuple(line for line in lrc.lines if not is_lrc_credit_metadata(line.text))
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


def _round_robin_unique_queries(
    query_groups: Sequence[Sequence[str]],
    *,
    max_queries: int,
) -> list[str]:
    """Schedule distinct discovery queries breadth-first across evidence lanes.

    Screen/visual hints are intentionally the first lane, but they are only a
    hint: one wrong title must not consume the whole query budget before the
    LLM-recognized title or audio-ASR lyric lines get a provider turn.  The
    global provider-call budget remains ``max_queries``.
    """

    limit = max(0, int(max_queries))
    if not limit:
        return []
    groups = [
        [query.strip() for query in group if isinstance(query, str) and query.strip()]
        for group in query_groups
    ]
    positions = [0] * len(groups)
    seen: set[str] = set()
    scheduled: list[str] = []
    while len(scheduled) < limit:
        progressed = False
        for group_index, group in enumerate(groups):
            while positions[group_index] < len(group):
                query = group[positions[group_index]]
                positions[group_index] += 1
                if query in seen:
                    continue
                seen.add(query)
                scheduled.append(query)
                progressed = True
                break
            if len(scheduled) >= limit:
                break
        if not progressed:
            break
    return scheduled


def attempt_song_repair(
    *,
    candidate_id: str,
    cues: Sequence[SourceCue],
    anchor_start_ms: int,
    anchor_end_ms: int,
    source_duration_ms: int,
    output_dir: Path,
    lrc_provider: LrcProvider | None = None,
    provider_name: str | None = None,
    hint_llm_call: LlmCall | None = None,
    extra_queries: Sequence[str] = (),
    max_queries: int = 10,
    max_lrc_candidates: int = 8,
    min_matched_ratio: float = 0.55,
    match_threshold: float = 0.45,
    max_window_gap_ms: int = 30_000,
    pre_roll_ms: int = 1_500,
    post_roll_ms: int = 4_000,
    max_outro_ms: int = 22_000,
    pinned_lrc_results: Sequence[LrcResult] = (),
    source_media_path: Path | None = None,
    audio_lrc_aligner: AudioLrcAligner | None = None,
    max_audio_lrc_attempts: int = MAX_AUDIO_LRC_VARIANT_ATTEMPTS,
) -> SongRepairResult:
    """Try to repair a song candidate into a fully-proven full-song boundary.

    ``pinned_lrc_results`` are caller-supplied LRCs (e.g. a known recurring song
    matched by fingerprint) that are added to the candidate pool BEFORE flaky
    LLM/text discovery, so alignment ranking can still pick the right song when
    search misses it.  ``max_outro_ms`` caps the instrumental 后奏 (outro) kept
    after the last sung line up to the next spoken cue.
    """

    attempts: list[SongRepairAttempt] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    window = _performance_window(cues, anchor_start_ms, anchor_end_ms, max_gap_ms=max_window_gap_ms)
    if not window:
        attempts.append(SongRepairAttempt("performance_window", "FAILED", "no cues overlap the anchor window"))
        return _finish(candidate_id, attempts, output_dir)
    attempts.append(
        SongRepairAttempt(
            "performance_window",
            "SUCCESS",
            f"{len(window)} cues {window[0].source_start_ms}..{window[-1].source_end_ms}ms",
        )
    )

    if lrc_provider is None and not pinned_lrc_results:
        attempts.append(
            SongRepairAttempt(
                "lrc_discovery",
                "SKIPPED",
                "no LRC provider configured (pass lrc_provider / --lrc-provider to enable API repair)",
            )
        )
        return _finish(candidate_id, attempts, output_dir)

    explicit_queries = [q.strip() for q in extra_queries if isinstance(q, str) and q.strip()]
    llm_queries: list[str] = []
    if hint_llm_call is not None:
        # Semantic repair: garbled ASR defeats text search, but an LLM can often
        # still recognize the song from misheard lyrics and give clean queries.
        try:
            hints = generate_llm_song_queries(window, hint_llm_call)
        except Exception as exc:
            hints = []
            attempts.append(SongRepairAttempt("llm_song_hint", "FAILED", f"{type(exc).__name__}: {exc}"))
        else:
            if hints:
                attempts.append(SongRepairAttempt("llm_song_hint", "SUCCESS", f"guessed queries: {hints}"))
            else:
                attempts.append(SongRepairAttempt("llm_song_hint", "FAILED", "LLM returned no usable song guesses"))
        llm_queries.extend(hints)
    lyric_queries = _build_lyric_queries(window, anchor_start_ms, anchor_end_ms)
    deduped_queries = _round_robin_unique_queries(
        (explicit_queries, llm_queries, lyric_queries),
        max_queries=max_queries,
    )

    candidates: list[LrcResult] = []
    seen_refs: set[str] = set()
    prepared_pinned_lrc_results: list[LrcResult] = []
    for pinned in pinned_lrc_results:
        if not isinstance(pinned, LrcResult):
            continue
        prepared, excluded_metadata = _singable_lrc_result(pinned)
        if prepared.lines and prepared.source_ref not in seen_refs:
            seen_refs.add(prepared.source_ref)
            candidates.append(prepared)
            prepared_pinned_lrc_results.append(prepared)
            attempts.append(
                SongRepairAttempt(
                    "pinned_lrc",
                    "SUCCESS",
                    f"{prepared.song_title!r} ({prepared.source_ref}) pinned "
                    f"({len(prepared.lines)} singable LRC lines, {excluded_metadata} timed credit rows excluded) "
                    "— deterministic, ranked against search",
                )
            )
    provider_errors: list[str] = []
    provider_results: list[tuple[str, list[LrcResult]]] = []
    provider_candidate_cap = max(0, int(max_lrc_candidates))
    if lrc_provider is not None and len(candidates) < provider_candidate_cap:
        for query in deduped_queries:
            try:
                found = lrc_provider(query)
            except Exception as exc:  # provider failures must not crash review; they are recorded
                provider_errors.append(f"{query!r}: {type(exc).__name__}: {exc}")
                continue
            # A provider may return many near-identical search rows.  Retain at
            # most the existing global candidate budget from any one call;
            # admission below is breadth-first across calls.
            provider_results.append((query, _coerce_lrc_results(found)[:provider_candidate_cap]))

        # Round-robin the provider result rank too.  Previously the first query
        # could append all eight rows and prevent a later correct title from
        # entering the pool at all.  Query order still makes the visual result
        # first when it is right, while actual ASR/audio proof may overturn it.
        result_rank = 0
        while len(candidates) < provider_candidate_cap:
            progressed = False
            for _query, found_list in provider_results:
                if result_rank >= len(found_list):
                    continue
                progressed = True
                item = found_list[result_rank]
                prepared, _excluded_metadata = _singable_lrc_result(item)
                if prepared.lines and prepared.source_ref not in seen_refs:
                    seen_refs.add(prepared.source_ref)
                    candidates.append(prepared)
                    if len(candidates) >= provider_candidate_cap:
                        break
            if not progressed:
                break
            result_rank += 1
    if not candidates:
        detail = f"no LRC found for {len(deduped_queries)} queries {deduped_queries!r}"
        if provider_errors:
            detail += f"; provider errors: {'; '.join(provider_errors[:3])}"
        attempts.append(SongRepairAttempt("lrc_discovery", "FAILED", detail))
        return _finish(candidate_id, attempts, output_dir)
    attempts.append(
        SongRepairAttempt(
            "lrc_discovery",
            "SUCCESS",
            f"{len(candidates)} LRC candidate(s) from {len(deduped_queries)} queries",
        )
    )

    # Rank candidates by how well their lyrics actually align to this
    # performance — search ranking is not evidence, alignment is.
    ranked: list[tuple[float, LrcResult, list[dict[str, object]]]] = []
    for candidate_lrc in candidates:
        candidate_alignment = _align_lrc_to_cues(candidate_lrc.lines, window, match_threshold=match_threshold)
        candidate_matched = [entry for entry in candidate_alignment if entry["matched_cue_id"] is not None]
        ratio = len(candidate_matched) / len(candidate_alignment) if candidate_alignment else 0.0
        ranked.append((ratio, candidate_lrc, candidate_alignment))
        attempts.append(
            SongRepairAttempt(
                "candidate_alignment",
                "SUCCESS" if ratio >= min_matched_ratio else "FAILED",
                f"{candidate_lrc.song_title!r} ({candidate_lrc.source_ref}): {ratio:.0%} of {len(candidate_alignment)} lines matched",
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    selected: tuple[float, LrcResult, list[dict[str, object]], list[dict[str, object]], int, int, int, int, int] | None = None
    audio_alignment_run: AudioLrcAlignmentRun | None = None
    audio_lrc_variant_attempts: list[dict[str, object]] = []
    if audio_lrc_aligner is not None:
        if source_media_path is None:
            attempts.append(SongRepairAttempt("agy_audio_lrc_alignment", "FAILED", "source media is required"))
            return _finish(candidate_id, attempts, output_dir)
        try:
            primary_lrc = _choose_audio_lrc_candidate(
                ranked,
                pinned_lrc_results=prepared_pinned_lrc_results,
                min_recall_ratio=0.20,
                min_margin=0.08,
                preferred_title_hints=extra_queries,
            )
        except ValueError as exc:
            attempts.append(SongRepairAttempt("agy_audio_lrc_identity", "FAILED", str(exc)))
            return _finish(
                candidate_id,
                attempts,
                output_dir,
                reason_codes=("SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS",),
            )
        verification_candidates = _audio_lrc_retry_candidates(
            ranked,
            primary=primary_lrc,
            preferred_title_hints=extra_queries,
            max_attempts=max_audio_lrc_attempts,
        )
        attempts.append(
            SongRepairAttempt(
                "agy_audio_lrc_identity",
                "SUCCESS",
                f"ranked {len(verification_candidates)} deduped LRC variant(s) for bounded audio proof "
                f"(hard cap {MAX_AUDIO_LRC_VARIANT_ATTEMPTS}); primary={primary_lrc.song_title!r} "
                f"({primary_lrc.source_ref})",
            )
        )
        for ordinal, lrc in enumerate(verification_candidates, start=1):
            ranked_entry = next(
                (entry for entry in ranked if entry[1].source_ref == lrc.source_ref),
                (0.0, lrc, []),
            )
            recall_ratio = float(ranked_entry[0])
            matched_cues = len(_matched_cue_ids(ranked_entry[2]))
            rank_detail = (
                f"variant {ordinal}/{len(verification_candidates)} title={lrc.song_title!r} "
                f"source={lrc.source_ref}; exact_title={_lrc_title_is_exact_hint(lrc, extra_queries)}; "
                f"variant_penalty={_lrc_variant_penalty(lrc)}; "
                f"ASR_support={matched_cues}/{len(lrc.lines)} distinct cues/lines "
                f"({recall_ratio:.0%} row recall)"
            )
            attempts.append(SongRepairAttempt("agy_audio_lrc_variant", "SUCCESS", rank_detail))
            try:
                current_run = audio_lrc_aligner(
                    Path(source_media_path),
                    lrc,
                    candidate_id,
                    output_dir / "agy_audio_lrc" / f"variant-{ordinal:02d}",
                )
            except Exception as exc:
                infrastructure_reason = _audio_lrc_infrastructure_reason(exc)
                detail = f"{rank_detail}; runner failed: {type(exc).__name__}: {exc}"
                attempts.append(SongRepairAttempt("agy_audio_lrc_alignment", "FAILED", detail))
                audio_lrc_variant_attempts.append(
                    {
                        "ordinal": ordinal,
                        "song_title": lrc.song_title,
                        "source_ref": lrc.source_ref,
                        "status": "INFRA_FAILED" if infrastructure_reason else "FAILED",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                # A provider timeout/quota/nonzero exit affects every variant.
                # Do not burn the remaining expensive attempts; preserve the
                # runner's cross-tick recoverable reason code.
                return _finish(
                    candidate_id,
                    attempts,
                    output_dir,
                    reason_codes=(infrastructure_reason or "SONG_AUDIO_LRC_ALIGNMENT_INVALID",),
                )
            try:
                current_selected = _validated_audio_lrc_selection(
                    run=current_run,
                    lrc=lrc,
                    candidate_id=candidate_id,
                    source_media_path=Path(source_media_path),
                    source_duration_ms=source_duration_ms,
                    min_matched_ratio=min_matched_ratio,
                )
            except LivePerformanceRejected as exc:
                detail = f"{rank_detail}; {type(exc).__name__}: {exc}"
                attempts.append(SongRepairAttempt("agy_audio_lrc_alignment", "FAILED", detail))
                audio_lrc_variant_attempts.append(
                    {
                        "ordinal": ordinal,
                        "song_title": lrc.song_title,
                        "source_ref": lrc.source_ref,
                        "status": "PERFORMER_REJECTED",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                # A structurally valid same-audio performer veto is not an LRC
                # variant problem and must never be bypassed by another lyric.
                return _finish(
                    candidate_id,
                    attempts,
                    output_dir,
                    reason_codes=exc.reason_codes,
                    live_performance=exc.performance,
                )
            except Exception as exc:
                detail = f"{rank_detail}; validator rejected this variant: {type(exc).__name__}: {exc}"
                attempts.append(SongRepairAttempt("agy_audio_lrc_alignment", "FAILED", detail))
                audio_lrc_variant_attempts.append(
                    {
                        "ordinal": ordinal,
                        "song_title": lrc.song_title,
                        "source_ref": lrc.source_ref,
                        "status": "CONTENT_OR_ALIGNMENT_REJECTED",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            selected = current_selected
            audio_alignment_run = current_run
            audio_lrc_variant_attempts.append(
                {
                    "ordinal": ordinal,
                    "song_title": lrc.song_title,
                    "source_ref": lrc.source_ref,
                    "status": "ACCEPTED",
                    "reason": "strict audio/LRC, live-performance, and global-shift proof passed",
                }
            )
            attempts.append(
                SongRepairAttempt(
                    "agy_audio_lrc_alignment",
                    "SUCCESS",
                    f"{rank_detail}; current audio proves {selected[0]:.0%} of canonical LRC lines, "
                    "one global shift, and LIVE_STREAMER_SINGING performance mode",
                )
            )
            break
        if selected is None:
            attempts.append(
                SongRepairAttempt(
                    "agy_audio_lrc_variants_exhausted",
                    "FAILED",
                    f"all {len(verification_candidates)} bounded, deduped LRC variant(s) failed strict audio proof",
                )
            )
            return _finish(
                candidate_id,
                attempts,
                output_dir,
                reason_codes=("SONG_AUDIO_LRC_ALIGNMENT_INVALID",),
            )
    elif ranked[0][0] < min_matched_ratio:
        matched_ratio, lrc, _alignment = ranked[0]
        if source_media_path is None or audio_lrc_aligner is None:
            attempts.append(
                SongRepairAttempt(
                    "lyrics_alignment",
                    "FAILED",
                    f"best of {len(ranked)} candidate(s) {lrc.song_title!r} matched only {matched_ratio:.0%} of LRC lines (need >= {min_matched_ratio:.0%}); likely a different song or too-noisy ASR",
                )
            )
            return _finish(candidate_id, attempts, output_dir)

    for raw_matched_ratio, lrc, raw_alignment in ranked if selected is None else ():
        if raw_matched_ratio < min_matched_ratio:
            continue
        # Strict process (song-lyrics-timeline-aligner skill): a real performance
        # of this LRC must be explainable by ONE global time shift
        # (clip_time = lrc_time + offset).  Greedy per-line matches that pile
        # onto the same cue or jump around in time are not evidence.
        # Two-pass: estimate the shift from the recall alignment, then re-align
        # constrained to that shift so lines whose text also appears in a false
        # start / abandoned take land on their shift-consistent occurrence.
        estimated_offset_ms = _median_offset(raw_alignment)
        if estimated_offset_ms is not None:
            constrained = _align_lrc_to_cues(
                lrc.lines,
                window,
                match_threshold=match_threshold,
                target_offset_ms=estimated_offset_ms,
                offset_tolerance_ms=SHIFT_TOLERANCE_MS,
            )
        else:
            constrained = raw_alignment
        alignment, offset_ms, shift_error = enforce_global_shift_alignment(alignment=constrained)
        if shift_error is not None:
            attempts.append(
                SongRepairAttempt(
                    "global_shift_check",
                    "FAILED",
                    f"{lrc.song_title!r}: {shift_error}",
                )
            )
            continue
        matched = [entry for entry in alignment if entry["matched_cue_id"] is not None]
        matched_ratio = len(matched) / len(alignment) if alignment else 0.0
        if matched_ratio < min_matched_ratio:
            attempts.append(
                SongRepairAttempt(
                    "global_shift_check",
                    "FAILED",
                    f"{lrc.song_title!r}: only {matched_ratio:.0%} of LRC lines survive the single-global-shift model (raw greedy match was {raw_matched_ratio:.0%})",
                )
            )
            continue
        attempts.append(
            SongRepairAttempt(
                "global_shift_check",
                "SUCCESS",
                f"{lrc.song_title!r}: offset {offset_ms}ms explains {matched_ratio:.0%} of LRC lines",
            )
        )
        head_missing = _leading_unmatched(alignment)
        tail_missing = _trailing_unmatched(alignment)
        if head_missing:
            attempts.append(
                SongRepairAttempt(
                    "song_completeness",
                    "FAILED",
                    f"{lrc.song_title!r}: song head missing from source: first {head_missing} LRC line(s) have no ASR match — the recording window starts mid-song, a complete-song slice is impossible from this source range",
                )
            )
            continue
        # Tolerate a SHORT unmatched tail: the ASR routinely drops the last few
        # sung lines of an outro (quiet fade, applause/'谢谢大家' bleed) even when
        # she sang the song in full (Ivan 2026-07-07 confirmed 《屑屑》 was complete
        # to 4:11 yet the ASR missed 3 tail lines → false BLOCK).  The LRC, not the
        # ASR, is the subtitle authority, so those lines still render.  A GENUINELY
        # cut-off song leaves many more unmatched tail lines and still fails.  The
        # HEAD stays strict — a mid-song start is truly unsliceable.
        tail_limit = max(3, len(alignment) // 12)
        if tail_missing > tail_limit:
            attempts.append(
                SongRepairAttempt(
                    "song_completeness",
                    "FAILED",
                    f"{lrc.song_title!r}: song tail missing from source: last {tail_missing} LRC line(s) have no ASR match (tolerated {tail_limit}) — the performance was cut off or the capture ended early",
                )
            )
            continue
        middle_gap = _longest_unmatched_run(alignment)
        middle_gap_limit = max(4, (len(alignment) * 3) // 10)
        if middle_gap > middle_gap_limit:
            attempts.append(
                SongRepairAttempt(
                    "song_completeness",
                    "FAILED",
                    f"{lrc.song_title!r}: song middle missing from source: {middle_gap} consecutive LRC line(s) have no shift-consistent ASR match (limit {middle_gap_limit}) — the performance is not continuously present in the capture",
                )
            )
            continue
        first_cue = next(entry for entry in alignment if entry["matched_cue_id"] is not None)
        last_cue = next(entry for entry in reversed(alignment) if entry["matched_cue_id"] is not None)
        first_cue_start = first_cue.get("cue_start_ms")
        last_cue_end = last_cue.get("cue_end_ms")
        if not isinstance(first_cue_start, int) or not isinstance(last_cue_end, int):
            attempts.append(
                SongRepairAttempt(
                    "song_boundary",
                    "FAILED",
                    f"{lrc.song_title!r}: matched cue timing is missing or non-integer",
                )
            )
            continue
        first_lyric_start_ms = first_cue_start
        last_lyric_end_ms = last_cue_end
        # A global shift is not sufficient proof when the implied LRC time zero
        # falls outside the captured source.  In particular, clamping a negative
        # offset to source 0 below would turn a recording that starts after the
        # song's instrumental intro into a false FULL_SONG_READY result.
        if not 0 <= offset_ms < source_duration_ms:
            attempts.append(
                SongRepairAttempt(
                    "song_boundary",
                    "FAILED",
                    f"{lrc.song_title!r}: nominal LRC zero {offset_ms}ms is outside source "
                    f"[0, {source_duration_ms})ms; cannot prove the complete song head",
                )
            )
            continue
        # ``offset_ms`` is where LRC time zero lands in the live source.  Start
        # from that nominal track boundary (plus a small safety lead), not from
        # ``first lyric - pre_roll``: the latter chopped the entire 7.7-second
        # instrumental intro from 2026-07-09 《芽吹くとき》.
        clip_start_ms = max(0, offset_ms - pre_roll_ms)
        # Keep the instrumental outro (后奏): a complete song does not end on the
        # last sung word.  The outro has no lyrics for ASR to anchor on, so extend
        # to just before the next spoken cue after the song (the post-song talk),
        # capped by max_outro_ms.  When the song ends the window, keep the full cap.
        next_cue_start_ms = min(
            (cue.source_start_ms for cue in cues if cue.source_start_ms > last_lyric_end_ms),
            default=None,
        )
        outro_cap_ms = last_lyric_end_ms + max_outro_ms
        outro_end_ms = outro_cap_ms if next_cue_start_ms is None else min(next_cue_start_ms, outro_cap_ms)
        # never cut the last sung word's natural tail
        outro_end_ms = max(outro_end_ms, last_lyric_end_ms + min(post_roll_ms, 1_000))
        clip_end_ms = min(source_duration_ms, outro_end_ms)
        if clip_end_ms <= clip_start_ms:
            attempts.append(
                SongRepairAttempt(
                    "song_boundary",
                    "FAILED",
                    f"{lrc.song_title!r}: matched LRC lines map to a non-monotonic cue order; cannot prove a valid full-song start/end boundary",
                )
            )
            continue
        attempts.append(
            SongRepairAttempt(
                "song_completeness",
                "SUCCESS",
                f"{lrc.song_title!r}: {matched_ratio:.0%} of LRC lines matched; head and tail covered",
            )
        )
        selected = (matched_ratio, lrc, alignment, matched, first_lyric_start_ms, last_lyric_end_ms, clip_start_ms, clip_end_ms, offset_ms)
        break

    if selected is None:
        return _finish(candidate_id, attempts, output_dir)

    matched_ratio, lrc, alignment, matched, first_lyric_start_ms, last_lyric_end_ms, clip_start_ms, clip_end_ms, offset_ms = selected

    provider_label = provider_name or lrc.provider
    report_payload = {
        "schema_version": "lyrics-alignment-report.v1",
        "alignment_model": "external_lrc_global_shift.v1",
        "candidate_id": candidate_id,
        "provider": provider_label,
        "song_title": lrc.song_title,
        "artist": lrc.artist,
        "source_ref": lrc.source_ref,
        "matched_line_ratio": round(matched_ratio, 4),
        "matched_line_denominator": "singable_lrc_lines",
        "line_count": len(alignment),
        "matched_line_count": len(matched),
        "offset_ms": offset_ms,
        "nominal_lrc_zero_ms": offset_ms,
        "lyric_lines": [
            {"lrc_time_ms": line.time_ms, "text": line.text} for line in lrc.lines
        ],
        "first_lyric_start_ms": first_lyric_start_ms,
        "last_lyric_end_ms": last_lyric_end_ms,
        "alignment": alignment,
    }
    if audio_alignment_run is not None:
        report_payload.update(
            {
                "evidence_source": "agy_audio_lrc",
                "audio_alignment_provider": audio_alignment_run.provider,
                "audio_alignment_model": audio_alignment_run.model,
                "audio_lrc_variant_attempts": audio_lrc_variant_attempts,
                "spot_checks": audio_alignment_run.payload["spot_checks"],
                "live_performance": audio_alignment_run.payload["live_performance"],
                "post_song_talk_start_ms": audio_alignment_run.payload["post_song_talk_start_ms"],
                "source_media_path": audio_alignment_run.source_origin_path,
                "source_media_sha256": audio_alignment_run.source_sha256,
                "audio_alignment_artifacts": {
                    "source_origin_path": audio_alignment_run.source_origin_path,
                    "source_path": audio_alignment_run.source_path,
                    "source_sha256": audio_alignment_run.source_sha256,
                    "source_duration_ms": audio_alignment_run.source_duration_ms,
                    "lrc_path": audio_alignment_run.lrc_path,
                    "lrc_sha256": audio_alignment_run.lrc_sha256,
                    "prompt_path": audio_alignment_run.prompt_path,
                    "prompt_sha256": audio_alignment_run.prompt_sha256,
                    "provider_raw_output_path": audio_alignment_run.provider_raw_output_path,
                    "provider_raw_output_sha256": audio_alignment_run.provider_raw_output_sha256,
                    "canonicalized_output_path": audio_alignment_run.output_path,
                    "canonicalized_output_sha256": audio_alignment_run.output_sha256,
                    # Compatibility aliases consumed by existing report
                    # verifiers.  These now name the exact-index canonicalized
                    # observation; the untouched provider bytes are above.
                    "raw_output_path": audio_alignment_run.output_path,
                    "raw_output_sha256": audio_alignment_run.output_sha256,
                    "run_manifest_path": audio_alignment_run.manifest_path,
                    "run_manifest_sha256": audio_alignment_run.manifest_sha256,
                },
            }
        )
    report_path = output_dir / f"{candidate_id}.lyrics-alignment-report.json"
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    attempts.append(SongRepairAttempt("alignment_report", "SUCCESS", f"{report_path.name} sha256:{report_sha[:12]}…"))

    song_boundary = {
        "status": "FULL_SONG_READY",
        "source": f"song_repair.{provider_label}",
        "song_title": lrc.song_title,
        "song_start_ms": clip_start_ms,
        "nominal_lrc_zero_ms": offset_ms,
        "first_lyric_start_ms": first_lyric_start_ms,
        "last_lyric_end_ms": last_lyric_end_ms,
        "clip_start_ms": clip_start_ms,
        "clip_end_ms": clip_end_ms,
    }
    lyrics_alignment = {
        "status": "READY",
        "provider": provider_label,
        "model": (
            f"{provider_label}-agy-audio-lrc-global-shift-v1"
            if audio_alignment_run is not None
            else f"{provider_label}-lrc-global-shift-align-v2"
        ),
        "source": f"song_repair.{provider_label}",
        "external_lrc": lrc.source_ref,
        "matched_line_ratio": round(matched_ratio, 4),
        "offset_ms": offset_ms,
        "nominal_lrc_zero_ms": offset_ms,
        "alignment_report_path": str(report_path),
        "alignment_report_sha256": report_sha,
        "source_media_path": audio_alignment_run.source_origin_path if audio_alignment_run is not None else None,
        "source_media_sha256": audio_alignment_run.source_sha256 if audio_alignment_run is not None else None,
    }
    return _finish(
        candidate_id,
        attempts,
        output_dir,
        repaired=True,
        song_boundary=song_boundary,
        lyrics_alignment=lyrics_alignment,
    )


def build_netease_lrc_provider(*, timeout_seconds: float = 8.0, max_results: int = 3) -> LrcProvider:
    """Real API repair path: NetEase Cloud Music public search + lyric endpoints.

    Returns up to ``max_results`` LRC candidates per query — the caller ranks
    them by actual lyric-to-ASR alignment, so search order is advisory only.
    """

    def provider(query: str) -> list[LrcResult]:
        search_url = "https://music.163.com/api/search/get?" + urllib.parse.urlencode(
            {"s": query, "type": 1, "limit": 6, "offset": 0}
        )
        search_payload = _http_json(search_url, timeout_seconds=timeout_seconds)
        songs = (((search_payload or {}).get("result") or {}).get("songs")) or []
        results: list[LrcResult] = []
        for song in songs:
            if len(results) >= max_results:
                break
            song_id = song.get("id")
            if not isinstance(song_id, int):
                continue
            lyric_url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=0&tv=0"
            try:
                lyric_payload = _http_json(lyric_url, timeout_seconds=timeout_seconds)
            except Exception:
                continue
            lrc_text = (((lyric_payload or {}).get("lrc") or {}).get("lyric")) or ""
            lines = parse_lrc_text(lrc_text)
            if len(lines) >= 8:
                artists = song.get("artists") or []
                artist = artists[0].get("name") if artists and isinstance(artists[0], Mapping) else None
                results.append(
                    LrcResult(
                        provider="netease",
                        song_title=str(song.get("name") or ""),
                        artist=str(artist) if artist else None,
                        source_ref=f"netease://song/{song_id}",
                        lines=tuple(lines),
                    )
                )
        return results

    return provider


def build_lrclib_lrc_provider(*, timeout_seconds: float = 20.0, max_results: int = 3) -> LrcProvider:
    """Build an LRCLIB search provider that returns only usable synced LRCs.

    LRCLIB's search response can include plain-only lyrics.  Those are useful
    for reading but cannot prove clip timing, so this provider deliberately
    fails closed unless ``syncedLyrics`` parses into at least eight timed lines.
    Search order remains advisory; :func:`attempt_song_repair` reranks results
    by alignment to the actual performance.
    """

    def provider(query: str) -> list[LrcResult]:
        search_url = "https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": query})
        payload = _http_json_value(search_url, timeout_seconds=timeout_seconds)
        records = payload if isinstance(payload, list) else []
        results: list[LrcResult] = []
        for record in records:
            if len(results) >= max_results:
                break
            if not isinstance(record, Mapping):
                continue
            result = _lrclib_record_to_result(record)
            if result is not None:
                results.append(result)
        return results

    return provider


def build_kugou_lrc_provider(*, timeout_seconds: float = 12.0, max_results: int = 3) -> LrcProvider:
    """Build a no-credential Kugou synced-lyric fallback.

    Kugou exposes search, lyric-candidate and lyric-download endpoints used by
    its web client.  Search ranking is still only discovery: before downloading
    a candidate, this adapter requires its title, artist and duration to agree
    with the selected search row.  The downloaded LRC then enters the same
    audio/alignment proof gates as every other provider result.

    Kugou commonly inserts ``[00:00] title - artist`` as a timed metadata row.
    That row is provider-specific metadata, not a sung lyric, and is removed
    here rather than weakening the generic LRC parser.
    """

    def provider(query: str) -> list[LrcResult]:
        search_url = "https://songsearch.kugou.com/song_search_v2?" + urllib.parse.urlencode(
            {
                "keyword": query,
                "page": 1,
                "pagesize": 6,
                "platform": "WebFilter",
                "userid": -1,
                "iscorrection": 1,
                "privilege_filter": 0,
            }
        )
        payload = _http_json_value(
            search_url,
            timeout_seconds=timeout_seconds,
            request_headers={"Referer": "https://www.kugou.com/"},
        )
        if not isinstance(payload, Mapping) or payload.get("status") != 1:
            return []
        data = payload.get("data")
        tracks = data.get("lists") if isinstance(data, Mapping) else None
        if not isinstance(tracks, list):
            return []

        results: list[LrcResult] = []
        seen_fingerprints: set[str] = set()
        # Three distinct search rows are enough for a fallback.  Bounding the
        # fan-out keeps an unsuccessful ASR-derived query from issuing dozens
        # of lyric downloads before the next independent query/provider runs.
        for track in tracks[:3]:
            if len(results) >= max_results:
                break
            if not isinstance(track, Mapping):
                continue
            song_title = _strip_kugou_markup(track.get("SongName") or track.get("OriSongName"))
            artist = _strip_kugou_markup(track.get("SingerName"))
            file_hash = str(track.get("FileHash") or "").strip().upper()
            duration_s = track.get("Duration")
            if (
                not song_title
                or not artist
                or not re.fullmatch(r"[0-9A-F]{32}", file_hash)
                or isinstance(duration_s, bool)
                or not isinstance(duration_s, (int, float))
                or duration_s <= 0
            ):
                continue
            duration_ms = int(round(float(duration_s) * 1_000))
            lyric_search_url = "https://lyrics.kugou.com/search?" + urllib.parse.urlencode(
                {
                    "ver": 1,
                    "man": "yes",
                    "client": "pc",
                    "keyword": f"{artist} - {song_title}",
                    "hash": file_hash,
                    "timelength": duration_ms,
                }
            )
            try:
                lyric_payload = _http_json_value(
                    lyric_search_url,
                    timeout_seconds=timeout_seconds,
                    request_headers={"Referer": "https://www.kugou.com/"},
                )
            except Exception:
                continue
            if not isinstance(lyric_payload, Mapping) or lyric_payload.get("status") != 200:
                continue
            candidates = lyric_payload.get("candidates")
            if not isinstance(candidates, list):
                continue

            # Candidate scores are advisory.  Only exact normalized identity
            # and a near-exact catalog duration are allowed to reach download.
            # Try at most two exact candidates for this track to bound requests.
            exact_downloads = 0
            for lyric_candidate in candidates:
                if not isinstance(lyric_candidate, Mapping):
                    continue
                if not _kugou_candidate_matches_track(
                    lyric_candidate,
                    song_title=song_title,
                    artist=artist,
                    duration_ms=duration_ms,
                ):
                    continue
                lyric_id = str(lyric_candidate.get("id") or "").strip()
                access_key = str(lyric_candidate.get("accesskey") or "").strip()
                if not lyric_id.isdigit() or not re.fullmatch(r"[0-9A-Fa-f]{32}", access_key):
                    continue
                exact_downloads += 1
                download_url = "https://lyrics.kugou.com/download?" + urllib.parse.urlencode(
                    {
                        "ver": 1,
                        "client": "pc",
                        "id": lyric_id,
                        "accesskey": access_key,
                        "fmt": "lrc",
                        "charset": "utf8",
                    }
                )
                try:
                    download_payload = _http_json_value(
                        download_url,
                        timeout_seconds=timeout_seconds,
                        request_headers={"Referer": "https://www.kugou.com/"},
                    )
                    if not isinstance(download_payload, Mapping) or download_payload.get("status") != 200:
                        raise ValueError("Kugou lyric download returned an invalid response")
                    content = download_payload.get("content")
                    if not isinstance(content, str):
                        raise ValueError("Kugou lyric download did not return base64 content")
                    lrc_text = base64.b64decode(content, validate=True).decode("utf-8")
                except (binascii.Error, UnicodeDecodeError, ValueError, OSError):
                    if exact_downloads >= 2:
                        break
                    continue
                lines = _filter_kugou_timed_metadata(
                    parse_lrc_text(lrc_text),
                    song_title=song_title,
                    artist=artist,
                )
                if len(lines) < 8:
                    if exact_downloads >= 2:
                        break
                    continue
                result = LrcResult(
                    provider="kugou",
                    song_title=song_title,
                    artist=artist,
                    source_ref=download_url,
                    lines=tuple(lines),
                )
                fingerprint = _lrc_fingerprint(result)
                if fingerprint not in seen_fingerprints:
                    seen_fingerprints.add(fingerprint)
                    results.append(result)
                break
        return results

    return provider


def build_composite_lrc_provider(*providers: LrcProvider, max_results: int | None = None) -> LrcProvider:
    """Combine providers as independent fallbacks without erasing provenance.

    A temporary failure in one public lyric service must not prevent another
    provider from supplying evidence.  Results are deduplicated by canonical
    ``source_ref`` while each result's original ``provider`` and ``source_ref``
    are preserved for the alignment report and proof gate.
    """

    def provider(query: str) -> list[LrcResult]:
        results: list[LrcResult] = []
        seen_refs: set[str] = set()
        for candidate_provider in providers:
            try:
                found = candidate_provider(query)
            except Exception:
                continue
            for item in _coerce_lrc_results(found):
                if item.source_ref in seen_refs:
                    continue
                seen_refs.add(item.source_ref)
                results.append(item)
                if max_results is not None and len(results) >= max_results:
                    return results
        return results

    return provider


def fetch_lrclib_lrc(song_ref: str, *, timeout_seconds: float = 20.0) -> LrcResult | None:
    """Fetch one LRCLIB synced lyric by numeric id or canonical LRCLIB ref.

    Accepted forms include ``"33542202"``, ``"lrclib://track/33542202"``,
    and ``"https://lrclib.net/api/get/33542202"``.  Plain, unsynchronised lyrics are rejected:
    the song repair path needs timestamps, not merely lyric text.
    """

    song_id = _parse_lrclib_id(song_ref)
    if song_id is None:
        return None
    try:
        payload = _http_json(
            f"https://lrclib.net/api/get/{song_id}",
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    return _lrclib_record_to_result(payload, fallback_id=song_id)


def fetch_netease_lrc(song_ref: str, *, timeout_seconds: float = 8.0) -> LrcResult | None:
    """Fetch one NetEase song's LRC by id/ref, bypassing flaky text search.

    Accepts a bare id (``"2615403834"``) or a ref (``"netease://song/2615403834"``).
    Used to pin a known recurring song so its identification is deterministic.
    """

    match = re.search(r"(\d{4,})", str(song_ref or ""))
    if not match:
        return None
    song_id = int(match.group(1))
    try:
        lyric_payload = _http_json(
            f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=0&tv=0",
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        return None
    lrc_text = (((lyric_payload or {}).get("lrc") or {}).get("lyric")) or ""
    lines = parse_lrc_text(lrc_text)
    if len(lines) < 8:
        return None
    song_title = ""
    artist: str | None = None
    try:
        detail = _http_json(
            f"https://music.163.com/api/song/detail?ids=%5B{song_id}%5D",
            timeout_seconds=timeout_seconds,
        )
        songs = (detail or {}).get("songs") or []
        if songs and isinstance(songs[0], Mapping):
            song_title = str(songs[0].get("name") or "")
            artists = songs[0].get("artists") or []
            if artists and isinstance(artists[0], Mapping):
                artist = str(artists[0].get("name") or "") or None
    except Exception:
        pass
    return LrcResult(
        provider="netease",
        song_title=song_title,
        artist=artist,
        source_ref=f"netease://song/{song_id}",
        lines=tuple(lines),
    )


def parse_lrc_text(lrc_text: str) -> list[LrcLine]:
    lines: list[LrcLine] = []
    for raw_line in lrc_text.splitlines():
        matches = re.findall(r"\[(\d+):(\d+)(?:\.(\d+))?\]", raw_line)
        text = re.sub(r"\[[^\]]*\]", "", raw_line).strip()
        if not matches or not text:
            continue
        # Metadata/credit lines are not sung lyrics or proof checkpoints.  The
        # structural bilingual classifier covers both the historical Chinese
        # outro credits and English/bilingual rows such as
        # ``录音师 Recording Engineer：...`` without deleting ordinary lyrics.
        if is_lrc_credit_metadata(text):
            continue
        for minute, second, fraction in matches:
            fraction_ms = int((fraction or "0").ljust(3, "0")[:3])
            lines.append(LrcLine(time_ms=(int(minute) * 60 + int(second)) * 1000 + fraction_ms, text=text))
    lines.sort(key=lambda line: line.time_ms)
    return lines


def _performance_window(
    cues: Sequence[SourceCue],
    anchor_start_ms: int,
    anchor_end_ms: int,
    *,
    max_gap_ms: int,
) -> list[SourceCue]:
    ordered = sorted(cues, key=lambda cue: (cue.source_start_ms, cue.source_end_ms))
    overlapping = [
        index
        for index, cue in enumerate(ordered)
        if cue.source_end_ms > anchor_start_ms and cue.source_start_ms < anchor_end_ms
    ]
    if not overlapping:
        return []
    start_index = overlapping[0]
    end_index = overlapping[-1]
    # expand outward across small gaps so a song whose head/tail lies just
    # outside the anchor is still considered part of the same performance
    while start_index > 0 and ordered[start_index].source_start_ms - ordered[start_index - 1].source_end_ms <= max_gap_ms:
        start_index -= 1
    while (
        end_index + 1 < len(ordered)
        and ordered[end_index + 1].source_start_ms - ordered[end_index].source_end_ms <= max_gap_ms
    ):
        end_index += 1
    return list(ordered[start_index : end_index + 1])


def _cjk_clean_score(text: str) -> float:
    """Rank a cue for use as a lyric search query.  A search matches on
    lyric text, so the best query is one clean CJK/kana-dense, distinctive line — not
    the longest.  The longest ASR lines are often the English/rap sections BCUT
    mangles ("chewe now baby just chewe now") which match nothing; a clean line
    like "谁说圆满的人生才能算圆满" returns 《屑屑》 as the #1 hit.  Japanese songs
    also need kana-only lines such as ``ただそばにいてほしい`` to remain
    searchable.  Score = CJK/kana density × capped length; lines with fewer
    than six such characters are unusable (-1)."""
    normalized = normalize_lyric_text(text)
    if len(normalized) < 6:
        return -1.0
    cjk = sum(
        1
        for ch in normalized
        if "㐀" <= ch <= "鿿" or "぀" <= ch <= "ヿ" or "ｦ" <= ch <= "ﾟ"
    )
    if cjk < 6:
        return -1.0
    return (cjk / len(normalized)) * min(len(normalized), 16)


def _build_lyric_queries(
    window: Sequence[SourceCue],
    anchor_start_ms: int | None = None,
    anchor_end_ms: int | None = None,
) -> list[str]:
    # netease search matches on lyric text, so issue the cleanest CJK-dense lines
    # as INDIVIDUAL queries — one distinctive line finds the song even when the
    # garbled ASR defeats title guessing (proven on 《屑屑》: the 3-longest lines
    # were all English mondegreens and found nothing; clean lines all hit).  The
    # anchor cues (the targeted song) are scored first.
    anchor_cues: Sequence[SourceCue] = window
    if anchor_start_ms is not None and anchor_end_ms is not None:
        overlapping = [
            cue for cue in window if cue.source_end_ms > anchor_start_ms and cue.source_start_ms < anchor_end_ms
        ]
        if overlapping:
            anchor_cues = overlapping

    queries: list[str] = []
    for cue in sorted(anchor_cues, key=lambda c: _cjk_clean_score(c.text), reverse=True):
        if _cjk_clean_score(cue.text) <= 0:
            break
        line = cue.text.strip().replace("\n", " ")[:40]
        if line:
            queries.append(line)
        if len(queries) >= 6:
            break
    # safety net: the two longest lines from the full window (covers songs whose
    # ASR is clean enough that raw length is a fine proxy, e.g. the old behavior).
    for cue in sorted(window, key=lambda c: len(normalize_lyric_text(c.text)), reverse=True)[:2]:
        line = cue.text.strip().replace("\n", " ")[:40]
        if line:
            queries.append(line)
    return list(dict.fromkeys(q for q in queries if q))


def generate_llm_song_queries(window: Sequence[SourceCue], llm_call: LlmCall, *, max_lines: int = 18) -> list[str]:
    """Ask an LLM to recognize the song(s) behind garbled ASR lyrics."""

    lines = [cue.text.strip().replace("\n", " ") for cue in window if cue.text.strip()]
    if len(lines) > max_lines:
        # sample evenly across the window: it may span several songs plus talk,
        # and only sampling the head previously hid the target song entirely
        step = len(lines) / max_lines
        lines = [lines[int(i * step)] for i in range(max_lines)]
    sample = lines
    prompt = (
        "下面是直播歌回的语音转写歌词，含大量同音错别字，可能横跨多首歌和聊天内容。"
        "请推断其中演唱过哪些歌（中文流行/古风/网络歌曲/粤语歌都可能）。\n"
        "转写歌词：\n" + "\n".join(sample) + "\n\n"
        '只输出一个 JSON 对象：{"guesses": [{"title": "歌名", "artist": "歌手或空字符串"}]}，'
        "最多给 4 个猜测，按置信度排序；完全无法判断就给空数组。"
    )
    payload = extract_json_object(llm_call(prompt))
    guesses = payload.get("guesses")
    queries: list[str] = []
    if isinstance(guesses, list):
        for guess in guesses[:4]:
            if not isinstance(guess, Mapping):
                continue
            title = str(guess.get("title") or "").strip()
            artist = str(guess.get("artist") or "").strip()
            if not title:
                continue
            queries.append(f"{title} {artist}".strip())
            queries.append(title)
    return list(dict.fromkeys(queries))


def _align_lrc_to_cues(
    lrc_lines: Sequence[LrcLine],
    window: Sequence[SourceCue],
    *,
    match_threshold: float,
    target_offset_ms: int | None = None,
    offset_tolerance_ms: int = 6_000,
) -> list[dict[str, object]]:
    """Monotonic DP alignment of LRC lines to ASR cues.

    Independent greedy best-match per line cannot handle repeated choruses:
    every occurrence of an identical lyric line matches the same single cue,
    which then (correctly) fails the global-shift check even for a genuinely
    complete performance.  The DP assigns each line the best-scoring cue such
    that matched cue times never go backwards, so each chorus occurrence lands
    on its own temporally-appropriate cue.

    With ``target_offset_ms`` set, only (line, cue) pairs consistent with that
    global shift are eligible.  This second constrained pass lets a line whose
    text also appears in a false start / abandoned take find its
    shift-consistent occurrence instead of being discarded.
    """

    ordered_cues = sorted(window, key=lambda cue: (cue.source_start_ms, cue.source_end_ms))
    normalized_cues = [(cue, normalize_lyric_text(cue.text)) for cue in ordered_cues]
    line_count = len(lrc_lines)
    cue_count = len(normalized_cues)

    scores: list[list[float]] = [[0.0] * cue_count for _ in range(line_count)]
    for i, line in enumerate(lrc_lines):
        normalized_line = normalize_lyric_text(line.text)
        if not normalized_line:
            continue
        for j, (cue, normalized_cue) in enumerate(normalized_cues):
            if not normalized_cue:
                continue
            ratio = SequenceMatcher(None, normalized_line, normalized_cue).ratio()
            if len(normalized_cue) > len(normalized_line) * 2:
                # long ASR cues may merge several lyric lines; compare against
                # the best local substring alignment, not whole-cue ratio only
                match = SequenceMatcher(None, normalized_line, normalized_cue).find_longest_match(
                    0, len(normalized_line), 0, len(normalized_cue)
                )
                contained = match.size / len(normalized_line) if normalized_line else 0.0
                ratio = max(ratio, contained)
            if ratio >= match_threshold:
                if target_offset_ms is not None:
                    predicted_ms = line.time_ms + target_offset_ms
                    if not (cue.source_start_ms - offset_tolerance_ms <= predicted_ms <= cue.source_end_ms + offset_tolerance_ms):
                        continue
                scores[i][j] = ratio

    # dp[i][j]: best total ratio for LRC lines 0..i using cues with index <= j
    # (non-decreasing across lines; a merged cue may serve consecutive lines).
    dp = [[0.0] * (cue_count + 1) for _ in range(line_count + 1)]
    choice: list[list[int]] = [[0] * (cue_count + 1) for _ in range(line_count + 1)]  # 0=skip line, 1=match, 2=narrow j
    for i in range(1, line_count + 1):
        for j in range(cue_count + 1):
            best = dp[i - 1][j]  # skip this line
            action = 0
            if j > 0 and dp[i][j - 1] > best:
                best = dp[i][j - 1]
                action = 2
            if j > 0 and scores[i - 1][j - 1] > 0.0:
                candidate = dp[i - 1][j] + scores[i - 1][j - 1]
                if candidate > best:
                    best = candidate
                    action = 1
            dp[i][j] = best
            choice[i][j] = action

    matched_cue_index: list[int | None] = [None] * line_count
    i, j = line_count, cue_count
    while i > 0 and j >= 0:
        action = choice[i][j]
        if action == 2:
            j -= 1
        elif action == 1:
            matched_cue_index[i - 1] = j - 1
            i -= 1
        else:
            i -= 1

    alignment: list[dict[str, object]] = []
    for i, line in enumerate(lrc_lines):
        index = matched_cue_index[i]
        cue = ordered_cues[index] if index is not None else None
        alignment.append(
            {
                "lrc_time_ms": line.time_ms,
                "lrc_text": line.text,
                "matched_cue_id": cue.cue_id if cue else None,
                "cue_start_ms": cue.source_start_ms if cue else None,
                "cue_end_ms": cue.source_end_ms if cue else None,
                "match_ratio": round(scores[i][index], 4) if cue is not None and index is not None else 0.0,
            }
        )
    return alignment


def _median_offset(alignment: Sequence[Mapping[str, object]]) -> int | None:
    residuals = sorted(
        int(entry["cue_start_ms"]) - int(entry["lrc_time_ms"])
        for entry in alignment
        if entry["matched_cue_id"] is not None
    )
    if not residuals:
        return None
    return residuals[len(residuals) // 2]


def enforce_global_shift_alignment(
    *,
    alignment: Sequence[Mapping[str, object]],
    shift_tolerance_ms: int = SHIFT_TOLERANCE_MS,
    min_consistent_matches: int = 4,
    span_ratio_min: float = 0.7,
    span_ratio_max: float = 1.35,
    span_check_min_lrc_span_ms: int = 20_000,
) -> tuple[list[dict[str, object]], int, str | None]:
    """Keep only matches explainable by a single global shift (skill core rule).

    ``clip_time = lrc_time + offset`` with one offset for the whole song.  The
    offset is the median residual of the greedy matches; a match survives only
    if its predicted clip time lands inside its cue (± tolerance).  This kills
    the failure class where many LRC lines pile onto one plausible-sounding
    cue (e.g. four "祝你生日快乐" lines all matching the same 4s cue) and a
    28s window gets promoted to a "complete" multi-minute song.

    Returns (filtered_alignment, offset_ms, failure_reason).
    """

    matched_entries = [entry for entry in alignment if entry["matched_cue_id"] is not None]
    if len(matched_entries) < min_consistent_matches:
        return list(dict(entry) for entry in alignment), 0, (
            f"only {len(matched_entries)} greedy match(es); need >= {min_consistent_matches} to fit a global shift"
        )
    residuals = sorted(int(entry["cue_start_ms"]) - int(entry["lrc_time_ms"]) for entry in matched_entries)
    offset_ms = residuals[len(residuals) // 2]

    filtered: list[dict[str, object]] = []
    consistent: list[dict[str, object]] = []
    for entry in alignment:
        record = dict(entry)
        if record["matched_cue_id"] is not None:
            predicted_ms = int(record["lrc_time_ms"]) + offset_ms
            cue_start = int(record["cue_start_ms"])
            cue_end = int(record["cue_end_ms"])
            if cue_start - shift_tolerance_ms <= predicted_ms <= cue_end + shift_tolerance_ms:
                consistent.append(record)
            else:
                record["matched_cue_id"] = None
                record["cue_start_ms"] = None
                record["cue_end_ms"] = None
                record["rejected_reason"] = "GLOBAL_SHIFT_INCONSISTENT"
        filtered.append(record)

    if len(consistent) < min_consistent_matches:
        return filtered, offset_ms, (
            f"only {len(consistent)} of {len(matched_entries)} greedy match(es) fit offset {offset_ms}ms ± {shift_tolerance_ms}ms; "
            "the matches are not one continuous performance of this song"
        )

    previous_start = None
    for record in consistent:
        cue_start = int(record["cue_start_ms"])
        if previous_start is not None and cue_start < previous_start:
            return filtered, offset_ms, (
                "shift-consistent matches map LRC lines to cues in non-monotonic time order; "
                "cannot prove a valid full-song timeline"
            )
        previous_start = cue_start

    lrc_span_ms = int(consistent[-1]["lrc_time_ms"]) - int(consistent[0]["lrc_time_ms"])
    cue_span_ms = int(consistent[-1]["cue_start_ms"]) - int(consistent[0]["cue_start_ms"])
    if lrc_span_ms >= span_check_min_lrc_span_ms:
        span_ratio = cue_span_ms / lrc_span_ms if lrc_span_ms else 0.0
        if not (span_ratio_min <= span_ratio <= span_ratio_max):
            return filtered, offset_ms, (
                f"performance span {cue_span_ms}ms vs LRC span {lrc_span_ms}ms (ratio {span_ratio:.2f}) is not a plausible "
                "single performance of this song; the capture likely covers only a fragment"
            )
    return filtered, offset_ms, None


def _lrc_fingerprint(lrc: LrcResult) -> str:
    payload = [
        (line.time_ms, normalize_lyric_text(line.text))
        for line in lrc.lines
        if normalize_lyric_text(line.text)
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _lrc_identity_key(lrc: LrcResult) -> str:
    """Stable song identity across provider-specific timing/text variants."""

    title = normalize_lyric_text(lrc.song_title)
    artist = normalize_lyric_text(lrc.artist or "")
    if title:
        return f"title:{title}|artist:{artist}"
    return f"lyrics:{_lrc_fingerprint(lrc)}"


def _lrc_content_similarity(left: LrcResult, right: LrcResult) -> float:
    """Lyrics-only equivalence signal across cover/provider timing variants.

    Exact timed fingerprints are too strict: the same song is commonly split
    into 23/35/57-line LRCs, includes a repeated chorus in one provider, or has
    a few credit/ad-lib differences.  Compare both full normalized streams and
    exact normalized-line containment; neither uses provider search rank.
    """

    left_lines = [normalize_lyric_text(line.text) for line in left.lines]
    right_lines = [normalize_lyric_text(line.text) for line in right.lines]
    left_lines = [line for line in left_lines if line]
    right_lines = [line for line in right_lines if line]
    if not left_lines or not right_lines:
        return 0.0
    stream_ratio = SequenceMatcher(None, "".join(left_lines), "".join(right_lines)).ratio()
    left_set, right_set = set(left_lines), set(right_lines)
    line_f1 = 2 * len(left_set & right_set) / max(1, len(left_set) + len(right_set))
    return max(stream_ratio, line_f1)


def _lrc_title_family(lrc: LrcResult) -> str:
    """Provider display-title noise stripped for same-song clustering."""

    title = str(lrc.song_title or "")
    title = re.split(r"\s+-\s+|[（(【\[]", title, maxsplit=1)[0]
    title = re.sub(r"(?i)\b(?:cover|live|ver(?:sion)?)\b.*$", "", title)
    return normalize_lyric_text(title)


def _matched_cue_ids(alignment: Sequence[Mapping[str, object]]) -> set[str]:
    return {
        str(row["matched_cue_id"])
        for row in alignment
        if isinstance(row, Mapping) and row.get("matched_cue_id") is not None
    }


def _matched_cue_overlap(
    left: Sequence[Mapping[str, object]],
    right: Sequence[Mapping[str, object]],
) -> tuple[float, float, int]:
    """Return containment, bilateral coverage, and shared ASR-cue count.

    Same-song LRCs routinely split one lyric into different numbers of rows.
    Containment handles line splitting, while coverage against the larger set
    prevents a short unrelated candidate from merging after matching only
    three cherry-picked cues.
    """

    left_ids, right_ids = _matched_cue_ids(left), _matched_cue_ids(right)
    if not left_ids or not right_ids:
        return 0.0, 0.0, 0
    shared = len(left_ids & right_ids)
    return (
        shared / min(len(left_ids), len(right_ids)),
        shared / max(len(left_ids), len(right_ids)),
        shared,
    )


def _preferred_title_keys(preferred_title_hints: Sequence[str]) -> set[str]:
    """Normalize explicit title/search hints without stripping variant labels."""

    return {
        normalized
        for hint in preferred_title_hints
        if isinstance(hint, str) and (normalized := normalize_lyric_text(hint))
    }


def _lrc_title_is_exact_hint(lrc: LrcResult, preferred_title_hints: Sequence[str]) -> bool:
    return normalize_lyric_text(lrc.song_title) in _preferred_title_keys(preferred_title_hints)


def _lrc_variant_penalty(lrc: LrcResult) -> int:
    """Count catalog decorations that usually denote a non-canonical timeline."""

    return len(_LRC_VARIANT_MARKERS.findall(unicodedata.normalize("NFKC", str(lrc.song_title or ""))))


def _lrc_span_ms(lrc: LrcResult) -> int:
    if len(lrc.lines) < 2:
        return 0
    return max(0, int(lrc.lines[-1].time_ms) - int(lrc.lines[0].time_ms))


def _audio_lrc_entry_sort_key(
    entry: tuple[float, LrcResult, list[dict[str, object]]],
    *,
    preferred_title_hints: Sequence[str],
    max_line_count: int,
    max_span_ms: int,
) -> tuple[object, ...]:
    """Evidence-mass-first ordering for expensive audio verification.

    A short remix can match a larger *ratio* simply because its denominator is
    tiny.  Require a sufficiently complete timeline tier first, then use an
    exact explicit title and canonical/non-remix display name when available.
    Absolute distinct cue support and line/span completeness outrank the ratio.
    """

    ratio, lrc, alignment = entry
    line_count = len(lrc.lines)
    span_ms = _lrc_span_ms(lrc)
    enough_lines = line_count >= max(8, (max_line_count * 3 + 4) // 5)
    enough_span = max_span_ms <= 0 or span_ms >= (max_span_ms * 3) // 5
    sufficiently_complete = enough_lines and enough_span
    exact_hint = _lrc_title_is_exact_hint(lrc, preferred_title_hints)
    variant_penalty = _lrc_variant_penalty(lrc)
    matched_cues = len(_matched_cue_ids(alignment))
    matched_rows = sum(
        1 for row in alignment if isinstance(row, Mapping) and row.get("matched_cue_id") is not None
    )
    provider_penalty = 0 if lrc.provider == "lrclib" else 1
    stable_tail = (provider_penalty, lrc.source_ref)
    if _preferred_title_keys(preferred_title_hints):
        return (
            -int(sufficiently_complete),
            -int(exact_hint),
            variant_penalty,
            -matched_cues,
            -matched_rows,
            -line_count,
            -span_ms,
            -float(ratio),
            *stable_tail,
        )
    return (
        -int(sufficiently_complete),
        -matched_cues,
        -matched_rows,
        variant_penalty,
        -line_count,
        -span_ms,
        -float(ratio),
        *stable_tail,
    )


def _audio_lrc_retry_candidates(
    ranked: Sequence[tuple[float, LrcResult, list[dict[str, object]]]],
    *,
    primary: LrcResult,
    preferred_title_hints: Sequence[str],
    max_attempts: int,
) -> list[LrcResult]:
    """Return a deduped, bounded list of independently timed same-song variants."""

    hard_cap = min(MAX_AUDIO_LRC_VARIANT_ATTEMPTS, max(1, int(max_attempts)))
    primary_family = _lrc_title_family(primary)
    preferred_families = {
        normalize_lyric_text(hint)
        for hint in preferred_title_hints
        if isinstance(hint, str) and normalize_lyric_text(hint)
    }
    primary_entry = next(
        (entry for entry in ranked if entry[1].source_ref == primary.source_ref),
        None,
    )
    if primary_entry is None:
        primary_entry = (0.0, primary, [])
    eligible: list[tuple[float, LrcResult, list[dict[str, object]]]] = [primary_entry]
    for entry in ranked:
        ratio, item, alignment = entry
        if item.source_ref == primary.source_ref or len(item.lines) < 8 or ratio < 0.08:
            continue
        item_family = _lrc_title_family(item)
        same_preferred_family = bool(
            primary_family and primary_family in preferred_families and item_family == primary_family
        )
        cue_overlap, cue_coverage, shared_cues = _matched_cue_overlap(
            primary_entry[2], alignment
        )
        independently_same_song = (
            _lrc_identity_key(item) == _lrc_identity_key(primary)
            or _lrc_fingerprint(item) == _lrc_fingerprint(primary)
            or _lrc_content_similarity(item, primary) >= 0.62
            or (
                item_family == primary_family
                and shared_cues >= 5
                and cue_overlap >= 0.80
                and cue_coverage >= 0.25
            )
        )
        if same_preferred_family or independently_same_song:
            eligible.append(entry)

    max_lines = max((len(entry[1].lines) for entry in eligible), default=len(primary.lines))
    max_span = max((_lrc_span_ms(entry[1]) for entry in eligible), default=_lrc_span_ms(primary))
    ordered = [
        primary_entry,
        *sorted(
            eligible[1:],
            key=lambda entry: _audio_lrc_entry_sort_key(
                entry,
                preferred_title_hints=preferred_title_hints,
                max_line_count=max_lines,
                max_span_ms=max_span,
            ),
        ),
    ]
    deduped: list[LrcResult] = []
    seen_fingerprints: set[str] = set()
    for _ratio, item, _alignment in ordered:
        fingerprint = _lrc_fingerprint(item)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        deduped.append(primary if item.source_ref == primary.source_ref else item)
        if len(deduped) >= hard_cap:
            break
    return deduped


def _audio_lrc_infrastructure_reason(exc: Exception) -> str | None:
    """Keep provider outages recoverable instead of spending variant budget."""

    detail = f"{type(exc).__name__}: {exc}".casefold()
    if "agy_quota_exhausted" in detail or "quota" in detail or "429" in detail or "rate limit" in detail:
        return "AGY_QUOTA_EXHAUSTED"
    if "timeout" in detail or "timed out" in detail:
        return "AGY_TIMEOUT"
    if "agy_empty_output" in detail or "without alignment.json" in detail or "empty output" in detail:
        return "AGY_EMPTY_OUTPUT"
    if "agy_failed_rc" in detail or "failed rc=" in detail:
        return "AGY_FAILED_RC"
    return None


def _choose_audio_lrc_candidate(
    ranked: Sequence[tuple[float, LrcResult, list[dict[str, object]]]],
    *,
    pinned_lrc_results: Sequence[LrcResult],
    min_recall_ratio: float,
    min_margin: float,
    preferred_title_hints: Sequence[str] = (),
) -> LrcResult:
    """Choose one deterministic lyric identity before an expensive audio pass.

    Provider records with the same normalized title+artist are one identity
    even when their synced timestamps differ slightly.  A weak or tied signal
    between *different songs* is intentionally not enough: feeding an
    arbitrary LRC to a multimodal model invites it to force the supplied lyrics
    onto unrelated audio.
    """

    entries = list(ranked)
    identities = [_lrc_identity_key(lrc) for _ratio, lrc, _alignment in entries]
    fingerprints = [_lrc_fingerprint(lrc) for _ratio, lrc, _alignment in entries]
    equivalent: list[list[bool]] = [
        [left == right for right in range(len(entries))]
        for left in range(len(entries))
    ]
    for left in range(len(entries)):
        for right in range(left + 1, len(entries)):
            # Provider records are the same song when either title+artist or
            # the complete canonical timed lyric agrees. Fuzzy evidence below
            # is deliberately kept pairwise; clustering enforces complete-link.
            same_title = bool(
                _lrc_title_family(entries[left][1])
                and _lrc_title_family(entries[left][1]) == _lrc_title_family(entries[right][1])
            )
            content_similarity = _lrc_content_similarity(entries[left][1], entries[right][1])
            cue_overlap, cue_coverage, shared_cues = _matched_cue_overlap(
                entries[left][2], entries[right][2]
            )
            same_family = (
                identities[left] == identities[right]
                or fingerprints[left] == fingerprints[right]
                # Same normalized title plus either textual agreement or at
                # least three high-overlap source cues handles provider line
                # splitting without merging same-title homonyms on title alone.
                or (
                    same_title
                    and (
                        content_similarity >= 0.62
                        or (
                            shared_cues >= 5
                            and cue_overlap >= 0.80
                            and cue_coverage >= 0.60
                        )
                    )
                )
                # Close aliases such as 园游会/游园会 require both lyric-text
                # agreement and near-containment of actually matched ASR cues.
                or (
                    content_similarity >= 0.70
                    and shared_cues >= 5
                    and cue_overlap >= 0.90
                    and cue_coverage >= 0.30
                )
            )
            equivalent[left][right] = same_family
            equivalent[right][left] = same_family

    # A partial lyric can be similar to two unrelated full songs, so pairwise
    # similarity is not transitive. Complete-link clusters require direct
    # evidence between every pair and cannot bridge A--B--C when A !~ C.
    group_indices: list[list[int]] = []
    for index in sorted(
        range(len(entries)),
        key=lambda item: (-entries[item][0], entries[item][1].source_ref),
    ):
        compatible = [
            group_index
            for group_index, members in enumerate(group_indices)
            if all(equivalent[index][member] for member in members)
        ]
        if compatible:
            group_indices[compatible[0]].append(index)
        else:
            group_indices.append([index])
    groups = [[entries[index] for index in members] for members in group_indices]
    if not groups:
        raise ValueError("no canonical LRC identity is available for audio alignment")
    exact_title_groups = [
        group
        for group in groups
        if max(item[0] for item in group) >= min_recall_ratio
        and any(_lrc_title_is_exact_hint(item[1], preferred_title_hints) for item in group)
    ]
    if len(exact_title_groups) > 1:
        raise ValueError(
            "ambiguous exact-title LRC identity: multiple disconnected songs match the explicit title hint"
        )
    preferred_group = exact_title_groups[0] if exact_title_groups else None
    pinned_identities = {_lrc_identity_key(item) for item in pinned_lrc_results}
    pinned_fingerprints = {_lrc_fingerprint(item) for item in pinned_lrc_results}
    grouped = [
        (
            max(item[0] for item in group_entries),
            any(
                _lrc_identity_key(item) in pinned_identities or _lrc_fingerprint(item) in pinned_fingerprints
                for _ratio, item, _alignment in group_entries
            ),
            min(item.source_ref for _ratio, item, _alignment in group_entries),
            group_entries,
        )
        for group_entries in groups
    ]
    # Recall remains the primary authority.  For an *exact* top-recall tie,
    # prefer the one identity already pinned by >=2 known-song fingerprint
    # lines.  Previously the disjoint-set root index broke ties, so the real
    # 《屑屑》 pin could lose arbitrarily to a Studio Live/provider variant with
    # the same 41/52 ASR recall and the audio verifier was never reached.
    grouped.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    preferred_top = preferred_group is not None
    variant_family_top = False
    variant_family: str | None = None
    if preferred_group is not None:
        preferred_row = next(item for item in grouped if item[3] is preferred_group)
        grouped.remove(preferred_row)
        grouped.insert(0, preferred_row)
    else:
        # A truncated Remix/Live/Piano row can form its own strict clique and
        # win on ratio alone.  If the natural top belongs to a decorated title
        # family, compare every clique in that same base-title family using
        # evidence mass/fullness; this does not merge unrelated plain-title
        # homonyms and other song families still participate in the margin
        # gate below.
        natural_top_entries = grouped[0][3]
        natural_top_entry = max(natural_top_entries, key=lambda item: item[0])
        natural_family = _lrc_title_family(natural_top_entry[1])
        family_rows = [
            entry
            for _ratio, _pinned, _key, group_entries in grouped
            for entry in group_entries
            if natural_family and _lrc_title_family(entry[1]) == natural_family
        ]
        if len(family_rows) > 1 and any(_lrc_variant_penalty(entry[1]) for entry in family_rows):
            family_max_lines = max(len(entry[1].lines) for entry in family_rows)
            family_max_span = max(_lrc_span_ms(entry[1]) for entry in family_rows)
            family_grouped = [
                row
                for row in grouped
                if any(
                    _lrc_title_family(entry[1]) == natural_family
                    for entry in row[3]
                )
            ]
            preferred_family_row = min(
                family_grouped,
                key=lambda row: min(
                    _audio_lrc_entry_sort_key(
                        entry,
                        preferred_title_hints=(),
                        max_line_count=family_max_lines,
                        max_span_ms=family_max_span,
                    )
                    for entry in row[3]
                    if _lrc_title_family(entry[1]) == natural_family
                ),
            )
            grouped.remove(preferred_family_row)
            grouped.insert(0, preferred_family_row)
            variant_family_top = True
            variant_family = natural_family
    top_ratio, is_curated, _top_key, top_entries = grouped[0]
    top_indices_for_margin = {
        index
        for index, entry in enumerate(entries)
        if entry[0] == top_ratio and any(entry is top for top in top_entries)
    }
    competing_groups = []
    for group in grouped[1:]:
        group_ratio, _pinned, _key, group_entries = group
        if variant_family_top and any(
            _lrc_title_family(entry[1]) == variant_family
            for entry in group_entries
        ):
            continue
        # Equal-best disconnected cliques remain a hard ambiguity. A lower-
        # recall provider variant that directly matches any member of the top
        # core is the same song for margin purposes, even if complete-link
        # correctly kept it outside the canonical selection clique.
        if group_ratio < top_ratio:
            max_row_indices = {
                index
                for index, entry in enumerate(entries)
                if entry[0] == group_ratio and any(entry is row for row in group_entries)
            }
            if max_row_indices and all(
                any(
                    equivalent[row_index][top_index]
                    for top_index in top_indices_for_margin
                )
                for row_index in max_row_indices
            ):
                continue
        competing_groups.append(group)
    second_ratio = competing_groups[0][0] if competing_groups else 0.0
    curated_top_ties = [
        group for group in grouped if group[0] == top_ratio and group[1]
    ]
    if len(curated_top_ties) > 1 and not preferred_top:
        raise ValueError(
            "ambiguous curated LRC identity: multiple pinned songs share the best ASR recall; "
            "refusing to choose one before audio verification"
        )
    if is_curated:
        if top_ratio < 0.08:
            raise ValueError(
                f"curated LRC identity has only {top_ratio:.0%} ASR recall; refusing to force it onto audio"
            )
    elif top_ratio < min_recall_ratio or (
        not preferred_top and top_ratio - second_ratio < min_margin
    ):
        raise ValueError(
            "ambiguous low-ASR LRC identity: "
            f"best={top_ratio:.0%}, runner-up={second_ratio:.0%}, "
            f"need best>={min_recall_ratio:.0%} and margin>={min_margin:.0%}"
        )
    # Always retain the strongest acoustic-discovery member of a fuzzy lyric
    # family.  LRCLIB is only a deterministic tie-break; preferring a weaker
    # truncated LRCLIB subset can move the canonical song boundary.
    max_line_count = max(len(item[1].lines) for item in top_entries)
    max_span_ms = max(_lrc_span_ms(item[1]) for item in top_entries)
    selected_lrc = sorted(
        top_entries,
        key=lambda item: _audio_lrc_entry_sort_key(
            item,
            preferred_title_hints=preferred_title_hints,
            max_line_count=max_line_count,
            max_span_ms=max_span_ms,
        ),
    )[0][1]

    # Catalog aliases can carry the right synced lyrics under a misleading
    # display title (real July 10 example: 园游会 lyrics under
    # ``Owen-只想为你撑伞``). Preserve the selected lyric bytes/source, but use
    # the title family corroborated by the most independent providers. Within
    # that family the shortest display form drops Cover/Live decorations.
    family_rows: dict[str, list[LrcResult]] = {}
    top_indices = {
        index
        for index, entry in enumerate(entries)
        if any(entry is top_entry for top_entry in top_entries)
    }
    for _ratio, item, _alignment in top_entries:
        family = _lrc_title_family(item)
        if family:
            family_rows.setdefault(family, []).append(item)
    # Complete-link is intentionally strict for identity choice, but a lower-
    # recall provider can still corroborate a title already present in the top
    # clique. Admit only same-family rows directly equivalent to at least one
    # top member of that family; this strengthens naming without reopening an
    # A--B--C identity bridge or introducing a new title family.
    for index, (_ratio, item, _alignment) in enumerate(entries):
        if index in top_indices:
            continue
        family = _lrc_title_family(item)
        if family not in family_rows:
            continue
        if any(
            equivalent[index][top_index]
            and _lrc_title_family(entries[top_index][1]) == family
            for top_index in top_indices
        ):
            family_rows[family].append(item)
    if family_rows:
        selected_family = _lrc_title_family(selected_lrc)
        provider_votes = {
            family: len({item.provider for item in rows})
            for family, rows in family_rows.items()
        }
        winning_votes = max(provider_votes.values())
        winners = [
            family for family, votes in provider_votes.items() if votes == winning_votes
        ]
        selected_votes = provider_votes.get(selected_family, 0)
        # Each provider family gets one vote. Rewrite only for a unique winner
        # with >=2 independent providers and strictly more evidence than the
        # acoustically selected title; a 2-vs-2 tie preserves the source name.
        if len(winners) == 1 and winning_votes >= 2 and winning_votes > selected_votes:
            corroborated = family_rows[winners[0]]
            canonical_title = min(
                (
                    str(item.song_title).strip()
                    for item in corroborated
                    if str(item.song_title).strip()
                ),
                key=lambda title: (len(title), title),
                default=str(selected_lrc.song_title),
            )
            if canonical_title != selected_lrc.song_title:
                selected_lrc = LrcResult(
                    provider=selected_lrc.provider,
                    song_title=canonical_title,
                    artist=selected_lrc.artist,
                    source_ref=selected_lrc.source_ref,
                    lines=selected_lrc.lines,
                )
    return selected_lrc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_bound_artifact(path_value: str, expected_sha256: str, label: str) -> Path:
    path = Path(path_value)
    if not path.is_file():
        raise ValueError(f"{label} artifact is missing: {path}")
    actual = _sha256_file(path)
    if actual != str(expected_sha256).lower().removeprefix("sha256:"):
        raise ValueError(f"{label} sha256 mismatch")
    return path


def load_audio_lrc_json_artifact(path: Path, label: str) -> object:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"{label} cannot be read: {exc}") from exc
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().casefold() in {"```", "```json"}:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON: {exc}") from exc


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validated_audio_lrc_selection(
    *,
    run: AudioLrcAlignmentRun,
    lrc: LrcResult,
    candidate_id: str,
    source_media_path: Path,
    source_duration_ms: int,
    min_matched_ratio: float,
) -> tuple[float, LrcResult, list[dict[str, object]], list[dict[str, object]], int, int, int, int, int]:
    """Validate an AGY audio observation and convert it into standard proof.

    The validator ignores any model-supplied title, offset, boundary, verdict,
    or completeness claim.  It binds the current media/LRC bytes and derives
    the single global shift from one exact observation per canonical LRC line.
    """

    if run.provider != "agy" or run.model != "Gemini 3.5 Flash (High)":
        raise ValueError(f"audio aligner must be agy Gemini 3.5 Flash (High), got {run.provider} {run.model}")
    if run.rc != 0 or run.provider_fallback_used:
        raise ValueError(f"audio aligner was not a clean no-fallback run (rc={run.rc})")
    if not source_media_path.is_file():
        raise ValueError(f"current source media is missing: {source_media_path}")
    try:
        current_source_origin = str(source_media_path.resolve(strict=True))
        declared_source_origin = str(Path(run.source_origin_path).resolve(strict=True))
    except OSError as exc:
        raise ValueError(f"audio source origin cannot be resolved: {exc}") from exc
    if declared_source_origin != current_source_origin:
        raise ValueError("audio observation source origin is not the current source media")
    source_path = _require_bound_artifact(run.source_path, run.source_sha256, "audio source")
    if _sha256_file(source_media_path) != run.source_sha256 or _sha256_file(source_path) != run.source_sha256:
        raise ValueError("audio observation is not bound to the current source media")
    lrc_path = _require_bound_artifact(run.lrc_path, run.lrc_sha256, "canonical LRC")
    _require_bound_artifact(run.prompt_path, run.prompt_sha256, "audio prompt")
    if not run.provider_raw_output_path or not run.provider_raw_output_sha256:
        raise ValueError("provider raw audio alignment binding is missing")
    provider_raw_path = _require_bound_artifact(
        run.provider_raw_output_path,
        run.provider_raw_output_sha256,
        "provider raw audio alignment",
    )
    canonical_output_path = _require_bound_artifact(
        run.output_path,
        run.output_sha256,
        "canonicalized audio alignment",
    )
    manifest_path = _require_bound_artifact(run.manifest_path, run.manifest_sha256, "audio run manifest")
    try:
        run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"audio run manifest is invalid JSON: {exc}") from exc
    manifest_artifacts = run_manifest.get("artifacts") if isinstance(run_manifest, Mapping) else None
    if (
        not isinstance(run_manifest, Mapping)
        or run_manifest.get("schema_version") != AGY_AUDIO_LRC_RUN_SCHEMA_VERSION
        or run_manifest.get("candidate_id") != candidate_id
        or run_manifest.get("provider") != run.provider
        or run_manifest.get("model") != run.model
        or run_manifest.get("agy_rc") != run.rc
        or run_manifest.get("provider_fallback_used") is not run.provider_fallback_used
        or run_manifest.get("sandbox") is not True
        or not isinstance(manifest_artifacts, Mapping)
        or any(
            manifest_artifacts.get(key) != value
            for key, value in (
                ("source_path", run.source_path),
                ("source_origin_path", run.source_origin_path),
                ("source_sha256", run.source_sha256),
                ("source_duration_ms", run.source_duration_ms),
                ("lrc_path", run.lrc_path),
                ("lrc_sha256", run.lrc_sha256),
                ("prompt_path", run.prompt_path),
                ("prompt_sha256", run.prompt_sha256),
                ("provider_raw_output_path", run.provider_raw_output_path),
                ("provider_raw_output_sha256", run.provider_raw_output_sha256),
                ("output_path", run.output_path),
                ("output_sha256", run.output_sha256),
            )
        )
    ):
        raise ValueError("audio run manifest is not bound to the current run artifacts")
    canonicalization = run_manifest.get("canonicalization")
    expected_canonicalization = {
        "strategy": AGY_AUDIO_LRC_CANONICALIZATION_STRATEGY,
        "row_identity": "strict_zero_based_lrc_index",
        "restored_fields": ["lrc_time_ms", "text"],
        "row_count": len(lrc.lines),
        "canonical_lrc_sha256": run.lrc_sha256,
        "provider_raw_output_sha256": run.provider_raw_output_sha256,
        "canonicalized_output_sha256": run.output_sha256,
    }
    if canonicalization != expected_canonicalization:
        raise ValueError("audio run canonicalization manifest is invalid")
    parsed_lrc = parse_lrc_text(lrc_path.read_text(encoding="utf-8"))
    if [(line.time_ms, line.text) for line in parsed_lrc] != [(line.time_ms, line.text) for line in lrc.lines]:
        raise ValueError("bound LRC artifact does not equal the selected canonical LRC")
    if not _is_int(run.source_duration_ms) or abs(run.source_duration_ms - source_duration_ms) > 1_000:
        raise ValueError(
            f"audio source duration {run.source_duration_ms}ms does not match job duration {source_duration_ms}ms"
        )
    effective_duration_ms = min(run.source_duration_ms, source_duration_ms)

    provider_payload = load_audio_lrc_json_artifact(
        provider_raw_path,
        "provider raw audio alignment",
    )
    if not isinstance(provider_payload, Mapping):
        raise ValueError("provider raw audio alignment is not a JSON object")
    expected_payload = canonicalize_audio_lrc_observation(provider_payload, lrc)
    canonical_artifact_payload = load_audio_lrc_json_artifact(
        canonical_output_path,
        "canonicalized audio alignment",
    )
    if canonical_artifact_payload != expected_payload or run.payload != expected_payload:
        raise ValueError("canonicalized audio alignment is not the deterministic exact-index projection")

    payload = run.payload
    required_top = {
        "schema_version",
        "record",
        "observations",
        "spot_checks",
        "live_performance",
        "post_song_talk_start_ms",
    }
    if not isinstance(payload, Mapping) or set(payload) != required_top:
        raise ValueError("audio observation top-level schema/keys are invalid")
    if payload.get("schema_version") != AGY_AUDIO_LRC_OBSERVATION_SCHEMA_VERSION:
        raise ValueError("audio observation schema_version is invalid")
    record = payload.get("record")
    if not isinstance(record, Mapping) or set(record) != {
        "attempt_id",
        "candidate_id",
        "source_sha256",
        "lrc_sha256",
        "source_duration_ms",
    }:
        raise ValueError("audio observation record is invalid")
    if record.get("candidate_id") != candidate_id:
        raise ValueError("audio observation candidate_id mismatch")
    if record.get("source_sha256") != run.source_sha256 or record.get("lrc_sha256") != run.lrc_sha256:
        raise ValueError("audio observation echoed artifact hash mismatch")
    if record.get("source_duration_ms") != run.source_duration_ms:
        raise ValueError("audio observation echoed duration mismatch")
    attempt_id = record.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("audio observation attempt_id is missing")

    observations = payload.get("observations")
    if not isinstance(observations, list) or len(observations) != len(lrc.lines) or len(observations) < 8:
        raise ValueError("audio observation must contain exactly one row per canonical LRC line")
    alignment: list[dict[str, object]] = []
    residuals: list[int] = []
    previous_start: int | None = None
    previous_end: int | None = None
    for index, (row, line) in enumerate(zip(observations, lrc.lines)):
        if not isinstance(row, Mapping) or set(row) != {
            "lrc_index",
            "lrc_time_ms",
            "text",
            "heard",
            "live_start_ms",
            "live_end_ms",
            "confidence",
            *LYRIC_VOCAL_ASSERTION_KEYS,
        }:
            raise ValueError(f"audio observation row {index} has invalid keys")
        if row.get("lrc_index") != index or row.get("lrc_time_ms") != line.time_ms or row.get("text") != line.text:
            raise ValueError(f"audio observation row {index} does not exactly echo the canonical LRC")
        # v1 burns every canonical lyric line.  Until the materializer supports
        # a performed-only sequence, skipped/repeated/changed arrangements must
        # block instead of silently burning studio lyrics that were not sung.
        if row.get("heard") is not True:
            raise ValueError(f"canonical LRC line {index} was not affirmatively heard")
        start_ms = row.get("live_start_ms")
        end_ms = row.get("live_end_ms")
        confidence = row.get("confidence")
        if not (_is_int(start_ms) and _is_int(end_ms) and 0 <= start_ms < end_ms <= effective_duration_ms):
            raise ValueError(f"audio observation row {index} timing is invalid")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.8 <= confidence <= 1.0:
            raise ValueError(f"audio observation row {index} confidence is below 0.8")
        if previous_start is not None and start_ms <= previous_start:
            raise ValueError("audio observation starts are not strictly monotonic")
        if previous_end is not None and previous_end - start_ms > 250:
            raise ValueError("adjacent audio observations overlap by more than 250ms")
        residuals.append(start_ms - line.time_ms)
        alignment.append(
            {
                "lrc_time_ms": line.time_ms,
                "lrc_text": line.text,
                "matched_cue_id": f"agy-audio:{run.output_sha256[:12]}:line-{index}",
                "cue_start_ms": start_ms,
                "cue_end_ms": end_ms,
                "match_ratio": round(float(confidence), 4),
                "evidence_source": "agy_audio_lrc",
                "lyric_vocal_subject": row.get("lyric_vocal_subject"),
                "lidousha_role": row.get("lidousha_role"),
                "same_live_vocal_source_as_lidousha": row.get(
                    "same_live_vocal_source_as_lidousha"
                ),
                "other_singer_or_harmony_audible": row.get(
                    "other_singer_or_harmony_audible"
                ),
                "recorded_or_playback_vocal_audible": row.get(
                    "recorded_or_playback_vocal_audible"
                ),
            }
        )
        previous_start = start_ms
        previous_end = end_ms

    offset_ms = sorted(residuals)[len(residuals) // 2]
    if any(abs(residual - offset_ms) > 1_500 for residual in residuals):
        raise ValueError("audio observations do not fit one global shift within ±1500ms")
    lrc_span = lrc.lines[-1].time_ms - lrc.lines[0].time_ms
    live_span = int(alignment[-1]["cue_start_ms"]) - int(alignment[0]["cue_start_ms"])
    if lrc_span < 20_000 or not 0.95 <= live_span / lrc_span <= 1.05:
        raise ValueError("audio observations imply tempo drift; explicit stretch proof is required")
    if not 0 <= offset_ms < effective_duration_ms:
        raise ValueError(f"nominal LRC zero {offset_ms}ms is outside the current source")

    first_lyric_start_ms = int(alignment[0]["cue_start_ms"])
    last_lyric_end_ms = int(alignment[-1]["cue_end_ms"])
    live_performance = payload.get("live_performance")
    performance_schema_error = validate_live_performance_observation(
        live_performance,
        first_lyric_start_ms=first_lyric_start_ms,
        last_lyric_end_ms=last_lyric_end_ms,
        observations=observations,
        require_ready=False,
    )
    if performance_schema_error is not None:
        raise ValueError(performance_schema_error)
    performance_error = validate_live_performance_observation(
        live_performance,
        first_lyric_start_ms=first_lyric_start_ms,
        last_lyric_end_ms=last_lyric_end_ms,
        observations=observations,
        require_ready=True,
    )
    if performance_error is not None:
        assert isinstance(live_performance, Mapping)
        raise LivePerformanceRejected(performance_error, live_performance)

    post_song_talk_start_ms = payload.get("post_song_talk_start_ms")
    if post_song_talk_start_ms is None:
        clip_end_ms = effective_duration_ms
        instrumental_spot_end_ms = last_lyric_end_ms
    elif _is_int(post_song_talk_start_ms) and last_lyric_end_ms <= post_song_talk_start_ms <= effective_duration_ms:
        clip_end_ms = post_song_talk_start_ms
        instrumental_spot_end_ms = post_song_talk_start_ms
    else:
        raise ValueError("post-song talk boundary is invalid or precedes the last lyric")

    spot_checks = payload.get("spot_checks")
    required_spots = {"first_line", "chorus", "repeated_section", "longest_instrumental_gap", "tail"}
    if not isinstance(spot_checks, list) or len(spot_checks) != 5:
        raise ValueError("audio observation must include exactly five spot checks")
    spots: dict[str, Mapping[str, object]] = {}
    for spot in spot_checks:
        if not isinstance(spot, Mapping) or set(spot) != {"name", "live_time_ms", "result", "notes"}:
            raise ValueError("audio spot-check schema is invalid")
        name = spot.get("name")
        time_ms = spot.get("live_time_ms")
        if name not in required_spots or name in spots or spot.get("result") != "OK":
            raise ValueError("audio spot-check names/results are invalid")
        if not _is_int(time_ms):
            raise ValueError(f"audio spot-check {name} time is outside the performed song")
        if name == "longest_instrumental_gap":
            # The longest instrumental can be an intro before the first lyric
            # or an outro after the last. It still must lie inside the
            # independently derived full-song boundary.
            in_allowed_range = offset_ms <= time_ms <= instrumental_spot_end_ms
        else:
            in_allowed_range = first_lyric_start_ms <= time_ms <= last_lyric_end_ms
        if not in_allowed_range:
            raise ValueError(f"audio spot-check {name} time is outside the performed song")
        spots[str(name)] = spot
    if set(spots) != required_spots:
        raise ValueError("audio spot-check set is incomplete")
    if abs(int(spots["first_line"]["live_time_ms"]) - first_lyric_start_ms) > 1_500:
        raise ValueError("first-line spot check does not bind the first observed lyric")
    tail_spot_ms = int(spots["tail"]["live_time_ms"])
    final_lyric_start_ms = int(alignment[-1]["cue_start_ms"])
    final_lyric_end_ms = int(alignment[-1]["cue_end_ms"])
    if not final_lyric_start_ms <= tail_spot_ms < final_lyric_end_ms:
        raise ValueError("tail spot check does not bind the final observed lyric")

    first_index_by_text: dict[str, int] = {}
    later_repeat_starts: list[int] = []
    for index, line in enumerate(lrc.lines):
        normalized = normalize_lyric_text(line.text)
        if not normalized:
            continue
        if normalized in first_index_by_text:
            later_repeat_starts.append(int(alignment[index]["cue_start_ms"]))
        else:
            first_index_by_text[normalized] = index
    if later_repeat_starts:
        repeated_spot_ms = int(spots["repeated_section"]["live_time_ms"])
        if all(abs(repeated_spot_ms - start_ms) > 1_500 for start_ms in later_repeat_starts):
            raise ValueError("repeated-section spot check does not bind a later repeated lyric occurrence")

    clip_start_ms = offset_ms
    if not 0 <= clip_start_ms <= offset_ms <= first_lyric_start_ms <= last_lyric_end_ms <= clip_end_ms <= effective_duration_ms:
        raise ValueError("derived full-song boundary ordering is invalid")
    matched_ratio = len(alignment) / len(lrc.lines)
    if matched_ratio < min_matched_ratio:
        raise ValueError(f"audio alignment ratio {matched_ratio:.0%} is below {min_matched_ratio:.0%}")
    matched = [dict(entry) for entry in alignment]
    return (
        matched_ratio,
        lrc,
        alignment,
        matched,
        first_lyric_start_ms,
        last_lyric_end_ms,
        clip_start_ms,
        clip_end_ms,
        offset_ms,
    )


def _lyric_row_interval(row: Mapping[str, object], index: int) -> tuple[int, int]:
    """Read one strict raw-AGY or projected-report lyric interval."""

    has_live = "live_start_ms" in row or "live_end_ms" in row
    has_cue = "cue_start_ms" in row or "cue_end_ms" in row
    if has_live and not {"live_start_ms", "live_end_ms"}.issubset(row):
        raise ValueError(f"live performance lyric row {index} has a partial raw interval")
    if has_cue and not {"cue_start_ms", "cue_end_ms"}.issubset(row):
        raise ValueError(f"live performance lyric row {index} has a partial report interval")
    if not has_live and not has_cue:
        raise ValueError(f"live performance lyric row {index} timing is missing")
    live_pair = (row.get("live_start_ms"), row.get("live_end_ms")) if has_live else None
    cue_pair = (row.get("cue_start_ms"), row.get("cue_end_ms")) if has_cue else None
    if live_pair is not None and cue_pair is not None and live_pair != cue_pair:
        raise ValueError(f"live performance lyric row {index} raw/report intervals conflict")
    selected_pair = live_pair if live_pair is not None else cue_pair
    assert selected_pair is not None
    start_ms, end_ms = selected_pair
    if not (_is_int(start_ms) and _is_int(end_ms) and 0 <= start_ms < end_ms):
        raise ValueError(f"live performance lyric row {index} timing is invalid")
    return int(start_ms), int(end_ms)


def _validate_lyric_vocal_observations(
    observations: object,
    *,
    require_ready: bool,
) -> tuple[bool, bool, bool]:
    """Recompute the singer/role aggregates from every canonical lyric row.

    The AGY top-level summary is never trusted as a substitute for the rows.
    A READY result requires each line to say that the same live lyric source is
    李豆沙 herself, with no guest/duet/harmony or recorded vocal audible.  A
    narrowly labelled canonical spoken passage is allowed only inside an
    otherwise predominantly sung performance; ordinary speech over music is
    not.  CAM++ remains an independent speaker-similarity subclaim and is not
    treated here (or elsewhere) as a singing classifier.
    """

    if (
        not isinstance(observations, Sequence)
        or isinstance(observations, (str, bytes, bytearray))
        or not observations
    ):
        raise ValueError("live performance lyric-source observations are missing")
    all_same_lidousha = True
    any_other_singer = False
    any_recorded_vocal = False
    singing_rows = 0
    consecutive_spoken_rows = 0
    longest_spoken_run = 0
    spoken_blocks = 0
    spoken_duration_ms = 0
    total_lyric_vocal_duration_ms = 0
    spoken_block_start_ms: int | None = None
    longest_spoken_block_span_ms = 0
    previous_start_ms: int | None = None
    previous_end_ms: int | None = None
    roles: list[object] = []
    for index, row in enumerate(observations):
        if not isinstance(row, Mapping) or not LYRIC_VOCAL_ASSERTION_KEYS.issubset(row):
            raise ValueError(f"live performance lyric row {index} singer schema is invalid")
        subject = row.get("lyric_vocal_subject")
        role = row.get("lidousha_role")
        same_lidousha = row.get("same_live_vocal_source_as_lidousha")
        other_singer = row.get("other_singer_or_harmony_audible")
        recorded_vocal = row.get("recorded_or_playback_vocal_audible")
        if subject not in LYRIC_VOCAL_SUBJECTS or role not in LIDOUSHA_LYRIC_ROLES:
            raise ValueError(f"live performance lyric row {index} singer enum is invalid")
        if not all(isinstance(value, bool) for value in (same_lidousha, other_singer, recorded_vocal)):
            raise ValueError(f"live performance lyric row {index} singer assertions are invalid")

        live_lidousha_lyric = (
            subject == "LIDOUSHA"
            and role in {"SINGING_THIS_LYRIC", "PERFORMING_THIS_LYRIC_SPOKEN"}
            and other_singer is False
            and recorded_vocal is False
        )
        if same_lidousha is not live_lidousha_lyric:
            raise ValueError(f"live performance lyric row {index} same-subject assertion is inconsistent")
        if subject == "LIDOUSHA" and role not in {
            "SINGING_THIS_LYRIC",
            "PERFORMING_THIS_LYRIC_SPOKEN",
        }:
            raise ValueError(f"live performance lyric row {index} Li-Dousha role contradicts its subject")
        if role in {"SINGING_THIS_LYRIC", "PERFORMING_THIS_LYRIC_SPOKEN"} and subject != "LIDOUSHA":
            raise ValueError(f"live performance lyric row {index} performance role contradicts its subject")
        if subject == "OTHER_OR_MIXED_SINGER" and other_singer is not True:
            raise ValueError(f"live performance lyric row {index} other-singer assertion is inconsistent")
        if subject == "RECORDED_OR_PLAYBACK_SINGER" and recorded_vocal is not True:
            raise ValueError(f"live performance lyric row {index} recorded-vocal assertion is inconsistent")
        if subject == "NO_AUDIBLE_LYRIC_VOCAL" and (other_singer or recorded_vocal):
            raise ValueError(f"live performance lyric row {index} no-vocal assertion is inconsistent")
        if require_ready and not live_lidousha_lyric:
            raise ValueError(
                f"live performance lyric row {index} does not affirm the same live Li-Dousha lyric source"
            )
        roles.append(role)
        row_interval: tuple[int, int] | None = None
        if require_ready:
            start_ms, end_ms = _lyric_row_interval(row, index)
            row_interval = (start_ms, end_ms)
            if previous_start_ms is not None and start_ms <= previous_start_ms:
                raise ValueError("live performance lyric starts are not strictly monotonic")
            if previous_end_ms is not None and previous_end_ms - start_ms > 250:
                raise ValueError("live performance adjacent lyric rows overlap by more than 250ms")
            total_lyric_vocal_duration_ms += end_ms - start_ms
            previous_start_ms = start_ms
            previous_end_ms = end_ms
        if role == "SINGING_THIS_LYRIC":
            singing_rows += 1
            consecutive_spoken_rows = 0
            spoken_block_start_ms = None
        elif role == "PERFORMING_THIS_LYRIC_SPOKEN":
            if require_ready and consecutive_spoken_rows == 0:
                spoken_blocks += 1
                assert row_interval is not None
                spoken_block_start_ms = row_interval[0]
            consecutive_spoken_rows += 1
            longest_spoken_run = max(longest_spoken_run, consecutive_spoken_rows)
            if require_ready:
                assert row_interval is not None
                spoken_duration_ms += row_interval[1] - row_interval[0]
                assert spoken_block_start_ms is not None
                longest_spoken_block_span_ms = max(
                    longest_spoken_block_span_ms,
                    row_interval[1] - spoken_block_start_ms,
                )
        else:
            consecutive_spoken_rows = 0
            spoken_block_start_ms = None
        all_same_lidousha = all_same_lidousha and bool(same_lidousha)
        any_other_singer = any_other_singer or bool(other_singer)
        any_recorded_vocal = any_recorded_vocal or bool(recorded_vocal)
    if require_ready:
        if roles[0] != "SINGING_THIS_LYRIC" or roles[-1] != "SINGING_THIS_LYRIC":
            raise ValueError("live performance first and final canonical lyric rows must be sung")
        if singing_rows < MIN_READY_SUNG_LYRIC_ROWS or singing_rows / len(roles) < MIN_READY_SUNG_LYRIC_RATIO:
            raise ValueError(
                "live performance is not predominantly sung by Li Dousha: "
                f"{singing_rows}/{len(roles)} canonical lyric rows are sung"
            )
        if longest_spoken_run > MAX_READY_CONSECUTIVE_SPOKEN_LYRIC_ROWS:
            raise ValueError(
                "live performance canonical spoken passage is too long: "
                f"{longest_spoken_run} consecutive rows"
            )
        if spoken_blocks > MAX_READY_SPOKEN_BLOCKS:
            raise ValueError(
                "live performance has multiple canonical spoken passages: "
                f"{spoken_blocks} blocks"
            )
        if (
            spoken_duration_ms > MAX_READY_SPOKEN_LYRIC_DURATION_MS
            # Integer cross-multiplication keeps an exact 20% boundary from
            # becoming 20.000000000000004% through binary float rounding.
            or spoken_duration_ms * 5 > total_lyric_vocal_duration_ms
        ):
            raise ValueError(
                "live performance canonical spoken passage is too long by voiced duration: "
                f"{spoken_duration_ms}/{total_lyric_vocal_duration_ms}ms"
            )
        if longest_spoken_block_span_ms > MAX_READY_SPOKEN_BLOCK_SPAN_MS:
            raise ValueError(
                "live performance canonical spoken block span is too long: "
                f"{longest_spoken_block_span_ms}ms"
            )
    return all_same_lidousha, any_other_singer, any_recorded_vocal


def validate_live_performance_observation(
    performance: object,
    *,
    first_lyric_start_ms: int,
    last_lyric_end_ms: int,
    observations: object,
    require_ready: bool,
) -> str | None:
    """Validate AGY's anti-background and same-subject singing observation.

    AGY must assert the active lyric vocalist and Li-Dousha's role on every
    canonical line.  Final delivery additionally combines this with the
    independently generated Li-Dousha voiceprint claim on the same lyric rows.
    """

    try:
        if not isinstance(performance, Mapping) or set(performance) != {
            "mode",
            "confidence",
            "continuous_live_song_performance",
            "background_recording_likelihood",
            "same_lidousha_live_performer_across_all_lyrics",
            "other_singer_or_harmony_present",
            "recorded_or_playback_vocal_present",
            "evidence",
            "notes",
        }:
            raise ValueError("live performance observation schema is invalid")
        mode = performance.get("mode")
        if mode not in LIVE_PERFORMANCE_MODES:
            raise ValueError("live performance observation mode is invalid")
        confidence = performance.get("confidence")
        background = performance.get("background_recording_likelihood")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= float(confidence) <= 1.0
            or isinstance(background, bool)
            or not isinstance(background, (int, float))
            or not 0.0 <= float(background) <= 1.0
            or not isinstance(performance.get("continuous_live_song_performance"), bool)
            or not isinstance(performance.get("same_lidousha_live_performer_across_all_lyrics"), bool)
            or not isinstance(performance.get("other_singer_or_harmony_present"), bool)
            or not isinstance(performance.get("recorded_or_playback_vocal_present"), bool)
            or not isinstance(performance.get("notes"), str)
            or not str(performance.get("notes")).strip()
        ):
            raise ValueError("live performance observation values are invalid")
        all_same_lidousha, any_other_singer, any_recorded_vocal = _validate_lyric_vocal_observations(
            observations,
            require_ready=require_ready,
        )
        if (
            performance.get("same_lidousha_live_performer_across_all_lyrics") is not all_same_lidousha
            or performance.get("other_singer_or_harmony_present") is not any_other_singer
            or performance.get("recorded_or_playback_vocal_present") is not any_recorded_vocal
        ):
            raise ValueError("live performance top-level performer assertions do not match lyric rows")
        evidence = performance.get("evidence")
        if not isinstance(evidence, list) or len(evidence) != 3:
            raise ValueError("live performance observation needs exactly three evidence timestamps")
        if not 0 <= first_lyric_start_ms < last_lyric_end_ms:
            raise ValueError("live performance lyric span is invalid")
        span = last_lyric_end_ms - first_lyric_start_ms
        sung_intervals: list[tuple[int, int]] = []
        if require_ready:
            sung_intervals = [
                _lyric_row_interval(row, index)
                for index, row in enumerate(observations)
                if isinstance(row, Mapping) and row.get("lidousha_role") == "SINGING_THIS_LYRIC"
            ]
        buckets: set[int] = set()
        previous_time = -1
        for index, row in enumerate(evidence):
            if not isinstance(row, Mapping) or set(row) != {"time_ms", "observation"}:
                raise ValueError(f"live performance evidence[{index}] schema is invalid")
            time_ms = row.get("time_ms")
            observation = row.get("observation")
            if (
                not _is_int(time_ms)
                or not first_lyric_start_ms <= time_ms <= last_lyric_end_ms
                or int(time_ms) <= previous_time
                or not isinstance(observation, str)
                or not observation.strip()
            ):
                raise ValueError(f"live performance evidence[{index}] is invalid")
            if require_ready and not any(
                start_ms <= int(time_ms) < end_ms
                for start_ms, end_ms in sung_intervals
            ):
                raise ValueError(
                    f"live performance evidence[{index}] does not bind a sung canonical lyric row"
                )
            previous_time = int(time_ms)
            buckets.add(min(2, ((int(time_ms) - first_lyric_start_ms) * 3) // max(1, span)))
        if buckets != {0, 1, 2}:
            raise ValueError("live performance evidence must cover lyric head, middle, and tail")
        if require_ready and (
            mode != LIVE_PERFORMANCE_READY_MODE
            or performance.get("continuous_live_song_performance") is not True
            or performance.get("same_lidousha_live_performer_across_all_lyrics") is not True
            or performance.get("other_singer_or_harmony_present") is not False
            or performance.get("recorded_or_playback_vocal_present") is not False
            or float(confidence) < 0.85
            or float(background) > 0.20
        ):
            raise ValueError(f"live performance not proven: mode={mode}")
    except ValueError as exc:
        return str(exc)
    return None


def _longest_unmatched_run(alignment: Sequence[Mapping[str, object]]) -> int:
    longest = 0
    current = 0
    for entry in alignment:
        if entry["matched_cue_id"] is None:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _leading_unmatched(alignment: Sequence[Mapping[str, object]], *, tolerance: int = 1) -> int:
    count = 0
    for entry in alignment:
        if entry["matched_cue_id"] is None:
            count += 1
        else:
            break
    return count if count > tolerance else 0


def _trailing_unmatched(alignment: Sequence[Mapping[str, object]], *, tolerance: int = 1) -> int:
    count = 0
    for entry in reversed(alignment):
        if entry["matched_cue_id"] is None:
            count += 1
        else:
            break
    return count if count > tolerance else 0


def _http_json(url: str, *, timeout_seconds: float) -> Mapping[str, object] | None:
    payload = _http_json_value(url, timeout_seconds=timeout_seconds)
    return payload if isinstance(payload, Mapping) else None


def _http_json_value(
    url: str,
    *,
    timeout_seconds: float,
    request_headers: Mapping[str, str] | None = None,
) -> object:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Referer": "https://music.163.com/",
    }
    if request_headers:
        headers.update({str(key): str(value) for key, value in request_headers.items()})
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _coerce_lrc_results(found: LrcResult | Sequence[LrcResult] | None) -> list[LrcResult]:
    if found is None:
        return []
    if isinstance(found, LrcResult):
        return [found]
    return [item for item in found if isinstance(item, LrcResult)]


def _strip_kugou_markup(value: object) -> str:
    """Remove search-result highlighting while preserving the exact identity text."""

    return re.sub(r"<[^>]*>", "", str(value or "")).strip()


def _kugou_identity_text(value: object) -> str:
    return normalize_lyric_text(_strip_kugou_markup(value))


def _kugou_candidate_matches_track(
    candidate: Mapping[str, object],
    *,
    song_title: str,
    artist: str,
    duration_ms: int,
) -> bool:
    """Fail closed unless the lyric row belongs to this exact catalog track."""

    if _kugou_identity_text(candidate.get("song")) != _kugou_identity_text(song_title):
        return False
    if _kugou_identity_text(candidate.get("singer")) != _kugou_identity_text(artist):
        return False
    candidate_duration = candidate.get("duration")
    if isinstance(candidate_duration, bool) or not isinstance(candidate_duration, (int, float)):
        return False
    return abs(int(candidate_duration) - duration_ms) <= 3_000


def _filter_kugou_timed_metadata(
    lines: Sequence[LrcLine],
    *,
    song_title: str,
    artist: str,
) -> list[LrcLine]:
    """Drop Kugou's timed title card without dropping a real opening lyric."""

    title_identity = _kugou_identity_text(song_title)
    artist_identity = _kugou_identity_text(artist)
    filtered: list[LrcLine] = []
    for line in lines:
        line_identity = _kugou_identity_text(line.text)
        is_timed_title_card = (
            line.time_ms <= 1_000
            and bool(title_identity)
            and bool(artist_identity)
            and title_identity in line_identity
            and artist_identity in line_identity
        )
        if not is_timed_title_card:
            filtered.append(line)
    return filtered


def _parse_lrclib_id(song_ref: str) -> int | None:
    value = str(song_ref or "").strip()
    if value.isdigit():
        return int(value)
    match = re.fullmatch(r"lrclib://(?:track|song|lyrics)/(\d+)", value, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"https?://(?:www\.)?lrclib\.net/api/get/(\d+)", value, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _lrclib_record_to_result(record: Mapping[str, object], *, fallback_id: int | None = None) -> LrcResult | None:
    raw_id = record.get("id")
    song_id = raw_id if isinstance(raw_id, int) else fallback_id
    if song_id is None:
        return None
    synced_lyrics = record.get("syncedLyrics")
    if not isinstance(synced_lyrics, str) or not synced_lyrics.strip():
        return None
    lines = parse_lrc_text(synced_lyrics)
    if len(lines) < 8:
        return None
    song_title = str(record.get("trackName") or record.get("name") or "")
    artist = str(record.get("artistName") or "").strip() or None
    return LrcResult(
        provider="lrclib",
        song_title=song_title,
        artist=artist,
        source_ref=f"https://lrclib.net/api/get/{song_id}",
        lines=tuple(lines),
    )


def _finish(
    candidate_id: str,
    attempts: list[SongRepairAttempt],
    output_dir: Path,
    *,
    repaired: bool = False,
    song_boundary: Mapping[str, object] | None = None,
    lyrics_alignment: Mapping[str, object] | None = None,
    reason_codes: Sequence[str] = (),
    live_performance: Mapping[str, object] | None = None,
) -> SongRepairResult:
    report_path = output_dir / f"{candidate_id}.song-repair.json"
    result = SongRepairResult(
        repaired=repaired,
        attempts=tuple(attempts),
        song_boundary=song_boundary,
        lyrics_alignment=lyrics_alignment,
        report_path=str(report_path),
        reason_codes=tuple(dict.fromkeys(str(code) for code in reason_codes if code)),
        live_performance=dict(live_performance) if live_performance else None,
    )
    report_path.write_text(json.dumps(result.to_manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
