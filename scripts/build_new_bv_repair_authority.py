#!/usr/bin/env python3
"""Build the same-BV repair authority of a normally published BV (read-only; writes only --out).

The authority is derived from the first publication's
``<manifest>.publication_reconciliation.json`` and the successor title.  It grants nothing by
itself: ``scripts/finished_media_review.py check/seal --repair-authority`` writes it into a
finished-media review package, and ``authorized_upload.py repair-plan`` replays the whole
first-publication closure before any remote step.  Run it where the evidence files are readable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.new_bv_repair_authority import (
    NewBvRepairAuthorityError,
    build_new_bv_repair_authority,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--publication", required=True, help="<manifest>.publication_reconciliation.json (absolute)")
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--title-file", required=True, help="the successor title.txt")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = Path(args.out)
    if out.exists():
        print(f"REFUSE: output already exists: {out}", file=sys.stderr)
        return 2
    title = Path(args.title_file).read_text(encoding="utf-8").strip()
    try:
        authority = build_new_bv_repair_authority(
            Path(args.publication), candidate_id=args.candidate_id, title=title)
    except (NewBvRepairAuthorityError, OSError, ValueError) as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    out.write_text(json.dumps(authority, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
