"""The same-BV authority of a revision: which published BV a successor package may replace, built from that BV's
first-publication receipt and checked against it again wherever it is used.

Its shape is the one the lane already reads (`new-bv-same-bv-publication-authority.v1`: candidate_id, recording_date, bvid,
aid, cid, observed_public_title, title, title_mode, source_kind, source_publication {path, sha256, bytes}, authority_sha256
over the rest).  Replaying it means: the receipt it binds is still those bytes, is a verified first publication
(`new-bv-publication-reconciliation-authority.v1`, VERIFIED_PUBLIC) of that same BV, and the registry holds that BV's
published row for the same package.  The public tree's `new_bv_repair_authority` is these functions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import registry
from .common import drift

SCHEMA_VERSION = "new-bv-same-bv-publication-authority.v1"
FIRST_SCHEMA = "new-bv-publication-reconciliation-authority.v1"
FIELDS = frozenset({"schema_version", "source_kind", "candidate_id", "recording_date", "bvid", "aid", "cid",
                    "observed_public_title", "title", "title_mode", "source_publication", "authority_sha256"})


class AuthorityError(ValueError):
    """The authority cannot be built or does not replay."""


def _need(condition: bool, detail: str) -> None:
    if not condition:
        raise AuthorityError(detail)


def _digest(value: Mapping[str, object]) -> str:
    raw = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _binding(path: Path) -> dict[str, Any]:
    path = Path(path).absolute()
    _need(path.is_file() and not path.is_symlink(), f"first-publication receipt is not a regular file: {path}")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}


def _source(binding: object) -> dict[str, Any]:
    """The first-publication receipt `binding` names, replayed (see the module docstring)."""
    problem = drift(binding, "first-publication receipt")
    _need(problem is None, str(problem))
    assert isinstance(binding, Mapping)
    source = json.loads(Path(str(binding["path"])).read_text(encoding="utf-8"))
    _need(isinstance(source, dict) and source.get("schema_version") == FIRST_SCHEMA and source.get("status") == "VERIFIED_PUBLIC",
          "the bound receipt is not a verified first publication")
    evidence = source.get("evidence")
    _need(isinstance(evidence, Mapping) and drift(evidence.get("manifest"), "manifest") is None,
          "the first publication's manifest is missing or changed")
    rows = registry.rows_for(registry.load()["entries"], bvid=str(source.get("bvid")))
    _need(len(rows) == 1, f"the registry holds {len(rows)} published rows for {source.get('bvid')}, not one")
    row = rows[0]
    _need(row.get("candidate_id") == source.get("candidate_id") and row.get("recording_date") == source.get("recording_date"),
          "the registry row is another package's")
    projection = row.get("publication_reconciliation") or {}
    if projection.get("status") == "VERIFIED_PUBLIC":           # never repaired: the row binds this very receipt
        _need(dict(projection.get("authority") or {}) == dict(binding), "the registry row binds another receipt")
    return source


def build(first_receipt: Path, *, candidate_id: str, title: str) -> dict[str, Any]:
    binding = _binding(first_receipt)
    source = _source(binding)
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "source_kind": "verified_normal_first_publication",
        "candidate_id": source["candidate_id"], "recording_date": source["recording_date"], "bvid": source["bvid"],
        "aid": source["aid"], "cid": source["cid"], "observed_public_title": source["title"], "title": title,
        "title_mode": "verified_package_repair", "source_publication": binding}
    value["authority_sha256"] = _digest(value)
    return validate(value, candidate_id=candidate_id, expected_final_title=title)


def validate(value: object, *, candidate_id: str, expected_final_title: str | None = None,
             replay_evidence: bool = True) -> dict[str, Any]:
    _need(isinstance(value, Mapping) and set(value) == FIELDS, "same-BV authority: the fields are not the schema's")
    assert isinstance(value, Mapping)
    authority = dict(value)
    digest = authority.pop("authority_sha256")
    _need(digest == _digest(authority), "same-BV authority: authority_sha256 does not match")
    authority["authority_sha256"] = digest
    _need(authority["schema_version"] == SCHEMA_VERSION and authority["source_kind"] == "verified_normal_first_publication"
          and authority["title_mode"] == "verified_package_repair" and authority["candidate_id"] == candidate_id
          and type(authority["aid"]) is int and type(authority["cid"]) is int,
          f"same-BV authority is not for {candidate_id}")
    _need(isinstance(authority["title"], str) and bool(authority["title"])
          and (expected_final_title is None or authority["title"] == expected_final_title),
          "same-BV authority: the final title differs")
    if replay_evidence:
        source = _source(authority["source_publication"])
        _need(all(authority[k] == source[k] for k in ("candidate_id", "recording_date", "bvid", "aid", "cid"))
              and authority["observed_public_title"] == source["title"], "same-BV authority: the target differs from the receipt")
    return authority


def replay_first(authority: Mapping[str, Any]) -> dict[str, Any]:
    """The first publication's manifest (its video and cover are what was online first), after replaying `authority`."""
    source = _source(authority.get("source_publication"))
    _need(all(authority.get(k) == source.get(k) for k in ("candidate_id", "recording_date", "bvid", "aid", "cid")),
          "same-BV authority: the target differs from the receipt")
    return json.loads(Path(str(source["evidence"]["manifest"]["path"])).read_text(encoding="utf-8"))
