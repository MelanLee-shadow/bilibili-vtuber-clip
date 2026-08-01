"""Pytest bootstrap: put the repo root on sys.path.

Tests import ``src.autoslice`` and ``scripts.*`` as top-level packages.
``python3 -m pytest`` already prepends the CWD, but bare ``pytest`` (and many
IDE runners) does not — this conftest makes both invocations equivalent.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
