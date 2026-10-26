#!/usr/bin/env python3
"""Consume one qualified publication action independently of production ticks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__" and (ROOT / "DEPLOYED_COMMIT").is_file():
    import runpy

    runpy.run_path(str(ROOT.parent / "runtime-code-lease.py"))[
        "ensure_cli_code_lease"
    ](ROOT, Path(__file__))

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-root", type=Path,
        default=Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice")),
    )
    args = parser.parse_args(argv)
    # Bind the canonical uploader's default runtime before its first import.
    os.environ["AUTOSLICE_BASE"] = str(args.runtime_root)
    from src.autoslice.publication_queue import consume_ready_publication_queue

    result = consume_ready_publication_queue(
        repository_root=ROOT, runtime_root=args.runtime_root, enabled=True,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result["status"] in {
        "NO_READY_ACTION", "MANIFEST_PREPARED", "ACTION_COMPLETED",
        "FOLLOWUP_PENDING", "RETRY_BACKOFF", "QUEUE_BUSY", "BLOCKED_RUNTIME",
        "QUOTA_BLOCKED", "DUPLICATE_ALREADY_UPLOADED", "BLOCKED_CONTENT",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
