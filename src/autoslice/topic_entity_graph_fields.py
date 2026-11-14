"""Field-level checks and limits of the topic/entity graph asset: identities, atoms, dates and sources.

Split out of topic_entity_graph (with topic_entity_graph_load) so the lane validates and loads the graph without the
chat authority that the resolver half of that module needs; topic_entity_graph imports the names back."""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
import urllib.parse

GRAPH_SCHEMA = "lidousha-topic-entity-graph.v1"

GRAPH_GENERATOR = "scripts/crawl_topic_entity_graph.py"

MAX_GRAPH_BYTES = 4 * 1024 * 1024

MAX_TOPICS = 96

MAX_WORKS = 384

MAX_ENTITIES = 4096

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


class TopicEntityGraphError(ValueError):
    """A graph or resolution artifact is unsafe or internally inconsistent."""


def _identity(value: object) -> str:
    return _NON_IDENTITY.sub("", unicodedata.normalize("NFKC", str(value))).casefold()


def dedupe_key(value: str) -> str:
    """Key used only to drop duplicate surfaces; matching keeps using ``_identity``.

    ``_identity`` keeps just Latin, kana and CJK, so a real surface written in
    another script (Hangul, Cyrillic, ...) would have an empty identity and be
    silently erased.  Such a value keeps its case-folded text as its own key;
    values without any letter stay unkeyed and are dropped as before.
    """

    key = _identity(value)
    if key or not any(ch.isalpha() for ch in value):
        return key
    return unicodedata.normalize("NFKC", value).casefold()


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
        key = dedupe_key(item)
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
