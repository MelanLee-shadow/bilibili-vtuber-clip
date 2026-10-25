#!/usr/bin/env python3
"""Prepare an explicit whole-final-media job or inspect its real raw-AV runtime.

Preparation never calls a provider. Runtime provisioning is create-only and
still requires the independent sentinel before any final-content claim.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    inspect = commands.add_parser("inspect-runtime")
    inspect.add_argument("--runtime-root", type=Path, required=True)
    configure = commands.add_parser("configure-api")
    configure.add_argument("--runtime-root", type=Path, required=True)
    configure.add_argument("--model", required=True)
    capability = commands.add_parser("test-capability")
    capability.add_argument("--runtime-root", type=Path, required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--spec", type=Path, required=True)
    prepare.add_argument("--package-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "inspect-runtime":
            from src.autoslice.requested_final_media_review import inspect_runtime
            result = inspect_runtime(args.runtime_root)
            code = 0 if result["status"] == "RAW_AV_CAPABILITY_READY" else 3
        elif args.action == "configure-api":
            from src.autoslice.final_media_review_gemini_api_runtime import ensure_gemini_api_sentinel_runtime
            from src.autoslice.final_media_review_capability_autobootstrap import validate_final_media_review_routes
            selected_environment = {
                **os.environ, "AUTOSLICE_FINAL_MEDIA_GEMINI_API_ENABLED": "1",
                "AUTOSLICE_FINAL_MEDIA_GEMINI_API_MODEL": args.model}
            validate_final_media_review_routes(selected_environment)
            result = ensure_gemini_api_sentinel_runtime(
                args.runtime_root, environment=selected_environment)
            result.update(content_review_status="NOT_PERFORMED", release_authorized=False)
            code = 0
        elif args.action == "test-capability":
            from src.autoslice.final_media_review_capability_autobootstrap import ensure_runtime_raw_av_capability
            from src.autoslice.requested_final_media_review import inspect_runtime
            result = ensure_runtime_raw_av_capability(args.runtime_root)
            result["inspection"] = inspect_runtime(args.runtime_root)
            result.update(content_review_status="NOT_PERFORMED", release_authorized=False)
            code = 0 if result["inspection"]["status"] == "RAW_AV_CAPABILITY_READY" else 3
        else:
            from src.autoslice.requested_final_media_review import build_request
            from src.autoslice.production_final_media_review_admission import bootstrap_production_final_media_review_job
            request = build_request(args.spec)
            result = bootstrap_production_final_media_review_job(args.package_root,
                request["candidate_id"], requested_review=request)
            result.update(content_review_status="UNASSESSED", release_authorized=False)
            code = 0 if result.get("job_path") else 3
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return code
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "BLOCKED",
                          "reason": getattr(exc, "reason_code", type(exc).__name__),
                          "detail": str(exc), "release_authorized": False},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
