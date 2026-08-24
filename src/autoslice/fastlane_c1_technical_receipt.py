"""Strict C1-only six-point technical receipt; never a generic content review."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
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
TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")
SEALS = {"line947": {"raw_sha256": "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa", "content_sha256": "sha256:0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b"}, "line1643": {"uuid": "b95d4356-7ad2-4481-b4a7-0b7afa3c35b9", "raw_sha256": "sha256:2269c653fa6be7fb0c20df98a7348d5f5c57176e3e41fe80eb13b85c39307609"}, "line1745": {"uuid": "a79d6670-88b1-43c3-a688-3c9615c1da51", "raw_sha256": "sha256:7f97b7f8b6a7ca9cad7f54e02836a185b41c5ea158d70cb31f0867a174f13329"}}

class C1TechnicalReceiptError(ValueError): pass

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
    if run.returncode or saved != current or not current.get("passed") or current.get("blocking_issue_count") != 0: raise C1TechnicalReceiptError("C1_AUDIT_REPLAY_DRIFT")
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
    if not isinstance(value.get("reviewed_at"),str) or TIME.fullmatch(value["reviewed_at"]) is None: raise C1TechnicalReceiptError("C1_RECEIPT_REVIEWED_AT_INVALID")
    points=value.get("six_named_points")
    if not isinstance(points,list) or len(points)!=6: raise C1TechnicalReceiptError("C1_RECEIPT_POINT_SET_INVALID")
    for actual, base in zip(points,expected["six_named_points"],strict=True):
        if not isinstance(actual,Mapping) or set(actual)!=set(base) or actual.get("point_id")!=base["point_id"] or actual.get("expectation")!=base["expectation"] or actual.get("root_evidence_point_sha256")!=base["root_evidence_point_sha256"] or actual.get("verdict")!="PASS" or actual.get("evidence")!=base["root_evidence_point_sha256"]: raise C1TechnicalReceiptError("C1_RECEIPT_POINT_INVALID")
    return dict(value)
