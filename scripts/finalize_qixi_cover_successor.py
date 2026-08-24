#!/usr/bin/env python3
"""Create a single, private Qixi CPA-cover successor review package."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from src.autoslice.qixi_cover_successor_finalization import QixiCoverSuccessorError, finalize  # noqa: E402

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preimage", required=True, type=Path)
    parser.add_argument("--trial-root", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--punch-response",
        type=Path,
        help="private CPA response used by the canonical punch semantic reviewer",
    )
    args = parser.parse_args(argv)
    try:
        response = (
            args.punch_response.read_text(encoding="utf-8")
            if args.punch_response is not None
            else None
        )
        result = finalize(
            repo_root=ROOT,
            preimage=args.preimage,
            trial_root=args.trial_root,
            target=args.target,
            apply=args.apply,
            punch_response=response,
        )
    except QixiCoverSuccessorError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr); return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)); return 0

if __name__ == "__main__": raise SystemExit(main())
