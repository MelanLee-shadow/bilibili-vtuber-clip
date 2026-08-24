#!/usr/bin/env python3
"""Create C1's create-only six-point technical-review template."""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from src.autoslice.fastlane_c1_technical_receipt import C1TechnicalReceiptError, template
def main(argv=None):
 p=argparse.ArgumentParser(); p.add_argument('package_root',type=Path); p.add_argument('--package-audit',required=True,type=Path); p.add_argument('--out',type=Path); a=p.parse_args(argv); root=a.package_root.resolve(); out=(a.out or root/'verification/c1-technical-receipt-evidence.v1.json').resolve()
 if not out.is_relative_to(root) or out.exists() or not out.parent.is_dir() or out.parent.is_symlink(): raise ValueError('C1_TECHNICAL_TEMPLATE_OUTPUT_UNSAFE')
 fd=os.open(out,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w',encoding='utf-8') as h: h.write(json.dumps(template(root,a.package_audit),ensure_ascii=False,sort_keys=True,indent=2)+'\n'); h.flush(); os.fsync(h.fileno())
 print(json.dumps({'status':'TEMPLATE_CREATED','path':str(out),'accepted':False})); return 0
if __name__=='__main__':
 try: raise SystemExit(main())
 except (ValueError,OSError,C1TechnicalReceiptError) as exc: print(f'REFUSE: {exc}',file=sys.stderr); raise SystemExit(2)
