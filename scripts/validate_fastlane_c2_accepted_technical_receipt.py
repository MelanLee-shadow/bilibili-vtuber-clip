#!/usr/bin/env python3
"""Validate a C2-only accepted technical receipt without any external action."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.autoslice.fastlane_c2_technical_receipt import _regular, validate_accepted_receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    receipt = args.receipt.absolute()
    _regular(receipt)
    validate_accepted_receipt(args.package.absolute(), args.proposal.absolute(), json.loads(receipt.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
