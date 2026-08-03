"""High-confidence "活字乱刷" phrase planning and evidence gates.

The planner treats historical speech as immutable timed tokens.  It never
invents phonetic substitutions: every character in a requested sentence must
be covered by an exact, token-boundary-aligned source span.  Speaker and text
authority are carried into every selected piece so rendering can fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from src.autoslice.surface_canon import CHANNEL_PROFILE


SOURCE_MANIFEST_SCHEMA = "huozi-source-manifest.v1"
CORPUS_SCHEMA = "huozi-corpus.v1"
PLAN_SCHEMA = "huozi-plan.v1"
VERIFICATION_SCHEMA = "huozi-verification.v1"
VERIFIED_PLAN_SCHEMA = "huozi-verified-plan.v1"

DEFAULT_MIN_SPEAKER_CONFIDENCE = 0.98
DEFAULT_MIN_TRANSCRIPT_CONFIDENCE = 0.90
RENDER_MIN_TRANSCRIPT_CONFIDENCE = 0.98
DEFAULT_MIN_TRANSCRIPT_AUTHORITIES = 2
# Long ASR tokens can be timing errors.  For a one-character clause ending,
# reward a naturally fuller syllable only up to a conservative Mandarin-speech
# ceiling instead of letting an anomalously long token dominate selection.
TERMINAL_SINGLE_FULLNESS_REWARD_CAP_MS = 420
ALLOWED_SPEAKER_AUTHORITIES = {
    "ivan_confirmed_solo_session",
    "ivan_confirmed_phrase",
    "human_reviewed_lidousha",
    "verified_lidousha_voiceprint",
}
PHRASE_SCOPED_SPEAKER_AUTHORITIES = {
    "ivan_confirmed_phrase",
    "human_reviewed_lidousha",
    "verified_lidousha_voiceprint",
}
HUMAN_SPEAKER_AUTHORITIES = {
    "ivan_confirmed_phrase",
    "human_reviewed_lidousha",
}
HUMAN_EVIDENCE_AUTHORITIES = {
    "human",
    "human_review",
    "ivan_confirmation",
    "ivan_confirmed_phrase",
}

_TEXT_CHAR_RX = re.compile(r"[0-9A-Za-z\u3400-\u9fff]")
_CLAUSE_RX = re.compile(r"([^，,。！？!?；;：:\n]+)([，,。！？!?；;：:]*)")


class HuoziError(RuntimeError):
    """Base error for fail-closed phrase generation."""


class CorpusValidationError(HuoziError):
    pass


class UncoveredTextError(HuoziError):
    def __init__(self, clause: str, offset: int) -> None:
        super().__init__(f"no exact corpus coverage for clause {clause!r} at character {offset}")
        self.clause = clause
        self.offset = offset


class VerificationError(HuoziError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def normalize_text(value: str) -> str:
    """Normalize presentation punctuation away without changing spoken words."""

    normalized = unicodedata.normalize("NFKC", value)
    return "".join(char.lower() for char in normalized if _TEXT_CHAR_RX.fullmatch(char))


def normalize_spoken_evidence(value: str) -> str:
    """Normalize only ASR orthography that is audibly identical.

    Chinese ASR providers routinely alternate between ``零``, ``〇``, and
    ``0`` for the same spoken syllable.  This narrow evidence-only mapping lets
    two providers corroborate the audio without weakening the planner's exact
    target-character contract.
    """

    return normalize_text(value).translate(str.maketrans({"0": "零", "〇": "零"}))


def split_clauses(value: str) -> list[dict[str, str]]:
    clauses: list[dict[str, str]] = []
    # Preserve the user's full-width presentation punctuation for subtitles;
    # ``normalize_text`` below still applies NFKC to the spoken content.
    for match in _CLAUSE_RX.finditer(unicodedata.normalize("NFC", value)):
        display = match.group(1).strip()
        normalized = normalize_text(display)
        if not normalized:
            continue
        clauses.append(
            {
                "display": display,
                "normalized": normalized,
                "punctuation": match.group(2),
            }
        )
    return clauses


@dataclass(frozen=True)
class TimedToken:
    text: str
    normalized: str
    start_ms: int
    end_ms: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TimedToken":
        text = str(value.get("label") or value.get("text") or "")
        normalized = normalize_text(text)
        start_ms = int(value.get("start_time", value.get("start_ms", -1)))
        end_ms = int(value.get("end_time", value.get("end_ms", -1)))
        if not normalized or start_ms < 0 or end_ms <= start_ms:
            raise CorpusValidationError(f"invalid timed token: {dict(value)!r}")
        return cls(text=text, normalized=normalized, start_ms=start_ms, end_ms=end_ms)


@dataclass(frozen=True)
class CorpusUtterance:
    utterance_id: str
    source_id: str
    source_date: str
    media_path: str
    media_sha256: str | None
    text: str
    normalized_text: str
    start_ms: int
    end_ms: int
    tokens: tuple[TimedToken, ...]
    speaker: str
    speaker_confidence: float
    speaker_authority: str
    transcript_confidence: float
    transcript_authorities: tuple[str, ...]
    transcript_evidence: tuple[Mapping[str, object], ...]
    speaker_evidence: tuple[Mapping[str, object], ...]
    content_kind: str
    source_asr_path: str
    source_asr_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "utterance_id": self.utterance_id,
            "source_id": self.source_id,
            "source_date": self.source_date,
            "media_path": self.media_path,
            "media_sha256": self.media_sha256,
            "text": self.text,
            "normalized_text": self.normalized_text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "tokens": [
                {
                    "text": token.text,
                    "normalized": token.normalized,
                    "start_ms": token.start_ms,
                    "end_ms": token.end_ms,
                }
                for token in self.tokens
            ],
            "speaker": self.speaker,
            "speaker_confidence": self.speaker_confidence,
            "speaker_authority": self.speaker_authority,
            "transcript_confidence": self.transcript_confidence,
            "transcript_authorities": list(self.transcript_authorities),
            "transcript_evidence": [dict(row) for row in self.transcript_evidence],
            "speaker_evidence": [dict(row) for row in self.speaker_evidence],
            "content_kind": self.content_kind,
            "source_asr_path": self.source_asr_path,
            "source_asr_sha256": self.source_asr_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CorpusUtterance":
        tokens = tuple(
            TimedToken(
                text=str(token["text"]),
                normalized=str(token["normalized"]),
                start_ms=int(token["start_ms"]),
                end_ms=int(token["end_ms"]),
            )
            for token in value.get("tokens", [])
        )
        return cls(
            utterance_id=str(value["utterance_id"]),
            source_id=str(value["source_id"]),
            source_date=str(value.get("source_date") or ""),
            media_path=str(value["media_path"]),
            media_sha256=str(value["media_sha256"]) if value.get("media_sha256") else None,
            text=str(value["text"]),
            normalized_text=str(value["normalized_text"]),
            start_ms=int(value["start_ms"]),
            end_ms=int(value["end_ms"]),
            tokens=tokens,
            speaker=str(value["speaker"]),
            speaker_confidence=float(value["speaker_confidence"]),
            speaker_authority=str(value["speaker_authority"]),
            transcript_confidence=float(value["transcript_confidence"]),
            transcript_authorities=tuple(str(item) for item in value.get("transcript_authorities", [])),
            transcript_evidence=tuple(
                dict(item)
                for item in value.get("transcript_evidence", [])
                if isinstance(item, Mapping)
            ),
            speaker_evidence=tuple(
                dict(item)
                for item in value.get("speaker_evidence", [])
                if isinstance(item, Mapping)
            ),
            content_kind=str(value.get("content_kind") or "talk"),
            source_asr_path=str(value["source_asr_path"]),
            source_asr_sha256=str(value["source_asr_sha256"]),
        )

    def token_boundaries(self) -> tuple[dict[int, int], dict[int, int]]:
        starts: dict[int, int] = {}
        ends: dict[int, int] = {}
        cursor = 0
        for token_index, token in enumerate(self.tokens):
            starts[cursor] = token_index
            cursor += len(token.normalized)
            ends[cursor] = token_index
        return starts, ends


def _range_kind(source: Mapping[str, object], start_ms: int, end_ms: int) -> str:
    for row in source.get("excluded_ranges_ms", []) or []:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 2:
            raise CorpusValidationError(f"invalid excluded range in {source.get('source_id')}: {row!r}")
        range_start, range_end = int(row[0]), int(row[1])
        if start_ms < range_end and end_ms > range_start:
            return str(row[2]) if len(row) >= 3 else "excluded"
    return str(source.get("content_kind") or "talk")


def _bind_evidence_rows(
    rows: object,
    *,
    base: Path,
    label: str,
) -> tuple[dict[str, object], ...]:
    if not isinstance(rows, list) or not rows:
        raise CorpusValidationError(f"{label} has no hash-bound evidence")
    bound: list[dict[str, object]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise CorpusValidationError(f"{label} has malformed evidence: {raw!r}")
        authority = str(raw.get("authority") or "").strip()
        path_value = str(raw.get("path") or "").strip()
        if not authority or not path_value:
            raise CorpusValidationError(f"{label} evidence needs authority and path")
        path = Path(path_value)
        if not path.is_absolute():
            path = (base / path).resolve()
        if not path.is_file():
            raise CorpusValidationError(f"{label} evidence missing: {path}")
        actual_sha = sha256_file(path)
        expected_sha = str(raw.get("sha256") or "").removeprefix("sha256:")
        if expected_sha and expected_sha != actual_sha:
            raise CorpusValidationError(f"{label} evidence hash mismatch: {path}")
        row = dict(raw)
        row.update({"authority": authority, "path": str(path), "sha256": actual_sha})
        raw_coverage = row.get("coverage_ranges_ms")
        if raw_coverage is None:
            raw_coverage = _speaker_evidence_coverage_from_file(path)
        coverage = _normalize_coverage_ranges(raw_coverage, label=f"{label} evidence coverage")
        if coverage:
            row["coverage_ranges_ms"] = [list(item) for item in coverage]
        if "timeline_offset_ms" in row:
            row["timeline_offset_ms"] = int(row["timeline_offset_ms"])
        if "confidence" in row:
            row["confidence"] = float(row["confidence"])
        bound.append(row)
    return tuple(bound)


def _normalize_coverage_ranges(
    raw_ranges: object,
    *,
    label: str,
) -> tuple[tuple[int, int], ...]:
    if raw_ranges is None:
        return ()
    if not isinstance(raw_ranges, list):
        raise CorpusValidationError(f"{label} must be a list")
    normalized: list[tuple[int, int]] = []
    for raw_range in raw_ranges:
        if (
            not isinstance(raw_range, Sequence)
            or isinstance(raw_range, (str, bytes))
            or len(raw_range) != 2
        ):
            raise CorpusValidationError(f"{label} has malformed range: {raw_range!r}")
        start_ms, end_ms = int(raw_range[0]), int(raw_range[1])
        if start_ms < 0 or end_ms <= start_ms:
            raise CorpusValidationError(f"{label} has invalid range: {raw_range!r}")
        normalized.append((start_ms, end_ms))
    return tuple(normalized)


def _speaker_evidence_coverage_from_file(path: Path) -> list[list[int]]:
    """Read exact cue coverage from known human/acoustic evidence documents.

    A broad source/session range is deliberately not accepted.  In particular,
    ``source_range_ms`` may describe the context that a reviewer heard while a
    nested ``previous_source_fragment_ms`` is the only phrase they confirmed.
    Promoting that context to the whole utterance caused a real mixed-source
    phrase to be mislabeled as one continuous take.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, Mapping):
        return []

    candidates: list[object] = []
    applies_to = payload.get("applies_to")
    if isinstance(applies_to, Mapping):
        candidates.append(applies_to.get("previous_source_fragment_ms"))
        candidates.append(applies_to.get("confirmed_range_ms"))
    lidousha_accept = payload.get("lidousha_accept")
    if isinstance(lidousha_accept, Mapping):
        candidates.append(lidousha_accept.get("range_ms"))
    candidate = payload.get("candidate")
    if isinstance(candidate, Mapping):
        candidates.append(candidate.get("range_ms"))
    candidates.extend(
        [
            payload.get("confirmed_range_ms"),
            payload.get("verified_range_ms"),
            payload.get("range_ms"),
        ]
    )
    for candidate_range in candidates:
        if (
            isinstance(candidate_range, Sequence)
            and not isinstance(candidate_range, (str, bytes))
            and len(candidate_range) == 2
        ):
            try:
                start_ms, end_ms = int(candidate_range[0]), int(candidate_range[1])
            except (TypeError, ValueError):
                continue
            if start_ms >= 0 and end_ms > start_ms:
                return [[start_ms, end_ms]]
    return []


