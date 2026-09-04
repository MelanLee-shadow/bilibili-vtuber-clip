#!/usr/bin/env python3
"""Recover the one committed Qixi dotfile cover namespace to a safe basename."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.qixi_post_correction_public_artifact_recovery import recover
from src.autoslice.qixi_post_correction_public_surface import (
    QixiPostCorrectionPublicSurfaceError,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit only the fixed, journal-bound legacy basename recovery",
    )
    args = parser.parse_args(argv)
    try:
        result = recover(apply=args.apply)
    except QixiPostCorrectionPublicSurfaceError as exc:
        print(json.dumps({"status": "RECOVERY_BLOCKED", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
