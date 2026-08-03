"""Source-backed topic -> work -> proper-name routing for subtitle correction.

The graph is deliberately separate from the global glossary.  A clip must
first match a topic/work surface before any child character names are exposed
to a correction model.  The graph narrows candidates; raw audio and exact
structured chat remain the authorities for which candidate was spoken.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
import unicodedata
import urllib.parse

from src.autoslice.chat_authority import ReferentEntity, ReferentGroup


GRAPH_SCHEMA = "lidousha-topic-entity-graph.v1"
GRAPH_GENERATOR = "scripts/crawl_topic_entity_graph.py"
RESOLUTION_SCHEMA = "topic-resolution.v1"
MAX_GRAPH_BYTES = 4 * 1024 * 1024
MAX_TOPICS = 96
MAX_WORKS = 384
MAX_ENTITIES = 4096
MAX_SCOPED_ENTITIES = 32
_ATOM_MAX = 160
_NON_IDENTITY = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", re.IGNORECASE)
_INSTRUCTION = re.compile(
    r"(?i)(?:\bignore\b.{0,24}\binstructions?\b|"
    r"\b(?:system|developer)\b.{0,24}\bprompt\b|"
    r"忽略.{0,12}(?:指令|提示)|系统提示|执行.{0,12}命令)"
)
_ALLOWED_SOURCE_HOSTS = frozenset(
    {
        "anilist.co",
        "api.bgm.tv",
        "animenewsnetwork.com",
        "bgm.tv",
        "bilibili.com",
        "bang-dream.com",
        "bushiroad.com",
        "tv-tokyo.co.jp",
    }
)
_EVIDENCE_WEIGHTS = {
    "structured_chat": 7,
    "screen_text": 7,
    "selection_hook": 5,
    "agy": 4,
    "transcript": 3,
    "bcut": 3,
}


class TopicEntityGraphError(ValueError):
    """A graph or resolution artifact is unsafe or internally inconsistent."""


@dataclass(frozen=True)
class TopicEvidence:
    kind: str
    text: str
    source_sha256: str = ""


@dataclass(frozen=True)
class TopicResolution:
    status: str
    selected_topic_ids: tuple[str, ...]
    selected_work_ids: tuple[str, ...]
    scoped_entity_ids: tuple[str, ...]
    matches: tuple[dict[str, Any], ...]
    graph_sha256: str
    recording_date: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESOLUTION_SCHEMA,
            "status": self.status,
            "recording_date": self.recording_date,
            "graph_sha256": self.graph_sha256,
            "selected_topic_ids": list(self.selected_topic_ids),
            "selected_work_ids": list(self.selected_work_ids),
            "scoped_entity_ids": list(self.scoped_entity_ids),
            "evidence": list(self.matches),
        }


def build_scoped_topic_context(
    graph: Mapping[str, object],
    resolution: TopicResolution,
) -> dict[str, object]:
    """Project source-backed selected nodes for downstream whole-clip review."""

    topic_ids = set(resolution.selected_topic_ids)
    work_ids = set(resolution.selected_work_ids)
    entity_ids = set(resolution.scoped_entity_ids)
    topic_fields = (
        "topic_id",
        "canonical",
        "aliases",
        "active_from",
        "active_until",
        "sources",
    )
    work_fields = (
        "work_id",
        "canonical",
        "aliases",
        "aired_from",
        "sources",
    )
    entity_fields = (
        "entity_id",
        "canonical_zh",
        "native_names",
        "aliases",
        "readings",
        "role",
        "sources",
    )

    def selected_rows(
        rows: object,
        *,
        id_field: str,
        selected_ids: set[str],
        fields: Sequence[str],
    ) -> list[dict[str, object]]:
        return [
            {field: row.get(field) for field in fields}
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, Mapping)
            and str(row.get(id_field) or "") in selected_ids
        ]

    return {
        "schema_version": "topic-scoped-context.v1",
        "status": (
            "SCOPED"
            if topic_ids or work_ids or entity_ids
            else "NO_SELECTED_NODES"
        ),
        "recording_date": resolution.recording_date,
        "graph_sha256": resolution.graph_sha256,
        "topics": selected_rows(
            graph.get("topics"),
            id_field="topic_id",
            selected_ids=topic_ids,
            fields=topic_fields,
        ),
        "works": selected_rows(
            graph.get("works"),
            id_field="work_id",
            selected_ids=work_ids,
            fields=work_fields,
        ),
        "entities": selected_rows(
            graph.get("entities"),
            id_field="entity_id",
            selected_ids=entity_ids,
            fields=entity_fields,
        ),
    }


def _identity(value: object) -> str:
    return _NON_IDENTITY.sub("", unicodedata.normalize("NFKC", str(value))).casefold()


def _atom(value: object, *, label: str, max_chars: int = _ATOM_MAX) -> str:
    if not isinstance(value, str):
        raise TopicEntityGraphError(f"{label} must be text")
    text = " ".join(value.replace("\r", " ").replace("\n", " ").split()).strip()
    if not text or len(text) > max_chars or _INSTRUCTION.search(text):
        raise TopicEntityGraphError(f"{label} is unsafe")
    if any(ord(ch) < 32 or ch in "{}" for ch in text):
        raise TopicEntityGraphError(f"{label} contains unsafe characters")
    return text


def _identifier(value: object, *, label: str) -> str:
    text = _atom(value, label=label, max_chars=128)
    if not re.fullmatch(r"[a-z0-9][a-z0-9:._-]{2,127}", text):
        raise TopicEntityGraphError(f"{label} is not a stable identifier")
    return text


def _atom_list(
    value: object,
    *,
    label: str,
    min_items: int = 0,
    max_items: int = 32,
) -> list[str]:
    if not isinstance(value, list) or not min_items <= len(value) <= max_items:
        raise TopicEntityGraphError(f"{label} has an invalid length")
    result: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = _atom(raw, label=f"{label}[{index}]")
        key = _identity(item)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    if len(result) < min_items:
        raise TopicEntityGraphError(f"{label} has too few unique values")
    return result


def _id_list(value: object, *, label: str, max_items: int = 128) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise TopicEntityGraphError(f"{label} has an invalid length")
    result = [_identifier(item, label=f"{label}[{index}]") for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise TopicEntityGraphError(f"{label} contains duplicates")
    return result


def _timestamp(value: object, *, label: str) -> str:
    text = _atom(value, label=label, max_chars=40)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TopicEntityGraphError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TopicEntityGraphError(f"{label} must include a timezone")
    return parsed.isoformat(timespec="seconds")


def _date(value: object, *, label: str) -> str:
    text = _atom(value, label=label, max_chars=10)
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise TopicEntityGraphError(f"{label} is not an ISO date") from exc


def _source(value: object, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"url", "published_at", "publisher"}:
        raise TopicEntityGraphError(f"{label} has unknown or missing fields")
    url = _atom(value["url"], label=f"{label}.url", max_chars=500)
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.port
        or not any(host == allowed or host.endswith("." + allowed) for allowed in _ALLOWED_SOURCE_HOSTS)
    ):
        raise TopicEntityGraphError(f"{label}.url is outside the source allowlist")
    clean_url = urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    return {
        "url": clean_url,
        "published_at": _date(value["published_at"], label=f"{label}.published_at"),
        "publisher": _atom(value["publisher"], label=f"{label}.publisher", max_chars=120),
    }


def _sources(value: object, *, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise TopicEntityGraphError(f"{label} must contain 1..8 sources")
    rows = [_source(item, label=f"{label}[{index}]") for index, item in enumerate(value)]
    if len({row["url"] for row in rows}) != len(rows):
        raise TopicEntityGraphError(f"{label} contains duplicate URLs")
    return rows


def validate_topic_entity_graph(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "generator",
        "input_timely_terms_sha256",
        "generated_at",
        "expires_at",
        "status",
        "topics",
        "works",
        "entities",
    }:
        raise TopicEntityGraphError("graph has unknown or missing top-level fields")
    if (
        payload["schema_version"] != GRAPH_SCHEMA
        or payload["generator"] != GRAPH_GENERATOR
        or payload["status"] != "fresh"
    ):
        raise TopicEntityGraphError("graph schema/status is unsupported")
    input_timely_terms_sha256 = _atom(
        payload["input_timely_terms_sha256"],
        label="input_timely_terms_sha256",
        max_chars=64,
    )
    if not re.fullmatch(r"[0-9a-f]{64}", input_timely_terms_sha256):
        raise TopicEntityGraphError("input timely-term SHA256 is invalid")
    generated_at = _timestamp(payload["generated_at"], label="generated_at")
    expires_at = _timestamp(payload["expires_at"], label="expires_at")
    if dt.datetime.fromisoformat(generated_at) >= dt.datetime.fromisoformat(expires_at):
        raise TopicEntityGraphError("graph expiry must follow generation")

    raw_topics = payload["topics"]
    raw_works = payload["works"]
    raw_entities = payload["entities"]
    if not isinstance(raw_topics, list) or not 1 <= len(raw_topics) <= MAX_TOPICS:
        raise TopicEntityGraphError("topics has an invalid length")
    if not isinstance(raw_works, list) or not 1 <= len(raw_works) <= MAX_WORKS:
        raise TopicEntityGraphError("works has an invalid length")
    if not isinstance(raw_entities, list) or not 1 <= len(raw_entities) <= MAX_ENTITIES:
        raise TopicEntityGraphError("entities has an invalid length")

    topics: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_topics):
        label = f"topics[{index}]"
        topic_fields = {
            "topic_id",
            "canonical",
            "aliases",
            "work_ids",
            "active_from",
            "active_until",
            "sources",
        }
        refresh_fields = {"refreshed_at", "refresh_expires_at"}
        if not isinstance(raw, dict) or set(raw) not in {
            frozenset(topic_fields),
            frozenset(topic_fields | refresh_fields),
        }:
            raise TopicEntityGraphError(f"{label} has unknown or missing fields")
        refreshed_at = _timestamp(
            raw.get("refreshed_at", generated_at),
            label=f"{label}.refreshed_at",
        )
        refresh_expires_at = _timestamp(
            raw.get("refresh_expires_at", expires_at),
            label=f"{label}.refresh_expires_at",
        )
        if (
            dt.datetime.fromisoformat(refreshed_at) > dt.datetime.fromisoformat(generated_at)
            or dt.datetime.fromisoformat(refreshed_at)
            >= dt.datetime.fromisoformat(refresh_expires_at)
        ):
            raise TopicEntityGraphError(f"{label} has an invalid refresh window")
        topics.append(
            {
                "topic_id": _identifier(raw["topic_id"], label=f"{label}.topic_id"),
                "canonical": _atom(raw["canonical"], label=f"{label}.canonical"),
                "aliases": _atom_list(raw["aliases"], label=f"{label}.aliases", max_items=32),
                "work_ids": _id_list(raw["work_ids"], label=f"{label}.work_ids", max_items=24),
                "active_from": _date(raw["active_from"], label=f"{label}.active_from"),
                "active_until": _date(raw["active_until"], label=f"{label}.active_until"),
                "sources": _sources(raw["sources"], label=f"{label}.sources"),
                "refreshed_at": refreshed_at,
                "refresh_expires_at": refresh_expires_at,
            }
        )

    works: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_works):
        label = f"works[{index}]"
        work_fields = {
            "work_id",
            "canonical",
            "aliases",
            "topic_ids",
            "entity_ids",
            "aired_from",
            "sources",
        }
        if not isinstance(raw, dict) or set(raw) not in {
            frozenset(work_fields),
            frozenset(work_fields | {"retrieval_entity_ids"}),
        }:
            raise TopicEntityGraphError(f"{label} has unknown or missing fields")
        work = {
                "work_id": _identifier(raw["work_id"], label=f"{label}.work_id"),
                "canonical": _atom(raw["canonical"], label=f"{label}.canonical"),
                "aliases": _atom_list(raw["aliases"], label=f"{label}.aliases", max_items=32),
                "topic_ids": _id_list(raw["topic_ids"], label=f"{label}.topic_ids", max_items=12),
                "entity_ids": _id_list(raw["entity_ids"], label=f"{label}.entity_ids", max_items=64),
                "aired_from": _date(raw["aired_from"], label=f"{label}.aired_from"),
                "sources": _sources(raw["sources"], label=f"{label}.sources"),
        }
        if "retrieval_entity_ids" in raw:
            work["retrieval_entity_ids"] = _id_list(
                raw["retrieval_entity_ids"],
                label=f"{label}.retrieval_entity_ids",
                max_items=32,
            )
        works.append(work)

    entities: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_entities):
        label = f"entities[{index}]"
        entity_fields = {
            "entity_id",
            "kind",
            "canonical_zh",
            "native_names",
            "aliases",
            "readings",
            "role",
            "work_ids",
            "sources",
        }
        if not isinstance(raw, dict) or set(raw) not in {
            frozenset(entity_fields),
            frozenset(entity_fields | {"activation_work_ids"}),
        }:
            raise TopicEntityGraphError(f"{label} has unknown or missing fields")
        supported_role = (
            raw["kind"] == "character"
            and raw["role"] in {"MAIN", "SUPPORTING"}
        ) or (
            raw["kind"] == "unit"
            and raw["role"] == "RELATED"
        )
        if not supported_role:
            raise TopicEntityGraphError(f"{label} kind/role is unsupported")
        entity = {
                "entity_id": _identifier(raw["entity_id"], label=f"{label}.entity_id"),
                "kind": raw["kind"],
                "canonical_zh": _atom(raw["canonical_zh"], label=f"{label}.canonical_zh"),
                "native_names": _atom_list(raw["native_names"], label=f"{label}.native_names", min_items=1, max_items=12),
                "aliases": _atom_list(raw["aliases"], label=f"{label}.aliases", max_items=24),
                "readings": _atom_list(raw["readings"], label=f"{label}.readings", min_items=1, max_items=20),
                "role": raw["role"],
                "work_ids": _id_list(raw["work_ids"], label=f"{label}.work_ids", max_items=16),
                "sources": _sources(raw["sources"], label=f"{label}.sources"),
        }
        if "activation_work_ids" in raw:
            entity["activation_work_ids"] = _id_list(
                raw["activation_work_ids"],
                label=f"{label}.activation_work_ids",
                max_items=16,
            )
        entities.append(entity)

    topic_ids = [row["topic_id"] for row in topics]
    work_ids = [row["work_id"] for row in works]
    entity_ids = [row["entity_id"] for row in entities]
    if len(topic_ids) != len(set(topic_ids)) or len(work_ids) != len(set(work_ids)) or len(entity_ids) != len(set(entity_ids)):
        raise TopicEntityGraphError("graph IDs must be unique within each node kind")
    topic_set, work_set, entity_set = set(topic_ids), set(work_ids), set(entity_ids)
    topics_by_id = {row["topic_id"]: row for row in topics}
    works_by_id = {row["work_id"]: row for row in works}
    entities_by_id = {row["entity_id"]: row for row in entities}
    for row in topics:
        if not set(row["work_ids"]) <= work_set or row["active_from"] > row["active_until"]:
            raise TopicEntityGraphError(f"topic {row['topic_id']} has invalid edges/window")
        if any(row["topic_id"] not in works_by_id[work_id]["topic_ids"] for work_id in row["work_ids"]):
            raise TopicEntityGraphError(f"topic {row['topic_id']} has a non-reciprocal work edge")
    for row in works:
        retrieval_ids = row.get("retrieval_entity_ids", [])
        if (
            not set(row["topic_ids"]) <= topic_set
            or not set(row["entity_ids"]) <= entity_set
            or not set(retrieval_ids) <= entity_set
        ):
            raise TopicEntityGraphError(f"work {row['work_id']} has dangling edges")
        if any(row["work_id"] not in topics_by_id[topic_id]["work_ids"] for topic_id in row["topic_ids"]):
            raise TopicEntityGraphError(f"work {row['work_id']} has a non-reciprocal topic edge")
        if any(row["work_id"] not in entities_by_id[entity_id]["work_ids"] for entity_id in row["entity_ids"]):
            raise TopicEntityGraphError(f"work {row['work_id']} has a non-reciprocal entity edge")
        if any(
            row["work_id"]
            not in entities_by_id[entity_id].get("activation_work_ids", [])
            for entity_id in retrieval_ids
        ):
            raise TopicEntityGraphError(
                f"work {row['work_id']} has a non-reciprocal retrieval edge"
            )
    for row in entities:
        activation_ids = row.get("activation_work_ids", [])
        if not set(row["work_ids"]) <= work_set or not set(activation_ids) <= work_set:
            raise TopicEntityGraphError(f"entity {row['entity_id']} has dangling work edges")
        if any(row["entity_id"] not in works_by_id[work_id]["entity_ids"] for work_id in row["work_ids"]):
            raise TopicEntityGraphError(f"entity {row['entity_id']} has a non-reciprocal work edge")
        if any(
            row["entity_id"]
            not in works_by_id[work_id].get("retrieval_entity_ids", [])
            for work_id in activation_ids
        ):
            raise TopicEntityGraphError(
                f"entity {row['entity_id']} has a non-reciprocal activation edge"
            )
    return {
        "schema_version": GRAPH_SCHEMA,
        "generator": GRAPH_GENERATOR,
        "input_timely_terms_sha256": input_timely_terms_sha256,
        "generated_at": generated_at,
        "expires_at": expires_at,
        "status": "fresh",
        "topics": topics,
        "works": works,
        "entities": entities,
    }


def load_topic_entity_graph(path: str | Path, *, expected_sha256: str = "") -> tuple[dict[str, Any], str]:
    source = Path(path)
    raw = source.read_bytes()
    if len(raw) > MAX_GRAPH_BYTES:
        raise TopicEntityGraphError("graph exceeds the maximum byte size")
    digest = hashlib.sha256(raw).hexdigest()
    expected = expected_sha256.removeprefix("sha256:").lower().strip()
    if expected and (not re.fullmatch(r"[0-9a-f]{64}", expected) or digest != expected):
        raise TopicEntityGraphError("graph SHA256 binding mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise TopicEntityGraphError("graph is not UTF-8 JSON") from exc
    return validate_topic_entity_graph(payload), digest


def _surface_match(surface: str, text: str) -> bool:
    key = _identity(surface)
    target = _identity(text)
    if len(key) < 2 or key not in target:
        return False
    if key.isascii() and len(key) <= 3:
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(surface)}(?![a-z0-9])", text, re.IGNORECASE))
    return True


def resolve_topic_context(
    graph: Mapping[str, Any],
    evidence: Sequence[TopicEvidence | Mapping[str, Any]],
    *,
    recording_date: str,
    graph_sha256: str,
    max_entities: int = MAX_SCOPED_ENTITIES,
) -> TopicResolution:
    try:
        as_of = dt.date.fromisoformat(recording_date)
    except ValueError:
        return TopicResolution("NO_MATCH", (), (), (), (), graph_sha256, recording_date)
    topics = {row["topic_id"]: row for row in graph["topics"]}
    works = {row["work_id"]: row for row in graph["works"]}
    entities = {row["entity_id"]: row for row in graph["entities"]}
    topic_scores: dict[str, int] = {}
    direct_work_scores: dict[str, int] = {}
    chat_work_scores: dict[str, int] = {}
    matches: list[dict[str, Any]] = []
    for raw in evidence:
        kind = str(raw.kind if isinstance(raw, TopicEvidence) else raw.get("kind") or "transcript")
        text = str(raw.text if isinstance(raw, TopicEvidence) else raw.get("text") or "")
        source_hash = str(raw.source_sha256 if isinstance(raw, TopicEvidence) else raw.get("source_sha256") or "")
        if not text.strip():
            continue
        weight = _EVIDENCE_WEIGHTS.get(kind, 2)
        for topic_id, topic in topics.items():
            if not dt.date.fromisoformat(topic["active_from"]) <= as_of <= dt.date.fromisoformat(topic["active_until"]):
                continue
            for surface in [topic["canonical"], *topic["aliases"]]:
                if _surface_match(surface, text):
                    topic_scores[topic_id] = topic_scores.get(topic_id, 0) + weight
                    matches.append({"kind": kind, "node_id": topic_id, "surface": surface, "source_sha256": source_hash})
                    break
        for work_id, work in works.items():
            if dt.date.fromisoformat(work["aired_from"]) > as_of:
                continue
            for surface in [work["canonical"], *work["aliases"]]:
                if _surface_match(surface, text):
                    work_scores = (
                        chat_work_scores if kind == "structured_chat" else direct_work_scores
                    )
                    work_scores[work_id] = work_scores.get(work_id, 0) + weight * 2
                    matches.append({"kind": kind, "node_id": work_id, "surface": surface, "source_sha256": source_hash})
                    break

    selected_work_ids: set[str] = set()
    selected_topic_ids: set[str] = set()
    # Nearby chat is exact authority when the streamer is demonstrably reading
    # that message, but an unaligned sibling-work name in chat must not decide
    # the topic for the whole clip.  Prefer explicit transcript/selection/screen
    # work matches; use structured chat as a work router only as a fallback.
    work_scores = direct_work_scores or chat_work_scores
    if work_scores:
        best_work_score = max(work_scores.values())
        selected_work_ids = {key for key, score in work_scores.items() if score == best_work_score}
        selected_topic_ids = {
            topic_id
            for work_id in selected_work_ids
            for topic_id in works[work_id]["topic_ids"]
        }
    elif topic_scores:
        best_topic_score = max(topic_scores.values())
        selected_topic_ids = {key for key, score in topic_scores.items() if score == best_topic_score}
        selected_work_ids = {
            work_id
            for topic_id in selected_topic_ids
            for work_id in topics[topic_id]["work_ids"]
            if work_id in works and dt.date.fromisoformat(works[work_id]["aired_from"]) <= as_of
        }
    if not selected_topic_ids or not selected_work_ids:
        return TopicResolution("NO_MATCH", (), (), (), tuple(matches), graph_sha256, recording_date)
    unrelated_roots = {
        tuple(sorted(works[work_id]["topic_ids"])) for work_id in selected_work_ids
    }
    if len(selected_topic_ids) > 1 and len(unrelated_roots) > 1:
        return TopicResolution(
            "AMBIGUOUS",
            tuple(sorted(selected_topic_ids)),
            tuple(sorted(selected_work_ids)),
            (),
            tuple(matches),
            graph_sha256,
            recording_date,
        )
    role_rank = {"MAIN": 0, "SUPPORTING": 1, "RELATED": 2}
    scoped = sorted(
        {
            entity_id
            for work_id in selected_work_ids
            for entity_id in [
                *works[work_id]["entity_ids"],
                *works[work_id].get("retrieval_entity_ids", []),
            ]
            if entity_id in entities
        },
        key=lambda entity_id: (
            role_rank.get(entities[entity_id]["role"], 9),
            entities[entity_id]["canonical_zh"],
            entity_id,
        ),
    )[:max_entities]
    return TopicResolution(
        "RESOLVED" if scoped else "NO_MATCH",
        tuple(sorted(selected_topic_ids)),
        tuple(sorted(selected_work_ids)),
        tuple(scoped),
        tuple(matches),
        graph_sha256,
        recording_date,
    )


def render_scoped_entity_context(graph: Mapping[str, Any], resolution: TopicResolution) -> str:
    if resolution.status != "RESOLVED" or not resolution.scoped_entity_ids:
        return ""
    topics = {row["topic_id"]: row for row in graph["topics"]}
    works = {row["work_id"]: row for row in graph["works"]}
    entities = {row["entity_id"]: row for row in graph["entities"]}
    header = {
        "topics": [topics[item]["canonical"] for item in resolution.selected_topic_ids],
        "works": [works[item]["canonical"] for item in resolution.selected_work_ids],
    }
    rows = []
    for entity_id in resolution.scoped_entity_ids:
        entity = entities[entity_id]
        rows.append(
            {
                "entity_id": entity_id,
                "kind": entity["kind"],
                "canonical_zh": entity["canonical_zh"],
                "native_names": entity["native_names"],
                "aliases": entity["aliases"],
                "readings": entity["readings"],
                "role": entity["role"],
            }
        )
    return (
        "\n已解析话题的专名子图（仅限本片话题；先按实际发音确认 entity_id，再使用 canonical_zh，"
        "不得把同作品角色/组合按热度互换；不确定就保留草稿）:\n"
        + json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        + "\n".join(
            "- " + json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for row in rows
        )
        + "\n"
    )


def _entity_surfaces(entity: Mapping[str, Any]) -> tuple[str, ...]:
    values = [entity["canonical_zh"], *entity["native_names"], *entity["aliases"]]
    return tuple(dict.fromkeys(str(value) for value in values if str(value).strip()))


def _phonetic_similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    left_values = [*left["readings"], *_entity_surfaces(left)]
    right_values = [*right["readings"], *_entity_surfaces(right)]
    return max(
        (
            SequenceMatcher(None, _identity(a), _identity(b)).ratio()
            for a in left_values
            for b in right_values
            if _identity(a) and _identity(b)
        ),
        default=0.0,
    )


def dynamic_referent_groups(
    graph: Mapping[str, Any],
    resolution: TopicResolution,
    srt_text: str,
    *,
    max_candidates: int = 8,
) -> list[ReferentGroup]:
    """Build narrow audio-verification groups only for names actually present.

    This avoids putting an entire franchise into every forced-choice request.
    A detected character is compared with its closest siblings from the
    resolved topic; every canonical surface is still verified from raw audio.
    """

    if resolution.status != "RESOLVED":
        return []
    entities = {row["entity_id"]: row for row in graph["entities"]}
    scoped = [entities[item] for item in resolution.scoped_entity_ids if item in entities]
    present: list[Mapping[str, Any]] = []
    for entity in scoped:
        if any(_surface_match(surface, srt_text) for surface in _entity_surfaces(entity)):
            present.append(entity)
    groups: list[ReferentGroup] = []
    covered: set[str] = set()
    for entity in present:
        entity_id = str(entity["entity_id"])
        if entity_id in covered:
            continue
        competitors = sorted(
            (other for other in scoped if other["entity_id"] != entity_id),
            key=lambda other: (-_phonetic_similarity(entity, other), other["canonical_zh"]),
        )
        selected = [entity, *competitors[: max(1, max_candidates - 1)]]
        # If another name is also present, let one common group own the cue so
        # chat_authority never sees duplicate overlapping groups.
        selected_ids = {str(row["entity_id"]) for row in selected}
        covered.update(selected_ids & {str(row["entity_id"]) for row in present})
        groups.append(
            ReferentGroup(
                tuple(
                    ReferentEntity(
                        str(row["canonical_zh"]),
                        _entity_surfaces(row),
                        tuple(str(value) for value in row["readings"]),
                    )
                    for row in selected
                ),
                reason=(
                    "Topic-scoped proper-name graph. Resolve the spoken entity from raw syllables; "
                    "after identity is resolved, preserve a correct spoken short Chinese name and use "
                    "the graph only to repair a wrong/non-Chinese surface."
                ),
                audio_verify_all_surfaces=True,
                # 图组的面全是正当别名(海铃/八幡海铃)不含误听面——多角色同句
                # 无可改写直接放行。
                alias_surfaces=True,
            )
        )
    return groups


def merge_referent_groups(
    static_groups: Iterable[ReferentGroup], dynamic_groups: Iterable[ReferentGroup]
) -> list[ReferentGroup]:
    dynamic = list(dynamic_groups)
    # The reviewed static list may use a short canonical (立希/祥子), while the
    # graph uses a full Chinese canonical (椎名立希/丰川祥子).  Canonical-only
    # deduplication would leave two groups matching the same SRT surface and
    # make chat_authority correctly fail with TRANSCRIPT_ENTITY_SLOT_AMBIGUOUS
    # before raw-audio verification is even called.  Shared normalized surface
    # identity means the dynamic, topic-scoped group supersedes the static one.
    dynamic_surfaces = {
        _identity(surface)
        for group in dynamic
        for entity in group.entities
        for surface in (entity.canonical, *entity.surfaces)
        if _identity(surface)
    }
    result = [
        group
        for group in static_groups
        if not (
            {
                _identity(surface)
                for entity in group.entities
                for surface in (entity.canonical, *entity.surfaces)
                if _identity(surface)
            }
            & dynamic_surfaces
        )
    ]
    result.extend(dynamic)
    return result
