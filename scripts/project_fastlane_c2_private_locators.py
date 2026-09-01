#!/usr/bin/env python3
"""Fail-closed locator projection for a copied C2 private transaction root."""
import argparse,hashlib,json
from pathlib import Path
CID='auto_203011_328_389'; WANT={'auto_203011_328_389.recut.mp4':'eedd41838b6d9d9f0d113f99b5513de45cc4f508c1595fe2099a56b5cdf3c1d6','auto_203011_328_389.recut.srt':'4122f3392a9531d200bf2cd6d6c10b1aeafd60d48f9b58553fdbfacc6ac4bcba'}
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();r=a.root.resolve();o=a.out.resolve()
 if '/opt/' in str(r) or o.exists():raise SystemExit('absolute-prod-path-or-existing-output')
 files={n:r/n for n in WANT}
 if any(not x.is_file() or sha(x)!=WANT[n] for n,x in files.items()):raise SystemExit('wrong-hash-or-stale-record')
 receipt={'schema_version':'fastlane-c2-private-locator-projection.v1','candidate_id':CID,'private_root':str(r),'locators':{n:str(x) for n,x in files.items()},'sha256':{n:sha(x) for n,x in files.items()}}
 o.write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt));
if __name__=='__main__':main()
