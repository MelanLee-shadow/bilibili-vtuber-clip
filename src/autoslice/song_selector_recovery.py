"""Durable use of the existing song infrastructure backoff in native selectors."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from src.autoslice.infra_retry_policy import SONG_INFRA_TRANSIENT_REASON_CODES, _bounded_retry_delay_seconds
from src.autoslice.package_import import atomic_write_bytes


def infrastructure_reasons(record: dict) -> list[str]:
    reasons = []
    stack = [record]
    while stack:
        value = stack.pop()
        if isinstance(value,dict):
            rows = value.get('reason_codes')
            if isinstance(rows,list):
                reasons.extend(code for code in rows if isinstance(code,str) and code in SONG_INFRA_TRANSIENT_REASON_CODES)
            stack.extend(v for v in value.values() if isinstance(v,(dict,list)))
        elif isinstance(value,list):
            stack.extend(v for v in value if isinstance(v,(dict,list)))
    return list(dict.fromkeys(reasons))


class SelectorRecovery:
    def __init__(self, path: Path, identity: dict):
        self.path, self.identity = path, identity
        self.state = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(self.state,dict) or (self.state and (self.state.get('schema_version') != 'song-selector-recovery.v1'
                or self.state.get('identity') != identity)):
            raise ValueError('SONG_SELECTOR_RECOVERY_INPUT_DRIFT')

    def persist(self):
        self.state.update(schema_version='song-selector-recovery.v1',identity=self.identity)
        atomic_write_bytes(self.path,(json.dumps(self.state,ensure_ascii=False,indent=2,sort_keys=True)+'\n').encode())

    def adopt_completed_failure(self, summary_path: Path, source_video: Path):
        if self.state or not summary_path.is_file():
            return
        raw = summary_path.read_bytes()
        summary = json.loads(raw)
        if not isinstance(summary,dict):
            return
        if Path(summary.get('source_video','')).resolve() != source_video.resolve():
            return
        if summary.get('source_sha256') not in (None,self.identity['source_sha256']):
            return
        for row in summary.get('records',[]):
            if isinstance(row,dict) and row.get('candidate_id') == self.identity['candidate_id'] and infrastructure_reasons(row):
                # Historical selectors omitted completion timestamps. Recover
                # from the actual saved result at the CURRENT observation time;
                # never invent a past dispatch time or immediate retry grant.
                digest = hashlib.sha256(raw).hexdigest()
                frozen = summary_path.with_name('summary.previous.'+digest+'.json')
                if not frozen.exists():
                    with frozen.open('xb') as stream:stream.write(raw)
                if frozen.is_symlink() or frozen.read_bytes() != raw:
                    raise ValueError('SONG_SELECTOR_RECOVERY_INPUT_DRIFT')
                self.state['recovered_failure'] = {'summary_path':str(frozen),
                    'summary_sha256':digest,
                    'observed_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
                    'basis':'COMPLETED_SELECTOR_FAILURE_OBSERVED_AT_RECOVERY'}
                self.finish(row)
                break

    def resume_record(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        if self.state.get('status') == 'RUNNING':
            return {'candidate_id':self.identity['candidate_id'],'decision_action':'BLOCK',
                'reason_codes':['CALL_OUTCOME_UNKNOWN'],'recovery_checkpoint':str(self.path)}
        epoch = self.state.get('next_retry_at_epoch')
        if self.state.get('status') == 'INFRASTRUCTURE_WAIT' and type(epoch) is int and epoch > now:
            return self.result_record()
        return None

    def start(self):
        self.state.update(status='RUNNING',started_utc=dt.datetime.now(dt.timezone.utc).isoformat(),pid=os.getpid())
        self.persist()

    def finish(self, record: dict, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        reasons = infrastructure_reasons(record)
        count = int(self.state.get('transient_retry_count') or 0)
        self.state.update(last_record=deepcopy(record),ended_utc=dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat())
        if reasons:
            delay = _bounded_retry_delay_seconds(count)
            epoch = int(now) + delay
            self.state.update(status='INFRASTRUCTURE_WAIT',transient_failure_code=reasons[0],
                transient_retry_count=count+1,retry_after_seconds=delay,next_retry_at_epoch=epoch,
                next_retry_at=dt.datetime.fromtimestamp(epoch,dt.timezone.utc).isoformat())
        else:
            self.state.update(status='RESULT_RECORDED')
            for key in ('transient_failure_code','retry_after_seconds','next_retry_at_epoch','next_retry_at'):
                self.state.pop(key,None)
        self.persist()
        return self.result_record()

    def result_record(self):
        record = deepcopy(self.state['last_record'])
        record['source_sha256'] = self.identity['source_sha256']
        record.update({key:self.state[key] for key in ('transient_failure_code','transient_retry_count',
            'retry_after_seconds','next_retry_at_epoch','next_retry_at') if key in self.state})
        record['recovery_checkpoint'] = str(self.path)
        return record


@contextmanager
def selector_recovery(source_video: Path, candidate_id: str, start_ms: int, end_ms: int):
    with source_video.open('rb') as stream:
        source_sha = hashlib.file_digest(stream,'sha256').hexdigest()
    identity = {'source_sha256':source_sha,'candidate_id':candidate_id,
        'anchor_start_ms':start_ms,'anchor_end_ms':end_ms}
    key = hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    directory = source_video.parent/'.selector-recovery'
    directory.mkdir(exist_ok=True)
    lock = directory/(key+'.lock')
    fd = os.open(lock,os.O_CREAT|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('SONG_SELECTOR_RECOVERY_INPUT_DRIFT')
        # Serialize only this exact source/candidate clock, never other songs.
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield SelectorRecovery(directory/(key+'.json'),identity)
    finally:
        os.close(fd)
