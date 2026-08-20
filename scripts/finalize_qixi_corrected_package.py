#!/usr/bin/env python3
"""Create-only Qixi Z2 package finalizer; dry-run unless --apply is supplied."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.qixi_corrected_package_finalization import (  # noqa: E402
    QixiCorrectedPackageError,
    finalize,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument(
        "--target",
        required=True,
        type=Path,
        help="absent candidate root; package is created at TARGET/replacement_recuts",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = finalize(
            repo_root=ROOT,
            release_root=args.release_root,
            evidence_root=args.evidence_root,
            target=args.target,
            apply=args.apply,
        )
    except QixiCorrectedPackageError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
