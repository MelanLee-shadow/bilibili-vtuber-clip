"""Ordinary producer observations consumed by the existing runtime report."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import datetime as dt
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .cpa_runtime import ATTEMPT_ENV, bind_observation

_active = ContextVar('autoslice_production_attempt', default=None)


def record_production_result(result: dict) -> None:
    attempt = _active.get()
    if attempt is not None:
        attempt['result'] = result


def attempt_path(runtime_root: Path, date: str) -> Path:
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',date) is None:
        raise ValueError('PRODUCTION_REPORT_DATE_INVALID')
    return runtime_root/'reports'/'production-attempts'/f'{date}.jsonl'


def read_attempts(runtime_root: Path, date: str) -> list[dict]:
    path=attempt_path(runtime_root,date)
    if not path.exists():
        return []
    latest={}
    for line in path.read_text(encoding='utf-8').splitlines():
        try: row=json.loads(line)
        except ValueError: continue  # A killed final append is not a result.
        if isinstance(row,dict) and row.get('schema_version')=='producer-runtime-attempt.v1':
            latest[(row['attempt_id'],row.get('candidate_id'),row.get('lane'),row.get('stage','production'))]=row
    return sorted(latest.values(),key=lambda row:row['observed_utc'])


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('PRODUCTION_REPORT_FILE_INVALID')
        payload=(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n').encode()
        if os.write(fd,payload)!=len(payload):
            raise OSError('production observation append incomplete')
        os.fsync(fd)
    finally: os.close(fd)


def observed_usage(path: Path, attempt_id: str) -> dict:
    rows=[]
    if path.is_file():
        for line in path.read_text().splitlines():
            try: row=json.loads(line)
            except ValueError: continue
            if isinstance(row,dict) and row.get('production_attempt_id')==attempt_id:
                rows.append(row)
    model_count = sum(row.get('event_type')=='model_request' for row in rows)
    http_count = sum(row.get('event_type')=='http_attempt' for row in rows)
    return {'cpa_http_attempts':None,
        'cpa_http_attempts_observed':None if model_count and not http_count else http_count,
        'model_requests_observed':model_count,
        'cache_hits_observed':sum(row.get('event_type')=='cache_hit' for row in rows),
        'audio_model_requests_observed':sum(row.get('event_type')=='audio_model_request' for row in rows),
        'audio_http_attempts_observed':sum(row.get('event_type')=='audio_http_attempt' for row in rows),
        'usage_coverage':'OBSERVED_INSTRUMENTED_TRANSPORTS; historical/SSH/custom transport totals UNKNOWN'}


def _outcome(result: object) -> tuple[str,str|None,dict]:
    if not isinstance(result,dict):
        return 'INCOMPLETE','NO_COMPLETED_DELIVERY_SUMMARY',{}
    stage = result.get('runtime_stage_result')
    if isinstance(stage,dict):
        status = stage.get('status')
        allowed = {'PACKAGE_BUILT','PACKAGE_AUDIT_PASS','JOINT_QC_PASS','SELECTOR_READY',
                   'INFRASTRUCTURE_WAIT','BLOCKED'}
        if status not in allowed:
            return 'INCOMPLETE','NO_COMPLETED_DELIVERY_SUMMARY',{}
        extra = {key:stage[key] for key in ('package_root','package_manifest_sha256',
            'next_retry_at','next_retry_at_epoch','transient_retry_count','retry_after_seconds',
            'recovery_checkpoint') if key in stage}
        return status,stage.get('reason_code'),{**extra,'public_package_accepted':False}
    evidence = result.get('prepared_summary') if result.get('status')=='delivery_prepared_no_target' else result
    if not isinstance(evidence,dict): evidence=result
    prepared=evidence.get('prepared_delivery')
    if isinstance(prepared,dict):
        roles=set(evidence.get('prepared_artifacts') or {})
        required={'video','subtitle','record','publish','chat_authority','clip_context','cover','cover_request','cover_response'}
        status='PREPARED_COMPLETE' if required.issubset(roles) and (evidence.get('cover_status') or result.get('cover_status'))=='AI_COVER_READY' else 'PARTIAL_PREPARED'
        reasons=result.get('reason_codes') or []
        return status, str(reasons[0]) if reasons else (None if status=='PREPARED_COMPLETE' else 'PREPARED_REQUIRED_ROLES_MISSING'),{
            'prepared_delivery':prepared,'prepared_role_count':len(roles),'public_package_accepted':False}
    status=str(result.get('status') or '')
    if status in {'ok','review_ready'} or (result.get('delivery') and result.get('cover_status')=='AI_COVER_READY'):
        return 'DELIVERED',None,{'public_package_accepted':False}
    return 'BLOCKED',str(result.get('reason_code') or result.get('rejection_reason') or result.get('failure_kind') or status or 'DELIVERY_INCOMPLETE'),{}


@contextmanager
def observed_production_attempt(*, runtime_root: Path, date: str, candidate_id: str,
        lane: str, trace: Path, inherit_process_attempt: bool = False, stage: str = 'production',
        execution_node: str | None = None):
    """Observe actual execution without modifying runner state or release gates."""
    if not runtime_root.is_dir():
        yield None
        return
    from .reporting import write_runtime_report
    # Runner candidates execute in sibling threads. Only an explicitly bound
    # child process or a same-candidate nested context may inherit an ID.
    parent=_active.get()
    attempt_id=(parent['attempt_id'] if parent and parent.get('candidate_id')==candidate_id
        else os.environ.get(ATTEMPT_ENV) if inherit_process_attempt else None) or uuid.uuid4().hex
    trace.parent.mkdir(parents=True,exist_ok=True)
    active={'result':None,'attempt_id':attempt_id,'candidate_id':candidate_id}
    token=_active.set(active);started=time.monotonic();error=None
    common={'schema_version':'producer-runtime-attempt.v1','attempt_id':attempt_id,
        'candidate_id':candidate_id,'date':date,'lane':lane,'pid':os.getpid(),'upload_claim':False}
    common['stage'] = stage
    if execution_node is not None: common['execution_node']=execution_node
    def persist(values):
        _append(attempt_path(runtime_root,date),{**common,**values,'observed_utc':dt.datetime.now(dt.timezone.utc).isoformat()})
        write_runtime_report(runtime_root,date)
    try:
        with bind_observation(trace,attempt_id):
            persist({'status':'RUNNING'})
            try: yield active
            except BaseException as exc:
                # Only the typed reason or leading constant is logged, never raw
                # exception text that could contain request bodies or credentials.
                reason=str(getattr(exc,'reason_code',None) or getattr(exc,'safe_reason',None) or '')
                match=re.match(r'[A-Z][A-Z0-9_]{3,}',str(exc))
                error=reason or (match.group(0) if match else type(exc).__name__)
                raise
            finally:
                status,reason,extra=_outcome(active['result'])
                if error: status,reason='BLOCKED',error
                metrics={'production_elapsed_seconds':round(time.monotonic()-started,3),
                    **observed_usage(trace,attempt_id)}
                if execution_node is not None:
                    remote=active['result'] if isinstance(active['result'],dict) else {}
                    for key in ('model_requests_observed','cpa_http_attempts_observed','cache_hits_observed',
                                'audio_model_requests_observed','audio_http_attempts_observed'):
                        metrics[key]=remote.get(key) if type(remote.get(key)) is int else None
                    metrics['usage_coverage']='VERIFIED_REMOTE_PRODUCER_RESULT; missing and historical transport totals UNKNOWN'
                    elapsed=remote.get('production_elapsed_seconds')
                    if type(elapsed) in (int,float):metrics['remote_producer_elapsed_seconds']=elapsed
                elif isinstance(active['result'],dict):
                    active['result'].update(metrics)
                persist({'status':status,'reason_code':reason,**extra,**metrics})
    finally:
        _active.reset(token)


def package_stage_binding(root: Path) -> dict:
    from .evidence_identity import file_sha256
    path = root/'review_manifest.json'
    try: digest = file_sha256(path)
    except OSError: digest = None
    return {'package_root':str(root.resolve()),
        'package_manifest_sha256':digest}


@contextmanager
def observed_package_stage(root: Path, stage: str, *, prepared_delivery: Path | None = None):
    """Observe ordinary package CLIs using their existing input identity."""
    metadata = {}
    for path in (root/'review_manifest.json', prepared_delivery):
        if path is None or not path.is_file():
            continue
        try: value = json.loads(path.read_text())
        except (OSError,ValueError): continue
        if isinstance(value,dict):
            metadata = value
            break
    if not (metadata.get('date') or metadata.get('recording_date')):
        # A first package build has no manifest yet, and prepared.json does
        # not carry a date. Read the same draft/context identity as the builder.
        drafts = [p for p in root.glob('*.publish.json')
                  if not p.name.endswith('.recut.publish.json')]
        if len(drafts) == 1:
            context = drafts[0].with_name(drafts[0].name[:-len('.publish.json')]+'.clip-context.json')
            try:
                draft, clip = json.loads(drafts[0].read_text()), json.loads(context.read_text())
            except (OSError,ValueError):
                pass
            else:
                if isinstance(draft,dict) and isinstance(clip,dict):
                    metadata = {**metadata,'candidate_id':draft.get('candidate_id'),
                                'recording_date':clip.get('recording_date')}
    items = metadata.get('items') or [{}]
    item = items[0] if isinstance(items,list) and items and isinstance(items[0],dict) else {}
    date = metadata.get('date') or metadata.get('recording_date')
    candidate = metadata.get('candidate_id') or item.get('candidate_id')
    runtime = Path(os.environ.get('AUTOSLICE_BASE') or '/opt/bilive/autoslice')
    if not runtime.is_dir() or not isinstance(date,str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}',date) is None or not candidate:
        yield None
        return
    lane = metadata.get('lane') or item.get('classification') or item.get('kind') or 'unknown'
    trace = runtime/'reports'/'production-attempt-traces'/(uuid.uuid4().hex+'.jsonl')
    with observed_production_attempt(runtime_root=runtime,date=date,candidate_id=str(candidate),
            lane=lane,trace=trace,stage=stage):
        yield
