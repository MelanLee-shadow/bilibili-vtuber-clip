#!/usr/bin/env python3
"""Create-only C2 media stage; deliberately stops before record/publish/manifest."""
import argparse, hashlib, json, re, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_fastlane_c2_private_successor import CID, load, project

ASS = re.compile(r"^(Dialogue: \d+,)(\d+:\d\d:\d\d\.\d\d),(\d+:\d\d:\d\d\.\d\d)(,.*?,,0,0,0,,)(.*)$")
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser(); p.add_argument('--predecessor-dir',type=Path,required=True); p.add_argument('--sealed-srt',type=Path,required=True); p.add_argument('--out',type=Path,required=True); a=p.parse_args(); auth=load(); src=a.predecessor_dir.resolve(); out=a.out.resolve()
 if out.exists(): raise SystemExit('output-exists')
 if a.sealed_srt.name != f'{CID}.recut.srt' or not a.sealed_srt.is_file() or sha(a.sealed_srt)!='3cb16000b1bb34f205ffd6db524e3a2874108666bb4b34d0b6902654900572c1': raise SystemExit('wrong-stem-or-sealed-SRT-drift')
 expected={f'{CID}.recut.mp4':auth['predecessor']['video_sha256'],f'{CID}.recut.srt':auth['predecessor']['srt_sha256']}
 for n,h in expected.items():
  q=src/n
  if not q.is_file() or sha(q)!=h: raise SystemExit('video-drift' if n.endswith('.mp4') else 'stale-SRT')
 ass=src/f'{CID}.recut.final-sapphire72.ass'
 if not ass.is_file(): raise SystemExit('missing-predecessor-ass')
 out.mkdir(parents=True); shutil.copy2(src/f'{CID}.recut.mp4',out/f'{CID}.recut.mp4'); shutil.copy2(a.sealed_srt,out/f'{CID}.recut.srt')
 lines=[]; changed=0
 for line in ass.read_text().splitlines():
  m=ASS.match(line)
  if m and m.group(2)=='0:00:08.72' and m.group(3)=='0:00:11.24': line=f'{m.group(1)}{m.group(2)},{m.group(3)}{m.group(4)}小豆老公；； 不是你老公'; changed+=1
  lines.append(line)
 if changed!=1: raise SystemExit('manifest-mismatch: expected one cue5 ASS dialogue')
 ap=out/f'{CID}.recut.final-sapphire72.ass'; ap.write_text('\n'.join(lines)+'\n')
 receipt={'schema_version':'fastlane-c2-private-materialization-stage.v1','candidate_id':CID,'upload_allowed':False,'next_required':['canonical record/publish projection','canonical review manifest','canonical package audit','canonical title-cover QC'],'authority_sha256':sha(Path('assets/lidousha/fastlane_c2_private/auto_203011_328_389.correction-authority.v1.json')),'artifacts':{x.name:sha(x) for x in out.iterdir()}}
 (out/'fastlane-c2-private-materialization-stage.v1.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n'); print(json.dumps(receipt,ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
