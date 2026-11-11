"""Read-only discovery terminal replay keeps retained history compact."""
from dataclasses import replace
from types import SimpleNamespace
import tracemalloc

import pytest

from src.autoslice import discovery_channels as channels
from src.autoslice import finding_closure as core
from src.autoslice.finding_closure_ports import ArtifactSet, RetainedObject, TaskSpec, ValidationRequest


TEXT = '1\n00:00:03,000 --> 00:00:05,000\n一句\n'
SOURCE_SHA256 = 'a' * 64


def _request(original_value, current_value, *, state=core.State.KEEP):
    original = RetainedObject.from_value(original_value)
    current = RetainedObject.from_value(current_value)
    spec = TaskSpec(core.FindingKey('b' * 64, 'c' * 64), (1,),
        core.local_text_sha(TEXT, (1,)), original.sha256, 'd' * 64, 'e' * 64)
    event_spec = replace(spec, finding_ref=current.sha256)
    work = core.Work('terminal-read-view', 'task_stop', core.sha(TEXT), task=spec,
        scan_channels=channels.scan_channels())
    event = core.ReviewCompleted(work.work_id, event_spec, state, current.sha256)
    state_value = core.initial_state(source_sha256=SOURCE_SHA256, srt_text=TEXT,
        scan_channels=channels.scan_channels())
    ports = channels._ClosureDiscoveryPorts(SimpleNamespace(source_sha256=SOURCE_SHA256))
    return ports, ValidationRequest(state_value, work, event, ArtifactSet((original, current))), original, current


def test_legacy_terminal_replay_keeps_repeated_history_memory_bounded():
    repeated = {'evidence': 'x' * 512, 'nested': {'stable': True}}
    original_value = {'discovery_task': {'asr_only': False}, 'shared_history': [repeated] * 20_000}
    current_value = {'shared_history': [repeated] * 20_000}
    ports, request, original, current = _request(original_value, current_value)
    original_hash, current_hash = original.sha256, current.sha256
    original_before, current_before = original.read_view(), current.read_view()

    # Measure the real consumer without replacing its decoding API. Keeping
    # two compact inputs should not allocate one dict per history occurrence.
    tracemalloc.start()
    try:
        ports.validate_terminal(request)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024, peak

    assert original.sha256 == original_hash and current.sha256 == current_hash
    assert original.read_view() == original_before
    assert current.read_view() == current_before
    caller_view = original.read_view()
    caller_view['shared_history'][0]['nested']['stable'] = False
    caller_view['shared_history'].append({'caller': 'mutation'})
    assert original.sha256 == original_hash
    assert original.read_view() == original_before


def test_asr_terminal_rejection_and_bindings_remain_unchanged_with_read_view():
    nomination = {'channel': 'C0', 'cue_index': 1, 'payload_json': '{}',
        'observation_id': 'observation', 'provider': 'mai', 'model': 'mai',
        'reading': 'candidate', 'origin_channels': []}
    original_value = {'discovery_nominations': [nomination],
        'discovery_task': {'asr_only': True, 'channels': ['C0']}}
    current_value = {'discovery_nominations': [nomination],
        'discovery_task': {'asr_only': True, 'channels': ['C0']}}
    ports, request, original, current = _request(original_value, current_value,
        state=core.State.TRUE_BLOCK)
    original_hash, current_hash = original.sha256, current.sha256
    original_before, current_before = original.read_view(), current.read_view()

    with pytest.raises(ValueError, match='DISCOVERY_ASR_TERMINAL_ORIGINAL_REQUIRED'):
        ports.validate_terminal(request)

    assert original.sha256 == original_hash and current.sha256 == current_hash
    assert original.read_view() == original_before
    assert current.read_view() == current_before
