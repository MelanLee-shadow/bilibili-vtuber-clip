from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.cpa_semantic_qa import (
    build_mock_cpa_response,
    load_request_artifact,
    write_cpa_semantic_response_artifact,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a fail-closed CPA semantic QA response artifact.")
    parser.add_argument("--request", required=True, help="Path to cpa-semantic-review-request.v1 JSON")
    parser.add_argument("--response", required=True, help="Path to cpa-semantic-review-response.v1 JSON")
    parser.add_argument(
        "--mode",
        default="mock-local",
        choices=("mock-local",),
        help="Only offline/mock execution is enabled in this repo lane.",
    )
    args = parser.parse_args()

    request = load_request_artifact(Path(args.request))
    response = build_mock_cpa_response(request)
    if response.response_path != args.response:
        response = type(response)(**{**response.__dict__, "response_path": args.response, "request_path": request.request_path})
    write_cpa_semantic_response_artifact(response, Path(args.response))
    print(json.dumps({"ok": True, "mode": args.mode, "response_path": args.response}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
