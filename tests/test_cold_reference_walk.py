"""Keep the ordinary cold reference seam current without scalar predicate work."""
import json

from src.autoslice import review_evidence_store as store


def test_published_cold_history_checks_only_containers_for_references(tmp_path, monkeypatch):
    leaf={'text':'保留原文','tokens':[str(i) for i in range(2000)]}
    path=tmp_path/'audit.json'
    store.persist_document(path,{'findings':[{'cue_index':i,'request':leaf} for i in range(20)]})
    compact=json.loads(path.read_text());value=store.load_review(path)
    predicate=store.is_reference;scalars=[]
    def measured(node):
        if not isinstance(node,(dict,list,tuple)):scalars.append(type(node).__name__)
        return predicate(node)
    monkeypatch.setattr(store,'is_reference',measured)
    assert store._contains_reference(value) is False
    assert scalars==[]
    # Detect a new ordinary reference after an earlier negative observation.
    value['new_reference']=compact['findings'][0]['request']
    assert store._contains_reference(value) is True


def test_cold_reference_walk_keeps_alias_cycle_and_deep_input_behavior():
    shared={'text':'inline'};value={'history':[shared]*500}
    assert store._contains_reference(value) is False
    shared['cycle']=value
    assert store._contains_reference(value) is True
    shared.pop('cycle')
    deep='leaf'
    for _ in range(2000):deep=[deep]
    assert store._contains_reference(deep) is False
    assert store._contains_reference(({'schema_version':store.REF_SCHEMA},)) is True
