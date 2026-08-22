#!/usr/bin/env python3
"""Create, but never run, one sealed historical autoslice ``--once`` authority.

The default output is a dry-run document.  ``--apply`` only writes a private
authority receipt; it neither invokes a provider nor calls the runner.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.recording.bililive_recorder_adapter import AdapterError, load_env_file, query_room_status  # noqa: E402
from src.autoslice.historical_fastlane_authority import (  # noqa: E402
    HistoricalFastlaneAuthorityError,
    create_historical_run_authority,
    prepare_historical_run_authority,
)
from src.autoslice.producer_delivery_transaction import deployment_authority_binding  # noqa: E402


def _direct_idle(*, endpoint: str, room: int, env_file: Path) -> bool:
    env = load_env_file(env_file)
    observed = query_room_status(
        endpoint, room,
        username=env.get("BREC_HTTP_BASIC_USER", ""),
        password=env.get("BREC_HTTP_BASIC_PASS", ""),
    )
    return observed.get("streaming") is False and observed.get("recording") is False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--recording-root", required=True, type=Path)
    parser.add_argument("--adapter-status-path", required=True, type=Path)
    parser.add_argument("--recorder-endpoint", required=True)
    parser.add_argument("--recorder-env", required=True, type=Path)
    parser.add_argument("--room", required=True, type=int)
    parser.add_argument("--date", required=True)
    parser.add_argument("--candidate-id", action="append", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--expected-state-sha256", required=True)
    parser.add_argument("--expected-deployed-commit", required=True)
    parser.add_argument("--expected-authority-manifest-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    authority = deployment_authority_binding(args.runtime_root)
    if authority["commit"] != args.expected_deployed_commit or authority["authority_manifest_sha256"] != args.expected_authority_manifest_sha256:
        print("HISTORICAL_FASTLANE_DEPLOYMENT_DRIFT", file=sys.stderr)
        return 2
    try:
        path, document = prepare_historical_run_authority(
            runtime_root=args.runtime_root, recording_root=args.recording_root,
            adapter_status_path=args.adapter_status_path, date=args.date,
            candidate_ids=tuple(args.candidate_id), nonce=args.nonce,
            expires_at=args.expires_at, expected_state_sha256=args.expected_state_sha256,
            expected_authority=authority,
            direct_recorder_idle=_direct_idle(endpoint=args.recorder_endpoint, room=args.room, env_file=args.recorder_env),
            room_id=args.room,
        )
        if args.apply:
            create_historical_run_authority(path, document)
        print(json.dumps({"dry_run": not args.apply, "authority_path": str(path), "authority": document}, ensure_ascii=False, sort_keys=True))
        return 0
    except (HistoricalFastlaneAuthorityError, AdapterError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
