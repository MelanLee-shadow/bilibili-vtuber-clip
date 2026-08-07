"""Streamer-dynamics theme-hint lane: bounded 动态 crawler + session receipt.

Not every stream is a game; the owner's own Bilibili dynamics (动态) may name
the session's real theme when static context (game glossary/roster) is blind.
This module resolves the channel owner's uid from the profile's live
room_id, crawls a bounded window of recent dynamics text via Bilibili's
signed WBI web-dynamic feed, and associates dynamics published near a
recording date into a per-session receipt.

Theme hints are candidates only: a hint's existence never proves a cue's
occurrence and carries no mechanical mutation authority (sibling lane to
``src.autoslice.game_context``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from src.autoslice.bilibili_wbi import signed_params as bilibili_wbi_signed_params
from src.autoslice.community_comment_discovery import fetch_wbi_mixin_key
from src.autoslice.runtime_candidate_asset import bind_runtime_candidate_asset
from src.autoslice.timely_term_crawler import (
    BILIBILI_USER_AGENT,
    BILIBILI_WBI_NAV_ENDPOINT,
    BoundedHttpClient,
    CrawlError,
)


SNAPSHOT_SCHEMA = "vtuber-slice.streamer-dynamics.v1"
RECEIPT_SCHEMA = "session-theme-hints.v1"
OCCURRENCE_POLICY = "THEME_HINT_NOT_CUE_OCCURRENCE_OR_MUTATION_AUTHORITY"
ENV_SUFFIX = "SESSION_THEME_HINTS"
ROOM_INFO_ENDPOINT = "https://api.live.bilibili.com/room/v1/Room/get_info"
DYNAMIC_FEED_ENDPOINT = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
ALLOWED_HOSTS = frozenset({"api.live.bilibili.com", "api.bilibili.com"})
MAX_ITEMS = 32
MAX_TEXT_CHARS = 500
SNAPSHOT_TTL = dt.timedelta(hours=48)
LOOKBACK_DAYS = 2
LOOKAHEAD_DAYS = 1

_STATUSES = frozenset({"fresh"})
_HINT_STATUSES = frozenset({"HINTS", "NO_HINTS"})
_INSTRUCTION_RX = re.compile(r"(?i)ignore |system prompt")
_ROOM_ID_RX = re.compile(r"^[0-9]{1,32}$")
_DYNAMIC_ID_RX = re.compile(r"^[0-9]{1,32}$")


class StreamerDynamicsError(ValueError):
    pass


def _parse_time(value: object) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise StreamerDynamicsError(f"not an ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise StreamerDynamicsError("timestamp must be timezone-aware")
    return parsed


def _clean_text(value: object, *, max_chars: int) -> str | None:
    """Strip control characters and reject instruction-shaped free text.

    Unlike the strict term-atom sanitizers elsewhere in the pipeline, dynamic
    prose is kept (not rejected) when it merely contains punctuation/emoji —
    only literal instruction-shaped phrasing and control characters are
    removed, matching the task's "sanitize, don't discard" free-text rule.
    """

    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value)
    if _INSTRUCTION_RX.search(text.casefold()):
        return None
    cleaned = "".join(
        char for char in text if not unicodedata.category(char).startswith("C")
    )
    cleaned = " ".join(cleaned.split()).strip()
    if not cleaned:
        return None
    return cleaned[:max_chars]


def validate_dynamics_snapshot(payload: object, *, as_of: dt.datetime | None = None) -> dict[str, Any]:
    """Strictly validate a crawled streamer-dynamics snapshot."""

    required = {
        "schema_version",
        "generated_at",
        "expires_at",
        "status",
        "uid",
        "room_id",
        "occurrence_policy",
        "items",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise StreamerDynamicsError("streamer dynamics snapshot has invalid fields")
    if payload.get("schema_version") != SNAPSHOT_SCHEMA:
        raise StreamerDynamicsError("unsupported streamer dynamics snapshot schema")
    if payload.get("occurrence_policy") != OCCURRENCE_POLICY:
        raise StreamerDynamicsError("streamer dynamics occurrence-neutral policy missing")
    if payload.get("status") not in _STATUSES:
        raise StreamerDynamicsError("streamer dynamics snapshot status is invalid")
    uid = payload.get("uid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise StreamerDynamicsError("streamer dynamics uid is invalid")
    room_id = str(payload.get("room_id") or "")
    if not _ROOM_ID_RX.fullmatch(room_id):
        raise StreamerDynamicsError("streamer dynamics room_id is invalid")
    generated_at = _parse_time(payload["generated_at"])
    expires_at = _parse_time(payload["expires_at"])
    if expires_at <= generated_at:
        raise StreamerDynamicsError("streamer dynamics expiry must be after generation")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise StreamerDynamicsError("streamer dynamics items are invalid")
    normalized_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, Mapping) or set(item) != {
            "dynamic_id", "published_at", "text",
        }:
            raise StreamerDynamicsError(f"streamer dynamics item {index} has invalid fields")
        dynamic_id = str(item["dynamic_id"])
        if not _DYNAMIC_ID_RX.fullmatch(dynamic_id) or dynamic_id in seen_ids:
            raise StreamerDynamicsError(f"streamer dynamics item {index} dynamic_id is invalid")
        seen_ids.add(dynamic_id)
        published_at = _parse_time(item["published_at"])
        text = item.get("text")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS:
            raise StreamerDynamicsError(f"streamer dynamics item {index} text is invalid")
        if _INSTRUCTION_RX.search(text.casefold()):
            raise StreamerDynamicsError(f"streamer dynamics item {index} text is instruction-shaped")
        if any(unicodedata.category(char).startswith("C") for char in text):
            raise StreamerDynamicsError(f"streamer dynamics item {index} text contains control characters")
        normalized_items.append(
            {
                "dynamic_id": dynamic_id,
                "published_at": published_at.isoformat(timespec="seconds"),
                "text": text,
            }
        )
    if as_of is not None and as_of > expires_at:
        raise StreamerDynamicsError("streamer dynamics snapshot is expired")
    result = dict(payload)
    result["items"] = normalized_items
    return result


def snapshot_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def resolve_uid(client: BoundedHttpClient, *, room_id: str) -> int:
    """Resolve the channel owner's Bilibili uid from their live room id."""

    response = client.fetch(
        f"{ROOM_INFO_ENDPOINT}?room_id={room_id}",
        headers={"User-Agent": BILIBILI_USER_AGENT},
    )
    try:
        payload = json.loads(response.body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CrawlError("Bilibili room info returned invalid JSON") from exc
    if not isinstance(payload, Mapping) or payload.get("code") != 0:
        raise CrawlError(f"Bilibili room info returned code {payload.get('code') if isinstance(payload, Mapping) else '?'}")
    data = payload.get("data")
    uid = int(data.get("uid") or 0) if isinstance(data, Mapping) else 0
    if uid <= 0:
        raise CrawlError("Bilibili room info returned an invalid uid")
    return uid


def _dynamic_items(payload: Mapping[str, Any], *, uid: int) -> tuple[list[dict[str, Any]], int]:
    if payload.get("code") != 0:
        raise CrawlError(f"Bilibili dynamic feed returned code {payload.get('code')}")
    data = payload.get("data")
    rows = data.get("items") if isinstance(data, Mapping) else None
    if not isinstance(rows, list):
        raise CrawlError("Bilibili dynamic feed returned an invalid item list")
    items: list[dict[str, Any]] = []
    skipped = 0
    for row in rows[:MAX_ITEMS]:
        parsed, malformed = _one_dynamic_item(row, uid=uid)
        if malformed:
            skipped += 1
        if parsed is not None:
            items.append(parsed)
    return items, skipped


def _one_dynamic_item(row: object, *, uid: int) -> tuple[dict[str, Any] | None, bool]:
    """Return (item-or-None, malformed).

    ``malformed`` only counts structurally broken rows (bad id/modules/pub_ts):
    a live feed legitimately mixes text-less posts (images/video/forwards)
    and other people's mid — those are normal filtering, not crawl failures,
    and must not turn a healthy run into a false-positive partial exit.
    """

    if not isinstance(row, Mapping):
        return None, True
    dynamic_id = str(row.get("id_str") or "")
    modules = row.get("modules")
    if not _DYNAMIC_ID_RX.fullmatch(dynamic_id) or not isinstance(modules, Mapping):
        return None, True
    author = modules.get("module_author")
    dynamic = modules.get("module_dynamic")
    if not isinstance(author, Mapping) or not isinstance(dynamic, Mapping):
        return None, True
    try:
        published_at = dt.datetime.fromtimestamp(int(author.get("pub_ts") or 0), dt.timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return None, True
    if int(author.get("mid") or 0) != uid:
        return None, False
    desc = dynamic.get("desc")
    raw_text = desc.get("text") if isinstance(desc, Mapping) else None
    text = _clean_text(raw_text, max_chars=MAX_TEXT_CHARS)
    if not text:
        return None, False
    return {
        "dynamic_id": dynamic_id,
        "published_at": published_at.isoformat(timespec="seconds"),
        "text": text,
    }, False


def fetch_recent_dynamics(
    client: BoundedHttpClient, *, uid: int, mixin_key: str, now: dt.datetime
) -> tuple[list[dict[str, Any]], int]:
    params = bilibili_wbi_signed_params({"host_mid": uid}, mixin_key=mixin_key, signed_at=now)
    url = DYNAMIC_FEED_ENDPOINT + "?" + urllib.parse.urlencode(params)
    response = client.fetch(
        url,
        headers={
            "User-Agent": BILIBILI_USER_AGENT,
            "Referer": f"https://space.bilibili.com/{uid}/dynamic",
        },
    )
    try:
        payload = json.loads(response.body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CrawlError("Bilibili dynamic feed returned invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise CrawlError("Bilibili dynamic feed returned invalid JSON")
    return _dynamic_items(payload, uid=uid)


def build_snapshot(
    client: BoundedHttpClient, *, room_id: str, now: dt.datetime
) -> tuple[dict[str, Any], int]:
    """Run the bounded 3-call crawl (room info, WBI nav, dynamic feed)."""

    if now.tzinfo is None:
        raise StreamerDynamicsError("build_snapshot now must be timezone-aware")
    if not _ROOM_ID_RX.fullmatch(str(room_id)):
        raise StreamerDynamicsError("invalid room_id")
    uid = resolve_uid(client, room_id=str(room_id))
    mixin_key, _fresh = fetch_wbi_mixin_key(client, endpoint=BILIBILI_WBI_NAV_ENDPOINT)
    items, skipped = fetch_recent_dynamics(client, uid=uid, mixin_key=mixin_key, now=now)
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA,
        "generated_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + SNAPSHOT_TTL).isoformat(timespec="seconds"),
        "status": "fresh",
        "uid": uid,
        "room_id": str(room_id),
        "occurrence_policy": OCCURRENCE_POLICY,
        "items": items,
    }
    return validate_dynamics_snapshot(snapshot), skipped


def session_theme_hints(snapshot: Mapping[str, Any], *, recording_date: str) -> dict[str, Any]:
    """Associate dynamics published within [date-2d, date+1d] to one session."""

    date = dt.date.fromisoformat(str(recording_date)[:10])
    window_start = dt.datetime.combine(
        date - dt.timedelta(days=LOOKBACK_DAYS), dt.time.min, dt.timezone.utc
    )
    window_end = dt.datetime.combine(
        date + dt.timedelta(days=LOOKAHEAD_DAYS), dt.time.max, dt.timezone.utc
    )
    hints = [
        dict(item)
        for item in snapshot.get("items", [])
        if window_start <= _parse_time(item["published_at"]) <= window_end
    ]
    return {
        "schema_version": RECEIPT_SCHEMA,
        "occurrence_policy": OCCURRENCE_POLICY,
        "recording_date": str(recording_date),
        "status": "HINTS" if hints else "NO_HINTS",
        "hints": hints,
    }


def validate_session_theme_hints(payload: object) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or payload.get("schema_version") != RECEIPT_SCHEMA:
        raise StreamerDynamicsError("unsupported session theme hints schema")
    if payload.get("occurrence_policy") != OCCURRENCE_POLICY:
        raise StreamerDynamicsError("session theme hints occurrence-neutral policy missing")
    status = payload.get("status")
    if status not in _HINT_STATUSES:
        raise StreamerDynamicsError("session theme hints status invalid")
    hints = payload.get("hints")
    if not isinstance(hints, list) or len(hints) > MAX_ITEMS:
        raise StreamerDynamicsError("session theme hints list is invalid")
    if (status == "HINTS") != bool(hints):
        raise StreamerDynamicsError("session theme hints status/hints mismatch")
    for index, hint in enumerate(hints):
        if not isinstance(hint, Mapping) or set(hint) != {"dynamic_id", "published_at", "text"}:
            raise StreamerDynamicsError(f"session theme hint {index} has invalid fields")
        text = hint.get("text")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS:
            raise StreamerDynamicsError(f"session theme hint {index} text is invalid")
        if _INSTRUCTION_RX.search(text.casefold()):
            raise StreamerDynamicsError(f"session theme hint {index} text is instruction-shaped")
        if any(unicodedata.category(char).startswith("C") for char in text):
            raise StreamerDynamicsError(f"session theme hint {index} text contains control characters")
        _parse_time(hint["published_at"])
    return dict(payload)


def render_theme_hints_context(receipt: Mapping[str, Any]) -> str:
    """Render associated theme hints as an occurrence-neutral prompt block."""

    if receipt.get("status") != "HINTS":
        return ""
    lines = [
        "主播动态主题提示（近期 B 站动态；只是主题提示候选，绝不证明本句出现，"
        "没有机械改字权限；逐处仍须本句音频、结构化弹幕/SC 与语境仲裁）:",
    ]
    for hint in receipt["hints"]:
        lines.append(f"- {hint['text']}")
    return "\n".join(lines) + "\n"


def compute_session_theme_hints_state(
    *, recording_date: str, snapshot_path: Path, state_path: Path,
) -> Path | None:
    """Compute (or reuse) the per-session theme-hints receipt, fail-open."""

    try:
        snapshot = validate_dynamics_snapshot(json.loads(snapshot_path.read_bytes()))
    except (OSError, ValueError):
        return state_path if state_path.is_file() else None
    receipt = session_theme_hints(snapshot, recording_date=recording_date)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = state_path.with_name(state_path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, state_path)
    except OSError:
        return state_path if state_path.is_file() else None
    return state_path


def bind_session_theme_hints(
    env: MutableMapping[str, str],
    *,
    recording_date: str,
    snapshot_path: Path,
    state_root: Path,
    truth_mode: str,
    selectors: Mapping[str, str],
) -> Path | None:
    """Ensure and env-bind the session theme hints for one recording date."""

    safe_date = re.sub(r"[^0-9-]", "", str(recording_date))[:10]
    if not safe_date:
        return None
    state_path = state_root / "session_theme_hints" / f"{safe_date}.json"
    if truth_mode != "withheld" and not selectors.get(f"AUTOSLICE_{ENV_SUFFIX}"):
        compute_session_theme_hints_state(
            recording_date=safe_date,
            snapshot_path=snapshot_path,
            state_path=state_path,
        )
    return bind_runtime_candidate_asset(
        env,
        selectors=selectors,
        truth_mode=truth_mode,
        env_suffix=ENV_SUFFIX,
        runtime_path=state_path,
        committed_path=state_path,
        digest_file=lambda path: hashlib.sha256(path.read_bytes()).hexdigest(),
    )
