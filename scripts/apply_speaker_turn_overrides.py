#!/usr/bin/env python3
"""Apply reviewed speaker turns to a labelled SRT.

The important invariant is that an ASR cue is not a speaker boundary.  A single
source cue may therefore be replaced by two or more contiguous speaker turns.
Overrides are fail-closed against the source hash and the expected cue text and
timestamps so that a decision cannot silently drift onto another transcript.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.channel_profile import load_channel_profile

CHANNEL_PROFILE = load_channel_profile(ROOT)
HOST_SPEAKER = CHANNEL_PROFILE.host_speaker_label


LDS_SAPPHIRE_STYLE = (
    "Microsoft YaHei,72,&H00FFFFFF,&H000000FF,&H00BA520F,&H70000000,"
    "0,0,0,0,100,100,0,0,1,3,2,2,60,60,40,1"
)
GUEST_WHITE_STYLE = (
    "Microsoft YaHei,72,&H00FFFFFF,&H000000FF,&H00203050,&H70000000,"
    "-1,0,0,0,100,100,0,0,1,3,2,2,60,60,40,1"
)


