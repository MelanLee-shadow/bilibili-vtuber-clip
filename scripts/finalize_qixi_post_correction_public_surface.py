#!/usr/bin/env python3
"""Operate the one sealed 女友感 public-surface closure.

There are intentionally no candidate, path, title, upload, or provider flags.
The checked-in/deployed authority owns the only runtime identity.  ``--plan``
is shallow sealed-runtime preflight, ``--diagnose`` is read-only, and
``--full-dry-run`` may run only private staging before formal replay.  Only
``--apply`` can commit the normal source-fact/cover transaction; it obtains
the runtime's canonical ``runner.lock`` only for short revalidation and the
durable journal commit.  Provider-backed after-image preparation and private
validation happen first without that hot mutation lock.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.qixi_post_correction_public_surface import (  # noqa: E402
    QixiPostCorrectionPublicSurfaceError,
)
from src.autoslice.qixi_post_correction_modes import Mode, run  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", dest="mode", action="store_const", const=Mode.PLAN)
    mode.add_argument("--diagnose", dest="mode", action="store_const", const=Mode.DIAGNOSE)
    mode.add_argument("--full-dry-run", dest="mode", action="store_const", const=Mode.FULL_DRY_RUN)
    mode.add_argument("--apply", dest="mode", action="store_const", const=Mode.APPLY)
    mode.add_argument("--dry-run", dest="mode", action="store_const", const=Mode.PLAN)
    parser.set_defaults(mode=Mode.PLAN)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = run(args.mode, repo_root=ROOT, runtime_root=Path("/opt/bilive/autoslice"))
    except QixiPostCorrectionPublicSurfaceError as exc:
        print(f"QIXI_PUBLIC_SURFACE_CLOSURE_BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result.get("status") == "FULL_DRY_RUN_BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
