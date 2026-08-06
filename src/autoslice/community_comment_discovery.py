"""Bounded, privacy-preserving Bilibili comment discovery for community names."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import html
import json
import re
import unicodedata
import urllib.parse
from typing import Any, Mapping, Sequence

from src.autoslice.timely_term_crawler import (
    BILIBILI_USER_AGENT,
    BoundedHttpClient,
    CrawlError,
)


_HTML_RX = re.compile(r"<[^>]{1,256}>")


@dataclass(frozen=True)
class CommentPage:
    """Transient comment data. Clear commenter IDs must never be persisted."""

    comments: tuple[dict[str, Any], ...]
    stale: bool


def _plain_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(_HTML_RX.sub("", value))
    return " ".join(text.split())[:limit]


def _match_key(value: object) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", str(value)).casefold()
        if unicodedata.category(char)[0] in {"L", "N"}
    )


def _salted_hash(salt: str, namespace: str, value: object) -> str:
    material = f"{salt}\0{namespace}\0{value}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def fetch_comment_page(
    client: BoundedHttpClient,
    *,
    endpoint: str,
    video: Mapping[str, Any],
    limit: int,
) -> CommentPage:
    """Read one modern top-level reply page without retaining account display data."""

    url = endpoint + "?" + urllib.parse.urlencode(
        {
            "next": 0,
            "type": 1,
            "oid": int(video["aid"]),
            "mode": 3,
            "plat": 1,
        }
    )
    response = client.fetch(
        url,
        headers={"User-Agent": BILIBILI_USER_AGENT, "Referer": str(video["url"])},
    )
    try:
        payload = json.loads(response.body)
    except json.JSONDecodeError as exc:
        raise CrawlError("Bilibili comment discovery returned invalid JSON") from exc
    if payload.get("code") != 0:
        raise CrawlError(f"Bilibili comment discovery returned code {payload.get('code')}")
    replies = payload.get("data", {}).get("replies")
    if replies is None:
        replies = []
    if not isinstance(replies, list):
        raise CrawlError("Bilibili comment discovery returned invalid replies")

    comments: list[dict[str, Any]] = []
    for raw in replies[:limit]:
        if not isinstance(raw, Mapping):
            continue
        try:
            rpid = int(raw.get("rpid") or 0)
            commenter_mid = int(raw.get("member", {}).get("mid") or 0)
            ctime = int(raw.get("ctime") or 0)
            commented_at = dt.datetime.fromtimestamp(ctime, dt.timezone.utc)
        except (OSError, OverflowError, TypeError, ValueError):
            continue
        message = _plain_text(raw.get("content", {}).get("message"), limit=320)
        if rpid <= 0 or commenter_mid <= 0 or ctime <= 0 or not message:
            continue
        comments.append(
            {
                "comment_ref": hashlib.sha256(
                    f"{video['bvid']}\0{rpid}".encode("utf-8")
                ).hexdigest()[:20],
                "rpid": rpid,
                "commenter_mid": commenter_mid,
                "commented_at": commented_at.isoformat(timespec="seconds"),
                "message": message,
            }
        )
    return CommentPage(tuple(comments), bool(response.stale))


def prompt_comments(comments: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Expose only an opaque reference and untrusted message to the judge."""

    return [
        {"comment_ref": str(row["comment_ref"]), "message": str(row["message"])}
        for row in comments
    ]


def comment_witnesses(
    comments: Sequence[Mapping[str, Any]],
    *,
    surface: str,
    privacy_salt: str,
    official_upload_for_target: bool,
    target_entity_count: int,
    target_surfaces: Sequence[object],
) -> list[dict[str, Any]]:
    """Convert exact comment usage into durable, deduplicated hashed witnesses."""

    target_keys = {_match_key(value) for value in target_surfaces if _match_key(value)}
    witnesses: list[dict[str, Any]] = []
    seen_commenters: set[str] = set()
    for row in comments:
        message = str(row.get("message") or "")
        if surface not in message:
            continue
        if target_entity_count > 1:
            message_key = _match_key(message)
            if not any(key in message_key for key in target_keys):
                continue
        commenter_key = _salted_hash(
            privacy_salt, "commenter", int(row["commenter_mid"])
        )
        if commenter_key in seen_commenters:
            continue
        seen_commenters.add(commenter_key)
        witnesses.append(
            {
                "comment_key_sha256": _salted_hash(
                    privacy_salt, "comment", int(row["rpid"])
                ),
                "commenter_key_sha256": commenter_key,
                "commented_at": str(row["commented_at"]),
                "official_upload_for_target": bool(official_upload_for_target),
                "raw_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            }
        )
    return witnesses


def select_comment_targets(
    bundles: Sequence[Mapping[str, Any]],
    *,
    member_crawl: Mapping[str, Mapping[str, Any]],
    cursor: int,
    limit: int,
) -> tuple[list[tuple[Mapping[str, Any], dict[str, Any]]], int]:
    """Round-robin members, then revisit the least-recently checked safe video."""

    available = [bundle for bundle in bundles if bundle.get("rows")]
    if not available or limit <= 0:
        return [], cursor
    available.sort(key=lambda item: str(item["member"]["entity_id"]))
    start = cursor % len(available)
    ordered = [available[(start + offset) % len(available)] for offset in range(len(available))]
    selected: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    used_bvids: set[str] = set()
    used_for_entity: dict[str, set[str]] = {}
    while len(selected) < limit:
        progressed = False
        for bundle in ordered:
            member = bundle["member"]
            entity_id = str(member["entity_id"])
            history = member_crawl.get(entity_id, {}).get("comment_video_history", {})
            if not isinstance(history, Mapping):
                history = {}
            already = used_for_entity.setdefault(entity_id, set())
            eligible = [
                row
                for row in bundle["rows"]
                if str(row["bvid"]) not in used_bvids
                and str(row["bvid"]) not in already
                and (
                    int(row.get("target_entity_count", 1)) == 1
                    or bool(row.get("official_upload_for_target"))
                )
            ]
            if not eligible:
                continue
            eligible.sort(
                key=lambda row: (str(row.get("published_at", "")), str(row["bvid"])),
                reverse=True,
            )
            eligible.sort(
                key=lambda row: (
                    str(history.get(str(row["bvid"]), "")),
                    not bool(row.get("official_upload_for_target")),
                )
            )
            row = eligible[0]
            selected.append((member, row))
            used_bvids.add(str(row["bvid"]))
            already.add(str(row["bvid"]))
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected, (start + min(len(available), limit)) % len(available)
