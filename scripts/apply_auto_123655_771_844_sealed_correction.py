#!/usr/bin/env python3
"""Replay the single sealed 2026-08-17 女友感 subtitle correction.

There are deliberately no candidate, path, title, or subtitle-text arguments.
The deployment-sealed authority owns all of those values.  ``--dry-run`` is
zero-write; ``--apply`` delegates exactly once to the existing staged
subtitle/reburn transaction.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.apply_subtitle_correction as correction  # noqa: E402
from src.autoslice.sealed_subtitle_correction import (  # noqa: E402
    CANDIDATE_ID,
    RECORDING_DATE,
    SealedSubtitleCorrectionError,
    SealedSubtitleCorrectionTransactionContext,
    load_deployed_authority,
    validate_post_transaction,
    validate_runtime,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def _transaction_argv(authority: dict[str, object]) -> list[str]:
    correction_plan = authority["correction"]
    delivery = authority["delivery"]
    assert isinstance(correction_plan, dict) and isinstance(delivery, dict)
    arguments = [
        "--cid",
        CANDIDATE_ID,
        "--date",
        RECORDING_DATE,
        "--delivery",
        str(delivery["video"]["path"]),
    ]
    for row in correction_plan["replacements"]:
        assert isinstance(row, dict)
        arguments.extend(["--set-line", f"{row['cue_index']}={row['after']}"])
    return arguments


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if os.environ.get("AUTOSLICE_SPEAKER_MODE") != "uniform_host":
        print("SEALED_CORRECTION_BLOCKED: AUTOSLICE_SPEAKER_MODE must be uniform_host", file=sys.stderr)
        return 2
    try:
        authority, seal = load_deployed_authority(ROOT)
        expected_srt = validate_runtime(authority, repo_root=ROOT)
    except SealedSubtitleCorrectionError as exc:
        print(f"SEALED_CORRECTION_PREFLIGHT_FAILED: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        correction_plan = authority["correction"]
        assert isinstance(correction_plan, dict)
        print(
            json.dumps(
                {
                    "schema_version": "sealed-subtitle-correction-dry-run.v1",
                    "status": "PASS",
                    "candidate_id": CANDIDATE_ID,
                    "recording_date": RECORDING_DATE,
                    "authority_repository_seal": seal,
                    "before_srt_sha256": correction_plan["source_srt_sha256"],
                    "after_srt_sha256": correction_plan["output_srt_sha256"],
                    "set_line_operations": [
                        f"{row['cue_index']}={row['after']}"
                        for row in correction_plan["replacements"]
                        if isinstance(row, dict)
                    ],
                    "upload_enabled": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    result = correction.main(
        _transaction_argv(authority),
        _sealed_transaction_context=SealedSubtitleCorrectionTransactionContext(ROOT),
    )
    if result != 0:
        return result
    try:
        validate_post_transaction(authority, expected_srt=expected_srt, repo_root=ROOT)
    except SealedSubtitleCorrectionError as exc:
        print(f"SEALED_CORRECTION_POSTCHECK_FAILED: {exc}", file=sys.stderr)
        return 1
    print("SEALED_CORRECTION_APPLIED: upload remains disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
