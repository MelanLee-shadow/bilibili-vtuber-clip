#!/usr/bin/env python3
"""Create-only C2 accepted technical receipt; never uploads or changes state."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.autoslice.fastlane_c2_technical_receipt import make_accepted_receipt, validate_accepted_receipt, write_create_only_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--reviewed-at", required=True)
    parser.add_argument("--decision-basis", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    package, proposal, out = args.package.absolute(), args.proposal.absolute(), args.out.absolute()
    receipt = make_accepted_receipt(package, proposal, args.reviewed_at, args.decision_basis)
    validate_accepted_receipt(package, proposal, receipt)
    write_create_only_json(out, receipt)
    validate_accepted_receipt(package, proposal, json.loads(out.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
