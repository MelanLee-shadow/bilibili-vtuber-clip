#!/usr/bin/env python3
"""Apply reviewed speaker turns to a labelled SRT.

The important invariant is that an ASR cue is not a speaker boundary.  A single
source cue may therefore be replaced by two or more contiguous speaker turns.
Overrides are fail-closed against the source hash and the expected cue text and
timestamps so that a decision cannot silently drift onto another transcript.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SRT_BLOCK_RE = re.compile(
    r"(?ms)^\s*(\d+)\s*\n"
    r"(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*\n"
    r"(.*?)(?=\n{2,}|\Z)"
)
LABEL_RE = re.compile(r"^\[(李豆沙|连线)(?:\s+[+-]?\d+(?:\.\d+)?)?\]\s*(.*)$", re.S)
SPEAKERS = {"李豆沙", "连线"}


@dataclass(frozen=True)
class Cue:
    source_index: int
    start: str
    end: str
    speaker: str
    text: str
    decision_source: str
    authority: str | None = None
    note: str | None = None
    speaker_detail: str | None = None
    layer: int = 0
    placement: str = "main"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    """Replace one text artifact atomically without exposing a truncated file."""
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


def timestamp_ms(value: str) -> int:
    try:
        hours, minutes, seconds_ms = value.split(":")
        seconds, millis = seconds_ms.split(",")
        return (
            int(hours) * 3_600_000
            + int(minutes) * 60_000
            + int(seconds) * 1_000
            + int(millis)
        )
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid SRT timestamp: {value!r}") from exc


def parse_labelled_srt(path: Path) -> list[Cue]:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for match in SRT_BLOCK_RE.finditer(text):
        source_index = int(match.group(1))
        body = " ".join(line.strip() for line in match.group(4).strip().splitlines())
        label_match = LABEL_RE.match(body)
        if not label_match:
            raise ValueError(
                f"source cue {source_index} is not labelled as [李豆沙] or [连线]: {body!r}"
            )
        cues.append(
            Cue(
                source_index=source_index,
                start=match.group(2),
                end=match.group(3),
                speaker=label_match.group(1),
                text=label_match.group(2).strip(),
                decision_source="inherited_source_label",
            )
        )
    if not cues:
        raise ValueError(f"no SRT cues found in {path}")
    expected = list(range(1, len(cues) + 1))
    actual = [cue.source_index for cue in cues]
    if actual != expected:
        raise ValueError(f"source SRT indices must be contiguous from 1: {actual}")
    return cues


def _expect_equal(cue: Cue, expected: dict[str, Any]) -> None:
    for field in ("start", "end", "text"):
        if field not in expected:
            raise ValueError(f"override for cue {cue.source_index} is missing expect.{field}")
        if getattr(cue, field) != expected[field]:
            raise ValueError(
                f"source cue {cue.source_index} {field} drift: "
                f"expected {expected[field]!r}, got {getattr(cue, field)!r}"
            )


def _replacement_cues(cue: Cue, override: dict[str, Any]) -> list[Cue]:
    _expect_equal(cue, override.get("expect", {}))
    authority = str(override.get("authority", "")).strip()
    if not authority:
        raise ValueError(f"override for cue {cue.source_index} has no authority")

    action = str(override.get("action", "replace")).strip()
    if action == "drop":
        reason = str(override.get("reason", "")).strip()
        if not reason:
            raise ValueError(f"drop override for cue {cue.source_index} has no reason")
        if override.get("segments") not in (None, []):
            raise ValueError(f"drop override for cue {cue.source_index} must not contain segments")
        if override.get("overlays") not in (None, []):
            raise ValueError(f"drop override for cue {cue.source_index} must not contain overlays")
        return []
    if action != "replace":
        raise ValueError(f"override for cue {cue.source_index} has invalid action {action!r}")

    segments = override.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError(f"override for cue {cue.source_index} has no segments")

    replacement: list[Cue] = []
    for position, segment in enumerate(segments, 1):
        speaker = segment.get("speaker")
        if speaker not in SPEAKERS:
            raise ValueError(
                f"override for cue {cue.source_index} segment {position} has invalid speaker {speaker!r}"
            )
        start = str(segment.get("start", ""))
        end = str(segment.get("end", ""))
        if timestamp_ms(end) <= timestamp_ms(start):
            raise ValueError(
                f"override for cue {cue.source_index} segment {position} has non-positive duration"
            )
        segment_text = str(segment.get("text", "")).strip()
        if not segment_text:
            raise ValueError(f"override for cue {cue.source_index} segment {position} has empty text")
        replacement.append(
            Cue(
                source_index=cue.source_index,
                start=start,
                end=end,
                speaker=speaker,
                text=segment_text,
                decision_source="reviewed_override",
                authority=authority,
                note=str(override.get("note", "")).strip() or None,
                speaker_detail=str(segment.get("speaker_detail", "")).strip() or None,
            )
        )

    if replacement[0].start != cue.start or replacement[-1].end != cue.end:
        raise ValueError(
            f"override for cue {cue.source_index} must cover the complete source interval "
            f"{cue.start} --> {cue.end}"
        )
    for left, right in zip(replacement, replacement[1:]):
        if left.end != right.start:
            raise ValueError(
                f"override for cue {cue.source_index} has a gap or overlap at {left.end}/{right.start}"
            )

    overlays = override.get("overlays", [])
    if not isinstance(overlays, list):
        raise ValueError(f"override for cue {cue.source_index} overlays must be a list")
    for position, overlay in enumerate(overlays, 1):
        speaker = overlay.get("speaker")
        if speaker not in SPEAKERS:
            raise ValueError(
                f"override for cue {cue.source_index} overlay {position} has invalid speaker {speaker!r}"
            )
        start = str(overlay.get("start", ""))
        end = str(overlay.get("end", ""))
        start_ms = timestamp_ms(start)
        end_ms = timestamp_ms(end)
        if end_ms <= start_ms:
            raise ValueError(
                f"override for cue {cue.source_index} overlay {position} has non-positive duration"
            )
        if start_ms < timestamp_ms(cue.start) or end_ms > timestamp_ms(cue.end):
            raise ValueError(
                f"override for cue {cue.source_index} overlay {position} escapes source interval"
            )
        overlay_text = str(overlay.get("text", "")).strip()
        if not overlay_text:
            raise ValueError(f"override for cue {cue.source_index} overlay {position} has empty text")
        layer = int(overlay.get("layer", 1))
        if layer < 1:
            raise ValueError(f"override for cue {cue.source_index} overlay {position} layer must be >= 1")
        replacement.append(
            Cue(
                source_index=cue.source_index,
                start=start,
                end=end,
                speaker=speaker,
                text=overlay_text,
                decision_source="reviewed_overlay",
                authority=authority,
                note=str(override.get("note", "")).strip() or None,
                speaker_detail=str(overlay.get("speaker_detail", "")).strip() or None,
                layer=layer,
                placement="above",
            )
        )
    return replacement


def apply_overrides(source_cues: list[Cue], document: dict[str, Any]) -> list[Cue]:
    if document.get("schema_version") != 1:
        raise ValueError("override schema_version must be 1")
    raw_overrides = document.get("overrides")
    if not isinstance(raw_overrides, list):
        raise ValueError("overrides must be a list")

    by_index: dict[int, dict[str, Any]] = {}
    for override in raw_overrides:
        source_index = int(override.get("source_cue", 0))
        if source_index in by_index:
            raise ValueError(f"duplicate override for source cue {source_index}")
        if not 1 <= source_index <= len(source_cues):
            raise ValueError(f"override references missing source cue {source_index}")
        by_index[source_index] = override

    output: list[Cue] = []
    for cue in source_cues:
        override = by_index.get(cue.source_index)
        output.extend(_replacement_cues(cue, override) if override else [cue])
    return sorted(
        output,
        key=lambda cue: (
            timestamp_ms(cue.start),
            cue.layer,
            timestamp_ms(cue.end),
            cue.source_index,
        ),
    )


def write_srt(cues: list[Cue], path: Path) -> None:
    blocks = [
        f"{index}\n{cue.start} --> {cue.end}\n[{cue.speaker}] {cue.text}"
        for index, cue in enumerate(cues, 1)
    ]
    atomic_write_text(path, "\n\n".join(blocks) + "\n")


def _ass_timestamp(value: str) -> str:
    hours, minutes, seconds_ms = value.split(":")
    seconds, millis = seconds_ms.split(",")
    return f"{int(hours)}:{minutes}:{seconds}.{millis[:2]}"


def _ass_timestamp_ms(value: int) -> str:
    centiseconds = max(0, (int(value) + 5) // 10)
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def _ass_escape(value: str) -> str:
    # ``_layout_cue_for_display`` already uses ASS's literal ``\N`` line-break
    # marker. Preserve that marker while escaping arbitrary user backslashes;
    # turning it into ``\\N`` makes libass render characters instead of a break.
    line_break = "\u0000ASS_LINE_BREAK\u0000"
    return (
        value.replace(r"\N", line_break)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", line_break)
        .replace(line_break, r"\N")
    )


def write_ass(cues: list[Cue], path: Path, *, show_speaker_labels: bool = False) -> None:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: LDS,Microsoft YaHei,72,&H00FFFFFF,&H000000FF,&H00203050,&H70000000,-1,0,0,0,100,100,0,0,1,3,2,2,60,60,40,1
Style: GUEST,Microsoft YaHei,72,&H0000FFFF,&H000000FF,&H00203050,&H70000000,-1,0,0,0,100,100,0,0,1,3,2,2,60,60,40,1
Style: LDS_OVERLAP,Microsoft YaHei,58,&H00FFFFFF,&H000000FF,&H00203050,&H50000000,-1,0,0,0,100,100,0,0,3,2,0,2,80,80,142,1
Style: GUEST_OVERLAP,Microsoft YaHei,58,&H0000FFFF,&H000000FF,&H00203050,&H50000000,-1,0,0,0,100,100,0,0,3,2,0,2,80,80,142,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    # Reuse the approved production line-layout policy: <=28 display chars per
    # line, <=2 lines, and sequential sub-cues for genuinely long text.
    from scripts.run_auto_review_shadow_pipeline import _layout_cue_for_display

    for cue in cues:
        style = "LDS" if cue.speaker == "李豆沙" else "GUEST"
        if cue.placement == "above":
            style += "_OVERLAP"
        visible = f"[{cue.speaker}] {cue.text}" if show_speaker_labels else cue.text
        for start_ms, end_ms, display_text in _layout_cue_for_display(
            timestamp_ms(cue.start), timestamp_ms(cue.end), visible
        ):
            text = _ass_escape(display_text)
            events.append(
                f"Dialogue: {cue.layer},{_ass_timestamp_ms(start_ms)},{_ass_timestamp_ms(end_ms)},"
                f"{style},,0,0,0,,{text}"
            )
    atomic_write_text(path, header + "\n".join(events) + "\n")


def build_manifest(
    source_path: Path,
    override_path: Path,
    output_srt: Path,
    output_ass: Path,
    cues: list[Cue],
    document: dict[str, Any],
    source_cue_count: int,
) -> dict[str, Any]:
    reviewed = [cue for cue in cues if cue.decision_source.startswith("reviewed_")]
    inherited = [cue for cue in cues if cue.decision_source == "inherited_source_label"]
    dropped = [
        {
            "source_cue": int(override["source_cue"]),
            "reason": str(override.get("reason", "")),
            "authority": str(override.get("authority", "")),
        }
        for override in document.get("overrides", [])
        if override.get("action") == "drop"
    ]
    return {
        "schema_version": 1,
        "source_srt": str(source_path.resolve()),
        "source_srt_sha256": sha256_file(source_path),
        "overrides": str(override_path.resolve()),
        "overrides_sha256": sha256_file(override_path),
        "output_srt": str(output_srt.resolve()),
        "output_srt_sha256": sha256_file(output_srt),
        "output_ass": str(output_ass.resolve()),
        "output_ass_sha256": sha256_file(output_ass),
        "source_cue_count": source_cue_count,
        "output_cue_count": len(cues),
        "reviewed_output_cue_count": len(reviewed),
        "inherited_output_cue_count": len(inherited),
        "overlap_output_cue_count": sum(cue.placement == "above" for cue in cues),
        "dropped_source_cues": dropped,
        "review_status": document.get("status"),
        "fully_reviewed": not inherited,
        "decisions": [asdict(cue) for cue in cues],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply fail-closed, boundary-aware speaker overrides to a labelled SRT."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--overrides", required=True, type=Path)
    parser.add_argument("--output-srt", required=True, type=Path)
    parser.add_argument("--output-ass", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    document = json.loads(args.overrides.read_text(encoding="utf-8"))
    expected_hash = document.get("source_srt_sha256")
    actual_hash = sha256_file(args.source)
    if expected_hash != actual_hash:
        raise ValueError(
            f"source SRT hash mismatch: override expects {expected_hash!r}, got {actual_hash!r}"
        )

    source_cues = parse_labelled_srt(args.source)
    output_cues = apply_overrides(source_cues, document)
    write_srt(output_cues, args.output_srt)
    write_ass(output_cues, args.output_ass)
    manifest = build_manifest(
        args.source,
        args.overrides,
        args.output_srt,
        args.output_ass,
        output_cues,
        document,
        len(source_cues),
    )
    atomic_write_text(args.manifest, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output_cues": manifest["output_cue_count"],
                "reviewed_output_cues": manifest["reviewed_output_cue_count"],
                "fully_reviewed": manifest["fully_reviewed"],
                "manifest": str(args.manifest),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
