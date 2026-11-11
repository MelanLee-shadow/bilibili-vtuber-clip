"""An ordinary cold history must not repeatedly measure an already seen DAG."""
import cProfile
import hashlib
import json
import pstats

import pytest

from src.autoslice.canonical_json_hash import canonical_sha256
from src.autoslice import review_evidence_store as store


@pytest.mark.parametrize('inventory_form', ['scalars', 'records'])
def test_cold_published_history_reuses_size_work_after_wide_inventory(tmp_path, inventory_form):
    inventory = [f'original-member-{i}' for i in range(5000)]
    if inventory_form == 'records':
        inventory = [{'member': value} for value in inventory]
    request = {'text': '真实保留证据' * 1000, 'scores': list(range(1000))}
    path = tmp_path/'history.json'
    store.persist_document(path, {'a_inventory': inventory,
        'history': [{'request': request, 'cue_index': i} for i in range(80)]})
    value = store.load_review(path)
    expected = hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
        separators=(',', ':'),allow_nan=False).encode()).hexdigest()
    profile = cProfile.Profile()
    actual = profile.runcall(canonical_sha256, value)
    assert actual == expected
    visits = sum(data[1] for (filename, line, name),data in pstats.Stats(profile).stats.items()
        if filename.endswith('/canonical_json_hash.py') and name == 'upper')
    # Work should follow the one inventory, one shared request and 80 rows.
    # Count original computations rather than imposing a machine-time budget.
    assert visits < 4 * (5000 + 1000 + 80), visits
