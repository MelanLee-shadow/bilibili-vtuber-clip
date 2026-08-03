"""Published-song history used to prevent duplicate song deliveries.

The committed snapshot covers uploads that predate the production upload
ledger.  Successful future uploads are merged from that append-only ledger so
the unattended runner does not need a code deploy after every publication.
Matching is intentionally exact after conservative Unicode/punctuation
normalization; semantic/fuzzy matching would silently suppress distinct songs.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path


SCHEMA_VERSION = "vtuber-slice.published-songs.v1"
_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
_BOOK_TITLE_RE = re.compile(r"《([^》]{1,160})》")


class PublishedSongHistoryError(ValueError):
    """The duplicate-prevention authority is malformed or ambiguous."""


def normalize_song_title(value: str) -> str:
    """Return a conservative exact-match key for one song title."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return "".join(
        char
        for char in text
        if not unicodedata.category(char).startswith(("P", "Z"))
    )


def extract_song_title(value: str, *, song_title_prefix: str) -> str:
    """Extract the canonical title from a policy-compliant upload title.

    New song uploads are required to include ``《song》``.  Raw LRC/visual
    titles are accepted unchanged.  Historical non-bracketed upload titles
    belong in the committed snapshot where they can be reviewed explicitly.
    """

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    bracketed = _BOOK_TITLE_RE.search(text)
    if bracketed:
        return bracketed.group(1).strip()
    if text.startswith(song_title_prefix):
        return ""
    return text


def _load_snapshot(path: Path) -> tuple[dict[str, dict], dict]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublishedSongHistoryError(
            f"published-song snapshot unavailable: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise PublishedSongHistoryError("published-song snapshot schema mismatch")
    songs = document.get("songs")
    if not isinstance(songs, list) or not songs:
        raise PublishedSongHistoryError("published-song snapshot songs must be non-empty")

    indexed: dict[str, dict] = {}
    for position, item in enumerate(songs):
        if not isinstance(item, dict):
            raise PublishedSongHistoryError(f"songs[{position}] must be an object")
        canonical = str(item.get("canonical_title") or "").strip()
        bvid = str(item.get("bvid") or "").strip()
        aliases = item.get("aliases", [])
        if not canonical or not _BVID_RE.fullmatch(bvid) or not isinstance(aliases, list):
            raise PublishedSongHistoryError(f"songs[{position}] has invalid title/bvid/aliases")
        surfaces = [canonical, *aliases]
        for surface in surfaces:
            if not isinstance(surface, str) or not surface.strip():
                raise PublishedSongHistoryError(f"songs[{position}] has an invalid alias")
            key = normalize_song_title(surface)
            if not key:
                raise PublishedSongHistoryError(f"songs[{position}] normalizes to an empty key")
            prior = indexed.get(key)
            if prior is not None and prior["canonical_title"] != canonical:
                raise PublishedSongHistoryError(
                    f"published-song alias collision: {surface!r} maps to both "
                    f"{prior['canonical_title']!r} and {canonical!r}"
                )
            indexed[key] = {
                "canonical_title": canonical,
                "matched_surface": surface,
                "source": "committed_snapshot",
                "bvid": bvid,
                "aid": item.get("aid"),
                "published_at_epoch": item.get("published_at_epoch"),
            }
    return indexed, document


def _merge_successful_ledger_uploads(
    indexed: dict[str, dict],
    ledger_path: Path,
    *,
    song_title_prefix: str,
) -> None:
    """Merge successful, BVID-bearing song upload events from the live ledger."""

    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        # The committed snapshot remains an independently verified baseline.
        # Production owns this ledger; tests and source staging do not.
        return
    except OSError as exc:
        raise PublishedSongHistoryError(
            f"upload ledger unreadable: {type(exc).__name__}: {exc}"
        ) from exc

    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise PublishedSongHistoryError(
                f"upload ledger line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(event, dict):
            raise PublishedSongHistoryError(
                f"upload ledger line {line_number} must be an object"
            )
        title = str(event.get("title") or "").strip()
        bvid = str(event.get("bvid") or "").strip()
        if (
            event.get("event") != "UPLOAD_ATTEMPT_FINISHED"
            or event.get("rc") != 0
            or not _BVID_RE.fullmatch(bvid)
            or not title.startswith(song_title_prefix)
        ):
            continue
        canonical = extract_song_title(title, song_title_prefix=song_title_prefix)
        key = normalize_song_title(canonical)
        if not key:
            # Non-bracketed historical titles require a reviewed snapshot row;
            # never guess a song name from marketing copy.
            continue
        indexed.setdefault(
            key,
            {
                "canonical_title": canonical,
                "matched_surface": canonical,
                "source": "production_upload_ledger",
                "bvid": bvid,
                "at": event.get("at"),
                "ledger_line": line_number,
            },
        )


def published_song_match(
    value: str,
    *,
    snapshot_path: Path,
    ledger_path: Path,
    song_title_prefix: str,
) -> dict | None:
    """Return auditable exact-match evidence when ``value`` was published."""

    indexed, document = _load_snapshot(snapshot_path)
    _merge_successful_ledger_uploads(
        indexed,
        ledger_path,
        song_title_prefix=song_title_prefix,
    )
    extracted = extract_song_title(value, song_title_prefix=song_title_prefix)
    key = normalize_song_title(extracted)
    match = indexed.get(key) if key else None
    if match is None:
        return None
    return {
        **match,
        "query": str(value),
        "extracted_title": extracted,
        "normalized_key": key,
        "snapshot_schema_version": document["schema_version"],
        "snapshot_verified_through": document.get("verified_through"),
    }
