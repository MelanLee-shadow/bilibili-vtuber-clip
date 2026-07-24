"""Typed same-BV public-title authority for recovery media rebuilds.

An already-published title is not an Ivan handwritten title override.  This
module turns one hash-bound ``authorized-upload-public-verify.v2`` receipt into
an independently typed authority envelope, then replays the source receipt at
every runtime boundary.  A bare historical record title can never enter this
lane.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Mapping

from src.autoslice.title_policy import (
    canonicalize_publish_title,
    manual_title_override,
    publish_title_policy_violations,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = "recovery-public-title-authority.v1"
_SOURCE_SCHEMA = "authorized-upload-public-verify.v2"
_SOURCE_STATUS = "VERIFIED_PUBLIC"
_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}")
_CANDIDATE_RX = re.compile(r"[A-Za-z0-9_-]{1,96}")
_BVID_RX = re.compile(r"BV[0-9A-Za-z]{10}")
_AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "source_kind",
        "candidate_id",
        "title",
        "bvid",
        "aid",
        "cid",
        "public_verify_repo_path",
        "public_verify_sha256",
        "public_verify_schema_version",
        "public_verify_status",
        "authority_sha256",
    }
)


class RecoveryTitleAuthorityError(ValueError):
    """The published-title preservation evidence is incomplete or stale."""


def _canonical_sha256(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _regular_file_bytes(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_EVIDENCE_MISSING"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_EVIDENCE_NOT_REGULAR"
        )
    return path.read_bytes()


def _source_path(
    repo_path: object,
    *,
    repo_root: Path,
) -> Path:
    raw = str(repo_path or "")
    candidate = Path(raw)
    if (
        not raw
        or candidate.is_absolute()
        or ".." in candidate.parts
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_EVIDENCE_PATH_INVALID"
        )
    resolved_root = repo_root.resolve()
    source = resolved_root / candidate
    resolved = source.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_EVIDENCE_PATH_INVALID"
        )
    return source


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_IDENTITY_INVALID"
        )
    return value


def _validated_source_receipt(
    raw: bytes,
) -> tuple[str, str, int, int]:
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_EVIDENCE_JSON_INVALID"
        ) from exc
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != _SOURCE_SCHEMA
        or receipt.get("status") != _SOURCE_STATUS
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_EVIDENCE_STATUS_INVALID"
        )
    expected = receipt.get("expected")
    public = receipt.get("public_view")
    member = receipt.get("member_archive")
    if not all(
        isinstance(value, Mapping)
        for value in (expected, public, member)
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_SURFACES_MISSING"
        )
    assert isinstance(expected, Mapping)
    assert isinstance(public, Mapping)
    assert isinstance(member, Mapping)
    titles = {
        str(receipt.get("manifest_title") or ""),
        str(expected.get("title") or ""),
        str(public.get("title") or ""),
        str(member.get("title") or ""),
    }
    if len(titles) != 1 or not (title := next(iter(titles))):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_SURFACE_MISMATCH"
        )
    if public.get("code") != 0 or public.get("state") != 0:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_NOT_PUBLIC"
        )
    bvid = str(receipt.get("bvid") or "")
    if (
        _BVID_RX.fullmatch(bvid) is None
        or member.get("bvid") != bvid
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_IDENTITY_INVALID"
        )
    aid = _positive_int(public.get("aid"))
    cid = _positive_int(public.get("cid"))
    if _positive_int(member.get("aid")) != aid:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_IDENTITY_MISMATCH"
        )
    if (
        canonicalize_publish_title(title, lane="talk") != title
        or publish_title_policy_violations(title, lane="talk")
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_POLICY_INVALID"
        )
    return title, bvid, aid, cid


def build_recovery_title_authority(
    *,
    candidate_id: str,
    evidence_path: Path,
    expected_evidence_sha256: str,
    repo_root: Path = ROOT,
) -> dict[str, object]:
    """Build one portable authority envelope from a verified public receipt."""

    if _CANDIDATE_RX.fullmatch(candidate_id) is None:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_CANDIDATE_INVALID"
        )
    if _SHA256_RX.fullmatch(expected_evidence_sha256) is None:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_EVIDENCE_SHA_INVALID"
        )
    resolved_root = repo_root.resolve()
    raw = _regular_file_bytes(evidence_path)
    resolved_evidence = evidence_path.resolve()
    if not resolved_evidence.is_relative_to(resolved_root):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_EVIDENCE_PATH_INVALID"
        )
    repo_path = resolved_evidence.relative_to(resolved_root).as_posix()
    observed_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
    if observed_sha256 != expected_evidence_sha256:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_EVIDENCE_SHA_MISMATCH"
        )
    title, bvid, aid, cid = _validated_source_receipt(raw)
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source_kind": "same_bv_verified_public_title",
        "candidate_id": candidate_id,
        "title": title,
        "bvid": bvid,
        "aid": aid,
        "cid": cid,
        "public_verify_repo_path": repo_path,
        "public_verify_sha256": observed_sha256,
        "public_verify_schema_version": _SOURCE_SCHEMA,
        "public_verify_status": _SOURCE_STATUS,
    }
    payload["authority_sha256"] = _canonical_sha256(payload)
    return validate_recovery_title_authority(
        payload,
        candidate_id=candidate_id,
        repo_root=repo_root,
    )


def validate_recovery_title_authority(
    value: object,
    *,
    candidate_id: str,
    expected_title: str | None = None,
    repo_root: Path = ROOT,
) -> dict[str, object]:
    """Revalidate the envelope and replay its hash-bound public receipt."""

    if not isinstance(value, Mapping) or set(value) != _AUTHORITY_FIELDS:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_AUTHORITY_SCHEMA_INVALID"
        )
    authority = dict(value)
    if (
        authority.get("schema_version") != SCHEMA_VERSION
        or authority.get("source_kind")
        != "same_bv_verified_public_title"
        or authority.get("candidate_id") != candidate_id
        or _CANDIDATE_RX.fullmatch(candidate_id) is None
        or authority.get("public_verify_schema_version") != _SOURCE_SCHEMA
        or authority.get("public_verify_status") != _SOURCE_STATUS
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_AUTHORITY_SCHEMA_INVALID"
        )
    claimed_sha = authority.pop("authority_sha256")
    if (
        not isinstance(claimed_sha, str)
        or _SHA256_RX.fullmatch(claimed_sha) is None
        or claimed_sha != _canonical_sha256(authority)
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_AUTHORITY_HASH_INVALID"
        )
    authority["authority_sha256"] = claimed_sha
    evidence_path = _source_path(
        authority.get("public_verify_repo_path"),
        repo_root=repo_root,
    )
    raw = _regular_file_bytes(evidence_path)
    evidence_sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    if (
        _SHA256_RX.fullmatch(
            str(authority.get("public_verify_sha256") or "")
        )
        is None
        or evidence_sha != authority.get("public_verify_sha256")
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_EVIDENCE_SHA_MISMATCH"
        )
    title, bvid, aid, cid = _validated_source_receipt(raw)
    if (
        authority.get("title") != title
        or authority.get("bvid") != bvid
        or authority.get("aid") != aid
        or authority.get("cid") != cid
        or (expected_title is not None and title != expected_title)
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_AUTHORITY_BINDING_MISMATCH"
        )
    manual = manual_title_override(candidate_id)
    if (
        manual is not None
        and canonicalize_publish_title(manual, lane="talk") != title
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_CONFLICTS_MANUAL_OVERRIDE"
        )
    return authority
