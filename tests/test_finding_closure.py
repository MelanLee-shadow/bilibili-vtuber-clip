"""Per-question workflow, durable resume and independent rejection controls."""
from __future__ import annotations

import ast
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from src.autoslice import finding_closure as c
from src.autoslice.finding_closure_ports import (ConfirmationResult, PreparedFinding,
    RetainedObject, ReviewResult, ScanResult, PatchResult, PrefetchResult)
from src.autoslice.finding_closure_shell import ClosureRunner, FileLedger, replay_journal

SRT = "1\n00:00:00,000 --> 00:00:01,000\n旧字\n\n2\n00:00:01,000 --> 00:00:02,000\n第二句\n\n3\n00:00:02,000 --> 00:00:03,000\n保留\n"


def prepared(text, physical, cue):
    local = c.local_text_sha(text, (cue,))
    key = c.FindingKey(c.sha(physical), c.sha(physical + local))
    row = RetainedObject.from_value({'physical': physical, 'cue': cue})
    check = RetainedObject.from_value({'physical': physical, 'whole_clip_current_srt': text})
    materials = RetainedObject.from_value({'physical_question_sha256': key.physical,
        'materials_sha256': key.materials, 'local': local})
    spec = c.TaskSpec(key, (cue,), local, row.sha256, check.sha256, materials.sha256)
    return PreparedFinding(spec, row, check, materials)


class Oracle:
    """A substitute port with cold authority independent of saved task flags."""
    def __init__(self):
        self.calls = []
        self.validations = 0

    def first_scan(self, request):
        self.calls.append(('initial_scan', None))
        return self.scan(request, (prepared(request.srt_text, 'edit', 1),
                                    prepared(request.srt_text, 'keep', 3)))

    def prefetch(self, request):
        self.calls.append(('prefetch',None))
        return PrefetchResult((RetainedObject.from_value({'source':request.source_sha256,
            'work_id':request.work.work_id,'provider':'recorded','authority':'EVIDENCE_ONLY'}),))

    def targeted_scan(self, request):
        self.calls.append(('targeted_scan', None))
        return self.scan(request, ())

    def final_scan(self, request):
        self.calls.append(('final_scan', None))
        return self.scan(request, ())

    def scan(self, request, findings):
        lanes = tuple(RetainedObject.from_value({'lane': i, 'work': asdict(request.work),
            'findings': [asdict(f.spec) for f in findings]}) for i in range(request.work.scan_n))
        coverage = tuple(v for v in c.cue_versions(request.srt_text) if v.cue in request.work.scope_cues)
        return ScanResult(findings, coverage, lanes)

    def result(self, request):
        state = c.State.EDIT_REVIEW if (request.finding.finding.value()['physical'] == 'edit'
            and '旧字' in request.srt_text) else c.State.KEEP
        receipt = RetainedObject.from_value({'key': asdict(request.finding.spec.key),
            'state': state, 'authority': 'recorded-oracle'})
        return ReviewResult(request.finding, state, receipt)

    def evidence(self, request):
        self.calls.append(('evidence', request.finding.spec.key))
        return self.result(request)

    def decide(self, request):
        self.calls.append(('decision', request.finding.spec.key))
        return self.result(request)

    def terminal(self, request):
        self.calls.append(('task_stop', request.finding.spec.key))
        return self.result(request)

    def recover(self, request):
        return self.result(request)

    def validate_scan(self, request):
        self.revalidate(request)

    def rebase(self, request):
        return tuple(prepared(request.srt_text, f.finding.value()['physical'],
                              f.finding.value()['cue']) for f in request.findings)

    def patch(self, request):
        output = request.srt_text.replace('旧字', '新字', 1)
        repair = RetainedObject.from_value({'before': '旧字', 'after': '新字',
                                           'ruling_ref': request.ruling.sha256})
        patch = c.Patch(request.work.work_id, request.finding.spec.key, request.ruling.sha256,
            c.sha(request.srt_text), c.sha(output), request.srt_text.index('旧字'),
            '旧字', '新字', repair.sha256)
        from src.autoslice.finding_closure_ports import RebaseRequest
        return PatchResult(patch, self.rebase(RebaseRequest(output, request.all_findings)), repair)

    def confirm(self, request):
        self.calls.append(('confirm', None))
        assert c.stop_reason(request.state) == 'READY'
        audit = RetainedObject.from_value({'reviewed_srt_sha256': 'sha256:' + c.sha(request.srt_text),
            'release_gate': 'PASS', 'status': 'CLEAN'})
        return ConfirmationResult(c.sha(request.srt_text), audit)

    def revalidate(self, request):
        self.validations += 1
        event = request.event
        if isinstance(event,c.PrefetchCompleted):
            for ref in event.receipt_refs:
                record=request.artifacts.get(ref).value()
                assert record['source']==request.state.source_sha256
                assert record['work_id']==request.work.work_id
        elif isinstance(event, c.ReviewCompleted):
            key = event.spec.key
            material = request.artifacts.get(event.spec.materials_ref).value()
            assert key.materials == material['materials_sha256']
            expected = RetainedObject.from_value({'key': asdict(key), 'state': event.state,
                                                 'authority': 'recorded-oracle'})
            if request.artifacts.get(event.receipt_ref) != expected:
                raise ValueError('ORACLE_ORIGINAL_RULING_REJECTED')
            # A self-consistent edited event cannot invent a different outcome.
            row = request.artifacts.get(event.spec.finding_ref).value()
            text = c.text_at(request.state, request.work.text_sha256)
            expected_state = c.State.EDIT_REVIEW if row['physical'] == 'edit' and '旧字' in text else c.State.KEEP
            if event.state != expected_state:
                raise ValueError('ORACLE_ORIGINAL_RULING_REJECTED')
        elif isinstance(event, c.PatchCompleted):
            repair = request.artifacts.get(event.patch.repair_ref).value()
            if (event.patch.before, event.patch.after) != ('旧字', '新字') or repair['after'] != '新字':
                raise ValueError('ORACLE_ORIGINAL_PATCH_REJECTED')
        elif isinstance(event, c.ScanCompleted):
            for i, ref in enumerate(event.lane_refs):
                lane = request.artifacts.get(ref).value()
                if lane['lane'] != i or lane['work'] != json.loads(json.dumps(asdict(request.work))):
                    raise ValueError('ORACLE_ORIGINAL_SCAN_REJECTED')


