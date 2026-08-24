"""Strict C1-only six-point technical receipt; never a generic content review."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .fastlane_c1_formal_adapter import (CID, SIX_NAMED_POINTS, TITLE,
    FastlaneC1FormalAdapterError, _contained, _read_json, load_formal_authority,
    sha256_file, validate_formal_package)

SCHEMA = "fastlane-c1-technical-receipt-evidence.v1"
ROOT_EVIDENCE = "c1.root-review-evidence.v1.json"
ROOT_SHA = "sha256:20f007654235824c35b3bbe2f0af414c39a2b7a1855c0a7f7fd25fcd89eb0ca9"
RULING = "ruling/2026-08-19-ivan-review-batch-rulings.md"
RULING_SHA = "sha256:29bc6e523645ecfbd9dbf15a5bea912dd741a6dced0b54ca41d4f906cebfde49"
PUBLIC_METADATA_SEAL = Path(__file__).resolve().parents[2] / "assets/lidousha/fastlane_c1_private/auto_173005_934_1166.public-metadata-seal.v1.json"
PUBLIC_METADATA_SEAL_SHA = "sha256:7166d3fe01927c14f3446071fd4604cc0cfe944c646a150af7fdc088f5f845bb"
PUBLIC_VERIFY_SHA = "sha256:14d1ba5bb648a87b6c4621b7d5e6ae852df5793c08c5f3d7a2fae04cb2dfe7bc"
PUBLIC_TAGS = (
    "李豆沙", "虚拟主播", "虚拟UP主", "直播切片", "梦限大",
    "夢限大みゅーたいぷ", "BanG Dream", "邦多利", "磕CP", "上头",
)
SEALS = {"line947": {"raw_sha256": "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa", "content_sha256": "sha256:0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b"}, "line1643": {"uuid": "b95d4356-7ad2-4481-b4a7-0b7afa3c35b9", "raw_sha256": "sha256:2269c653fa6be7fb0c20df98a7348d5f5c57176e3e41fe80eb13b85c39307609", "content_sha256": "sha256:61e0ee6e0811fce540efc959d7468354bbd1633c271b140b8eed8ac44e8d010a"}, "line1745": {"uuid": "a79d6670-88b1-43c3-a688-3c9615c1da51", "raw_sha256": "sha256:7f97b7f8b6a7ca9cad7f54e02836a185b41c5ea158d70cb31f0867a174f13329", "content_sha256": "sha256:f5d60aee9cc02d100ec6f2b660ade76f951e7b113fe0ae95397e1ca6d2cbbc69"}}

class C1TechnicalReceiptError(ValueError): pass

def public_metadata_projection() -> dict[str, object]:
    """Return the one sealed, current-public metadata after-image for C1."""
    try:
        seal = json.loads(PUBLIC_METADATA_SEAL.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise C1TechnicalReceiptError("C1_PUBLIC_METADATA_SEAL_INVALID") from exc
    identity = seal.get("public_identity") if isinstance(seal, Mapping) else None
    tags = seal.get("tags") if isinstance(seal, Mapping) else None
    verify = seal.get("public_verify") if isinstance(seal, Mapping) else None
    if not isinstance(seal, Mapping) or set(seal) != {"schema_version", "candidate_id", "public_identity", "public_verify", "tags", "same_bv_only"} or sha256_file(PUBLIC_METADATA_SEAL) != PUBLIC_METADATA_SEAL_SHA or (seal.get("schema_version"), seal.get("candidate_id"), seal.get("same_bv_only")) != ("fastlane-c1-public-metadata-seal.v1", CID, True) or not isinstance(identity, Mapping) or set(identity) != {"bvid", "aid", "cid", "title"} or (identity.get("bvid"), identity.get("aid"), identity.get("cid"), identity.get("title")) != ("BV1os8q61Eya",117132650155234,41126267272,TITLE) or tags != list(PUBLIC_TAGS) or not isinstance(verify, Mapping) or set(verify) != {"schema_version", "status", "sha256"} or (verify.get("schema_version"), verify.get("status"), verify.get("sha256")) != ("authorized-upload-public-verify.v2", "VERIFIED_PUBLIC", PUBLIC_VERIFY_SHA):
        raise C1TechnicalReceiptError("C1_PUBLIC_METADATA_SEAL_INVALID")
    return {"identity": dict(identity), "tags": list(PUBLIC_TAGS), "seal_sha256": PUBLIC_METADATA_SEAL_SHA, "public_verify": dict(verify)}


PROJECTION_SCHEMA = "fastlane-c1-authorized-same-bv-projection.v1"


def projection_authority() -> dict[str, object]:
    """The one C1-only recovery target accepted by the same-BV state machine."""
    public = public_metadata_projection()
    identity = public["identity"]
    return {
        "schema_version": PROJECTION_SCHEMA,
        "candidate_id": CID,
        "recording_date": "2026-08-11",
        "formal_review_schema": "fastlane-c1-formal-private-review-manifest.v1",
        "formal_authority_sha256": load_formal_authority()["authority_sha256"],
        "public_metadata_seal_sha256": PUBLIC_METADATA_SEAL_SHA,
        "public_verify_sha256": PUBLIC_VERIFY_SHA,
        "bvid": identity["bvid"],
        "aid": identity["aid"],
        "cid": identity["cid"],
        "title": identity["title"],
        "tags": list(PUBLIC_TAGS),
        "same_bv_only": True,
    }


def validate_projection_authority(value: object, *, candidate_id: str, expected_final_title: str | None = None) -> dict[str, object]:
    expected = projection_authority()
    if not isinstance(value, Mapping) or dict(value) != expected or candidate_id != CID or (expected_final_title is not None and expected_final_title != TITLE):
        raise C1TechnicalReceiptError("C1_AUTHORIZED_PROJECTION_INVALID")
    return expected


def validate_authorized_projection_manifest(manifest: object, *, verify_audit: bool = True) -> None:
    """Replay C1's non-generic same-BV manifest without relaxing v3 rules."""
    required = {
        "manifest_version", "schema_version", "artifact_id", "video", "cover", "title",
        "description", "publish_policy", "season", "package_attestation", "authorization",
        "created_at", "tags", "tags_source", "recovery_publication_authority",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != required:
        raise C1TechnicalReceiptError("C1_AUTHORIZED_MANIFEST_SCHEMA_INVALID")
    authorization = manifest.get("authorization")
    if manifest.get("manifest_version") != 3 or manifest.get("schema_version") != "authorized-upload-manifest.v3" or manifest.get("title") != TITLE or manifest.get("tags") != list(PUBLIC_TAGS) or manifest.get("tags_source") != "c1-public-metadata-seal.v1" or manifest.get("recovery_publication_authority") != projection_authority() or not isinstance(authorization, Mapping) or authorization.get("by") != "Ivan" or not isinstance(authorization.get("quote"), str) or "快车道上传" not in authorization["quote"]:
        raise C1TechnicalReceiptError("C1_AUTHORIZED_MANIFEST_BINDING_INVALID")
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, Mapping) or set(attestation) != {"package_root", "review_manifest", "package_audit", "c1_technical_receipt"}:
        raise C1TechnicalReceiptError("C1_AUTHORIZED_MANIFEST_ATTESTATION_INVALID")
    root_text = attestation.get("package_root")
    root = Path(str(root_text or ""))
    if not isinstance(root_text, str) or not root.is_dir() or str(root.resolve()) != root_text:
        raise C1TechnicalReceiptError("C1_AUTHORIZED_MANIFEST_ROOT_INVALID")
    authority = load_formal_authority()
    names = authority["output_names"]
    expected_files = {
        "review_manifest": "review_manifest.json",
        "package_audit": "package_audit.json",
        "c1_technical_receipt": "c1.technical-receipt.accepted.v1.json",
    }
    entries: dict[str, Path] = {}
    for key, filename in expected_files.items():
        entry = attestation.get(key)
        if not isinstance(entry, Mapping) or set(entry) != {"path", "sha256", "bytes"}:
            raise C1TechnicalReceiptError("C1_AUTHORIZED_MANIFEST_ATTESTATION_INVALID")
        path = Path(str(entry.get("path") or ""))
        if not path.is_file() or path.parent.resolve() != root.resolve() or path.name != filename or str(path.resolve()) != entry.get("path") or sha256_file(path)[7:] != entry.get("sha256") or path.stat().st_size != entry.get("bytes"):
            raise C1TechnicalReceiptError("C1_AUTHORIZED_MANIFEST_ATTESTATION_INVALID")
        entries[key] = path
    bindings = closure(root, entries["package_audit"])
    if verify_audit and not bindings["audit"]:
        raise C1TechnicalReceiptError("C1_AUTHORIZED_MANIFEST_AUDIT_INVALID")
    validate_completed(_read_json(entries["c1_technical_receipt"], label="TECHNICAL_RECEIPT"), root, entries["package_audit"])
    for key, filename in (("video", names["burned_final"]), ("cover", names["cover"])):
        entry = manifest.get(key)
        if not isinstance(entry, Mapping) or set(entry) != {"path", "sha256", "bytes"}:
            raise C1TechnicalReceiptError("C1_AUTHORIZED_MANIFEST_ARTIFACT_INVALID")
        path = Path(str(entry.get("path") or ""))
        if not path.is_file() or path.parent.resolve() != root.resolve() or path.name != filename or str(path.resolve()) != entry.get("path") or sha256_file(path)[7:] != entry.get("sha256") or path.stat().st_size != entry.get("bytes"):
            raise C1TechnicalReceiptError("C1_AUTHORIZED_MANIFEST_ARTIFACT_INVALID")