def _speaker_evidence_covers_range(
    evidence: Sequence[Mapping[str, object]],
    *,
    start_ms: int,
    end_ms: int,
    human_only: bool,
) -> bool:
    for row in evidence:
        authority = str(row.get("authority") or "")
        if human_only and authority not in HUMAN_EVIDENCE_AUTHORITIES:
            continue
        raw_ranges = row.get("coverage_ranges_ms")
        if not isinstance(raw_ranges, list):
            continue
        for raw_range in raw_ranges:
            if (
                isinstance(raw_range, Sequence)
                and not isinstance(raw_range, (str, bytes))
                and len(raw_range) == 2
                and int(raw_range[0]) <= start_ms
                and end_ms <= int(raw_range[1])
            ):
                return True
    return False


def _validate_speaker_evidence(
    *,
    source_id: str,
    speaker_authority: str,
    evidence: Sequence[Mapping[str, object]],
) -> None:
    authorities = {str(row.get("authority") or "") for row in evidence}
    if not authorities or "" in authorities:
        raise CorpusValidationError(f"source {source_id} has no speaker evidence")
    if speaker_authority == "verified_lidousha_voiceprint" and len(authorities) < 2:
        raise CorpusValidationError(
            f"source {source_id} voiceprint authority needs two independent evidence authorities"
        )


