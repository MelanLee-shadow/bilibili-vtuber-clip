"""Read-only ordinary owners must not unfold repeated immutable histories."""
from copy import deepcopy
import json

import pytest

from src.autoslice import finding_closure as core, finding_closure_shell as shell
from src.autoslice.finding_closure_ports import RetainedObject
from src.autoslice import exact_final_terminal_funnel as funnel
from tests.test_terminal_funnel_local_cost import SRT, CONTEXT, FINDING, request


def containers(value):
    pending=[value];seen=set()
    while pending:
        item=pending.pop()
        if isinstance(item,(dict,list)) and id(item) not in seen:
            seen.add(id(item));pending.extend(item.values() if isinstance(item,dict) else item)
    return len(seen)


def owners():
    return shell.ExistingFindingOwners(shell.OwnerInputs('a'*64,0,
        RetainedObject.from_value(CONTEXT),RetainedObject.from_value({'meaning':'test'}),
        RetainedObject.from_value({'transport':'recorded','models':['test'],'effort':'medium'})))


def test_read_view_is_ordered_detached_and_public_mutable_occurrences_stay_independent():
    repeated={'z':[True,1,1.0,-0.0,0.0], 'a':{'汉':'字'}}
    doc=RetainedObject.from_value({'z':repeated,'a':repeated})
    first=doc.read_view();second=doc.read_view()
    assert first==second==doc.value()
    assert list(first)==['a','z'] and list(first['a'])==['a','z']
    assert first['a'] is first['z'] and first['a'] is not second['a']
    before=doc.sha256
    first['a']['a']['汉']='changed in detached snapshot'
    assert doc.sha256==before and second['a']['a']['汉']=='字'
    public=doc.value();public['a']['a']['汉']='caller mutation'
    assert public['z']['a']['汉']=='字' and doc.value()==second


@pytest.mark.parametrize('construction',['factory','canonical'])
def test_ordinary_retained_ledger_finding_reaches_terminal_without_tree_expansion(tmp_path,monkeypatch,construction):
    historical=request()
    owner={'request':historical,'argon_escalation':{'status':'UNCERTAIN','reason':'first'}}
    tree={'later':deepcopy(owner),'first':owner}
    for _ in range(7):tree={'z':tree,'a':tree}
    source={**FINDING,'exact_release_adjudication':tree}
    document=(RetainedObject.from_value(source) if construction=='factory'
              else RetainedObject(json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(',',':'))))
    ledger=shell.FileLedger(tmp_path/'ledger',core.initial_state(source_sha256='a'*64,
        srt_text=SRT,scan_channels=('text',),configuration_ref='b'*64))
    digest=ledger.put(document);stored=ledger.get(digest)
    original=funnel.terminal_check;seen=[]
    def measured(text,finding,**kwargs):
        seen.append(containers(finding))
        return original(text,finding,**kwargs)
    monkeypatch.setattr(funnel,'terminal_check',measured)
    actual=owners().prepare_finding(SRT,stored)
    assert max(seen)<200,seen
    assert actual.finding.sha256==digest and ledger.get(digest)==document
    # Restore the old public reader for the same ordinary owner. All emitted
    # task/check/material bytes must match, including ordered duplicate owners.
    monkeypatch.setattr(RetainedObject,'read_view',RetainedObject.value)
    expected=owners().prepare_finding(SRT,stored)
    assert actual==expected and seen[-1]>2000
    assert actual.check.value()['target_argon_state']['owner_audit']['reason']=='first'


def test_read_view_keeps_subclass_public_decoding_and_validation():
    class Alternate(RetainedObject):
        def value(self):return {'subclass':True}
    assert Alternate('{}').read_view()=={'subclass':True}


@pytest.mark.parametrize('drift',['checksum','source','unknown_owner'])
def test_shared_owner_read_preserves_original_history_guards(drift):
    original=request();owner={'request':original,'argon_escalation':{'status':'UNCERTAIN'}}
    if drift=='checksum':original['request_sha256']='f'*64
    elif drift=='source':
        original['clip_context_binding']['source_media_sha256s']=['f'*64]
        original.pop('request_sha256');original['request_sha256']=funnel.digest(original)
    else:owner['argon_escalation']['status']='UNKNOWN'
    document=RetainedObject.from_value({**FINDING,'exact_release_adjudication':[owner,owner]})
    actual=owners().prepare_finding(SRT,document)
    assert 'target_argon_state' not in actual.check.value()
    assert actual.check.value()['prior_acoustic_observations']==[]
