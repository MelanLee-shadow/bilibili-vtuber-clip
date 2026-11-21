"""Where the runtime lives when AUTOSLICE_BASE is not set: the one definition of that default.

Production sets neither variable and gets /opt/bilive/autoslice, as always.  The test suite leaves AUTOSLICE_BASE unset
(an unset base means "no cache, no persistence" to much of the code, and tests are written against that) and points
AUTOSLICE_DEFAULT_BASE at an empty directory, so a reader that falls back to the default finds nothing on any host —
the production host included, where the default exists (tests run there read the production runtime
publication registry, receipts and lock files)."""

from __future__ import annotations

import os

DEFAULT_BASE_ENV = "AUTOSLICE_DEFAULT_BASE"
PRODUCTION_BASE = "/opt/bilive/autoslice"


def default_base() -> str:
    return os.environ.get(DEFAULT_BASE_ENV) or PRODUCTION_BASE
