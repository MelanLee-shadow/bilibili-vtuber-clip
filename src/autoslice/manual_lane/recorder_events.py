"""Recorder-native SC/gift/guard evidence; sender identity never implies spoken text."""

from __future__ import annotations

import hashlib
import math
from typing import NotRequired, TypedDict
import xml.etree.ElementTree as ET

from ..chat_context_horizons import GIFT_PRE_CONTEXT_MS, GUARD_PRE_CONTEXT_MS, SC_PRE_CONTEXT_MS


class Danmaku(TypedDict):
    t_s: float
    text: str
    type: NotRequired[str]
    sender: NotRequired[str]
    source_sha256: NotRequired[str]
    source_event_id: NotRequired[str]
    gift_name: NotRequired[str]
    count: NotRequired[int]
    guard_level: NotRequired[int]
    p_rest: NotRequired[str]    # ordinary comment: the XML `p` attribute after the send time (session_events)
    raw: NotRequired[str]       # ordinary comment: the XML text as written, still escaped


def event_lookback_s(kind: str) -> float:
    return {"sc": SC_PRE_CONTEXT_MS, "gift": GIFT_PRE_CONTEXT_MS,
            "guard": GUARD_PRE_CONTEXT_MS}[kind] / 1000


def recorder_events(payload: bytes) -> list[Danmaku]:
    """Read the recorder's relative ``ts`` clock, without consuming opaque raw payloads."""
    source_sha = hashlib.sha256(payload).hexdigest()
    rows: list[Danmaku] = []
    for ordinal, node in enumerate(ET.fromstring(payload).iter()):
        if node.tag not in {"sc", "gift", "guard"}:
            continue
        sent = float(node.attrib["ts"])
        if not math.isfinite(sent):
            raise ValueError(f"non-finite {node.tag} timestamp")
        row: Danmaku = {"t_s": sent, "text": (node.text or "").strip(),
                              "type": node.tag, "sender": node.get("user", ""),
                              "source_sha256": source_sha,
                              "source_event_id": f"xml-{node.tag}-{source_sha}-{ordinal}"}
        if node.tag == "gift":
            row["gift_name"] = node.attrib["giftname"]
            row["count"] = int(node.attrib["giftcount"])
        elif node.tag == "guard":
            level = int(node.attrib["level"])
            row["gift_name"] = {1: "总督", 2: "提督", 3: "舰长"}[level]
            row["count"] = int(node.attrib["count"])
            row["guard_level"] = level
        rows.append(row)
    return rows


def event_xml(row: Danmaku, sent: float) -> str:
    attrs = {"ts": f"{sent:.3f}", "user": row["sender"]}
    if row["type"] == "gift":
        attrs.update(giftname=row["gift_name"], giftcount=str(row["count"]))
    elif row["type"] == "guard":
        attrs.update(level=str(row["guard_level"]), count=str(row["count"]))
    node = ET.Element(row["type"], attrs)
    node.text = row["text"]
    return ET.tostring(node, encoding="unicode")


def evidence_texts(row: Danmaku) -> list[str]:
    """Written spellings, with usernames kept separate from message bodies."""
    if row.get("type") is None:
        return [row["text"]] if row["text"] else []
    return [text for text in (row.get("sender"), row["text"], row.get("gift_name")) if text]


def evidence_view(row: Danmaku) -> str:
    if row.get("type") is None:
        return str(row["text"])
    kind = str(row["type"])
    body = f"正文：{row['text']}" if kind == "sc" else f"礼物：{row['gift_name']} × {row['count']}"
    return f"[{kind}] 发送者：{row['sender']}；{body}"
