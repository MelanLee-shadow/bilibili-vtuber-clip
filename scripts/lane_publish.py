#!/usr/bin/env python3
"""Step 10's upload tool for any channel: package audit, new BV, same-BV video or cover replacement, publication registry.
See src/autoslice/lane_publish/__init__.py."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.lane_publish.cli import main

if __name__ == "__main__":
    sys.exit(main())
