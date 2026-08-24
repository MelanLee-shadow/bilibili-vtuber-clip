#!/usr/bin/env python3
"""Run the sealed C2 replay only in its no-upload private authority mode."""
from __future__ import annotations

import sys

from scripts import replay_reviewed_subtitle_baseline as replay
from src.autoslice.fastlane_c2_private_authority import (
    C2_CANDIDATE_ID,
    C2_RECORDING_DATE,
    resolve_c2_private_record_authority,
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--apply" in args:
        raise SystemExit("C2_PRIVATE_REPLAY_APPLY_FORBIDDEN")
    if args.count("--candidate-id") != 1 or args[args.index("--candidate-id") + 1] != C2_CANDIDATE_ID:
        raise SystemExit("C2_PRIVATE_REPLAY_CANDIDATE_INVALID")
    if "--date" not in args or args[args.index("--date") + 1] != C2_RECORDING_DATE:
        raise SystemExit("C2_PRIVATE_REPLAY_DATE_INVALID")
    return replay.main(args, _record_authority_resolver=resolve_c2_private_record_authority)


if __name__ == "__main__":
    raise SystemExit(main())
