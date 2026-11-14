"""Fail-closed filler-removal planning for discontinuous talk clips.

Semantic recall may propose removals, but this module is the deterministic
authorization boundary.  It either returns ordered retained source intervals
with an auditable join map or falls back to the original contiguous interval.
"""

from __future__ import annotations


MIN_TALK_EFFECTIVE_DURATION_MS = 60_000
MIN_DEAD_PAUSE_MS = 3_000


