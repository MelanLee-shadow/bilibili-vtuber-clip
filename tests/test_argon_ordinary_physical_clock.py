"""Argon's ordinary antecedent compares one physical target on the source clock.

Rule (41:112): identities of the same physical target are compared by physical
source and physical clock; padded/delivery offsets are representations. A
padded-local (offset 0) ordinary hearing and a delivery-local request with a
different offset address the same source window and must be the same hearing;
a different source window or a self-inconsistent clock record still rejects.

Synthetic audio and provider stubs come from the existing consumer scene; the
autouse ``offline`` fixture forbids network and real env reads.
"""
from __future__ import annotations

import json

import pytest

from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.argon_escalation_contract import ContractError
from src.autoslice.final_review_auditor import build_context_adjudication_request
from tests import test_argon_consumer_integration as consumer

# Reuse the consumer scene (production composition, synthetic audio/provider)
# and its autouse no-network/no-real-env guard.
scene = consumer.scene
offline = consumer.offline

PADDED_SRT = (
    "1\n00:00:02,000 --> 00:00:03,000\n一百五十秒\n\n"
    "2\n00:00:04,000 --> 00:00:05,000\n还差两个\n"
)


def _requests(scene):
    padded = build_context_adjudication_request(
        PADDED_SRT, scene.finding, clip_context=scene.context,
        source_media_timeline_offset_ms=0)
    delivery = build_context_adjudication_request(
        scene.srt, scene.finding, clip_context=scene.context,
        source_media_timeline_offset_ms=1000)
    return padded, delivery


def _padded_hearing(scene):
    scene.ordinary_status = "OBSERVED"
    padded, delivery = _requests(scene)
    fresh = scene.verifier(build_witness_request(padded))
    assert fresh["status"] == "OBSERVED" and fresh.get("witness_cache_replay") is None
    manifest = (scene.out / "entity_verdicts"
                / build_witness_request(padded)["request_sha256"][:20] / "verdict.manifest.json")
    assert manifest.is_file()
    return padded, delivery, fresh, manifest


def test_offset_zero_hearing_is_the_same_physical_target_for_delivery_offset(scene):
    padded, delivery, _, manifest = _padded_hearing(scene)
    timeline = json.loads(manifest.read_text())["timeline_binding"]
    assert timeline["source_media_timeline_offset_ms"] == 0
    assert delivery["source_media_timeline_offset_ms"] == 1000
    replayed = scene.verifier.probe_witness_cache(build_witness_request(delivery))
    assert replayed["witness_cache_replay"]["status"] == "PASS"

    session = scene.verifier.argon_escalation.session(delivery, scene.finding)
    assert list(session.spec.finding.target_source_ms) == [
        timeline["source_media"]["target_start_ms"], timeline["source_media"]["target_end_ms"]]
    ordinary = session._ordinary(delivery, replayed)

    assert ordinary is not None and ordinary["cache_replay"] is not None
    assert ordinary["raw_status"] == "OBSERVED"


@pytest.mark.parametrize("damage", [
    "physical_end_plus_1ms", "local_inconsistent", "offset_without_local", "source_identity",
])
def test_different_physical_window_or_source_still_rejected(scene, damage):
    padded, _, fresh, manifest = _padded_hearing(scene)
    session = scene.verifier.argon_escalation.session(padded, scene.finding)
    assert session._ordinary(padded, fresh) is not None
    original = manifest.read_bytes()
    data = json.loads(original)
    timeline = data["timeline_binding"]
    if damage == "physical_end_plus_1ms":
        timeline["source_media"]["target_end_ms"] += 1
        timeline["delivery_local"]["target_end_ms"] += 1
        expected = "ARGON_ORDINARY_CLOCK_DRIFT"
    elif damage == "local_inconsistent":
        timeline["delivery_local"]["target_start_ms"] += 1
        expected = "ARGON_ORDINARY_CLOCK_DRIFT"
    elif damage == "offset_without_local":
        timeline["source_media_timeline_offset_ms"] = 9800
        expected = "ARGON_ORDINARY_CLOCK_DRIFT"
    else:
        data["source_media_sha256"] = "0" * 64
        expected = "ARGON_ORDINARY_MANIFEST_DRIFT"
    manifest.write_text(json.dumps(data))
    try:
        with pytest.raises(ContractError, match=expected):
            session._ordinary(padded, fresh)
    finally:
        manifest.write_bytes(original)
