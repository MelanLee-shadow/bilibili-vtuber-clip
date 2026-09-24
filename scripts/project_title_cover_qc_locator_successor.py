#!/usr/bin/env python3
"""Create a zero-provider target-bound successor for an existing joint-QC PASS."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from src.autoslice.title_cover_qc_locator_successor import (
    create_title_cover_qc_locator_successor,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-package", type=Path, required=True)
    parser.add_argument("--source-receipt", type=Path, required=True)
    parser.add_argument("--destination-package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args(argv)
    receipt = create_title_cover_qc_locator_successor(
        source_package_root=args.source_package,
        source_receipt_path=args.source_receipt,
        destination_package_root=args.destination_package,
        output_path=args.output,
        title=args.title,
    )
    result = {
        "status": "PASS_ZERO_PROVIDER_QC_LOCATOR_SUCCESSOR",
        "output": str(args.output.absolute()),
        "output_sha256": "sha256:" + hashlib.sha256(args.output.read_bytes()).hexdigest(),
        "candidate_id": receipt["candidate_id"],
        "cover_sha256": receipt["cover_sha256"],
        "provider_calls": 0,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
