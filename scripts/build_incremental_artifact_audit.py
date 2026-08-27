#!/usr/bin/env python3
"""Build a create-only component-scoped review plan from two artifact versions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.incremental_artifact_audit import (  # noqa: E402
    ArtifactPaths,
    IncrementalArtifactAuditError,
    build_incremental_audit,
    build_video_edit_map,
    write_create_only,
)


def _window(raw: str, *, label: str) -> dict[str, object]:
    try:
        start, end = (int(value) for value in raw.split(":", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be START_MS:END_MS") from exc
    return {"start_ms": start, "end_ms": end}


def _point(raw: str) -> dict[str, object]:
    try:
        component, start, end = raw.split(":", 2)
        result = _window(f"{start}:{end}", label="change point")
    except ValueError as exc:
        raise ValueError("change point must be COMPONENT:START_MS:END_MS") from exc
    result["component"] = component
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--recording-date", required=True)
    parser.add_argument("--parent-authority-id", required=True)
    for prefix in ("parent", "current"):
        parser.add_argument(f"--{prefix}-record", type=Path, required=True)
        parser.add_argument(f"--{prefix}-video", type=Path, required=True)
        parser.add_argument(f"--{prefix}-subtitle", type=Path, required=True)
        parser.add_argument(f"--{prefix}-cover", type=Path, required=True)
        parser.add_argument(f"--{prefix}-boundary", type=Path)
        parser.add_argument(f"--{prefix}-title", type=Path)
    parser.add_argument("--video-window", action="append", default=[])
    parser.add_argument("--video-edit-map-id")
    parser.add_argument("--cover-roi", nargs=4, type=int)
    parser.add_argument("--change-point", action="append", default=[])
    parser.add_argument("--issue-count", type=int)
    parser.add_argument("--explicitly-exhaustive", action="store_true")
    parser.add_argument("--only-these-errors", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        parent = ArtifactPaths(
            args.parent_record,
            args.parent_video,
            args.parent_subtitle,
            args.parent_cover,
            args.parent_boundary,
            args.parent_title,
        )
        current = ArtifactPaths(
            args.current_record,
            args.current_video,
            args.current_subtitle,
            args.current_cover,
            args.current_boundary,
            args.current_title,
        )
        declared: dict[str, object] = {}
        if args.video_window:
            video_windows = [_window(raw, label="video window") for raw in args.video_window]
            declared["video"] = {"windows": video_windows}
            if args.video_edit_map_id:
                declared["video"] = build_video_edit_map(
                    parent_video=args.parent_video,
                    current_video=args.current_video,
                    edit_map_id=args.video_edit_map_id,
                    windows=video_windows,
                )
        if args.cover_roi:
            declared["cover"] = {"roi": args.cover_roi}
        plan = build_incremental_audit(
            parent=parent,
            current=current,
            parent_authority_id=args.parent_authority_id,
            candidate_id=args.candidate_id,
            recording_date=args.recording_date,
            declared_changes=declared,
            issue_count=args.issue_count,
            explicitly_exhaustive=args.explicitly_exhaustive,
            only_these_errors=args.only_these_errors,
            operator_change_points=[_point(raw) for raw in args.change_point],
        )
        write_create_only(args.out, plan)
    except (IncrementalArtifactAuditError, OSError, ValueError) as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "plan": str(args.out.resolve()),
                "plan_sha256": plan["plan_sha256"],
                "candidate_id": plan["candidate_id"],
                "recording_date": plan["recording_date"],
                "changed_components": plan["changed_components"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
