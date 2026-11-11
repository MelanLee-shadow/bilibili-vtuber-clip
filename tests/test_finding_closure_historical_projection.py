from dataclasses import replace

import pytest

from src.autoslice import finding_closure as core, finding_closure_shell as shell
from src.autoslice.finding_closure_ports import (
    ArtifactSet, RebaseRequest, RetainedObject, ReviewRequest, ValidationRequest,
)
from src.autoslice.context_assisted_audio import anchor
from tests.test_current_audio_proof_projection import setup_finding
from tests.test_finding_closure import Oracle, SRT
from tests.test_wpb_finding_context import WORDS, check, srt


def request_for(finding, text, kind='decision'):
    return ReviewRequest(core.Work('w',kind,core.sha(text),task=finding.spec),text,finding)


@pytest.mark.parametrize('index', [2,14])
def test_historical_projection_is_exact_and_cannot_be_committed_as_current(index):
    owners, original, proof = setup_finding()
    words=list(WORDS)
    words[index]+='变'
    text=srt(words)
    projection=RebaseRequest(text,(original,))
    # This is the old ordinary producer's preserved output. Real sealed
    # journal examples are also exercised by the bounded production replay.
    historical=owners._rebase(projection,historical_audio_proofs=True)
    actual=owners.rebase(projection)
    assert historical[0].finding.read_view()['_audio_gain_termination']==proof
    assert '_audio_gain_termination' not in actual[0].finding.read_view()
    initial=core.initial_state(source_sha256='a'*64,srt_text=text,scan_channels=('recorded',))
    work=core.Work('w','targeted_scan',core.sha(text))
    expected=tuple(f.spec for f in historical)
    event=core.ScanCompleted('w',(original.spec,),(),(),expected)
    objects=tuple(d for finding in (*historical,original) for d in shell.finding_documents(finding))
    request=ValidationRequest(initial,work,event,ArtifactSet(objects))
    with pytest.raises(core.ClosureContractError,match='PROJECTION_DRIFT'):
        owners.revalidate(request)
    owners.revalidate(replace(request,historical=True))
    bad=replace(expected[0],local_text_sha256='0'*64)
    with pytest.raises(core.ClosureContractError,match='PROJECTION_DRIFT'):
        owners.revalidate(replace(request,historical=True,event=replace(event,rebased=(bad,))))
    missing=replace(request,historical=True,artifacts=ArtifactSet(tuple(
        d for d in objects if d.sha256!=historical[0].finding.sha256)))
    with pytest.raises(core.ClosureContractError,match='ARTIFACT_MISSING'):
        owners.revalidate(missing)
    assert original.finding.read_view()['_audio_gain_termination']==proof


def test_journal_replay_marks_history_but_new_effects_do_not(tmp_path):
    class ObservedOracle(Oracle):
        def revalidate(self, request):
            seen.append(request.historical)
            super().revalidate(request)
    seen=[]
    owner=ObservedOracle()
    initial=core.initial_state(source_sha256='a'*64,srt_text=SRT,scan_channels=('recorded',))
    ledger=shell.FileLedger(tmp_path/'ledger',initial)
    shell.ClosureRunner(initial=initial,ledger=ledger,ports=owner).run()
    assert seen and not any(seen)
    seen.clear()
    shell.replay_journal(initial,ledger,owner)
    assert seen and all(seen)


def with_bound_proof(finding):
    row=finding.finding.read_view()
    row['_audio_gain_termination']={'anchor':anchor(finding.check.read_view()),'preserved':'raw'}
    retained=RetainedObject.from_value(row)
    return replace(finding,finding=retained,spec=replace(finding.spec,finding_ref=retained.sha256))


@pytest.mark.parametrize('kind',['evidence','decision','task_stop','revalidate'])
def test_current_owner_excludes_stale_proof_without_editing_historical_evidence(monkeypatch,kind):
    owners,finding,_=setup_finding()
    finding=with_bound_proof(finding)
    words=list(WORDS)
    words[14]+='变'
    text=srt(words)
    request=request_for(finding,text,kind)
    before=finding.finding.sha256
    current=owners._current_review(request)
    assert current.finding.spec.key==finding.spec.key
    assert '_audio_gain_termination' not in current.finding.finding.read_view()
    assert current.finding.finding.read_view()['previous_finding_ref']==before
    assert finding.finding.sha256==before
    assert owners._current_review(current)==current

    class ReachedCurrentOwner(Exception):
        pass
    def consume(self, current, **kwargs):
        assert '_audio_gain_termination' not in current.finding.finding.read_view()
        if kind == 'revalidate':
            assert kwargs['provider_calls_allowed'] is False
        raise ReachedCurrentOwner
    monkeypatch.setattr(type(owners),'_adjudicate_captured',consume)
    if kind=='revalidate':
        original_result=type(owners)._result
        def result(self,text,row,**kwargs):
            assert '_audio_gain_termination' not in row.read_view()
            return original_result(self,text,row,**kwargs)
        monkeypatch.setattr(type(owners),'_result',result)
        # Main recovery reopens the ordinary cache-only owner. The native
        # branch has a different return contract; preserve main's behavior.
        with pytest.raises(ReachedCurrentOwner):
            owners.recover(request)
        return
    with pytest.raises(ReachedCurrentOwner):
        getattr(owners,{'decision':'decide','task_stop':'terminal','revalidate':'recover'}.get(kind,kind))(request)


@pytest.mark.parametrize('damage',['none','foreign','malformed'])
def test_current_or_foreign_or_malformed_proof_is_not_silently_discarded(damage):
    owners,finding,_=setup_finding()
    finding=with_bound_proof(finding)
    text=check()['whole_clip_current_srt']
    if damage!='none':
        words=list(WORDS)
        words[14]+='变'
        text=srt(words)
        row=finding.finding.read_view()
        bound=row['_audio_gain_termination']['anchor']
        if damage=='foreign':
            bound['source_media_sha256s']=['b'*64]
        else:
            bound['current_srt_sha256']='invalid'
        retained=RetainedObject.from_value(row)
        finding=replace(finding,finding=retained,spec=replace(finding.spec,finding_ref=retained.sha256))
    request=request_for(finding,text)
    assert owners._current_review(request)==request
