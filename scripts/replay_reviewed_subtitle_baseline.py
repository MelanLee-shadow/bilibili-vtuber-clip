#!/usr/bin/env python3
"""Prepare a sealed private reviewed-baseline replay, never an upload.

The default is a no-target-write plan.  ``--stage`` adds only a private
candidate stage.  The package transaction deliberately has no operator-supplied
target/state arguments: only the lane's sealed after-image builder may name
delivery, record, publish, and state surfaces.
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
        staged: dict[str, object] = {}
        candidates = []
        for plan in plans:
            item: dict[str, object] = {
                "candidate_id": plan.candidate_id,
                "record_path": str(plan.record_path),
                "padded_path": str(plan.padded_path),
                "local_start_ms": plan.local_start_ms,
                "local_end_ms": plan.local_end_ms,
                "expected_video_sha256": plan.expected_video_sha256,
                # Full finalization cannot silently be claimed from baseline
                # text alone.  A source-fact receipt is the only remaining
                # provider-bearing prerequisite, so it is explicit rather
                # than a misleading PENDING state.
                "predicate_matrix": [
                    *list(plan.matrix[:4]),
                    {"predicate": "SOURCE_FACT_REVIEW", "status": "NEEDS_PROVIDER"},
                    {"predicate": "PACKAGE_AFTER_IMAGE", "status": "BLOCKED_BY_SOURCE_FACT"},
                    {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"},
                ],
            }
            if args.stage:
                private = stage_replay(plan, stage_parent=args.stage_parent)
                item["private_stage"] = private
                staged[plan.candidate_id] = private
            candidates.append(item)
        result: dict[str, object] = {
            "schema_version": "reviewed-baseline-replay-plan.v1",
            "mode": "PRIVATE_STAGE" if args.stage else "DRY_RUN",
            "upload_allowed": False,
            "candidates": candidates,
        }
    except ReviewedBaselineReplayError as exc:
        print(json.dumps({"status": "REFUSED", "reason_code": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
