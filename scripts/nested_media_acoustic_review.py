#!/usr/bin/env python3
"""Create or consume an exact nested-media acoustic-review batch."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.nested_media_acoustic_review import (  # noqa: E402
    NestedMediaAcousticReviewError,
    build_acoustic_review_plan,
    consume_acoustic_review_batch,
    write_create_only,
)


def _json(path: Path, *, label: str) -> object:
    if path.is_symlink() or not path.is_file():
        raise NestedMediaAcousticReviewError(f"{label.upper()}_UNAVAILABLE")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NestedMediaAcousticReviewError(f"{label.upper()}_INVALID") from exc


def _list_payload(value: object, *, key: str, label: str) -> list[dict[str, object]]:
    if isinstance(value, Mapping):
        value = value.get(key)
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise NestedMediaAcousticReviewError(f"{label.upper()}_INVALID")
    return [dict(item) for item in value]


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="freeze the exact visual observation set")
    plan.add_argument("--candidate-id", required=True)
    plan.add_argument("--source-media", required=True, type=Path)
    plan.add_argument("--source-media-sha256", required=True)
    plan.add_argument("--final-srt", required=True, type=Path)
    plan.add_argument("--final-srt-sha256", required=True)
    plan.add_argument("--observations", required=True, type=Path)
    plan.add_argument("--observations-sha256", required=True)
    plan.add_argument("--observation-list-key", default="observations")
    plan.add_argument("--lead-lag-ms", type=int, default=1_800)
    plan.add_argument("--max-span-cues", type=int, default=3)
    plan.add_argument("--fuzzy-threshold", type=float, default=0.72)
    plan.add_argument("--out", required=True, type=Path)

    consume = sub.add_parser("consume", help="consume partial or complete receipts")
    consume.add_argument("--plan", required=True, type=Path)
    consume.add_argument("--receipts", required=True, type=Path)
    consume.add_argument("--out", required=True, type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = build_acoustic_review_plan(
                candidate_id=args.candidate_id,
                source_media_path=args.source_media,
                source_media_sha256=args.source_media_sha256,
                final_srt_path=args.final_srt,
                final_srt_sha256=args.final_srt_sha256,
                observation_manifest_path=args.observations,
                observation_manifest_sha256=args.observations_sha256,
                observation_list_key=args.observation_list_key,
                lead_lag_ms=args.lead_lag_ms,
                max_span_cues=args.max_span_cues,
                fuzzy_threshold=args.fuzzy_threshold,
            )
        else:
            plan = _json(args.plan, label="plan")
            if not isinstance(plan, Mapping):
                raise NestedMediaAcousticReviewError("PLAN_INVALID")
            bindings = _list_payload(
                _json(args.receipts, label="receipts"),
                key="bindings",
                label="receipts",
            )
            result = consume_acoustic_review_batch(
                plan=dict(plan), acoustic_receipt_bindings=bindings
            )
        output = write_create_only(args.out, result)
    except NestedMediaAcousticReviewError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": result["status"],
                "complete": result.get("complete", False),
                "out": str(output),
                "mutation_authorized": False,
                "publication_authority": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