def _point_hash(value: Mapping[str, object]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()

def _audit(root: Path, path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink(): raise C1TechnicalReceiptError("C1_AUDIT_FILE_INVALID")
    try: saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc: raise C1TechnicalReceiptError("C1_AUDIT_FILE_INVALID") from exc
    script = Path(__file__).resolve().parents[2] / "scripts/audit_lidousha_review_package.py"
    run = subprocess.run([sys.executable, str(script), str(root), "--json"], capture_output=True, text=True, check=False)
    try: current = json.loads(run.stdout)
    except json.JSONDecodeError as exc: raise C1TechnicalReceiptError("C1_AUDIT_REPLAY_FAILED") from exc
    # The accepted C1 audit was made in its isolated private package.  A
    # copied formal package has an intentionally different absolute root, but
    # no other audit input or result may change.  Keep this relocation rule
    # private to C1; generic package-audit binding remains byte-for-byte.
    saved_relocated, current_relocated = dict(saved), dict(current)
    saved_relocated["root"] = str(root.resolve())
    current_relocated["root"] = str(root.resolve())
    if run.returncode or saved_relocated != current_relocated or not current.get("passed") or current.get("blocking_issue_count") != 0: raise C1TechnicalReceiptError("C1_AUDIT_REPLAY_DRIFT")
    return current

def _direct_authority(root: Path) -> dict[str, object]:
    path = _contained(root, RULING, label="DIRECT_UPLOAD_RULING")
    if sha256_file(path) != RULING_SHA: raise C1TechnicalReceiptError("C1_DIRECT_UPLOAD_RULING_HASH_DRIFT")
    text = path.read_text(encoding="utf-8")
    values = ("| line 1643 |", SEALS["line1643"]["uuid"], SEALS["line1643"]["raw_sha256"].removeprefix("sha256:"), "| line 1745 |", SEALS["line1745"]["uuid"], SEALS["line1745"]["raw_sha256"].removeprefix("sha256:"), SEALS["line947"]["raw_sha256"].removeprefix("sha256:"), SEALS["line947"]["content_sha256"].removeprefix("sha256:"))
    if not all(item in text for item in values): raise C1TechnicalReceiptError("C1_DIRECT_UPLOAD_AUTHORITY_DRIFT")
    return {"ruling_document_sha256": RULING_SHA, "seals": json.loads(json.dumps(SEALS))}

def closure(root: Path, audit_path: Path) -> dict[str, Any]:
    try: validate_formal_package(root)
    except FastlaneC1FormalAdapterError as exc: raise C1TechnicalReceiptError(str(exc)) from exc
    evidence_path = _contained(root, ROOT_EVIDENCE, label="ROOT_EVIDENCE")
    if sha256_file(evidence_path) != ROOT_SHA: raise C1TechnicalReceiptError("C1_ROOT_EVIDENCE_HASH_DRIFT")
    evidence = _read_json(evidence_path, label="ROOT_EVIDENCE")
    if (evidence.get("candidate_id"), evidence.get("title"), evidence.get("ivan_rereview_required"), len(evidence.get("points", []))) != (CID, TITLE, False, 6): raise C1TechnicalReceiptError("C1_ROOT_EVIDENCE_STRUCTURE_DRIFT")
    audit, authority = _audit(root, audit_path), load_formal_authority(); names = authority["output_names"]
    identity = _read_json(_contained(root, str(names["public_identity"]), label="PUBLIC_IDENTITY"), label="PUBLIC_IDENTITY").get("identity")
    if not isinstance(identity, Mapping) or (identity.get("bvid"), identity.get("aid"), identity.get("cid"), identity.get("title")) != ("BV1os8q61Eya", 117132650155234, 41126267272, TITLE): raise C1TechnicalReceiptError("C1_PUBLIC_IDENTITY_DRIFT")
    artifacts = {key: {"path": str(names[name]), "sha256": sha256_file(_contained(root, str(names[name]), label=key.upper()))} for key, name in {"video":"burned_final", "subtitle":"successor_srt", "cover":"cover"}.items()}
    return {"formal_authority_sha256":authority["authority_sha256"], "root_evidence_sha256":ROOT_SHA, "audit":{"path":audit_path.name,"sha256":sha256_file(audit_path),"policy_fingerprint":audit["policy_fingerprint"]}, "direct_upload_authority":_direct_authority(root), "artifacts":artifacts, "publication_target":{"bvid":"BV1os8q61Eya","aid":117132650155234,"cid":41126267272,"title":TITLE}}

def template(root: Path, audit_path: Path) -> dict[str, Any]:
    bindings = closure(root, audit_path); evidence = _read_json(_contained(root, ROOT_EVIDENCE, label="ROOT_EVIDENCE"), label="ROOT_EVIDENCE")
    by_id = {point.get("point_id"):point for point in evidence["points"] if isinstance(point, Mapping)}; points = []
    for named in SIX_NAMED_POINTS:
        point = by_id.get(named["point_id"])
        if not isinstance(point, Mapping) or point.get("expected") != named["expected"]: raise C1TechnicalReceiptError("C1_ROOT_EVIDENCE_POINT_DRIFT")
        points.append({"point_id":named["point_id"],"expectation":named["expected"],"verdict":None,"evidence":None,"root_evidence_point_sha256":_point_hash(point)})
    return {"schema_version":SCHEMA,"candidate_id":CID,"status":"TECHNICAL_REVIEW_REQUIRED","accepted":False,"reviewed_by":None,"reviewed_at":None,"upload_allowed":False,"bindings":bindings,"six_named_points":points}

def validate_completed(value: object, root: Path, audit_path: Path) -> dict[str, Any]:
    fields={"schema_version","candidate_id","status","accepted","reviewed_by","reviewed_at","upload_allowed","bindings","six_named_points"}
    if not isinstance(value, Mapping) or set(value) != fields: raise C1TechnicalReceiptError("C1_RECEIPT_SCHEMA_INVALID")
    expected=template(root,audit_path)
    if (value.get("schema_version"),value.get("candidate_id"),value.get("status"),value.get("accepted"),value.get("reviewed_by"),value.get("upload_allowed"),value.get("bindings")) != (SCHEMA,CID,"ACCEPTED_FOR_SAME_BV_TECHNICAL",True,"Codex root",False,expected["bindings"]): raise C1TechnicalReceiptError("C1_RECEIPT_BINDING_INVALID")
    try: timestamp = datetime.fromisoformat(str(value.get("reviewed_at", "")).replace("Z", "+00:00"))
    except ValueError: raise C1TechnicalReceiptError("C1_RECEIPT_REVIEWED_AT_INVALID") from None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None: raise C1TechnicalReceiptError("C1_RECEIPT_REVIEWED_AT_INVALID")
    points=value.get("six_named_points")
    if not isinstance(points,list) or len(points)!=6: raise C1TechnicalReceiptError("C1_RECEIPT_POINT_SET_INVALID")
    for actual, base in zip(points,expected["six_named_points"],strict=True):
        if not isinstance(actual,Mapping) or set(actual)!=set(base) or actual.get("point_id")!=base["point_id"] or actual.get("expectation")!=base["expectation"] or actual.get("root_evidence_point_sha256")!=base["root_evidence_point_sha256"] or actual.get("verdict")!="PASS" or actual.get("evidence")!=base["root_evidence_point_sha256"]: raise C1TechnicalReceiptError("C1_RECEIPT_POINT_INVALID")
    return dict(value)
