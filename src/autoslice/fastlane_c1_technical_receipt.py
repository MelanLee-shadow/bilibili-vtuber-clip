"""Strict C1-only technical-review closure; never a generic content review."""
from __future__ import annotations
import hashlib, json
from pathlib import Path
from typing import Any, Mapping
from .fastlane_c1_formal_adapter import CID, TITLE, SIX_NAMED_POINTS, FastlaneC1FormalAdapterError, _contained, _read_json, load_formal_authority, sha256_file, validate_formal_package
from scripts.audit_lidousha_review_package import audit_package

SCHEMA="fastlane-c1-technical-receipt-evidence.v1"; ROOT_EVIDENCE="c1.root-review-evidence.v1.json"; ROOT_SHA="sha256:20f007654235824c35b3bbe2f0af414c39a2b7a1855c0a7f7fd25fcd89eb0ca9"
class C1TechnicalReceiptError(ValueError): pass
def _audit(root:Path, audit_path:Path)->dict[str,Any]:
 if not audit_path.is_file() or audit_path.is_symlink(): raise C1TechnicalReceiptError("C1_AUDIT_FILE_INVALID")
 try: saved=json.loads(audit_path.read_text())
 except Exception as e: raise C1TechnicalReceiptError("C1_AUDIT_FILE_INVALID") from e
 current=audit_package(root)
 if saved!=current or not current.get("passed") or current.get("blocking_issue_count")!=0: raise C1TechnicalReceiptError("C1_AUDIT_REPLAY_DRIFT")
 return current
def closure(root:Path,audit_path:Path)->dict[str,Any]:
 try: validate_formal_package(root)
 except FastlaneC1FormalAdapterError as e: raise C1TechnicalReceiptError(str(e)) from e
 evidence=_contained(root,ROOT_EVIDENCE,label="ROOT_EVIDENCE")
 if sha256_file(evidence)!=ROOT_SHA: raise C1TechnicalReceiptError("C1_ROOT_EVIDENCE_HASH_DRIFT")
 e=_read_json(evidence,label="ROOT_EVIDENCE")
 if e.get("candidate_id")!=CID or e.get("title")!=TITLE or e.get("ivan_rereview_required") is not False or len(e.get("points",[]))!=6: raise C1TechnicalReceiptError("C1_ROOT_EVIDENCE_STRUCTURE_DRIFT")
 a=_audit(root,audit_path); au=load_formal_authority(); names=au["output_names"]
 identity=_read_json(_contained(root,str(names["public_identity"]),label="PUBLIC_IDENTITY"),label="PUBLIC_IDENTITY").get("identity")
 if not isinstance(identity,Mapping) or (identity.get("bvid"),identity.get("aid"),identity.get("cid"),identity.get("title")) != ("BV1os8q61Eya",117132650155234,41126267272,TITLE): raise C1TechnicalReceiptError("C1_PUBLIC_IDENTITY_DRIFT")
 artifacts={k:{"path":str(names[n]),"sha256":sha256_file(_contained(root,str(names[n]),label=k.upper()))} for k,n in {"video":"burned_final","subtitle":"successor_srt","cover":"cover"}.items()}
 return {"formal_authority_sha256":au["authority_sha256"],"root_evidence_sha256":ROOT_SHA,"audit":{"path":audit_path.name,"sha256":sha256_file(audit_path),"policy_fingerprint":a["policy_fingerprint"]},"artifacts":artifacts,"publication_target":{"bvid":"BV1os8q61Eya","aid":117132650155234,"cid":41126267272,"title":TITLE}}
def template(root:Path,audit_path:Path)->dict[str,Any]:
 return {"schema_version":SCHEMA,"candidate_id":CID,"status":"TECHNICAL_REVIEW_REQUIRED","accepted":False,"reviewed_by":None,"reviewed_at":None,"upload_allowed":False,"bindings":closure(root,audit_path),"six_named_points":[{"point_id":x["point_id"],"expectation":x["expected"],"verdict":None,"evidence":None} for x in SIX_NAMED_POINTS]}
