#!/usr/bin/env python3
"""Score already-generated blind subtitles against withheld review truth.

This process is deliberately separate from subtitle generation.  The generator
runs with ``AUTOSLICE_HUMAN_TRUTH_MODE=withheld`` and therefore cannot discover
candidate overrides or regression assets; only the sealed output files enter
this post-hoc scorer.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.autoslice.subtitle_regression import (
    SubtitleRegressionError,
    verify_subtitle_regression_surfaces,
)


ROOT = Path(__file__).resolve().parents[1]


def _regular_file(path: Path, *, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post-hoc score a truth-withheld subtitle run")
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--final-text-srt", type=Path, required=True)
    parser.add_argument("--final-speaker-srt", type=Path, required=True)
    parser.add_argument("--truth-asset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    from src.autoslice.channel_profile import load_channel_profile

    truth = args.truth_asset or (
        load_channel_profile(ROOT).asset_directory("subtitle_regressions")
        / f"{args.candidate_id}.subtitle-regression.v1.json"
    )
    try:
        text_path = _regular_file(args.final_text_srt, label="final text SRT")
        speaker_path = _regular_file(args.final_speaker_srt, label="final speaker SRT")
        truth_path = _regular_file(truth, label="withheld truth asset")
        audit = verify_subtitle_regression_surfaces(
            truth_path,
            candidate_id=args.candidate_id,
            final_text_srt=text_path.read_text(encoding="utf-8"),
            final_speaker_srt=speaker_path.read_text(encoding="utf-8"),
        )
    except (OSError, UnicodeError, ValueError, SubtitleRegressionError) as exc:
        print(f"blind subtitle scoring failed: {exc}", file=sys.stderr)
        return 2

    audit["evaluation_mode"] = "post_hoc_withheld_truth"
    audit["generation_truth_mode_required"] = "withheld"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": audit["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if audit["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
