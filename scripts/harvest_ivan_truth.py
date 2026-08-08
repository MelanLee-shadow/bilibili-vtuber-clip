"""Harvest Ivan's A/B-annotated subtitle review into a truth-diff artifact.

Ivan reviews delivered ``*.speaker.srt`` / ``*.srt`` files in place. His grammar
(HANDOFF 2026-08-08, 标注收割节):

- An isolated ``A``/``B`` that is preceded by a space and followed by a space or
  end-of-line is a speaker marker: ``A`` = 李豆沙, ``B`` = 非李豆沙(连线).
  Letters inside words (``BW``/``OK``/``PSP``) are never markers.
- Within a cue, each marker governs the text back to the previous marker (or the
  cue start). Unmarked remainder text keeps the machine label, and a cue without
  markers keeps its machine label wholesale.
- Any text difference outside markers is a text correction by Ivan.

Output schema ``ivan-speaker-truth-diff.v2``: the v1 cue shape plus provenance
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
_MARKER_RE = re.compile(r" ([AB])(?=[ \n]|$)")
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


def harvest_cue(pristine: Cue, annotated: Cue) -> dict:
    if pristine.timing != annotated.timing:
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
    return {
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


def harvest(pristine_path: Path, annotated_path: Path, candidate_id: str, authority: str) -> dict:
    pristine_cues = parse_srt(pristine_path.read_text(encoding="utf-8"))
    annotated_cues = parse_srt(annotated_path.read_text(encoding="utf-8"))
    if len(pristine_cues) != len(annotated_cues):
        raise HarvestError(
            f"cue count mismatch: pristine={len(pristine_cues)} annotated={len(annotated_cues)}"
        )
    cues = [harvest_cue(p, a) for p, a in zip(pristine_cues, annotated_cues)]
    return {
        "schema": "ivan-speaker-truth-diff.v2",
        "candidate_id": candidate_id,
        "source_machine_sha256": _sha256(pristine_path),
        "annotated_sha256": _sha256(annotated_path),
        "authority": authority,
        "generated_by": "scripts/harvest_ivan_truth.py",
        "summary": {
            "cues": len(cues),
            "text_changed": sum(c["text_changed"] for c in cues),
            "label_changed": sum(c["label_changed"] for c in cues),
            "mixed": sum(c["mixed"] for c in cues),
            "marked": sum(c["marked"] for c in cues),
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
    args = parser.parse_args()
    artifact = harvest(args.pristine, args.annotated, args.candidate_id, args.authority)
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
