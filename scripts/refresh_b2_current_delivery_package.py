#!/usr/bin/env python3
"""Create and locally verify B2's current audit/mechanical package successor."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]


def _bootstrap_repository_namespaces() -> None:
    # A third-party regular package named ``scripts`` may shadow this
    # repository's namespace package.  Bind only the two repository roots used
    # by the current process; do not mutate the Python environment.
    for name, path in (("scripts", ROOT / "scripts"), ("src", ROOT / "src")):
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        module.__package__ = name
        sys.modules[name] = module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-package", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--result-out", type=Path, required=True)
    args = parser.parse_args(argv)
    _bootstrap_repository_namespaces()
    from src.autoslice.b2_current_delivery_refresh import (
        B2CurrentDeliveryRefreshError,
        refresh_current_delivery,
        write_result_create_only,
    )

    try:
        result = refresh_current_delivery(
            source_package=args.source_package,
            source_manifest_path=args.source_manifest,
            output=args.output,
            source_commit=args.source_commit,
        )
        write_result_create_only(args.result_out, result)
    except (B2CurrentDeliveryRefreshError, OSError, ValueError, RuntimeError) as exc:
        print(
            json.dumps(
                {
                    "status": "FAILED_CLOSED",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "output_preserved_if_created": True,
                    "production_adopted": False,
                    "same_bv_executed": False,
                    "upload_performed": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
