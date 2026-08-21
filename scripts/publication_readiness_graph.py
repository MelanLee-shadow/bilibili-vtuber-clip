#!/usr/bin/env python3
"""Print the fixed, read-only Li Dousha publication readiness graph."""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.autoslice.publication_readiness import build_readiness_graph


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    runtime_root = Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
    print(json.dumps(
        build_readiness_graph(repository_root=repo_root, runtime_root=runtime_root),
        ensure_ascii=False, sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
