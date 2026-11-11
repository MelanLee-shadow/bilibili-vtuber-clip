"""Keep the original public dataclass/subclass behavior outside the DAG fast path."""
from __future__ import annotations

from copy import copy, deepcopy
import hashlib
import pickle

import pytest

from src.autoslice.finding_closure_ports import RetainedObject


class SlottedRetained(RetainedObject):
    __slots__ = ("label",)

    def __init__(self, json_text: str, label: str) -> None:
        super().__init__(json_text)
        self.label = label


class RewrittenRetained(RetainedObject):
    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "json_text", '{"rewritten":true}')


class PropertyRetained(RetainedObject):
    @property
    def json_text(self) -> str:
        return self._supplied_text

    @json_text.setter
    def json_text(self, text: str) -> None:
        object.__setattr__(self, "_supplied_text", text)


class MutableEncodingText(str):
    alternate = False

    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        if self.alternate:
            return b'{"changed_encoding":true}'
        return super().encode(encoding, errors)


def pickle_round_trip(value):
    return pickle.loads(pickle.dumps(value))


@pytest.mark.parametrize("restore", [copy, deepcopy, pickle_round_trip],
                         ids=["copy", "deepcopy", "pickle"])
def test_non_dataclass_subclass_slots_survive_copy_state(restore):
    original = SlottedRetained('{"x":1}', "retained-slot-value")
    restored = restore(original)
    assert type(restored) is SlottedRetained
    assert restored.label == original.label
    assert restored.json_text == original.json_text
    assert restored.value() == {"x": 1}
    assert restored.sha256 == original.sha256


def test_post_init_text_rewrite_remains_the_public_value_and_digest():
    first = RewrittenRetained('{"original":1}')
    second = RewrittenRetained('{"original":2}')
    assert first.json_text == second.json_text == '{"rewritten":true}'
    assert first.value() == second.value() == {"rewritten": True}
    assert first.sha256 == hashlib.sha256(first.json_text.encode("utf-8")).hexdigest()
    assert first == second
    assert hash(first) == hash(second)


def test_public_constructor_still_invokes_subclass_json_text_setter():
    retained = PropertyRetained('{"x":1}')
    assert retained._supplied_text == retained.json_text == '{"x":1}'
    assert retained.value() == {"x": 1}
    assert retained.sha256 == hashlib.sha256(retained.json_text.encode("utf-8")).hexdigest()


def test_public_str_subclass_encode_hook_is_not_frozen_in_digest_cache():
    text = MutableEncodingText('{"x":1}')
    retained = RetainedObject(text)
    assert retained.json_text is text
    first_digest = retained.sha256
    text.alternate = True
    assert retained.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert retained.sha256 != first_digest
    assert retained.value() == {"x": 1}
