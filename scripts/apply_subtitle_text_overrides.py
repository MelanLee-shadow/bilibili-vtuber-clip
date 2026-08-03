#!/usr/bin/env python3
"""Apply hash-bound human text decisions before speaker separation.

The automatic ASR/AGY/CPA/pronoun chain remains the default text authority.
When a human resolves a genuinely tricky cue, this stage records that decision
as data and applies it *before* any speaker inference or subtitle rendering.
It never edits timestamps and never burns media.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.autoslice.chat_authority import (
    canonicalize_hard_meme_surfaces,
    canonicalize_japanese_native_script_surfaces,
)


SRT_BLOCK_RE = re.compile(
    r"(?ms)^\s*(\d+)\s*\n"
    r"(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*\n"
    r"(.*?)(?=\n{2,}|\Z)"
)
PUNCTUATION_INSENSITIVE_TEXT_RE = re.compile(
    r"""[\s,，、。.!！?？:：;；"'“”‘’()（）《》〈〉【】\[\]…—-]+"""
)


@dataclass(frozen=True)
class TextCue:
    source_index: int
    start: str
    end: str
    text: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parse_srt(path: Path) -> list[TextCue]:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    cues = [
        TextCue(
            source_index=int(match.group(1)),
            start=match.group(2),
            end=match.group(3),
            text=" ".join(line.strip() for line in match.group(4).strip().splitlines()),
        )
        for match in SRT_BLOCK_RE.finditer(text)
    ]
    if not cues:
        raise ValueError(f"no SRT cues found in {path}")
    if [cue.source_index for cue in cues] != list(range(1, len(cues) + 1)):
        raise ValueError("source SRT indices must be contiguous from 1")
    return cues


def _expected_text_matches(
    actual: str,
    expected: dict[str, Any],
) -> bool:
    alternatives = expected.get("text_alternatives", [])
    if alternatives is None:
        alternatives = []
    if not isinstance(alternatives, list) or not all(
        isinstance(value, str) for value in alternatives
    ):
        raise ValueError("text_alternatives must be a string list")
    allowed = [expected.get("text"), *alternatives]
    if actual in allowed:
        return True
    mode = str(expected.get("text_match_mode") or "exact")
    if mode == "punctuation_insensitive":
        normalized_actual = PUNCTUATION_INSENSITIVE_TEXT_RE.sub("", actual)
        return any(
            isinstance(value, str)
            and PUNCTUATION_INSENSITIVE_TEXT_RE.sub("", value) == normalized_actual
            for value in allowed
        )
    if mode != "exact":
        raise ValueError(f"unsupported text_match_mode {mode!r}")
    return False


