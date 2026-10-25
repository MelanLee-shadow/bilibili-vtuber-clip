"""Replay a normal first-publication closure for repair of that exact BV.

This binds an existing target and a successor title; it grants no upload or
content approval. Package, title/cover, mechanical and live before-image gates
remain consumers of the ordinary same-BV lane.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

from src.autoslice.first_publication_boundary import BOUNDARY_FIELDS

SCHEMA_VERSION = "new-bv-same-bv-publication-authority.v1"
_SOURCE_SCHEMA = "new-bv-publication-reconciliation-authority.v1"
_FIELDS = frozenset({
    "schema_version", "source_kind", "candidate_id", "recording_date",
    "bvid", "aid", "cid", "observed_public_title", "title", "title_mode",
    "source_publication", "authority_sha256",
})
_OPTIONAL_FIELDS = frozenset({"review_manifest_snapshot"})


class NewBvRepairAuthorityError(ValueError):
    pass


def _need(condition: bool, detail: str) -> None:
    if not condition:
        raise NewBvRepairAuthorityError(detail)


def _regular(path: Path) -> None:
    _need(
        path.is_absolute() and path.is_file()
        and not any(p.is_symlink() for p in (path, *path.parents)),
        "new-BV publication evidence must be an absolute regular file",
    )


def _digest(value: Mapping[str, object]) -> str:
    raw = json.dumps(dict(value), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _snapshot_binding(
    manifest: Mapping[str, object], snapshot: object, *, reconciliation
) -> dict[str, object]:
    """Validate a portable copy of the original review-manifest binding.

    The source publication remains the original first-publication closure.  A
    snapshot is only a date/candidate witness for a portable clone, so its
    bytes must be exactly those bound by the original manifest.
    """

    _need(
        isinstance(snapshot, Mapping)
        and set(snapshot) == {"path", "sha256", "bytes"},
        "review manifest snapshot binding is invalid",
    )
    path = Path(str(snapshot.get("path") or ""))
    _regular(path)
    _need(path.stat().st_size <= 8 * 1024 * 1024, "review manifest snapshot is oversized")
    actual = reconciliation._sha_entry(path)
    _need(actual == dict(snapshot), "review manifest snapshot drifted")
    attestation = manifest.get("package_attestation")
    _need(isinstance(attestation, Mapping), "first manifest has no package_attestation")
    original = attestation.get("review_manifest")
    _need(
        isinstance(original, Mapping)
        and set(original) == {"path", "sha256", "bytes"},
        "first manifest review manifest binding is missing",
    )
    original_path = Path(str(original.get("path") or ""))
    _need(
        original_path.is_absolute()
        and not original_path.is_symlink()
        and re.fullmatch(
            r"(?:sha256:)?[0-9a-f]{64}", str(original.get("sha256") or "")
        )
        and type(original.get("bytes")) is int
        and original["bytes"] >= 0,
        "first manifest review manifest binding shape is invalid",
    )
    _need(
        actual["sha256"] == original["sha256"]
        and actual["bytes"] == original["bytes"],
        "review manifest snapshot is not the original bound review manifest",
    )
    return actual


def _candidate_and_date(
    manifest: Mapping[str, object], snapshot: object | None, *, reconciliation
) -> tuple[str, str]:
    if snapshot is None:
        return reconciliation._candidate_and_date(manifest)

    # Force the existing resolver to consume the supplied historical review
    # bytes while preserving the original package_root and record bindings.
    # This keeps the resolver's normal identity rules intact; it only replaces
    # the stale review-manifest file reference.
    snapshot_entry = _snapshot_binding(manifest, snapshot, reconciliation=reconciliation)
    attestation = manifest.get("package_attestation")
    assert isinstance(attestation, Mapping)
    shadow = dict(manifest)
    shadow_attestation = dict(attestation)
    shadow_attestation["review_manifest"] = snapshot_entry
    shadow["package_attestation"] = shadow_attestation
    candidate, date = reconciliation._candidate_and_date(shadow)
    review = reconciliation._load_object(
        Path(str(snapshot_entry["path"])), "review manifest snapshot"
    )
    reconciliation._validate_review_manifest_candidate(review, candidate)
    review_dates = {
        str(review[key])
        for key in ("date", "recording_date")
        if key in review
    }
    _need(
        len(review_dates) == 1 and next(iter(review_dates)) == date,
        "review manifest snapshot candidate/date differs from source",
    )
    return candidate, date


def _replay_source(binding: object, *, review_manifest_snapshot: object | None = None) -> dict:
    from src.autoslice import publication_reconciliation as reconciliation

    _need(isinstance(binding, Mapping) and set(binding) == {"path", "sha256", "bytes"},
          "new-BV publication binding is invalid")
    path = Path(str(binding.get("path") or ""))
    _regular(path)
    _need(path.stat().st_size <= 8 * 1024 * 1024, "publication receipt is oversized")
    _need(reconciliation._sha_entry(path) == dict(binding), "publication receipt drifted")
    source = reconciliation._load_object(path, "first publication authority")
    _need(source.get("schema_version") == _SOURCE_SCHEMA
          and source.get("status") == "VERIFIED_PUBLIC",
          "complete normal first-publication authority required")
    evidence = source.get("evidence")
    _need(isinstance(evidence, Mapping)
          and set(evidence) == {"manifest", "public_verify", "season_verify", "uploaded"},
          "first-publication evidence closure is missing")
    paths = {}
    for key, entry in evidence.items():
        _need(isinstance(entry, Mapping), "publication evidence binding is missing")
        p = Path(str(entry.get("path") or ""))
        _regular(p)
        paths[key] = reconciliation._validate_sha_entry(entry, key)
    manifest = reconciliation._load_object(paths["manifest"], "first upload manifest")
    candidate, date = _candidate_and_date(
        manifest, review_manifest_snapshot, reconciliation=reconciliation
    )
    replayed, aid, cid = reconciliation._validate_new_public_evidence(
        manifest=manifest, manifest_path=paths["manifest"], bvid=source.get("bvid"),
        public_verify_path=paths["public_verify"],
        season_verify_path=paths["season_verify"], uploaded_path=paths["uploaded"],
    )
    _need(replayed == evidence, "publication evidence payload drifted")
    _need(source.get("candidate_id") == candidate
          and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", candidate) is not None
          and source.get("recording_date") == date
          and re.fullmatch(r"BV[0-9A-Za-z]{10}", str(source.get("bvid") or "")) is not None
          and type(source.get("aid")) is int and source["aid"] == aid and aid > 0
          and type(source.get("cid")) is int and source["cid"] == cid and cid > 0
          and source.get("title") == manifest.get("title"),
          "first-publication candidate/target identity drifted")
    _need(reconciliation._sha_entry(path) == dict(binding), "publication receipt changed during replay")
    return source


def build_new_bv_repair_authority(
    publication_path: Path, *, candidate_id: str, title: str,
    review_manifest_snapshot: Path | None = None,
    published_recall_anchor: bool = False,
) -> dict[str, object]:
    from src.autoslice.publication_reconciliation import _sha_entry

    _regular(publication_path)
    binding = _sha_entry(publication_path)
    snapshot_binding = None
    if review_manifest_snapshot is not None:
        snapshot_path = Path(review_manifest_snapshot)
        _regular(snapshot_path)
        snapshot_binding = _sha_entry(snapshot_path)
    source = _replay_source(binding, review_manifest_snapshot=snapshot_binding)
    value = {
        "schema_version": SCHEMA_VERSION,
        "source_kind": "verified_normal_first_publication",
        "candidate_id": source["candidate_id"], "recording_date": source["recording_date"],
        "bvid": source["bvid"], "aid": source["aid"], "cid": source["cid"],
        "observed_public_title": source["title"], "title": title,
        "title_mode": "verified_package_repair", "source_publication": binding,
    }
    if snapshot_binding is not None:
        value["review_manifest_snapshot"] = snapshot_binding
    if published_recall_anchor:
        from src.autoslice.first_publication_boundary import publication_boundary_fields
        value.update(publication_boundary_fields(value))
    value["authority_sha256"] = _digest(value)
    return validate_new_bv_repair_authority(value, candidate_id=candidate_id,
                                           expected_final_title=title)


def validate_new_bv_repair_authority(
    value: object, *, candidate_id: str, expected_final_title: str | None = None,
) -> dict[str, object]:
    from src.autoslice.title_policy import publish_title_lane, publish_title_policy_violations

    _need(
        isinstance(value, Mapping)
        and _FIELDS.issubset(value)
        and set(value).issubset(_FIELDS | _OPTIONAL_FIELDS | BOUNDARY_FIELDS)
        and (not BOUNDARY_FIELDS.intersection(value) or BOUNDARY_FIELDS.issubset(value)),
          "normal same-BV authority schema invalid")
    authority = dict(value)
    _need(
        "review_manifest_snapshot" not in authority
        or isinstance(authority["review_manifest_snapshot"], Mapping),
        "review manifest snapshot binding is invalid",
    )
    digest = authority.pop("authority_sha256")
    _need(digest == _digest(authority), "normal same-BV authority hash invalid")
    authority["authority_sha256"] = digest
    _need(authority["schema_version"] == SCHEMA_VERSION
          and authority["source_kind"] == "verified_normal_first_publication"
          and authority["title_mode"] == "verified_package_repair"
          and authority["candidate_id"] == candidate_id
          and type(authority["aid"]) is int and type(authority["cid"]) is int,
          "normal same-BV authority candidate/schema drifted")
    title = authority["title"]
    _need(isinstance(title, str) and bool(title)
          and not publish_title_policy_violations(
              title, lane=publish_title_lane(str(authority["observed_public_title"])))
          and (expected_final_title is None or title == expected_final_title),
          "normal same-BV successor title invalid or drifted")
    source = _replay_source(
        authority["source_publication"],
        review_manifest_snapshot=authority.get("review_manifest_snapshot"),
    )
    for key in ("candidate_id", "recording_date", "bvid", "aid", "cid"):
        _need(authority[key] == source[key], "normal same-BV target identity drifted")
    _need(authority["observed_public_title"] == source["title"],
          "normal same-BV original title drifted")
    if BOUNDARY_FIELDS.intersection(authority):
        from src.autoslice.first_publication_boundary import validate_boundary_fields
        validate_boundary_fields(authority)
    return authority


def replay_first_publication_manifest(authority: Mapping[str, object]) -> dict:
    """Load the original manifest after replaying the authority closure.

    Callers must validate the authority with
    :func:`validate_new_bv_repair_authority` first.  This helper deliberately
    returns the source manifest, rather than a successor package projection,
    so cover-only consumers can compare immutable media and subtitle bytes.
    """

    from src.autoslice import publication_reconciliation as reconciliation

    _need(isinstance(authority, Mapping), "normal same-BV authority is invalid")
    source = _replay_source(
        authority.get("source_publication"),
        review_manifest_snapshot=authority.get("review_manifest_snapshot"),
    )
    evidence = source.get("evidence")
    _need(isinstance(evidence, Mapping), "first-publication evidence closure is missing")
    manifest_entry = evidence.get("manifest")
    _need(isinstance(manifest_entry, Mapping), "first-publication manifest evidence is missing")
    for key in ("candidate_id", "recording_date", "bvid", "aid", "cid"):
        _need(authority.get(key) == source.get(key), "normal same-BV target identity drifted")
    _need(
        authority.get("observed_public_title") == source.get("title"),
        "normal same-BV original title drifted",
    )
    return reconciliation._load_object(
        Path(str(manifest_entry.get("path") or "")),
        "first upload manifest",
    )
