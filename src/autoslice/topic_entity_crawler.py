"""Bounded Bangumi enrichment for the topic/entity graph.

Input is the already validated timely-term snapshot.  Search only discovers a
stable subject ID; every emitted character spelling comes from structured
subject/character data, never from arbitrary news or community prose.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import datetime as dt
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping
import unicodedata
import urllib.parse

from scripts.gemini_slice_jingting import validate_timely_terms_payload
from src.autoslice.timely_term_crawler import BoundedHttpClient, CrawlError
from src.autoslice.topic_entity_graph import MAX_TOPICS as MAX_GRAPH_TOPICS
from src.autoslice.topic_entity_graph import validate_topic_entity_graph


_NON_IDENTITY = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", re.IGNORECASE)
_GENERIC_TOPICS = frozenset(
    {
        "anime",
        "manga",
        "acgproject",
        "acgevent",
        "bangumi",
        "bilibili",
        "bilibilicommunity",
        "二次元",
        "二次元社区",
        "动漫展",
        "漫展",
        "同人",
    }
)
_ROLE = {"主角": "MAIN", "配角": "SUPPORTING"}
_LATIN_WORD = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_TITLE_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "cour",
        "of",
        "part",
        "season",
        "stage",
        "the",
    }
)
_INSTALLMENT_AFTER_KIND = re.compile(
    r"(?i)\b(season|cour|part|stage)\s*[-:]?\s*(\d+|[ivx]+)\b"
)
_INSTALLMENT_BEFORE_KIND = re.compile(
    r"(?i)\b(\d+|[ivx]+)(?:st|nd|rd|th)?\s*(season|cour|part|stage)\b"
)
_CJK_INSTALLMENT = re.compile(r"第\s*([一二三四五六七八九十0-9]+)\s*(季|期|部|クール)")
_JAPANESE_COUR = re.compile(r"(?:第\s*)?([0-9]+)\s*クール")
_ROMAN_INSTALLMENT = re.compile(r"(?<![A-Za-z])([IVX]{2,4})(?![A-Za-z])")
_CHINESE_ORDINALS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True)
class GraphCrawlResult:
    graph: dict[str, Any]
    query_count: int
    work_count: int
    entity_count: int
    diagnostics: tuple[str, ...]


def _identity(value: object) -> str:
    return _NON_IDENTITY.sub("", unicodedata.normalize("NFKC", str(value))).casefold()


def _clean(value: object, *, max_chars: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace("\r", " ").replace("\n", " ").split()).strip()
    if not text or len(text) > max_chars or any(ord(ch) < 32 or ch in "{}" for ch in text):
        return ""
    return text


def _unique(values: Iterable[object], *, limit: int = 32) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _clean(raw)
        key = _identity(value)
        if not value or not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _ordinal(value: str) -> int | None:
    normalized = value.strip().upper()
    if normalized.isdigit():
        number = int(normalized)
        return number if 0 < number <= 99 else None
    if normalized in _CHINESE_ORDINALS:
        return _CHINESE_ORDINALS[normalized]
    if re.fullmatch(r"[IVX]+", normalized):
        total = 0
        prior = 0
        for char in reversed(normalized):
            current = {"I": 1, "V": 5, "X": 10}[char]
            total += -current if current < prior else current
            prior = max(prior, current)
        return total if 0 < total <= 99 else None
    return None


def _installment_signature(value: object) -> tuple[tuple[str, int], ...]:
    """Normalize Season/Cour/Part spellings so adjacent installments cannot mix."""

    text = unicodedata.normalize("NFKC", str(value))
    result: set[tuple[str, int]] = set()
    kind_map = {
        "season": "season",
        "stage": "season",
        "季": "season",
        "期": "season",
        "cour": "cour",
        "クール": "cour",
        "part": "part",
        "部": "part",
    }
    occupied: list[tuple[int, int]] = []
    for pattern, kind_index, ordinal_index in (
        (_INSTALLMENT_AFTER_KIND, 1, 2),
        (_INSTALLMENT_BEFORE_KIND, 2, 1),
    ):
        for match in pattern.finditer(text):
            number = _ordinal(match.group(ordinal_index))
            if number is not None:
                result.add((kind_map[match.group(kind_index).casefold()], number))
                occupied.append(match.span())
    for match in _CJK_INSTALLMENT.finditer(text):
        number = _ordinal(match.group(1))
        if number is not None:
            result.add((kind_map[match.group(2)], number))
            occupied.append(match.span())
    for match in _JAPANESE_COUR.finditer(text):
        number = _ordinal(match.group(1))
        if number is not None:
            result.add(("cour", number))
            occupied.append(match.span())
    for match in _ROMAN_INSTALLMENT.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in occupied):
            continue
        number = _ordinal(match.group(1))
        if number is not None:
            result.add(("season", number))
    return tuple(sorted(result))


def _installment_free_identity(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value))
    for pattern in (
        _INSTALLMENT_AFTER_KIND,
        _INSTALLMENT_BEFORE_KIND,
        _CJK_INSTALLMENT,
        _JAPANESE_COUR,
        _ROMAN_INSTALLMENT,
    ):
        text = pattern.sub(" ", text)
    return _identity(text)


def _topic_id(canonical: object) -> str:
    topic_key = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:20]
    return f"timely:{topic_key}"


def _query_candidates(term: Mapping[str, Any]) -> list[str]:
    # Discovery searches the exact current work before parent franchises.  The
    # timely crawler's readings are structured AniList/Bangumi titles; sort
    # them by specificity so ``BanG Dream YUME MITA`` beats phonetic
    # ``mengxianda`` and a Season/Cour title beats its bare franchise.
    readings = _unique(term.get("readings") or [], limit=16)
    aliases = _unique(term.get("aliases") or [], limit=16)

    def specificity(value: str) -> tuple[int, int, str]:
        marker = bool(_installment_signature(value))
        return (int(marker), len(_identity(value)), value.casefold())

    readings.sort(key=specificity, reverse=True)
    reading_keys = {_identity(value) for value in readings}
    aliases = [value for value in aliases if _identity(value) not in reading_keys]
    aliases.sort(key=specificity, reverse=True)
    values = [term.get("canonical"), *readings[:4], *aliases[:3], term.get("display_name")]
    return [value for value in _unique(values, limit=9) if _identity(value) not in _GENERIC_TOPICS]


def _franchise_query_candidates(term: Mapping[str, Any]) -> list[str]:
    """Return sibling works before the generic parent franchise."""

    anchors = [
        value
        for value in _unique(term.get("topic_entities") or [], limit=24)
        if _identity(value) not in _GENERIC_TOPICS
    ]
    if len(anchors) <= 1:
        return anchors
    parent, rest = anchors[0], anchors[1:]
    latin = [value for value in rest if re.search(r"[A-Za-z]", value)]
    native = [value for value in rest if value not in latin]
    return _unique([*latin, *native, parent], limit=12)


def _fallback_query_candidates(term: Mapping[str, Any]) -> list[str]:
    """Structured same-franchise works used only if current work has no cast."""

    return [
        value
        for value in _unique(term.get("topic_entities") or [], limit=16)
        if _identity(value) not in _GENERIC_TOPICS
    ]


def _source_subject_rows(term: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    """Use stable Bangumi subject IDs already carried by the timely snapshot."""

    rows: dict[int, dict[str, Any]] = {}
    for source in term.get("sources") or []:
        if not str(source.get("publisher") or "").casefold().startswith("bangumi structured"):
            continue
        parsed = urllib.parse.urlsplit(str(source.get("url") or ""))
        if (parsed.hostname or "").lower() not in {"bgm.tv", "www.bgm.tv"}:
            continue
        match = re.fullmatch(r"/subject/(\d+)/?", parsed.path)
        if not match:
            continue
        subject_id = int(match.group(1))
        rows[subject_id] = {
            "id": subject_id,
            "type": 2,
            "name": str(term.get("canonical") or ""),
            "name_cn": str(term.get("display_name") or term.get("canonical") or ""),
            "collection": {},
        }
    return rows


def _is_anime_term(term: Mapping[str, Any]) -> bool:
    """Keep the character graph scoped to anime, not manga/news/events."""

    entities = {_identity(value) for value in term.get("topic_entities") or []}
    if entities & {"acgevent", "动漫展", "漫展"}:
        return False
    if "anime" in entities:
        return True
    # Reviewed seed assets predate the machine crawler's generic ``Anime``
    # topic marker.  Preserve a narrowly source-backed compatibility path for
    # explicit TV-anime records without admitting manga/news-only terms.
    reason = str(term.get("reason") or "").casefold()
    publishers = [
        str(source.get("publisher") or "").casefold() for source in term.get("sources") or []
    ]
    return reason.startswith("tv anime ") and any("anime" in value for value in publishers)


def _is_community_term(term: Mapping[str, Any]) -> bool:
    entities = {_identity(value) for value in term.get("topic_entities") or []}
    if entities & {"bilibilicommunity", "二次元社区"}:
        return True
    return any("community" in str(source.get("publisher") or "").casefold() for source in term.get("sources") or [])


def _ordered_terms_for_crawl(
    terms: Iterable[Mapping[str, Any]],
    *,
    as_of: dt.date,
    max_topics: int,
    future_horizon: dt.timedelta,
    generated_at: dt.datetime | None = None,
    previous_topics: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[Mapping[str, Any]]:
    """Choose a current-heavy frontier while reserving space for new seasons.

    The timely snapshot is already relevance-ranked.  We preserve that order,
    but reserve one quarter of the frontier for machine-discovered upcoming
    anime.  As dates advance, those nodes move into the current quota without
    a reviewed dictionary or a code change.
    """

    current: list[tuple[int, Mapping[str, Any]]] = []
    upcoming: list[tuple[int, Mapping[str, Any]]] = []
    future_limit = as_of + future_horizon
    for index, term in enumerate(terms):
        if not _is_anime_term(term) or not _query_candidates(term):
            continue
        active_from = dt.date.fromisoformat(str(term["active_from"]))
        active_until = dt.date.fromisoformat(str(term["active_until"]))
        if active_until < as_of or active_from > future_limit:
            continue
        if not any(dt.date.fromisoformat(source["published_at"]) <= as_of for source in term["sources"]):
            continue
        (current if active_from <= as_of else upcoming).append((index, term))

    prior = previous_topics or {}
    now = generated_at

    def refresh_order(item: tuple[int, Mapping[str, Any]]) -> tuple[int, dt.datetime, int]:
        index, term = item
        topic = prior.get(_topic_id(term["canonical"]))
        if topic is None or now is None:
            return (0, dt.datetime.min.replace(tzinfo=dt.timezone.utc), index)
        refreshed_at = dt.datetime.fromisoformat(str(topic["refreshed_at"]))
        refresh_expires_at = dt.datetime.fromisoformat(str(topic["refresh_expires_at"]))
        # Missing/expired nodes come first.  Once the graph is filled, the
        # oldest still-fresh nodes rotate to the front deterministically.
        due = refresh_expires_at <= now
        return (0 if due else 1, refreshed_at, index)

    current.sort(key=refresh_order)
    upcoming.sort(key=refresh_order)

    upcoming_quota = min(len(upcoming), max_topics // 4) if upcoming else 0
    current_quota = max_topics - upcoming_quota
    primary = [*current[:current_quota], *upcoming[:upcoming_quota]]
    if len(primary) < max_topics:
        selected = {index for index, _term in primary}
        for item in [*current, *upcoming]:
            if item[0] in selected:
                continue
            primary.append(item)
            selected.add(item[0])
            if len(primary) >= max_topics:
                break
    selected = {index for index, _term in primary}
    ordered = [*primary, *(item for item in [*current, *upcoming] if item[0] not in selected)]
    return [term for _index, term in ordered]


def _search_score(query: str, row: Mapping[str, Any]) -> float:
    query_key = _identity(query)
    query_installment = _installment_signature(query)
    names = _unique([row.get("name_cn"), row.get("name")], limit=2)
    best = 0.0
    for name in names:
        key = _identity(name)
        if not key:
            continue
        # A Season/Cour/Part query may not silently collapse to the franchise
        # base work.  If no current installment has a cast yet, the caller has
        # an explicit same-franchise fallback path after materialization.
        if query_installment:
            name_installment = _installment_signature(name)
            if not name_installment:
                continue
            if any(
                not any(
                    query_number == name_number and query_kind == name_kind
                    for name_kind, name_number in name_installment
                )
                for query_kind, query_number in query_installment
            ):
                continue
            if key == query_key:
                best = max(best, 1.0)
                continue
            query_base = _installment_free_identity(query)
            name_base = _installment_free_identity(name)
            if query_base and query_base == name_base:
                best = max(best, 0.99)
                continue
            if (
                min(len(query_base), len(name_base)) >= 4
                and (query_base in name_base or name_base in query_base)
            ):
                best = max(best, 0.9)
                continue
        if key == query_key:
            best = max(best, 1.0)
        elif len(query_key) >= 4 and (query_key in key or key in query_key):
            best = max(best, 0.88)
        else:
            query_words = {
                word for word in _LATIN_WORD.findall(str(query).casefold()) if word not in _TITLE_STOP_WORDS and not word.isdigit()
            }
            name_words = {
                word for word in _LATIN_WORD.findall(name.casefold()) if word not in _TITLE_STOP_WORDS and not word.isdigit()
            }
            if min(len(query_words), len(name_words)) >= 2:
                overlap = query_words & name_words
                if len(overlap) < 2 or len(overlap) / min(len(query_words), len(name_words)) < 0.6:
                    continue
            best = max(best, SequenceMatcher(None, query_key, key).ratio())
    return best


def _stable_search_url(query: str) -> str:
    return "https://bgm.tv/subject_search/" + urllib.parse.quote(query, safe="") + "?cat=2"


def _search_subjects(
    client: BoundedHttpClient,
    endpoint: str,
    query: str,
    *,
    max_results: int,
) -> list[dict[str, Any]]:
    body = json.dumps(
        {"keyword": query, "filter": {"type": [2]}}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    response = client.fetch(endpoint, method="POST", body=body, content_type="application/json")
    try:
        payload = json.loads(response.body)
        rows = payload["data"]
    except (UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise CrawlError("Bangumi subject search returned malformed data") from exc
    if not isinstance(rows, list):
        raise CrawlError("Bangumi subject search data is not a list")
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for raw in rows[:50]:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), int) or raw.get("type") != 2:
            continue
        score = _search_score(query, raw)
        if score < 0.68:
            continue
        popularity = int((raw.get("collection") or {}).get("collect") or 0)
        scored.append((score, popularity, raw))
    scored.sort(key=lambda item: (-item[0], -item[1], int(item[2]["id"])))
    return [row for _score, _popularity, row in scored[:max_results]]


def _subject_large(
    client: BoundedHttpClient,
    endpoint_template: str,
    subject_id: int,
) -> dict[str, Any]:
    endpoint = endpoint_template.replace("{subject_id}", str(subject_id))
    response = client.fetch(endpoint)
    try:
        payload = json.loads(response.body)
    except (UnicodeError, ValueError) as exc:
        raise CrawlError("Bangumi subject graph response is malformed") from exc
    if not isinstance(payload, dict) or payload.get("id") != subject_id:
        raise CrawlError("Bangumi subject graph response has the wrong identity")
    return payload


def _source(url: str, published_at: str, publisher: str) -> dict[str, str]:
    return {"url": url, "published_at": published_at, "publisher": publisher}


def _subject_date(row: Mapping[str, Any], fallback: str) -> str:
    for value in (row.get("air_date"), row.get("date")):
        try:
            return dt.date.fromisoformat(str(value)).isoformat()
        except ValueError:
            continue
    return fallback


def _aliases_from_info(info: object) -> tuple[list[str], list[str], str]:
    aliases: list[str] = []
    readings: list[str] = []
    spaced_native = ""
    if not isinstance(info, dict):
        return aliases, readings, spaced_native
    raw_aliases = info.get("alias") or []
    if not isinstance(raw_aliases, list):
        return aliases, readings, spaced_native
    for raw in raw_aliases:
        if not isinstance(raw, dict):
            continue
        key = _identity(raw.get("key"))
        value = _clean(raw.get("value"))
        if not value:
            continue
        if key in {"kana", "romaji", "假名", "纯假名", "羅馬字", "罗马字"}:
            readings.append(value)
        else:
            # Bangumi sometimes stores several independently useful aliases in
            # one structured value (for example ``オカルン、玄灵小子``).
            # Split punctuation-delimited atoms so ASR output can match each
            # spoken surface independently.
            aliases.extend(
                part
                for part in (_clean(item) for item in re.split(r"[、，,;/；]+", value))
                if part
            )
        if key in {"jp", "japanese", "日文名"} and " " in value:
            spaced_native = value
    return _unique(aliases, limit=20), _unique(readings, limit=16), spaced_native


def _short_name_aliases(canonical_zh: str, spaced_native: str, readings: list[str]) -> tuple[list[str], list[str]]:
    aliases: list[str] = []
    short_readings: list[str] = []
    native_parts = spaced_native.split()
    if len(native_parts) >= 2:
        compact = "".join(native_parts)
        given_length = len(native_parts[-1])
        if len(compact) == len(canonical_zh) and 0 < given_length < len(canonical_zh):
            aliases.append(canonical_zh[-given_length:])
        aliases.append(native_parts[-1])
    for reading in readings:
        parts = reading.split()
        if len(parts) >= 2:
            short_readings.append(parts[-1])
    return _unique(aliases, limit=4), _unique(short_readings, limit=8)


def _character_node(raw: Mapping[str, Any], *, work_id: str, aired_from: str) -> dict[str, Any] | None:
    character_id = raw.get("id")
    role = _ROLE.get(str(raw.get("role_name") or raw.get("relation") or ""))
    canonical_zh = _clean(raw.get("name_cn") or (raw.get("info") or {}).get("name_cn"))
    native = _clean(raw.get("name"))
    if not isinstance(character_id, int) or role is None or not canonical_zh or not native:
        return None
    aliases, readings, spaced_native = _aliases_from_info(raw.get("info"))
    short_aliases, short_readings = _short_name_aliases(canonical_zh, spaced_native, readings)
    native_names = _unique([native, spaced_native], limit=8)
    aliases = _unique([*aliases, *short_aliases], limit=24)
    readings = _unique([*readings, *short_readings], limit=20)
    if not readings:
        # A native spelling is still a legitimate reading aid, but never
        # invent kana/romaji that the source did not provide.
        readings = native_names[:]
    return {
        "entity_id": f"bgm:character:{character_id}",
        "kind": "character",
        "canonical_zh": canonical_zh,
        "native_names": native_names,
        "aliases": [value for value in aliases if _identity(value) not in {_identity(canonical_zh), *map(_identity, native_names)}],
        "readings": readings,
        "role": role,
        "work_ids": [work_id],
        "sources": [
            _source(
                f"https://bgm.tv/character/{character_id}",
                aired_from,
                "Bangumi structured character record",
            )
        ],
    }


def _merge_retained_topics(
    *,
    topics: list[dict[str, Any]],
    works_by_id: dict[str, dict[str, Any]],
    entities_by_id: dict[str, dict[str, Any]],
    previous_graph: Mapping[str, Any] | None,
    eligible_terms: Iterable[Mapping[str, Any]],
    generated_at: dt.datetime,
) -> None:
    """Carry forward source-backed nodes until their per-topic refresh TTL.

    The current timely snapshot still controls topic aliases, sources, and
    active windows.  Only the structured work/character subgraph is retained,
    and all reciprocal edges are rebuilt after the merge.
    """

    if previous_graph is None:
        return
    eligible_terms = list(eligible_terms)
    fresh_topic_ids = {topic["topic_id"] for topic in topics}
    previous_topics = {row["topic_id"]: row for row in previous_graph["topics"]}
    previous_works = {row["work_id"]: row for row in previous_graph["works"]}
    previous_entities = {row["entity_id"]: row for row in previous_graph["entities"]}
    for term in eligible_terms:
        if len(topics) >= MAX_GRAPH_TOPICS:
            break
        topic_id = _topic_id(term["canonical"])
        previous_topic = previous_topics.get(topic_id)
        if (
            previous_topic is None
            or topic_id in fresh_topic_ids
            or dt.datetime.fromisoformat(previous_topic["refresh_expires_at"]) <= generated_at
        ):
            continue
        retained_work_ids = [
            work_id for work_id in previous_topic["work_ids"] if work_id in previous_works
        ]
        if not retained_work_ids:
            continue
        aliases = _unique(
            [
                term["canonical"],
                term.get("display_name"),
                *term["aliases"],
                *term["topic_entities"],
            ],
            limit=32,
        )
        topics.append(
            {
                "topic_id": topic_id,
                "canonical": term["canonical"],
                "aliases": [
                    value
                    for value in aliases
                    if _identity(value) != _identity(term["canonical"])
                ],
                "work_ids": retained_work_ids,
                "active_from": term["active_from"],
                "active_until": term["active_until"],
                "sources": term["sources"],
                "refreshed_at": previous_topic["refreshed_at"],
                "refresh_expires_at": previous_topic["refresh_expires_at"],
            }
        )
        for work_id in retained_work_ids:
            prior_work = previous_works[work_id]
            works_by_id.setdefault(work_id, copy.deepcopy(prior_work))
            for entity_id in prior_work["entity_ids"]:
                if entity_id in previous_entities:
                    entities_by_id.setdefault(entity_id, copy.deepcopy(previous_entities[entity_id]))

    # Rebuild edges from topic -> work -> entity.  This removes stale sibling
    # references when only part of an older graph remains eligible.
    valid_topics: list[dict[str, Any]] = []
    for topic in topics:
        valid_work_ids: list[str] = []
        for work_id in topic["work_ids"]:
            work = works_by_id.get(work_id)
            if work is None:
                continue
            work["entity_ids"] = [
                entity_id for entity_id in work["entity_ids"] if entity_id in entities_by_id
            ]
            if work["entity_ids"]:
                valid_work_ids.append(work_id)
        if valid_work_ids:
            topic["work_ids"] = sorted(set(valid_work_ids))
            valid_topics.append(topic)
    topics[:] = valid_topics

    topic_refs_by_work: dict[str, set[str]] = {}
    for topic in topics:
        for work_id in topic["work_ids"]:
            topic_refs_by_work.setdefault(work_id, set()).add(topic["topic_id"])
    retained_works: dict[str, dict[str, Any]] = {}
    for work_id, topic_ids in topic_refs_by_work.items():
        work = works_by_id[work_id]
        work["topic_ids"] = sorted(topic_ids)
        retained_works[work_id] = work
    works_by_id.clear()
    works_by_id.update(retained_works)

    work_refs_by_entity: dict[str, set[str]] = {}
    for work in works_by_id.values():
        for entity_id in work["entity_ids"]:
            work_refs_by_entity.setdefault(entity_id, set()).add(work["work_id"])
    retained_entities: dict[str, dict[str, Any]] = {}
    for entity_id, work_ids in work_refs_by_entity.items():
        entity = entities_by_id[entity_id]
        entity["work_ids"] = sorted(work_ids)
        retained_entities[entity_id] = entity
    entities_by_id.clear()
    entities_by_id.update(retained_entities)


@dataclass(frozen=True)
class _TopicCrawlSetup:
    previous: dict[str, Any] | None
    ordered_terms: list[dict[str, Any]]
    effective_max_queries: int
    states: list[dict[str, Any]]


def _prepare_topic_crawl(
    *,
    client: BoundedHttpClient,
    timely_snapshot: object,
    generated_at: dt.datetime,
    input_timely_terms_sha256: str,
    max_topics: int,
    max_queries: int,
    max_works_per_topic: int,
    future_horizon: dt.timedelta,
    previous_graph: object | None,
    node_ttl: dt.timedelta,
) -> _TopicCrawlSetup:
    """Validate immutable inputs and prepare the breadth-first term frontier."""

    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must include a timezone")
    if not 1 <= max_topics <= 96 or max_queries < 1 or not 1 <= max_works_per_topic <= 24:
        raise ValueError("invalid topic/entity crawl budget")
    if future_horizon < dt.timedelta(0) or future_horizon > dt.timedelta(days=366):
        raise ValueError("future_horizon must be between zero and 366 days")
    if node_ttl <= dt.timedelta(0) or node_ttl > dt.timedelta(days=366):
        raise ValueError("node_ttl must be between zero and 366 days")
    snapshot = validate_timely_terms_payload(timely_snapshot)
    previous = validate_topic_entity_graph(previous_graph) if previous_graph is not None else None
    if previous is not None and dt.datetime.fromisoformat(previous["generated_at"]) > generated_at:
        raise ValueError("previous_graph cannot come from the future")
    if not re.fullmatch(r"[0-9a-f]{64}", input_timely_terms_sha256):
        raise ValueError("input_timely_terms_sha256 must bind the exact source snapshot")
    ordered_terms = _ordered_terms_for_crawl(
        snapshot["terms"],
        as_of=generated_at.date(),
        max_topics=max_topics,
        future_horizon=future_horizon,
        generated_at=generated_at,
        previous_topics={row["topic_id"]: row for row in previous["topics"]} if previous else None,
    )
    effective_max_queries = max_queries
    client_max_requests = getattr(client, "max_requests", None)
    if isinstance(client_max_requests, int):
        remaining = max(0, client_max_requests - int(getattr(client, "requests_made", 0)))
        effective_max_queries = min(effective_max_queries, remaining // 2)
    if effective_max_queries < 1:
        raise CrawlError("request budget cannot cover one search and one subject record")
    states: list[dict[str, Any]] = []
    for term in ordered_terms:
        source_rows = _source_subject_rows(term)
        states.append(
            {
                "term": term,
                "queries": _query_candidates(term),
                "next_query": 0,
                "franchise_queries": _franchise_query_candidates(term),
                "next_franchise_query": 0,
                "fallback_queries": _fallback_query_candidates(term),
                "next_fallback_query": 0,
                "attempted_queries": set(),
                "found_subjects": source_rows,
                "matched_queries_by_subject": {
                    subject_id: [str(term["canonical"])] for subject_id in source_rows
                },
            }
        )
    return _TopicCrawlSetup(previous, ordered_terms, effective_max_queries, states)


@dataclass
class _TopicQueryScheduler:
    client: BoundedHttpClient
    search_endpoint: str
    query_limit: int
    diagnostics: list[str]
    query_count: int = 0

    def query_value(self, state: dict[str, Any], query: str) -> bool:
        query_key = _identity(query)
        if (
            self.query_count >= self.query_limit
            or not query_key
            or query_key in state["attempted_queries"]
        ):
            return False
        state["attempted_queries"].add(query_key)
        self.query_count += 1
        try:
            for row in _search_subjects(
                self.client,
                self.search_endpoint,
                query,
                # Preserve breadth: a franchise spelling cannot consume every slot.
                max_results=1,
            ):
                subject_id = int(row["id"])
                state["found_subjects"].setdefault(subject_id, row)
                state["matched_queries_by_subject"].setdefault(subject_id, []).append(query)
        except Exception as exc:
            self.diagnostics.append(f"query {query!r}: {type(exc).__name__}: {exc}")
        return True

    def _query_once(self, state: dict[str, Any], *, values: str, cursor: str) -> bool:
        while state[cursor] < len(state[values]):
            if self.query_count >= self.query_limit:
                return False
            query = state[values][state[cursor]]
            state[cursor] += 1
            if self.query_value(state, query):
                return True
        return False

    def query_once(self, state: dict[str, Any]) -> bool:
        return self._query_once(state, values="queries", cursor="next_query")

    def query_franchise_once(self, state: dict[str, Any]) -> bool:
        return self._query_once(
            state, values="franchise_queries", cursor="next_franchise_query"
        )

    def query_fallback_once(self, state: dict[str, Any]) -> bool:
        return self._query_once(
            state, values="fallback_queries", cursor="next_fallback_query"
        )


def _finalize_crawled_graph(
    *,
    topics: list[dict[str, Any]],
    works_by_id: dict[str, dict[str, Any]],
    entities_by_id: dict[str, dict[str, Any]],
    previous: dict[str, Any] | None,
    ordered_terms: list[dict[str, Any]],
    generated_at: dt.datetime,
    input_timely_terms_sha256: str,
    ttl: dt.timedelta,
    query_count: int,
    diagnostics: list[str],
) -> GraphCrawlResult:
    _merge_retained_topics(
        topics=topics,
        works_by_id=works_by_id,
        entities_by_id=entities_by_id,
        previous_graph=previous,
        eligible_terms=ordered_terms,
        generated_at=generated_at,
    )
    if not topics or not works_by_id or not entities_by_id:
        raise CrawlError("no source-backed topic/entity subgraph could be built")
    graph = {
        "schema_version": "lidousha-topic-entity-graph.v1",
        "generator": "scripts/crawl_topic_entity_graph.py",
        "input_timely_terms_sha256": input_timely_terms_sha256,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "expires_at": (generated_at + ttl).isoformat(timespec="seconds"),
        "status": "fresh",
        "topics": sorted(topics, key=lambda row: row["topic_id"]),
        "works": sorted(works_by_id.values(), key=lambda row: row["work_id"]),
        "entities": sorted(entities_by_id.values(), key=lambda row: row["entity_id"]),
    }
    normalized = validate_topic_entity_graph(graph)
    return GraphCrawlResult(
        normalized,
        query_count,
        len(normalized["works"]),
        len(normalized["entities"]),
        tuple(diagnostics),
    )


def crawl_topic_entity_graph(
    *,
    client: BoundedHttpClient,
    timely_snapshot: object,
    input_timely_terms_sha256: str,
    generated_at: dt.datetime,
    search_endpoint: str = "https://api.bgm.tv/v0/search/subjects",
    subject_endpoint_template: str = "https://api.bgm.tv/subject/{subject_id}?responseGroup=large",
    max_topics: int = 16,
    max_queries: int = 40,
    max_works_per_topic: int = 3,
    max_entities_per_work: int = 16,
    ttl: dt.timedelta = dt.timedelta(days=7),
    future_horizon: dt.timedelta = dt.timedelta(days=183),
    previous_graph: object | None = None,
    node_ttl: dt.timedelta = dt.timedelta(days=30),
) -> GraphCrawlResult:
    setup = _prepare_topic_crawl(
        client=client,
        timely_snapshot=timely_snapshot,
        generated_at=generated_at,
        input_timely_terms_sha256=input_timely_terms_sha256,
        max_topics=max_topics,
        max_queries=max_queries,
        max_works_per_topic=max_works_per_topic,
        future_horizon=future_horizon,
        previous_graph=previous_graph,
        node_ttl=node_ttl,
    )
    previous = setup.previous
    ordered_terms = setup.ordered_terms
    effective_max_queries = setup.effective_max_queries
    states = setup.states

    topics: list[dict[str, Any]] = []
    works_by_id: dict[str, dict[str, Any]] = {}
    entities_by_id: dict[str, dict[str, Any]] = {}
    diagnostics: list[str] = []
    community_expansion_queries = sum(
        max(0, max_works_per_topic - 1)
        for state in states[:max_topics]
        if _is_community_term(state["term"])
    )
    expansion_reserve = min(
        max(0, effective_max_queries - 1),
        min(2, community_expansion_queries) + min(2, effective_max_queries // 16),
    )
    query_limit = max(1, effective_max_queries - expansion_reserve)
    scheduler = _TopicQueryScheduler(client, search_endpoint, query_limit, diagnostics)

    primary_states = states[:max_topics]

    # Round one is deliberately breadth-first: every primary anime gets its
    # exact canonical query before any nickname/franchise gets a second query.
    for state in primary_states:
        if state["found_subjects"]:
            continue
        if not scheduler.query_once(state):
            break

    # A single retry round lets machine/community spellings reach their first
    # structured work anchor without returning to the old six-queries-per-term
    # starvation pattern.
    for state in primary_states:
        if state["found_subjects"]:
            continue
        if not scheduler.query_once(state) and scheduler.query_count >= effective_max_queries:
            break

    # If some primary terms do not exist in Bangumi yet, fill their slots from
    # later snapshot terms in breadth-first batches, with at most two discovery
    # spellings per fallback term.
    cursor = max_topics
    while scheduler.query_count < query_limit:
        resolved = sum(bool(state["found_subjects"]) for state in states[:cursor])
        if resolved >= max_topics or cursor >= len(states):
            break
        batch_size = min(max_topics - resolved, len(states) - cursor)
        batch = states[cursor : cursor + batch_size]
        cursor += batch_size
        for state in batch:
            if not scheduler.query_once(state):
                break
        for state in batch:
            if state["found_subjects"]:
                continue
            if not scheduler.query_once(state) and scheduler.query_count >= effective_max_queries:
                break

    scheduler.query_limit = effective_max_queries
    # The discovery reserve must first finish unresolved states already inside
    # the chosen frontier.  Otherwise a successful run can skip the last
    # primary term and jump to the next ranked term merely because one request
    # was held for fallback/expansion.
    for state in states[:cursor]:
        if state["found_subjects"]:
            continue
        if not scheduler.query_once(state):
            break
    selected_states = [state for state in states[:cursor] if state["found_subjects"]][:max_topics]

    def expand_community(state: dict[str, Any]) -> None:
        """Bound sibling discovery after the current specific work resolves."""

        if not _is_community_term(state["term"]):
            return
        expansion_attempts = 0
        while (
            len(state["found_subjects"]) < max_works_per_topic
            and scheduler.query_count < effective_max_queries
            and expansion_attempts < max_works_per_topic
            and scheduler.query_franchise_once(state)
        ):
            expansion_attempts += 1

    # Community terms are the one intentional bounded expansion: after broad
    # discovery succeeds, search their structured franchise/work anchors so a
    # fan nickname can route to sibling work-specific character subgraphs.
    for state in selected_states:
        expand_community(state)

    subject_cache: dict[int, dict[str, Any]] = {}

    def materialize_state(state: dict[str, Any]) -> dict[str, Any] | None:
        term = state["term"]
        found_subjects = state["found_subjects"]
        matched_queries_by_subject = state["matched_queries_by_subject"]
        if not found_subjects:
            return None
        topic_id = _topic_id(term["canonical"])
        topic_work_ids: list[str] = []

        processed_subjects: set[int] = set()

        def materialize_subject(subject_id: int, search_row: Mapping[str, Any]) -> str | None:
            try:
                subject = subject_cache.get(subject_id)
                if subject is None:
                    subject = _subject_large(client, subject_endpoint_template, subject_id)
                    subject_cache[subject_id] = subject
            except Exception as exc:
                diagnostics.append(f"subject {subject_id}: {type(exc).__name__}: {exc}")
                return None
            work_id = f"bgm:subject:{subject_id}"
            aired_from = _subject_date(subject, str(term["active_from"]))
            canonical = _clean(subject.get("name_cn") or subject.get("name") or search_row.get("name_cn") or search_row.get("name"))
            aliases = _unique(
                [
                    subject.get("name"),
                    subject.get("name_cn"),
                    search_row.get("name"),
                    search_row.get("name_cn"),
                    *matched_queries_by_subject.get(subject_id, []),
                ],
                limit=20,
            )
            aliases = [value for value in aliases if _identity(value) != _identity(canonical)]
            if not canonical:
                return None
            entity_ids: list[str] = []
            characters = subject.get("crt") or []
            if not isinstance(characters, list):
                characters = []
            for raw_character in characters:
                if len(entity_ids) >= max_entities_per_work or not isinstance(raw_character, dict):
                    break
                node = _character_node(raw_character, work_id=work_id, aired_from=aired_from)
                if node is None:
                    continue
                entity_id = node["entity_id"]
                prior = entities_by_id.get(entity_id)
                if prior is None:
                    entities_by_id[entity_id] = node
                elif work_id not in prior["work_ids"]:
                    prior["work_ids"].append(work_id)
                entity_ids.append(entity_id)
            if not entity_ids:
                return None
            prior_work = works_by_id.get(work_id)
            if prior_work is None:
                works_by_id[work_id] = {
                    "work_id": work_id,
                    "canonical": canonical,
                    "aliases": aliases,
                    "topic_ids": [topic_id],
                    "entity_ids": entity_ids,
                    "aired_from": aired_from,
                    "sources": [
                        _source(
                            f"https://bgm.tv/subject/{subject_id}",
                            aired_from,
                            "Bangumi structured anime subject",
                        )
                    ],
                }
            elif topic_id not in prior_work["topic_ids"]:
                prior_work["topic_ids"].append(topic_id)
            return work_id

        def materialize_new_subjects() -> None:
            for subject_id, search_row in list(found_subjects.items()):
                if subject_id in processed_subjects or len(topic_work_ids) >= max_works_per_topic:
                    continue
                processed_subjects.add(subject_id)
                work_id = materialize_subject(subject_id, search_row)
                if work_id is not None:
                    topic_work_ids.append(work_id)

        materialize_new_subjects()

        # Some announced/current-season Bangumi subjects exist before their
        # cast list is populated.  Only then query a structured same-franchise
        # base/older work; this supplies candidate names without pretending the
        # fallback work is the newly announced season.
        fallback_attempts = 0
        while (
            not topic_work_ids
            and fallback_attempts < max_works_per_topic
            and scheduler.query_count < effective_max_queries
            and scheduler.query_fallback_once(state)
        ):
            fallback_attempts += 1
            materialize_new_subjects()
        if not topic_work_ids:
            return None
        aliases = _unique(
            [term["canonical"], term.get("display_name"), *term["aliases"], *term["topic_entities"]],
            limit=32,
        )
        aliases = [value for value in aliases if _identity(value) != _identity(term["canonical"])]
        return {
            "topic_id": topic_id,
            "canonical": term["canonical"],
            "aliases": aliases,
            "work_ids": sorted(set(topic_work_ids)),
            "active_from": term["active_from"],
            "active_until": term["active_until"],
            "sources": term["sources"],
            "refreshed_at": generated_at.isoformat(timespec="seconds"),
            "refresh_expires_at": (generated_at + node_ttl).isoformat(timespec="seconds"),
        }

    materialized_state_ids: set[int] = set()

    def materialize_candidates(candidates: Iterable[dict[str, Any]]) -> None:
        for state in candidates:
            if len(topics) >= max_topics or id(state) in materialized_state_ids:
                continue
            materialized_state_ids.add(id(state))
            topic = materialize_state(state)
            if topic is not None:
                topics.append(topic)

    materialize_candidates(selected_states)
    # A subject search hit is not a usable topic until structured character
    # nodes materialize.  Consume already-discovered alternates first, then
    # discover later terms in breadth-first batches until the actual topic
    # count reaches the requested frontier.
    materialize_candidates(
        state for state in states[:cursor] if state["found_subjects"]
    )
    while len(topics) < max_topics and cursor < len(states) and scheduler.query_count < effective_max_queries:
        batch_size = min(max_topics - len(topics), len(states) - cursor)
        batch = states[cursor : cursor + batch_size]
        cursor += batch_size
        for state in batch:
            if state["found_subjects"]:
                continue
            if not scheduler.query_once(state):
                break
        for state in batch:
            if state["found_subjects"]:
                continue
            if not scheduler.query_once(state) and scheduler.query_count >= effective_max_queries:
                break
        for state in batch:
            if state["found_subjects"]:
                expand_community(state)
        materialize_candidates(state for state in batch if state["found_subjects"])

    return _finalize_crawled_graph(
        topics=topics,
        works_by_id=works_by_id,
        entities_by_id=entities_by_id,
        previous=previous,
        ordered_terms=ordered_terms,
        generated_at=generated_at,
        input_timely_terms_sha256=input_timely_terms_sha256,
        ttl=ttl,
        query_count=scheduler.query_count,
        diagnostics=diagnostics,
    )


def graph_json(graph: object) -> str:
    return json.dumps(validate_topic_entity_graph(graph), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_graph_atomically(graph: object, destination: Path) -> tuple[str, bool]:
    text = graph_json(graph)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        if destination.read_text(encoding="utf-8") == text:
            return digest, False
    except OSError:
        pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o444)
        os.replace(temporary, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return digest, True
