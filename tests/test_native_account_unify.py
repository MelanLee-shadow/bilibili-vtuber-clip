"""Ordinary account continuation and cold range consumption, with no provider I/O."""
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from src.autoslice import native_context_witness as native
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice import gemini_range_context as gemini
from src.autoslice import prepared_native_audio_recovery as recovery
from src.autoslice.native_audio_budget_receipt import resume_native_audio_budget
from src.autoslice.prepared_native_audio_recovery import inherit_prepared_native_audio
from src.autoslice.supplement_audio_budget import start_budget
from tests.test_current_native_account_continuation import _offline, _kwargs
from tests.test_native_audio_history_reconciliation import _fork, _auto, _observe
from tests.test_gemini_range_context import _check_request, _range_request, _verifier
from tests.test_native_context_witness import (
    _install_real_range_budget_writer, _range_requests,
    _write_canonical_budget_receipt, _write_pre_http_config_envelope,
)


def _active_fork_member(tmp_path, monkeypatch):
    """Real writer -> v3 -> two ordinary output continuations, no hand ledger."""
    fork = _fork(tmp_path, monkeypatch)
    source, runtime, _, _, _ = fork
    state = _auto(fork)
    from src.autoslice.native_foreign_witness import build_native_foreign_witness
    _install_real_range_budget_writer(monkeypatch, [])
    def observe_active(root, start):
        # Generate the complete response hash/timeline via the ordinary writer,
        # with only extraction and provider I/O replaced at their existing seams.
        observer = build_native_foreign_witness(source_media=source, output_dir=root, provider='mai',
            budget_receipt_path=root / 'native-audio-budget.json')
        assert observer(start_ms=start, end_ms=start + 1000)['status'] == 'OBSERVED'
    # Resolve only the active branch with normal writes. The first immutable
    # incoming branch's pending reservation remains unknown and charged.
    observe_active(state.cache_root, 13000)
    incoming = tmp_path / 'new-active-output'
    shutil.copytree(state.cache_root, incoming)
    shared = json.loads(state.budget_receipt.read_bytes())
    _observe(state.cache_root, source, start=15000, status='FAILED')
    observe_active(state.cache_root, 17000)
    canonical_raw = state.budget_receipt.read_bytes()
    start_budget(source)
    resume_native_audio_budget(source_media=source,
        source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        receipt_path=incoming / 'native-audio-budget.json')
    _observe(incoming, source, start=19000, status='RESPONSE_REJECTED')
    observe_active(incoming, 21000)
    incoming_raw = (incoming / 'native-audio-budget.json').read_bytes()
    start_budget(source)
    resume_native_audio_budget(source_media=source,
        source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), receipt_path=state.budget_receipt)
    return source, runtime, state, incoming, shared, canonical_raw, incoming_raw


def test_active_fork_union_factory_retains_each_call_receipt_and_cold_cache_once(tmp_path, monkeypatch):
    from src.autoslice.native_audio_budget_receipt import _require_budget_history_extension
    from src.autoslice.supplement_audio_budget import get_budget
    from src.autoslice.native_foreign_witness import native_acquisition_identity, read_native_acquisition
    source, runtime, state, incoming, shared, canonical_raw, incoming_raw = _active_fork_member(tmp_path, monkeypatch)
    _offline(monkeypatch)
    kw = _kwargs(tmp_path, source, state)
    kw.update(cid='candidate', out_root=incoming)
    receipt = pipeline._build_entity_verification_context(**kw).verify_confusable_entity.relisten_budget_state()['receipt']
    canonical, original = json.loads(canonical_raw), json.loads(incoming_raw)
    b, h = receipt['budget'], receipt['budget']['account_history']
    assert b['attempt_count'] == canonical['budget']['attempt_count'] + original['budget']['attempt_count'] - shared['budget']['attempt_count']
    assert b['total_audio_ms'] == canonical['budget']['total_audio_ms'] + original['budget']['total_audio_ms'] - shared['budget']['total_audio_ms']
    assert len({(r['history_branch_sha256'], r['attempt_id']) for r in b['attempts']}) == b['attempt_count']
    assert {'OBSERVED', 'FAILED', 'RESPONSE_REJECTED'} <= {r['status'] for r in b['attempts']}
    assert b['pending_attempt_count'] == shared['budget']['pending_attempt_count']
    retained = h['active_forks'][0]
    assert Path(retained['canonical']['path']).read_bytes() == canonical_raw
    assert Path(retained['incoming']['path']).read_bytes() == incoming_raw
    assert (incoming / 'native-audio-budget.json').read_bytes() == incoming_raw
    _require_budget_history_extension(canonical, receipt)
    _require_budget_history_extension(original, receipt)
    from src.autoslice.semantic_audio_recovery import _dispatched_since
    assert _dispatched_since(canonical, receipt) == []
    assert _dispatched_since(original, receipt) is None
    from src.autoslice.native_audio_history_reconciliation import history_cache_roots
    cache = history_cache_roots(state.cache_root, h)[-1]
    observation = read_native_acquisition(source_media=source, root=cache,
        identity=native_acquisition_identity(source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            provider='mai', start_ms=21000, end_ms=22000))
    assert observation['status'] == 'OBSERVED'
    # Cold restore, same parent repeated, and a new continuation must not bill
    # the retained suffix again or alter either original receipt.
    start_budget(source)
    joined = inherit_prepared_native_audio(cid='candidate', parent=incoming, source=source, runtime_root=runtime)
    assert json.loads(joined.budget_receipt.read_bytes()) == receipt
    assert get_budget(source).snapshot() == b
    recovery._parent_inputs(parent=joined.cache_root, cid='candidate', source=source,
        source_sha=hashlib.sha256(source.read_bytes()).hexdigest())
    _observe(joined.cache_root, source, start=23000, status='FAILED')
    after = json.loads(joined.budget_receipt.read_bytes())
    assert after['budget']['attempt_count'] == b['attempt_count'] + 1
    assert after['budget']['account_history']['active_forks'] == h['active_forks']
    dispatched = _dispatched_since(receipt, after)
    assert len(dispatched) == 1 and dispatched[0]['start_ms'] == 23000


def test_active_fork_later_parent_extension_adds_only_new_calls(tmp_path, monkeypatch):
    from src.autoslice.supplement_audio_budget import get_budget
    source, runtime, state, incoming, _, _, incoming_raw = _active_fork_member(tmp_path, monkeypatch)
    first = inherit_prepared_native_audio(cid='candidate', parent=incoming, source=source, runtime_root=runtime)
    before = get_budget(source).snapshot()
    start_budget(source)
    resume_native_audio_budget(source_media=source, source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        receipt_path=incoming / 'native-audio-budget.json')
    _observe(incoming, source, start=31000, status='FAILED')
    extended = (incoming / 'native-audio-budget.json').read_bytes()
    start_budget(source)
    final = inherit_prepared_native_audio(cid='candidate', parent=incoming, source=source, runtime_root=runtime)
    after = get_budget(source).snapshot()
    assert after['attempt_count'] == before['attempt_count'] + 1
    assert after['total_audio_ms'] == before['total_audio_ms'] + 1000
    assert len(after['account_history']['active_forks']) == 2
    assert Path(after['account_history']['active_forks'][0]['incoming']['path']).read_bytes() == incoming_raw
    assert (incoming / 'native-audio-budget.json').read_bytes() == extended
    assert len({(r['history_branch_sha256'], r['attempt_id']) for r in after['attempts']}) == after['attempt_count']
    assert inherit_prepared_native_audio(cid='candidate', parent=incoming, source=source, runtime_root=runtime) == final


def test_active_fork_unavailable_incoming_cache_keeps_identity_on_cold_consumer(tmp_path, monkeypatch):
    from src.autoslice.supplement_audio_budget import get_budget
    source, runtime, state, incoming, _, _, incoming_raw = _active_fork_member(tmp_path, monkeypatch)
    job = next((incoming / 'native-exact/mai').glob('*-21000-22000'))
    shutil.rmtree(job)
    result = inherit_prepared_native_audio(cid='candidate', parent=incoming, source=source, runtime_root=runtime)
    b = get_budget(source).snapshot()
    missing = next(a for a in b['attempts'] if a['start_ms'] == 21000)
    assert missing['status'] == 'OBSERVED'
    assert missing['history_branch_sha256'] == json.loads(incoming_raw)['receipt_sha256']
    start_budget(source)
    resume_native_audio_budget(source_media=source, source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        receipt_path=result.budget_receipt)
    parsed, _ = recovery._parent_inputs(parent=result.cache_root, cid='candidate', source=source,
        source_sha=hashlib.sha256(source.read_bytes()).hexdigest())
    assert parsed['budget'] == b
    assert (incoming / 'native-audio-budget.json').read_bytes() == incoming_raw


