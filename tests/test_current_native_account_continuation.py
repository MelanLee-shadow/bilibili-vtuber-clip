"""Offline account continuity: real receipts/readers, no model or media processing."""
import ast
import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice import entity_audio_verifier as audio, native_context_witness as native
from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
from src.autoslice.prepared_native_audio_recovery import inherit_prepared_native_audio
from src.autoslice.supplement_audio_budget import ensure_budget
from tests.test_producer_relisten_seam_wiring import _adapters
from tests import test_producer_current_text_auto_resume as auto


def _forbidden(*args, **kwargs):
    raise AssertionError("OFFLINE_PROVIDER_IO_FORBIDDEN")


def _offline(monkeypatch):
    # Keep the real independent wrapper and its receipt reader; disable provider
    # construction and every LLM seam before entering the producer factory.
    real_independent = native.build_independent_context_witnesses
    monkeypatch.setattr(native, "build_independent_context_witnesses",
                        lambda **kw: real_independent(**{**kw, "providers": ()}))
    monkeypatch.setattr(audio, "build_local_audio_entity_verifier", lambda **kw: _forbidden)
    monkeypatch.setattr(pipeline, "witness_audio_locally_resolvable", lambda *a, **kw: True)
    monkeypatch.setattr(pipeline, "_ensure_local_native_audio_budget", lambda *a: None)
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", lambda: _forbidden)


def _account(tmp_path, monkeypatch, source=None):
    source = source or tmp_path / "source.mp4"
    if not source.exists():
        source.write_bytes(b"synthetic-source")
    parent = tmp_path / "parent-account"
    parent.mkdir()
    budget = ensure_budget(source)
    budget.consume("agy", "synthetic-native-model", 10, 20)
    # An unresolved real dispatch is deliberately left charged and unsealed.
    persist_native_audio_budget_receipt(source_media=source,
        source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        receipt_path=parent / "native-audio-budget.json")
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    state = inherit_prepared_native_audio(cid="new-member", parent=parent,
        source=source, runtime_root=tmp_path / "runtime")
    return source, state


def _kwargs(tmp_path, source, state):
    out = tmp_path / "new-output"
    out.mkdir(exist_ok=True)
    return dict(spec={}, padded=source, padded_dur=100, host="localhost",
                text_override_path=None, cid="new-member", out_root=out,
                srt_text="1\n00:00:00,000 --> 00:00:00,050\n原话\n",
                authoritative_chat=[], adapters=_adapters(), native_audio_state=state)


def _members():
    tree = ast.parse(Path(pipeline.__file__).read_text())
    found = []
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            calls = sorted((n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "_build_entity_verification_context"),
                key=lambda n: n.lineno)
            found.extend((f"{fn.name}:{i}", call) for i, call in enumerate(calls))
    return found


MEMBERS = [
    "build_prepared_exact_final_reviewer:0",
    "_run_current_text_continuation:0",
    "_run_current_text_continuation:1",
    "_resume_pending_source_finalization:0",
    "_automatic_current_source_stage_checkpoint:0",
    "run_text_pipeline:0",
]


@pytest.mark.parametrize("member", MEMBERS)
@pytest.mark.parametrize("drop_identity", [False, True])
def test_every_constructor_edge_preserves_identity_or_fails_at_construction(
    tmp_path, monkeypatch, member, drop_identity,
):
    """Evaluate each actual call expression, including nested prepared recovery.

    Dropping its keyword simulates a newly missed edge: the constructor must
    reject before the provider/cache consumer can guess another namespace.
    Ordinary entry/recovery flows are exercised separately below.
    """
    _offline(monkeypatch)
    source, state = _account(tmp_path, monkeypatch)
    kw = _kwargs(tmp_path, source, state)
    receipt_before = state.budget_receipt.read_bytes()
    members = dict(_members())
    assert list(members) == MEMBERS  # New call sites must join the member test.
    call = copy.deepcopy(members[member])
    if drop_identity:
        call.keywords = [k for k in call.keywords if k.arg != "native_audio_state"]
    roots = (tmp_path / "old-witnesses",)
    current = kw['srt_text']
    subtitle = tmp_path / "completed.srt"
    subtitle.write_text(current)
    namespace = {**pipeline.__dict__, **kw, "current": current,
        "native_audio_state": state, "witness_cache_roots": roots,
        "continuation": SimpleNamespace(native_audio_state=state, witness_cache_roots=roots),
        "draft": SimpleNamespace(srt_text=current), "snapshot": {"srt_text": current},
        "completed_record": {"subtitle_path": str(subtitle)},
        "ffprobe_duration_ms": lambda _: 100,
        "_continuation_chat_from_context": lambda _: (),
        "spec": {"clip_context": {}},
    }
    seen = []
    def construct(**kwargs):
        seen.append(kwargs)
        return pipeline._build_entity_verification_context(**kwargs)
    namespace['_build_entity_verification_context'] = construct
    expression = compile(ast.Expression(call), str(pipeline.__file__), "eval")
    if drop_identity:
        with pytest.raises(pipeline.NativeAudioAccountContinuationError) as exc:
            eval(expression, namespace)
        assert exc.value.reason_code == "CURRENT_NATIVE_AUDIO_ACCOUNT_IDENTITY_MISSING"
    else:
        context = eval(expression, namespace)
        assert seen[0]['native_audio_state'] is state
        read = context.verify_confusable_entity.relisten_budget_state()
        assert read['receipt_path'] == str(state.budget_receipt)
        assert read['receipt']['budget']['pending_attempt_count'] == 1
        assert 'unstarted_owner_state' not in read
    assert state.budget_receipt.read_bytes() == receipt_before
    assert not (kw['out_root'] / 'native-audio-budget.json').exists()


