#!/usr/bin/env python3
"""Entry point: python scripts/manual_lane.py {init,run,status,listen-queue,listen-import,reselect,supersede} (see src/autoslice/manual_lane/cli.py)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.manual_lane.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
