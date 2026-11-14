"""The checkpoint retains shared histories while preserving its JSON contract."""
import hashlib
import json
from pathlib import Path

from src.autoslice.completed_review_checkpoint import CompletedReviewCheckpoint, _canonical


def test_shared_graph_identity_and_saved_response(tmp_path):
    history = {'text': 'history ' * 2000}
    for _ in range(8):
        history = {'left': history, 'right': history}
    binding = {'stage': 'example', 'inputs': {'history': history}}
    prompt = 'original request'
    expected = json.loads(_canonical(binding))
    expected.setdefault('request_sha256', hashlib.sha256(prompt.encode()).hexdigest())
    first = CompletedReviewCheckpoint(tmp_path / 'cache', binding, prompt)
    view = first._binding_view()['inputs']['history']
    assert view['left'] is view['right']
    public = CompletedReviewCheckpoint(tmp_path / 'cache', binding, prompt).binding
    assert public == expected
    # Public value retains the original independent-occurrence semantics.
    assert public['inputs']['history']['left'] is not public['inputs']['history']['right']
    first.raw = '原始 response\n'
    first.save()
    second = CompletedReviewCheckpoint(tmp_path / 'cache', binding, prompt)
    assert second.load() == first.raw
    assert second.directory == first.directory


def test_legacy_json_special_values(tmp_path):
    from dataclasses import dataclass
    @dataclass
    class Value:
        name: str
    binding = {'path': Path('/x'), 'tuple': (1, 2), 'data': Value('a')}
    cp = CompletedReviewCheckpoint(tmp_path, binding, 'p')
    expected = json.loads(_canonical(binding))
    expected['request_sha256'] = hashlib.sha256(b'p').hexdigest()
    assert cp.binding == expected
