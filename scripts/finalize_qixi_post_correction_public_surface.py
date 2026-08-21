#!/usr/bin/env python3
"""Stage or apply the one sealed 女友感 public-surface closure.

There are intentionally no candidate, path, title, upload, or provider flags.
The checked-in/deployed authority owns the only runtime identity.  ``--dry-run``
performs preflight only; ``--apply`` is the sole mode allowed to invoke the
normal source-fact or cover provider routes.  Before that work begins it
nonblockingly acquires the runtime's canonical ``runner.lock`` itself.
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
    finalize,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false")
    mode.add_argument("--apply", dest="apply", action="store_true")
    parser.set_defaults(apply=False)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = finalize(apply=args.apply, repo_root=ROOT)
    except QixiPostCorrectionPublicSurfaceError as exc:
        print(f"QIXI_PUBLIC_SURFACE_CLOSURE_BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