def test_revision_zero_is_still_the_unstarted_owner_control(tmp_path, monkeypatch):
    _offline(monkeypatch)
    source = tmp_path / "new-source.mp4"
    source.write_bytes(b"never-dispatched")
    assert ensure_budget(source).snapshot()['revision'] == 0
    read = pipeline._build_entity_verification_context(
        **_kwargs(tmp_path, source, None)).verify_confusable_entity.relisten_budget_state()
    assert read['receipt'] is None
    assert read['unstarted_owner_state']['status'] == 'NO_NATIVE_RECEIPT'
    assert read['unstarted_owner_state']['memory_snapshot']['revision'] == 0


@pytest.mark.parametrize('retained_form', ['completed', 'partial'])
@pytest.mark.parametrize('explicit', [False, True])
def test_producer_entry_binds_retained_account_once_into_current_object(
    tmp_path, monkeypatch, retained_form, explicit,
):
    """Enter via the ordinary retained input boundary, not a repaired verifier."""
    _offline(monkeypatch)
    c = auto._checkpoint(tmp_path)
    source, state = _account(tmp_path, monkeypatch, c['padded'])
    auto._stub_current_validation(monkeypatch)
    monkeypatch.setattr(pipeline, '_collect_timeline_chat', lambda *a: ([], []))
    monkeypatch.setattr(pipeline, '_transcribe_draft',
                        lambda **kw: auto._fake_draft(kw['retained_srt_text']))
    from src.autoslice import retained_reviewed_draft
    roots = (tmp_path / 'original-witness-cache',)
    retained = ({'srt_text': c['srt'], 'retained_native': state,
                 'retained_native_parent_output_root': str(roots[0])}
                if retained_form == 'completed' else
                {'srt_text': c['srt'], 'partial_evidence': {
                    'native': state, 'proof': {'original_output_root': str(roots[0])}}})
    # The real inheritance constructor above produces the account; isolate the
    # already independently tested retained text/owner validation from this edge.
    monkeypatch.setattr(retained_reviewed_draft, 'load_retained_reviewed_draft', lambda **kw: retained)
    monkeypatch.setattr(retained_reviewed_draft, 'validate_pending_entity_checkpoint', lambda *a: None)
    snapshot = pipeline._automatic_current_checkpoint_snapshot(**{k: auto._pipeline_kwargs(c)[k]
        for k in ('spec', 'durations', 'padded', 'padded_dur', 'cid', 'out_root')})
    # The entry's delivery-lane selector reads this existing v2 marker. Keep
    # the preflight snapshot byte/hash pair consistent while isolating the
    # unrelated retained text validators in this account-edge test.
    authority = snapshot['chat_authority']
    authority['final_review_audit']['schema_version'] = pipeline.FINAL_REVIEW_SCHEMA_VERSION
    c['authority_path'].write_text(json.dumps(authority))
    snapshot['chat_authority_sha256'] = hashlib.sha256(c['authority_path'].read_bytes()).hexdigest()
    monkeypatch.setattr(pipeline, '_automatic_current_checkpoint_snapshot', lambda **kw: snapshot)
    seen = []
    monkeypatch.setattr(pipeline, '_run_current_text_continuation',
                        lambda **kw: seen.append(kw['continuation']) or 'continued')
    kw = auto._pipeline_kwargs(c)
    kw['spec'] = {**kw['spec'], 'retained_current_text': {}, 'human_truth_mode': 'withheld'}
    if explicit:
        kw['continuation'] = pipeline.CurrentTextContinuation(
            current_srt_path=c['out_root'] / 'padded.fresh.srt',
            chat_authority_path=c['authority_path'], clip_context_path=c['context_path'],
            padded_sha256='sha256:' + hashlib.sha256(source.read_bytes()).hexdigest(),
            source_media_sha256s=('sha256:' + c['source_sha'],), spans=[], transcriber=_forbidden,
            native_audio_state=replace(state, history_cache_roots=(tmp_path / 'earlier-history',)))
    assert pipeline.run_text_pipeline(**kw) == 'continued'
    assert len(seen) == 1
    assert seen[0].native_audio_state is state
    assert seen[0].witness_cache_roots == roots


