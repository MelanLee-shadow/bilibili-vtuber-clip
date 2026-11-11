#!/usr/bin/env python3
"""render_ass.py SRT ASS : ASS from a clean SRT via the deployed repo's own renderer (display-only projection).
Run on OCI3 with venv-main.  The repo is read-only; nothing is written into it."""
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
REPO_ENV = "MANUAL_LANE_REMOTE_REPO"   # the deployed repo, set by the lane (adapters.remote_python)
sys.path.insert(0, os.environ[REPO_ENV])
from src.autoslice.subtitle_rendering import (
    _write_lidousha_sapphire_ass_from_srt,
)
from scripts.audit_lidousha_review_package import DEFAULT_MAX_VISUAL_LINE_CHARS

_write_lidousha_sapphire_ass_from_srt(Path(sys.argv[1]), Path(sys.argv[2]),
                                     max_chars_per_line=DEFAULT_MAX_VISUAL_LINE_CHARS)
print("ass written", sys.argv[2])
