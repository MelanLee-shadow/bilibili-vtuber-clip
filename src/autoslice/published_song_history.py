"""Published-song history used to prevent duplicate song deliveries.

The committed snapshot covers uploads that predate the production upload
ledger.  Successful future uploads are merged from that append-only ledger so
the unattended runner does not need a code deploy after every publication.
Matching is intentionally exact after conservative Unicode/punctuation
normalization; semantic/fuzzy matching would silently suppress distinct songs.
"""

from __future__ import annotations

import unicodedata


def normalize_song_title(value: str) -> str:
    """Return a conservative exact-match key for one song title."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return "".join(
        char
        for char in text
        if not unicodedata.category(char).startswith(("P", "Z"))
    )