def test_explicit_current_recovery_and_final_consumer_read_original_owner(tmp_path, monkeypatch):
    from tests.test_producer_current_text_continuation import (
        test_current_continuation_consumes_caption_display_canon_before_fresh_review as run_case)
    _offline(monkeypatch)
    run = pipeline.run_text_pipeline
    construct = pipeline._build_entity_verification_context
    seen, identity = [], []
    def capture(**kw):
        assert kw['native_audio_state'] is identity[0]
        seen.append(kw)
        result = construct(**kw)
        read = result.verify_confusable_entity.relisten_budget_state()
        assert read['receipt_path'] == str(identity[0].budget_receipt)
        assert read['receipt']['budget']['pending_attempt_count'] == 1
        return result
    monkeypatch.setattr(pipeline, '_build_entity_verification_context', capture)
    def enter(**kw):
        _, state = _account(tmp_path, monkeypatch, kw['padded'])
        identity.append(state)
        kw['continuation'] = replace(kw['continuation'], verify_confusable_entity=None,
            native_audio_state=state, witness_cache_roots=(tmp_path / 'history-witness',))
        return run(**kw)
    monkeypatch.setattr(pipeline, 'run_text_pipeline', enter)
    # Reuse the existing whole CURRENT flow and all its authority/grid/display
    # assertions; only replace the injected verifier with the real composition.
    run_case(tmp_path, monkeypatch, gate_failure=None, tail_gap_ms=0)
    assert len(seen) == 2
    assert all(kw['witness_cache_roots'] == (tmp_path / 'history-witness',) for kw in seen)


def test_real_source_parent_entry_passes_loaded_native_identity(tmp_path, monkeypatch):
    from tests.test_source_checkpoint_parent import parent_fixture
    from src.autoslice import retained_source_input
    construct = pipeline._build_entity_verification_context
    c, spec, output = parent_fixture(tmp_path, monkeypatch)
    output.mkdir(parents=True)
    _offline(monkeypatch)
    load = retained_source_input.load_retained_source_input
    loaded, consumed = [], []
    def load_once(**kw):
        value = load(**kw)
        if value is not None:
            loaded.append(value.native)
        return value
    def consume(**kw):
        assert kw['native_audio_state'] is loaded[0]
        consumed.append(kw['native_audio_state'])
        result = construct(**kw)
        assert result.verify_confusable_entity.relisten_budget_state()['receipt'] is not None
        return result
    monkeypatch.setattr(retained_source_input, 'load_retained_source_input', load_once)
    monkeypatch.setattr(pipeline, '_build_entity_verification_context', consume)
    adapters = replace(_adapters(), profile_asset_file=lambda name:
        auto._pipeline_kwargs(c)['adapters'].profile_asset_file(name)
        if name == 'subtitle_truth_ledger' else tmp_path / name)
    result = pipeline.run_text_pipeline(**{**auto._pipeline_kwargs(c, adapters=adapters),
        'spec': spec, 'out_root': output})
    assert result.srt_text == c['srt']
    assert consumed and all(s is loaded[0] for s in consumed)
    assert not (output / 'native-audio-budget.json').exists()


