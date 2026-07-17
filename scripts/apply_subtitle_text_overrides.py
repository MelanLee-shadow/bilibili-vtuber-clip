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


def _override_map(cues: list[TextCue], document: dict[str, Any]) -> dict[int, dict[str, Any]]:
    schema_version = document.get("schema_version")
    if schema_version not in {1, 2}:
        raise ValueError("text override schema_version must be 1 or 2")
    raw = document.get("overrides")
    if not isinstance(raw, list):
        raise ValueError("text overrides must be a list")
    by_index: dict[int, dict[str, Any]] = {}
    for override in raw:
        if not isinstance(override, dict):
            raise ValueError("each text override must be an object")
        source_index = int(override.get("source_cue", 0))
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


def source_cue_witness_sha256(cues: list[TextCue], document: dict[str, Any]) -> str:
    """Bind only reviewed source cues while still binding candidate and cue layout."""

    by_index = _override_map(cues, document)
    payload = {
        "schema_version": "subtitle-text-cue-witness.v1",
        "candidate_id": str(document.get("candidate_id", "")),
        "source_cue_count": len(cues),
        "cues": [
            {
                "source_cue": source_index,
                "start": cues[source_index - 1].start,
                "end": cues[source_index - 1].end,
                "text": cues[source_index - 1].text,
            }
            for source_index in sorted(by_index)
        ],
    }
    return _canonical_sha256(payload)


def decision_output_witness_sha256(cues: list[TextCue], document: dict[str, Any]) -> str:
    """Bind the reviewed decisions without binding unrelated automatic cues."""

    by_index = _override_map(cues, document)
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


def apply_overrides(cues: list[TextCue], document: dict[str, Any]) -> tuple[list[TextCue], list[dict[str, Any]]]:
    by_index = _override_map(cues, document)

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
        raise ValueError("Ivan text authority requires at least one override decision")
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


def apply_document(source: Path, document_path: Path, output: Path, manifest_path: Path) -> dict[str, Any]:
    document = json.loads(document_path.read_text(encoding="utf-8"))
    actual_source_hash = sha256_file(source)
    source_cues = parse_srt(source)
    schema_version = document.get("schema_version")
    witness_manifest: dict[str, Any] = {}
    if schema_version == 1:
        expected_source_hash = str(document.get("source_srt_sha256", ""))
        if actual_source_hash != expected_source_hash:
            raise ValueError(
                f"source SRT hash mismatch: override expects {expected_source_hash!r}, got {actual_source_hash!r}"
            )
    elif schema_version == 2:
        candidate_id = str(document.get("candidate_id", "")).strip()
        if not candidate_id:
            raise ValueError("cue-bound text override has no candidate_id")
        expected_cue_count = int(document.get("source_cue_count", 0))
        if len(source_cues) != expected_cue_count:
            raise ValueError(
                "source cue count drift: "
                f"expected {expected_cue_count}, got {len(source_cues)}"
            )
        actual_source_witness = source_cue_witness_sha256(source_cues, document)
        expected_source_witness = str(document.get("source_cue_witness_sha256", ""))
        if actual_source_witness != expected_source_witness:
            raise ValueError(
                "reviewed source cue witness mismatch: "
                f"expected {expected_source_witness!r}, got {actual_source_witness!r}"
            )
        actual_decision_witness = decision_output_witness_sha256(source_cues, document)
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
        raise ValueError("text override schema_version must be 1 or 2")
    output_cues, decisions = apply_overrides(source_cues, document)
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
