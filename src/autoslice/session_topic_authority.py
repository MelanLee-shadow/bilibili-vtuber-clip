"""Session-scoped topic-name authority from recorder metadata and full chat.

The clip-local chat window is intentionally small, but proper names often get
spelled correctly elsewhere in the same live session.  A particularly strong
case is an orthographic disagreement where the room title and a full-session
chat surface have identical pinyin (for example ``仗剑传说``/``杖剑传说``).
The chat spelling becomes a bounded dynamic authority, and phonetically close
ASR variants with the same suffix are absorbed into it.
"""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import re
from typing import Any, Iterable, Mapping
import xml.etree.ElementTree as ElementTree

from src.autoslice.jingting_chunker import parse_srt_cues

try:
    from pypinyin import lazy_pinyin as _lazy_pinyin
except Exception:  # pragma: no cover - production image carries pypinyin
    _lazy_pinyin = None


_CJK_RUN_RX = re.compile(r"[一-鿿]{4,20}")
_MAX_XML_BYTES = 32 * 1024 * 1024


def _reading(value: str) -> tuple[str, ...]:
    if _lazy_pinyin is None:
        return ()
    return tuple(str(part).lower() for part in _lazy_pinyin(value) if part)


def _ngrams(value: str, length: int) -> Iterable[str]:
    for run in _CJK_RUN_RX.findall(value):
        for start in range(0, len(run) - length + 1):
            yield run[start : start + length]


