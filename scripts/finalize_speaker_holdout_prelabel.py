#!/usr/bin/env python3
"""Seal the exact 10+5 unlabeled holdout ASR/cue package.

This verifier never calls an ASR provider, creates predictions, opens human
truth, or writes outside the plan-bound private run root. It independently
replays every successful segment artifact twice and publishes one create-only
aggregate receipt while holding the run-level seal lock.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

sys.dont_write_bytecode = True
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.autoslice import speaker_holdout_prelabel as prelabel
from scripts import run_speaker_holdout_prelabel_one as run_one_wrapper


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expected-plan-file-sha256", required=True)
    args = parser.parse_args(argv)
    plan = prelabel.load_plan(
        args.plan,
        expected_file_sha256=args.expected_plan_file_sha256,
    )
    _, decode_mp3, _ = run_one_wrapper.build_execution_callbacks(
        plan,
        runner_path=Path(run_one_wrapper.__file__).resolve(strict=True),
    )
    receipt = prelabel.finalize_aggregate(
        plan_path=args.plan,
        expected_plan_file_sha256=args.expected_plan_file_sha256,
        verifier_path=Path(__file__).resolve(strict=True),
        decode_mp3=decode_mp3,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
