#!/usr/bin/env python3
"""Prepare one hash-bound completed Song selector summary.

This entry point is intentionally smaller than the unattended runner.  It
consumes an existing selector summary, creates the ordinary private Song
delivery transaction, and writes one create-only result envelope.  The result
contains intended targets and a prepared handle; it is not autoslice state and
does not expose a canonical delivery result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selector-summary", required=True, type=Path)
    parser.add_argument("--selector-summary-sha256", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument(
        "--base",
        type=Path,
        default=Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice")),
        help="runtime BASE; defaults to AUTOSLICE_BASE or /opt/bilive/autoslice",
    )
    args = parser.parse_args()

    # The runner proxy resolves this module after import.  Pin the runtime base
    # before importing it so every ordinary helper uses the same authority root.
    os.environ["AUTOSLICE_BASE"] = str(args.base)
    try:
        import scripts.session_autoslice  # noqa: F401
        from src.autoslice.song_delivery import prepare_song_from_selector_summary

        result = prepare_song_from_selector_summary(
            selector_summary=args.selector_summary,
            selector_summary_sha256=args.selector_summary_sha256,
            candidate_id=args.candidate_id,
            date=args.date,
            result_path=args.result,
        )
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
        print(f"REFUSE: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "status": result.get("status"),
                "candidate_id": result.get("candidate_id"),
                "date": result.get("date"),
                "selector_summary_path": result.get("selector_summary_path"),
                "selector_summary_sha256": result.get("selector_summary_sha256"),
                "result": str(args.result.resolve()),
                "prepared_manifest": (
                    result.get("prepared_delivery", {}).get("manifest_path")
                    if isinstance(result.get("prepared_delivery"), dict)
                    else None
                ),
                "prepared_sha256": (
                    result.get("prepared_delivery", {}).get("prepared_sha256")
                    if isinstance(result.get("prepared_delivery"), dict)
                    else None
                ),
                "upload_enabled": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
