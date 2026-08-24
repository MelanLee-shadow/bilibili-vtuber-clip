"""Typed same-BV publication and legacy public-title recovery authorities.

Production recovery uses one committed registry under ``assets/`` to bind the
complete candidate set to the existing BVID/AID/CID and either an exact public
title or an Ivan-manual title source.  The older per-receipt title envelope is
kept as a narrow validator for historical packages/tests, but it is not the
v10 operator path.  A bare historical record title can never enter either lane.
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
PUBLICATION_SCHEMA_VERSION = "recovery-same-bv-publication-authority.v1"
PUBLICATION_REGISTRY_SCHEMA = (
    "lidousha-recovery-publication-authority.v1"
)
_SOURCE_SCHEMA = "authorized-upload-public-verify.v2"
_SOURCE_STATUS = "VERIFIED_PUBLIC"
_REPAIR_IDENTITY_SCHEMA = "recovery-publication-identity.v1"
_REPAIR_IDENTITY_STATUS = "VERIFIED_TITLE_AND_TARGET_IDENTITY"
_SOURCE_AUTHORITY_TYPES = frozenset(
    {
        (_SOURCE_SCHEMA, _SOURCE_STATUS),
        (_REPAIR_IDENTITY_SCHEMA, _REPAIR_IDENTITY_STATUS),
    }
)
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
_PUBLICATION_ENTRY_FIELDS = frozenset(
    {
        "candidate_id",
        "title_mode",
        "observed_public_title",
        "required_given_end_ms",
        "boundary_end_mode",
        "bvid",
        "aid",
        "cid",
        "source_public_verify_repo_path",
        "source_public_verify_sha256",
        "source_public_verify_schema_version",
        "source_public_verify_status",
    }
)
_PUBLICATION_AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "source_kind",
        "candidate_id",
        "title_mode",
        "observed_public_title",
        "required_given_end_ms",
        "boundary_end_mode",
        "bvid",
        "aid",
        "cid",
        "registry_repo_path",
        "registry_sha256",
        "registry_schema_version",
        "registry_authority",
        "source_public_verify_repo_path",
        "source_public_verify_sha256",
        "source_public_verify_schema_version",
        "source_public_verify_status",
        "authority_sha256",
    }
)
_PUBLICATION_TITLE_MODES = frozenset(
    {"verified_public_exact", "ivan_manual_override"}
)
_BOUNDARY_END_MODES = frozenset(
    {
        "semantic_lower_bound",
        "published_recall_anchor",
        "exact_source_pin",
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


def _validated_source_identity_receipt(
    raw: bytes,
) -> tuple[str, str, int, int]:
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_EVIDENCE_JSON_INVALID"
        ) from exc
    if not isinstance(receipt, Mapping) or (
        receipt.get("schema_version"), receipt.get("status")
    ) not in _SOURCE_AUTHORITY_TYPES:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_EVIDENCE_STATUS_INVALID"
        )
    if receipt.get("schema_version") == _REPAIR_IDENTITY_SCHEMA:
        section = receipt.get("section_api")
        if (
            receipt.get("problems")
            != ["exact section episode title mismatch"]
            or not isinstance(section, Mapping)
            or section.get("code") != 0
            or section.get("episode_match_count") != 1
            or not isinstance(section.get("episode_titles"), list)
            or len(section["episode_titles"]) != 1
        ):
            raise RecoveryTitleAuthorityError(
                "RECOVERY_PUBLIC_TITLE_REPAIR_IDENTITY_INVALID"
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
    if (
        receipt.get("schema_version") == _REPAIR_IDENTITY_SCHEMA
        and (receipt.get("section_api") or {}).get("episode_titles")
        == [title]
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_REPAIR_IDENTITY_INVALID"
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
    return title, bvid, aid, cid


def _validated_source_receipt(
    raw: bytes,
) -> tuple[str, str, int, int]:
    title, bvid, aid, cid = _validated_source_identity_receipt(raw)
    if (
        canonicalize_publish_title(title, lane="talk") != title
        or publish_title_policy_violations(title, lane="talk")
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLIC_TITLE_POLICY_INVALID"
        )
    return title, bvid, aid, cid


def _validated_publication_entry(
    value: object,
    *,
    repo_root: Path,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _PUBLICATION_ENTRY_FIELDS:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_REGISTRY_ENTRY_INVALID"
        )
    entry = dict(value)
    candidate_id = str(entry.get("candidate_id") or "")
    title_mode = str(entry.get("title_mode") or "")
    observed_title = str(entry.get("observed_public_title") or "")
    source_path = str(entry.get("source_public_verify_repo_path") or "")
    source_sha = str(entry.get("source_public_verify_sha256") or "")
    if (
        _CANDIDATE_RX.fullmatch(candidate_id) is None
        or title_mode not in _PUBLICATION_TITLE_MODES
        or not observed_title
        or isinstance(entry.get("required_given_end_ms"), bool)
        or not isinstance(entry.get("required_given_end_ms"), int)
        or entry["required_given_end_ms"] <= 0
        or entry.get("boundary_end_mode") not in _BOUNDARY_END_MODES
        or _BVID_RX.fullmatch(str(entry.get("bvid") or "")) is None
        or _SHA256_RX.fullmatch(source_sha) is None
        or (
            entry.get("source_public_verify_schema_version"),
            entry.get("source_public_verify_status"),
        )
        not in _SOURCE_AUTHORITY_TYPES
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_REGISTRY_ENTRY_INVALID"
        )
    _positive_int(entry.get("aid"))
    _positive_int(entry.get("cid"))
    manual = manual_title_override(candidate_id)
    if title_mode == "verified_public_exact":
        if (
            manual is not None
            or canonicalize_publish_title(observed_title, lane="talk")
            != observed_title
            or publish_title_policy_violations(observed_title, lane="talk")
        ):
            raise RecoveryTitleAuthorityError(
                "RECOVERY_PUBLICATION_TITLE_MODE_INVALID"
            )
    elif (
        manual is None
        or manual != observed_title
        or publish_title_policy_violations(
            canonicalize_publish_title(manual, lane="talk"),
            lane="talk",
        )
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_TITLE_MODE_INVALID"
        )

    # The committed registry is the runtime authority.  In source worktrees
    # that retain the historical receipt, replay it too; production deploys
    # intentionally omit disposable reports and therefore rely on the
    # hash-bound registry copy under assets/.
    source = _source_path(source_path, repo_root=repo_root)
    if source.exists() or source.is_symlink():
        raw = _regular_file_bytes(source)
        if "sha256:" + hashlib.sha256(raw).hexdigest() != source_sha:
            raise RecoveryTitleAuthorityError(
                "RECOVERY_PUBLICATION_SOURCE_RECEIPT_SHA_MISMATCH"
            )
        receipt = json.loads(raw.decode("utf-8"))
        if (
            receipt.get("schema_version")
            != entry.get("source_public_verify_schema_version")
            or receipt.get("status")
            != entry.get("source_public_verify_status")
        ):
            raise RecoveryTitleAuthorityError(
                "RECOVERY_PUBLICATION_SOURCE_RECEIPT_TYPE_MISMATCH"
            )
        title, bvid, aid, cid = _validated_source_identity_receipt(raw)
        if (
            title != observed_title
            or bvid != entry.get("bvid")
            or aid != entry.get("aid")
            or cid != entry.get("cid")
        ):
            raise RecoveryTitleAuthorityError(
                "RECOVERY_PUBLICATION_SOURCE_RECEIPT_BINDING_MISMATCH"
            )
    return entry


def _validated_publication_registry(
    raw: bytes,
    *,
    repo_root: Path,
) -> tuple[str, dict[str, dict[str, object]]]:
    try:
        registry = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_REGISTRY_JSON_INVALID"
        ) from exc
    if (
        not isinstance(registry, Mapping)
        or set(registry) != {"schema_version", "authority", "entries"}
        or registry.get("schema_version") != PUBLICATION_REGISTRY_SCHEMA
        or not str(registry.get("authority") or "").strip()
        or not isinstance(registry.get("entries"), list)
        or not registry.get("entries")
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_REGISTRY_SCHEMA_INVALID"
        )
    entries: dict[str, dict[str, object]] = {}
    for raw_entry in registry["entries"]:
        entry = _validated_publication_entry(
            raw_entry,
            repo_root=repo_root,
        )
        candidate_id = str(entry["candidate_id"])
        if candidate_id in entries:
            raise RecoveryTitleAuthorityError(
                "RECOVERY_PUBLICATION_REGISTRY_DUPLICATE_CANDIDATE"
            )
        entries[candidate_id] = entry
    return str(registry["authority"]).strip(), entries


def build_recovery_publication_authorities(
    *,
    candidate_ids: set[str] | frozenset[str],
    registry_path: Path,
    expected_registry_sha256: str,
    require_exact_candidate_set: bool = False,
    repo_root: Path = ROOT,
) -> dict[str, dict[str, object]]:
    """Build exact same-BV target/title authorities for one recovery set."""

    requested = set(candidate_ids)
    if (
        not requested
        or any(_CANDIDATE_RX.fullmatch(value) is None for value in requested)
        or _SHA256_RX.fullmatch(expected_registry_sha256) is None
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_REQUEST_INVALID"
        )
    resolved_root = repo_root.resolve()
    raw = _regular_file_bytes(registry_path)
    resolved_registry = registry_path.resolve()
    if not resolved_registry.is_relative_to(resolved_root):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_REGISTRY_PATH_INVALID"
        )
    registry_sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    if registry_sha != expected_registry_sha256:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_REGISTRY_SHA_MISMATCH"
        )
    registry_authority, entries = _validated_publication_registry(
        raw,
        repo_root=repo_root,
    )
    if not requested.issubset(entries):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_CANDIDATE_MISSING"
        )
    if require_exact_candidate_set and requested != set(entries):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_CANDIDATE_SET_MISMATCH"
        )
    repo_path = resolved_registry.relative_to(resolved_root).as_posix()
    result: dict[str, dict[str, object]] = {}
    for candidate_id in sorted(requested):
        entry = entries[candidate_id]
        payload: dict[str, object] = {
            "schema_version": PUBLICATION_SCHEMA_VERSION,
            "source_kind": "committed_verified_publication_registry",
            **entry,
            "registry_repo_path": repo_path,
            "registry_sha256": registry_sha,
            "registry_schema_version": PUBLICATION_REGISTRY_SCHEMA,
            "registry_authority": registry_authority,
        }
        payload["authority_sha256"] = _canonical_sha256(payload)
        result[candidate_id] = validate_recovery_publication_authority(
            payload,
            candidate_id=candidate_id,
            repo_root=repo_root,
        )
    return result


def expected_recovery_publish_title(
    authority: Mapping[str, object],
) -> str:
    """Resolve the only permitted final title from a validated authority."""

    if authority.get("schema_version") == "fastlane-c1-authorized-same-bv-projection.v1":
        return str(authority["title"])

    title = str(authority["observed_public_title"])
    if authority["title_mode"] == "ivan_manual_override":
        return canonicalize_publish_title(title, lane="talk")
    return title


def validate_recovery_publication_authority(
    value: object,
    *,
    candidate_id: str,
    expected_final_title: str | None = None,
    repo_root: Path = ROOT,
) -> dict[str, object]:
    """Replay one committed registry entry and bind title plus BV identity."""

    # C1 is the one explicitly sealed fastlane formal package.  It is not a
    # registry-shaped recovery record and must never be coerced into one.
    if isinstance(value, Mapping) and value.get("schema_version") == "fastlane-c1-authorized-same-bv-projection.v1":
        from src.autoslice.fastlane_c1_technical_receipt import (
            C1TechnicalReceiptError,
            validate_projection_authority,
        )
        try:
            return validate_projection_authority(
                value,
                candidate_id=candidate_id,
                expected_final_title=expected_final_title,
            )
        except C1TechnicalReceiptError as exc:
            raise RecoveryTitleAuthorityError(str(exc)) from exc

    if (
        not isinstance(value, Mapping)
        or set(value) != _PUBLICATION_AUTHORITY_FIELDS
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_AUTHORITY_SCHEMA_INVALID"
        )
    authority = dict(value)
    if (
        authority.get("schema_version") != PUBLICATION_SCHEMA_VERSION
        or authority.get("source_kind")
        != "committed_verified_publication_registry"
        or authority.get("candidate_id") != candidate_id
        or _CANDIDATE_RX.fullmatch(candidate_id) is None
        or authority.get("registry_schema_version")
        != PUBLICATION_REGISTRY_SCHEMA
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_AUTHORITY_SCHEMA_INVALID"
        )
    claimed_sha = authority.pop("authority_sha256")
    if (
        not isinstance(claimed_sha, str)
        or _SHA256_RX.fullmatch(claimed_sha) is None
        or claimed_sha != _canonical_sha256(authority)
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_AUTHORITY_HASH_INVALID"
        )
    authority["authority_sha256"] = claimed_sha
    registry_path = _source_path(
        authority.get("registry_repo_path"),
        repo_root=repo_root,
    )
    raw = _regular_file_bytes(registry_path)
    registry_sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    if (
        _SHA256_RX.fullmatch(str(authority.get("registry_sha256") or ""))
        is None
        or registry_sha != authority.get("registry_sha256")
    ):
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_REGISTRY_SHA_MISMATCH"
        )
    registry_authority, entries = _validated_publication_registry(
        raw,
        repo_root=repo_root,
    )
    entry = entries.get(candidate_id)
    if entry is None:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_CANDIDATE_MISSING"
        )
    expected_surface = {
        "schema_version": PUBLICATION_SCHEMA_VERSION,
        "source_kind": "committed_verified_publication_registry",
        **entry,
        "registry_repo_path": authority["registry_repo_path"],
        "registry_sha256": registry_sha,
        "registry_schema_version": PUBLICATION_REGISTRY_SCHEMA,
        "registry_authority": registry_authority,
        "authority_sha256": claimed_sha,
    }
    if authority != expected_surface:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_AUTHORITY_BINDING_MISMATCH"
        )
    final_title = expected_recovery_publish_title(authority)
    if expected_final_title is not None and final_title != expected_final_title:
        raise RecoveryTitleAuthorityError(
            "RECOVERY_PUBLICATION_FINAL_TITLE_MISMATCH"
        )
    return authority


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
