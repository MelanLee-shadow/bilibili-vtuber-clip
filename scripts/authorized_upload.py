#!/usr/bin/env python3
"""Manifest-bound Li Dousha publish and existing-BV repair entry point.

``make-manifest`` freezes reviewed bytes and 维护者's authorization; ``upload``
and ``season-add`` enforce hashes, idempotency, metadata, public, Creator, and
exact-section readback.  ``repair-*`` uses a separate crash-safe journal and
can only append/edit an explicitly named existing BV; it cannot create one.
``repair-verify-live`` re-observes all four public/Creator surfaces after the
transaction reaches VERIFIED and writes a create-only completed sidecar.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


