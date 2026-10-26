"""Repair only policy fields corrupted by a legacy new-upload wrapper.

The immutable intent is the dispatch boundary: a resumed command only observes.
Publication closure remains owned by the existing season-add command.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import time
from contextlib import nullcontext
from pathlib import Path

from src.autoslice import publish_policy as archive_policy
from src.autoslice.authorized_upload_metadata import normalise_tags
from src.autoslice.authorized_upload_published_recovery import uploaded_sidecar_path
from src.autoslice.authorized_upload_recovery_cli import manifest_ledger_binding, write_json_sidecar
from src.autoslice.same_bv_repair import normalise_creator_snapshot


FIELDS = ("tid", "desc", "copyright", "source")
ALLOWED_PROBLEMS = {f"{surface} {field} mismatch"
                    for surface in ("public", "Creator archive") for field in FIELDS}
SCHEMA = "new-upload-policy-repair.v1"


class PolicyRepairError(ValueError):
    pass


def parse_args(argv, *, default_ledger, default_cookie):
    parser = argparse.ArgumentParser(prog="authorized_upload.py policy-repair",
                                     description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--bvid", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lock", default=None)
    parser.add_argument("--ledger", default=str(default_ledger))
    parser.add_argument("--cookie-json", default=str(default_cookie))
    parser.add_argument("--wait", type=float, default=240.0)
    parser.add_argument("--poll", type=float, default=30.0)
    return parser.parse_args(argv)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise PolicyRepairError("POLICY_REPAIR_EVIDENCE_MISSING_OR_UNSAFE")
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise PolicyRepairError("POLICY_REPAIR_EVIDENCE_INVALID")
    return value, hashlib.sha256(raw).hexdigest()


def _stable(snapshot):
    value = copy.deepcopy(snapshot)
    for surface in ("creator", "public"):
        for field in FIELDS:
            value[surface]["metadata"].pop(field, None)
    return value


def _latest_proof(path):
    # Canonical evidence is replaced until VERIFIED_PUBLIC; every observation
    # is retained separately. Prefer the latest verified_at, including a newer
    # observation written just before a crash replacing the canonical sidecar.
    candidates = [path, *path.parent.glob(f"{path.stem}.observation-*.json")]
    from datetime import datetime

    proofs = []
    for candidate in candidates:
        proof, sha = _read(candidate)
        try:
            stamp = datetime.fromisoformat(proof["verified_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("timezone required")
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            raise PolicyRepairError("POLICY_REPAIR_PROOF_TIME_INVALID") from exc
        proofs.append((stamp.timestamp(), candidate.stat().st_mtime_ns, candidate, proof, sha))
    _, _, candidate, proof, sha = max(proofs, key=lambda row: row[:2])
    return candidate, proof, sha


def _admit(args, manifest_path, *, load_and_verify, read_ledger, public_verify_path):
    ledger = Path(args.ledger)
    manifest, problems = load_and_verify(manifest_path)
    if (manifest is not None and manifest.get("manifest_version") == 3
            and manifest.get("recovery_publication_authority") is None
            and any("PUBLISH_POLICY_LANE_DRIFT" in p for p in problems)):
        manifest, problems = load_and_verify(manifest_path, posted_policy_ledger=ledger)
    if problems or manifest is None:
        raise PolicyRepairError("POLICY_REPAIR_MANIFEST_INVALID:" + ";".join(problems))
    if (manifest.get("manifest_version") != 3
            or any(key in manifest for key in ("recovery_publication_authority", "same_bv_authority"))):
        raise PolicyRepairError("POLICY_REPAIR_NEW_UPLOAD_ONLY")
    published = uploaded_sidecar_path(manifest_path)
    if published.exists() or published.is_symlink():
        raise PolicyRepairError("POLICY_REPAIR_ALREADY_CLOSED_OR_UPLOADED_PRESENT")
    policy = archive_policy.checked_policy(manifest.get("publish_policy") or archive_policy.new_upload_policy())
    if policy != archive_policy.new_upload_policy():
        raise PolicyRepairError("POLICY_REPAIR_NEW_POLICY_REQUIRED")
    expected = {**policy, "desc": manifest["description"]}
    entries, problems = read_ledger(ledger)
    if problems:
        raise PolicyRepairError("POLICY_REPAIR_LEDGER_INVALID:" + ";".join(problems))
    binding = manifest_ledger_binding(manifest, manifest_path)
    matches = [row for row in entries
               if row.get("event") == "UPLOAD_ATTEMPT_FINISHED" and row.get("attempt_id")
               and row.get("bvid") == args.bvid
               and all(row.get(key) == value for key, value in binding.items())]
    if not matches or len({row["attempt_id"] for row in matches}) != 1:
        raise PolicyRepairError("POLICY_REPAIR_LEDGER_BVID_BINDING_INVALID")
    proof_path, proof, proof_sha = _latest_proof(public_verify_path(manifest_path))
    problems = proof.get("problems")
    if (proof.get("schema_version") != "authorized-upload-public-verify.v2"
            or proof.get("status") != "PUBLIC_VERIFY_FAILED" or proof.get("bvid") != args.bvid
            or not isinstance(problems, list) or not problems
            or any(not isinstance(p, str) or p not in ALLOWED_PROBLEMS for p in problems)):
        raise PolicyRepairError("POLICY_REPAIR_NON_POLICY_PROBLEM_OR_BVID_DRIFT")
    frozen = proof.get("expected") or {}
    if (any(frozen.get(key) != value for key, value in policy.items())
            or frozen.get("description") != expected["desc"]
            or frozen.get("title") != manifest["title"]
            or frozen.get("tags") != manifest["tags"]):
        raise PolicyRepairError("POLICY_REPAIR_PROOF_EXPECTATION_DRIFT")
    creator, public = proof.get("member_archive") or {}, proof.get("public_view") or {}
    aid, cid, section = public.get("aid"), public.get("cid"), frozen.get("section_id")
    if (creator.get("bvid") != args.bvid or creator.get("aid") != aid
            or any(type(v) is not int or v < 1 for v in (aid, cid, section))):
        raise PolicyRepairError("POLICY_REPAIR_PROOF_IDENTITY_INVALID")
    binding.update(bvid=args.bvid, aid=aid, cid=cid, section_id=section,
                   attempt_id=matches[0]["attempt_id"], expected=expected,
                   public_verify=str(proof_path), public_verify_sha256=proof_sha)
    return manifest, binding


def _check_live(snapshot, binding, manifest):
    if not all(snapshot[s]["available"] for s in ("creator", "public", "section")):
        raise PolicyRepairError("POLICY_REPAIR_OBSERVATION_UNAVAILABLE")
    creator, public, section = (snapshot[s] for s in ("creator", "public", "section"))
    if (creator["bvid"] != binding["bvid"] or public["bvid"] != binding["bvid"]
            or creator["aid"] != binding["aid"] or public["aid"] != binding["aid"]
            or public["cid"] != binding["cid"]
            or [v["cid"] for v in creator["videos"]] != [binding["cid"]]
            or section["section_id"] != binding["section_id"]
            or len(section["matches"]) != 1
            or section["matches"][0].get("aid") != binding["aid"]
            or section["matches"][0].get("cid") != binding["cid"]
            or section["matches"][0].get("title") != manifest["title"]):
        raise PolicyRepairError("POLICY_REPAIR_LIVE_IDENTITY_DRIFT")
    for surface in (creator, public):
        if (surface["state"] != 0 or surface["metadata"]["title"] != manifest["title"]
                or set(surface["metadata"]["tags"]) != set(manifest["tags"])):
            raise PolicyRepairError("POLICY_REPAIR_LIVE_NON_POLICY_DRIFT")


def _verified(snapshot, expected):
    # Public view does not expose source; the same contract as public_verify:
    # source is read from Creator, the remaining fields from both surfaces.
    return all(snapshot[s]["metadata"].get(field) == value
               for s in ("creator", "public") for field, value in expected.items()
               if s == "creator" or field != "source")


def run(args, *, load_and_verify, read_ledger, public_verify_path, adapter_factory,
        upload_lock, default_lock, create_json, description_for_title,
        clock=time.monotonic, sleeper=time.sleep):
    if any(not math.isfinite(v) or v < 0 for v in (args.wait, args.poll)):
        raise PolicyRepairError("POLICY_REPAIR_WAIT_INVALID")
    from src.autoslice.authorized_upload_recovery_cli import valid_bvid
    if not valid_bvid(args.bvid):
        raise PolicyRepairError("POLICY_REPAIR_BVID_INVALID")
    manifest_path = Path(args.manifest).absolute()
    intent_path = manifest_path.with_name(manifest_path.name + ".policy-repair.intent.json")
    receipt_path = manifest_path.with_name(manifest_path.name + ".policy-repair.json")
    if receipt_path.is_symlink():
        raise PolicyRepairError("POLICY_REPAIR_RECEIPT_UNSAFE")
    # Dry-run must not create the upload mutex inode or any sidecar.
    lock = nullcontext() if args.dry_run else upload_lock(Path(args.lock) if args.lock else default_lock)
    with lock:
        manifest, binding = _admit(args, manifest_path, load_and_verify=load_and_verify,
                                  read_ledger=read_ledger, public_verify_path=public_verify_path)
        adapter = adapter_factory(Path(args.cookie_json))
        expected = binding["expected"]
        resumed = intent_path.exists() or intent_path.is_symlink()
        if resumed:
            intent, _ = _read(intent_path)
            if (intent.get("schema_version") != SCHEMA or intent.get("binding") != binding
                    or intent.get("before_sha256") != _digest(intent.get("before"))
                    or intent.get("edit_calls") not in (0, 1)):
                raise PolicyRepairError("POLICY_REPAIR_INTENT_BINDING_DRIFT")
        if not resumed or args.dry_run:
            snapshot = adapter.observe(args.bvid, binding["section_id"])
            _check_live(snapshot, binding, manifest)
            raw = adapter.session.archive_view(args.bvid)
            if normalise_creator_snapshot(bvid=args.bvid, creator_data=raw) != snapshot["creator"]:
                raise PolicyRepairError("POLICY_REPAIR_CREATOR_CHANGED_BEFORE_EDIT")
            changes = {field: {"before": raw["archive"].get(field), "after": value}
                       for field, value in expected.items() if raw["archive"].get(field) != value}
            if resumed and _stable(snapshot) != _stable(intent["before"]):
                raise PolicyRepairError("POLICY_REPAIR_NON_POLICY_DRIFT")
        if not resumed:
            intent = {"schema_version": SCHEMA, "status": "EDIT_INTENT", "binding": binding,
                      "before": snapshot, "before_sha256": _digest(snapshot),
                      "changes": changes, "edit_calls": int(bool(changes))}
        if args.dry_run:
            return 0, {**intent, "status": "DRY_RUN", "changes": changes,
                       "edit_calls": 0, "receipt": str(receipt_path)}
        if not resumed:
            payload = adapter.session.build_edit_payload(raw)
            for field, change in changes.items():
                payload[field] = (description_for_title(manifest["title"], submission=True)
                                  if field == "desc" else change["after"])
            # Persist no csrf, cookies or edit payload. Once this succeeds,
            # every failure path and reentry is observation-only.
            create_json(intent_path, intent)
            if changes:
                try:
                    response = adapter.session.edit_archive(payload)
                    edit_status = "EDIT_ACCEPTED" if response.get("code") == 0 else "EDIT_OUTCOME_UNKNOWN"
                except Exception:
                    edit_status = "EDIT_OUTCOME_UNKNOWN"
            else:
                edit_status = "OBSERVATION_ONLY"
        else:
            edit_status = "OBSERVATION_ONLY"
        receipt = {**intent, "receipt": str(receipt_path), "edit_status": edit_status}
        deadline = clock() + args.wait
        while True:
            try:
                after = adapter.observe(args.bvid, binding["section_id"])
                if not all(after[s]["available"] for s in ("creator", "public", "section")):
                    raise PolicyRepairError("POLICY_REPAIR_OBSERVATION_UNAVAILABLE")
            except Exception:
                after = None
            if after is not None:
                if _stable(after) != _stable(intent["before"]):
                    # Editing may transiently put a previously public archive
                    # into review. Wait for state=0 but reject metadata/media drift.
                    stable_after, stable_before = _stable(after), _stable(intent["before"])
                    for s in ("creator", "public"):
                        for key in ("state", "state_desc"):
                            stable_after[s].pop(key, None)
                            stable_before[s].pop(key, None)
                    if stable_after != stable_before:
                        receipt.update(status="BLOCKED_NON_POLICY_DRIFT", after=after)
                        write_json_sidecar(receipt_path, receipt)
                        return 2, receipt
                if (_verified(after, expected)
                        and all(after[s]["state"] == 0 for s in ("creator", "public"))):
                    receipt.update(status="VERIFIED_POLICY_ONLY", after=after)
                    write_json_sidecar(receipt_path, receipt)
                    return 0, receipt
            if clock() >= deadline:
                receipt.update(status="PUBLIC_PENDING", after=after)
                write_json_sidecar(receipt_path, receipt)
                return 6, receipt
            sleeper(args.poll)
