#!/usr/bin/env python3
"""render_ass.py SRT ASS [FONTSDIR] : ASS from a clean SRT via the deployed repo's own renderer (display-only projection).
Run on OCI3 with venv-main.  The repo is read-only; nothing is written into it.

Then every character the ASS shows must have a glyph in a font the burn can use (fontconfig's fonts and FONTSDIR):
a character no font draws is burned as an empty box (2026-11-15: 「南町的🔑」 read from a danmaku came out 「南町的□」).
Such characters are printed after GLYPH_MISSING and the tool exits 3, so step 5 stops instead of burning the box."""
import os
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
REPO_ENV = "MANUAL_LANE_REMOTE_REPO"   # the deployed repo, set by the lane (adapters.remote_python)
sys.path.insert(0, os.environ[REPO_ENV])
from src.autoslice.subtitle_rendering import (
    _write_sapphire_ass_from_srt,
)
from src.autoslice.subtitle_display_limits import DEFAULT_MAX_VISUAL_LINE_CHARS


def _ranges(charsets: str) -> list[tuple[int, int]]:
    out = []
    for item in charsets.split():
        low, _, high = item.partition("-")
        out.append((int(low, 16), int(high or low, 16)))
    return out


def drawable(fontsdir: Path | None) -> list[tuple[int, int]]:
    """The code points some font covers: fontconfig's fonts, and the fonts in `fontsdir` (libass loads those too)."""
    found = _ranges(subprocess.run(["fc-list", "--format", "%{charset}\n"], capture_output=True, text=True, check=True).stdout)
    for font in sorted(fontsdir.iterdir()) if fontsdir and fontsdir.is_dir() else []:
        if font.suffix.lower() in {".ttf", ".otf", ".ttc"}:
            found += _ranges(subprocess.run(["fc-query", "--format", "%{charset}\n", str(font)],
                                            capture_output=True, text=True).stdout)
    return found


def missing(ass_text: str, covered: list[tuple[int, int]]) -> list[str]:
    shown = set()
    for line in ass_text.splitlines():
        if line.startswith("Dialogue:"):
            text = re.sub(r"\{[^}]*\}", "", line.split(",", 9)[-1]).replace("\\N", "").replace("\\n", "").replace("\\h", "")
            shown.update(ch for ch in text if ord(ch) > 0x7E and not ch.isspace())
    return sorted(ch for ch in shown if not any(lo <= ord(ch) <= hi for lo, hi in covered)
                  and not (0xFE00 <= ord(ch) <= 0xFE0F or ord(ch) in (0x200D, 0x200B)))   # selectors and joiners draw nothing


if __name__ == "__main__":
    _write_sapphire_ass_from_srt(Path(sys.argv[1]), Path(sys.argv[2]),
                                         max_chars_per_line=DEFAULT_MAX_VISUAL_LINE_CHARS)
    print("ass written", sys.argv[2])
    lost = missing(Path(sys.argv[2]).read_text(encoding="utf-8"),
                   drawable(Path(sys.argv[3]) if len(sys.argv) > 3 else None))
    if lost:
        print("GLYPH_MISSING " + " ".join(f"{ch}(U+{ord(ch):04X})" for ch in lost))
        sys.exit(3)
