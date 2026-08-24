#!/usr/bin/env python3
"""Create-only C2 root technical-review proposal; it cannot accept or upload."""
from __future__ import annotations
import argparse, json, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from src.autoslice.fastlane_c2_formal_adapter import CID, NAMES, TITLE, sha256
def main() -> int:
 p=argparse.ArgumentParser(); p.add_argument('--package',type=Path,required=True); p.add_argument('--out',type=Path,required=True); a=p.parse_args(); root=a.package.resolve(); out=a.out.resolve()
 if out.exists(): raise SystemExit('refusing to overwrite root proposal')
 saved=json.loads((root/'package_audit.json').read_text(encoding='utf-8'))
 run=subprocess.run([sys.executable,str(ROOT/'scripts/audit_lidousha_review_package.py'),'--json',str(root)],capture_output=True,text=True,check=False)
 current=json.loads(run.stdout)
 if run.returncode or current != saved or not current.get('passed') or current.get('blocking_issue_count') != 0: raise SystemExit('C2_AUDIT_REPLAY_DRIFT')
 proposal={'schema_version':'fastlane-c2-root-technical-receipt-proposal.v1','candidate_id':CID,'title':TITLE,'accepted':False,'upload_allowed':False,'status':'PENDING_ROOT_REVIEW','bindings':{'review_manifest_sha256':'sha256:'+sha256(root/'review_manifest.json'),'package_audit_sha256':'sha256:'+sha256(root/'package_audit.json'),'audit_policy_fingerprint':current['policy_fingerprint'],'artifacts':{key:{'path':name,'sha256':'sha256:'+sha256(root/name)} for key,name in NAMES.items()}},'required_root_checks':['cue5 start/mid/end burned evidence','cue21 start/mid/end burned evidence','intro transition','full burned playback','title and cover visual surface'],'reviewer':None,'reviewed_at':None}
 out.write_text(json.dumps(proposal,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); return 0
if __name__=='__main__': raise SystemExit(main())
