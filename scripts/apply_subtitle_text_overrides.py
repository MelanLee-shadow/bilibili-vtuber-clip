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


SRT_BLOCK_RE = re.compile(
    r"(?ms)^\s*(\d+)\s*\n"
    r"(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*\n"
    r"(.*?)(?=\n{2,}|\Z)"
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


def _expect(cue: TextCue, override: dict[str, Any]) -> None:
    expected = override.get("expect")
    if not isinstance(expected, dict):
        raise ValueError(f"override for cue {cue.source_index} is missing expect")
    for field in ("start", "end", "text"):
        if expected.get(field) != getattr(cue, field):
            raise ValueError(
                f"source cue {cue.source_index} {field} drift: "
                f"expected {expected.get(field)!r}, got {getattr(cue, field)!r}"
            )


def apply_overrides(cues: list[TextCue], document: dict[str, Any]) -> tuple[list[TextCue], list[dict[str, Any]]]:
    if document.get("schema_version") != 1:
        raise ValueError("text override schema_version must be 1")
    raw = document.get("overrides")
    if not isinstance(raw, list):
        raise ValueError("text overrides must be a list")
    by_index: dict[int, dict[str, Any]] = {}
    for override in raw:
        source_index = int(override.get("source_cue", 0))
        if not 1 <= source_index <= len(cues):
            raise ValueError(f"text override references missing cue {source_index}")
        if source_index in by_index:
            raise ValueError(f"duplicate text override for cue {source_index}")
        authority = str(override.get("authority", "")).strip()
        if not authority:
            raise ValueError(f"text override for cue {source_index} has no authority")
        by_index[source_index] = override

    output: list[TextCue] = []
    decisions: list[dict[str, Any]] = []
    for cue in cues:
        override = by_index.get(cue.source_index)
        if override is None:
            output.append(cue)
            continue
        _expect(cue, override)
        action = str(override.get("action", "replace"))
        if action == "drop":
            if not str(override.get("reason", "")).strip():
                raise ValueError(f"drop override for cue {cue.source_index} has no reason")
            decisions.append({"source": asdict(cue), "action": "drop", **override})
            continue
        if action != "replace":
            raise ValueError(f"text override for cue {cue.source_index} has invalid action {action!r}")
        replacement = str(override.get("text", "")).strip()
        if not replacement:
            raise ValueError(f"replacement text for cue {cue.source_index} is empty")
        output.append(TextCue(cue.source_index, cue.start, cue.end, replacement))
        decisions.append({"source": asdict(cue), "action": "replace", "output_text": replacement, **override})
    return output, decisions


def write_srt(cues: list[TextCue], path: Path) -> None:
    blocks = [
        f"{index}\n{cue.start} --> {cue.end}\n{cue.text}"
        for index, cue in enumerate(cues, start=1)
    ]
    atomic_write_text(path, "\n\n".join(blocks) + "\n")


def apply_document(source: Path, document_path: Path, output: Path, manifest_path: Path) -> dict[str, Any]:
    document = json.loads(document_path.read_text(encoding="utf-8"))
    actual_source_hash = sha256_file(source)
    expected_source_hash = str(document.get("source_srt_sha256", ""))
    if actual_source_hash != expected_source_hash:
        raise ValueError(
            f"source SRT hash mismatch: override expects {expected_source_hash!r}, got {actual_source_hash!r}"
        )
    source_cues = parse_srt(source)
    output_cues, decisions = apply_overrides(source_cues, document)
    write_srt(output_cues, output)
    manifest = {
        "schema_version": "subtitle-text-finalization.v1",
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
        "decisions": decisions,
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
