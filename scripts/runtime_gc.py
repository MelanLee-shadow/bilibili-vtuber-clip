#!/usr/bin/env python3
"""Normal CLI for the pipeline lifecycle collector."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.autoslice.runtime_gc import main

if __name__ == "__main__":
    raise SystemExit(main())
