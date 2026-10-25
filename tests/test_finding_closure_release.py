"""The ordinary package call point and dual-mode release consumer."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from src.autoslice import finding_closure as c
from src.autoslice import finding_closure_shell as shell
from src.autoslice.finding_closure_ports import ConfirmationResult, RetainedObject
from src.autoslice.final_review_contract import FinalReviewContractError, validate_final_review_release
from tests.test_finding_closure import Oracle, SRT, prepared
from tests.test_exact_final_funnel import terminal_rig  # noqa: F401


class ValidOracle(Oracle):
    def first_scan(self, request):
        self.calls.append(('initial_scan', None))
        return self.scan(request, (prepared(request.srt_text, 'keep', 3),))

    def confirm(self, request):
        self.calls.append(('confirm', None))
        from tests.test_producer_current_text_auto_resume import _completed_current_review
        audit, _ = _completed_current_review(request.srt_text)
        validate_final_review_release(audit, expected_srt_sha256='sha256:' + c.sha(request.srt_text))
        return ConfirmationResult(c.sha(request.srt_text), RetainedObject.from_value(audit))


def package(tmp_path, monkeypatch):
    from src.autoslice import producer_package_finalization as producer
    oracle = ValidOracle()
    monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_MODE','per_finding')
    monkeypatch.setattr(shell,'cold_ports',lambda _configuration: oracle)
    reviewer = shell.PerFindingReviewer(lambda _request: shell.ClosureSession(
        'a'*64, oracle, RetainedObject.from_value({'legacy_prefix': None,'scan_channels':['recorded']}),('recorded',)))
    text = tmp_path/'current.srt'
    text.write_bytes(SRT.encode())
    authority = {}
    result = producer._run_exact_final_review_gate(cid='candidate', out_root=tmp_path,
        final_start=0, final_end=3000, recut=producer.CurrentTextReviewArtifacts(text),
        chat_authority_audit=authority, chat_authority_path=tmp_path/'authority.json',
        run_exact_final_review=reviewer)
    return result, oracle, text


def test_new_package_call_uses_ports_and_original_final_gate(tmp_path, monkeypatch):
    audit, oracle, text = package(tmp_path,monkeypatch)
    assert audit['final_review_mode']=='per_finding'
    assert audit['finding_closure']['task_count']==1
    calls=list(oracle.calls)
    assert validate_final_review_release(audit, expected_srt_sha256='sha256:'+c.sha(text.read_text()))==audit
    assert oracle.calls==calls  # The release consumer only replays/revalidates.


def test_text_recovery_keeps_confirmation_pending_and_resumes_without_reasking(tmp_path,monkeypatch):
    from tests.test_producer_current_text_auto_resume import _completed_current_review

    class TextThenSource(ValidOracle):
        boundary_ready=False

        def confirm(self, request):
            if self.boundary_ready:
                return super().confirm(request)
            self.calls.append(('text_ready',None))
            audit,_=_completed_current_review(request.srt_text)
            audit['boundary_semantic_review']={'schema_version':'talk-boundary-semantic-review.v1',
                'status':'BLOCK','reason_codes':['CURRENT_TEXT_REQUIRES_SOURCE_REVIEW']}
            audit['status']='TEXT_COMPLETE_BOUNDARY_PENDING'
            audit['release_gate']='BLOCK'
            audit['pre_boundary_text_recovery']={'status':'TEXT_COMPLETE_BOUNDARY_PENDING',
                'reviewed_srt_sha256':'sha256:'+c.sha(request.srt_text),'release_gate':'BLOCK'}
            shell.validate_text_ready(RetainedObject.from_value(audit),c.sha(request.srt_text))
            return ConfirmationResult(c.sha(request.srt_text),RetainedObject.from_value(audit),release_ready=False)

        def revalidate(self, request):
            super().revalidate(request)
            if isinstance(request.event,c.TextReady):
                shell.validate_text_ready(request.artifacts.get(request.event.receipt_ref),request.event.output_sha256)

    oracle=TextThenSource()
    monkeypatch.setattr(shell,'cold_ports',lambda _config:oracle)
    reviewer=shell.PerFindingReviewer(lambda _request:shell.ClosureSession('a'*64,oracle,
        RetainedObject.from_value({'legacy_prefix':None,'scan_channels':['recorded']}),('recorded',)))
    path=tmp_path/'current.srt';path.write_bytes(SRT.encode());authority={}
    call=shell.PackageCall(reviewer,'candidate',tmp_path,path,authority,tmp_path/'authority.json',0,3000,
        text_recovery_only=True)
    intermediate=shell.run_package_closure(call)
    assert intermediate['release_gate']=='BLOCK'
    assert intermediate['finding_closure']['schema_version']=='finding-closure-text-ready.v1'
    with pytest.raises(FinalReviewContractError,match='BOUNDARY_SEMANTIC_BLOCKED'):
        validate_final_review_release(intermediate,expected_srt_sha256='sha256:'+c.sha(SRT))
    root=tmp_path/'candidate.finding-closure'
    initial=shell.load_initial(root);ledger=shell.FileLedger(root,initial,create=False)
    waiting=shell.replay_journal(initial,ledger,oracle)
    assert waiting.text_ready_ref and not waiting.confirmation_ref
    assert [w.kind for w in waiting.pending]==['confirm']
    before=list(oracle.calls)
    assert shell.run_package_closure(call)==intermediate and oracle.calls==before
    oracle.boundary_ready=True
    released=shell.run_package_closure(replace(call,text_recovery_only=False))
    assert oracle.calls==before+[('confirm',None)]
    assert released['finding_closure']['schema_version']=='finding-closure-release.v1'
    validate_final_review_release(released,expected_srt_sha256='sha256:'+c.sha(SRT))


def test_text_ready_cannot_be_a_release_or_mask_a_nonboundary_failure(tmp_path):
    class NotRelease(Oracle):
        def confirm(self,request):
            result=super().confirm(request)
            return replace(result,release_ready=False)
    initial=c.initial_state(source_sha256='a'*64,srt_text=SRT,scan_channels=('recorded',))
    with pytest.raises(c.ClosureContractError,match='BOUNDARY_SEMANTIC_BLOCKED'):
        shell.ClosureRunner(initial=initial,ledger=shell.FileLedger(tmp_path/'closure',initial),
            ports=NotRelease()).run()
    from tests.test_producer_current_text_auto_resume import _completed_current_review
    audit,_=_completed_current_review(SRT)
    audit.update(status='TEXT_COMPLETE_BOUNDARY_PENDING',release_gate='BLOCK',
        pre_boundary_text_recovery={'status':'TEXT_COMPLETE_BOUNDARY_PENDING',
            'reviewed_srt_sha256':'sha256:'+c.sha(SRT),'release_gate':'BLOCK'})
    # A real missing discovery/mutation contract remains its own blocker.
    audit['discovery']['status']='INCOMPLETE'
    with pytest.raises(FinalReviewContractError,match='DISCOVERY_INCOMPLETE'):
        shell.validate_text_ready(RetainedObject.from_value(audit),c.sha(SRT))


def test_new_postburn_input_cannot_reuse_a_frozen_discovery_snapshot(monkeypatch):
    from src.autoslice.finding_closure_ports import DiscoveryInputs
    empty=RetainedObject.from_value({})
    owners=shell.ExistingFindingOwners(shell.OwnerInputs('a'*64,0,empty,empty,empty))
    discovery=DiscoveryInputs('a'*64,0,'','','',empty,empty,empty,(),(),
        owners.prepare_finding,lambda _prompt:'forbidden',json.loads)
    request=shell.ReviewerRequest(SRT,empty,lambda:empty,0,3000,frozenset())
    monkeypatch.setattr(shell,'registered_scan_channels',lambda:('recorded',))
    monkeypatch.setattr(shell,'discovery_factory',lambda _inputs:ValidOracle())
    original=shell.make_session(request,owners=owners,discovery=discovery,final_gate=lambda _:empty)
    latest=replace(discovery,verified_authority=RetainedObject.from_value({
        'postburn_subtitle_audio_review':{'status':'FLAGGED','release_gate':'BLOCK','request_sha256':'b'*64}}))
    resumed=shell.make_session(replace(request,retained_configuration=original.configuration),
        owners=owners,discovery=latest,final_gate=lambda _:empty)
    state=replace(c.initial_state(source_sha256='a'*64,srt_text=SRT,scan_channels=('recorded',)),
        confirmation_ref='a'*64)
    with pytest.raises(ValueError,match='DISCOVERY_COMPLETED_POSTBURN_BINDING_INVALID'):
        resumed.advance_inputs(state)
    assert original.configuration.value()['discovery']['verified_authority']=={}


def test_dual_mode_legacy_still_passes_and_new_one_byte_rejects_independently(tmp_path,monkeypatch):
    from tests.test_producer_current_text_auto_resume import _completed_current_review
    legacy,_=_completed_current_review(SRT)
    validate_final_review_release(legacy,expected_srt_sha256='sha256:'+c.sha(SRT))
    audit,_,text=package(tmp_path,monkeypatch)
    text.write_bytes(text.read_bytes()+b' ')
    with pytest.raises(FinalReviewContractError,match='FINDING_CLOSURE_RELEASE_BYTES_MISMATCH'):
        validate_final_review_release(audit,expected_srt_sha256='sha256:'+c.sha(text.read_text()))


def test_new_release_cannot_mutate_confirmed_audit_or_snapshot(tmp_path,monkeypatch):
    audit,_,_=package(tmp_path,monkeypatch)
    audit['extra_unconfirmed_claim']='not part of the original final confirmation'
    with pytest.raises(FinalReviewContractError,match='CONFIRMED_AUDIT_DRIFT'):
        validate_final_review_release(audit)
    audit.pop('extra_unconfirmed_claim')
    root=__import__('pathlib').Path(audit['finding_closure']['ledger_root'])
    state=root/'state.json'
    state.write_bytes(state.read_bytes().replace(b'KEEP',b'TRUE_BLOCK',1))
    with pytest.raises(FinalReviewContractError,match='SNAPSHOT_DRIFT'):
        validate_final_review_release(audit)


def test_new_carryover_writer_retires_without_touching_legacy_files(tmp_path,monkeypatch):
    from src.autoslice.final_review_carryover import persist_final_review_carryover,checkpoint_final_review_carryover
    audit,_,_=package(tmp_path,monkeypatch)
    path=tmp_path/'candidate.final-review-carryover.json'
    original=b'{"legacy":"kept exactly"}\n'
    path.write_bytes(original)
    assert persist_final_review_carryover(path,audit)==0
    assert checkpoint_final_review_carryover(path,audit)==0
    assert path.read_bytes()==original


def test_enabled_mode_never_falls_back_to_an_untyped_legacy_callback(tmp_path,monkeypatch):
    from src.autoslice import producer_package_finalization as producer
    monkeypatch.setenv('AUTOSLICE_FINAL_REVIEW_MODE','per_finding')
    path=tmp_path/'current.srt';path.write_text(SRT)
    calls=[]
    with pytest.raises(SystemExit,match='TYPED_REVIEWER_REQUIRED'):
        producer._run_exact_final_review_gate(cid='candidate',out_root=tmp_path,final_start=0,final_end=3000,
            recut=producer.CurrentTextReviewArtifacts(path),chat_authority_audit={},
            chat_authority_path=tmp_path/'authority.json',run_exact_final_review=lambda *_args:calls.append(1))
    assert not calls and path.read_text()==SRT


def test_real_f2_original_answer_is_not_promoted_without_owner_typed_materials(monkeypatch,terminal_rig):
    from tests.test_exact_final_funnel import run_terminal,unresolved,answer,SRT as real_srt
    monkeypatch.delenv('AUTOSLICE_BASE',raising=False)
    result=run_terminal([unresolved()],lambda _:json.dumps(answer(),ensure_ascii=False),terminal_rig=terminal_rig)
    row=RetainedObject.from_value(result['unresolved_findings_disclosed'][0])
    inputs=shell.OwnerInputs('a'*64,terminal_rig.offset_ms,
        RetainedObject.from_value(terminal_rig.context),RetainedObject.from_value({'meaning':'test'}),
        RetainedObject.from_value({'transport':'recorded','models':['top'],'effort':'medium'}))
    owners=shell.ExistingFindingOwners(inputs)
    outcome=owners._result(real_srt,row,terminal=True)
    assert outcome.state==c.State.CONTRACT_ERROR
    assert outcome.receipt.value()['finding']==row.value()
    assert outcome.receipt.value()['owner_error']=='FINDING_CLOSURE_OWNER_TYPED_MATERIALS_REQUIRED'


def test_real_context_reentry_freezes_original_p_after_shortening_current_text(monkeypatch,terminal_rig):
    from tests.test_exact_final_funnel import unresolved,SRT as original_text
    from src.autoslice.finding_closure_ports import RebaseRequest
    monkeypatch.delenv('AUTOSLICE_BASE',raising=False)
    owners=shell.ExistingFindingOwners(shell.OwnerInputs('a'*64,terminal_rig.offset_ms,
        RetainedObject.from_value(terminal_rig.context),RetainedObject.from_value({'meaning':'test'}),
        RetainedObject.from_value({'transport':'recorded','models':['top'],'effort':'medium'})))
    original=owners.prepare_finding(original_text,RetainedObject.from_value(unresolved()))
    saved=original.finding.json_text
    changed=original_text.replace('我喜欢她啊','我喜欢她')
    current,=owners.rebase(RebaseRequest(changed,(original,)))
    assert current.spec.key.physical==original.spec.key.physical
    assert current.spec.key.materials!=original.spec.key.materials
    assert current.origin_check==original.origin_check
    assert current.check.value()['whole_clip_current_srt']==changed
    assert original.finding.json_text==saved
    assert current.finding.value()['previous_finding_ref']==original.finding.sha256


def test_changed_timing_grid_requires_owner_before_context_projection(monkeypatch,terminal_rig):
    from tests.test_exact_final_funnel import unresolved,SRT as original_text
    from src.autoslice.finding_closure_ports import RebaseRequest
    monkeypatch.delenv('AUTOSLICE_BASE',raising=False)
    owners=shell.ExistingFindingOwners(shell.OwnerInputs('a'*64,terminal_rig.offset_ms,
        RetainedObject.from_value(terminal_rig.context),RetainedObject.from_value({'meaning':'test'}),
        RetainedObject.from_value({'transport':'recorded','models':['top'],'effort':'medium'})))
    original=owners.prepare_finding(original_text,RetainedObject.from_value(unresolved()))
    with pytest.raises(c.ClosureContractError,match='PROJECTION_WITHOUT_LEGAL_PATCH'):
        owners.rebase(RebaseRequest(original_text.replace('00:00:01,000','00:00:01,001'),(original,)))
