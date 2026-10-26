#!/usr/bin/env python3
"""Prepare an immutable batch or apply 维护者's hash-bound reply; offline only.

prepare does not write the ledger. review defaults to dry-run; --apply appends
review revisions after checking the exact original ledger prefix/preimage.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.autoslice import persona_facts as pf
from src.autoslice import persona_review as review
from src.autoslice.asset_write_bindings import asset_binding_surfaces


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Make batch.json + review-summary.md + receipt copy")
    prepare.add_argument("--extraction-receipt", required=True, type=Path)
    prepare.add_argument("--output-dir", required=True, type=Path, help="New directory; never overwritten")
    prepare.add_argument("--recorded-at", required=True)
    apply = commands.add_parser("review", help="Verify message and batch; default dry-run")
    apply.add_argument("--batch-dir", required=True, type=Path)
    apply.add_argument("--authority", required=True, type=Path, help="Exact HumanAuthorityEvidence JSON")
    apply.add_argument("--raw-message", required=True, type=Path, help="Original UTF-8 bytes, including newline")
    apply.add_argument("--apply", action="store_true")
    for command in (prepare, apply):
        command.add_argument("--ledger", required=True, type=Path)
        command.add_argument("--profile-id", required=True)
    args = parser.parse_args(argv)
    try:
        before = args.ledger.read_bytes()
        if args.command == "prepare":
            batch, summary = review.prepare_batch(args.ledger, args.extraction_receipt,
                profile_id=args.profile_id, recorded_at=args.recorded_at)
            receipt = args.extraction_receipt.read_bytes()
            if review.sha256(receipt) != batch.extraction_receipt_sha256 or args.ledger.read_bytes() != before:
                raise ValueError("Preparation input drift")
            args.output_dir.mkdir(parents=True, exist_ok=False)
            (args.output_dir / "extraction-receipt.json").write_bytes(receipt)
            (args.output_dir / "review-summary.md").write_bytes(summary)
            # Published last: an interrupted directory is not an eligible batch.
            (args.output_dir / "batch.json").write_bytes(review.encode(asdict(batch)))
            print(review.encode(asdict(batch)).decode(), end="")
            return 0
        authority = cast(pf.HumanAuthorityEvidence, pf._decode(review.document(args.authority), pf.HumanAuthorityEvidence))
        original = pf.load_persona_fact_ledger(args.ledger, profile_id=args.profile_id)
        candidate = review.review_candidate(args.ledger, args.batch_dir,
            authority=authority, raw_message=args.raw_message.read_bytes())
        if candidate.profile_id != args.profile_id or args.ledger.read_bytes() != before:
            raise ValueError("Ledger profile/preimage drift")
        pf.verify_append_only(original, candidate)
        if args.apply:
            if asset_binding_surfaces("persona_facts", repo_root=ROOT, profile_id=args.profile_id):
                raise ValueError("Ledger now has binding surfaces; deployment-class change required")
            protected_inputs = [args.authority, args.raw_message, *args.batch_dir.iterdir()]
            if any(path.resolve() == args.ledger.resolve() for path in protected_inputs):
                raise ValueError("Ledger overlaps review inputs")
            review.atomic_compare_write(args.ledger, before=before, after=review.encode(asdict(candidate)))
        print(review.encode({"mode": "APPLIED" if args.apply else "DRY_RUN", "append_only": True,
            "before_sha256": review.sha256(before),
            "after_sha256": review.sha256(review.encode(asdict(candidate))),
            "appended_revisions": [asdict(row) for row in candidate.revisions[len(original.revisions):]]}).decode(), end="")
        return 0
    except (ValueError, OSError, TypeError) as exc:
        print(f"PERSONA_REVIEW_INPUT_INVALID: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
