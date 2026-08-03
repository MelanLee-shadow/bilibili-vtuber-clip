"""Strict, reusable release validation for SRT sidecars.

The production parser is intentionally convenient for transcription inputs and
may skip malformed blocks. Release validation must do the opposite: every
non-empty block is consumed or the artifact fails closed.
"""

from __future__ import annotations

import re
from typing import Any


SRT_RELEASE_POLICY_SCHEMA = "lidousha-srt-release-policy.v1"
_TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})$")
_TIMING_RE = re.compile(r"^(.+?)\s+-->\s+(.+?)$")
_PUNCT_ONLY_RE = re.compile(r"^[，。！？、,.!?…]+$")
_LEADING_PUNCT_RE = re.compile(r"^[，。！？、,.!?…]")
_CJK_SINGLE_RE = re.compile(r"^([\u3400-\u9fff])[，。！？、,.!?…]*$")
# Single-hanzi cues are usually ASR shatter (a content word cut in half), but
# a narrow closed class legitimately stands alone as a complete utterance.
# This includes interjections ("\u54ce\u2014\u2014", "\u554a?") plus "\u6709" as a self-contained
# affirmative answer.  Do not broaden this to generic predicates such as
# "\u884c"/"\u597d": those are still merged into a contiguous neighbour or rejected.
_CJK_SINGLE_STANDALONE_UTTERANCES = frozenset(
    "\u554a\u54ce\u5509\u54e6\u5662\u5594\u55ef\u8bf6\u6b38\u54a6\u5440\u54c7\u563f\u54c8\u5475\u54fc\u5582\u54b3\u55ec\u56af\u54df\u55f7\u545c\u5443\u5466\u561e\u561b\u54af"
) | {"\u6709"}


def _parse_time_ms(value: str) -> int:
    match = _TIME_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(value)
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    if minutes > 59 or seconds > 59:
        raise ValueError(value)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw_line in str(text or "").lstrip("\ufeff").splitlines():
        line = raw_line.rstrip()
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def validate_srt_text(
    text: str,
    *,
    media_duration_ms: int | None = None,
    min_cue_ms: int = 300,
    media_tail_tolerance_ms: int = 100,
) -> dict[str, Any]:
    """Return a deterministic release verdict without silently skipping blocks."""

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    parsed: list[dict[str, Any]] = []
    single_cjk_blocks: list[tuple[int, str]] = []
    blocks = _blocks(text)
    if not blocks:
        errors.append({"code": "SRT_ZERO_CUES", "block": None})
    previous_start = -1
    previous_end = -1
    for block_number, block in enumerate(blocks, start=1):
        if len(block) < 3:
            errors.append(
                {"code": "SRT_BLOCK_TOO_SHORT", "block": block_number}
            )
            continue
        try:
            cue_index = int(block[0].strip())
        except ValueError:
            errors.append(
                {"code": "SRT_CUE_INDEX_INVALID", "block": block_number}
            )
            continue
        if cue_index != block_number:
            errors.append(
                {
                    "code": "SRT_CUE_INDEX_NON_CONSECUTIVE",
                    "block": block_number,
                    "actual": cue_index,
                }
            )
        timing = _TIMING_RE.fullmatch(block[1].strip())
        if timing is None:
            errors.append({"code": "SRT_TIMING_INVALID", "block": block_number})
            continue
        try:
            start_ms = _parse_time_ms(timing.group(1))
            end_ms = _parse_time_ms(timing.group(2))
        except ValueError:
            errors.append(
                {"code": "SRT_TIMESTAMP_INVALID", "block": block_number}
            )
            continue
        if end_ms <= start_ms:
            errors.append(
                {"code": "SRT_DURATION_NON_POSITIVE", "block": block_number}
            )
        elif end_ms - start_ms < min_cue_ms:
            errors.append(
                {
                    "code": "SRT_CUE_TOO_SHORT",
                    "block": block_number,
                    "duration_ms": end_ms - start_ms,
                }
            )
        if start_ms < previous_start:
            errors.append(
                {"code": "SRT_START_ORDER_INVALID", "block": block_number}
            )
        if previous_end >= 0 and start_ms < previous_end:
            errors.append({"code": "SRT_CUE_OVERLAP", "block": block_number})
        previous_start = start_ms
        previous_end = end_ms
        if media_duration_ms is not None:
            if start_ms >= media_duration_ms:
                errors.append(
                    {"code": "SRT_START_AFTER_MEDIA", "block": block_number}
                )
            if end_ms > media_duration_ms + media_tail_tolerance_ms:
                errors.append(
                    {"code": "SRT_END_AFTER_MEDIA", "block": block_number}
                )
        text_lines = [line.strip() for line in block[2:]]
        for text_line in text_lines:
            if not text_line:
                errors.append(
                    {"code": "SRT_TEXT_EMPTY", "block": block_number}
                )
            elif _PUNCT_ONLY_RE.fullmatch(text_line):
                errors.append(
                    {"code": "SRT_TEXT_PUNCTUATION_ONLY", "block": block_number}
                )
            elif _LEADING_PUNCT_RE.match(text_line):
                errors.append(
                    {"code": "SRT_TEXT_LEADING_PUNCTUATION", "block": block_number}
                )
            single_cjk_match = _CJK_SINGLE_RE.fullmatch(text_line)
            if (
                single_cjk_match
                and single_cjk_match.group(1)
                not in _CJK_SINGLE_STANDALONE_UTTERANCES
            ):
                # Terminal punctuation does not turn a shattered content
                # character into a complete cue.  Keep the bare character for
                # the adjacent name-echo exemption below.
                single_cjk_blocks.append(
                    (block_number, single_cjk_match.group(1))
                )
        parsed.append(
            {
                "index": cue_index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": "\n".join(text_lines),
            }
        )
    # Name-echo exemption (秦秦/秦 case): a lone CJK character is a
    # real utterance when the SAME character appears inside a >=2-char run of
    # an adjacent cue (呼名回声/结巴 onset). Isolated content-word fragments
    # keep failing closed.
    index_by_block = {
        cue["index"]: position for position, cue in enumerate(parsed)
    }
    for block_number, char in single_cjk_blocks:
        position = index_by_block.get(block_number)
        echoed = False
        if position is not None:
            for neighbor in (position - 1, position + 1):
                if 0 <= neighbor < len(parsed):
                    neighbor_text = parsed[neighbor]["text"]
                    if char in neighbor_text and len(
                        neighbor_text.strip()
                    ) >= 2:
                        echoed = True
        if not echoed:
            errors.append(
                {"code": "SRT_SINGLE_CJK_CHARACTER", "block": block_number}
            )
    return {
        "schema_version": SRT_RELEASE_POLICY_SCHEMA,
        "status": "PASS" if not errors else "FAIL",
        "cue_count": len(parsed),
        "block_count": len(blocks),
        "errors": errors,
        "warnings": warnings,
    }


def validate_srt_file(path, **kwargs: Any) -> dict[str, Any]:
    """Read one UTF-8 SRT path and return the shared release verdict."""

    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return {
            "schema_version": SRT_RELEASE_POLICY_SCHEMA,
            "status": "FAIL",
            "cue_count": 0,
            "block_count": 0,
            "errors": [
                {
                    "code": "SRT_FILE_UNREADABLE",
                    "block": None,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            ],
            "warnings": [],
        }
    return validate_srt_text(text, **kwargs)
