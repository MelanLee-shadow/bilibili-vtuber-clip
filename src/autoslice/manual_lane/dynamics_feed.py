"""The first page of a Bilibili account's dynamics, read the way the dynamics crawler reads it (src/autoslice/streamer_dynamics:
the public opus feed per host_mid, the publication time from the dynamic id), without the crawler's snapshot and receipt
machinery, which the lane's import boundary keeps out.  Step 6 reads it on the media host when it asks who is in a call
(collab_guests)."""

from __future__ import annotations

import datetime as dt
import json
import unicodedata
import urllib.request

ROOM_INFO = "https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room}"
FEED = "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/feed/space?host_mid={uid}&page=1&type=all"
EPOCH = 1498838400          # streamer_dynamics.DYNAMIC_ID_EPOCH: a dynamic id's high bits count seconds from it
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/138.0.0.0 Safari/537.36")      # timely_term_crawler.BILIBILI_USER_AGENT
TIMEOUT_S = 15
MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 500


class FeedError(RuntimeError):
    """Bilibili answered with an error code, an unreadable body or an empty first page (its risk control)."""


def _get(url: str, referer: str = "") -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **({"Referer": referer} if referer else {})})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 - fixed https endpoints above
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise FeedError(f"{url}: answer over {MAX_BYTES} bytes")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise FeedError(f"{url}: not JSON") from exc
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise FeedError(f"{url}: code {payload.get('code') if isinstance(payload, dict) else '?'}")
    return payload


def room_uid(room_id: str) -> int:
    """The uid of the live room's owner."""
    data = _get(ROOM_INFO.format(room=int(room_id))).get("data")
    uid = int(data.get("uid") or 0) if isinstance(data, dict) else 0
    if uid <= 0:
        raise FeedError(f"room {room_id}: no uid")
    return uid


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in unicodedata.normalize("NFKC", value) if not unicodedata.category(ch).startswith("C"))
    return " ".join(text.split())[:MAX_TEXT]


def recent(uid: int) -> list[dict[str, str]]:
    """The account's latest dynamics that carry text: [{"published_at" (UTC, ISO), "text"}], newest first; a picture-only
    post has no text and is left out."""
    data = _get(FEED.format(uid=int(uid)), referer=f"https://space.bilibili.com/{int(uid)}/dynamic").get("data")
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise FeedError(f"uid {uid}: no item list")
    if not rows:            # an account that posts never has an empty first page: Bilibili's soft refusal
        raise FeedError(f"uid {uid}: empty first page (risk control)")
    out = []
    for row in rows:
        opus = str(row.get("opus_id") or "") if isinstance(row, dict) else ""
        text = _text(row.get("content")) if opus.isdigit() else ""
        if text:
            posted = dt.datetime.fromtimestamp((int(opus) >> 32) + EPOCH, dt.timezone.utc)
            out.append({"published_at": posted.isoformat(timespec="seconds"), "text": text})
    return out
