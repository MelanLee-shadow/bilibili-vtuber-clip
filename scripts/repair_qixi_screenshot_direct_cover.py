#!/usr/bin/env python3
"""Fixed screenshot-direct repair entrypoint for auto_123655_771_844."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.qixi_screenshot_direct_cover_repair import (  # noqa: E402
    QixiScreenshotDirectCoverRepairError,
    load_authority,
)


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--full-dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    try:
        authority = load_authority(ROOT)
        if args.plan or not (args.full_dry_run or args.apply):
            print(json.dumps({"status": "PLAN_PASS", "candidate_id": authority["candidate_id"], "upload_enabled": False}, ensure_ascii=False, sort_keys=True))
            return 0
        # Provider staging and transaction consumption are intentionally not
        # inferred from a CLI flag.  Their fixed adapters are supplied by the
        # sealed runtime closure, never user paths/text/provider parameters.
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_FIXED_ADAPTERS_REQUIRED")
    except QixiScreenshotDirectCoverRepairError as exc:
        print(json.dumps({"status": "BLOCKED", "reason_code": str(exc), "upload_enabled": False}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
