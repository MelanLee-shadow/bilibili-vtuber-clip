"""The display limits a burned-in subtitle cue must fit: lines per cue, characters per line, longest static cue.

One definition for the package audit and for the subtitle splitter and renderer that must agree with it;
scripts/audit_review_package re-exports them."""
from __future__ import annotations

DEFAULT_MAX_VISUAL_LINES = 2
DEFAULT_MAX_VISUAL_LINE_CHARS = 18
LONG_STATIC_CUE_SECONDS = 10.0