def run(tmp_path, ports=None, on_projection=None):
    initial = c.initial_state(source_sha256='a'*64, srt_text=SRT,scan_channels=('recorded',))
    ledger = FileLedger(tmp_path/'closure', initial)
    oracle = ports or Oracle()
    state = ClosureRunner(initial=initial, ledger=ledger, ports=oracle, output=tmp_path/'out.srt',
                          on_projection=on_projection).run()
    return initial, ledger, state, oracle


def test_pure_core_import_guard_and_all_records_frozen():
    source = Path(c.__file__).read_text()
    tree = ast.parse(source)
    allowed = {'__future__','dataclasses','hashlib','json','typing','finding_lifecycle','jingting_chunker'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name in allowed for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module in allowed
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {'open','exec','eval','input','print'}
    for value in vars(c).values():
        if isinstance(value, type) and value.__module__ == c.__name__ and hasattr(value, '__dataclass_params__'):
            assert value.__dataclass_params__.frozen


def test_scan_strength_is_injected_channel_set_and_final_scan_cannot_reopen_p(tmp_path):
    class Reopens(Oracle):
        def final_scan(self,request):
            assert request.work.scan_channels==('recorded',)
            assert request.work.scan_n==1
            return self.scan(request,(prepared(request.srt_text,'keep',3),))
    with pytest.raises(c.ClosureContractError,match='FINAL_SCAN_REOPENS_CLOSED_P'):
        run(tmp_path,ports=Reopens())


def test_independent_task_edit_patch_coverage_final_scan_and_byte_identical_replay(tmp_path):
    initial, ledger, state, oracle = run(tmp_path)
    assert state.current_srt == SRT.replace('旧字','新字')
    assert all(t.state == c.State.KEEP for t in state.tasks)
    assert len(state.patches) == 1 and c.replay_patches(state) == state.current_srt
    before_calls = list(oracle.calls)
    a = replay_journal(initial, ledger, oracle)
    b = replay_journal(initial, ledger, oracle)
    assert c.state_bytes(a) == c.state_bytes(b) == c.state_bytes(state)
    assert oracle.calls == before_calls
    keep = c.sha('keep')
    assert sum(kind == 'evidence' and key.physical == keep for kind,key in oracle.calls if key) == 1
    assert sum(kind == 'final_scan' for kind,_ in oracle.calls) == 1
    assert not c.uncovered(state)


def test_journal_commit_survives_projection_crash_with_zero_reask(tmp_path):
    class Crash(Exception): pass
    def crash(state):
        if state.confirmation_ref:
            raise Crash()
    oracle = Oracle()
    with pytest.raises(Crash):
        run(tmp_path, oracle, crash)
    calls = list(oracle.calls)
    initial, ledger, state, _ = run(tmp_path, oracle)
    assert state.confirmation_ref and oracle.calls == calls
    assert c.state_bytes(replay_journal(initial, ledger, oracle)) == c.state_bytes(state)


def test_forged_snapshot_is_rejected_not_used_as_release_authority(tmp_path):
    _, ledger, _, _ = run(tmp_path)
    snapshot = ledger.root/'state.json'
    snapshot.write_text(snapshot.read_text().replace('KEEP','TRUE_BLOCK',1))
    with pytest.raises(ValueError, match='SNAPSHOT_DRIFT'):
        run(tmp_path)


def test_confirmed_output_one_byte_change_has_independent_rejection(tmp_path):
    run(tmp_path)
    output = tmp_path/'out.srt'
    output.write_bytes(output.read_bytes()+b' ')
    with pytest.raises(ValueError, match='RELEASE_BYTES_MISMATCH'):
        run(tmp_path)


def test_hash_rewritten_patch_still_rejected_by_original_consumer(tmp_path):
    from src.autoslice.finding_closure_ports import ArtifactSet, ValidationRequest
    initial, ledger, _, oracle = run(tmp_path)
    state = initial
    for envelope in ledger.events():
        from src.autoslice.finding_closure_shell import decode_event, event_artifacts
        event = decode_event(RetainedObject.from_value(envelope.value()['event']))
        if isinstance(event, c.PatchCompleted):
            work = next(w for w in state.pending if w.work_id == event.work_id)
            forged_patch = replace(event.patch, after='伪造', after_sha256=c.sha(SRT.replace('旧字','伪造')))
            forged = replace(event, patch=forged_patch)
            with pytest.raises(ValueError, match='ORIGINAL_PATCH_REJECTED'):
                oracle.revalidate(ValidationRequest(state, work, forged,
                                                   event_artifacts(ledger,event,work)))
            break
        state,_ = c.step(state,event)
    else:
        pytest.fail('no actual patch event')


def test_stop_has_no_unchanged_round_requirement_and_d_is_not_release(tmp_path):
    _, _, state, _ = run(tmp_path)
    assert 'UNCHANGED' not in Path(c.__file__).read_text()
    blocked = replace(state, confirmation_ref='', tasks=(replace(state.tasks[0],state=c.State.TRUE_BLOCK),))
    assert c.stop_reason(blocked) == 'FINAL_REVIEW_TRUE_CONTENT_BLOCK'


def test_rediscovery_nomination_never_overwrites_an_inflight_consumed_material_version():
    initial=c.initial_state(source_sha256='a'*64,srt_text=SRT,scan_channels=('recorded',))
    state,_=c.step(initial,c.Start())
    prefetch=next(w for w in state.pending if w.kind=='prefetch')
    state,_=c.step(state,c.PrefetchCompleted(prefetch.work_id,('b'*64,)))
    scan=next(w for w in state.pending if w.kind=='initial_scan')
    original=prepared(SRT,'edit',1).spec
    state,_=c.step(state,c.ScanCompleted(scan.work_id,(original,),c.cue_versions(SRT),('c'*64,)))
    evidence=next(w for w in state.pending if w.kind=='evidence')
    consumed=replace(original,key=c.FindingKey(original.key.physical,'d'*64),materials_ref='e'*64)
    state,_=c.step(state,c.ReviewCompleted(evidence.work_id,consumed,c.State.AWAITING_DECISION,'f'*64))
    scan=c.Work('1'*64,'targeted_scan',c.sha(SRT),scope_cues=(1,2,3),scan_channels=('recorded',))
    state=replace(state,pending=(*state.pending,scan))
    result,_=c.step(state,c.ScanCompleted(scan.work_id,(original,),c.cue_versions(SRT),('2'*64,)))
    task=result.tasks[0]
    assert task.state==c.State.AWAITING_DECISION and task.spec.key==consumed.key
    assert task.nomination.key==original.key


def test_crlf_confirmed_bytes_resume_without_newline_normalization(tmp_path):
    original=SRT.replace('\n','\r\n')
    initial=c.initial_state(source_sha256='a'*64,srt_text=original,scan_channels=('recorded',))
    ledger=FileLedger(tmp_path/'closure',initial)
    oracle=Oracle();output=tmp_path/'out.srt'
    runner=lambda:ClosureRunner(initial=initial,ledger=ledger,ports=oracle,output=output).run()
    state=runner();before=list(oracle.calls)
    assert output.read_bytes()==state.current_srt.encode()
    assert b'\r\n' in output.read_bytes()
    assert runner()==state and oracle.calls==before
