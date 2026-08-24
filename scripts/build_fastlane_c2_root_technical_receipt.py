#!/usr/bin/env python3
"""Create-only C2 root technical-review proposal; it cannot accept or upload."""
from __future__ import annotations
import argparse, json, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from src.autoslice.fastlane_c2_technical_receipt import make_ready_proposal
def main() -> int:
 p=argparse.ArgumentParser(); p.add_argument('--package',type=Path,required=True); p.add_argument('--out',type=Path,required=True); a=p.parse_args(); root=a.package.resolve(); out=a.out.resolve()
 if out.exists(): raise SystemExit('refusing to overwrite root proposal')
 saved=json.loads((root/'package_audit.json').read_text(encoding='utf-8'))
 run=subprocess.run([sys.executable,str(ROOT/'scripts/audit_lidousha_review_package.py'),'--json',str(root)],capture_output=True,text=True,check=False)
 current=json.loads(run.stdout)
 if run.returncode or current != saved or not current.get('passed') or current.get('blocking_issue_count') != 0: raise SystemExit('C2_AUDIT_REPLAY_DRIFT')
 proposal=make_ready_proposal(root,current)
 out.write_text(json.dumps(proposal,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); return 0
if __name__=='__main__': raise SystemExit(main())
