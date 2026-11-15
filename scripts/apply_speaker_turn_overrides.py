#!/usr/bin/env python3
"""Apply reviewed speaker turns to a labelled SRT.

The important invariant is that an ASR cue is not a speaker boundary.  A single
source cue may therefore be replaced by two or more contiguous speaker turns.
Overrides are fail-closed against the source hash and the expected cue text and
timestamps so that a decision cannot silently drift onto another transcript.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.channel_profile import load_channel_profile

CHANNEL_PROFILE = load_channel_profile(ROOT)
HOST_SPEAKER = CHANNEL_PROFILE.host_speaker_label


SUBTITLE_BURN_STYLE_SCHEMA = "channel-subtitle-burn-style.v1"


def channel_burn_styles(profile=None) -> dict[str, str]:
    """{"host": ASS style value, "guest": ASS style value} of the channel; raises when the profile declares none."""
    profile = profile or CHANNEL_PROFILE
    path = profile.asset_file("subtitle_burn_style")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != SUBTITLE_BURN_STYLE_SCHEMA:
        raise ValueError(f"{path}: schema_version is not {SUBTITLE_BURN_STYLE_SCHEMA}")
    styles = {}
    for key in ("host", "guest"):
        value = document.get(key)
        if not isinstance(value, str) or len(value.split(",")) != 22 or "\n" in value:
            raise ValueError(f"{path}: {key} is not one ASS style value (22 comma-separated fields after the name)")
        styles[key] = value
    return styles


