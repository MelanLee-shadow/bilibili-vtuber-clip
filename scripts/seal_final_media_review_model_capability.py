#!/usr/bin/env python3
"""Seal one completed dual-media sentinel run into runtime capability evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.final_media_review_model_capability_seal import (
    FinalMediaReviewModelCapabilitySealError,
    seal_model_capability_attestation,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create or recover a deterministic model-capability attestation "
            "and commit-marker receipt from an already completed sentinel run."
        )
    )
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--sentinel-result", required=True, type=Path)
    parser.add_argument("--target-executable", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--valid-for-seconds", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        result = seal_model_capability_attestation(
            runtime_root=args.runtime_root,
            sentinel_result_path=args.sentinel_result,
            target_executable_path=args.target_executable,
            output_directory=args.output_directory,
            valid_for_seconds=args.valid_for_seconds,
        )
    except FinalMediaReviewModelCapabilitySealError as exc:
        sys.stderr.write(
            json.dumps(
                {"reason_code": exc.reason_code, "detail": exc.detail},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