@pytest.mark.parametrize('mode', ['same', 'extend', 'damaged_extend'])
def test_active_prefix_after_union_preserves_frozen_branches_without_new_fork(tmp_path, monkeypatch, mode):
    from src.autoslice.supplement_audio_budget import get_budget
    from src.autoslice.native_audio_budget_receipt import BudgetReceiptPersistenceError, receipt_sha256
    extend = mode != 'same'
    source, runtime, state, incoming, _, _, _ = _active_fork_member(tmp_path, monkeypatch)
    prefix_parent = tmp_path / 'canonical-branch-output'
    shutil.copytree(state.cache_root, prefix_parent)
    inherit_prepared_native_audio(cid='candidate', parent=incoming, source=source, runtime_root=runtime)
    before = get_budget(source).snapshot()
    start_budget(source)
    resume_native_audio_budget(source_media=source, source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        receipt_path=prefix_parent / 'native-audio-budget.json')
    if extend:
        _observe(prefix_parent, source, start=33000, status='FAILED')
    raw = (prefix_parent / 'native-audio-budget.json').read_bytes()
    # The union's global counter is larger even though the incoming active
    # branch is the genuine continuation. Total revision cannot decide this.
    assert json.loads(raw)['revision'] < before['revision']
    start_budget(source)
    if mode == 'damaged_extend':
        bad = json.loads(raw)
        bad['providers'] = []
        bad['receipt_sha256'] = receipt_sha256(bad)
        (prefix_parent / 'native-audio-budget.json').write_text(json.dumps(bad))
        frozen = state.budget_receipt.read_bytes()
        with pytest.raises(BudgetReceiptPersistenceError, match='provider summary'):
            inherit_prepared_native_audio(cid='candidate', parent=prefix_parent, source=source, runtime_root=runtime)
        assert state.budget_receipt.read_bytes() == frozen
        return
    result = inherit_prepared_native_audio(cid='candidate', parent=prefix_parent, source=source, runtime_root=runtime)
    after = get_budget(source).snapshot()
    assert after['account_history']['active_forks'] == before['account_history']['active_forks']
    assert after['attempt_count'] == before['attempt_count'] + int(extend)
    if extend:
        assert after['account_history']['active_budget']['attempts'][-1]['start_ms'] == 33000
    assert (prefix_parent / 'native-audio-budget.json').read_bytes() == raw


@pytest.mark.parametrize('mode', ['same', 'canonical_prefix', 'incoming_prefix'])
def test_active_continuation_prefix_and_identity_do_not_add_forks(tmp_path, monkeypatch, mode):
    fork = _fork(tmp_path, monkeypatch)
    source, runtime, _, _, _ = fork
    state = _auto(fork)
    incoming = tmp_path / 'prefix-output'
    shutil.copytree(state.cache_root, incoming)
    if mode == 'canonical_prefix':
        _observe(incoming, source, start=27000, status='FAILED')
    elif mode == 'incoming_prefix':
        _observe(state.cache_root, source, start=29000, status='RESPONSE_REJECTED')
    expected = (incoming / 'native-audio-budget.json').read_bytes() if mode == 'canonical_prefix' else state.budget_receipt.read_bytes()
    start_budget(source)
    result = inherit_prepared_native_audio(cid='candidate', parent=incoming, source=source, runtime_root=runtime)
    assert json.loads(result.budget_receipt.read_bytes()) == json.loads(expected)
    assert not json.loads(expected)['budget']['account_history'].get('active_forks')


@pytest.mark.parametrize('damage', ['receipt_sha', 'immutable_history', 'archive_bytes', 'shared_receipt'])
def test_active_fork_union_keeps_original_identity_and_byte_guards(tmp_path, monkeypatch, damage):
    from src.autoslice.native_audio_budget_receipt import BudgetReceiptPersistenceError, receipt_sha256
    source, runtime, state, incoming, _, canonical_raw, incoming_raw = _active_fork_member(tmp_path, monkeypatch)
    value = json.loads(incoming_raw)
    if damage == 'receipt_sha':
        value['receipt_sha256'] = 'sha256:' + 'f' * 64
    elif damage == 'immutable_history':
        # Independently valid plan bytes, but a different immutable identity.
        original_ref = value['budget']['account_history']['plan_reference']
        other = tmp_path / 'other-valid-plan.json'
        other.write_bytes(Path(original_ref['path']).read_bytes())
        value['budget']['account_history']['plan_reference'] = {**original_ref, 'path': str(other)}
        value['receipt_sha256'] = receipt_sha256(value)
    elif damage == 'archive_bytes':
        digest = hashlib.sha256(canonical_raw).hexdigest()
        (state.cache_root / 'parents' / (digest + '.json')).write_text('{}')
    else:
        from src.autoslice.subtitle_audio_evidence import _digest
        path = next((incoming / 'native-exact/mai').glob('*-13000-14000/input.audio-evidence.mai.json'))
        saved = json.loads(path.read_bytes())
        saved['evidence']['response_sha256'] = 'd' * 64
        saved['evidence_sha256'] = _digest(saved['evidence'])
        path.write_text(json.dumps(saved))
    if damage in {'receipt_sha', 'immutable_history'}:
        (incoming / 'native-audio-budget.json').write_text(json.dumps(value))
    with pytest.raises((ValueError, BudgetReceiptPersistenceError)):
        inherit_prepared_native_audio(cid='candidate', parent=incoming, source=source, runtime_root=runtime)
    assert state.budget_receipt.read_bytes() == canonical_raw


@pytest.mark.parametrize('damage', ['same_identity_different_receipt_sha', 'changed_archived_bytes'])
def test_active_fork_cold_resume_revalidates_original_receipt_bindings(tmp_path, monkeypatch, damage):
    from src.autoslice.native_audio_budget_receipt import BudgetReceiptPersistenceError, receipt_sha256
    source, runtime, state, incoming, _, _, _ = _active_fork_member(tmp_path, monkeypatch)
    result = inherit_prepared_native_audio(cid='candidate', parent=incoming, source=source, runtime_root=runtime)
    value = json.loads(result.budget_receipt.read_bytes())
    ref = value['budget']['account_history']['active_forks'][0]['incoming']
    if damage == 'same_identity_different_receipt_sha':
        ref['receipt_sha256'] = 'sha256:' + 'a' * 64
        value['receipt_sha256'] = receipt_sha256(value)
        result.budget_receipt.write_text(json.dumps(value))
    else:
        path = Path(ref['path'])
        path.write_bytes(path.read_bytes() + b' ')
    start_budget(source)
    with pytest.raises(BudgetReceiptPersistenceError, match='reconciliation'):
        resume_native_audio_budget(source_media=source, source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            receipt_path=result.budget_receipt)


def _forbidden(*args, **kwargs):
    raise AssertionError("OFFLINE_PROVIDER_IO_FORBIDDEN")


def test_factory_joins_own_observed_and_failed_history_before_range_consumption(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'new ordinary member')
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path / 'runtime'))
    parent = tmp_path / 'parent'
    _write_canonical_budget_receipt(source=source, receipt_path=parent / 'native-audio-budget.json',
                                  source_sha256=sha)
    state = inherit_prepared_native_audio(cid='new-member', parent=parent, source=source,
                                         runtime_root=tmp_path / 'runtime')
    kw = _kwargs(tmp_path, source, state)
    output = kw['out_root']
    check, request = _range_requests(sha)
    dispatches = []
    _install_real_range_budget_writer(monkeypatch, dispatches)
    real_builder = native.build_independent_context_witnesses
    original = real_builder(fallback=_forbidden, source_media=source,
        output_dir=output / 'independent-context', providers=('mai',),
        budget_receipt_path=output / 'native-audio-budget.json', native_observation_cache_root=output)
    assert original.observe_range_context(check_request=check, range_witness_request=request)[0]['status'] == 'OBSERVED'
    # Real failed reservation remains charged alongside the usable observation.
    from src.autoslice.supplement_audio_budget import get_budget
    from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
    b = get_budget(source)
    token = b.consume('moss', native.NATIVE_MODELS['moss'], 30000, 31000)
    b.finish_attempt(token, status='FAILED', reason_code='OFFLINE_FAILURE')
    persist_native_audio_budget_receipt(source_media=source, source_media_sha256=sha,
                                       receipt_path=output / 'native-audio-budget.json')
    own_before = (output / 'native-audio-budget.json').read_bytes()
    start_budget(source)
    resume_native_audio_budget(source_media=source, source_media_sha256=sha, receipt_path=state.budget_receipt)
    _offline(monkeypatch)
    monkeypatch.setattr(native, 'build_native_foreign_witness', _forbidden)
    monkeypatch.setattr(native, 'build_independent_context_witnesses',
                        lambda **kwargs: real_builder(**{**kwargs, 'providers': ('mai',)}))
    context = pipeline._build_entity_verification_context(**kw)
    read = context.verify_confusable_entity.relisten_budget_state()
    assert read['receipt']['budget']['attempt_count'] == 2
    assert read['receipt']['budget']['failed_attempt_count'] == 1
    assert read['receipt']['budget']['total_audio_ms'] == 15000
    rows = context.verify_confusable_entity.observe_range_context(check_request=check,
                                                                  range_witness_request=request)
    assert rows[0]['status'] == 'OBSERVED'
    assert dispatches == [('mai', 5100, 19100)]
    assert (output / 'native-audio-budget.json').read_bytes() == own_before
    assert read['receipt']['budget']['attempts'] == json.loads(own_before)['budget']['attempts']


