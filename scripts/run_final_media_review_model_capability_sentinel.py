#!/usr/bin/env python3
"""Run or resume the autonomous dual-media capability sentinel."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.final_media_review_model_capability_sentinel import (
    FinalMediaReviewModelCapabilitySentinelError,
    run_model_capability_sentinel,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate real audio/video sentinel media, run or resume the "
            "runtime adapter, seal a successful result, and create the "
            "ordinary raw-AV capability manifest."
        )
    )
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--valid-for-seconds", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        result = run_model_capability_sentinel(
            runtime_root=args.runtime_root,
            run_id=args.run_id,
            valid_for_seconds=args.valid_for_seconds,
            runner_path=Path(__file__).resolve(),
        )
    except FinalMediaReviewModelCapabilitySentinelError as exc:
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
    status = result.get("status")
    if status == "CAPABILITY_BOOTSTRAPPED":
        return 0
    if status in {"PREPARED", "RETRY_WAIT", "SENTINEL_RUNTIME_ABSENT"}:
        return 75
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
