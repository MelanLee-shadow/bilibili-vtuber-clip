#!/usr/bin/env python3
"""Prepare a bounded batch of selected Talk candidates through the normal backend.

This entry never runs a tick, commits deliveries, or enables upload. The input
retains the original runner items and selection evidence; output is the actual
prepare-only producer result, including failures and deferred candidates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-json',type=Path,required=True)
    parser.add_argument('--out-json',type=Path,required=True)
    args=parser.parse_args(argv)
    batch_bytes=args.batch_json.read_bytes()
    batch=json.loads(batch_bytes)
    if (not isinstance(batch,dict) or batch.get('schema_version')!='autoslice-prepare-talk-batch.v1'
            or re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(batch.get('date',''))) is None
            or not isinstance(batch.get('items'),list) or not batch['items']):
        parser.error('batch must contain its date and nonempty original selected Talk items')
    seen=set()
    for row in batch['items']:
        if not isinstance(row,dict) or not isinstance(row.get('cid'),str) or row['cid'] in seen:
            parser.error('candidate ids must be present and unique')
        seen.add(row['cid'])
        source=Path(str(row.get('segment_path','')))
        if not source.is_absolute() or source.is_symlink() or not source.is_file():
            parser.error('candidate segment_path must be an existing regular absolute source')
        with source.open('rb') as stream:
            source_sha=hashlib.file_digest(stream,'sha256').hexdigest()
        if row.get('source_media_sha256') not in (source_sha,'sha256:'+source_sha):
            parser.error('candidate source_media_sha256 differs from actual source bytes')
    # Explicit private preparation is independent of the paused autonomous
    # queue. Keep both public entrypoints disabled for all subprocesses.
    os.environ['AUTOSLICE_ENABLE_UPLOAD']='0'
    os.environ['AUTOSLICE_ENABLE_AUTHORIZED_UPLOAD_QUEUE']='0'
    os.environ.pop('AUTO_UPLOAD',None)
    os.environ['GEMINI_PAID_BACKUP_DAILY_CAP']='0'
    os.environ.pop('GEMINI_PAID_BACKUP_DEV_EXCEPTION',None)
    if (ROOT/'DEPLOYED_COMMIT').is_file():
        from src.autoslice.runtime_code_lease import pin_version
        pin_version(ROOT)
    from scripts import session_autoslice as runner
    from src.autoslice.channel_profile import binding_for_profile, require_profile_binding
    require_profile_binding(batch.get('profile_binding'),binding_for_profile(runner.CHANNEL_PROFILE))
    from src.autoslice.runner_batch_dispatch import produce_batch
    from src.autoslice.package_import import atomic_write_bytes
    runtime={**vars(runner),'producer_stop_requested':lambda:False}
    os.environ.update(runner.profile_environment(runner.CHANNEL_PROFILE))
    os.environ.update(runner.load_env_file(runner._cpa_env_path(),('CPA_API_KEY','CPA_BASE_URL')))
    os.environ.update(runner.load_gemini_credentials())
    rows=produce_batch(batch['date'],batch['items'],runner.produce_talk,runtime,prepare_only=True)
    result={'schema_version':'autoslice-prepare-talk-batch-result.v1','date':batch['date'],
        'batch_path':str(args.batch_json.resolve()),
        'batch_sha256':hashlib.sha256(batch_bytes).hexdigest(),
        'profile_binding':batch['profile_binding'],'upload_enabled':False,
        'requested_candidates':[row['cid'] for row in batch['items']], 'results':rows}
    atomic_write_bytes(args.out_json,(json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True,default=str)+'\n').encode())
    print(json.dumps({'out_json':str(args.out_json),'requested':len(batch['items']),'completed':len(rows),
        'statuses':[row.get('status') for row in rows],'upload_enabled':False},ensure_ascii=False))
    return 0 if len(rows)==len(batch['items']) and all(row.get('status')=='delivery_prepared_no_target' for row in rows) else 1


if __name__=='__main__':
    raise SystemExit(main())
