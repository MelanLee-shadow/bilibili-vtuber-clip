"""Same-BV topic-tag correction; reuse the member clone and live observations."""
from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import copy
import hashlib
import json
import time
from pathlib import Path

from src.autoslice.authorized_upload_metadata import validate_tags
from src.autoslice.authorized_upload_published_recovery import uploaded_sidecar_path
from src.autoslice.authorized_upload_recovery_cli import write_json_sidecar
from src.autoslice.same_bv_repair import normalise_creator_snapshot, snapshots_equivalent


def stable_without_tags(snapshot):
    value = copy.deepcopy(snapshot)
    for surface in ("creator", "public"):
        value[surface]["metadata"].pop("tags", None)
        value[surface].pop("state", None)
        value[surface].pop("state_desc", None)
    return value


def _sha(path):
    return _evidence_file_sha256(Path(path))


def _snapshot_sha(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def run(args, *, load_and_verify, adapter_factory, upload_lock, default_lock,
        clock=time.monotonic, sleeper=time.sleep):
    """Persist edit intent once; every resumed invocation is observation-only."""
    receipt_path = Path(args.receipt).resolve()
    with upload_lock(Path(args.lock) if args.lock else default_lock):
        manifest_path = Path(args.manifest).resolve()
        manifest, problems = load_and_verify(manifest_path, frozen_plan_resume=True)
        if problems or manifest is None:
            raise ValueError("TAGS_REPAIR_MANIFEST_INVALID:" + ";".join(problems))
        input_path, result_path = Path(args.tag_input).resolve(), Path(args.tag_result).resolve()
        tag_input, tags_result = json.loads(input_path.read_text()), json.loads(result_path.read_text())
        tags = tags_result.get("final_tags")
        subtitle = manifest["package_attestation"]["subtitle"]
        if (tag_input.get("manifest_sha256") != _sha(manifest_path)
                or tag_input.get("srt_sha256") != subtitle["sha256"]
                or tag_input.get("title") != manifest["title"]
                or tag_input.get("bvid") != args.bvid
                or tags_result.get("status") != "OK"
                or not tags or validate_tags(tags)
                or not set(manifest["tags"]).issubset(tags)):
            raise ValueError("TAGS_REPAIR_PROPOSAL_BINDING_INVALID")
        published_path = uploaded_sidecar_path(manifest_path)
        published = json.loads(published_path.read_text())
        if (published.get("bvid") != args.bvid
                or published.get("manifest_sha256") != _sha(manifest_path)
                or published.get("status") != "VERIFIED_PUBLIC"):
            raise ValueError("TAGS_REPAIR_PUBLISHED_IDENTITY_INVALID")
        public_verify_path = Path(published["public_verify"])
        if _sha(public_verify_path) != published["public_verify_sha256"]:
            raise ValueError("TAGS_REPAIR_PUBLIC_PROOF_DRIFT")
        public_proof = json.loads(public_verify_path.read_text())
        section_id = (public_proof.get("expected") or {}).get("section_id")
        if (public_proof.get("status") != "VERIFIED_PUBLIC"
                or public_proof.get("bvid") != args.bvid
                or not isinstance(section_id, int) or isinstance(section_id, bool) or section_id < 1):
            raise ValueError("TAGS_REPAIR_EXACT_SECTION_PROOF_INVALID")
        binding = {"manifest_sha256": _sha(manifest_path), "published_sha256": _sha(published_path),
                   "public_verify_sha256": _sha(public_verify_path), "section_id": section_id,
                   "tag_input_sha256": _sha(input_path), "tag_result_sha256": _sha(result_path),
                   "bvid": args.bvid, "aid": published["aid"], "cid": published["cid"],
                   "tags": tags, "authorized_by": args.authorized_by, "quote": args.quote}
        if not args.authorized_by or not args.quote.strip():
            raise ValueError("TAGS_REPAIR_AUTHORIZATION_MISSING")
        adapter = adapter_factory(Path(args.cookie_json))
        snapshot = adapter.observe(args.bvid, section_id)
        for surface in ("creator", "public", "section"):
            if not snapshot[surface]["available"]:
                raise ValueError("TAGS_REPAIR_OBSERVATION_UNAVAILABLE")
        creator, public = snapshot["creator"], snapshot["public"]
        if (creator["aid"] != published["aid"] or public["aid"] != published["aid"]
                or public["cid"] != published["cid"]
                or [row["cid"] for row in creator["videos"]] != [published["cid"]]
                or len(snapshot["section"]["matches"]) != 1
                or snapshot["section"]["matches"][0]["cid"] != published["cid"]):
            raise ValueError("TAGS_REPAIR_LIVE_IDENTITY_DRIFT")
        for surface in (creator, public):
            if surface["metadata"]["title"] != manifest["title"]:
                raise ValueError("TAGS_REPAIR_LIVE_TITLE_DRIFT")
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if (receipt.get("binding") != binding
                    or receipt.get("schema_version") != "same-bv-tags-repair.v1"
                    or receipt.get("before_sha256") != _snapshot_sha(receipt.get("before"))
                    or receipt.get("status") not in {"PLANNED", "EDIT_INTENT", "EDIT_ACCEPTED",
                        "EDIT_OUTCOME_UNKNOWN", "PUBLIC_PENDING", "VERIFIED_TAGS_ONLY", "BLOCKED_NON_TAG_DRIFT"}
                    or receipt.get("edit_calls") != (0 if receipt.get("status") == "PLANNED" else 1)):
                raise ValueError("TAGS_REPAIR_RECEIPT_BINDING_DRIFT")
        else:
            if (set(creator["metadata"]["tags"]) != set(manifest["tags"])
                    or set(public["metadata"]["tags"]) != set(manifest["tags"])):
                raise ValueError("TAGS_REPAIR_LIVE_TAGS_DRIFT")
            receipt = {"schema_version": "same-bv-tags-repair.v1", "binding": binding,
                       "before": snapshot, "before_sha256": _snapshot_sha(snapshot),
                       "status": "PLANNED", "edit_calls": 0}
            write_json_sidecar(receipt_path, receipt)
        if stable_without_tags(snapshot) != stable_without_tags(receipt["before"]):
            raise ValueError("TAGS_REPAIR_NON_TAG_DRIFT")
        if args.execute and receipt["status"] == "PLANNED":
            current = adapter.session.archive_view(args.bvid)
            if not snapshots_equivalent(
                    {"creator": normalise_creator_snapshot(bvid=args.bvid, creator_data=current)},
                    {"creator": creator}):
                raise ValueError("TAGS_REPAIR_CREATOR_CHANGED_BEFORE_EDIT")
            payload = adapter.session.build_edit_payload(current, tag=",".join(tags))
            receipt.update(status="EDIT_INTENT", edit_calls=1)
            write_json_sidecar(receipt_path, receipt)
            try:
                response = adapter.session.edit_archive(payload)
                receipt.update(status="EDIT_ACCEPTED", edit_code=response.get("code"))
            except Exception as exc:
                receipt.update(status="EDIT_OUTCOME_UNKNOWN", error=type(exc).__name__)
                write_json_sidecar(receipt_path, receipt)
                return 6, receipt
            write_json_sidecar(receipt_path, receipt)
        if receipt["status"] == "PLANNED":
            return 0, receipt
        deadline = clock() + args.wait
        while True:
            current = adapter.observe(args.bvid, section_id)
            if not all(current[s]["available"] for s in ("creator", "public", "section")):
                if clock() >= deadline:
                    receipt.update(status="PUBLIC_PENDING", after=current)
                    write_json_sidecar(receipt_path, receipt)
                    return 6, receipt
                sleeper(args.poll)
                continue
            if stable_without_tags(current) != stable_without_tags(receipt["before"]):
                receipt.update(status="BLOCKED_NON_TAG_DRIFT", after=current)
                write_json_sidecar(receipt_path, receipt)
                return 2, receipt
            verified = all(current[s]["available"] for s in ("creator", "public", "section")) and (
                current["public"]["state"] == 0 and current["creator"]["state"] == 0
                and all(set(current[s]["metadata"]["tags"]) == set(tags) for s in ("creator", "public")))
            if verified:
                receipt.update(status="VERIFIED_TAGS_ONLY", after=current)
                write_json_sidecar(receipt_path, receipt)
                return 0, receipt
            if clock() >= deadline:
                receipt.update(status="PUBLIC_PENDING", after=current)
                write_json_sidecar(receipt_path, receipt)
                return 6, receipt
            sleeper(args.poll)