def test_registered_v3_extension_joins_via_same_factory_without_new_branch_or_ids(tmp_path, monkeypatch):
    fork = _fork(tmp_path, monkeypatch)
    source, runtime, _, _, _ = fork
    state = _auto(fork)
    own = tmp_path / 'own-output'
    shutil.copytree(state.cache_root, own)
    _observe(own, source, start=15000, tag='new ordinary v3 continuation')
    own_bytes = (own / 'native-audio-budget.json').read_bytes()
    expected = json.loads(own_bytes)
    start_budget(source)
    resume_native_audio_budget(source_media=source,
        source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), receipt_path=state.budget_receipt)
    _offline(monkeypatch)
    kw = _kwargs(tmp_path, source, state)
    kw.update(cid='candidate', out_root=own)
    before = json.loads(state.budget_receipt.read_bytes())
    context = pipeline._build_entity_verification_context(**kw)
    receipt = context.verify_confusable_entity.relisten_budget_state()['receipt']
    assert receipt == expected
    assert receipt['budget']['account_history']['plan'] == before['budget']['account_history']['plan']
    assert receipt['budget']['account_history']['incoming'] == before['budget']['account_history']['incoming']
    assert receipt['budget']['pending_attempt_count'] == 1
    assert (own / 'native-audio-budget.json').read_bytes() == own_bytes


@pytest.mark.parametrize('cache_only', [False, True])
@pytest.mark.parametrize('failure, expected', [
    ('NATIVE_ACQUISITION_OWNER_RECOVERY_REQUIRED', 'NATIVE_ACQUISITION_ORIGINAL_IDENTITY_MISSING'),
    ('MOSS_HTTP_ERROR', 'MOSS_HTTP_ERROR'),
    ('CORRUPT', 'NATIVE_RANGE_ENVELOPE_INVALID'),
])
def test_range_distinguishes_identity_provider_failure_and_bad_envelope(
    tmp_path, monkeypatch, cache_only, failure, expected,
):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'failure classification member')
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / 'output'
    budget = output / 'native-audio-budget.json'
    _write_canonical_budget_receipt(source=source, receipt_path=budget, source_sha256=sha)
    check, request = _range_requests(sha)
    envelope, _ = _write_pre_http_config_envelope(source=source, output=output,
        request=request, source_sha256=sha, provider='moss', reason_code=failure)
    if failure == 'CORRUPT':
        envelope.write_text('{}')
    before = envelope.read_bytes(), budget.read_bytes()
    monkeypatch.setattr(native, 'build_native_foreign_witness', _forbidden)
    wrapper = native.build_independent_context_witnesses(fallback=_forbidden,
        source_media=source, output_dir=output, providers=('moss',), budget_receipt_path=budget)
    row = wrapper.observe_range_context(check_request=check, range_witness_request=request,
                                         cache_only=cache_only)[0]
    assert row['status'] == 'UNCERTAIN'
    assert row['reason_code'] == expected
    assert (envelope.read_bytes(), budget.read_bytes()) == before


@pytest.mark.parametrize('provider', ['mai', 'moss'])
@pytest.mark.parametrize('new_metadata', [False, True])
def test_range_alias_consumes_original_request_and_envelope_without_resigning(tmp_path, monkeypatch, provider, new_metadata):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'alias ordinary member')
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    check, request = _range_requests(sha)
    output = tmp_path / 'original'
    cache = tmp_path / 'cache'
    budget = tmp_path / 'native-audio-budget.json'
    dispatches = []
    _install_real_range_budget_writer(monkeypatch, dispatches)
    first = native.build_independent_context_witnesses(fallback=_forbidden, source_media=source,
        output_dir=output, providers=(provider,), budget_receipt_path=budget,
        native_observation_cache_root=cache)
    observed = first.observe_range_context(check_request=check, range_witness_request=request)[0]
    assert observed['status'] == 'OBSERVED'
    from pathlib import Path
    envelope = Path(observed['artifact_bindings']['native_envelope_path'])
    before = envelope.read_bytes(), budget.read_bytes()
    alias = {**request, **({'evidence_id': 'new independent candidate metadata'} if new_metadata else {})}
    alias['request_sha256'] = native._canonical_sha256({k: v for k, v in alias.items() if k != 'request_sha256'})
    monkeypatch.setattr(native, 'build_native_foreign_witness', _forbidden)
    cold = native.build_independent_context_witnesses(fallback=_forbidden, source_media=source,
        output_dir=tmp_path / 'current', providers=(provider,), budget_receipt_path=budget,
        native_observation_cache_root=cache, cached_observation_roots=(output,))
    row = cold.observe_range_context(check_request=check, range_witness_request=alias, cache_only=True)[0]
    assert row['status'] == 'OBSERVED'
    assert row['artifact_bindings']['native_envelope_path'] == str(envelope)
    assert row['witness_request'] == request
    assert not (tmp_path / 'current' / provider / 'native-context' / f"{alias['request_sha256']}.json").exists()
    assert (envelope.read_bytes(), budget.read_bytes()) == before
    assert dispatches == [(provider, 5100, 19100)]
    # The actual cold consumer still rejects drift in the retained original.
    envelope.write_bytes(envelope.read_bytes() + b' ')
    from src.autoslice.independent_context_adjudication import validate_native_range_context_observation
    assert validate_native_range_context_observation(check['whole_clip_current_srt'], check, row) == (
        None, 'NATIVE_RANGE_ARTIFACT_BINDING_INVALID')


@pytest.mark.parametrize('route', ['copy', 'rebuild', 'unrecoverable', 'writer-error', 'receipt', 'both'])
@pytest.mark.parametrize('status', ['OBSERVED', 'TEXT_UNLOCATED'])
def test_inheritance_cache_loss_retains_original_account_and_only_complete_caches(
    tmp_path, monkeypatch, route, status,
):
    from src.autoslice import native_foreign_witness as foreign
    from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
    from src.autoslice.supplement_audio_budget import get_budget
    from tests.test_prepared_native_audio_recovery import _seed
    parent, source, _ = _seed(tmp_path)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    # Create the alternative status via the ordinary budget producer, not by
    # changing a signed ledger. Its media/receipt shares the real acquisition.
    resume_native_audio_budget(source_media=source, source_media_sha256=sha,
                              receipt_path=parent / 'native-audio-budget.json')
    if status == 'TEXT_UNLOCATED':
        budget = get_budget(source)
        token = budget.consume('mai', native.NATIVE_MODELS['mai'], 1000, 2500)
        budget.finish_attempt(token, status=status)
        persist_native_audio_budget_receipt(source_media=source, source_media_sha256=sha,
                                          receipt_path=parent / 'native-audio-budget.json')
    audio = next((parent / 'native-exact/mai').rglob('input.mp3'))
    receipt = audio.with_suffix('.audio-evidence.mai.json')
    raw = audio.read_bytes()
    if route == 'copy':
        copy = tmp_path / 'runtime/another-run/input.mp3'
        copy.parent.mkdir(parents=True)
        copy.write_bytes(raw)
    monkeypatch.setattr(foreign, '_extract_exact_mp3',
        lambda src, out, start, end: out.write_bytes(raw if route == 'rebuild' else b'different codec bytes'))
    if route == 'writer-error':
        def extraction_failed(*args):
            raise RuntimeError('WITNESS_AUDIO_EXTRACTION_FAILED')
        monkeypatch.setattr(foreign, '_extract_exact_mp3', extraction_failed)
    if route in {'copy', 'rebuild', 'unrecoverable', 'writer-error', 'both'}:
        audio.unlink()
    if route in {'receipt', 'both'}:
        receipt.unlink()
    before = {p: p.read_bytes() for p in parent.rglob('*') if p.is_file()}
    original = (parent / 'native-audio-budget.json').read_bytes()
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path / 'runtime'))
    start_budget(source)
    state = inherit_prepared_native_audio(cid='missing-member', parent=parent,
                                         source=source, runtime_root=tmp_path / 'runtime')
    assert state.budget_receipt.read_bytes() == original
    assert get_budget(source).snapshot()['attempts'] == json.loads(original)['budget']['attempts']
    manifest = json.loads((state.cache_root / 'parent.json').read_bytes())
    target = state.cache_root / audio.relative_to(parent)
    if route in {'copy', 'rebuild'}:
        assert target.read_bytes() == raw
        assert not manifest.get('unusable_caches')
    else:
        assert not target.exists()
        assert len(manifest['unusable_caches']) == 1
        # A cold downstream checkpoint sees the same eligibility, rather than
        # requiring all historical caches again or manufacturing observations.
        from src.autoslice.prepared_native_audio_recovery import _parent_inputs
        cold_budget, cold_inventory = _parent_inputs(parent=state.cache_root, cid='missing-member',
            source=source, source_sha=sha)
        assert cold_budget == json.loads(original)
        assert str(audio.relative_to(parent)) not in cold_inventory
    # Unaffected MOSS cache remains reusable and all historical originals stay
    # unchanged, including absence at the original paths.
    assert next((state.cache_root / 'native-exact/moss').rglob('input.mp3')).is_file()
    assert {p: p.read_bytes() for p in parent.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('damage', ['audio', 'receipt', 'symlink', 'missing-audio-bad-receipt', 'identity'])
def test_missing_cache_contract_preserves_damage_and_identity_guards(tmp_path, monkeypatch, damage):
    from tests.test_prepared_native_audio_recovery import _seed
    parent, source, _ = _seed(tmp_path)
    audio = next((parent / 'native-exact/mai').rglob('input.mp3'))
    receipt = audio.with_suffix('.audio-evidence.mai.json')
    if damage == 'audio':
        audio.write_bytes(b'changed')
    elif damage in {'receipt', 'missing-audio-bad-receipt'}:
        receipt.write_text('{}')
        if damage.startswith('missing'):
            audio.unlink()
    elif damage == 'symlink':
        audio.unlink()
        audio.symlink_to(source)
    else:
        (parent / 'native-audio-budget.json').unlink()
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path / 'runtime'))
    with pytest.raises((OSError, ValueError)):
        inherit_prepared_native_audio(cid='control', parent=parent, source=source, runtime_root=tmp_path)


