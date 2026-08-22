#!/usr/bin/env python3
"""Private, no-target-write preparation for a reviewed-baseline replay.

``--stage`` is intentionally separate from the default plan: it writes only a
0600 manifest and candidate-private media/SRT artifacts beneath the caller's
private directory.  There is no ``--apply`` yet because a staged video/SRT is
not a complete final package and must not replace live package surfaces.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.reviewed_baseline_replay import (
    ReviewedBaselineReplayError,
    build_replay_plan,
    stage_replay,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--candidate-id", action="append", required=True)
    parser.add_argument("--stage-parent", type=Path)
    parser.add_argument("--stage", action="store_true")
    args = parser.parse_args(argv)
    if len(set(args.candidate_id)) != len(args.candidate_id):
        parser.error("candidate ids must be unique")
    if args.stage and args.stage_parent is None:
        parser.error("--stage requires --stage-parent")
    try:
        plans = [
            build_replay_plan(
                repo_root=args.repo_root, out_root=args.out_root,
                date=args.date, candidate_id=candidate_id,
            )
            for candidate_id in args.candidate_id
        ]
        result = {
            "schema_version": "reviewed-baseline-replay-plan.v1",
            "mode": "PRIVATE_STAGE" if args.stage else "DRY_RUN",
            "upload_allowed": False,
            "candidates": [
                ({
                    "candidate_id": plan.candidate_id,
                    "record_path": str(plan.record_path),
                    "padded_path": str(plan.padded_path),
                    "local_start_ms": plan.local_start_ms,
                    "local_end_ms": plan.local_end_ms,
                    "expected_video_sha256": plan.expected_video_sha256,
                    "predicate_matrix": list(plan.matrix),
                } | (
                    {"private_stage": stage_replay(plan, stage_parent=args.stage_parent)}
                    if args.stage else {}
                ))
                for plan in plans
            ],
        }
    except ReviewedBaselineReplayError as exc:
        print(json.dumps({"status": "REFUSED", "reason_code": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
