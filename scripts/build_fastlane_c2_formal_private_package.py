#!/usr/bin/env python3
"""Create a C2-only formal review package from the private candidate bytes."""
from __future__ import annotations
import argparse, json, shutil, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from src.autoslice.fastlane_c2_formal_adapter import CID, NAMES, SCHEMA, TITLE, sha256

def main() -> int:
 p=argparse.ArgumentParser(); p.add_argument('--candidate',type=Path,required=True); p.add_argument('--out',type=Path,required=True); a=p.parse_args(); src=a.candidate.resolve(); out=a.out.resolve()
 if out.exists(): raise SystemExit('refusing to overwrite formal package')
 out.mkdir(mode=0o700,parents=True)
 for name in NAMES.values():
  s=src/name
  if not s.is_file() or s.is_symlink(): raise SystemExit(f'missing candidate artifact: {name}')
  d=out/name; d.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(s,d)
 artifacts={key:{'path':name,'sha256':sha256(out/name),'bytes':(out/name).stat().st_size} for key,name in NAMES.items()}
 record={'schema_version':'fastlane-c2-formal-private-record.v1','candidate_id':CID,'status':'FORMAL_PRIVATE_REVIEW','upload_allowed':False,'title':TITLE,'artifact_hashes':{key:'sha256:'+entry['sha256'] for key,entry in artifacts.items()},'named_scope':{'subtitle_mutation_cues':[5],'chat_context_freeze_cue':21,'title_identity':'小李 is 李豆沙'}}
 publish={'schema_version':'fastlane-c2-formal-private-publish.v1','candidate_id':CID,'title':TITLE,'cover_lines':['拔智齿求亲亲','小豆：只会嘲笑'],'upload_allowed':False,'video_sha256':'sha256:'+artifacts['video']['sha256'],'cover_sha256':'sha256:'+artifacts['cover']['sha256']}
 (out/'c2.formal.record.v1.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); (out/'c2.formal.publish.v1.json').write_text(json.dumps(publish,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 manifest={'schema_version':SCHEMA,'candidate_id':CID,'title':TITLE,'upload_allowed':False,'scope':'C2_NAMED_FASTLANE_REPAIR_ONLY','artifacts':artifacts,'record':{'path':'c2.formal.record.v1.json','sha256':sha256(out/'c2.formal.record.v1.json')},'publish':{'path':'c2.formal.publish.v1.json','sha256':sha256(out/'c2.formal.publish.v1.json')}}
 (out/'review_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); return 0
if __name__=='__main__': raise SystemExit(main())
