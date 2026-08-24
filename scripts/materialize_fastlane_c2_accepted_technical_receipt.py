#!/usr/bin/env python3
"""Create-only C2 accepted technical receipt; never uploads or changes state."""
from __future__ import annotations

import argparse
import json
import subprocess
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
    package, proposal, out = args.package.resolve(), args.proposal.resolve(), args.out.resolve()
    run = subprocess.run([sys.executable, str(ROOT / "scripts/audit_lidousha_review_package.py"), "--json", str(package)], capture_output=True, text=True, check=False)
    try:
        current, saved = json.loads(run.stdout), json.loads((package / "package_audit.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise SystemExit(f"C2_AUDIT_REPLAY_DRIFT: {exc}") from exc
    if run.returncode or current != saved or current.get("passed") is not True or current.get("blocking_issue_count") != 0:
        raise SystemExit("C2_AUDIT_REPLAY_DRIFT")
    receipt = make_accepted_receipt(package, proposal, args.reviewed_at, args.decision_basis)
    write_create_only_json(out, receipt)
    validate_accepted_receipt(package, proposal, json.loads(out.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
