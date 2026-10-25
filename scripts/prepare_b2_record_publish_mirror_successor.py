#!/usr/bin/env python3
"""Plan or create a private B2 record/publish hash-mirror successor."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, ".")

from src.autoslice.b2_record_publish_mirror_successor import (
    B2RecordPublishMirrorSuccessorError,
    plan_successor,
    prepare_successor,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = (
            prepare_successor(args.source_package, args.output)
            if args.apply
            else plan_successor(args.source_package, args.output)
        )
    except (B2RecordPublishMirrorSuccessorError, OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                    "provider_calls": 0,
                    "package_audit_calls": 0,
                    "mechanical_review_calls": 0,
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
