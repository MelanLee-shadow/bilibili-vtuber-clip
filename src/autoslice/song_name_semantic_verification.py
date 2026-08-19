"""Local-first lyric semantics gate for talk-lane song-name decisions.

The gate deliberately has no network implementation.  Production loads lyric
lines from the selected profile's known-song catalog.  A caller may inject a
typed external provider and explicitly enable it, but the default always emits
a disabled verification request instead of making an outbound call.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from src.autoslice.jingting_chunker import parse_srt_cues


SCHEMA_VERSION = "song-name-semantic-verification.v1"
REQUEST_SCHEMA_VERSION = "song-name-lyrics-verification-request.v1"
_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}")
_SONG_CONTEXT_RX = re.compile(r"(下一首|点歌|想唱|接下来唱|歌词|这首歌|这句歌|歌曲大意)")
_SEMANTIC_STRIP_RX = re.compile(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", re.UNICODE)
_RECEIPT_KEYS = {
    "schema_version",
    "input_srt_sha256",
    "candidate_set_sha256",
    "evidence_lines",
    "candidate_results",
    "verification_requests",
    "preferred_candidate",
    "receipt_sha256",
}
_EVIDENCE_KEYS = {"source", "cue_index", "text"}
_RESULT_KEYS = {
    "candidate",
    "lyrics_status",
    "lyrics_provider",
    "lyrics_source_ref",
    "lyrics_sha256",
    "evidence_line_indexes",
    "matched_surfaces",
    "semantic_score",
    "verdict",
    "reason_code",
}
_SURFACE_KEYS = {
    "evidence_line_index",
    "evidence_text",
    "lyrics_line",
    "surface",
    "match_type",
    "coverage",
    "score",
}
_REQUEST_KEYS = {
    "schema_version",
    "candidate",
    "purpose",
    "local_lookup_status",
    "external_lookup_allowed",
    "external_provider",
    "status",
}
_VERDICTS = {"MATCH", "DISPUTED", "LYRICS_UNAVAILABLE", "INSUFFICIENT_EVIDENCE"}
_REQUEST_STATUSES = {"DISABLED_BY_DEFAULT", "NOT_CONFIGURED", "CALLED"}


class SongNameSemanticVerificationError(ValueError):
    """Raised when a semantic receipt is malformed or bound to other bytes."""


@dataclass(frozen=True)
class SongLyricsLookupRequest:
    candidate: str
    purpose: str = "talk_song_name_semantic_verification"


@dataclass(frozen=True)
class SongLyricsDocument:
    candidate: str
    provider: str
    source_ref: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class SongLyricsLookupResult:
    status: str
    document: SongLyricsDocument | None = None
    reason: str = ""


class SongLyricsProvider(Protocol):
    """Typed lookup seam; external implementations must be injected explicitly."""

    provider_name: str

    def lookup(self, request: SongLyricsLookupRequest) -> SongLyricsLookupResult: ...


class MappingSongLyricsProvider:
    """Deterministic local provider used by profile assets and synthetic tests."""

    def __init__(
        self,
        songs: Mapping[str, Sequence[str]],
        *,
        provider_name: str = "local_mapping",
        source_ref: str = "memory://song-lyrics",
    ) -> None:
        self.provider_name = provider_name
        self._source_ref = source_ref
        self._songs = {
            _identity_key(candidate): tuple(
                str(line).strip() for line in lines if str(line).strip()
            )
            for candidate, lines in songs.items()
            if str(candidate).strip()
        }

    def lookup(self, request: SongLyricsLookupRequest) -> SongLyricsLookupResult:
        lines = self._songs.get(_identity_key(request.candidate), ())
        if not lines:
            return SongLyricsLookupResult(status="NOT_FOUND", reason="candidate_not_in_local_catalog")
        return SongLyricsLookupResult(
            status="FOUND",
            document=SongLyricsDocument(
                candidate=request.candidate,
                provider=self.provider_name,
                source_ref=self._source_ref,
                lines=lines,
            ),
        )


def load_known_songs_lyrics_provider(path: Path) -> MappingSongLyricsProvider:
    """Load the local known-song fingerprint/LRC cache without any network fallback."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SongNameSemanticVerificationError(f"cannot read known songs {path}: {exc}") from exc
    songs = payload.get("songs") if isinstance(payload, Mapping) else None
    if not isinstance(songs, list):
        raise SongNameSemanticVerificationError("known songs payload must contain a songs list")
    local_lines: dict[str, tuple[str, ...]] = {}
    for row in songs:
        if not isinstance(row, Mapping):
            continue
        title = str(row.get("title") or "").strip()
        raw_lines = row.get("fingerprint")
        if not title or not isinstance(raw_lines, list):
            continue
        lines = tuple(str(line).strip() for line in raw_lines if str(line).strip())
        if lines:
            local_lines[title] = lines
    return MappingSongLyricsProvider(
        local_lines,
        provider_name="local_known_songs",
        source_ref=f"file://{path}",
    )


