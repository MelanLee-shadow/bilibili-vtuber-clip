#!/usr/bin/env python3
"""Run the sealed C2 replay only in its no-upload private authority mode."""
from __future__ import annotations

import sys
from pathlib import Path

from scripts import replay_reviewed_subtitle_baseline as replay
from src.autoslice.fastlane_c2_private_authority import (
    C2_CANDIDATE_ID,
    C2_RECORDING_DATE,
    classify_c2_private_path_unavailable,
    materialize_c2_private_provenance,
    resolve_c2_private_record_authority,
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    def required_value(flag: str, error: str) -> str:
        try:
            index = args.index(flag)
            value = args[index + 1]
        except (ValueError, IndexError):
            raise SystemExit(error) from None
        if value.startswith("--"):
            raise SystemExit(error)
        return value
    if "--apply" in args:
        raise SystemExit("C2_PRIVATE_REPLAY_APPLY_FORBIDDEN")
    if args.count("--candidate-id") != 1 or required_value("--candidate-id", "C2_PRIVATE_REPLAY_CANDIDATE_INVALID") != C2_CANDIDATE_ID:
        raise SystemExit("C2_PRIVATE_REPLAY_CANDIDATE_INVALID")
    if args.count("--date") != 1 or required_value("--date", "C2_PRIVATE_REPLAY_DATE_INVALID") != C2_RECORDING_DATE:
        raise SystemExit("C2_PRIVATE_REPLAY_DATE_INVALID")
    # This is a C2-only private-runtime materialization, deliberately outside
    # the generic no-write PLAN path.  It makes the copied predecessor's five
    # permitted provenance locators current before the generic directory guard
    # evaluates the candidate during full dry-run.
    if "--full-dry-run" in args:
        runtime_root = Path(required_value("--runtime-root", "C2_PRIVATE_REPLAY_RUNTIME_INVALID"))
        try:
            materialize_c2_private_provenance(
                runtime_root=runtime_root, candidate_id=C2_CANDIDATE_ID,
                date=C2_RECORDING_DATE,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    return replay.main(
        args,
        _record_authority_resolver=resolve_c2_private_record_authority,
        _path_unavailable_diagnostic=classify_c2_private_path_unavailable,
    )


if __name__ == "__main__":
    raise SystemExit(main())
