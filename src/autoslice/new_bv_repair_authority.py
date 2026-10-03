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

SCHEMA_VERSION = "new-bv-same-bv-publication-authority.v1"
_SOURCE_SCHEMA = "new-bv-publication-reconciliation-authority.v1"
_FIELDS = frozenset({
    "schema_version", "source_kind", "candidate_id", "recording_date",
    "bvid", "aid", "cid", "observed_public_title", "title", "title_mode",
    "source_publication", "authority_sha256",
})


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


def _replay_source(binding: object) -> dict:
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
    candidate, date = reconciliation._candidate_and_date(manifest)
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
) -> dict[str, object]:
    from src.autoslice.publication_reconciliation import _sha_entry

    _regular(publication_path)
    binding = _sha_entry(publication_path)
    source = _replay_source(binding)
    value = {
        "schema_version": SCHEMA_VERSION,
        "source_kind": "verified_normal_first_publication",
        "candidate_id": source["candidate_id"], "recording_date": source["recording_date"],
        "bvid": source["bvid"], "aid": source["aid"], "cid": source["cid"],
        "observed_public_title": source["title"], "title": title,
        "title_mode": "verified_package_repair", "source_publication": binding,
    }
    value["authority_sha256"] = _digest(value)
    return validate_new_bv_repair_authority(value, candidate_id=candidate_id,
                                           expected_final_title=title)


def validate_new_bv_repair_authority(
    value: object, *, candidate_id: str, expected_final_title: str | None = None,
) -> dict[str, object]:
    from src.autoslice.title_policy import publish_title_policy_violations

    _need(isinstance(value, Mapping) and set(value) == _FIELDS,
          "normal same-BV authority schema invalid")
    authority = dict(value)
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
          and not publish_title_policy_violations(title, lane="talk")
          and (expected_final_title is None or title == expected_final_title),
          "normal same-BV successor title invalid or drifted")
    source = _replay_source(authority["source_publication"])
    for key in ("candidate_id", "recording_date", "bvid", "aid", "cid"):
        _need(authority[key] == source[key], "normal same-BV target identity drifted")
    _need(authority["observed_public_title"] == source["title"],
          "normal same-BV original title drifted")
    return authority
