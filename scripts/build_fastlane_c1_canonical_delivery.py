#!/usr/bin/env python3
"""Create C1's one canonical, same-BV-only review package (no upload)."""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, stat, subprocess, sys, tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
CID="auto_173005_934_1166"; DATE="2026-08-11"; BVID="BV1os8q61Eya"
TITLE="【李豆沙】经小李判断，薇欧拉对阿拉蕾就是铁暗恋！"
FORMAL_AUDIT="977ed296d3e490344fa6395233ab8a958e09bfbd37c10b8ea19ffe6cce505fa1"
ROOT_EVIDENCE="20f007654235824c35b3bbe2f0af414c39a2b7a1855c0a7f7fd25fcd89eb0ca9"
RELEASE_BASE="8832fce37cfce310d8ca49787204422b9a108991"
NEW={"burned_video_sha256":"7cee5261f35fb4ad5fe44d85dddec5f8739833fd78cc1585179c84816c9f3b6f","subtitle_sha256":"d0b71d63579d3b9de0164dda8b346da076b67ca387e5671bdc9a23389237e201","ass_sha256":"6bd45cd2b0e55530b72bf59c0b95e6163a4a8018394eda030e362f376333202f","cover_sha256":"9a3636c47b6bfe808dfdd24cd9486788b6101e9260e1cdf8d98a3886942b980f"}
def sha(p):
 h=hashlib.sha256();
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''): h.update(b)
 return h.hexdigest()
def reg(p):
 s=p.lstat();
 if stat.S_ISLNK(s.st_mode) or not stat.S_ISREG(s.st_mode): raise ValueError(f"C1_UNSAFE_INPUT:{p}")
def copytree(src,dst):
 for p in src.rglob('*'):
  rel=p.relative_to(src); q=dst/rel
  if p.is_symlink(): raise ValueError(f"C1_SYMLINK_INPUT:{p}")
  if p.is_dir(): q.mkdir(parents=True,exist_ok=True)
  elif p.is_file(): q.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,q)
def rewrite(v, old):
 if isinstance(v,dict): return {k:rewrite(x,old) for k,x in v.items()}
 if isinstance(v,list): return [rewrite(x,old) for x in v]
 if isinstance(v,str):
  for a,b in old.items(): v=v.replace(a,b)
 return v
def main(argv=None):
 ap=argparse.ArgumentParser(); ap.add_argument('--formal-package',type=Path,required=True); ap.add_argument('--predecessor-root',type=Path,required=True); ap.add_argument('--root-evidence',type=Path,required=True); ap.add_argument('--out',type=Path,required=True); ap.add_argument('--release-commit',required=True); a=ap.parse_args(argv)
 if a.out.exists() or a.out.is_symlink(): raise ValueError('C1_OUTPUT_ALREADY_EXISTS')
 if a.release_commit!=RELEASE_BASE or subprocess.run(['git','merge-base','--is-ancestor',RELEASE_BASE,'HEAD'],cwd=ROOT).returncode: raise ValueError('C1_STALE_RELEASE_COMMIT')
 for p in (a.formal_package/'package_audit.json',a.root_evidence): reg(p)
 if sha(a.formal_package/'package_audit.json')!=FORMAL_AUDIT or sha(a.root_evidence)!=ROOT_EVIDENCE: raise ValueError('C1_SEALED_INPUT_HASH_DRIFT')
 from src.autoslice.fastlane_c1_formal_adapter import validate_formal_package
 validate_formal_package(a.formal_package,repo_root=ROOT)
 with tempfile.TemporaryDirectory(dir=a.out.parent,prefix='.c1-canonical-') as tmp:
  stage=Path(tmp)/'package'; copytree(a.predecessor_root,stage)
  names={'video':f'{CID}.recut.burned-final-speaker.mp4','subtitle':f'{CID}.recut.srt','ass':f'{CID}.recut.final-sapphire72.ass','cover':f'{CID}.recut.burned-final-sapphire72.cover.png'}
  formal={'video':f'{CID}.recut.burned-final-speaker.mp4','subtitle':f'{CID}.recut.srt','ass':f'{CID}.recut.final-sapphire72.ass','cover':f'{CID}.recut.burned-final-sapphire72.cover.png'}
  for k,n in names.items(): reg(a.formal_package/formal[k]); shutil.copy2(a.formal_package/formal[k],stage/n)
  old={}
  record=next(stage.glob(f'{CID}*.record.json')); publish=next(stage.glob(f'{CID}*.publish.json')); manifest=stage/'review_manifest.json'
  before=json.loads(record.read_text());
  for k,n in NEW.items():
   for x in before.get('artifact_hashes',{}).values():
    if isinstance(x,str) and k.split('_sha256')[0] in x: old[x.removeprefix('sha256:')]=n
  old.update({f'{CID}.recut.burned-final-sapphire72.mp4':names['video'],f'{CID}.recut.burned-final-sapphire72.srt':names['subtitle']})
  after=rewrite(before,old); after['artifact_hashes'].update({'burned_video_sha256':'sha256:'+NEW['burned_video_sha256'],'subtitle_sha256':'sha256:'+NEW['subtitle_sha256'],'ass_sha256':'sha256:'+NEW['ass_sha256'],'cover_sha256':'sha256:'+NEW['cover_sha256']}); after['media_path']=names['video']; after['subtitle_path']=names['subtitle']; after['subtitle_ass_path']=names['ass']; after['publish_staging'].update({'video_path':names['video'],'subtitle_path':names['subtitle'],'subtitle_ass_path':names['ass'],'cover_path':names['cover'],'target_bvid':BVID,'title':TITLE,'upload_enabled':False})
  record.write_text(json.dumps(after,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
  pub=rewrite(json.loads(publish.read_text()),old); pub['artifact_hashes'].update({'burned_video_sha256':'sha256:'+NEW['burned_video_sha256'],'subtitle_sha256':'sha256:'+NEW['subtitle_sha256'],'ass_sha256':'sha256:'+NEW['ass_sha256'],'cover_sha256':'sha256:'+NEW['cover_sha256']}); pub.update({'title':TITLE,'video_path':names['video'],'cover_path':names['cover'],'upload_enabled':False}); publish.write_text(json.dumps(pub,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
  m=json.loads(manifest.read_text()); item=m['items'][0]; item.update({'video':names['video'],'mp4':names['video'],'subtitle_srt':names['subtitle'],'speaker_srt':names['subtitle'],'ass_path':names['ass'],'cover':names['cover'],'record':record.name,'publish_json':publish.name,'title':TITLE}); m.update({'schema_version':'lidousha-review-package.v1','run_mode':'RECOVERY_REVIEW','status':'finished_review_package_no_upload_pending_human_review','upload_allowed':False,'exact_candidate_ids':[CID]}); m['c1_delivery_bridge']={'schema_version':'fastlane-c1-canonical-delivery-bridge.v1','formal_package_audit_sha256':'sha256:'+FORMAL_AUDIT,'root_evidence_sha256':'sha256:'+ROOT_EVIDENCE,'release_base_commit':RELEASE_BASE,'same_bv_only':True,'bvid':BVID,'aid':117132650155234,'cid':41126267272,'named_fix_count':6}; manifest.write_text(json.dumps(m,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
  os.rename(stage,a.out)
 print(json.dumps({'package':str(a.out),'upload_allowed':False,'bvid':BVID},ensure_ascii=False)); return 0
if __name__=='__main__':
 try: raise SystemExit(main())
 except (ValueError,OSError,StopIteration) as e: print(str(e),file=sys.stderr); raise SystemExit(2)