def clean_song_name_candidates(candidates: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        candidate = str(value).strip() if isinstance(value, str) else ""
        key = _identity_key(candidate)
        if len(candidate) < 2 or not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(candidate)
    return cleaned


def collect_song_semantic_evidence(
    srt_text: str,
    *,
    selection_hook: str = "",
    title_quote: str = "",
    neighbor_radius: int = 2,
) -> list[dict[str, object]]:
    """Collect title/hook plus cues neighboring an explicit song-context cue."""

    evidence: list[dict[str, object]] = []
    for source, value in (("title_quote", title_quote), ("selection_hook", selection_hook)):
        text = str(value or "").strip()
        if text:
            evidence.append({"source": source, "cue_index": None, "text": text})
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    anchors = [index for index, cue in enumerate(cues) if _SONG_CONTEXT_RX.search(cue.text)]
    selected = {
        index
        for anchor in anchors
        for index in range(max(0, anchor - neighbor_radius), min(len(cues), anchor + neighbor_radius + 1))
    }
    for index in sorted(selected):
        evidence.append({"source": "nearby_cue", "cue_index": index + 1, "text": cues[index].text})
    deduped: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for row in evidence:
        key = (row["source"], row["cue_index"], row["text"])
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    return deduped


def build_song_name_semantic_verification(
    srt_text: str,
    *,
    candidates: Sequence[str],
    evidence_lines: Sequence[Mapping[str, object]],
    local_provider: SongLyricsProvider,
    external_provider: SongLyricsProvider | None = None,
    allow_external_lookup: bool = False,
) -> dict[str, object]:
    """Build a strict hash-bound local-first semantic verification receipt."""

    cleaned = clean_song_name_candidates(candidates)
    normalized_evidence = _normalize_evidence_lines(evidence_lines)
    results: list[dict[str, object]] = []
    requests: list[dict[str, object]] = []
    for candidate in cleaned:
        request = SongLyricsLookupRequest(candidate=candidate)
        lookup = local_provider.lookup(request)
        local_status = lookup.status
        if lookup.status != "FOUND" or lookup.document is None:
            external_status = (
                "DISABLED_BY_DEFAULT" if not allow_external_lookup else "NOT_CONFIGURED"
            )
            if allow_external_lookup and external_provider is not None:
                lookup = external_provider.lookup(request)
                external_status = "CALLED"
            requests.append(
                {
                    "schema_version": REQUEST_SCHEMA_VERSION,
                    "candidate": candidate,
                    "purpose": request.purpose,
                    "local_lookup_status": local_status,
                    "external_lookup_allowed": bool(allow_external_lookup),
                    "external_provider": (
                        external_provider.provider_name if external_provider is not None else None
                    ),
                    "status": external_status,
                }
            )
        results.append(_candidate_result(candidate, lookup, normalized_evidence))
    preferred = _preferred_candidate(results)
    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "input_srt_sha256": _sha256_text(srt_text),
        "candidate_set_sha256": _sha256_json(cleaned),
        "evidence_lines": normalized_evidence,
        "candidate_results": results,
        "verification_requests": requests,
        "preferred_candidate": preferred,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _receipt_sha256(receipt)
    validate_song_name_semantic_verification(
        receipt, srt_text=srt_text, candidates=cleaned
    )
    return receipt


def verify_and_pin_song_names(
    srt_text: str,
    *,
    candidates: Sequence[str],
    known_songs_path: Path,
    selection_hook: str = "",
    title_quote: str = "",
) -> tuple[str, dict[str, object], dict[str, object]]:
    """Production choke point: build receipt first, then pass it to the pin mutator."""

    evidence = collect_song_semantic_evidence(
        srt_text,
        selection_hook=selection_hook,
        title_quote=title_quote,
    )
    verification = build_song_name_semantic_verification(
        srt_text,
        candidates=candidates,
        evidence_lines=evidence,
        local_provider=load_known_songs_lyrics_provider(known_songs_path),
    )
    from src.autoslice.song_name_pin import pin_song_names_in_srt

    output, audit = pin_song_names_in_srt(
        srt_text,
        candidates=candidates,
        semantic_verification=verification,
    )
    return output, audit, verification


def validate_song_name_semantic_verification(
    receipt: Mapping[str, object],
    *,
    srt_text: str,
    candidates: Sequence[str],
) -> dict[str, object]:
    """Validate exact fields, source bytes, candidate set, rows, and receipt hash."""

    _require_keys(receipt, _RECEIPT_KEYS, "semantic receipt")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise SongNameSemanticVerificationError("unsupported semantic receipt schema")
    if receipt.get("input_srt_sha256") != _sha256_text(srt_text):
        raise SongNameSemanticVerificationError("semantic receipt input SRT hash mismatch")
    cleaned = clean_song_name_candidates(candidates)
    if receipt.get("candidate_set_sha256") != _sha256_json(cleaned):
        raise SongNameSemanticVerificationError("semantic receipt candidate set hash mismatch")
    expected_hash = _receipt_sha256(receipt)
    if receipt.get("receipt_sha256") != expected_hash or not _SHA256_RX.fullmatch(expected_hash):
        raise SongNameSemanticVerificationError("semantic receipt hash mismatch")
    evidence = receipt.get("evidence_lines")
    results = receipt.get("candidate_results")
    requests = receipt.get("verification_requests")
    if not isinstance(evidence, list) or not isinstance(results, list) or not isinstance(requests, list):
        raise SongNameSemanticVerificationError("semantic receipt rows must be lists")
    for index, row in enumerate(evidence):
        if not isinstance(row, Mapping):
            raise SongNameSemanticVerificationError(f"evidence_lines[{index}] must be an object")
        _require_keys(row, _EVIDENCE_KEYS, f"evidence_lines[{index}]")
    row_candidates: list[str] = []
    for index, row in enumerate(results):
        if not isinstance(row, Mapping):
            raise SongNameSemanticVerificationError(f"candidate_results[{index}] must be an object")
        _require_keys(row, _RESULT_KEYS, f"candidate_results[{index}]")
        row_candidates.append(str(row.get("candidate") or ""))
        _validate_candidate_result(row, evidence_count=len(evidence))
        surfaces = row.get("matched_surfaces")
        if not isinstance(surfaces, list):
            raise SongNameSemanticVerificationError("matched_surfaces must be a list")
        for surface_index, surface in enumerate(surfaces):
            if not isinstance(surface, Mapping):
                raise SongNameSemanticVerificationError("matched surface must be an object")
            _require_keys(surface, _SURFACE_KEYS, f"matched_surfaces[{surface_index}]")
            evidence_index = surface.get("evidence_line_index")
            if (
                isinstance(evidence_index, bool)
                or not isinstance(evidence_index, int)
                or not 0 <= evidence_index < len(evidence)
                or surface.get("evidence_text") != evidence[evidence_index].get("text")
            ):
                raise SongNameSemanticVerificationError("matched surface evidence binding mismatch")
            if any(
                isinstance(surface.get(key), bool)
                or not isinstance(surface.get(key), (int, float))
                or not 0 <= float(surface[key]) <= 1
                for key in ("coverage", "score")
            ):
                raise SongNameSemanticVerificationError("matched surface score is invalid")
    if row_candidates != cleaned:
        raise SongNameSemanticVerificationError("semantic receipt candidate rows mismatch")
    for index, row in enumerate(requests):
        if not isinstance(row, Mapping):
            raise SongNameSemanticVerificationError(f"verification_requests[{index}] invalid")
        _require_keys(row, _REQUEST_KEYS, f"verification_requests[{index}]")
        if row.get("schema_version") != REQUEST_SCHEMA_VERSION:
            raise SongNameSemanticVerificationError("unsupported verification request schema")
        if row.get("candidate") not in cleaned or row.get("status") not in _REQUEST_STATUSES:
            raise SongNameSemanticVerificationError("verification request value mismatch")
        if not isinstance(row.get("external_lookup_allowed"), bool):
            raise SongNameSemanticVerificationError("external_lookup_allowed must be boolean")
    preferred = receipt.get("preferred_candidate")
    if preferred != _preferred_candidate(results):
        raise SongNameSemanticVerificationError("preferred candidate does not match result ranking")
    return dict(receipt)


def _validate_candidate_result(row: Mapping[str, object], *, evidence_count: int) -> None:
    verdict = row.get("verdict")
    score = row.get("semantic_score")
    surfaces = row.get("matched_surfaces")
    indexes = row.get("evidence_line_indexes")
    if verdict not in _VERDICTS:
        raise SongNameSemanticVerificationError("invalid semantic verdict")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise SongNameSemanticVerificationError("semantic_score must be within 0..1")
    if not isinstance(indexes, list) or any(
        isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < evidence_count
        for index in indexes
    ):
        raise SongNameSemanticVerificationError("evidence_line_indexes are invalid")
    if not isinstance(surfaces, list):
        raise SongNameSemanticVerificationError("matched_surfaces must be a list")
    if verdict == "MATCH" and (row.get("lyrics_status") != "FOUND" or not surfaces):
        raise SongNameSemanticVerificationError("MATCH requires reviewed lyrics and a match surface")
    if verdict != "MATCH" and surfaces:
        raise SongNameSemanticVerificationError("non-MATCH verdict cannot carry match surfaces")
    lyrics_sha256 = row.get("lyrics_sha256")
    if row.get("lyrics_status") == "FOUND":
        if not isinstance(lyrics_sha256, str) or not _SHA256_RX.fullmatch(lyrics_sha256):
            raise SongNameSemanticVerificationError("FOUND lyrics require a valid hash")
        if not str(row.get("lyrics_provider") or "").strip():
            raise SongNameSemanticVerificationError("FOUND lyrics require a provider")
    elif any(row.get(key) is not None for key in ("lyrics_provider", "lyrics_source_ref", "lyrics_sha256")):
        raise SongNameSemanticVerificationError("unavailable lyrics cannot claim provider evidence")


def _candidate_result(
    candidate: str,
    lookup: SongLyricsLookupResult,
    evidence_lines: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    document = lookup.document if lookup.status == "FOUND" else None
    if document is None:
        return {
            "candidate": candidate,
            "lyrics_status": lookup.status,
            "lyrics_provider": None,
            "lyrics_source_ref": None,
            "lyrics_sha256": None,
            "evidence_line_indexes": list(range(len(evidence_lines))),
            "matched_surfaces": [],
            "semantic_score": 0.0,
            "verdict": "LYRICS_UNAVAILABLE",
            "reason_code": "NO_REVIEWED_LYRICS_FOR_CANDIDATE",
        }
    surfaces = _matched_surfaces(evidence_lines, document.lines)
    best_score = max((float(row["score"]) for row in surfaces), default=0.0)
    if not evidence_lines:
        verdict, reason = "INSUFFICIENT_EVIDENCE", "NO_TALK_CONTEXT_EVIDENCE"
    elif surfaces:
        verdict, reason = "MATCH", "LYRICS_CONTEXT_SEMANTIC_MATCH"
    else:
        verdict, reason = "DISPUTED", "LYRICS_CONTEXT_SEMANTIC_MISMATCH"
    return {
        "candidate": candidate,
        "lyrics_status": "FOUND",
        "lyrics_provider": document.provider,
        "lyrics_source_ref": document.source_ref,
        "lyrics_sha256": _sha256_json(list(document.lines)),
        "evidence_line_indexes": list(range(len(evidence_lines))),
        "matched_surfaces": surfaces,
        "semantic_score": round(best_score, 4),
        "verdict": verdict,
        "reason_code": reason,
    }


def _matched_surfaces(
    evidence_lines: Sequence[Mapping[str, object]], lyrics_lines: Sequence[str]
) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    for evidence_index, evidence in enumerate(evidence_lines):
        evidence_text = str(evidence.get("text") or "")
        evidence_folded = _semantic_fold(evidence_text)
        if len(evidence_folded) < 4:
            continue
        best: dict[str, object] | None = None
        for lyrics_line in lyrics_lines:
            lyric_folded = _semantic_fold(lyrics_line)
            if len(lyric_folded) < 4:
                continue
            match = SequenceMatcher(None, evidence_folded, lyric_folded).find_longest_match()
            surface = evidence_folded[match.a : match.a + match.size]
            coverage = match.size / min(len(evidence_folded), len(lyric_folded))
            ratio = SequenceMatcher(None, evidence_folded, lyric_folded).ratio()
            exact = evidence_folded in lyric_folded or lyric_folded in evidence_folded
            score = 1.0 if exact else max(coverage, ratio)
            accepted = exact and min(len(evidence_folded), len(lyric_folded)) >= 8
            accepted = accepted or (match.size >= 8 and coverage >= 0.6 and ratio >= 0.55)
            if not accepted:
                continue
            row = {
                "evidence_line_index": evidence_index,
                "evidence_text": evidence_text,
                "lyrics_line": str(lyrics_line),
                "surface": surface,
                "match_type": "normalized_phrase" if exact else "bounded_shared_phrase",
                "coverage": round(coverage, 4),
                "score": round(score, 4),
            }
            if best is None or float(row["score"]) > float(best["score"]):
                best = row
        if best is not None:
            matches.append(best)
    return matches


def _preferred_candidate(results: Sequence[Mapping[str, object]]) -> str | None:
    matches = [row for row in results if row.get("verdict") == "MATCH"]
    if not matches:
        return None
    ranked = sorted(matches, key=lambda row: float(row.get("semantic_score") or 0.0), reverse=True)
    if len(ranked) > 1 and ranked[0].get("semantic_score") == ranked[1].get("semantic_score"):
        return None
    return str(ranked[0]["candidate"])


def _normalize_evidence_lines(
    evidence_lines: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for row in evidence_lines:
        source = str(row.get("source") or "").strip()
        text = str(row.get("text") or "").strip()
        cue_index = row.get("cue_index")
        if not source or not text:
            continue
        if cue_index is not None and (isinstance(cue_index, bool) or not isinstance(cue_index, int)):
            raise SongNameSemanticVerificationError("evidence cue_index must be an integer or null")
        normalized.append({"source": source, "cue_index": cue_index, "text": text})
    return normalized


def _identity_key(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _semantic_fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _SEMANTIC_STRIP_RX.sub("", normalized)


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(encoded)


def _receipt_sha256(receipt: Mapping[str, object]) -> str:
    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return _sha256_json(payload)


def _require_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        raise SongNameSemanticVerificationError(
            f"{label} keys mismatch: missing={missing}, unknown={unknown}"
        )
