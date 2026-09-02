#!/usr/bin/env python3
"""Emit the only generic-text override permitted for the sealed C2 repair."""
import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; CID='auto_203011_328_389'
AUTH=ROOT/'assets/lidousha/fastlane_c2_private/auto_203011_328_389.correction-authority.v1.json'
def sha(b): return hashlib.sha256(b).hexdigest()
def main():
 a=json.loads(AUTH.read_text()); c=a['exact_subtitle_changes']
 if a.get('candidate_id')!=CID or a.get('upload_allowed') is not False or len(c)!=1 or c[0].get('cue')!=5: raise SystemExit('sealed-authority-invalid')
 if any('ruling' in str(v).lower() for v in a.values()): raise SystemExit('rejected-ruling-doc-binding')
 x=c[0]; witness={'candidate_id':CID,'cue':5,'start_ms':x['start_ms'],'end_ms':x['end_ms'],'before':x['before'],'after':x['after'],'raw_line_sha256':a['operator_source']['raw_line_sha256'],'content_sha256':a['operator_source']['content_sha256']}
 out={'schema_version':4,'candidate_id':CID,'upload':False,'source_cue_witness_sha256':sha(json.dumps(witness,ensure_ascii=False,sort_keys=True).encode()),'decision_output_witness_sha256':sha(json.dumps({'candidate_id':CID,'after':x['after']},ensure_ascii=False,sort_keys=True).encode()),'overrides':[{'source_cue':5,'action':'replace','expect':{'start':'00:00:08,720','end':'00:00:11,240','text':x['before']},'text':x['after'],'authority':'sealed C2 Claude raw-line/content hash authority; no ruling document binding','reason':'exact one-cue fastlane repair'}]}
 print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