def _speaker_authority_for_range(
    source: Mapping[str, object], start_ms: int, end_ms: int
) -> tuple[str, float, tuple[Mapping[str, object], ...]] | None:
    """Resolve speaker authority without promoting a whole collab session.

    ``trusted_ranges_ms`` is the safe surface for a user-reviewed phrase or a
    voiceprint-verified cue inside a mixed-speaker recording.  A cue must be
    fully contained by the trusted range; mere overlap is not enough.
    """

    ranges = source.get("trusted_ranges_ms") or []
    if ranges:
        for row in ranges:
            if not isinstance(row, Mapping):
                raise CorpusValidationError(
                    f"invalid trusted range in {source.get('source_id')}: {row!r}"
                )
            range_start = int(row.get("start_ms", -1))
            range_end = int(row.get("end_ms", -1))
            if range_start <= start_ms and end_ms <= range_end:
                authority = str(row.get("speaker_authority") or "")
                confidence = float(row.get("speaker_confidence", 0.0))
                evidence = row.get("speaker_evidence") or source.get("speaker_evidence") or []
                assert isinstance(evidence, tuple)
                if authority in PHRASE_SCOPED_SPEAKER_AUTHORITIES and not _speaker_evidence_covers_range(
                    evidence,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    human_only=authority in HUMAN_SPEAKER_AUTHORITIES,
                ):
                    continue
                return authority, confidence, evidence
        return None
    return (
        str(source.get("speaker_authority") or ""),
        float(source.get("speaker_confidence", 0.0)),
        source.get("speaker_evidence") if isinstance(source.get("speaker_evidence"), tuple) else (),
    )


