#!/usr/bin/env python3
"""Create C1's six-point-only technical receipt template; never an upload grant."""
from __future__ import annotations
import argparse, hashlib, json, os, stat, sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from scripts.audit_lidousha_review_package import audit_package
from src.autoslice.fastlane_c1_formal_adapter import (CID, TITLE, SIX_NAMED_POINTS, FastlaneC1FormalAdapterError, _contained, _read_json, load_formal_authority, sha256_file, validate_formal_package)

SCHEMA="fastlane-c1-technical-receipt-evidence.v1"
ROOT_EVIDENCE_NAME="c1.root-review-evidence.v1.json"
ROOT_EVIDENCE_SHA256="sha256:20f007654235824c35b3bbe2f0af414c39a2b7a1855c0a7f7fd25fcd89eb0ca9"
def regular_create(path:Path, root:Path, value:dict)->Path:
    path=path.resolve(); root=root.resolve()
    if not path.is_relative_to(root) or path.exists() or path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir(): raise ValueError("C1_TECHNICAL_TEMPLATE_OUTPUT_UNSAFE")
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as f: f.write(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+"\n"); f.flush(); os.fsync(f.fileno())
    return path
def build(root:Path)->dict:
    validate_formal_package(root)
    auth=load_formal_authority(); names=auth["output_names"]
    evidence=_contained(root,ROOT_EVIDENCE_NAME,label="ROOT_EVIDENCE")
    if sha256_file(evidence)!=ROOT_EVIDENCE_SHA256: raise FastlaneC1FormalAdapterError("C1_ROOT_EVIDENCE_HASH_DRIFT")
    audit=audit_package(root)
    if not audit["passed"]: raise FastlaneC1FormalAdapterError("C1_FORMAL_AUDIT_NOT_PASS")
    identity=_read_json(_contained(root,str(names["public_identity"]),label="PUBLIC_IDENTITY"),label="PUBLIC_IDENTITY")["identity"]
    if not isinstance(identity,dict) or (identity.get("bvid"),identity.get("aid"),identity.get("cid"),identity.get("title")) != ("BV1os8q61Eya",117132650155234,41126267272,TITLE): raise FastlaneC1FormalAdapterError("C1_PUBLIC_IDENTITY_CONTENT_DRIFT")
    artifacts={k:{"path":str(names[n]),"sha256":sha256_file(_contained(root,str(names[n]),label=k.upper()))} for k,n in {"video":"burned_final","subtitle":"successor_srt","cover":"cover"}.items()}
    return {"schema_version":SCHEMA,"candidate_id":CID,"status":"TECHNICAL_REVIEW_REQUIRED","accepted":False,"upload_allowed":False,"reviewed_by":None,"reviewed_at":None,"six_named_points":[{"point_id":p["point_id"],"anchor":p["public_anchor"],"expectation":p["expected"],"verdict":None,"evidence":None} for p in SIX_NAMED_POINTS],"bindings":{"formal_authority_sha256":auth["authority_sha256"],"root_evidence_sha256":ROOT_EVIDENCE_SHA256,"formal_audit":{"passed":True,"policy_fingerprint":audit["policy_fingerprint"]},"artifacts":artifacts,"publication_target":{"bvid":"BV1os8q61Eya","aid":117132650155234,"cid":41126267272,"title":TITLE},"same_bv_only":True},"technical_checks":{"formal_grid":"PASS_BY_FORMAL_ADAPTER","unnamed_subtitles":"FROZEN_BY_FORMAL_GRID","decode_duration":"REQUIRED_AT_REVIEW","predecessor_identity_cover_intro":"PASS_BY_FORMAL_ADAPTER"}}
def main(argv=None):
 p=argparse.ArgumentParser(); p.add_argument("package_root",type=Path); p.add_argument("--out",type=Path); a=p.parse_args(argv); root=a.package_root.resolve(); out=a.out or root/"verification/c1-technical-receipt-evidence.v1.json"; print(json.dumps({"status":"TEMPLATE_CREATED","path":str(regular_create(out,root,build(root))),"accepted":False},ensure_ascii=False)); return 0
if __name__=="__main__":
 try: raise SystemExit(main())
 except (ValueError,OSError,FastlaneC1FormalAdapterError) as e: print(f"REFUSE: {e}",file=sys.stderr); raise SystemExit(2)
