"""Harvest 维护者's A/B-annotated subtitle review into a truth-diff artifact.

维护者 reviews delivered ``*.speaker.srt`` / ``*.srt`` files in place. His grammar
（标注收割节）：

- An isolated ``A``/``B`` that is preceded by a space and followed by a space or
  end-of-line is a speaker marker: ``A`` = 李豆沙, ``B`` = 非李豆沙(连线).
  Letters inside words (``BW``/``OK``/``PSP``) are never markers.
- Within a cue, each marker governs the text back to the previous marker (or the
  cue start). Unmarked remainder text keeps the machine label, and a cue without
  markers keeps its machine label wholesale.
- Any text difference outside markers is a text correction by 维护者.

Output schema ``维护者-speaker-truth-diff.v2``: the v1 cue shape plus provenance
hashes and a ``marked`` flag. Unlike the hand-built v1 fixture, ``text_changed``
here means a real text edit — the marker-stripped segment concatenation is
compared against the machine text with all whitespace removed, so neither a pure
speaker split nor a moved segment boundary across a machine-emitted space counts
as a text change.

The parser is deliberately fail-closed: cue-count, timing, or label-prefix drift
between the pristine and annotated files raises instead of guessing, and a
pristine file that itself contains marker-shaped tokens must be resolved by a
human before harvesting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

MARKER_LABELS = {"A": "李豆沙", "B": "连线"}
# A marker is an isolated ` A`/` B` followed by whitespace, end-of-text, or a
# CJK punctuation mark (维护者 writes `不是 A，不是吗 B` — the marker governs the
# text before it even when a full-width comma follows). Letters inside words
# (`BW`/`OK`) never match because they lack the leading space.
_MARKER_RE = re.compile(r" ([AB])(?=[ \n，。？！、）]|$)")
_PREFIX_RE = re.compile(r"^\[([^\]]+)\] ?")
_WS_RE = re.compile(r"\s+")


class HarvestError(ValueError):
    """Raised when the annotated file cannot be safely interpreted."""


@dataclass
class Cue:
    index: int
    timing: str
    text: str


def parse_srt(raw: str) -> list[Cue]:
    cues = []
    blocks = re.split(r"\n\s*\n", raw.replace("\r\n", "\n").replace("\r", "\n").strip())
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 2 or "-->" not in lines[1]:
            raise HarvestError(f"malformed SRT block: {block[:80]!r}")
        text = "\n".join(line.rstrip() for line in lines[2:])
        cues.append(Cue(index=int(lines[0].strip()), timing=lines[1].strip(), text=text))
    return cues


def split_label(text: str) -> tuple[str | None, str]:
    match = _PREFIX_RE.match(text)
    if match:
        return match.group(1), text[match.end():]
    return None, text


def parse_truth_segments(body: str, machine_label: str | None) -> list[dict]:
    segments = []
    pos = 0
    for match in _MARKER_RE.finditer(body):
        chunk = body[pos:match.start()]
        if not chunk.strip():
            raise HarvestError(f"marker {match.group(1)!r} governs empty text in {body!r}")
        segments.append({"text": chunk.strip(), "label": MARKER_LABELS[match.group(1)]})
        pos = match.end()
        if pos < len(body) and body[pos] == " ":
            pos += 1
    remainder = body[pos:]
    if remainder.strip():
        segments.append({"text": remainder.strip(), "label": machine_label})
    if not segments:
        raise HarvestError(f"cue reduced to no segments: {body!r}")
    return segments


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_TIMING_RX = re.compile(r"(\d+):(\d+):(\d+),(\d+)\s*-->\s*(\d+):(\d+):(\d+),(\d+)")


def _timing_ms(timing: str) -> tuple[int, int]:
    match = _TIMING_RX.match(timing)
    if match is None:
        raise HarvestError(f"unparseable timing {timing!r}")
    g = [int(x) for x in match.groups()]
    return (
        g[0] * 3600000 + g[1] * 60000 + g[2] * 1000 + g[3],
        g[4] * 3600000 + g[5] * 60000 + g[6] * 1000 + g[7],
    )


def harvest_cue(
    pristine: Cue, annotated: Cue, *, timing_tolerance_ms: int = 0
) -> dict:
    if pristine.timing != annotated.timing:
        p_start, p_end = _timing_ms(pristine.timing)
        a_start, a_end = _timing_ms(annotated.timing)
        jitter = max(abs(p_start - a_start), abs(p_end - a_end))
        if jitter > timing_tolerance_ms:
            raise HarvestError(
                f"cue {pristine.index}: timing drift {pristine.timing!r} -> {annotated.timing!r}"
            )
    machine_label, machine_body = split_label(pristine.text)
    annotated_label, annotated_body = split_label(annotated.text)
    if machine_label != annotated_label:
        raise HarvestError(
            f"cue {pristine.index}: label prefix edited "
            f"{machine_label!r} -> {annotated_label!r}; A/B grammar expected instead"
        )
    if _MARKER_RE.search(machine_body):
        raise HarvestError(
            f"cue {pristine.index}: pristine text contains marker-shaped token; "
            "resolve by hand before harvesting"
        )
    marked = bool(_MARKER_RE.search(annotated_body))
    segments = parse_truth_segments(annotated_body, machine_label)
    labels = {seg["label"] for seg in segments}
    row = {
        "cue": pristine.index,
        "timing": pristine.timing,
        "machine_label": machine_label,
        "machine_text": machine_body,
        "truth_segments": segments,
        "truth_text": " ".join(seg["text"] for seg in segments),
        "text_changed": _WS_RE.sub("", "".join(seg["text"] for seg in segments))
        != _WS_RE.sub("", machine_body),
        "label_changed": any(seg["label"] != machine_label for seg in segments),
        "mixed": len(labels) > 1,
        "marked": marked,
    }
    if pristine.timing != annotated.timing:
        row["annotated_timing"] = annotated.timing
    return row


def _align_with_merges(
    pristine_cues: list[Cue], annotated_cues: list[Cue], timing_tolerance_ms: int
) -> list[tuple[list[Cue], Cue]]:
    """Pair each annotated cue with the pristine cue(s) it covers.

    Only two shapes are accepted: 1:1 (timings equal within tolerance) and an
    N:1 merge where the annotated cue's span covers a run of consecutive
    pristine cues (start matches the first, end matches the last, both within
    tolerance). Anything else raises — deletions, insertions, or reflowed
    timings must be resolved by a human before harvesting.
    """

    pairs: list[tuple[list[Cue], Cue]] = []
    i = 0
    for annotated in annotated_cues:
        if i >= len(pristine_cues):
            raise HarvestError(f"annotated cue {annotated.index} has no pristine counterpart")
        a_start, a_end = _timing_ms(annotated.timing)
        p_start, _ = _timing_ms(pristine_cues[i].timing)
        if abs(p_start - a_start) > timing_tolerance_ms:
            raise HarvestError(
                f"annotated cue {annotated.index}: start {annotated.timing!r} does not "
                f"match pristine cue {pristine_cues[i].index} {pristine_cues[i].timing!r}"
            )
        group = [pristine_cues[i]]
        i += 1
        while abs(_timing_ms(group[-1].timing)[1] - a_end) > timing_tolerance_ms:
            if i >= len(pristine_cues):
                raise HarvestError(
                    f"annotated cue {annotated.index}: end {annotated.timing!r} matches no "
                    "consecutive pristine cue run"
                )
            nxt = pristine_cues[i]
            if _timing_ms(nxt.timing)[0] < _timing_ms(group[-1].timing)[1] - timing_tolerance_ms:
                raise HarvestError(
                    f"annotated cue {annotated.index}: pristine cues overlap during merge scan"
                )
            group.append(nxt)
            i += 1
        pairs.append((group, annotated))
    if i != len(pristine_cues):
        raise HarvestError(
            f"{len(pristine_cues) - i} trailing pristine cue(s) unmatched by annotated file"
        )
    return pairs


def harvest_merged_cue(
    group: list[Cue], annotated: Cue, *, timing_tolerance_ms: int
) -> dict:
    """Harvest an N:1 merge: machine text is the join of the merged cues."""

    if len(group) == 1:
        return harvest_cue(group[0], annotated, timing_tolerance_ms=timing_tolerance_ms)
    labels_bodies = [split_label(cue.text) for cue in group]
    machine_labels = {label for label, _ in labels_bodies}
    if len(machine_labels) > 1:
        raise HarvestError(
            f"cue {group[0].index}: merge spans differing machine labels {machine_labels}"
        )
    machine_label = next(iter(machine_labels))
    for _, body in labels_bodies:
        if _MARKER_RE.search(body):
            raise HarvestError(
                f"cue {group[0].index}: pristine text contains marker-shaped token; "
                "resolve by hand before harvesting"
            )
    annotated_label, annotated_body = split_label(annotated.text)
    if machine_label != annotated_label:
        raise HarvestError(
            f"cue {group[0].index}: label prefix edited "
            f"{machine_label!r} -> {annotated_label!r}; A/B grammar expected instead"
        )
    machine_body = " ".join(body for _, body in labels_bodies)
    segments = parse_truth_segments(annotated_body, machine_label)
    labels = {seg["label"] for seg in segments}
    return {
        "cue": group[0].index,
        "timing": group[0].timing,
        "annotated_timing": annotated.timing,
        "merged_from": [cue.index for cue in group],
        "merged_machine_timings": [cue.timing for cue in group],
        "machine_label": machine_label,
        "machine_text": machine_body,
        "truth_segments": segments,
        "truth_text": " ".join(seg["text"] for seg in segments),
        "text_changed": _WS_RE.sub("", "".join(seg["text"] for seg in segments))
        != _WS_RE.sub("", machine_body),
        "label_changed": any(seg["label"] != machine_label for seg in segments),
        "mixed": len(labels) > 1,
        "marked": bool(_MARKER_RE.search(annotated_body)),
    }


def harvest(
    pristine_path: Path,
    annotated_path: Path,
    candidate_id: str,
    authority: str,
    *,
    timing_tolerance_ms: int = 0,
) -> dict:
    pristine_cues = parse_srt(pristine_path.read_text(encoding="utf-8"))
    annotated_cues = parse_srt(annotated_path.read_text(encoding="utf-8"))
    if len(pristine_cues) != len(annotated_cues):
        if timing_tolerance_ms <= 0:
            raise HarvestError(
                f"cue count mismatch: pristine={len(pristine_cues)} annotated={len(annotated_cues)}"
            )
        pairs = _align_with_merges(pristine_cues, annotated_cues, timing_tolerance_ms)
        cues = [
            harvest_merged_cue(group, a, timing_tolerance_ms=timing_tolerance_ms)
            for group, a in pairs
        ]
    else:
        cues = [
            harvest_cue(p, a, timing_tolerance_ms=timing_tolerance_ms)
            for p, a in zip(pristine_cues, annotated_cues)
        ]
    return {
        "schema": "维护者-speaker-truth-diff.v2",
        "candidate_id": candidate_id,
        "source_machine_sha256": _sha256(pristine_path),
        "annotated_sha256": _sha256(annotated_path),
        "authority": authority,
        "generated_by": "scripts/harvest_human_review_truth.py",
        "summary": {
            "cues": len(cues),
            "text_changed": sum(c["text_changed"] for c in cues),
            "label_changed": sum(c["label_changed"] for c in cues),
            "mixed": sum(c["mixed"] for c in cues),
            "marked": sum(c["marked"] for c in cues),
            "merged": sum(1 for c in cues if c.get("merged_from")),
            "timing_tweaked": sum(1 for c in cues if c.get("annotated_timing")),
        },
        "cues": cues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pristine", required=True, type=Path)
    parser.add_argument("--annotated", required=True, type=Path)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--authority", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--timing-tolerance-ms",
        type=int,
        default=0,
        help=(
            "Accept per-cue timing jitter up to this many ms (editor re-serialization) "
            "and N:1 cue merges whose span matches a consecutive pristine run. "
            "0 (default) keeps the original strict byte-equal timing behaviour."
        ),
    )
    args = parser.parse_args()
    artifact = harvest(
        args.pristine,
        args.annotated,
        args.candidate_id,
        args.authority,
        timing_tolerance_ms=args.timing_tolerance_ms,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    summary = artifact["summary"]
    print(
        f"{args.candidate_id}: cues={summary['cues']} text_changed={summary['text_changed']} "
        f"label_changed={summary['label_changed']} mixed={summary['mixed']} -> {args.out}"
    )
    for cue in artifact["cues"]:
        if cue["text_changed"] or cue["label_changed"]:
            print(f"  cue {cue['cue']}: {cue['machine_text']!r} -> {cue['truth_segments']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
