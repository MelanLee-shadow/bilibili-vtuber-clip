"""Explicit intake of an already-public manual Talk delivery, never an upload gate.

A queued candidate may have been produced through the native manual lane. Its
verified publication is a fact, not permission to synthesize a review_ready row.
The plan below preserves the original queue row and uses existing native public
and package validators before moving exactly that row into published picks.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from . import publication_reconciliation as reconciliation
from .publication_state_projection import (
    PublicationReconciliationError,
    project_publication_closure,
)

SCHEMA = "published-manual-queue-intake.v1"
_ACTIVE = ("picks", "songs", "pending_talk", "talk_backlog", "pending_song", "song_backlog")


def _identity(row: dict) -> str:
    return str(row.get("candidate_id") or row.get("cid") or "")


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def verified_manual_inputs(manifest_path: Path, bvid: str) -> dict:
    from scripts import authorized_upload as uploader
    from scripts.audit_review_package import audit_package

    manifest, problems = uploader.load_and_verify(manifest_path, ordinary_upload=True)
    if problems:
        raise PublicationReconciliationError("manual publication manifest failed native validation")
    if (manifest.get("season") or {}).get("lane") != "talk":
        raise PublicationReconciliationError("manual queue intake is Talk-only")
    authority_path = reconciliation.authority_sidecar_path(manifest_path)
    authority = reconciliation._load_object(authority_path, "native public authority")
    if authority.get("schema_version") != "new-bv-publication-reconciliation-authority.v1":
        raise PublicationReconciliationError("manual intake requires a new-BV public authority")
    evidence = authority["evidence"]
    manifest_bound = reconciliation._validate_sha_entry(evidence["manifest"], "public manifest")
    if manifest_bound != manifest_path.resolve():
        raise PublicationReconciliationError("manual intake manifest identity differs")
    checked, aid, cid = reconciliation._validate_new_public_evidence(
        manifest=manifest, manifest_path=manifest_path, bvid=bvid,
        public_verify_path=reconciliation._validate_sha_entry(evidence["public_verify"], "public"),
        season_verify_path=reconciliation._validate_sha_entry(evidence["season_verify"], "section"),
        uploaded_path=reconciliation._validate_sha_entry(evidence["uploaded"], "uploaded"),
    )
    candidate, date = reconciliation._candidate_and_date(manifest)
    if any(authority.get(k) != v for k, v in {
        "candidate_id": candidate, "recording_date": date, "bvid": bvid,
        "aid": aid, "cid": cid, "status": "VERIFIED_PUBLIC",
    }.items()) or checked != evidence:
        raise PublicationReconciliationError("manual public authority identity or evidence differs")
    attestation = manifest["package_attestation"]
    review_path = reconciliation._validate_sha_entry(attestation["review_manifest"], "manual review")
    review = reconciliation._load_object(review_path, "manual review")
    manual = review.get("manual_attestation") or {}
    if (review.get("schema_version") != "lidousha-manual-review-manifest.v1"
            or review.get("run_mode") != "MANUAL_PRODUCE_REVIEW"
            or manual.get("spec_lane") != "produce_slice_package"
            or not str(manual.get("operator") or "").strip()
            or review.get("date") != date or review.get("candidate_id") != candidate):
        raise PublicationReconciliationError("not a source-bound native manual delivery")
    record_path = reconciliation._validate_sha_entry(attestation["record"], "manual record")
    record = reconciliation._load_object(record_path, "manual record")
    if (record.get("artifact_role") == "DIAGNOSTIC_TRAINING"
            or record.get("release_excluded") is True
            or record.get("story_contract", {}).get("candidate_id") != candidate):
        raise PublicationReconciliationError("manual record is excluded or has a different identity")
    audit = audit_package(Path(attestation["package_root"]))
    if not audit.get("passed") or audit.get("issues"):
        raise PublicationReconciliationError("current manual package audit failed")
    media = Path(record["media_path"])
    provenance_path = media.with_suffix(".provenance.json")
    provenance = reconciliation._load_object(provenance_path, "source recut provenance")
    recut, piece = provenance["final_recut"], provenance["source_piece"]
    media_sha = reconciliation.sha256_file(media)
    if (recut["output_sha256"] != media_sha
            or record["artifact_hashes"]["video_sha256"].removeprefix("sha256:") != media_sha
            or recut["output_path"] != str(media)):
        raise PublicationReconciliationError("manual source media binding differs")
    lo, hi = recut["absolute_source_start_ms"], recut["absolute_source_end_ms"]
    if type(lo) is not int or type(hi) is not int or not 0 <= lo < hi:
        raise PublicationReconciliationError("manual source interval invalid")
    publication = {
        "schema_version": reconciliation.RECONCILIATION_SCHEMA, "status": "VERIFIED_PUBLIC",
        "candidate_id": candidate, "recording_date": date, "bvid": bvid,
        "aid": aid, "cid": cid, "title": manifest["title"],
        "authority": reconciliation._sha_entry(authority_path),
        "reconciled_at": authority["created_at"],
    }
    return {
        "publication": publication, "recording_basename": Path(piece["source_path"]).name,
        "source_interval_ms": [lo, hi], "source_media_sha256": piece["source_sha256"],
        "manifest": reconciliation._sha_entry(manifest_path),
        "record": reconciliation._sha_entry(record_path),
        "review_manifest": reconciliation._sha_entry(review_path),
        "provenance": reconciliation._sha_entry(provenance_path),
        "video_path": manifest["video"]["path"],
        "subtitle_path": attestation["subtitle"]["path"],
        "video_sha256": manifest["video"]["sha256"],
        "cover_path": manifest["cover"]["path"],
        "cover_sha256": manifest["cover"]["sha256"],
    }


def plan_manual_publication_intake(state: dict, verified: dict) -> tuple[dict, dict]:
    """Pure exact-row projection; callers must supply native-validated inputs."""
    publication = verified["publication"]
    candidate, date = publication["candidate_id"], publication["recording_date"]
    if state.get("date") not in (None, date) or publication.get("status") != "VERIFIED_PUBLIC":
        raise PublicationReconciliationError("manual intake date/public status conflicts")
    found = [(lane, i, row) for lane in _ACTIVE
             for i, row in enumerate(state.get(lane) or [])
             if isinstance(row, dict) and _identity(row) == candidate]
    if len(found) != 1:
        raise PublicationReconciliationError("manual intake requires exactly one active candidate")
    lane, index, row = found[0]
    if lane == "picks" and row.get("status") == "published":
        prior = row.get("manual_publication_intake") or {}
        if (row.get("publication_reconciliation") == publication
                and prior.get("schema_version") == SCHEMA
                and prior.get("manifest") == verified["manifest"]):
            return deepcopy(state), {"status": "ALREADY_APPLIED", "state_changed": False}
        raise PublicationReconciliationError("existing published manual row conflicts")
    if lane != "pending_talk" or row.get("bvid") or row.get("published_bvid"):
        raise PublicationReconciliationError("manual intake is not an unconsumed Talk queue row")
    source = Path(str(row.get("segment_path") or "")).name
    lo, hi = verified["source_interval_ms"]
    start, end = row.get("start_ms"), row.get("end_ms")
    if (source != verified["recording_basename"] or type(start) is not int
            or type(end) is not int or not lo <= start < end <= hi):
        raise PublicationReconciliationError("manual queue source identity/coverage conflicts")
    receipt = {
        "schema_version": SCHEMA, "source_collection": lane, "source_index": index,
        "original_queue_row": deepcopy(row), "original_queue_row_sha256": _hash(row),
        "manifest": deepcopy(verified["manifest"]), "record": deepcopy(verified["record"]),
        "review_manifest": deepcopy(verified["review_manifest"]),
        "provenance": deepcopy(verified["provenance"]),
        "source_media_sha256": verified["source_media_sha256"],
        "source_interval_ms": [lo, hi], "prepublication_quality_synthesized": False,
    }
    after = deepcopy(state)
    published = after[lane].pop(index)
    published.update({
        "candidate_id": candidate, "status": "published", "rc": 0,
        "bvid": publication["bvid"], "aid": publication["aid"],
        "published_cid": publication["cid"], "publication_reconciliation": deepcopy(publication),
        "manual_publication_intake": receipt, "video_sha256": verified["video_sha256"],
        "cover_path": verified["cover_path"], "cover_sha256": verified["cover_sha256"],
        "summary": {"candidate_id": candidate, "delivery": verified["video_path"],
                    "subtitle": verified["subtitle_path"], "title": publication["title"],
                    "record": verified["record"]["path"]},
    })
    after.setdefault("picks", []).append(published)
    after["publication_closure"] = project_publication_closure(after)
    # Recording/maintenance pause is independent of one observed publication.
    assert after.get("status") == state.get("status")
    for key in set(state) | set(after):
        if key not in {"picks", "pending_talk", "publication_closure"}:
            assert after.get(key) == state.get(key)
    return after, {"status": "PLANNED", "state_changed": True, "candidate_id": candidate,
                   "bvid": publication["bvid"], "receipt": receipt}


def already_applied_manual_intake(state: dict, manifest_path: Path, bvid: str) -> bool:
    """Recognize a bound completed intake without replaying mutable public polls.

    Initial intake still requires all original public evidence. After completion,
    normal season/public polling can replace its observation files; the immutable
    authority and the actual accepted state/package are the idempotency sources.
    This branch authorizes no state change or renewed publication.
    """
    from scripts import authorized_upload as uploader
    from .publication_state_projection import validate_runtime_registry_entry

    manifest, problems = uploader.load_and_verify(manifest_path, ordinary_upload=True)
    if problems:
        raise PublicationReconciliationError("applied manual manifest failed native validation")
    candidate, date = reconciliation._candidate_and_date(manifest)
    found = [(lane, row) for lane in _ACTIVE for row in (state.get(lane) or [])
             if isinstance(row, dict) and _identity(row) == candidate]
    if len(found) != 1 or found[0][0] != "picks":
        return False
    row = found[0][1]
    receipt = row.get("manual_publication_intake")
    if row.get("status") != "published" or not isinstance(receipt, dict):
        return False
    publication = row.get("publication_reconciliation")
    if (receipt.get("schema_version") != SCHEMA or row.get("bvid") != bvid
            or not isinstance(publication, dict) or publication.get("status") != "VERIFIED_PUBLIC"):
        raise PublicationReconciliationError("completed manual intake identity conflicts")
    validate_runtime_registry_entry({"status": "published", "candidate_id": candidate,
                                    "recording_date": date, "bvid": bvid,
                                    "publication_reconciliation": publication})
    expected = {"manifest": reconciliation._sha_entry(manifest_path),
                "record": manifest["package_attestation"]["record"],
                "review_manifest": manifest["package_attestation"]["review_manifest"]}
    for key in ("manifest", "record", "review_manifest", "provenance"):
        reconciliation._validate_sha_entry(receipt.get(key), "completed manual " + key)
        if key in expected and receipt[key] != expected[key]:
            raise PublicationReconciliationError("completed manual input binding conflicts")
    original = receipt.get("original_queue_row")
    if (not isinstance(original, dict) or _identity(original) != candidate
            or _hash(original) != receipt.get("original_queue_row_sha256")
            or receipt.get("source_collection") != "pending_talk"):
        raise PublicationReconciliationError("completed manual source-row evidence conflicts")
    return True
