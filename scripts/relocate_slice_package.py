#!/usr/bin/env python3
"""Relocate one frozen slice package to its current host paths."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.package_relocation import (  # noqa: E402
    PackageRelocationError,
    relocate_slice_package,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--source-package-root", required=True)
    parser.add_argument("--destination-package-root", required=True)
    parser.add_argument("--source-repo-root", required=True)
    parser.add_argument("--destination-repo-root", required=True)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument(
        "--source-workspace-root",
        default="/home/ivan/Project",
    )
    args = parser.parse_args(argv)
    try:
        receipt = relocate_slice_package(
            args.package_root,
            candidate_id=args.candidate,
            source_package_root=args.source_package_root,
            destination_package_root=args.destination_package_root,
            source_repo_root=args.source_repo_root,
            destination_repo_root=args.destination_repo_root,
            evidence_root=args.evidence_root,
            source_workspace_root=args.source_workspace_root,
        )
    except PackageRelocationError as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"passed": True, "receipt": receipt}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
