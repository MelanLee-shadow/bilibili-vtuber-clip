#!/usr/bin/env python3
"""Build the deployed title editorial projection from the editable skill source.

The deploy installs the skill by the commit in config/title_skill_source.json, so building the deployed
projection also moves that pin to the commit the skill text was read from.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.autoslice.title_editorial_method import (
    TITLE_EDITORIAL_METHOD,
    extract_title_editorial_method,
    projection_data,
)


PIN = REPO_ROOT / "config" / "title_skill_source.json"


def committed_skill(skill_root: Path) -> str:
    """The commit `skill_root` is at; refuses skill text that no commit holds, because no deploy could install it."""
    def git(*args: str) -> str:
        done = subprocess.run(["git", "-C", str(skill_root), *args], capture_output=True, text=True)
        if done.returncode:
            raise SystemExit(f"{skill_root}: cannot pin the skill commit ({done.stderr.strip()})")
        return done.stdout.strip()

    if git("status", "--porcelain", "--", "."):
        raise SystemExit(f"{skill_root}: commit the skill before building the deployed projection from it")
    return git("rev-parse", "HEAD")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-root", type=Path, help="the method's source directory (default: the active "
                        "channel's own assets/<id>/title_method, which ships with the repository and needs no pin)")
    parser.add_argument("--output", type=Path, default=TITLE_EDITORIAL_METHOD)
    parser.add_argument("--pin", type=Path, help="pin file to move with the projection (default: the repo's pin "
                        "when --output is the deployed projection, none otherwise)")
    args = parser.parse_args()
    own = args.skill_root is None
    if own:
        args.skill_root = TITLE_EDITORIAL_METHOD.parent / "title_method"
    pin = args.pin or (PIN if not own and args.output.resolve() == TITLE_EDITORIAL_METHOD.resolve() else None)
    commit = committed_skill(args.skill_root) if pin else None
    method = extract_title_editorial_method(args.skill_root)
    args.output.write_text(json.dumps(projection_data(method), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if pin and commit:
        data = json.loads(pin.read_text(encoding="utf-8"))
        data["agent_toolkit_commit"] = commit
        pin.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