def test_missing_parent_receipt_keeps_verified_canonical_copy_reusable(tmp_path, monkeypatch):
    from tests.test_prepared_native_audio_recovery import _seed
    parent, source, _ = _seed(tmp_path)
    monkeypatch.setenv('AUTOSLICE_BASE', str(tmp_path / 'runtime'))
    state = inherit_prepared_native_audio(cid='copy-retained', parent=parent, source=source, runtime_root=tmp_path)
    original = state.budget_receipt.read_bytes()
    next((parent / 'native-exact/mai').rglob('*.json')).unlink()
    start_budget(source)
    again = inherit_prepared_native_audio(cid='copy-retained', parent=parent, source=source, runtime_root=tmp_path)
    assert again == state and state.budget_receipt.read_bytes() == original
    assert not json.loads((state.cache_root / 'parent.json').read_bytes()).get('unusable_caches')


def test_missing_incoming_cache_preserves_v3_branch_identity_and_reaches_ordinary_consumer(tmp_path, monkeypatch):
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    fork = _fork(tmp_path, monkeypatch)
    source, runtime, _, incoming, _ = fork
    lost = next((incoming / 'native-exact/mai').glob('*-7000-8000/*.json'))
    lost.unlink()
    immutable = (incoming / 'native-audio-budget.json').read_bytes()
    state = _auto(fork)
    before = json.loads(state.budget_receipt.read_bytes())
    assert before['budget']['attempt_count'] == 6
    assert before['budget']['pending_attempt_count'] == 1
    manifest = json.loads((state.cache_root / 'parent.json').read_bytes())
    identity = native.native_acquisition_identity(
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), provider='mai',
        start_ms=7000, end_ms=8000)
    key = str(native.native_acquisition_path(state.cache_root, identity).relative_to(state.cache_root))
    assert manifest['unusable_caches'][key]['attempts'][0] in before['budget']['attempts']
    calls = []
    _install_real_range_budget_writer(monkeypatch, calls)
    request = build_witness_request({'evidence_id': 'new fork member', 'cue_indexes': [1],
        'matched_start_ms': 7000, 'matched_end_ms': 8000,
        'context_start_ms': 6500, 'context_end_ms': 8500,
        'source_media_timeline_offset_ms': 0})
    wrapper = native.build_independent_context_witnesses(fallback=_forbidden, source_media=source,
        output_dir=tmp_path / 'current', providers=('mai',),
        budget_receipt_path=state.budget_receipt, native_observation_cache_root=state.cache_root)
    assert wrapper.observe_cached_independent_witnesses(request) == []
    assert wrapper.observe_independent_witnesses(request)[0]['status'] == 'OBSERVED'
    after = json.loads(state.budget_receipt.read_bytes())
    assert after['budget']['attempt_count'] == 7
    assert after['budget']['pending_attempt_count'] == 1
    assert after['budget']['account_history']['incoming'] == before['budget']['account_history']['incoming']
    assert (incoming / 'native-audio-budget.json').read_bytes() == immutable
    assert calls == [('mai', 7000, 8000)]


def test_registered_source_parent_consumes_inherited_unavailable_cache_without_losing_history(tmp_path, monkeypatch):
    from src.autoslice.supplement_audio_budget import get_budget
    from src.autoslice.source_checkpoint_parent import consume_source_checkpoint_parent
    from tests.test_accept_source_endpoint_rebuild import _resumed_parent, _build, _files
    parent, checkpoint, _, padded, state = _resumed_parent(tmp_path, monkeypatch, unavailable=True)
    receipt = state.budget_receipt.read_bytes()
    original = _files(tmp_path)
    spec = _build(parent, checkpoint, tmp_path / 'rebuild', accept_source_endpoint=True)
    assert _files(tmp_path) == original
    out = tmp_path / 'rebuild' / spec['candidate_id']
    out.mkdir(parents=True)
    consumed = consume_source_checkpoint_parent(spec=spec, cid=spec['candidate_id'], padded=padded,
                                               out_root=out)
    assert consumed.native == state
    assert state.budget_receipt.read_bytes() == receipt
    assert get_budget(padded).snapshot() == json.loads(receipt)['budget']
    assert get_budget(padded).snapshot()['pending_attempt_count'] == 1
    assert _files(tmp_path) == original


@pytest.mark.parametrize('schema', ['v2', 'v3'])
def test_source_parent_qualifies_legacy_unregistered_range_hole_without_manual_inheritance(tmp_path, monkeypatch, schema):
    from src.autoslice.supplement_audio_budget import get_budget
    from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
    from src.autoslice.source_checkpoint_parent import consume_source_checkpoint_parent
    from tests.test_accept_source_endpoint_rebuild import _resumed_parent, _build, _files
    parent, checkpoint, _, padded, state = _resumed_parent(tmp_path, monkeypatch, schema=schema)
    sha = hashlib.sha256(padded.read_bytes()).hexdigest()
    resume_native_audio_budget(source_media=padded, source_media_sha256=sha, receipt_path=state.budget_receipt)
    budget = get_budget(padded)
    attempt = budget.consume('agy', 'Gemini 3.8 Flash (High)', 41000, 42340)
    budget.finish_attempt(attempt, status='OBSERVED')
    persist_native_audio_budget_receipt(source_media=padded, source_media_sha256=sha,
        receipt_path=state.budget_receipt)
    # A charged historical response with no retained acquisition closure. This
    # enters at SOURCE, which must perform qualification itself before cold read.
    raw = state.budget_receipt.read_bytes()
    spec = _build(parent, checkpoint, tmp_path / 'legacy-rebuild', accept_source_endpoint=True)
    out = Path(spec['output_root']) / spec['candidate_id']
    out.mkdir(parents=True)
    consumed = consume_source_checkpoint_parent(spec=spec, cid=spec['candidate_id'], padded=padded, out_root=out)
    assert consumed.native == state
    assert state.budget_receipt.read_bytes() == raw
    assert get_budget(padded).snapshot() == json.loads(raw)['budget']
    manifest = json.loads((state.cache_root / 'parent.json').read_bytes())
    key = f'gemini-range-acquisition/{sha[:20]}/41000-42340'
    assert manifest['unusable_caches'][key]['attempts'][0] in json.loads(raw)['budget']['attempts']
    after = _files(tmp_path)
    assert consume_source_checkpoint_parent(spec=spec, cid=spec['candidate_id'], padded=padded, out_root=out).native == state
    assert _files(tmp_path) == after


