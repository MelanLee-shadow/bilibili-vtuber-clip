#!/usr/bin/env python3
"""Validate a holdout prelabel plan; real provider execution is intentionally absent."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.autoslice.speaker_holdout_prelabel import load_plan


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expected-plan-file-sha256", required=True)
    args = parser.parse_args(argv)
    plan = load_plan(args.plan, expected_file_sha256=args.expected_plan_file_sha256)
    print(
        json.dumps(
            {
                "status": "VALIDATED_ONLY_EXTERNAL_PROVIDER_EXECUTION_NOT_IMPLEMENTED",
                "plan_status": plan["status"],
                "plan_payload_sha256": plan["deterministic_payload_sha256"],
                "segment_count": plan["segment_count"],
                "external_audio_upload_authorized": plan["execution_contract"][
                    "external_audio_upload_authorized"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
