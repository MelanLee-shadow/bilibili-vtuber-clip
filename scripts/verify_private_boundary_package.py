#!/usr/bin/env python3
"""Verify one package-carried private boundary authority without publication."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.private_boundary_package import (
    PrivateBoundaryPackageError,
    verify_private_boundary_package,
    write_verification_receipt,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--clip-context", type=Path, required=True)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = verify_private_boundary_package(
            args.package_root,
            clip_context_path=args.clip_context,
            result_path=args.result,
        )
        if args.output is not None:
            write_verification_receipt(args.output, receipt)
    except PrivateBoundaryPackageError as exc:
        print(json.dumps({"status": "BLOCK", "code": exc.code, "detail": exc.detail}, ensure_ascii=False))
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