@pytest.mark.parametrize('provider', ['mai', 'moss'])
@pytest.mark.parametrize('lane', ['range', 'cue', 'factory', 'bad-envelope', 'symlink-envelope', 'missing-identity', 'changed-survivor'])
def test_ordinary_writer_missing_receipt_inherits_then_recollects_without_old_envelope_reuse(
    tmp_path, monkeypatch, provider, lane,
):
    from pathlib import Path
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'new missing-cache consumer member')
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    check, request = _range_requests(sha)
    old = tmp_path / 'old'
    calls = []
    _install_real_range_budget_writer(monkeypatch, calls)
    original = native.build_independent_context_witnesses(fallback=_forbidden, source_media=source,
        output_dir=old / 'independent-context', providers=(provider,),
        native_observation_cache_root=old, budget_receipt_path=old / 'native-audio-budget.json')
    row = original.observe_range_context(check_request=check, range_witness_request=request)[0]
    assert row['status'] == 'OBSERVED'
    Path(row['artifact_bindings']['provider_receipt_path']).unlink()
    before = {p: p.read_bytes() for p in old.rglob('*') if p.is_file()}
    runtime = tmp_path / 'runtime'
    monkeypatch.setenv('AUTOSLICE_BASE', str(runtime))
    start_budget(source)
    state = inherit_prepared_native_audio(cid='member', parent=old, source=source, runtime_root=runtime)
    budget_before = json.loads(state.budget_receipt.read_bytes())
    wrapper = native.build_independent_context_witnesses(fallback=_forbidden, source_media=source,
        output_dir=old / 'independent-context', providers=(provider,),
        native_observation_cache_root=state.cache_root, budget_receipt_path=state.budget_receipt)
    if lane == 'factory':
        real_builder = native.build_independent_context_witnesses
        _offline(monkeypatch)
        monkeypatch.setattr(native, 'build_independent_context_witnesses',
                            lambda **kw: real_builder(**{**kw, 'providers': (provider,)}))
        kw = _kwargs(tmp_path, source, state)
        kw.update(cid='member', out_root=old)
        wrapper = pipeline._build_entity_verification_context(**kw).verify_confusable_entity
    assert wrapper.observe_cached_independent_witnesses(request) == []
    if lane in {'bad-envelope', 'symlink-envelope', 'missing-identity', 'changed-survivor'}:
        if lane == 'changed-survivor':
            Path(row['artifact_bindings']['audio_clip_path']).write_bytes(b'drift after inheritance')
            with pytest.raises(ValueError, match='PREPARED_NATIVE_PARENT_DRIFT'):
                wrapper.observe_range_context(check_request=check, range_witness_request=request)
        elif lane == 'missing-identity':
            manifest_path = state.cache_root / 'parent.json'
            manifest = json.loads(manifest_path.read_bytes())
            next(iter(manifest['unusable_caches'].values()))['attempts'][0]['attempt_id'] = 999
            manifest_path.write_text(json.dumps(manifest))
            with pytest.raises(native._NativeAcquisitionIdentityMissing):
                wrapper.observe_range_context(check_request=check, range_witness_request=request)
        else:
            path = Path(row['artifact_bindings']['native_envelope_path'])
            if lane == 'bad-envelope':
                path.write_text('{}')
            else:
                path.unlink()
                path.symlink_to(source)
            damaged = wrapper.observe_range_context(check_request=check, range_witness_request=request)[0]
            assert damaged['status'] == 'UNCERTAIN'
            assert damaged['reason_code'] == 'NATIVE_RANGE_ENVELOPE_INVALID'
        assert len(calls) == 1
        assert json.loads(state.budget_receipt.read_bytes()) == budget_before
        return
    cold = wrapper.observe_range_context(check_request=check, range_witness_request=request,
                                         cache_only=True)[0]
    assert cold['status'] != 'OBSERVED' and len(calls) == 1
    assert json.loads(state.budget_receipt.read_bytes()) == budget_before
    if lane in {'range', 'factory'}:
        new = wrapper.observe_range_context(check_request=check, range_witness_request=request)[0]
    else:
        new = wrapper.observe_independent_witnesses(request)[0]
    assert new['status'] == 'OBSERVED'
    after = json.loads(state.budget_receipt.read_bytes())
    assert after['budget']['attempts'][:-1] == budget_before['budget']['attempts']
    assert after['budget']['attempt_count'] == 2
    assert len(calls) == 2
    # The factory may create its ordinary topic-resolution sidecar. Every
    # historical file and the entire native evidence namespace stay immutable.
    after_files = {p: p.read_bytes() for p in old.rglob('*') if p.is_file()}
    assert {p: after_files[p] for p in before} == before
    assert set(after_files) - set(before) == (
        {old / 'member.topic-resolution.json'} if lane == 'factory' else set())
    # The same ordinary consumer now reads the NEW complete cache, and cannot
    # silently fall back to the historical success after a cold restart.
    monkeypatch.setattr(native, 'build_native_foreign_witness', _forbidden)
    again = wrapper.observe_range_context(check_request=check, range_witness_request=request,
                                         cache_only=True)[0]
    assert again['status'] == 'OBSERVED'
    assert len(calls) == 2


# Ordinary range writer -> inheritance -> cold inventory and reacquisition.


@pytest.mark.parametrize('schema', ['v2', 'v3'])
@pytest.mark.parametrize('status', ['OBSERVED', 'TEXT_UNLOCATED'])
def test_join_qualifies_destination_writer_holes_before_cold_read(written, tmp_path, monkeypatch, schema, status):
    """The writer's output and its charged receipt need not share a root."""
    from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
    from src.autoslice.supplement_audio_budget import get_budget
    w = written
    if schema == 'v3':
        member = tmp_path / 'destination-member'
        member.mkdir()
        source, runtime, _, incoming, _ = fork = _fork(member, monkeypatch)
        state = _auto(fork)
        cid = 'candidate'
    else:
        state = inherit(w)
        source, runtime, cid = w.source, w.runtime, 'range-member'
        incoming = w.parent
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    before = json.loads(state.budget_receipt.read_bytes())['budget']
    # This is the real range writer shape: an output directory with a separate
    # canonical budget_receipt_path. Its OBSERVED acquisition remains in the
    # output, while only the canonical ledger contains the charged attempt.
    observer = _verifier(source, incoming)
    check = _check_request(source, '1\n00:00:04,000 --> 00:00:06,000\nspoken words\n\n')
    check.update(matched_start_ms=4000, matched_end_ms=6000,
                 context_start_ms=4000, context_end_ms=6000, source_media_timeline_offset_ms=0)
    request = _range_request(check, start=4000, end=6000)
    row = gemini.observe_gemini_range_context(verifier=observer, check_request=check,
        range_witness_request=request, budget_receipt_path=state.budget_receipt)[0]
    assert row['status'] == 'OBSERVED', row
    if status == 'TEXT_UNLOCATED':
        token = get_budget(source).consume(w.provider, w.model, 7000, 8000)
        get_budget(source).finish_attempt(token, status=status)
        persist_native_audio_budget_receipt(source_media=source, source_media_sha256=sha,
            receipt_path=state.budget_receipt)
    raw = state.budget_receipt.read_bytes()
    parent_raw = (incoming / 'native-audio-budget.json').read_bytes()
    with pytest.raises(ValueError, match='PREPARED_NATIVE_RANGE_ACQUISITION_MISSING'):
        recovery._parent_inputs(parent=state.cache_root, cid=cid, source=source, source_sha=sha)
    state, diagnostic = recovery.join_own_interrupted_account(state, cid=cid, out_root=incoming,
        source=source, runtime_root=runtime)
    assert diagnostic['status'] == 'JOINED'
    # These parents are prefixes, so qualification alone never rewrites or
    # re-signs the charged receipt. A fresh read consumes the saved declaration.
    assert state.budget_receipt.read_bytes() == raw
    assert (incoming / 'native-audio-budget.json').read_bytes() == parent_raw
    start_budget(source)
    parsed, _ = recovery._parent_inputs(parent=state.cache_root, cid=cid, source=source, source_sha=sha)
    assert parsed['budget']['attempt_count'] == before['attempt_count'] + (1 if status == 'OBSERVED' else 2)
    manifest = json.loads((state.cache_root / 'parent.json').read_bytes())
    key = f'gemini-range-acquisition/{sha[:20]}/4000-6000'
    attempt = next(a for a in parsed['budget']['attempts'] if a['start_ms'] == 4000)
    assert attempt in manifest['unusable_caches'][key]['attempts']
    assert manifest['unusable_caches'][key]['budget_sha256'] == 'sha256:' + hashlib.sha256(raw).hexdigest()
    # Repeat from disk: no caller-side registry preparation and no new billing.
    state, diagnostic = recovery.join_own_interrupted_account(state, cid=cid, out_root=incoming,
        source=source, runtime_root=runtime)
    assert diagnostic['status'] == 'JOINED'
    assert state.budget_receipt.read_bytes() == raw
    assert json.loads((state.cache_root / 'parent.json').read_bytes()) == manifest


def test_join_destination_registered_cache_loss_still_rejects(written):
    w = written
    state = inherit(w)
    bound = state.cache_root / w.acquisition.relative_to(w.parent)
    bound.unlink()
    original = (state.cache_root / 'parent.json').read_bytes()
    _, diagnostic = recovery.join_own_interrupted_account(state, cid='range-member', out_root=w.parent,
        source=w.source, runtime_root=w.runtime)
    assert diagnostic['status'] == 'NOT_JOINED'
    assert diagnostic['reason_code'] == 'PREPARED_NATIVE_PARENT_DRIFT'
    assert (state.cache_root / 'parent.json').read_bytes() == original


