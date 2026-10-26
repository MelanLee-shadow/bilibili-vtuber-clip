"""The shared Argon consumer identifies one physical source by bytes, not path.

Rule (41:112): the same physical target is identified by physical source and
clock; a run's output-root media path is only where that run reads the bytes.
The finding root ``cache/argon-consumer/<finding_id>`` is shared across runs
by design, so a later output root with the same source bytes and finding must
reuse the original source binding instead of raising
IMMUTABLE_ARTIFACT_CONFLICT; a different source window is still a conflict.

Synthetic audio/providers come from the consumer scene; its autouse
``offline`` fixture forbids network and real env reads.
"""
from __future__ import annotations

import json
import shutil

import pytest

from src.autoslice import argon_escalation_storage as store
from src.autoslice.argon_escalation_contract import ContractError
from src.autoslice.argon_escalation_integration import ArgonIntegration
from tests import test_argon_consumer_integration as consumer
from tests.test_argon_ordinary_physical_clock import _padded_hearing

scene = consumer.scene
offline = consumer.offline


def _relocated_integration(scene, integration):
    """Same media bytes under another output root (a later fresh run)."""
    other_root = scene.root / "other-output-root"
    other_root.mkdir()
    relocated = other_root / scene.source.name
    shutil.copyfile(scene.source, relocated)
    return ArgonIntegration(
        source_media=relocated, source_sha256=integration.source_sha,
        source_duration_ms=integration.duration,
        ordinary_output_dir=integration.ordinary_root, runtime=integration.runtime,
        ordinary_verifier=integration.ordinary_verifier,
    ), relocated


def test_same_source_bytes_from_a_new_output_root_reuse_the_finding_record(scene):
    padded, _delivery, fresh, _manifest = _padded_hearing(scene)
    integration = scene.verifier.argon_escalation
    first = integration.session(padded, scene.finding)
    record = first.root / "source-binding.json"
    original = record.read_bytes()

    relocated_integration, relocated = _relocated_integration(scene, integration)
    second = relocated_integration.session(padded, scene.finding)

    assert second.root == first.root
    assert record.read_bytes() == original  # immutable first observation kept
    assert json.loads(original)["source_path"] == str(scene.source.absolute())
    assert second.spec.source_path == relocated.absolute()  # this run reads its own copy
    # The ordinary 3.8 antecedent written under the first root is the same
    # hearing of the same bytes for the relocated run.
    ordinary = second._ordinary(padded, fresh)
    assert ordinary is not None and ordinary["raw_status"] == "OBSERVED"


def test_different_source_window_under_the_same_finding_still_conflicts(scene):
    padded, _delivery, _fresh, _manifest = _padded_hearing(scene)
    integration = scene.verifier.argon_escalation
    first = integration.session(padded, scene.finding)
    record = first.root / "source-binding.json"
    tampered = json.loads(record.read_bytes())
    tampered["context_source_ms"] = [tampered["context_source_ms"][0] + 1,
                                     tampered["context_source_ms"][1]]
    record.unlink()
    store.put_json(record, tampered)

    with pytest.raises(ContractError, match="IMMUTABLE_ARTIFACT_CONFLICT"):
        integration.session(padded, scene.finding)


def test_different_source_bytes_are_refused_before_the_record(scene):
    padded, _delivery, _fresh, _manifest = _padded_hearing(scene)
    integration = scene.verifier.argon_escalation
    integration.session(padded, scene.finding)
    relocated_integration, relocated = _relocated_integration(scene, integration)
    relocated.write_bytes(relocated.read_bytes() + b"\0")

    with pytest.raises(ContractError, match="ARGON_RUNTIME_SOURCE_DRIFT"):
        relocated_integration.session(padded, scene.finding)


def test_span_upgrade_state_of_a_relocated_run_reads_the_shared_record(scene):
    from src.autoslice.argon_span_termination import read_upgrade

    padded, _delivery, _fresh, _manifest = _padded_hearing(scene)
    integration = scene.verifier.argon_escalation
    integration.session(padded, scene.finding)
    relocated_integration, _relocated = _relocated_integration(scene, integration)
    session = relocated_integration.session(padded, scene.finding)

    state = read_upgrade(relocated_integration, session.mark_request(padded))

    assert state["status"] != "STATE_INVALID", state
    assert state["status"] in {"AVAILABLE", "GRANTED"}
