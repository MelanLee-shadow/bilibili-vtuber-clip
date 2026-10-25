#!/usr/bin/env python3
"""Plan an explicit deployment-class persona epoch; dry-run by default.

The inventory is an operator-provided current snapshot, not an inferred empty
production set. Each entry reopens a receipt and names JSON pointers to the
asset's path and byte digest (e.g. cover identity dependencies). Opaque stage
digests alone cannot prove which persona bytes were bound and block apply.
--apply requires the SHA256 printed by dry-run and an explicit deployment epoch.
This command writes only the selected persona projection, never deploys code.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice import persona_review as review


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--ledger", type=Path, help="Default: profile persona_facts asset")
    parser.add_argument("--persona", type=Path, help="Default: profile persona asset")
    parser.add_argument("--inflight-inventory", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--deployment-epoch")
    args = parser.parse_args(argv)
    try:
        profile = load_channel_profile(args.repo_root, profile_id=args.profile_id, environ={})
        ledger = args.ledger or profile.asset_file("persona_facts")
        persona = args.persona or profile.asset_file("persona")
        if ledger.resolve() == persona.resolve():
            raise ValueError("Ledger and projection paths overlap")
        plan = review.promotion_plan(repo_root=args.repo_root, profile_id=args.profile_id,
            ledger_path=ledger, persona_path=persona, inventory_path=args.inflight_inventory)
        payload = asdict(plan)
        plan_sha = review.sha256(review.encode(payload))
        if args.apply:
            if (not args.expected_plan_sha256 or not args.deployment_epoch or args.inflight_inventory is None):
                raise ValueError("Apply requires --expected-plan-sha256, --deployment-epoch and --inflight-inventory")
            review.promote(plan=plan, expected_plan_sha256=args.expected_plan_sha256,
                deployment_epoch=args.deployment_epoch, repo_root=args.repo_root, ledger_path=ledger,
                persona_path=persona, inventory_path=args.inflight_inventory)
        print(review.encode({"mode": "APPLIED" if args.apply else "DRY_RUN", "plan_sha256": plan_sha,
            "plan": payload}).decode(), end="")
        return 0
    except (ValueError, OSError, TypeError) as exc:
        print(f"PERSONA_PROMOTION_INPUT_INVALID: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
