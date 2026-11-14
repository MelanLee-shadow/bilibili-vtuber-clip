"""Copy/pickle compatibility and one authoritative retained representation."""
from copy import copy, deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import pickle

import pytest

from src.autoslice.finding_closure_ports import RetainedObject


CONSTRUCTIONS = []


@dataclass(frozen=True)
class RequiredChild(RetainedObject):
    label: str
    notes: list[str] = field(default_factory=list)

    def __post_init__(self):
        CONSTRUCTIONS.append(self.label)
        super().__post_init__()


@dataclass(frozen=True, slots=True)
class SlottedChild(RetainedObject):
    label: str

    def __post_init__(self):
        CONSTRUCTIONS.append(self.label)
        # slots=True dataclasses replace the class object; use the explicit
        # original base call instead of an unrelated zero-argument super trap.
        RetainedObject.__post_init__(self)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


@pytest.mark.parametrize('restore', [copy, deepcopy, lambda item: pickle.loads(pickle.dumps(item))])
def test_required_dataclass_child_fields_and_hooks_survive_copy_without_reconstruction(restore):
    CONSTRUCTIONS.clear()
    document = RequiredChild('{"answer":"汉"}', 'required-label', ['original note'])
    assert CONSTRUCTIONS == ['required-label']
    before = len(CONSTRUCTIONS)
    original_digest = document.sha256
    restored = restore(document)
    assert type(restored) is RequiredChild
    assert len(CONSTRUCTIONS) == before
    assert restored.label == 'required-label'
    assert restored.notes == ['original note']
    assert restored.json_text == document.json_text
    assert restored.value() == {'answer': '汉'}
    assert restored.sha256 == original_digest
    if restore is copy:
        assert restored.notes is document.notes
    else:
        assert restored.notes is not document.notes


@pytest.mark.parametrize('restore', [copy, deepcopy, lambda item: pickle.loads(pickle.dumps(item))])
def test_slotted_dataclass_child_keeps_its_legacy_field_state(restore):
    CONSTRUCTIONS.clear()
    document = SlottedChild('{"answer":7}', 'slot-label')
    before = len(CONSTRUCTIONS)
    restored = restore(document)
    assert type(restored) is SlottedChild
    assert len(CONSTRUCTIONS) == before
    assert restored.label == 'slot-label'
    assert restored.value() == {'answer': 7}
    assert restored.json_text == document.json_text
    assert restored.sha256 == document.sha256


def test_pre_graph_dataclass_pickle_state_still_restores_original_public_text():
    document = object.__new__(RetainedObject)
    document.__setstate__({'json_text': '{"answer":"old-format"}', 'extra': ['retained']})
    assert document.json_text == '{"answer":"old-format"}'
    assert document.value() == {'answer': 'old-format'}
    assert document.sha256 == hashlib.sha256(document.json_text.encode()).hexdigest()
    assert document.extra == ['retained']


def test_restore_cannot_use_one_graph_for_value_and_another_text_for_hash():
    document = object.__new__(RetainedObject)
    mismatched = {'_retained_graph_state': 1, 'attributes': {},
                  'graph': {'answer': 'graph answer'}, 'text': '{"answer":"text answer"}'}
    try:
        document.__setstate__(mismatched)
    except (TypeError, ValueError):
        return  # Explicitly rejecting an inconsistent representation is valid.
    assert document.json_text == canonical(document.value())
    assert document.sha256 == hashlib.sha256(document.json_text.encode()).hexdigest()


def test_graph_state_snapshot_is_detached_and_restored_digest_is_fresh():
    original = {'answer': {'text': 'original'}}
    document = RetainedObject.from_value(original)
    original_digest = document.sha256
    state = document.__getstate__()
    assert isinstance(state, dict)
    graph = state.get('graph')
    assert isinstance(graph, dict)
    graph['answer']['text'] = 'changed in copied state'
    assert document.value() == original
    assert document.sha256 == original_digest
    restored = object.__new__(RetainedObject)
    restored.__setstate__(state)
    assert restored.value() == {'answer': {'text': 'changed in copied state'}}
    assert restored.json_text == canonical(restored.value())
    assert restored.sha256 == hashlib.sha256(restored.json_text.encode()).hexdigest()
    assert restored.sha256 != original_digest
