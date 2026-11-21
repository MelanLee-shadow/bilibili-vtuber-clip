"""Runtime registry and daily-state projection helpers for publication reconciliation.

The parent module remains the compatibility surface.  These helpers delegate
file primitives back to it at call time so existing tests and callers that
patch those seams continue to observe the same behavior.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

import fcntl

RECONCILIATION_SCHEMA = "publication-reconciliation.v1"
RUNTIME_REGISTRY_SCHEMA = "publication-reconciliation-registry.v1"
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PublicationReconciliationError(ValueError):
    """A purported public completion cannot safely update local authority."""


# Each helper imports its name at call time (a test's monkeypatch of publication_reconciliation still applies, the
# import cycle stays broken) and names it, so a reader of the imports -- the public tree's shaker -- sees the use.


def _load_object(path: Path, label: str) -> dict:
    from .publication_reconciliation import _load_object as load

    return load(path, label)


def _validate_sha_entry(value: object, label: str) -> Path:
    from .publication_reconciliation import _validate_sha_entry as validate

    return validate(value, label)


@contextmanager
def _reconciliation_lock(base: Path):
    path = base.resolve() / "state" / "publication-reconciliation.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def validate_runtime_registry_entry(
    entry: object,
    *,
    verify_authority: bool = True,
) -> dict:
    if not isinstance(entry, dict):
        raise PublicationReconciliationError(
            "runtime publication registry row is invalid"
        )
    publication = entry.get("publication_reconciliation")
    if (
        entry.get("status") != "published"
        or not str(entry.get("candidate_id") or "")
        or not _DATE_RX.fullmatch(str(entry.get("recording_date") or ""))
        or not str(entry.get("bvid") or "")
        or not isinstance(publication, Mapping)
        or publication.get("schema_version") != RECONCILIATION_SCHEMA
        or publication.get("status")
        not in {
            "VERIFIED_PUBLIC",
            "VERIFIED_SAME_BV",
            "VERIFIED_SAME_BV_COVER",
            "VERIFIED_SAME_BV_METADATA",
        }
        or publication.get("candidate_id") != entry.get("candidate_id")
        or publication.get("recording_date") != entry.get("recording_date")
        or publication.get("bvid") != entry.get("bvid")
    ):
        raise PublicationReconciliationError(
            "runtime publication registry row is not a verified projection"
        )
    if verify_authority:
        from .publication_content_coverage import validate_publication_content_coverage
        try:
            validate_publication_content_coverage(publication)
        except (OSError, ValueError) as exc:
            raise PublicationReconciliationError(str(exc)) from exc
        authority_path = _validate_sha_entry(
            publication.get("authority"),
            "runtime publication reconciliation authority",
        )
        authority = _load_object(
            authority_path, "runtime publication reconciliation authority"
        )
        if publication.get("status") == "VERIFIED_PUBLIC":
            valid_authority = (
                authority.get("schema_version")
                == "new-bv-publication-reconciliation-authority.v1"
                and authority.get("status") == "VERIFIED_PUBLIC"
                and authority.get("candidate_id") == entry.get("candidate_id")
                and authority.get("recording_date")
                == entry.get("recording_date")
                and authority.get("bvid") == entry.get("bvid")
                and authority.get("aid") == publication.get("aid")
                and authority.get("cid") == publication.get("cid")
            )
        elif publication.get("status") == "VERIFIED_SAME_BV":
            valid_authority = (
                authority.get("schema_version")
                == "same-bv-repair-completed.v1"
                and authority.get("status") == "VERIFIED_FRESH_LIVE"
                and authority.get("rc") == 0
                and authority.get("candidate_id") == entry.get("candidate_id")
                and authority.get("bvid") == entry.get("bvid")
                and authority.get("aid") == publication.get("aid")
                and authority.get("new_cid") == publication.get("cid")
            )
        elif publication.get("status") == "VERIFIED_SAME_BV_METADATA":
            # lane_publish metadata-*: only the title, tags and description changed; the cid is the one online before.
            valid_authority = (
                authority.get("schema_version")
                == "same-bv-metadata-edit-completed.v1"
                and authority.get("status") == "VERIFIED_FRESH_LIVE"
                and authority.get("rc") == 0
                and authority.get("candidate_id") == entry.get("candidate_id")
                and authority.get("bvid") == entry.get("bvid")
                and authority.get("aid") == publication.get("aid")
                and authority.get("unchanged_cid") == publication.get("cid")
            )
        else:
            valid_authority = (
                authority.get("schema_version")
                == "same-bv-cover-repair-completed.v1"
                and authority.get("status") == "VERIFIED_FRESH_LIVE"
                and authority.get("rc") == 0
                and authority.get("candidate_id") == entry.get("candidate_id")
                and authority.get("bvid") == entry.get("bvid")
                and authority.get("aid") == publication.get("aid")
                and authority.get("unchanged_cid") == publication.get("cid")
            )
        if not valid_authority:
            raise PublicationReconciliationError(
                "runtime publication registry authority content conflicts"
            )
    return entry