def build_corpus(
    source_manifest: Mapping[str, object],
    *,
    manifest_dir: Path | None = None,
    include_kinds: Iterable[str] = ("talk",),
) -> dict[str, object]:
    """Build a compact word-timed corpus from explicitly trusted sources."""

    if source_manifest.get("schema_version") != SOURCE_MANIFEST_SCHEMA:
        raise CorpusValidationError("unsupported source manifest schema")
    allowed_kinds = set(include_kinds)
    utterances: list[CorpusUtterance] = []
    sources = source_manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise CorpusValidationError("source manifest has no sources")
    base = manifest_dir or Path.cwd()

    for raw_source in sources:
        if not isinstance(raw_source, Mapping):
            raise CorpusValidationError("source rows must be objects")
        source = dict(raw_source)
        source_id = str(source.get("source_id") or "")
        media_path = str(source.get("media_path") or "")
        asr_value = source.get("asr_json_path")
        if not source_id or not media_path or not isinstance(asr_value, str) or not asr_value:
            raise CorpusValidationError("source_id, media_path, and asr_json_path are required")
        asr_path = Path(asr_value)
        if not asr_path.is_absolute():
            asr_path = (base / asr_path).resolve()
        if not asr_path.is_file():
            raise CorpusValidationError(f"ASR JSON missing: {asr_path}")
        speaker = str(source.get("speaker") or "")
        has_trusted_ranges = bool(source.get("trusted_ranges_ms"))
        transcript_confidence = float(source.get("transcript_confidence", 0.0))
        transcript_authorities = tuple(str(item) for item in source.get("transcript_authorities", []))
        if speaker != CHANNEL_PROFILE.profile_id and not (speaker == "mixed" and has_trusted_ranges):
            raise CorpusValidationError(f"source {source_id} is not explicitly Li Dousha")
        if not has_trusted_ranges:
            source_authority = str(source.get("speaker_authority") or "")
            source_speaker_confidence = float(source.get("speaker_confidence", 0.0))
            if source_authority not in ALLOWED_SPEAKER_AUTHORITIES:
                raise CorpusValidationError(
                    f"source {source_id} has untrusted speaker authority {source_authority!r}"
                )
            if source_speaker_confidence < DEFAULT_MIN_SPEAKER_CONFIDENCE:
                raise CorpusValidationError(f"source {source_id} speaker confidence is below gate")
        if transcript_confidence < DEFAULT_MIN_TRANSCRIPT_CONFIDENCE:
            raise CorpusValidationError(f"source {source_id} transcript confidence is below discovery gate")
        if not transcript_authorities:
            raise CorpusValidationError(f"source {source_id} has no transcript authority")

        raw_transcript_evidence = source.get("transcript_evidence")
        if raw_transcript_evidence is None:
            raw_transcript_evidence = [
                {
                    "authority": transcript_authorities[0],
                    "path": str(asr_path),
                    "timeline_offset_ms": int(source.get("timeline_offset_ms", 0)),
                    "confidence": transcript_confidence,
                }
            ]
        transcript_evidence = _bind_evidence_rows(
            raw_transcript_evidence,
            base=base,
            label=f"source {source_id} transcript",
        )
        if not set(transcript_authorities).issubset(
            {str(row["authority"]) for row in transcript_evidence}
        ):
            raise CorpusValidationError(
                f"source {source_id} transcript authorities are not backed by evidence"
            )

        source["speaker_evidence"] = (
            _bind_evidence_rows(
                source.get("speaker_evidence"),
                base=base,
                label=f"source {source_id} speaker",
            )
            if source.get("speaker_evidence") is not None
            else ()
        )
        if has_trusted_ranges:
            trusted_ranges: list[dict[str, object]] = []
            for index, raw_range in enumerate(source.get("trusted_ranges_ms") or []):
                if not isinstance(raw_range, Mapping):
                    raise CorpusValidationError(
                        f"invalid trusted range in {source_id}: {raw_range!r}"
                    )
                trusted_range = dict(raw_range)
                if raw_range.get("speaker_evidence") is not None:
                    trusted_range["speaker_evidence"] = _bind_evidence_rows(
                        raw_range.get("speaker_evidence"),
                        base=base,
                        label=f"source {source_id} trusted range {index}",
                    )
                trusted_ranges.append(trusted_range)
            source["trusted_ranges_ms"] = trusted_ranges

        asr_payload = json.loads(asr_path.read_text(encoding="utf-8"))
        rows = asr_payload.get("utterances")
        if not isinstance(rows, list):
            raise CorpusValidationError(f"source {source_id} ASR JSON has no utterances")
        asr_sha256 = sha256_file(asr_path)
        offset_ms = int(source.get("timeline_offset_ms", 0))
        for row_index, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                continue
            text = str(row.get("transcript") or "").strip()
            normalized = normalize_text(text)
            raw_words = row.get("words")
            if not normalized or not isinstance(raw_words, list) or not raw_words:
                continue
            try:
                tokens = tuple(
                    TimedToken.from_mapping(word)
                    for word in raw_words
                    if isinstance(word, Mapping)
                    and normalize_text(str(word.get("label") or word.get("text") or ""))
                )
            except CorpusValidationError:
                continue
            if not tokens or "".join(token.normalized for token in tokens) != normalized:
                # Exact token/text agreement is mandatory because cuts use the
                # word boundaries.  Sentence-only or drifting rows stay out.
                continue
            start_ms = int(row.get("start_time", tokens[0].start_ms)) + offset_ms
            end_ms = int(row.get("end_time", tokens[-1].end_ms)) + offset_ms
            shifted_tokens = tuple(
                TimedToken(token.text, token.normalized, token.start_ms + offset_ms, token.end_ms + offset_ms)
                for token in tokens
            )
            kind = _range_kind(source, start_ms, end_ms)
            if kind not in allowed_kinds:
                continue
            speaker_evidence = _speaker_authority_for_range(source, start_ms, end_ms)
            if speaker_evidence is None:
                continue
            authority, speaker_confidence, bound_speaker_evidence = speaker_evidence
            if authority not in ALLOWED_SPEAKER_AUTHORITIES:
                raise CorpusValidationError(
                    f"source {source_id} has untrusted range authority {authority!r}"
                )
            if speaker_confidence < DEFAULT_MIN_SPEAKER_CONFIDENCE:
                raise CorpusValidationError(
                    f"source {source_id} trusted range speaker confidence is below gate"
                )
            _validate_speaker_evidence(
                source_id=source_id,
                speaker_authority=authority,
                evidence=bound_speaker_evidence,
            )
            utterances.append(
                CorpusUtterance(
                    utterance_id=f"{source_id}:u{row_index:05d}",
                    source_id=source_id,
                    source_date=str(source.get("source_date") or ""),
                    media_path=media_path,
                    media_sha256=(
                        str(source["media_sha256"]).removeprefix("sha256:")
                        if source.get("media_sha256")
                        else None
                    ),
                    text=text,
                    normalized_text=normalized,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    tokens=shifted_tokens,
                    # A mixed source only enters the corpus through a
                    # cue-local Li Dousha authority above.
                    speaker=CHANNEL_PROFILE.profile_id,
                    speaker_confidence=speaker_confidence,
                    speaker_authority=authority,
                    transcript_confidence=transcript_confidence,
                    transcript_authorities=transcript_authorities,
                    transcript_evidence=transcript_evidence,
                    speaker_evidence=tuple(bound_speaker_evidence),
                    content_kind=kind,
                    source_asr_path=str(asr_path),
                    source_asr_sha256=asr_sha256,
                )
            )

    if not utterances:
        raise CorpusValidationError("no exact word-timed utterances survived corpus gates")
    payload: dict[str, object] = {
        "schema_version": CORPUS_SCHEMA,
        "utterance_count": len(utterances),
        "source_count": len({utterance.source_id for utterance in utterances}),
        "utterances": [utterance.to_dict() for utterance in utterances],
    }
    payload["corpus_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


def load_corpus(value: Mapping[str, object]) -> list[CorpusUtterance]:
    if value.get("schema_version") != CORPUS_SCHEMA:
        raise CorpusValidationError("unsupported corpus schema")
    rows = value.get("utterances")
    if not isinstance(rows, list):
        raise CorpusValidationError("corpus utterances must be a list")
    return [CorpusUtterance.from_dict(row) for row in rows if isinstance(row, Mapping)]


@dataclass(frozen=True)
class MatchPiece:
    text: str
    utterance: CorpusUtterance
    source_char_start: int
    source_char_end: int
    token_start: int
    token_end: int
    core_start_ms: int
    core_end_ms: int
    cut_start_ms: int
    cut_end_ms: int
    boundary_score: int

    @property
    def char_count(self) -> int:
        return len(self.text)

    def to_dict(self, *, order: int, clause_index: int) -> dict[str, object]:
        return {
            "piece_id": f"p{order:03d}",
            "order": order,
            "clause_index": clause_index,
            "text": self.text,
            "char_count": self.char_count,
            "source_id": self.utterance.source_id,
            "source_date": self.utterance.source_date,
            "utterance_id": self.utterance.utterance_id,
            "source_utterance_text": self.utterance.text,
            "source_char_start": self.source_char_start,
            "source_char_end": self.source_char_end,
            "token_start": self.token_start,
            "token_end": self.token_end,
            "media_path": self.utterance.media_path,
            "media_sha256": self.utterance.media_sha256,
            "core_start_ms": self.core_start_ms,
            "core_end_ms": self.core_end_ms,
            "cut_start_ms": self.cut_start_ms,
            "cut_end_ms": self.cut_end_ms,
            "speaker": self.utterance.speaker,
            "speaker_confidence": self.utterance.speaker_confidence,
            "speaker_authority": self.utterance.speaker_authority,
            "speaker_evidence": [dict(row) for row in self.utterance.speaker_evidence],
            "transcript_confidence": self.utterance.transcript_confidence,
            "transcript_authorities": list(self.utterance.transcript_authorities),
            "transcript_evidence": [dict(row) for row in self.utterance.transcript_evidence],
            "content_kind": self.utterance.content_kind,
            "source_asr_path": self.utterance.source_asr_path,
            "source_asr_sha256": self.utterance.source_asr_sha256,
            "boundary_score": self.boundary_score,
            "verification_status": "DISCOVERY_ONLY",
        }


def _occurrences(utterance: CorpusUtterance, fragment: str) -> Iterable[MatchPiece]:
    starts, ends = utterance.token_boundaries()
    cursor = 0
    while True:
        found = utterance.normalized_text.find(fragment, cursor)
        if found < 0:
            return
        stop = found + len(fragment)
        cursor = found + 1
        if found not in starts or stop not in ends:
            continue
        token_start = starts[found]
        token_end = ends[stop]
        first = utterance.tokens[token_start]
        last = utterance.tokens[token_end]
        previous_end = utterance.tokens[token_start - 1].end_ms if token_start > 0 else utterance.start_ms
        next_start = (
            utterance.tokens[token_end + 1].start_ms
            if token_end + 1 < len(utterance.tokens)
            else utterance.end_ms
        )
        lead_room = max(0, first.start_ms - previous_end)
        tail_room = max(0, next_start - last.end_ms)
        lead_pad = min(35, lead_room // 2)
        tail_pad = min(55, tail_room // 2)
        boundary_score = int(found == 0) + int(stop == len(utterance.normalized_text))
        yield MatchPiece(
            text=fragment,
            utterance=utterance,
            source_char_start=found,
            source_char_end=stop,
            token_start=token_start,
            token_end=token_end,
            core_start_ms=first.start_ms,
            core_end_ms=last.end_ms,
            cut_start_ms=max(utterance.start_ms, first.start_ms - lead_pad),
            cut_end_ms=min(utterance.end_ms, last.end_ms + tail_pad),
            boundary_score=boundary_score,
        )


def _piece_preference(
    piece: MatchPiece, *, prefer_full_terminal_single: bool = False
) -> tuple[object, ...]:
    core_duration_ms = piece.core_end_ms - piece.core_start_ms
    if prefer_full_terminal_single and piece.char_count == 1:
        source_utterance_terminal = int(
            piece.source_char_end == len(piece.utterance.normalized_text)
        )
        return (
            0 if piece.utterance.content_kind == "talk" else 1,
            -source_utterance_terminal,
            -min(core_duration_ms, TERMINAL_SINGLE_FULLNESS_REWARD_CAP_MS),
            -piece.utterance.speaker_confidence,
            -piece.utterance.transcript_confidence,
            -piece.boundary_score,
            piece.cut_end_ms - piece.cut_start_ms,
            piece.utterance.source_id,
            piece.core_start_ms,
        )
    return (
        0 if piece.utterance.content_kind == "talk" else 1,
        -piece.utterance.speaker_confidence,
        -piece.utterance.transcript_confidence,
        -piece.boundary_score,
        piece.cut_end_ms - piece.cut_start_ms,
        piece.utterance.source_id,
        piece.core_start_ms,
    )


def _corroborating_transcript_authorities(
    piece: MatchPiece,
    payload_cache: dict[tuple[str, str], Mapping[str, object]],
    *,
    timing_tolerance_ms: int = 1_200,
) -> set[str]:
    authorities: set[str] = set()
    for evidence in piece.utterance.transcript_evidence:
        authority = str(evidence.get("authority") or "")
        path_value = str(evidence.get("path") or "")
        evidence_sha = str(evidence.get("sha256") or "")
        if not authority or authority in authorities or not path_value or not evidence_sha:
            continue
        cache_key = (path_value, evidence_sha)
        payload = payload_cache.get(cache_key)
        if payload is None:
            try:
                loaded = json.loads(Path(path_value).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(loaded, Mapping):
                continue
            payload = loaded
            payload_cache[cache_key] = payload
        observation = _find_timed_observation(
            payload,
            expected_text=piece.text,
            core_start_ms=piece.core_start_ms,
            core_end_ms=piece.core_end_ms,
            timeline_offset_ms=int(evidence.get("timeline_offset_ms", 0)),
            timing_tolerance_ms=timing_tolerance_ms,
        )
        if observation is not None:
            authorities.add(authority)
    return authorities


def _path_quality(pieces: Sequence[MatchPiece]) -> tuple[object, ...]:
    lengths = [piece.char_count for piece in pieces]
    reuse_count = len(pieces) - len({(piece.utterance.source_id, piece.core_start_ms, piece.core_end_ms) for piece in pieces})
    return (
        len(pieces),
        sum(length == 1 for length in lengths),
        -sum(length * length for length in lengths),
        -max(lengths, default=0),
        reuse_count,
        -sum(piece.boundary_score for piece in pieces),
        -sum(piece.utterance.transcript_confidence for piece in pieces),
        tuple((piece.utterance.source_id, piece.core_start_ms) for piece in pieces),
    )


def plan_clause(
    clause: str,
    corpus: Sequence[CorpusUtterance],
    *,
    max_fragment_chars: int = 18,
    min_transcript_authorities: int = DEFAULT_MIN_TRANSCRIPT_AUTHORITIES,
) -> list[MatchPiece]:
    normalized = normalize_text(clause)
    if not normalized:
        return []
    candidates_by_start: dict[int, list[MatchPiece]] = {}
    evidence_payload_cache: dict[tuple[str, str], Mapping[str, object]] = {}
    for target_start in range(len(normalized)):
        target_candidates: list[MatchPiece] = []
        max_end = min(len(normalized), target_start + max_fragment_chars)
        for target_end in range(max_end, target_start, -1):
            fragment = normalized[target_start:target_end]
            occurrences = [
                piece
                for utterance in corpus
                for piece in _occurrences(utterance, fragment)
                if len(
                    _corroborating_transcript_authorities(
                        piece,
                        evidence_payload_cache,
                    )
                )
                >= min_transcript_authorities
            ]
            if occurrences:
                occurrences.sort(
                    key=lambda piece: _piece_preference(
                        piece,
                        prefer_full_terminal_single=(
                            target_end == len(normalized) and len(fragment) == 1
                        ),
                    )
                )
                # Multiple acoustic alternatives are useful for later QA, but
                # the exact-text segmentation score only needs the strongest.
                target_candidates.append(occurrences[0])
        candidates_by_start[target_start] = target_candidates

    best: dict[int, list[MatchPiece]] = {len(normalized): []}
    for position in range(len(normalized) - 1, -1, -1):
        options: list[list[MatchPiece]] = []
        for piece in candidates_by_start.get(position, []):
            next_position = position + piece.char_count
            suffix = best.get(next_position)
            if suffix is not None:
                options.append([piece, *suffix])
        if options:
            best[position] = min(options, key=_path_quality)
    if 0 not in best:
        missing = min(position for position in range(len(normalized)) if position not in best)
        raise UncoveredTextError(clause, missing)
    return best[0]


def _quality_metrics(pieces: Sequence[MatchPiece], total_chars: int) -> dict[str, object]:
    lengths = [piece.char_count for piece in pieces]
    multi_chars = sum(length for length in lengths if length > 1)
    return {
        "character_count": total_chars,
        "piece_count": len(pieces),
        "single_character_piece_count": sum(length == 1 for length in lengths),
        "longest_piece_characters": max(lengths, default=0),
        "average_piece_characters": round(total_chars / len(pieces), 3) if pieces else 0.0,
        "multi_character_coverage_ratio": round(multi_chars / total_chars, 4) if total_chars else 0.0,
        "fragmented": bool(lengths and (any(length == 1 for length in lengths) or total_chars / len(pieces) < 2.5)),
    }


def plan_text(target: str, corpus_payload: Mapping[str, object]) -> dict[str, object]:
    corpus = load_corpus(corpus_payload)
    clauses = split_clauses(target)
    if not clauses:
        raise UncoveredTextError(target, 0)
    all_pieces: list[MatchPiece] = []
    piece_rows: list[dict[str, object]] = []
    order = 1
    for clause_index, clause in enumerate(clauses):
        clause_pieces = plan_clause(clause["normalized"], corpus)
        all_pieces.extend(clause_pieces)
        for piece in clause_pieces:
            piece_rows.append(piece.to_dict(order=order, clause_index=clause_index))
            order += 1
    normalized_target = "".join(clause["normalized"] for clause in clauses)
    quality = _quality_metrics(all_pieces, len(normalized_target))
    plan: dict[str, object] = {
        "schema_version": PLAN_SCHEMA,
        "status": "DISCOVERY_READY",
        "target": target,
        "normalized_target": normalized_target,
        "clauses": clauses,
        "quality": quality,
        "pieces": piece_rows,
        "corpus_sha256": corpus_payload.get("corpus_sha256"),
        "confidence_gate": {
            "speaker_minimum": DEFAULT_MIN_SPEAKER_CONFIDENCE,
            "transcript_discovery_minimum": DEFAULT_MIN_TRANSCRIPT_CONFIDENCE,
            "transcript_authority_minimum": DEFAULT_MIN_TRANSCRIPT_AUTHORITIES,
            "render_transcript_minimum": RENDER_MIN_TRANSCRIPT_CONFIDENCE,
            "exact_character_coverage": True,
            "token_boundary_alignment": True,
        },
    }
    plan["plan_sha256"] = hashlib.sha256(canonical_json_bytes(plan)).hexdigest()
    return plan


def levenshtein_distance(left: str, right: str) -> int:
    left_norm, right_norm = normalize_text(left), normalize_text(right)
    previous = list(range(len(right_norm) + 1))
    for left_index, left_char in enumerate(left_norm, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right_norm, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def rank_suggestions(
    target: str,
    corpus_payload: Mapping[str, object],
    suggestions: Sequence[Mapping[str, object]],
    *,
    max_edits: int = 4,
) -> list[dict[str, object]]:
    original = plan_text(target, corpus_payload)
    original_quality = original["quality"]
    ranked: list[dict[str, object]] = []
    for suggestion in suggestions:
        text = str(suggestion.get("text") or "").strip()
        if not text or normalize_text(text) == normalize_text(target):
            continue
        distance = levenshtein_distance(target, text)
        if distance > max_edits:
            continue
        try:
            plan = plan_text(text, corpus_payload)
        except UncoveredTextError:
            continue
        quality = plan["quality"]
        improvement = (
            int(quality["piece_count"]) < int(original_quality["piece_count"])
            or int(quality["single_character_piece_count"])
            < int(original_quality["single_character_piece_count"])
            or (
                int(quality["piece_count"]) == int(original_quality["piece_count"])
                and int(quality["longest_piece_characters"])
                > int(original_quality["longest_piece_characters"])
            )
        )
        if not improvement:
            continue
        ranked.append(
            {
                "text": text,
                "edit_distance": distance,
                "rationale": str(suggestion.get("rationale") or ""),
                "meaning_preservation": str(
                    suggestion.get("meaning_preservation") or "user_review_required"
                ),
                "plan": plan,
            }
        )
    ranked.sort(
        key=lambda row: (
            int(row["plan"]["quality"]["piece_count"]),
            int(row["plan"]["quality"]["single_character_piece_count"]),
            -int(row["plan"]["quality"]["longest_piece_characters"]),
            int(row["edit_distance"]),
            str(row["text"]),
        )
    )
    return ranked


def _find_timed_observation(
    payload: Mapping[str, object],
    *,
    expected_text: str,
    core_start_ms: int,
    core_end_ms: int,
    timeline_offset_ms: int,
    timing_tolerance_ms: int,
) -> dict[str, object] | None:
    expected = normalize_spoken_evidence(expected_text)
    rows = payload.get("utterances")
    if not expected or not isinstance(rows, list):
        return None
    candidates: list[tuple[int, int, dict[str, object]]] = []
    for utterance_index, raw_utterance in enumerate(rows):
        if not isinstance(raw_utterance, Mapping):
            continue
        raw_words = raw_utterance.get("words")
        if not isinstance(raw_words, list) or not raw_words:
            continue
        tokens: list[tuple[str, int, int, int, int]] = []
        cursor = 0
        for raw_word in raw_words:
            if not isinstance(raw_word, Mapping):
                continue
            token = normalize_spoken_evidence(
                str(raw_word.get("label") or raw_word.get("text") or "")
            )
            start_ms = int(raw_word.get("start_time", raw_word.get("start_ms", -1)))
            end_ms = int(raw_word.get("end_time", raw_word.get("end_ms", -1)))
            if not token or start_ms < 0 or end_ms < start_ms:
                continue
            tokens.append((token, cursor, cursor + len(token), start_ms, end_ms))
            cursor += len(token)
        joined = "".join(token[0] for token in tokens)
        search_from = 0
        while tokens:
            found = joined.find(expected, search_from)
            if found < 0:
                break
            stop = found + len(expected)
            search_from = found + 1
            first = next((token for token in tokens if token[1] <= found < token[2]), None)
            last = next((token for token in tokens if token[1] < stop <= token[2]), None)
            if first is None or last is None:
                continue
            observed_start = first[3] + timeline_offset_ms
            observed_end = last[4] + timeline_offset_ms
            endpoint_delta = max(
                abs(observed_start - core_start_ms), abs(observed_end - core_end_ms)
            )
            if endpoint_delta > timing_tolerance_ms:
                continue
            total_delta = abs(observed_start - core_start_ms) + abs(observed_end - core_end_ms)
            candidates.append(
                (
                    total_delta,
                    utterance_index,
                    {
                        "observed_text": str(raw_utterance.get("transcript") or joined),
                        "observed_interval_ms": [observed_start, observed_end],
                        "timing_delta_ms": {
                            "start": observed_start - core_start_ms,
                            "end": observed_end - core_end_ms,
                            "maximum_absolute": endpoint_delta,
                        },
                        "utterance_index": utterance_index,
                    },
                )
            )
    if not candidates:
        return None
    return min(candidates, key=lambda row: (row[0], row[1]))[2]


def build_verification(
    plan: Mapping[str, object],
    *,
    timing_tolerance_ms: int = 1_200,
) -> dict[str, object]:
    """Derive hash-bound dual-ASR evidence directly from a discovery plan."""

    if plan.get("schema_version") != PLAN_SCHEMA:
        raise VerificationError("unsupported plan schema")
    if timing_tolerance_ms < 0:
        raise VerificationError("timing tolerance must be non-negative")
    pieces = plan.get("pieces")
    if not isinstance(pieces, list) or not pieces:
        raise VerificationError("plan has no pieces")

    evidence_cache: dict[tuple[str, str], Mapping[str, object]] = {}
    media_hashes: dict[str, str] = {}
    verification_rows: dict[str, object] = {}
    for raw_piece in pieces:
        if not isinstance(raw_piece, Mapping):
            raise VerificationError("invalid plan piece")
        piece_id = str(raw_piece.get("piece_id") or "")
        expected = str(raw_piece.get("text") or "")
        specs = raw_piece.get("transcript_evidence")
        if not isinstance(specs, list):
            raise VerificationError(f"piece {piece_id} has no transcript evidence")
        observations: list[dict[str, object]] = []
        seen_authorities: set[str] = set()
        for raw_spec in specs:
            if not isinstance(raw_spec, Mapping):
                continue
            authority = str(raw_spec.get("authority") or "")
            path = Path(str(raw_spec.get("path") or ""))
            expected_sha = str(raw_spec.get("sha256") or "").removeprefix("sha256:")
            if not authority or authority in seen_authorities or not path.is_file():
                continue
            actual_sha = sha256_file(path)
            if expected_sha != actual_sha:
                raise VerificationError(f"piece {piece_id} transcript evidence hash drift")
            cache_key = (str(path), actual_sha)
            payload = evidence_cache.get(cache_key)
            if payload is None:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, Mapping):
                    raise VerificationError(f"piece {piece_id} transcript evidence is not an object")
                payload = loaded
                evidence_cache[cache_key] = payload
            observation = _find_timed_observation(
                payload,
                expected_text=expected,
                core_start_ms=int(raw_piece["core_start_ms"]),
                core_end_ms=int(raw_piece["core_end_ms"]),
                timeline_offset_ms=int(raw_spec.get("timeline_offset_ms", 0)),
                timing_tolerance_ms=timing_tolerance_ms,
            )
            if observation is None:
                continue
            observations.append(
                {
                    "authority": authority,
                    "path": str(path),
                    "sha256": actual_sha,
                    **observation,
                }
            )
            seen_authorities.add(authority)
        if len(seen_authorities) < 2:
            raise VerificationError(
                f"piece {piece_id} was not independently observed by two transcript authorities"
            )

        media_path = str(raw_piece.get("media_path") or "")
        if not media_path:
            raise VerificationError(f"piece {piece_id} has no source media")
        if media_path not in media_hashes:
            path = Path(media_path)
            if not path.is_file():
                raise VerificationError(f"piece {piece_id} source media is missing")
            media_hashes[media_path] = sha256_file(path)
        media_sha = media_hashes[media_path]
        planned_media_sha = str(raw_piece.get("media_sha256") or "").removeprefix("sha256:")
        if planned_media_sha and planned_media_sha != media_sha:
            raise VerificationError(f"piece {piece_id} source media hash drift")
        confidences = [
            float(spec.get("confidence", 0.99))
            for spec in specs
            if isinstance(spec, Mapping) and str(spec.get("authority") or "") in seen_authorities
        ]
        verification_rows[piece_id] = {
            "status": "VERIFIED",
            "observed_text": observations[0]["observed_text"],
            "transcript_authorities": sorted(seen_authorities),
            "transcript_confidence": min(confidences, default=0.99),
            "media_sha256": media_sha,
            "evidence": observations,
        }
    return {
        "schema_version": VERIFICATION_SCHEMA,
        "plan_sha256": plan.get("plan_sha256"),
        "timing_tolerance_ms": timing_tolerance_ms,
        "pieces": verification_rows,
    }


def apply_verification(
    plan: Mapping[str, object], verification: Mapping[str, object]
) -> dict[str, object]:
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise VerificationError("unsupported plan schema")
    if verification.get("schema_version") != VERIFICATION_SCHEMA:
        raise VerificationError("unsupported verification schema")
    expected_plan_sha = str(plan.get("plan_sha256") or "")
    if verification.get("plan_sha256") != expected_plan_sha:
        raise VerificationError("verification is bound to a different plan")
    verification_rows = verification.get("pieces")
    if not isinstance(verification_rows, Mapping):
        raise VerificationError("verification pieces must be keyed by piece_id")

    verified_pieces: list[dict[str, object]] = []
    for raw_piece in plan.get("pieces", []):
        if not isinstance(raw_piece, Mapping):
            raise VerificationError("invalid plan piece")
        piece = dict(raw_piece)
        piece_id = str(piece.get("piece_id") or "")
        row = verification_rows.get(piece_id)
        if not isinstance(row, Mapping) or row.get("status") != "VERIFIED":
            raise VerificationError(f"piece {piece_id} lacks VERIFIED evidence")
        observed = normalize_spoken_evidence(str(row.get("observed_text") or ""))
        expected = normalize_spoken_evidence(str(piece.get("text") or ""))
        if expected not in observed:
            raise VerificationError(f"piece {piece_id} text was not independently observed")
        authorities = [str(item) for item in row.get("transcript_authorities", [])]
        if len(set(authorities)) < 2:
            raise VerificationError(f"piece {piece_id} needs two transcript authorities")
        evidence_rows = row.get("evidence")
        if not isinstance(evidence_rows, list) or len(evidence_rows) < 2:
            raise VerificationError(f"piece {piece_id} needs two hash-bound evidence files")
        evidence_authorities: set[str] = set()
        for evidence in evidence_rows:
            if not isinstance(evidence, Mapping):
                raise VerificationError(f"piece {piece_id} has malformed evidence")
            evidence_authority = str(evidence.get("authority") or "")
            evidence_sha = str(evidence.get("sha256") or "").removeprefix("sha256:")
            evidence_path = str(evidence.get("path") or "")
            evidence_observed = normalize_spoken_evidence(
                str(evidence.get("observed_text") or "")
            )
            if (
                not evidence_authority
                or evidence_authority not in authorities
                or not evidence_path
                or re.fullmatch(r"[0-9a-f]{64}", evidence_sha) is None
                or expected not in evidence_observed
            ):
                raise VerificationError(f"piece {piece_id} has unbound evidence")
            evidence_authorities.add(evidence_authority)
        if len(evidence_authorities) < 2:
            raise VerificationError(f"piece {piece_id} evidence is not independent")
        transcript_confidence = float(row.get("transcript_confidence", 0.0))
        if transcript_confidence < RENDER_MIN_TRANSCRIPT_CONFIDENCE:
            raise VerificationError(f"piece {piece_id} transcript confidence is below render gate")
        media_sha = str(row.get("media_sha256") or "").removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", media_sha):
            raise VerificationError(f"piece {piece_id} has no source media hash")
        if piece.get("media_sha256") and piece.get("media_sha256") != media_sha:
            raise VerificationError(f"piece {piece_id} source media hash drift")
        piece.update(
            {
                "media_sha256": media_sha,
                "verification_status": "VERIFIED",
                "verified_observed_text": str(row.get("observed_text") or ""),
                "transcript_authorities": authorities,
                "transcript_confidence": transcript_confidence,
                "verification_evidence": dict(row),
            }
        )
        verified_pieces.append(piece)

    verified = dict(plan)
    verified.update(
        {
            "schema_version": VERIFIED_PLAN_SCHEMA,
            "status": "READY_TO_RENDER",
            "pieces": verified_pieces,
            "discovery_plan_sha256": expected_plan_sha,
            "verification_sha256": hashlib.sha256(canonical_json_bytes(verification)).hexdigest(),
        }
    )
    verified.pop("plan_sha256", None)
    verified["verified_plan_sha256"] = hashlib.sha256(canonical_json_bytes(verified)).hexdigest()
    return verified


def validate_renderable_plan(plan: Mapping[str, object]) -> None:
    if plan.get("schema_version") != VERIFIED_PLAN_SCHEMA or plan.get("status") != "READY_TO_RENDER":
        raise VerificationError("only a verified plan may be rendered")
    expected_plan_sha = str(plan.get("verified_plan_sha256") or "")
    unsigned_plan = dict(plan)
    unsigned_plan.pop("verified_plan_sha256", None)
    if (
        re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha) is None
        or hashlib.sha256(canonical_json_bytes(unsigned_plan)).hexdigest() != expected_plan_sha
    ):
        raise VerificationError("verified plan hash drift")
    pieces = plan.get("pieces")
    if not isinstance(pieces, list) or not pieces:
        raise VerificationError("verified plan has no pieces")
    reconstructed = ""
    checked_speaker_evidence: dict[str, str] = {}
    checked_transcript_evidence: dict[str, str] = {}
    for piece in pieces:
        if not isinstance(piece, Mapping):
            raise VerificationError("invalid verified piece")
        if piece.get("verification_status") != "VERIFIED":
            raise VerificationError("unverified piece reached render gate")
        if piece.get("speaker") != CHANNEL_PROFILE.profile_id:
            raise VerificationError("non-Li-Dousha piece reached render gate")
        if float(piece.get("speaker_confidence", 0.0)) < DEFAULT_MIN_SPEAKER_CONFIDENCE:
            raise VerificationError("speaker confidence drift")
        if float(piece.get("transcript_confidence", 0.0)) < RENDER_MIN_TRANSCRIPT_CONFIDENCE:
            raise VerificationError("transcript confidence drift")
        verification_evidence = piece.get("verification_evidence")
        transcript_rows = (
            verification_evidence.get("evidence")
            if isinstance(verification_evidence, Mapping)
            else None
        )
        if not isinstance(transcript_rows, list) or len(transcript_rows) < 2:
            raise VerificationError("transcript verification evidence drift")
        transcript_authorities: set[str] = set()
        for evidence in transcript_rows:
            if not isinstance(evidence, Mapping):
                raise VerificationError("malformed transcript evidence")
            authority = str(evidence.get("authority") or "")
            path_value = str(evidence.get("path") or "")
            expected_sha = str(evidence.get("sha256") or "").removeprefix("sha256:")
            if (
                not authority
                or not path_value
                or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
            ):
                raise VerificationError("unbound transcript evidence")
            path = Path(path_value)
            actual_sha = checked_transcript_evidence.get(path_value)
            if actual_sha is None:
                if not path.is_file():
                    raise VerificationError("transcript evidence file is missing")
                actual_sha = sha256_file(path)
                checked_transcript_evidence[path_value] = actual_sha
            if actual_sha != expected_sha:
                raise VerificationError("transcript evidence hash drift")
            transcript_authorities.add(authority)
        if len(transcript_authorities) < 2:
            raise VerificationError("transcript evidence is not independent")
        if str(piece.get("speaker_authority") or "") not in ALLOWED_SPEAKER_AUTHORITIES:
            raise VerificationError("speaker authority drift")
        speaker_evidence = piece.get("speaker_evidence")
        if not isinstance(speaker_evidence, list) or not speaker_evidence:
            raise VerificationError("speaker evidence drift")
        speaker_evidence_authorities: set[str] = set()
        for evidence in speaker_evidence:
            if not isinstance(evidence, Mapping):
                raise VerificationError("malformed speaker evidence")
            authority = str(evidence.get("authority") or "")
            path_value = str(evidence.get("path") or "")
            expected_sha = str(evidence.get("sha256") or "").removeprefix("sha256:")
            if (
                not authority
                or not path_value
                or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
            ):
                raise VerificationError("unbound speaker evidence")
            path = Path(path_value)
            actual_sha = checked_speaker_evidence.get(path_value)
            if actual_sha is None:
                if not path.is_file():
                    raise VerificationError("speaker evidence file is missing")
                actual_sha = sha256_file(path)
                checked_speaker_evidence[path_value] = actual_sha
            if actual_sha != expected_sha:
                raise VerificationError("speaker evidence hash drift")
            speaker_evidence_authorities.add(authority)
        if (
            piece.get("speaker_authority") == "verified_lidousha_voiceprint"
            and len(speaker_evidence_authorities) < 2
        ):
            raise VerificationError("voiceprint speaker evidence is not independent")
        speaker_authority = str(piece.get("speaker_authority") or "")
        if speaker_authority in PHRASE_SCOPED_SPEAKER_AUTHORITIES and not _speaker_evidence_covers_range(
            [row for row in speaker_evidence if isinstance(row, Mapping)],
            start_ms=int(piece.get("core_start_ms", -1)),
            end_ms=int(piece.get("core_end_ms", -1)),
            human_only=speaker_authority in HUMAN_SPEAKER_AUTHORITIES,
        ):
            raise VerificationError("speaker evidence does not cover piece range")
        reconstructed += normalize_text(str(piece.get("text") or ""))
    if reconstructed != str(plan.get("normalized_target") or ""):
        raise VerificationError("verified pieces no longer exactly reconstruct the target")
