"""Schema validation and bounded loading of the topic/entity graph asset.

Split out of topic_entity_graph so the lane loads the graph without the chat authority that the resolver half of
that module needs; topic_entity_graph imports the names back."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from src.autoslice.topic_entity_graph_fields import (
    GRAPH_GENERATOR,
    GRAPH_SCHEMA,
    MAX_ENTITIES,
    MAX_GRAPH_BYTES,
    MAX_TOPICS,
    MAX_WORKS,
    TopicEntityGraphError,
    _atom,
    _atom_list,
    _date,
    _id_list,
    _identifier,
    _sources,
    _timestamp,
)


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