@pytest.mark.parametrize('entry', ['inherit', 'join', 'cold'])
@pytest.mark.parametrize('partial', [False, True])
@pytest.mark.parametrize('damage', ['missing_metadata', 'model', 'provider'])
def test_registered_range_metadata_never_becomes_a_new_hole(written, entry, partial, damage):
    w = written
    if partial:
        # The ordinary writer's partial closure is first qualified legitimately;
        # its surviving metadata is still a registered immutable acquisition.
        Path(w.metadata['paths']['raw_response']['path']).unlink()
    state = inherit(w)
    raw = state.budget_receipt.read_bytes()
    manifest_path = state.cache_root / 'parent.json'
    manifest = manifest_path.read_bytes()
    original = (w.parent / 'native-audio-budget.json').read_bytes()
    from src.autoslice.supplement_audio_budget import get_budget
    before = get_budget(w.source).snapshot()
    if damage == 'missing_metadata':
        w.acquisition.unlink()
    else:
        metadata = json.loads(w.acquisition.read_bytes())
        metadata[damage] = 'changed-registered-identity'
        metadata.pop('metadata_sha256')
        metadata['metadata_sha256'] = gemini._digest_json(metadata)
        w.acquisition.write_text(json.dumps(metadata))
    if entry == 'join':
        _, diagnostic = recovery.join_own_interrupted_account(state, cid='range-member', out_root=w.parent,
            source=w.source, runtime_root=w.runtime)
        assert diagnostic['status'] == 'NOT_JOINED'
        assert diagnostic['reason_code'].startswith(('PREPARED_NATIVE_RANGE_ACQUISITION',
                                                     'PREPARED_NATIVE_PARENT_DRIFT'))
    elif entry == 'inherit':
        with pytest.raises(ValueError, match='PREPARED_NATIVE_RANGE_ACQUISITION|PREPARED_NATIVE_PARENT_DRIFT'):
            recovery.inherit_prepared_native_audio(cid='range-member', parent=w.parent,
                source=w.source, runtime_root=w.runtime)
    else:
        # A complete canonical copy is independently byte-bound and remains
        # readable; an unusable acquisition still points to the original bytes.
        if partial:
            with pytest.raises(ValueError, match='PREPARED_NATIVE_RANGE_ACQUISITION|PREPARED_NATIVE_PARENT_DRIFT'):
                recovery._parent_inputs(parent=state.cache_root, cid='range-member',
                    source=w.source, source_sha=hashlib.sha256(w.source.read_bytes()).hexdigest())
        else:
            recovery._parent_inputs(parent=state.cache_root, cid='range-member',
                source=w.source, source_sha=hashlib.sha256(w.source.read_bytes()).hexdigest())
    assert get_budget(w.source).snapshot() == before
    assert state.budget_receipt.read_bytes() == raw
    assert manifest_path.read_bytes() == manifest
    assert (w.parent / 'native-audio-budget.json').read_bytes() == original


def test_active_fork_join_qualifies_new_canonical_range_without_losing_either_branch(written, tmp_path, monkeypatch):
    w = written
    member = tmp_path / 'fork-destination-member'
    member.mkdir()
    source, runtime, state, incoming, shared, _, incoming_raw = _active_fork_member(member, monkeypatch)
    observer = _verifier(source, incoming)
    check = _check_request(source, '1\n00:00:04,000 --> 00:00:06,000\nspoken words\n\n')
    check.update(matched_start_ms=4000, matched_end_ms=6000,
                 context_start_ms=4000, context_end_ms=6000, source_media_timeline_offset_ms=0)
    row = gemini.observe_gemini_range_context(verifier=observer, check_request=check,
        range_witness_request=_range_request(check, start=4000, end=6000),
        budget_receipt_path=state.budget_receipt)[0]
    assert row['status'] == 'OBSERVED', row
    raw = state.budget_receipt.read_bytes()
    before = json.loads(raw)['budget']
    start_budget(source)
    state = inherit_prepared_native_audio(cid='candidate', parent=incoming, source=source, runtime_root=runtime)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    receipt, _ = recovery._parent_inputs(parent=state.cache_root, cid='candidate', source=source, source_sha=sha)
    b = receipt['budget']
    assert b['attempt_count'] == before['attempt_count'] + json.loads(incoming_raw)['budget']['attempt_count'] - shared['budget']['attempt_count']
    assert b['total_audio_ms'] == before['total_audio_ms'] + json.loads(incoming_raw)['budget']['total_audio_ms'] - shared['budget']['total_audio_ms']
    manifest = json.loads((state.cache_root / 'parent.json').read_bytes())
    key = f'gemini-range-acquisition/{sha[:20]}/4000-6000'
    attempt = next(a for a in b['attempts'] if a['start_ms'] == 4000)
    assert attempt in manifest['unusable_caches'][key]['attempts']
    assert (state.cache_root / 'parents' / (hashlib.sha256(raw).hexdigest() + '.json')).read_bytes() == raw
    assert (incoming / 'native-audio-budget.json').read_bytes() == incoming_raw


def test_first_fork_remaps_previously_qualified_canonical_attempts(tmp_path, monkeypatch):
    from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
    from src.autoslice.supplement_audio_budget import get_budget
    source, runtime, canonical, incoming, _ = fork = _fork(tmp_path, monkeypatch)
    resume_native_audio_budget(source_media=source,
        source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), receipt_path=canonical.budget_receipt)
    token = get_budget(source).consume('agy', 'Gemini 3.8 Flash (High)', 41000, 42340)
    get_budget(source).finish_attempt(token, status='OBSERVED')
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    persist_native_audio_budget_receipt(source_media=source, source_media_sha256=sha,
        receipt_path=canonical.budget_receipt)
    inherit_prepared_native_audio(cid='candidate', parent=canonical.cache_root, source=source, runtime_root=runtime)
    raw = canonical.budget_receipt.read_bytes()
    state = _auto(fork)
    receipt, _ = recovery._parent_inputs(parent=state.cache_root, cid='candidate', source=source, source_sha=sha)
    key = f'gemini-range-acquisition/{sha[:20]}/41000-42340'
    record = json.loads((state.cache_root / 'parent.json').read_bytes())['unusable_caches'][key]
    assert record['attempts'] == [a for a in receipt['budget']['attempts'] if a['start_ms'] == 41000]
    assert record['attempts'][0]['history_branch_sha256'] == json.loads(raw)['receipt_sha256']


@pytest.fixture(params=['agy', 'gemini_api'])
def written(request, tmp_path, monkeypatch):
    provider = request.param
    model = 'test-agy-model' if provider == 'agy' else 'test-api-model'
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    source = runtime / 'source.mp4'
    source.write_bytes(b'new range inheritance member')
    parent = runtime / 'old-output'
    parent.mkdir()
    monkeypatch.setenv('AUTOSLICE_BASE', str(runtime))
    monkeypatch.setattr(gemini, '_entity_api_model', lambda: 'test-api-model')
    monkeypatch.setattr(gemini, '_extract_exact_mp3',
        lambda src, out, start, end: out.write_bytes(b'original mp3'))
    def crop(**kw):
        kw['audio_path'].write_bytes(b'original black-frame mp4')
        return True, ''
    monkeypatch.setattr(gemini, '_crop_black_frame_audio', crop)
    calls = []
    def observe(**kw):
        calls.append(provider)
        kw['on_provider_dispatch'](provider, model)
        job = kw['job_dir']
        prompt = job / 'prompt.md'
        from src.autoslice import context_assisted_audio as assisted
        is_assisted = kw['request']['schema_version'] == assisted.REQUEST_SCHEMA
        prompt.write_text(assisted.prompt(kw['request']) if is_assisted
                          else gemini.range_transcript_prompt(kw['request']))
        if provider == 'gemini_api':
            (job / 'input.gemini-api.mp3').write_bytes(b'api mp3')
        raw = {'status': 'OBSERVED', 'target_audible': True, 'full_transcript': 'whole block',
            'segments': [{'segment': 'complete_block', 'start_ms': 0,
                'end_ms': kw['request']['target_audio_end_ms'], 'transcript': 'whole block'}]}
        if is_assisted:
            raw.update(schema_version=assisted.assisted_response_contract().version,
                       heard='whole block', inferred=[], uncertain=[])
        response = job / 'verdict.raw.json'
        response.write_text(json.dumps(raw))
        return SimpleNamespace(observed=raw, provider=provider, model=model,
            prompt_path=prompt, response_path=response, provider_failures=[])
    monkeypatch.setattr(gemini, '_observe_entity_audio', observe)
    verifier = _verifier(source, parent)
    srt = '1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n'
    check = _check_request(source, srt)
    broad = _range_request(check)
    start_budget(source)
    row = gemini.observe_gemini_range_context(verifier=verifier, check_request=check,
        range_witness_request=broad, budget_receipt_path=parent / 'native-audio-budget.json')[0]
    assert row['status'] == 'OBSERVED'
    acquisition = next(parent.glob('gemini-range-acquisition/*/*/*/*/acquisition.json'))
    return SimpleNamespace(source=source, parent=parent, acquisition=acquisition,
        metadata=json.loads(acquisition.read_bytes()), verifier=verifier, check=check,
        broad=broad, calls=calls, runtime=runtime, provider=provider, model=model,
        row=row)


