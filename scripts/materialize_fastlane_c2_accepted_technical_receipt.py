#!/usr/bin/env python3
"""Create-only C2 accepted technical receipt; never uploads or changes state."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.autoslice.fastlane_c2_technical_receipt import make_accepted_receipt, validate_accepted_receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--reviewed-at", required=True)
    parser.add_argument("--decision-basis", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    package, proposal, out = args.package.resolve(), args.proposal.resolve(), args.out.resolve()
    if out.exists() or out.is_symlink():
        raise SystemExit("refusing to overwrite accepted C2 technical receipt")
    receipt = make_accepted_receipt(package, proposal, args.reviewed_at, args.decision_basis)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(receipt, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(out.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    validate_accepted_receipt(package, proposal, json.loads(out.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
