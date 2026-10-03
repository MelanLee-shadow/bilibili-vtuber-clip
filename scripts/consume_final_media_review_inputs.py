#!/usr/bin/env python3
"""Consume one hash-bound final-media perceptual-input job.

This is a normal pipeline entry point: it resolves a package-local raw-AV
capability successor when a verified runtime manifest exists, persists
provider backoff and terminal state, and never opens a browser or delegates an
HTML form to the human operator. Missing capability remains a typed, provider-free block.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.final_media_review_capability_autobootstrap import (
    CAPABILITY_BOOTSTRAP_STOP_STATUSES,
    CAPABILITY_BOOTSTRAP_WAIT_STATUSES,
    FinalMediaReviewCapabilityAutobootstrapError,
    ensure_runtime_raw_av_capability,
)
from src.autoslice.final_media_review_inputs import (
    FinalMediaReviewInputError,
    consume_review_job,
)
from src.autoslice.final_media_review_raw_av import (
    FinalMediaReviewRawAvError,
    bind_review_job_to_runtime_capability,
)


def _package_root(job_path: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().absolute()
    try:
        resolved = job_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FinalMediaReviewRawAvError(
            "FINAL_MEDIA_REVIEW_PACKAGE_ROOT_INVALID",
            f"review job is unavailable: {job_path}",
        ) from exc
    if resolved.parent.name != "verification":
        raise FinalMediaReviewRawAvError(
            "FINAL_MEDIA_REVIEW_PACKAGE_ROOT_INVALID",
            "--package-root is required when the job is not directly under verification/",
        )
    return resolved.parent.parent


def _runtime_root(argument: Path | None) -> Path | None:
    selected: str | Path | None = argument
    if selected is None:
        selected = (
            os.environ.get("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT")
            or os.environ.get("AUTOSLICE_BASE")
            or None
        )
    return Path(selected).expanduser().absolute() if selected else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Consume a final-media perceptual-review input job."
    )
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument(
        "--package-root",
        type=Path,
        help=(
            "Package authority root. By default, derive it from a job directly "
            "under package/verification/."
        ),
    )
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--runtime-ssh-host")
    args = parser.parse_args(argv)

    try:
        package_root = _package_root(args.job, args.package_root)
        runtime_root = _runtime_root(args.runtime_root)
        active_job = args.job
        capability_bootstrap: dict[str, object] | None = None
        capability_binding: dict[str, object] | None = None
        if runtime_root is not None:
            capability_bootstrap = ensure_runtime_raw_av_capability(runtime_root)
            bootstrap_status = capability_bootstrap.get("status")
            if bootstrap_status in CAPABILITY_BOOTSTRAP_WAIT_STATUSES:
                print(
                    json.dumps(
                        {
                            "capability_bootstrap": capability_bootstrap,
                            "capability_binding": None,
                            "state": None,
                            "cache_reused": False,
                            "provider_called": capability_bootstrap.get(
                                "provider_calls", 0
                            )
                            > 0,
                            "provider_call_status": "CAPABILITY_BOOTSTRAP_WAIT",
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 4
            if bootstrap_status in CAPABILITY_BOOTSTRAP_STOP_STATUSES:
                print(
                    json.dumps(
                        {
                            "capability_bootstrap": capability_bootstrap,
                            "capability_binding": None,
                            "state": None,
                            "cache_reused": False,
                            "provider_called": capability_bootstrap.get(
                                "provider_calls", 0
                            )
                            > 0,
                            "provider_call_status": "CAPABILITY_BOOTSTRAP_STOPPED",
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 3
            capability_binding = bind_review_job_to_runtime_capability(
                args.job,
                allowed_root=package_root,
                runtime_root=runtime_root,
            )
            active_job = Path(str(capability_binding["active_job_path"]))
        outcome = consume_review_job(
            active_job,
            args.state,
            runtime_root=runtime_root,
            runtime_ssh_host=args.runtime_ssh_host,
            allowed_root=package_root,
            executor=None,
        )
    except (
        FinalMediaReviewCapabilityAutobootstrapError,
        FinalMediaReviewInputError,
        FinalMediaReviewRawAvError,
    ) as exc:
        sys.stderr.write(
            json.dumps(
                {"reason_code": exc.reason_code, "detail": exc.detail},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        return 2

    rendered = dict(outcome)
    rendered["capability_bootstrap"] = capability_bootstrap
    rendered["capability_binding"] = capability_binding
    print(json.dumps(rendered, ensure_ascii=False, indent=2, sort_keys=True))
    state = outcome["state"]
    status = state.get("status") if isinstance(state, dict) else None
    if status == "COMPLETE":
        result = state.get("result")
        return 0 if isinstance(result, dict) and result.get("status") == "PASS" else 3
    if status in {"BLOCKED_INPUT", "FAILED"}:
        return 3
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