def test_pending_source_recursion_keeps_the_identical_account(tmp_path, monkeypatch):
    construct = pipeline._build_entity_verification_context
    c = auto._checkpoint(tmp_path)
    auto._interrupt_partial_native_emitter(c, monkeypatch)
    auto._stub_source_stage_reconstruction(monkeypatch, c)
    _offline(monkeypatch)
    _, state = _account(tmp_path, monkeypatch, c['padded'])
    consumed = []
    def consume(**kw):
        assert kw['native_audio_state'] is state
        consumed.append(kw['native_audio_state'])
        context = construct(**kw)
        assert context.verify_confusable_entity.relisten_budget_state()['receipt'] is not None
        return context
    monkeypatch.setattr(pipeline, '_build_entity_verification_context', consume)
    monkeypatch.setattr(pipeline, '_run_final_review', _forbidden)
    def finalize(**kw):
        assert kw['srt_text'] == c['srt'] == kw['source_language_witness_srt']
        (c['out_root'] / 'padded.fresh.srt').write_text(kw['srt_text'])
        c['authority_path'].write_text(json.dumps(kw['chat_authority_audit']))
        auto._make_source_stage_checkpoint(c)
        authority = json.loads(c['authority_path'].read_bytes())
        authority['final_output_srt_sha256'] = hashlib.sha256(c['srt'].encode()).hexdigest()
        c['authority_path'].write_text(json.dumps(authority))
        return pipeline.TextEvidenceResult(srt_text=c['srt'],
            cues=pipeline.parse_srt_cues(c['srt']), chat_authority_path=c['authority_path'])
    monkeypatch.setattr(pipeline, '_finalize_text_evidence', finalize)
    adapters = replace(_adapters(), profile_asset_file=lambda name:
        auto._pipeline_kwargs(c)['adapters'].profile_asset_file(name)
        if name == 'subtitle_truth_ledger' else tmp_path / name)
    kw = auto._pipeline_kwargs(c, adapters=adapters)
    result = pipeline._automatic_current_source_stage_checkpoint(**kw, merged=[],
        authoritative_chat=[], native_audio_state=state, witness_cache_roots=(tmp_path / 'prior',))
    assert result.srt_text == c['srt']
    assert len(consumed) == 2 and all(s is state for s in consumed)


def test_explicit_account_drift_is_rejected_before_current_consumer(tmp_path, monkeypatch):
    from src.autoslice import retained_reviewed_draft
    c = auto._checkpoint(tmp_path)
    _, state = _account(tmp_path, monkeypatch, c['padded'])
    monkeypatch.setattr(retained_reviewed_draft, 'load_retained_reviewed_draft',
        lambda **kw: {'retained_native': state})
    monkeypatch.setattr(pipeline, '_run_current_text_continuation', _forbidden)
    kw = auto._pipeline_kwargs(c)
    kw['spec'] = {**kw['spec'], 'retained_current_text': {}}
    kw['continuation'] = pipeline.CurrentTextContinuation(
        current_srt_path=c['out_root'] / 'padded.fresh.srt',
        chat_authority_path=c['authority_path'], clip_context_path=c['context_path'],
        padded_sha256='unused', source_media_sha256s=(), spans=[], transcriber=_forbidden,
        native_audio_state=replace(state, budget_receipt=tmp_path / 'foreign-receipt.json'))
    with pytest.raises(pipeline.NativeAudioAccountContinuationError) as exc:
        pipeline.run_text_pipeline(**kw)
    assert exc.value.reason_code == 'CURRENT_NATIVE_AUDIO_ACCOUNT_IDENTITY_DRIFT'


def test_native_provider_consumers_share_receipt_cache_and_history_roots(tmp_path, monkeypatch):
    _offline(monkeypatch)
    source, state = _account(tmp_path, monkeypatch)
    state = replace(state, history_cache_roots=(tmp_path / 'fork-history',))
    roots = (tmp_path / 'retained-witnesses',)
    seen = {}
    def local(**kw):
        seen['local'] = kw
        return _forbidden
    def direct(**kw):
        seen['direct'] = kw
        return kw['fallback']
    independent = native.build_independent_context_witnesses
    def wrapped(**kw):
        seen['independent'] = kw
        return independent(**kw)
    monkeypatch.setattr(audio, 'build_local_audio_entity_verifier', local)
    monkeypatch.setattr(native, 'build_native_context_verifier', direct)
    monkeypatch.setattr(native, 'build_independent_context_witnesses', wrapped)
    monkeypatch.setattr(pipeline, '_ensure_local_native_audio_budget', lambda *a: 'moss')
    pipeline._build_entity_verification_context(**_kwargs(tmp_path, source, state),
        witness_cache_roots=roots)
    assert seen['local']['witness_cache_roots'] == (*roots, *state.history_cache_roots)
    for name in ('direct', 'independent'):
        assert seen[name]['budget_receipt_path'] is state.budget_receipt
        assert seen[name]['native_observation_cache_root'] is state.cache_root