def inherit(w):
    start_budget(w.source)
    return recovery.inherit_prepared_native_audio(cid='range-member', parent=w.parent,
        source=w.source, runtime_root=w.runtime)


@pytest.mark.parametrize('lost', ['acquisition', 'whole-root', 'prompt', 'execution',
    'raw_response', 'provider_stdout', 'audio_source', 'audio-source-writer-error', 'rendered_audio_clip'])
def test_range_inheritance_missing_artifacts_reaches_ordinary_reacquisition(written, monkeypatch, lost):
    w = written
    if lost == 'whole-root':
        import shutil
        shutil.rmtree(w.acquisition.parents[2])
    else:
        role = 'audio_source' if lost == 'audio-source-writer-error' else lost
        path = w.acquisition if lost == 'acquisition' else Path(w.metadata['paths'][role]['path'])
        path.unlink()
    # Bound-media recovery is attempted, but this member cannot reproduce its
    # original media bytes; metadata and provider responses are never rebuilt.
    from src.autoslice import native_foreign_witness, entity_audio_verifier
    monkeypatch.setattr(native_foreign_witness, '_extract_exact_mp3',
        lambda src, out, start, end: out.write_bytes(b'different codec'))
    if lost == 'audio-source-writer-error':
        def extraction_failed(*args):
            raise RuntimeError('WITNESS_AUDIO_EXTRACTION_FAILED')
        monkeypatch.setattr(native_foreign_witness, '_extract_exact_mp3', extraction_failed)
    monkeypatch.setattr(entity_audio_verifier, '_crop_black_frame_audio',
        lambda **kw: (kw['audio_path'].write_bytes(b'different codec') is not None, ''))
    original = (w.parent / 'native-audio-budget.json').read_bytes()
    survivors = {p: p.read_bytes() for p in w.parent.rglob('*') if p.is_file()}
    state = inherit(w)
    assert state.budget_receipt.read_bytes() == original
    manifest = json.loads((state.cache_root / 'parent.json').read_bytes())
    assert len(manifest['unusable_caches']) == 1
    record = next(iter(manifest['unusable_caches'].values()))
    assert record['attempts'] == json.loads(original)['budget']['attempts']
    receipt, inventory = recovery._parent_inputs(parent=state.cache_root, cid='range-member',
        source=w.source, source_sha=hashlib.sha256(w.source.read_bytes()).hexdigest())
    assert receipt == json.loads(original)
    assert len(inventory) == 1
    # Reuse the original output root and the canonical receipt, as the factory
    # does. A present partial acquisition is skipped only under the bound
    # inheritance declaration, and ordinary dispatch appends a fresh attempt.
    row = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=w.check,
        range_witness_request=w.broad, budget_receipt_path=state.budget_receipt)[0]
    assert row['status'] == 'OBSERVED' and not row.get('served_from_cache')
    after = json.loads(state.budget_receipt.read_bytes())['budget']
    assert after['attempts'][:1] == json.loads(original)['budget']['attempts']
    assert after['attempt_count'] == 2 and len(w.calls) == 2
    assert all(p.read_bytes() == raw for p, raw in survivors.items())


@pytest.mark.parametrize('damage', ['prompt-drift', 'bad-acquisition', 'symlink', 'missing-budget', 'unknown-contract',
    'missing-prompt-bad-execution', 'missing-execution-bad-response'])
def test_range_cache_inheritance_preserves_existing_damage_rejection(written, damage):
    w = written
    if damage == 'bad-acquisition':
        w.acquisition.write_text('{broken')
    elif damage == 'missing-budget':
        (w.parent / 'native-audio-budget.json').unlink()
    elif damage == 'unknown-contract':
        metadata = dict(w.metadata, prompt_contract='unknown-contract')
        metadata.pop('metadata_sha256')
        metadata['metadata_sha256'] = gemini._digest_json(metadata)
        w.acquisition.write_text(json.dumps(metadata))
    elif damage == 'symlink':
        prompt = Path(w.metadata['paths']['prompt']['path'])
        prompt.unlink()
        prompt.symlink_to(w.source)
    elif damage.startswith('missing-'):
        missing, changed = (('prompt', 'execution') if 'bad-execution' in damage
                            else ('execution', 'raw_response'))
        Path(w.metadata['paths'][missing]['path']).unlink()
        Path(w.metadata['paths'][changed]['path']).write_text('{broken')
    else:
        Path(w.metadata['paths']['prompt']['path']).write_text('different prompt')
    with pytest.raises((ValueError, OSError)):
        inherit(w)


def test_unregistered_partial_range_still_rejects_and_valid_cache_still_reuses(written):
    w = written
    state = inherit(w)
    assert (state.cache_root / w.acquisition.relative_to(w.parent)).read_bytes() == w.acquisition.read_bytes()
    assert not json.loads((state.cache_root / 'parent.json').read_bytes()).get('unusable_caches')
    # The cold observer addresses the canonical account, not the old output.
    w.verifier.output_dir = state.cache_root
    row = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=w.check,
        range_witness_request=w.broad, budget_receipt_path=state.budget_receipt)[0]
    assert row['status'] == 'OBSERVED' and row['served_from_cache']
    assert len(w.calls) == 1
    Path(w.metadata['paths']['prompt']['path']).unlink()
    bad = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=w.check,
        range_witness_request=w.broad, budget_receipt_path=state.budget_receipt)[0]
    assert bad['reason_code'] == 'GEMINI_RANGE_CACHE_INVALID' and len(w.calls) == 1


def test_range_missing_cache_incoming_v3_keeps_branch_identity_and_pending(written, tmp_path, monkeypatch):
    w = written
    member = tmp_path / 'fork-member'
    member.mkdir()
    fork = _fork(member, monkeypatch)
    source, runtime, canonical, incoming, _ = fork
    verifier = _verifier(source, incoming)
    check = _check_request(source, '1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n')
    broad = _range_request(check)
    row = gemini.observe_gemini_range_context(verifier=verifier, check_request=check,
        range_witness_request=broad, budget_receipt_path=incoming / 'native-audio-budget.json')[0]
    assert row['status'] == 'OBSERVED'
    acquisition = next(incoming.glob('gemini-range-acquisition/*/*/*/*/acquisition.json'))
    metadata = json.loads(acquisition.read_bytes())
    Path(metadata['paths']['prompt']['path']).unlink()
    before = (incoming / 'native-audio-budget.json').read_bytes()
    start_budget(source)
    state = _auto(fork)
    joined = json.loads(state.budget_receipt.read_bytes())
    assert joined['schema_version'] == 'native-audio-budget.v3'
    assert joined['budget']['account_history']['incoming'] == json.loads(before)
    assert joined['budget']['pending_attempt_count'] == 1
    manifest = json.loads((state.cache_root / 'parent.json').read_bytes())
    record = manifest['unusable_caches'][str(Path('gemini-range-acquisition') /
        hashlib.sha256(source.read_bytes()).hexdigest()[:20] /
        f"{metadata['source_target_ms'][0]}-{metadata['source_target_ms'][1]}")]
    assert record['attempts'][0] in joined['budget']['attempts']
    assert record['attempts'][0]['history_branch_sha256'] == json.loads(before)['receipt_sha256']
    recovery._parent_inputs(parent=state.cache_root, cid='candidate', source=source,
        source_sha=hashlib.sha256(source.read_bytes()).hexdigest())
    again = gemini.observe_gemini_range_context(verifier=verifier, check_request=check,
        range_witness_request=broad, budget_receipt_path=state.budget_receipt)[0]
    assert again['status'] == 'OBSERVED' and not again.get('served_from_cache')
    assert (incoming / 'native-audio-budget.json').read_bytes() == before
    assert json.loads(state.budget_receipt.read_bytes())['budget']['pending_attempt_count'] == 1


@pytest.mark.parametrize('role', ['audio_source', 'rendered_audio_clip'])
@pytest.mark.parametrize('route', ['copy', 'rebuild'])
def test_range_inheritance_recovers_bound_media_into_inventory(written, monkeypatch, role, route):
    w = written
    original = Path(w.metadata['paths'][role]['path'])
    raw = original.read_bytes()
    original.unlink()
    if route == 'copy':
        copy = w.runtime / 'another-run' / original.name
        copy.parent.mkdir(parents=True)
        copy.write_bytes(raw)
    else:
        from src.autoslice import native_foreign_witness, entity_audio_verifier
        monkeypatch.setattr(native_foreign_witness, '_extract_exact_mp3',
            lambda src, out, start, end: out.write_bytes(raw))
        def crop(**kw):
            kw['audio_path'].write_bytes(raw)
            return True, ''
        monkeypatch.setattr(entity_audio_verifier, '_crop_black_frame_audio', crop)
    before = (w.parent / 'native-audio-budget.json').read_bytes()
    state = inherit(w)
    assert state.budget_receipt.read_bytes() == before
    assert (state.cache_root / original.relative_to(w.parent)).read_bytes() == raw
    assert not json.loads((state.cache_root / 'parent.json').read_bytes()).get('unusable_caches')
    assert not original.exists()
    row = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=w.check,
        range_witness_request=w.broad, budget_receipt_path=state.budget_receipt)[0]
    assert row['status'] == 'OBSERVED' and row['served_from_cache'] and len(w.calls) == 1


