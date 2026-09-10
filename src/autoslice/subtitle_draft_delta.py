"""Strict sparse CPA draft output: complete review, immutable source grid.

This only decodes an explicitly selected experimental response format. It does
not grant mutation authority, discard cues, alter times, or replace final review.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from src.autoslice.llm_client import extract_json_object

SCHEMA = "cpa-draft-delta.v1"


def parse_draft_delta(response: str, originals: Sequence[str]) -> tuple[dict[int, str], list[dict]]:
    value = extract_json_object(response)
    count = len(originals)
    if (
        value.get("schema_version") != SCHEMA
        or value.get("review_complete") is not True
        or type(value.get("reviewed_cue_count")) is not int
        or value["reviewed_cue_count"] != count
    ):
        raise ValueError("CPA_DELTA_REVIEW_INCOMPLETE")
    rows = value.get("edits")
    doubts = value.get("needs_audio")
    if not isinstance(rows, list) or not isinstance(doubts, list):
        raise ValueError("CPA_DELTA_INVALID_COLLECTION")
    texts = {n: text for n, text in enumerate(originals, 1)}
    seen = set()
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"n", "before", "after"}
            or type(row["n"]) is not int
            or not 1 <= row["n"] <= count
            or row["n"] in seen
            or row["before"] != originals[row["n"] - 1]
            or not isinstance(row["after"], str)
            or not row["after"].strip()
            or "\n" in row["after"]
            or "\r" in row["after"]
        ):
            raise ValueError("CPA_DELTA_EDIT_UNBOUND")
        seen.add(row["n"])
        texts[row["n"]] = row["after"]
    seen_doubts = set()
    for row in doubts:
        if (
            not isinstance(row, dict)
            or set(row) != {"n", "priority", "reason_code"}
            or type(row["n"]) is not int
            or not 1 <= row["n"] <= count
            or row["n"] in seen_doubts
            or type(row["priority"]) is not int
            or row["priority"] not in (1, 2, 3)
            or row["reason_code"] not in ("NAME", "LANGUAGE", "NUMBER_NEGATION", "MISSING", "OTHER")
        ):
            raise ValueError("CPA_DELTA_DOUBT_INVALID")
        seen_doubts.add(row["n"])
    return texts, doubts
