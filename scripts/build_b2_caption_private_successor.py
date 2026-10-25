#!/usr/bin/env python3
"""Build B2's hash-bound private corrected-caption successor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.b2_caption_successor import (
    B2CaptionSuccessorError,
    materialize_private_successor,
    sha256_file,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--predecessor-package", required=True, type=Path)
    value.add_argument("--corrected-srt", required=True, type=Path)
    value.add_argument("--source-attribution", required=True, type=Path)
    value.add_argument("--source-attribution-result", required=True, type=Path)
    value.add_argument("--authorization", required=True, type=Path)
    value.add_argument("--branding-intro", required=True, type=Path)
    value.add_argument("--out", required=True, type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        package = materialize_private_successor(
            predecessor_package=args.predecessor_package,
            corrected_srt=args.corrected_srt,
            attribution_path=args.source_attribution,
            result_path=args.source_attribution_result,
            authorization_path=args.authorization,
            intro_path=args.branding_intro,
            output=args.out,
        )
    except B2CaptionSuccessorError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    receipt = package / "b2-caption-private-successor.v1.json"
    print(
        json.dumps(
            {
                "status": "PASS",
                "package": str(package),
                "receipt": str(receipt),
                "receipt_sha256": sha256_file(receipt),
                "upload_performed": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