def test_inherited_range_cache_loss_is_a_bound_reason_after_soft_pressure(written):
    w = written
    # Generate all three historical attempts through the real budget/cache
    # writer. Missing metadata forces a cache miss without altering any row.
    for _ in range(2):
        for path in w.parent.glob('gemini-range-acquisition/*/*/*/*/acquisition.json'):
            path.unlink()
        row = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=w.check,
            range_witness_request=w.broad, budget_receipt_path=w.parent / 'native-audio-budget.json')[0]
        assert row['status'] == 'OBSERVED'
    for path in w.parent.glob('gemini-range-acquisition/*/*/*/*/acquisition.json'):
        path.unlink()
    before = json.loads((w.parent / 'native-audio-budget.json').read_bytes())['budget']
    assert before['attempt_count'] == 3
    state = inherit(w)
    row = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=w.check,
        range_witness_request=w.broad, budget_receipt_path=state.budget_receipt)[0]
    assert row['status'] == 'OBSERVED' and not row.get('served_from_cache')
    after = json.loads(state.budget_receipt.read_bytes())['budget']
    assert after['attempts'][:3] == before['attempts'] and after['attempt_count'] == 4
    # A later successful dispatch was never declared unavailable. The old
    # declaration must not become an unlimited reason for further requests.
    for path in w.parent.glob('gemini-range-acquisition/*/*/*/*/acquisition.json'):
        path.unlink()
    refused = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=w.check,
        range_witness_request=w.broad, budget_receipt_path=state.budget_receipt)[0]
    assert refused['reason_code'] == gemini.SOFT_PRESSURE_NO_NEW_REASON
    assert json.loads(state.budget_receipt.read_bytes())['budget']['attempt_count'] == 4


@pytest.mark.parametrize('missing_prompt', [False, True])
def test_assisted_range_writer_inherits_without_promoting_it_to_blind_evidence(written, missing_prompt):
    from src.autoslice import context_assisted_audio as assisted
    w = written
    check = {**w.check, 'context_only_range_acoustic_observations': [w.row]}
    ids = assisted.validate_prior_blind(check)
    assistance = assisted.build_assistance(check,
        {'context_refs': ['cue:1'], 'question': 'What was audible?'}, blind_observation_ids=ids)
    row = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=check,
        range_witness_request=w.broad, budget_receipt_path=w.parent / 'native-audio-budget.json',
        context_assistance=assistance)[0]
    assert row['status'] == 'OBSERVED' and row['candidate_blind'] is False
    paths = list(w.parent.glob('gemini-range-acquisition/*/*/*/*/acquisition.json'))
    path = next(p for p in paths if json.loads(p.read_bytes())['candidate_blind'] is False)
    raw = path.read_bytes()
    metadata = json.loads(raw)
    if missing_prompt:
        Path(metadata['paths']['prompt']['path']).unlink()
    before = (w.parent / 'native-audio-budget.json').read_bytes()
    state = inherit(w)
    assert state.budget_receipt.read_bytes() == before and path.read_bytes() == raw
    recovery._parent_inputs(parent=state.cache_root, cid='range-member', source=w.source,
        source_sha=hashlib.sha256(w.source.read_bytes()).hexdigest())
    blind = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=w.check,
        range_witness_request=w.broad, budget_receipt_path=state.budget_receipt)[0]
    assert blind['candidate_blind'] is True and blind['served_from_cache']
    again = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=check,
        range_witness_request=w.broad, budget_receipt_path=state.budget_receipt,
        context_assistance=assistance)[0]
    assert again['status'] == 'OBSERVED' and again['candidate_blind'] is False
    assert again['independent_witness'] is False
    assert bool(again.get('served_from_cache')) is (not missing_prompt)
    assert len(w.calls) == (3 if missing_prompt else 2)


@pytest.mark.parametrize('damage', ['survivor', 'identity'])
def test_inherited_range_missing_metadata_does_not_hide_later_damage(written, damage):
    w = written
    w.acquisition.unlink()
    state = inherit(w)
    if damage == 'survivor':
        Path(w.metadata['paths']['execution']['path']).write_text('changed survivor')
    else:
        path = state.cache_root / 'parent.json'
        manifest = json.loads(path.read_bytes())
        next(iter(manifest['unusable_caches'].values()))['attempts'][0]['attempt_id'] = 999
        path.write_text(json.dumps(manifest))
    row = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=w.check,
        range_witness_request=w.broad, budget_receipt_path=state.budget_receipt)[0]
    assert row['status'] != 'OBSERVED' and len(w.calls) == 1
    assert json.loads(state.budget_receipt.read_bytes())['budget']['attempt_count'] == 1


def test_other_model_pre_http_acquisition_without_dispatch_is_not_completed_history(written, monkeypatch):
    w = written
    # Ordinary writer, a newly configured route, and a real pre-dispatch
    # failure: its immutable UNCERTAIN metadata has no charged attempt.
    w.verifier.model = 'new-agy-route'
    monkeypatch.setattr(gemini, '_entity_api_model', lambda: 'new-api-route')
    def no_dispatch(**kw):
        return SimpleNamespace(observed=None, provider='agy', model='new-agy-route',
            provider_failures=[{'category': 'PROVIDER_UNAVAILABLE_BEFORE_DISPATCH'}])
    monkeypatch.setattr(gemini, '_observe_entity_audio', no_dispatch)
    row = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=w.check,
        range_witness_request=w.broad, budget_receipt_path=w.parent / 'native-audio-budget.json')[0]
    assert row['status'] == 'UNCERTAIN'
    before = (w.parent / 'native-audio-budget.json').read_bytes()
    assert json.loads(before)['budget']['attempt_count'] == 1
    extra = next(p for p in w.parent.glob('gemini-range-acquisition/*/*/*/*/acquisition.json')
                 if json.loads(p.read_bytes())['model'] == 'new-agy-route')
    extra_raw = extra.read_bytes()
    state = inherit(w)
    assert state.budget_receipt.read_bytes() == before
    assert extra.read_bytes() == extra_raw
    assert not (state.cache_root / extra.relative_to(w.parent)).exists()
    assert not json.loads((state.cache_root / 'parent.json').read_bytes()).get('unusable_caches')


@pytest.mark.parametrize('which', ['reacquired', 'complete-sibling'])
def test_old_range_unavailability_cannot_excuse_a_different_acquisition(written, which):
    w = written
    if which == 'reacquired':
        Path(w.metadata['paths']['prompt']['path']).unlink()
        state = inherit(w)
        new = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=w.check,
            range_witness_request=w.broad, budget_receipt_path=state.budget_receipt)[0]
        assert new['status'] == 'OBSERVED'
        damaged = next(p for p in w.parent.glob('gemini-range-acquisition/*/*/*/*/acquisition.json')
                       if p != w.acquisition)
    else:
        from src.autoslice import context_assisted_audio as assisted
        check = {**w.check, 'context_only_range_acoustic_observations': [w.row]}
        assistance = assisted.build_assistance(check,
            {'context_refs': ['cue:1'], 'question': 'What was audible?'},
            blind_observation_ids=assisted.validate_prior_blind(check))
        new = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=check,
            range_witness_request=w.broad, budget_receipt_path=w.parent / 'native-audio-budget.json',
            context_assistance=assistance)[0]
        assert new['status'] == 'OBSERVED'
        partial = next(p for p in w.parent.glob('gemini-range-acquisition/*/*/*/*/acquisition.json')
                       if json.loads(p.read_bytes())['candidate_blind'] is False)
        Path(json.loads(partial.read_bytes())['paths']['prompt']['path']).unlink()
        state = inherit(w)
        damaged = w.acquisition
    Path(json.loads(damaged.read_bytes())['paths']['prompt']['path']).unlink()
    before = json.loads(state.budget_receipt.read_bytes())['budget']
    count = len(w.calls)
    rejected = gemini.observe_gemini_range_context(verifier=w.verifier, check_request=w.check,
        range_witness_request=w.broad, budget_receipt_path=state.budget_receipt)[0]
    assert rejected['reason_code'] == 'GEMINI_RANGE_CACHE_INVALID'
    assert len(w.calls) == count
    assert json.loads(state.budget_receipt.read_bytes())['budget']['attempts'] == before['attempts']
