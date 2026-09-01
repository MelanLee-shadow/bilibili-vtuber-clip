#!/usr/bin/env python3
"""Create-only C2 legacy execution-contract proposal; it cannot upload."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.autoslice.fastlane_c2_legacy_recovery import make_proposal
from src.autoslice.fastlane_c2_technical_receipt import write_create_only_json

p = argparse.ArgumentParser(); p.add_argument("--formal", type=Path, required=True); p.add_argument("--authorization", type=Path, required=True); p.add_argument("--receipt", type=Path, required=True); p.add_argument("--out", type=Path, required=True)
a = p.parse_args()
write_create_only_json(a.out, make_proposal(a.formal, a.authorization, a.receipt))
