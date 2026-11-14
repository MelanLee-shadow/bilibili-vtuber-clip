#!/usr/bin/env python3
"""Archive the current raw manifest and retain every registered predecessor.

Run after editing a profile, before validate_channel_profile.py. The validator
refuses an unregistered current version or missing/corrupt historical bytes.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.profile_manifest_history import archive_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    manifest = args.manifest if args.manifest.is_absolute() else ROOT / args.manifest
    print(archive_manifest(ROOT, manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
