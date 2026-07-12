"""Bounded Bangumi enrichment for the topic/entity graph.

Input is the already validated timely-term snapshot.  Search only discovers a
stable subject ID; every emitted character spelling comes from structured
subject/character data, never from arbitrary news or community prose.
"""

from __future__ import annotations

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


def _query_candidates(term: Mapping[str, Any]) -> list[str]:
    # Search the exact timely entity first, then its franchise/topic anchors.
    # Community spelling variants are valuable graph aliases, but spending the
    # whole request budget on three variants of the same nickname prevents the
    # structured catalog from finding the parent franchise.
    values = [term.get("canonical"), term.get("display_name")]
    values.extend(term.get("topic_entities") or [])
    values.extend(term.get("aliases") or [])
    return [value for value in _unique(values, limit=12) if _identity(value) not in _GENERIC_TOPICS]


def _search_score(query: str, row: Mapping[str, Any]) -> float:
    query_key = _identity(query)
    names = _unique([row.get("name_cn"), row.get("name")], limit=2)
    best = 0.0
    for name in names:
        key = _identity(name)
        if not key:
            continue
        if key == query_key:
            best = max(best, 1.0)
        elif len(query_key) >= 4 and (query_key in key or key in query_key):
            best = max(best, 0.88)
        else:
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
        if score < 0.62:
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
            aliases.append(value)
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


def crawl_topic_entity_graph(
    *,
    client: BoundedHttpClient,
    timely_snapshot: object,
    input_timely_terms_sha256: str,
    generated_at: dt.datetime,
    search_endpoint: str = "https://api.bgm.tv/v0/search/subjects",
    subject_endpoint_template: str = "https://api.bgm.tv/subject/{subject_id}?responseGroup=large",
    max_topics: int = 6,
    max_queries: int = 12,
    max_works_per_topic: int = 4,
    max_entities_per_work: int = 16,
    ttl: dt.timedelta = dt.timedelta(days=7),
) -> GraphCrawlResult:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must include a timezone")
    snapshot = validate_timely_terms_payload(timely_snapshot)
    if not re.fullmatch(r"[0-9a-f]{64}", input_timely_terms_sha256):
        raise ValueError("input_timely_terms_sha256 must bind the exact source snapshot")
    as_of = generated_at.date()
    active_terms = []
    for term in snapshot["terms"]:
        if not dt.date.fromisoformat(term["active_from"]) <= as_of <= dt.date.fromisoformat(term["active_until"]):
            continue
        if not any(dt.date.fromisoformat(source["published_at"]) <= as_of for source in term["sources"]):
            continue
        if _query_candidates(term):
            active_terms.append(term)

    topics: list[dict[str, Any]] = []
    works_by_id: dict[str, dict[str, Any]] = {}
    entities_by_id: dict[str, dict[str, Any]] = {}
    diagnostics: list[str] = []
    query_count = 0
    for term in active_terms:
        if len(topics) >= max_topics or query_count >= max_queries:
            break
        root_queries = _query_candidates(term)[:6]
        found_subjects: dict[int, dict[str, Any]] = {}
        matched_queries_by_subject: dict[int, list[str]] = {}
        for query in root_queries:
            if query_count >= max_queries:
                break
            query_count += 1
            try:
                for row in _search_subjects(
                    client,
                    search_endpoint,
                    query,
                    # One best work per spelling/topic anchor preserves breadth:
                    # a franchise query must not fill every slot with old
                    # seasons before a sibling anchor such as MyGO is searched.
                    max_results=1,
                ):
                    subject_id = int(row["id"])
                    found_subjects.setdefault(subject_id, row)
                    matched_queries_by_subject.setdefault(subject_id, []).append(query)
            except Exception as exc:
                diagnostics.append(f"query {query!r}: {type(exc).__name__}: {exc}")
        if not found_subjects:
            continue
        topic_key = hashlib.sha256(str(term["canonical"]).encode("utf-8")).hexdigest()[:20]
        topic_id = f"timely:{topic_key}"
        topic_work_ids: list[str] = []
        for subject_id, search_row in list(found_subjects.items())[:max_works_per_topic]:
            try:
                subject = _subject_large(client, subject_endpoint_template, subject_id)
            except Exception as exc:
                diagnostics.append(f"subject {subject_id}: {type(exc).__name__}: {exc}")
                continue
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
                continue
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
                continue
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
            topic_work_ids.append(work_id)
        if not topic_work_ids:
            continue
        aliases = _unique(
            [term["canonical"], term.get("display_name"), *term["aliases"], *term["topic_entities"]],
            limit=32,
        )
        aliases = [value for value in aliases if _identity(value) != _identity(term["canonical"])]
        topics.append(
            {
                "topic_id": topic_id,
                "canonical": term["canonical"],
                "aliases": aliases,
                "work_ids": sorted(set(topic_work_ids)),
                "active_from": term["active_from"],
                "active_until": term["active_until"],
                "sources": term["sources"],
            }
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
