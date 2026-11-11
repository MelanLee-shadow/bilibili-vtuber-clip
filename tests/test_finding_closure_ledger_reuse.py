"""Validated per-ledger reuse retains original guards and public isolation."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import pickle
import time

import pytest

from src.autoslice import finding_closure as core
from src.autoslice.finding_closure_ports import RetainedObject
from src.autoslice.finding_closure_shell import FileLedger
from src.autoslice import review_evidence_store as evidence


def make_ledger(tmp_path):
    initial = core.initial_state(source_sha256='a'*64,
        srt_text='1\n00:00:00,000 --> 00:00:01,000\n文字\n', scan_channels=('recorded',))
    return FileLedger(tmp_path / 'ledger', initial)


def observe_loads(monkeypatch):
    original = evidence.load_retained_review
    calls = []
    def load(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(evidence, 'load_retained_review', load)
    return calls


def test_repeat_public_isolation_and_copy_contract(tmp_path, monkeypatch):
    ledger = make_ledger(tmp_path)
    digest = ledger.put(RetainedObject.from_value({'left': {'value': [1,2]}, 'right': {'value':[1,2]}}))
    calls = observe_loads(monkeypatch)
    first = ledger.get(digest); second = ledger.get(digest)
    assert first is second and len(calls) == 1
    public = first.value(); public['left']['value'].append(3)
    view = first.read_view(); view['right']['value'].append(4)
    assert second.value() == {'left':{'value':[1,2]},'right':{'value':[1,2]}}
    assert copy.deepcopy(second).value() == second.value()
    assert pickle.loads(pickle.dumps(second)).value() == second.value()
    assert json.loads(second.json_text) == second.value()


@pytest.mark.parametrize('damage', ['document','nested','missing'])
def test_cached_reads_recheck_changed_or_missing_evidence(tmp_path, monkeypatch, damage):
    ledger = make_ledger(tmp_path)
    digest = ledger.put(RetainedObject.from_value({'evidence':{'raw':'x'*10000}}))
    calls = observe_loads(monkeypatch); ledger.get(digest)
    path = ledger.root / 'objects' / (digest + '.json')
    packed = json.loads(path.read_bytes())
    blob = next((ledger.root/'objects'/'evidence-store'/'blobs').glob('*.bin'))
    if damage == 'document':
        packed['other'] = 'changed'; path.write_bytes(evidence.encoded(packed))
    elif damage == 'nested':
        blob.write_bytes(b'{}')
    else:
        blob.unlink()
    with pytest.raises((ValueError, OSError)):
        ledger.get(digest)
    with pytest.raises((ValueError, OSError)):
        ledger.get(digest)
    assert len(calls) == 3  # failures never become reusable entries


def test_top_path_replacement_is_rechecked(tmp_path, monkeypatch):
    ledger = make_ledger(tmp_path); digest = ledger.put(RetainedObject.from_value({'text':'stable'}))
    calls = observe_loads(monkeypatch); ledger.get(digest)
    path = ledger.root/'objects'/(digest+'.json'); target = path.with_suffix('.saved')
    path.rename(target); path.symlink_to(target.name)
    # If the original reader permits an alias, it still must reopen/recheck it.
    assert ledger.get(digest).value() == {'text':'stable'}
    assert len(calls) == 2


def test_concurrent_same_reference_and_bounded_lru(tmp_path, monkeypatch):
    ledger = make_ledger(tmp_path)
    a = ledger.put(RetainedObject.from_value({'value':'a'})); b = ledger.put(RetainedObject.from_value({'value':'b'}))
    original=evidence.load_retained_review; calls=[]
    def slow(path):
        calls.append(path); time.sleep(.02); return original(path)
    monkeypatch.setattr(evidence,'load_retained_review',slow)
    with ThreadPoolExecutor(max_workers=6) as pool:
        values=list(pool.map(lambda _: ledger.get(a).value(),range(12)))
    assert values == [{'value':'a'}]*12 and len(calls)==1
    ledger._retained_cache.MAX_ENTRIES=1
    ledger.get(b); assert len(ledger._retained_cache._entries)==1
    ledger.get(a); assert len(calls)==3
    assert ledger._retained_cache._bytes <= ledger._retained_cache.MAX_BYTES


def test_oversize_entries_are_not_retained(tmp_path, monkeypatch):
    ledger = make_ledger(tmp_path)
    digest = ledger.put(RetainedObject.from_value({'value':'a' * 3000}))
    ledger._retained_cache.MAX_BYTES = 1
    calls = observe_loads(monkeypatch)
    assert ledger.get(digest).value() == ledger.get(digest).value()
    assert len(calls) == 2 and not ledger._retained_cache._entries
    assert ledger._retained_cache._bytes == 0
