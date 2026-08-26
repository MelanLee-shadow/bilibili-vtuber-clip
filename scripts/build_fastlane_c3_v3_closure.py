#!/usr/bin/env python3
"""Build one create-only C3 v3 closure from explicit verified byte stores."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.fastlane_c3_v3_direct import build_c3_v3_closure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v2-root", type=Path, required=True)
    parser.add_argument("--auxiliary-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    args = parser.parse_args()
    authority = build_c3_v3_closure(
        v2_root=args.v2_root,
        auxiliary_root=args.auxiliary_root,
        destination=args.destination,
        repo_root=args.repo_root,
    )
    print(json.dumps({
        "root": str(authority.root),
        "manifest_raw_sha256": authority.manifest_raw_sha256,
        "manifest_self_seal": authority.manifest_self_seal,
        "root_tree_sha256": authority.root_tree_sha256,
        "role_count": len(authority.roles),
        "provider_attempted": False,
        "upload_allowed": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
