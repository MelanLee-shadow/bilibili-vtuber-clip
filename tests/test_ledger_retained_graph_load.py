"""Ordinary immutable ledger ingress must retain storage DAGs as DAGs."""
import hashlib
import json
from pathlib import Path
import tracemalloc

import pytest

from src.autoslice import finding_closure as closure
from src.autoslice.finding_closure_ports import RetainedObject
from src.autoslice.finding_closure_shell import FileLedger
from src.autoslice import review_evidence_store as evidence
from src.autoslice.argon_escalation_storage import MAX_ARTIFACT_BYTES


def ledger(tmp_path):
    state = closure.initial_state(source_sha256='a'*64,
        srt_text='1\n00:00:00,000 --> 00:00:01,000\n原话\n', scan_channels=('recorded',))
    return FileLedger(tmp_path/'closure', state)


@pytest.mark.parametrize('fields', [('prior', 'successor'), ('left', 'right')])
def test_ordinary_writer_and_immutable_reader_keep_large_repeated_history_bounded(tmp_path, fields):
    # Repeated containers, not just large scalar strings, expose expansion at
    # immutable ingress. Scalars were already shared by the old blob cache.
    body = {'rows': [
        {'index': index, 'raw': 'retained unmodified answer 汉字' * 10}
        for index in range(100)
    ]}
    for _ in range(8):
        body = {fields[0]: body, fields[1]: body}
    document = RetainedObject.from_value(body)
    owner = ledger(tmp_path)
    digest = owner.put(document)
    # The ordinary writer can deduplicate any large field, including fields
    # outside REFERENCE_FIELDS. Loading a ledger must not expand it again.
    tracemalloc.start()
    restored = owner.get(digest)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert restored == document
    assert restored.sha256 == digest
    assert current < 2_000_000, current
    # The unchanged protected blob reader reserves its maximum read buffer.
    assert peak < MAX_ARTIFACT_BYTES + 2_000_000, peak


def test_public_values_and_mutable_audit_reader_keep_independent_envelopes(tmp_path):
    leaf = {'raw': '原始记录' * 3000, 'values': [1, 2]}
    source = {'left': leaf, 'right': leaf}
    owner = ledger(tmp_path)
    digest = owner.put(RetainedObject.from_value(source))
    doc = owner.get(digest)
    expected = json.loads(json.dumps(source))
    first, second = doc.value(), doc.value()
    first['left']['values'].append(3)
    assert first['right'] == second['left'] == expected['left']
    assert second == doc.value() == expected
    mutable = evidence.load_review(owner.root/'objects'/(digest+'.json'))
    mutable['left']['values'].append(4)
    assert mutable['right'] == expected['right']
    assert doc.sha256 == hashlib.sha256(evidence.encoded(source)).hexdigest()


@pytest.mark.parametrize('damage', ['nested', 'document', 'missing'])
def test_ledger_graph_sharing_preserves_current_hash_and_missing_blob_rejections(tmp_path, damage):
    owner = ledger(tmp_path)
    digest = owner.put(RetainedObject.from_value({'left': {'raw': 'answer'*2000}}))
    path = owner.root/'objects'/(digest+'.json')
    packed = json.loads(path.read_bytes())
    owner.get(digest)
    if damage == 'document':
        packed['changed'] = True
        path.write_bytes(evidence.encoded(packed))
        reason = 'FINDING_CLOSURE_ARTIFACT_HASH_DRIFT'
    else:
        blob = Path(packed['left']['raw']['path'])
        if damage == 'missing':
            blob.unlink()
            reason = 'REVIEW_EVIDENCE_BLOB_MISSING'
        else:
            blob.write_bytes(b'{}')
            reason = 'ARTIFACT_SHA_MISMATCH'
    with pytest.raises((ValueError, closure.ClosureContractError), match=reason):
        owner.get(digest)
