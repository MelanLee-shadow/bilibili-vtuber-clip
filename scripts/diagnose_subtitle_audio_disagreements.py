#!/usr/bin/env python3
"""Expose temporal text disagreements without granting subtitle mutation authority.

Reuse the existing timing/provenance validator. Unlike its anchor-only summary,
this diagnostic retains EVERY final cue, including short and unanchored ones.
ASR text differences are review candidates, not proof that a subtitle is wrong.
"""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.subtitle_audio_correspondence import (  # noqa: E402
    check_subtitle_audio_correspondence,
    parse_timed_srt,
    sha256_file,
)


def diagnose(
    final_srt: Path,
    actual_media: Path,
    witness_srt: Path,
    provenance: Path,
    intro_offset_ms: int = 0,
) -> dict[str, Any]:
    """Return all time-based comparisons, never a semantic PASS or a replacement.

    The largest positive temporal overlap chooses the comparison, not lexical
    similarity. All other overlaps and ties are retained, so a convenient text
    elsewhere cannot hide a disagreement or masquerade as a unique match.
    """
    paths = (final_srt, actual_media, witness_srt, provenance)
    before = {str(path): sha256_file(path) for path in paths}
    timing = check_subtitle_audio_correspondence(
        final_srt, actual_media, witness_srt, provenance, intro_offset_ms
    )
    final = parse_timed_srt(final_srt.read_text(encoding="utf-8"), label="final")
    witness = parse_timed_srt(witness_srt.read_text(encoding="utf-8"), label="witness")
    anchors = {row["final_cue_index"] for row in timing["anchors"]}
    rows = []
    for cue in final:
        start, end = cue.start_ms + intro_offset_ms, cue.end_ms + intro_offset_ms
        overlaps = []
        for other in witness:
            overlap = min(end, other.end_ms) - max(start, other.start_ms)
            if overlap > 0:
                overlaps.append((overlap, other))
        overlaps.sort(key=lambda item: (-item[0], item[1].start_ms))
        row: dict[str, Any] = {
            "cue_index": cue.index,
            "current_text": cue.text,
            "delivery_interval_ms": [cue.start_ms, cue.end_ms],
            "actual_media_interval_ms": [start, end],
            "used_as_timing_anchor": cue.index in anchors,
            "witness_overlaps": [
                {
                    "index": other.index,
                    "text": other.text,
                    "interval_ms": [other.start_ms, other.end_ms],
                    "overlap_ms": overlap,
                }
                for overlap, other in overlaps
            ],
            "normalized_similarity": None,
        }
        if not overlaps:
            row["comparison"] = "NO_TEMPORAL_WITNESS"
        else:
            maximum = overlaps[0][0]
            dominant = [other for overlap, other in overlaps if overlap == maximum]
            row["dominant_witness_indexes"] = [other.index for other in dominant]
            if len(dominant) != 1:
                row["comparison"] = "AMBIGUOUS_TEMPORAL_WITNESS"
            else:
                other = dominant[0]
                row["normalized_similarity"] = round(
                    difflib.SequenceMatcher(
                        None, cue.normalized_text, other.normalized_text, autojunk=False
                    ).ratio(),
                    6,
                )
                row["comparison"] = (
                    "NORMALIZED_TEXT_AGREEMENT"
                    if cue.normalized_text == other.normalized_text
                    else "TEXT_DISAGREEMENT_CANDIDATE"
                )
        rows.append(row)
    if any(sha256_file(path) != before[str(path)] for path in paths):
        raise ValueError("diagnostic input changed during comparison")
    candidates = [row for row in rows if row["comparison"] != "NORMALIZED_TEXT_AGREEMENT"]
    return {
        "schema_version": "subtitle-audio-disagreement-diagnostic.v1",
        "status": "DIAGNOSTIC_ONLY",
        "text_correctness_status": "UNASSESSED",
        "mutation_authorized": False,
        "release_authorized": False,
        "inputs": timing["inputs"],
        "timebase": timing["timebase"],
        "timing_status": timing["timing_status"],
        "timing_reason_codes": timing["reason_codes"],
        "cue_count": len(rows),
        "review_candidate_count": len(candidates),
        "review_candidate_indexes": [row["cue_index"] for row in candidates],
        "rows": rows,
        "limits": [
            "A text match is not correctness; ASR and subtitles may share an error.",
            "A disagreement may reflect ASR mistakes, homophones, or split/merged cues.",
            "Timing BLOCK or insufficiency remains unresolved; this tool never clears it.",
            "Similarity is descriptive, not confidence, a calibrated threshold, or a vote.",
            "Only the original text judge and applicable local audio evidence can resolve candidates.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final-srt", required=True, type=Path)
    parser.add_argument("--actual-media", required=True, type=Path)
    parser.add_argument("--witness-srt", required=True, type=Path)
    parser.add_argument("--provenance", required=True, type=Path)
    parser.add_argument("--intro-offset-ms", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        result = diagnose(
            args.final_srt,
            args.actual_media,
            args.witness_srt,
            args.provenance,
            args.intro_offset_ms,
        )
    except (OSError, ValueError) as exc:
        print(f"Subtitle/audio diagnostic failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