def _srt_clock_ms(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value)
    if match is None:
        raise ValueError(f"invalid SRT timestamp: {value!r}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _shift_srt_clock(value: str, offset_ms: int) -> str:
    shifted_ms = _srt_clock_ms(value) - offset_ms
    if shifted_ms < 0:
        raise ValueError(
            f"timeline override timestamp {value!r} precedes "
            f"the recut offset {offset_ms}ms"
        )
    hours, remainder = divmod(shifted_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _timeline_value(value: Any, timeline_offset_ms: int) -> str:
    raw = str(value or "")
    return _shift_srt_clock(raw, timeline_offset_ms) if timeline_offset_ms else raw


def _expect(
    cue: TextCue,
    override: dict[str, Any],
    *,
    timeline_offset_ms: int = 0,
) -> None:
    action = str(override.get("action", "replace"))
    if action in {"replace_substring", "replace_pattern"}:
        locator = override.get("locator")
        replacement = str(override.get("text", ""))
        if (
            not isinstance(locator, dict)
            or not str(locator.get("start") or "")
            or not str(locator.get("end") or "")
            or not replacement
        ):
            raise ValueError(
                f"localized override for cue {cue.source_index} has an invalid locator or text"
            )
        if action == "replace_substring":
            old_text = str(override.get("old_text", ""))
            match_count = cue.text.count(old_text) if old_text else 0
            label = old_text
        else:
            pattern = str(override.get("pattern", ""))
            if not pattern or len(pattern) > 256:
                raise ValueError(
                    f"pattern override for cue {cue.source_index} has an invalid pattern"
                )
            try:
                match_count = len(list(re.finditer(pattern, cue.text)))
            except re.error as exc:
                raise ValueError(
                    f"pattern override for cue {cue.source_index} has an invalid pattern"
                ) from exc
            label = pattern
        if match_count != 1:
            raise ValueError(
                f"{action} override for cue {cue.source_index} expected one "
                f"{label!r}, got {match_count}"
            )
        return
    expected = override.get("expect")
    if not isinstance(expected, dict):
        raise ValueError(f"override for cue {cue.source_index} is missing expect")
    for field in ("start", "end", "text"):
        if field == "text":
            if not _expected_text_matches(cue.text, expected):
                raise ValueError(
                    f"source cue {cue.source_index} text drift: "
                    f"expected {expected!r}, got {cue.text!r}"
                )
            continue
        alternatives = expected.get(f"{field}_alternatives", [])
        if alternatives is None:
            alternatives = []
        if not isinstance(alternatives, list) or not all(
            isinstance(value, str) for value in alternatives
        ):
            raise ValueError(
                f"override for cue {cue.source_index} {field}_alternatives must be a string list"
            )
        allowed = [
            _timeline_value(value, timeline_offset_ms)
            for value in [expected.get(field), *alternatives]
        ]
        if getattr(cue, field) not in allowed:
            raise ValueError(
                f"source cue {cue.source_index} {field} drift: "
                f"expected one of {allowed!r}, got {getattr(cue, field)!r}"
            )


def _override_map(
    cues: list[TextCue],
    document: dict[str, Any],
    *,
    timeline_offset_ms: int = 0,
) -> dict[int, dict[str, Any]]:
    schema_version = document.get("schema_version")
    if schema_version not in {1, 2, 3}:
        raise ValueError("text override schema_version must be 1, 2, or 3")
    raw = document.get("overrides")
    if not isinstance(raw, list):
        raise ValueError("text overrides must be a list")
    by_index: dict[int, dict[str, Any]] = {}
    for override in raw:
        if not isinstance(override, dict):
            raise ValueError("each text override must be an object")
        declared_source_index = int(override.get("source_cue", 0))
        if schema_version == 3:
            action = str(override.get("action", "replace"))
            if action in {"replace_substring", "replace_pattern"}:
                locator = override.get("locator")
                if not isinstance(locator, dict):
                    raise ValueError(
                        f"timeline override {declared_source_index} is missing locator"
                    )
                if (
                    timeline_offset_ms
                    and override.get("required") is False
                    and _srt_clock_ms(str(locator.get("end") or ""))
                    <= timeline_offset_ms
                ):
                    # A reviewed repair may target padded pre-context that the
                    # final boundary removes completely.  Optional overrides
                    # remain hash-bound evidence, but must not fail rebasing
                    # merely because their entire locator precedes t=0.
                    continue
                start = _timeline_value(locator.get("start"), timeline_offset_ms)
                end = _timeline_value(locator.get("end"), timeline_offset_ms)
                old_text = str(override.get("old_text") or "")
                pattern = str(override.get("pattern") or "")
                compiled_pattern = None
                if action == "replace_pattern":
                    if not pattern or len(pattern) > 256:
                        raise ValueError(
                            f"timeline override {declared_source_index} has an invalid pattern"
                        )
                    try:
                        compiled_pattern = re.compile(pattern)
                    except re.error as exc:
                        raise ValueError(
                            f"timeline override {declared_source_index} has an invalid pattern"
                        ) from exc
                matches = [
                    cue.source_index
                    for cue in cues
                    if cue.start < end
                    and cue.end > start
                    and (
                        old_text in cue.text
                        if action == "replace_substring"
                        else bool(
                            compiled_pattern is not None
                            and compiled_pattern.search(cue.text)
                        )
                    )
                ]
                if not matches and override.get("required") is False:
                    continue
            else:
                expected = override.get("expect")
                if not isinstance(expected, dict):
                    raise ValueError(
                        f"timeline override {declared_source_index} is missing expect"
                    )
                start = _timeline_value(expected.get("start"), timeline_offset_ms)
                end = _timeline_value(expected.get("end"), timeline_offset_ms)
                matches = [
                    cue.source_index
                    for cue in cues
                    if cue.start == start and cue.end == end
                ]
            if len(matches) != 1:
                raise ValueError(
                    f"timeline override {declared_source_index} matched "
                    f"{len(matches)} cues at {start} --> {end}"
                )
            source_index = matches[0]
        else:
            source_index = declared_source_index
        if not 1 <= source_index <= len(cues):
            raise ValueError(f"text override references missing cue {source_index}")
        if source_index in by_index:
            raise ValueError(f"duplicate text override for cue {source_index}")
        authority = str(override.get("authority", "")).strip()
        if not authority:
            raise ValueError(f"text override for cue {source_index} has no authority")
        by_index[source_index] = override
    return by_index


def _canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_cue_witness_sha256(
    cues: list[TextCue],
    document: dict[str, Any],
    *,
    timeline_offset_ms: int = 0,
) -> str:
    """Bind only reviewed source cues while still binding candidate and cue layout."""

    by_index = _override_map(
        cues, document, timeline_offset_ms=timeline_offset_ms
    )

    def witnessed_value(cue: TextCue, override: dict[str, Any], field: str) -> str:
        expected = override.get("expect")
        if not isinstance(expected, dict):
            return getattr(cue, field)
        if field == "text" and _expected_text_matches(cue.text, expected):
            return str(expected.get("text"))
        primary = expected.get(field)
        alternatives = expected.get(f"{field}_alternatives", [])
        allowed = (
            [primary, *alternatives]
            if isinstance(alternatives, list)
            else [primary]
        )
        actual = getattr(cue, field)
        match_values = (
            [_timeline_value(value, timeline_offset_ms) for value in allowed]
            if field in {"start", "end"}
            else allowed
        )
        return str(primary) if actual in match_values else actual

    timeline_bound = document.get("schema_version") == 3
    if timeline_bound:
        for source_index, override in by_index.items():
            _expect(
                cues[source_index - 1],
                override,
                timeline_offset_ms=timeline_offset_ms,
            )

    def witness_row(source_index: int) -> dict[str, Any]:
        override = by_index[source_index]
        action = str(override.get("action", "replace"))
        if timeline_bound and action in {"replace_substring", "replace_pattern"}:
            row = {
                "source_cue": int(override.get("source_cue", 0)),
                "action": action,
                "locator": override.get("locator"),
            }
            row[
                "old_text" if action == "replace_substring" else "pattern"
            ] = str(
                override.get(
                    "old_text" if action == "replace_substring" else "pattern"
                )
                or ""
            )
            return row
        return {
            "source_cue": (
                int(override.get("source_cue", 0))
                if timeline_bound
                else source_index
            ),
            "start": witnessed_value(
                cues[source_index - 1], override, "start"
            ),
            "end": witnessed_value(
                cues[source_index - 1], override, "end"
            ),
            "text": witnessed_value(
                cues[source_index - 1], override, "text"
            ),
        }

    witness_rows = [
        witness_row(source_index) for source_index in sorted(by_index)
    ]
    if timeline_bound:
        raw_overrides = document.get("overrides") or []
        mapped_override_ids = {id(override) for override in by_index.values()}
        witness_rows.extend(
            {
                "source_cue": int(override.get("source_cue", 0)),
                "action": str(override.get("action", "replace")),
                "locator": override.get("locator"),
                **(
                    {"old_text": str(override.get("old_text") or "")}
                    if str(override.get("action", "replace")) == "replace_substring"
                    else {"pattern": str(override.get("pattern") or "")}
                ),
            }
            for override in raw_overrides
            if isinstance(override, dict)
            and id(override) not in mapped_override_ids
            and str(override.get("action", "replace"))
            in {"replace_substring", "replace_pattern"}
            and override.get("required") is False
        )
        witness_rows.sort(key=lambda row: int(row["source_cue"]))
    payload = {
        "schema_version": (
            "subtitle-text-timeline-cue-witness.v1"
            if timeline_bound
            else "subtitle-text-cue-witness.v1"
        ),
        "candidate_id": str(document.get("candidate_id", "")),
        **({} if timeline_bound else {"source_cue_count": len(cues)}),
        "cues": witness_rows,
    }
    return _canonical_sha256(payload)


def decision_output_witness_sha256(
    cues: list[TextCue],
    document: dict[str, Any],
    *,
    timeline_offset_ms: int = 0,
) -> str:
    """Bind the reviewed decisions without binding unrelated automatic cues."""

    by_index = _override_map(
        cues, document, timeline_offset_ms=timeline_offset_ms
    )
    timeline_bound = document.get("schema_version") == 3
    if timeline_bound:
        reviewed_outputs = []
        for source_index, override in sorted(by_index.items()):
            _expect(
                cues[source_index - 1],
                override,
                timeline_offset_ms=timeline_offset_ms,
            )
            action = str(override.get("action", "replace"))
            row: dict[str, Any] = {
                "source_cue": int(override.get("source_cue", 0)),
                "action": action,
            }
            if action == "drop":
                reviewed_outputs.append(row)
                continue
            if action in {"replace_substring", "replace_pattern"}:
                row.update(
                    {
                        "locator": override.get("locator"),
                        "text": str(override.get("text", "")).strip(),
                    }
                )
                row[
                    "old_text" if action == "replace_substring" else "pattern"
                ] = str(
                    override.get(
                        "old_text" if action == "replace_substring" else "pattern"
                    )
                    or ""
                )
            else:
                expected = override.get("expect") or {}
                row.update(
                    {
                        "start": str(expected.get("start") or ""),
                        "end": str(expected.get("end") or ""),
                        "text": str(override.get("text", "")).strip(),
                    }
                )
            reviewed_outputs.append(row)
        raw_overrides = document.get("overrides") or []
        mapped_override_ids = {id(override) for override in by_index.values()}
        reviewed_outputs.extend(
            {
                "source_cue": int(override.get("source_cue", 0)),
                "action": str(override.get("action", "replace")),
                "locator": override.get("locator"),
                "text": str(override.get("text", "")).strip(),
                **(
                    {"old_text": str(override.get("old_text") or "")}
                    if str(override.get("action", "replace")) == "replace_substring"
                    else {"pattern": str(override.get("pattern") or "")}
                ),
            }
            for override in raw_overrides
            if isinstance(override, dict)
            and id(override) not in mapped_override_ids
            and str(override.get("action", "replace"))
            in {"replace_substring", "replace_pattern"}
            and override.get("required") is False
        )
        reviewed_outputs.sort(key=lambda row: int(row["source_cue"]))
        payload = {
            "schema_version": "subtitle-text-timeline-decision-witness.v1",
            "candidate_id": str(document.get("candidate_id", "")),
            "reviewed_outputs": reviewed_outputs,
        }
        return _canonical_sha256(payload)

    reviewed_outputs: list[dict[str, Any]] = []
    for source_index, override in sorted(by_index.items()):
        cue = cues[source_index - 1]
        action = str(override.get("action", "replace"))
        if action == "drop":
            reviewed_outputs.append({"source_cue": source_index, "action": "drop"})
            continue
        reviewed_outputs.append(
            {
                "source_cue": source_index,
                "action": action,
                "start": cue.start,
                "end": cue.end,
                "text": str(override.get("text", "")).strip(),
            }
        )
    payload = {
        "schema_version": "subtitle-text-decision-witness.v1",
        "candidate_id": str(document.get("candidate_id", "")),
        "source_cue_count": len(cues),
        "reviewed_outputs": reviewed_outputs,
    }
    return _canonical_sha256(payload)


def apply_overrides(
    cues: list[TextCue],
    document: dict[str, Any],
    *,
    timeline_offset_ms: int = 0,
) -> tuple[list[TextCue], list[dict[str, Any]]]:
    by_index = _override_map(
        cues, document, timeline_offset_ms=timeline_offset_ms
    )

    output: list[TextCue] = []
    decisions: list[dict[str, Any]] = []
    for cue in cues:
        override = by_index.get(cue.source_index)
        if override is None:
            output.append(cue)
            continue
        _expect(cue, override, timeline_offset_ms=timeline_offset_ms)
        action = str(override.get("action", "replace"))
        if action == "drop":
            if not str(override.get("reason", "")).strip():
                raise ValueError(f"drop override for cue {cue.source_index} has no reason")
            decisions.append({"source": asdict(cue), "action": "drop", **override})
            continue
        if action == "replace_substring":
            old_text = str(override.get("old_text", ""))
            replacement = str(override.get("text", ""))
            output_text = cue.text.replace(old_text, replacement, 1)
            output.append(TextCue(cue.source_index, cue.start, cue.end, output_text))
            decisions.append(
                {
                    "source": asdict(cue),
                    "action": "replace_substring",
                    "output_text": output_text,
                    **override,
                }
            )
            continue
        if action == "replace_pattern":
            pattern = str(override.get("pattern", ""))
            replacement = str(override.get("text", ""))
            output_text = re.sub(pattern, replacement, cue.text, count=1)
            output.append(TextCue(cue.source_index, cue.start, cue.end, output_text))
            decisions.append(
                {
                    "source": asdict(cue),
                    "action": "replace_pattern",
                    "output_text": output_text,
                    **override,
                }
            )
            continue
        if action != "replace":
            raise ValueError(f"text override for cue {cue.source_index} has invalid action {action!r}")
        replacement = str(override.get("text", "")).strip()
        if not replacement:
            raise ValueError(f"replacement text for cue {cue.source_index} is empty")
        output.append(TextCue(cue.source_index, cue.start, cue.end, replacement))
        decisions.append({"source": asdict(cue), "action": "replace", "output_text": replacement, **override})
    # A reviewed decision can supersede ordinary automatic normalizations, but
    # it cannot bypass the profile's explicitly unbypassable meme canon. Apply
    # this inside the hash-producing function so every downstream manifest
    # binds the actual canonical output.
    canonical_output: list[TextCue] = []
    for cue in output:
        canonical_text, replacements = canonicalize_hard_meme_surfaces(cue.text)
        canonical_text, japanese_replacements = (
            canonicalize_japanese_native_script_surfaces(canonical_text)
        )
        replacements.extend(japanese_replacements)
        canonical_output.append(
            TextCue(
                cue.source_index,
                cue.start,
                cue.end,
                canonical_text,
            )
        )
        if canonical_text != cue.text:
            related_decision = next(
                (
                    row
                    for row in reversed(decisions)
                    if isinstance(row.get("source"), dict)
                    and int(row["source"].get("source_index", 0))
                    == cue.source_index
                ),
                None,
            )
            policy = {
                "authority": ",".join(
                    str(row["authority"]) for row in replacements
                ),
                "reason": (
                    "profile final-surface canon is an output invariant and "
                    "cannot be bypassed by a text override"
                ),
                "replacements": replacements,
            }
            if related_decision is not None:
                related_decision["requested_output_text"] = related_decision.get(
                    "output_text"
                )
                related_decision["output_text"] = canonical_text
                related_decision["final_surface_policy"] = policy
            else:
                decisions.append(
                    {
                        "source": asdict(cue),
                        "action": "final_surface_canonicalize",
                        "output_text": canonical_text,
                        **policy,
                    }
                )
    return canonical_output, decisions


def render_srt(cues: list[TextCue]) -> str:
    blocks = [
        f"{index}\n{cue.start} --> {cue.end}\n{cue.text}"
        for index, cue in enumerate(cues, start=1)
    ]
    return "\n\n".join(blocks) + "\n"


def write_srt(cues: list[TextCue], path: Path) -> None:
    atomic_write_text(path, render_srt(cues))


def validate_bound_override_document(
    source: Path,
    document_path: Path,
    *,
    candidate_id: str,
    expected_source_srt_sha256: str,
    expected_final_srt_sha256: str,
) -> dict[str, Any]:
    """Prove a human-decision asset maps one exact source SRT to one final SRT."""

    document = json.loads(document_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("text override schema_version must be 1")
    if document.get("candidate_id") != candidate_id:
        raise ValueError(
            "text override candidate_id mismatch: "
            f"expected {candidate_id!r}, got {document.get('candidate_id')!r}"
        )
    if document.get("source_srt_sha256") != expected_source_srt_sha256:
        raise ValueError("text override source_srt_sha256 does not match the batch plan")
    if document.get("text_final_srt_sha256") != expected_final_srt_sha256:
        raise ValueError("text override text_final_srt_sha256 does not match the batch plan")
    if not isinstance(document.get("overrides"), list) or not document["overrides"]:
        raise ValueError("维护者 text authority requires at least one override decision")
    actual_source_hash = sha256_file(source)
    if actual_source_hash != expected_source_srt_sha256:
        raise ValueError(
            "source SRT hash mismatch: "
            f"expected {expected_source_srt_sha256!r}, got {actual_source_hash!r}"
        )
    output_cues, _ = apply_overrides(parse_srt(source), document)
    actual_final_hash = hashlib.sha256(render_srt(output_cues).encode("utf-8")).hexdigest()
    if actual_final_hash != expected_final_srt_sha256:
        raise ValueError(
            "text override derived final SRT hash mismatch: "
            f"expected {expected_final_srt_sha256!r}, got {actual_final_hash!r}"
        )
    return document


def apply_document(
    source: Path,
    document_path: Path,
    output: Path,
    manifest_path: Path,
    *,
    timeline_offset_ms: int = 0,
) -> dict[str, Any]:
    document = json.loads(document_path.read_text(encoding="utf-8"))
    actual_source_hash = sha256_file(source)
    source_cues = parse_srt(source)
    schema_version = document.get("schema_version")
    if timeline_offset_ms < 0:
        raise ValueError("timeline_offset_ms must be non-negative")
    if timeline_offset_ms and schema_version != 3:
        raise ValueError("timeline_offset_ms is supported only for schema_version 3")
    witness_manifest: dict[str, Any] = {}
    if schema_version == 1:
        expected_source_hash = str(document.get("source_srt_sha256", ""))
        if actual_source_hash != expected_source_hash:
            raise ValueError(
                f"source SRT hash mismatch: override expects {expected_source_hash!r}, got {actual_source_hash!r}"
            )
    elif schema_version in {2, 3}:
        candidate_id = str(document.get("candidate_id", "")).strip()
        if not candidate_id:
            raise ValueError("cue-bound text override has no candidate_id")
        if schema_version == 2:
            expected_cue_count = int(document.get("source_cue_count", 0))
            if len(source_cues) != expected_cue_count:
                raise ValueError(
                    "source cue count drift: "
                    f"expected {expected_cue_count}, got {len(source_cues)}"
                )
        actual_source_witness = source_cue_witness_sha256(
            source_cues,
            document,
            timeline_offset_ms=timeline_offset_ms,
        )
        expected_source_witness = str(document.get("source_cue_witness_sha256", ""))
        if actual_source_witness != expected_source_witness:
            raise ValueError(
                "reviewed source cue witness mismatch: "
                f"expected {expected_source_witness!r}, got {actual_source_witness!r}"
            )
        actual_decision_witness = decision_output_witness_sha256(
            source_cues,
            document,
            timeline_offset_ms=timeline_offset_ms,
        )
        expected_decision_witness = str(document.get("decision_output_witness_sha256", ""))
        if actual_decision_witness != expected_decision_witness:
            raise ValueError(
                "reviewed decision output witness mismatch: "
                f"expected {expected_decision_witness!r}, got {actual_decision_witness!r}"
            )
        witness_manifest = {
            "candidate_id": candidate_id,
            "source_cue_witness_sha256": actual_source_witness,
            "decision_output_witness_sha256": actual_decision_witness,
        }
    else:
        raise ValueError("text override schema_version must be 1, 2, or 3")
    output_cues, decisions = apply_overrides(
        source_cues,
        document,
        timeline_offset_ms=timeline_offset_ms,
    )
    write_srt(output_cues, output)
    declared_final_hash = document.get("text_final_srt_sha256") if schema_version == 1 else None
    if declared_final_hash is not None and sha256_file(output) != declared_final_hash:
        output.unlink(missing_ok=True)
        raise ValueError("text override derived final SRT hash does not match its decision asset")
    manifest = {
        "schema_version": "subtitle-text-finalization.v1",
        "override_schema_version": schema_version,
        "status": "READY",
        "stage_order": "asr_correction_then_pronoun_then_human_text_then_speaker_then_burn",
        "source_srt": str(source.resolve()),
        "source_srt_sha256": actual_source_hash,
        "override_document": str(document_path.resolve()),
        "override_document_sha256": sha256_file(document_path),
        "output_srt": str(output.resolve()),
        "output_srt_sha256": sha256_file(output),
        "source_cue_count": len(source_cues),
        "output_cue_count": len(output_cues),
        "source_timeline_offset_ms": timeline_offset_ms,
        "decisions": decisions,
        **witness_manifest,
    }
    atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--overrides", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = apply_document(args.source, args.overrides, args.output, args.manifest)
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
