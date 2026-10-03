#!/usr/bin/env python3
"""Plan or apply a zero-provider journaled canonical QC receipt recovery."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, ".")

from src.autoslice.title_cover_qc_receipt_recovery import (
    TitleCoverQcRecoveryError,
    plan_title_cover_qc_receipt_recovery,
    recover_title_cover_qc_receipt,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-package", type=Path, required=True)
    parser.add_argument("--source-receipt", type=Path, required=True)
    parser.add_argument("--destination-package", type=Path, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="persist archive/plan/journal and atomically install the successor",
    )
    args = parser.parse_args(argv)
    try:
        if args.apply:
            result = recover_title_cover_qc_receipt(
                source_package_root=args.source_package,
                source_receipt_path=args.source_receipt,
                destination_package_root=args.destination_package,
                title=args.title,
            )
        else:
            result = plan_title_cover_qc_receipt_recovery(
                source_package_root=args.source_package,
                source_receipt_path=args.source_receipt,
                destination_package_root=args.destination_package,
                title=args.title,
            )
    except (TitleCoverQcRecoveryError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                    "provider_calls": 0,
                    "upload_calls": 0,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
