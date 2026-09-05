#!/usr/bin/env python3
"""Materialize C1's one candidate-locked private formal review package.

This command has no candidate, text, upload, or Bilibili mutation flag.  It
can only replay ``auto_173005_934_1166`` through the fixed C1 authority and
then writes a current local package-audit result beside the package.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_review_package import audit_package  # noqa: E402
from src.autoslice.fastlane_c1_formal_adapter import (  # noqa: E402
    FastlaneC1FormalAdapterError,
    materialize_formal_private_package,
    sha256_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predecessor-dir", required=True, type=Path)
    parser.add_argument("--predecessor-package", required=True, type=Path)
    parser.add_argument("--branding-intro", required=True, type=Path)
    parser.add_argument("--public-identity", required=True, type=Path)
    parser.add_argument("--claude-jsonl", required=True, type=Path)
    parser.add_argument("--ruling-document", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        package = materialize_formal_private_package(
            predecessor_dir=args.predecessor_dir,
            predecessor_package=args.predecessor_package,
            branding_intro=args.branding_intro,
            public_identity=args.public_identity,
            claude_jsonl=args.claude_jsonl,
            ruling_document=args.ruling_document,
            out=args.out,
        )
    except FastlaneC1FormalAdapterError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    audit = audit_package(package)
    audit_path = package / "package_audit.json"
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    result = {
        "package": str(package),
        "package_audit": str(audit_path),
        "package_audit_sha256": sha256_file(audit_path),
        "audit_passed": audit["passed"],
        "root_technical_receipt_created": False,
        "upload_performed": False,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if audit["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
