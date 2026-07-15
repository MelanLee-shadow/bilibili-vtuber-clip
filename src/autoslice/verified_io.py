"""Generic hash / path verification leaf utilities.

Extracted verbatim from scripts/free_session_autoslice.py (2026-07-15 屎山
治理第二刀). Pure functions, no runner state, not monkeypatched — the runner
re-imports them by name so its ~60 call sites are unchanged, and
song_completion.py imports the three it needs without a cycle.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


def _matches_sha256(path: Path, expected: str) -> bool:
    expected = expected.removeprefix("sha256:").lower()
    if path.is_symlink() or len(expected) != 64 or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == expected


def _canonical_existing_path(path_value: object) -> str | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    try:
        return str(Path(path_value).resolve(strict=True))
    except OSError:
        return None


def _normalized_sha256(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.lower().removeprefix("sha256:")
    return normalized if re.fullmatch(r"[0-9a-f]{64}", normalized) else None
