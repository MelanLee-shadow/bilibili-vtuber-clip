#!/usr/bin/env python3
"""Run the approved skill fetch_shabutton and seal its private image catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote

APPROVED_SCRIPT_SHA256 = "b32ebc7051e4bc6d854c86b6123f242a3d5ef66b9334083c72824412a2606709"
SITE = "https://lu-91015.github.io/shadowlee.github.io/assets/images/emotes/"


def prepare(skill: Path, out: Path) -> dict:
    script = skill / "scripts/fetch_shabutton.py"
    if (
        script.is_symlink()
        or hashlib.sha256(script.read_bytes()).hexdigest() != APPROVED_SCRIPT_SHA256
    ):
        raise ValueError("Approved fetch_shabutton.py source SHA mismatch")
    if any((parent / ".git").exists() for parent in (out, *out.parents)):
        raise ValueError("Downloaded emotes must stay outside Git")
    if any(parent.is_symlink() for parent in (out, *out.parents)):
        raise ValueError("Private emote output must not contain symlinks")
    if out.exists() and any(out.iterdir()):
        raise ValueError("Use an empty private output directory")
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(script), "--out", str(out)], check=True)
    rows = []
    for line in (out / "index.txt").read_text().splitlines():
        index, name = line.split("\t", 1)
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("Unsafe fetched filename")
        path = out / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Fetched emote missing or unsafe")
        rows.append(
            {
                "id": "shabutton:" + index,
                "file": name,
                "source_url": SITE + quote(name),
                "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    payload = {
        "schema_version": "lidousha-shabutton-assets.v1",
        "source_page": "https://lu-91015.github.io/shadowlee.github.io/",
        "fetch_script_sha256": "sha256:" + APPROVED_SCRIPT_SHA256,
        "entries": rows,
    }
    (out / "source-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    )
    return {
        "status": "PREPARED_PRIVATE_ASSETS",
        "count": len(rows),
        "manifest": str(out / "source-manifest.json"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.skill.absolute(), args.out.absolute())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