def _xml_paths(spec: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for piece in spec.get("pieces") or []:
        if not isinstance(piece, Mapping):
            continue
        raw = str(piece.get("danmaku_xml_local") or "").strip()
        if raw and raw not in seen:
            paths.append(raw)
            seen.add(raw)
    return paths


def discover_session_topic_authorities(
    spec: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Find unique same-pinyin title/chat spellings from the full session.

    Exact room-title repeats are deliberately insufficient: ordinary prose in
    a title is not automatically a proper noun.  A differing, same-pinyin
    spelling in structured full-session chat is the second independent source
    that makes the narrow absorption safe.
    """

    candidates: list[dict[str, Any]] = []
    for raw_path in _xml_paths(spec):
        from pathlib import Path

        path = Path(raw_path)
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > _MAX_XML_BYTES
            ):
                continue
            root = ElementTree.fromstring(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except (OSError, ElementTree.ParseError):
            continue
        title = str(root.findtext("./metadata/room_title") or "").strip()
        chat_rows: list[dict[str, Any]] = []
        for node in root.iter("d"):
            text = str(node.text or "").strip()
            if not text:
                continue
            try:
                offset_ms = int(
                    float((node.get("p") or "0").split(",", 1)[0]) * 1000
                )
            except (TypeError, ValueError):
                continue
            chat_rows.append({"text": text, "offset_ms": offset_ms})
        for length in range(4, 11):
            title_by_reading: dict[tuple[str, ...], set[str]] = {}
            for surface in _ngrams(title, length):
                reading = _reading(surface)
                if reading:
                    title_by_reading.setdefault(reading, set()).add(surface)
            if not title_by_reading:
                continue
            chat_by_reading: dict[tuple[str, ...], Counter[str]] = {}
            chat_evidence: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
            for row in chat_rows:
                for surface in _ngrams(row["text"], length):
                    reading = _reading(surface)
                    if reading not in title_by_reading:
                        continue
                    chat_by_reading.setdefault(reading, Counter())[surface] += 1
                    chat_evidence.setdefault((reading, surface), row)
            for reading, title_surfaces in title_by_reading.items():
                counts = chat_by_reading.get(reading)
                if not counts:
                    continue
                top_count = max(counts.values())
                top_chat_surfaces = sorted(
                    surface for surface, count in counts.items() if count == top_count
                )
                if len(top_chat_surfaces) != 1:
                    continue
                canonical = top_chat_surfaces[0]
                differing_titles = sorted(
                    surface for surface in title_surfaces if surface != canonical
                )
                if not differing_titles:
                    continue
                row = chat_evidence[(reading, canonical)]
                candidates.append(
                    {
                        "canonical": canonical,
                        "title_surfaces": differing_titles,
                        "reading": list(reading),
                        "room_title": title,
                        "chat_text": row["text"],
                        "chat_offset_ms": row["offset_ms"],
                        "source_xml": str(path),
                        "chat_occurrences": top_count,
                    }
                )

    # Longest corroborated spelling owns its contained suffixes.  This keeps
    # ``杖剑传说`` rather than also emitting ``剑传说``/``传说`` authorities.
    selected: list[dict[str, Any]] = []
    seen_canonicals: set[str] = set()
    for row in sorted(
        candidates,
        key=lambda item: (-len(str(item["canonical"])), -int(item["chat_occurrences"])),
    ):
        canonical = str(row["canonical"])
        if canonical in seen_canonicals:
            continue
        if any(canonical in str(existing["canonical"]) for existing in selected):
            continue
        selected.append(row)
        seen_canonicals.add(canonical)
    return tuple(selected)


def absorb_session_topic_entities(
    srt_text: str,
    authorities: Iterable[Mapping[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Normalize close ASR variants to cross-source session spellings."""

    rows = tuple(authorities)
    audit: dict[str, Any] = {
        "schema_version": "session-topic-authority-audit.v1",
        "status": "CLEAN",
        "authorities": [dict(row) for row in rows],
        "repairs": [],
    }
    cues = parse_srt_cues(srt_text)
    rendered: list[str] = []
    for cue_index, cue in enumerate(cues, start=1):
        text = cue.text
        for row in rows:
            canonical = str(row.get("canonical") or "")
            canonical_reading = _reading(canonical)
            if len(canonical) < 4 or not canonical_reading:
                continue
            length = len(canonical)
            cursor = 0
            while cursor <= len(text) - length:
                exact_starts = [
                    match.start()
                    for match in re.finditer(re.escape(canonical), text)
                ]
                if any(
                    cursor < exact_start + length
                    and exact_start < cursor + length
                    for exact_start in exact_starts
                ):
                    cursor += 1
                    continue
                surface = text[cursor : cursor + length]
                if surface == canonical or not _CJK_RUN_RX.fullmatch(surface):
                    cursor += 1
                    continue
                surface_reading = _reading(surface)
                similarity = SequenceMatcher(
                    None,
                    " ".join(surface_reading),
                    " ".join(canonical_reading),
                    autojunk=False,
                ).ratio()
                syllable_similarities = [
                    SequenceMatcher(None, actual, expected, autojunk=False).ratio()
                    for actual, expected in zip(surface_reading, canonical_reading)
                ]
                mean_syllable_similarity = (
                    sum(syllable_similarities) / len(syllable_similarities)
                    if syllable_similarities
                    else 0.0
                )
                suffix_similarity = (
                    sum(syllable_similarities[-2:]) / 2
                    if len(syllable_similarities) >= 2
                    else 0.0
                )
                exact_syllables = sum(
                    actual == expected
                    for actual, expected in zip(surface_reading, canonical_reading)
                )
                # The authority itself has two independent structured sources.
                # Therefore permit the common ASR failure where every
                # character drifts slightly (杖剑传说 -> 钻戒传送), provided
                # the entire syllable sequence remains close and at least one
                # syllable is exact.  This is intentionally stricter than a
                # generic fuzzy text replacement and applies only to the
                # already corroborated session topic name.
                phonetic_sequence_match = (
                    len(surface_reading) == len(canonical_reading)
                    and mean_syllable_similarity >= 0.68
                    and suffix_similarity >= 0.70
                    and min(syllable_similarities, default=0.0) >= 0.50
                    and exact_syllables >= 1
                )
                standard_phonetic_match = (
                    similarity >= 0.80 and suffix_similarity >= 0.80
                )
                if not standard_phonetic_match and not phonetic_sequence_match:
                    cursor += 1
                    continue
                text = text[:cursor] + canonical + text[cursor + length :]
                audit["repairs"].append(
                    {
                        "cue_index": cue_index,
                        "surface": surface,
                        "canonical": canonical,
                        "pinyin_similarity": round(similarity, 6),
                        "mean_syllable_similarity": round(
                            mean_syllable_similarity, 6
                        ),
                        "suffix_syllable_similarity": round(suffix_similarity, 6),
                        "exact_syllables": exact_syllables,
                        "authority": "ROOM_TITLE_PLUS_FULL_SESSION_STRUCTURED_CHAT",
                    }
                )
                cursor += length
        rendered.append(
            f"{cue_index}\n{_ms_to_ts(cue.start_ms)} --> {_ms_to_ts(cue.end_ms)}\n{text}"
        )
    if audit["repairs"]:
        audit["status"] = "APPLIED"
    audit["repair_count"] = len(audit["repairs"])
    return "\n\n".join(rendered) + ("\n" if rendered else ""), audit


def _ms_to_ts(value_ms: int) -> str:
    hours, rem = divmod(int(value_ms), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
