#!/usr/bin/env python3
"""Plan or run a create-only supplemental CPA visual batch."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.cpa_frame_witness import image_vision_probe  # noqa: E402
from src.autoslice.cpa_visual_batch_resume import (  # noqa: E402
    CpaVisualBatchResumeError,
    build_visual_batch_resume_plan,
    execute_visual_batch_resume,
    write_create_only_json,
)


def _json(path: Path, *, code: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise CpaVisualBatchResumeError(code)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CpaVisualBatchResumeError(code) from exc
    if not isinstance(value, dict):
        raise CpaVisualBatchResumeError(code)
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--manifest", required=True, type=Path)
    plan.add_argument("--manifest-sha256", required=True)
    plan.add_argument("--prior-batch", required=True, type=Path)
    plan.add_argument("--prior-batch-sha256", required=True)
    plan.add_argument("--sheet-index", required=True, action="append", type=int)
    plan.add_argument("--out", required=True, type=Path)
    run = sub.add_parser("run")
    run.add_argument("--plan", required=True, type=Path)
    run.add_argument("--evidence-root", required=True, type=Path)
    run.add_argument("--image-root", required=True, type=Path)
    run.add_argument("--receipt-root", required=True, type=Path)
    run.add_argument("--timeout-seconds", type=float, default=240.0)
    run.add_argument("--max-tokens", type=int, default=3500)
    run.add_argument("--max-width", type=int, default=1920)
    run.add_argument("--out", required=True, type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.out.exists() or args.out.is_symlink():
            raise CpaVisualBatchResumeError("VISUAL_RESUME_OUTPUT_EXISTS")
        if args.command == "plan":
            result = build_visual_batch_resume_plan(
                manifest_path=args.manifest,
                manifest_sha256=args.manifest_sha256,
                prior_batch_path=args.prior_batch,
                prior_batch_sha256=args.prior_batch_sha256,
                requested_sheet_indices=args.sheet_index,
            )
        else:
            api_base = os.environ.get("CPA_BASE_URL", "").strip().rstrip("/")
            api_key = os.environ.get("CPA_API_KEY", "").strip()
            if not api_base or not api_key:
                raise CpaVisualBatchResumeError("CPA_CREDENTIALS_MISSING")
            plan = _json(args.plan, code="VISUAL_RESUME_PLAN_UNAVAILABLE")

            def probe(image_path: Path, question: str, *, model: str):
                return image_vision_probe(
                    image_path,
                    question,
                    api_base=api_base,
                    api_key=api_key,
                    model=model,
                    timeout_seconds=args.timeout_seconds,
                    max_tokens=args.max_tokens,
                    max_width=args.max_width,
                )

            result = execute_visual_batch_resume(
                plan=plan,
                evidence_root=args.evidence_root,
                image_root=args.image_root,
                receipt_root=args.receipt_root,
                probe=probe,
            )
        output = write_create_only_json(args.out, result)
    except CpaVisualBatchResumeError as exc:
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
