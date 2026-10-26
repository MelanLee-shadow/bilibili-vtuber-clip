#!/usr/bin/env python3
"""Reject recording-root literals outside the canonical topology definition."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath

DEFINITION = Path("ops/recording/oci3-recording-topology.json")
SCOPES = ("src", "scripts", "ops", "config")
ROOT_KEYS = ("spool_root", "archive_root", "n100_archive_root", "backup_root")


def find_literals(root: Path) -> list[str]:
    definition = json.loads((root / DEFINITION).read_text(encoding="utf-8"))
    mount = PurePosixPath(definition["cloud_mount"])
    needles = {definition[key] for key in ROOT_KEYS}
    # Also catch rebuilding a root by appending a private relative suffix.
    for key in ROOT_KEYS[1:]:
        needles.add(PurePosixPath(definition[key]).relative_to(mount).as_posix())
    violations = []
    for scope in SCOPES:
        for path in sorted((root / scope).rglob("*")):
            if (not path.is_file() or path.is_symlink() or "__pycache__" in path.parts
                    or path.relative_to(root) == DEFINITION):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if any(value in line for value in needles):
                    violations.append(f"{path.relative_to(root)}:{number}")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    violations = find_literals(args.root)
    if violations:
        print("Recording root literals outside definition:\n" + "\n".join(violations))
        return 1
    print("Recording root literals outside definition: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
