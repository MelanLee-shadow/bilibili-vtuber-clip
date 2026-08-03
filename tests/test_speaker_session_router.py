import hashlib
import json
import sys
from pathlib import Path

import pytest

from src.autoslice.speaker_common import HOST_SPEAKER
from src.autoslice.speaker_finalizer import (
    SpeakerFinalizationError,
    finalize_fast_solo_subtitles,
)
from src.autoslice.speaker_session_router import (
    ACOUSTIC_EVIDENCE_SCHEMA_VERSION,
    AUDITED_PROVIDER_BUNDLES,
    FAST_SOLO,
    PROVIDER_SCHEMA_VERSION,
    REQUEST_SCHEMA_VERSION,
    ROUTER_POLICY_VERSION,
    RUN_BINARY_FINALIZER,
    SpeakerRoutingError,
    build_provider_authority,
    generate_speaker_routing,
    routing_policy_fingerprint,
    routing_runtime_fingerprint,
    segment_binding_sha256,
    segment_stat_signature,
    verify_speaker_routing_claim,
)


PIPELINE_FINGERPRINT = "sha256:" + "9" * 64


@pytest.fixture(autouse=True)
def _empty_test_provider_allowlist():
    AUDITED_PROVIDER_BUNDLES.clear()
    yield
    AUDITED_PROVIDER_BUNDLES.clear()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _request_fixture(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"sealed source segment")
    bcut_srt = tmp_path / "segment.bcut.srt"
    bcut_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n第一句\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n第二句\n",
        encoding="utf-8",
    )
    segment_sha256 = segment_binding_sha256(segment)
    candidate = {
        "candidate_id": "candidate-1",
        "segment_path": str(segment.resolve()),
        "segment_binding_sha256": segment_sha256,
        "segment_content_sha256": segment_sha256,
        "segment_stat_signature": segment_stat_signature(segment),
        "bcut_srt_path": str(bcut_srt.resolve()),
        "bcut_srt_sha256": _sha(bcut_srt),
        "start_ms": 0,
        "end_ms": 4_000,
    }
    artifact_paths = {}
    for role in ("executable", "script", "config", "model", "profile"):
        artifact = tmp_path / f"provider-{role}.bin"
        artifact.write_bytes(f"sealed {role}".encode())
        artifact_paths[role] = artifact
    authority = build_provider_authority(
        name="accepted-provider-fixture",
        algorithm_id="acoustic-test-v1",
        artifact_paths=artifact_paths,
    )
    AUDITED_PROVIDER_BUNDLES[str(authority["algorithm_fingerprint"])] = str(
        authority["provider_bundle_fingerprint"]
    )
    request = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "date": "2026-07-10",
        "pipeline_fingerprint": PIPELINE_FINGERPRINT,
        "router_policy_version": ROUTER_POLICY_VERSION,
        "router_policy_fingerprint": routing_policy_fingerprint(),
        "provider_authority": authority,
        "routing_runtime_fingerprint": routing_runtime_fingerprint(authority),
        "candidates": [candidate],
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    return request_path, candidate


def _provider_fixture(
    tmp_path: Path,
    request_path: Path,
    candidate: dict[str, object],
    *,
    verdict: str = "SOLO_HOST",
    complete_coverage: bool = True,
) -> Path:
    if verdict == "UNCERTAIN":
        complete_coverage = False
    unresolved = 0 if complete_coverage else 1
    evidence = {
        "schema_version": ACOUSTIC_EVIDENCE_SCHEMA_VERSION,
        "source_media_sha256": candidate["segment_content_sha256"],
        "window_start_ms": candidate["start_ms"],
        "window_end_ms": candidate["end_ms"],
        "audio_window_sha256": "b" * 64,
        "analyzed_speech_ms": 2_000,
        "coverage_unit_count": 2,
        "covered_unit_count": 2 - unresolved,
        "unresolved_unit_count": unresolved,
        "speaker_count_lower_bound": 2 if verdict == "MULTI_SPEAKER" else 1,
        "overlap_detected": False,
        "mixed_speaker_within_unit_detected": False,
        "acoustic_observation_sha256": "c" * 64,
    }
    request = json.loads(request_path.read_text(encoding="utf-8"))
    provider = {
        "schema_version": PROVIDER_SCHEMA_VERSION,
        "status": "READY",
        "request_sha256": _sha(request_path),
        "pipeline_fingerprint": PIPELINE_FINGERPRINT,
        "routing_runtime_fingerprint": request["routing_runtime_fingerprint"],
        "input_modality": "audio",
        "transcript_or_llm_used": False,
        "provider": request["provider_authority"],
        "candidate_results": [
            {
                "candidate_id": candidate["candidate_id"],
                "segment_binding_sha256": candidate["segment_binding_sha256"],
                "bcut_srt_sha256": candidate["bcut_srt_sha256"],
                "verdict": verdict,
                "complete_coverage": complete_coverage,
                "unresolved_cue_count": unresolved,
                "mixed_or_overlap_detected": False,
                "evidence": evidence,
                "evidence_sha256": hashlib.sha256(
                    json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        ],
    }
    provider_path = tmp_path / "provider.json"
    provider_path.write_text(json.dumps(provider), encoding="utf-8")
    return provider_path


def _verify(
    claim: dict[str, object],
    claim_path: Path,
    candidate: dict[str, object],
):
    return verify_speaker_routing_claim(
        claim,
        claim_path=claim_path,
        expected_claim_sha256=_sha(claim_path),
        expected_date="2026-07-10",
        expected_candidate_id=str(candidate["candidate_id"]),
        expected_segment_binding_sha256=str(candidate["segment_binding_sha256"]),
        expected_start_ms=int(candidate["start_ms"]),
        expected_end_ms=int(candidate["end_ms"]),
        expected_pipeline_fingerprint=PIPELINE_FINGERPRINT,
        expected_segment_path=Path(str(candidate["segment_path"])),
        expected_segment_stat_signature=candidate["segment_stat_signature"],
        expected_bcut_srt_path=Path(str(candidate["bcut_srt_path"])),
        expected_bcut_srt_sha256=str(candidate["bcut_srt_sha256"]),
    )


def test_missing_provider_falls_back_to_binary_without_model_work(tmp_path: Path) -> None:
    request_path, _candidate = _request_fixture(tmp_path)
    claim = generate_speaker_routing(
        request_path=request_path,
        output_path=tmp_path / "claim.json",
    )

    assert claim["decision"] == RUN_BINARY_FINALIZER
    assert claim["fast_solo_authorized"] is False
    assert claim["reason_codes"] == ["ROUTER_PROVIDER_UNAVAILABLE"]


def test_routing_request_rejects_non_date_path_value_before_claim_write(
    tmp_path: Path,
) -> None:
    request_path, _candidate = _request_fixture(tmp_path)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["date"] = "../../escape"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    output = tmp_path / "claim.json"

    with pytest.raises(SpeakerRoutingError, match="strict YYYY-MM-DD"):
        generate_speaker_routing(request_path=request_path, output_path=output)
    assert not output.exists()


@pytest.mark.parametrize("verdict", ["MULTI_SPEAKER", "UNCERTAIN"])
def test_non_solo_provider_verdict_falls_back_to_binary(
    tmp_path: Path, verdict: str
) -> None:
    request_path, candidate = _request_fixture(tmp_path)
    provider_path = _provider_fixture(
        tmp_path, request_path, candidate, verdict=verdict
    )
    claim = generate_speaker_routing(
        request_path=request_path,
        output_path=tmp_path / "claim.json",
        provider_evidence_path=provider_path,
    )

    assert claim["decision"] == RUN_BINARY_FINALIZER
    assert claim["reason_codes"] == [f"PROVIDER_{verdict}:candidate-1"]


def test_incomplete_solo_provider_coverage_falls_back_to_binary(tmp_path: Path) -> None:
    request_path, candidate = _request_fixture(tmp_path)
    provider_path = _provider_fixture(
        tmp_path, request_path, candidate, complete_coverage=False
    )
    claim = generate_speaker_routing(
        request_path=request_path,
        output_path=tmp_path / "claim.json",
        provider_evidence_path=provider_path,
    )

    assert claim["decision"] == RUN_BINARY_FINALIZER
    assert claim["reason_codes"] == ["ROUTER_PROVIDER_INVALID:SpeakerRoutingError"]


@pytest.mark.parametrize(
    "modality,transcript_used",
    [("transcript_text_only", False), ("audio", True)],
)
def test_text_or_llm_identity_evidence_is_rejected(
    tmp_path: Path, modality: str, transcript_used: bool
) -> None:
    request_path, candidate = _request_fixture(tmp_path)
    provider_path = _provider_fixture(tmp_path, request_path, candidate)
    provider = json.loads(provider_path.read_text(encoding="utf-8"))
    provider["input_modality"] = modality
    provider["transcript_or_llm_used"] = transcript_used
    provider_path.write_text(json.dumps(provider), encoding="utf-8")

    claim = generate_speaker_routing(
        request_path=request_path,
        output_path=tmp_path / "claim.json",
        provider_evidence_path=provider_path,
    )

    assert claim["decision"] == RUN_BINARY_FINALIZER
    assert claim["reason_codes"] == ["ROUTER_PROVIDER_INVALID:SpeakerRoutingError"]


def test_unallowlisted_provider_or_unbound_evidence_falls_back_to_binary(
    tmp_path: Path,
) -> None:
    request_path, candidate = _request_fixture(tmp_path)
    provider_path = _provider_fixture(tmp_path, request_path, candidate)
    provider = json.loads(provider_path.read_text(encoding="utf-8"))
    provider["provider"]["name"] = "unallowlisted-provider"
    provider_path.write_text(json.dumps(provider), encoding="utf-8")

    claim = generate_speaker_routing(
        request_path=request_path,
        output_path=tmp_path / "claim.json",
        provider_evidence_path=provider_path,
    )

    assert claim["decision"] == RUN_BINARY_FINALIZER
    assert claim["reason_codes"] == ["ROUTER_PROVIDER_INVALID:SpeakerRoutingError"]


def test_fast_claim_verifier_rechecks_request_provider_and_source_bindings(
    tmp_path: Path,
) -> None:
    request_path, candidate = _request_fixture(tmp_path)
    provider_path = _provider_fixture(tmp_path, request_path, candidate)
    claim_path = tmp_path / "claim.json"
    claim = generate_speaker_routing(
        request_path=request_path,
        output_path=claim_path,
        provider_evidence_path=provider_path,
    )

    verified = _verify(claim, claim_path, candidate)
    assert claim["decision"] == FAST_SOLO
    assert verified.candidate_id == "candidate-1"
    assert verified.claim_sha256 == _sha(claim_path)

    provider_path.write_text(provider_path.read_text() + "\n", encoding="utf-8")
    with pytest.raises(SpeakerRoutingError, match="provider evidence drifted"):
        _verify(claim, claim_path, candidate)


def test_fast_claim_verifier_rehashes_provider_model_bytes(tmp_path: Path) -> None:
    request_path, candidate = _request_fixture(tmp_path)
    provider_path = _provider_fixture(tmp_path, request_path, candidate)
    claim_path = tmp_path / "claim.json"
    claim = generate_speaker_routing(
        request_path=request_path,
        output_path=claim_path,
        provider_evidence_path=provider_path,
    )
    request = json.loads(request_path.read_text(encoding="utf-8"))
    model_path = Path(request["provider_authority"]["artifacts"]["model"]["path"])
    model_path.write_bytes(b"different model bytes at same path")

    with pytest.raises(SpeakerRoutingError, match="provider artifact/config/model/profile bytes drifted"):
        _verify(claim, claim_path, candidate)


def test_fast_claim_verifier_rehashes_executed_script_bytes(tmp_path: Path) -> None:
    request_path, candidate = _request_fixture(tmp_path)
    provider_path = _provider_fixture(tmp_path, request_path, candidate)
    claim_path = tmp_path / "claim.json"
    claim = generate_speaker_routing(
        request_path=request_path,
        output_path=claim_path,
        provider_evidence_path=provider_path,
    )
    request = json.loads(request_path.read_text(encoding="utf-8"))
    script_path = Path(request["provider_authority"]["artifacts"]["script"]["path"])
    script_path.write_bytes(b"different actually executed script bytes")

    with pytest.raises(SpeakerRoutingError, match="provider artifact/config/model/profile bytes drifted"):
        _verify(claim, claim_path, candidate)


def test_self_asserted_audio_hashes_cannot_cross_empty_production_allowlist(
    tmp_path: Path,
) -> None:
    request_path, candidate = _request_fixture(tmp_path)
    provider_path = _provider_fixture(tmp_path, request_path, candidate)
    AUDITED_PROVIDER_BUNDLES.clear()

    with pytest.raises(SpeakerRoutingError, match="not repo-audited"):
        generate_speaker_routing(
            request_path=request_path,
            output_path=tmp_path / "claim.json",
            provider_evidence_path=provider_path,
        )


def test_provider_artifact_tree_rejects_nested_symlink(tmp_path: Path) -> None:
    artifacts = {}
    for role in ("executable", "script", "config", "profile"):
        path = tmp_path / role
        path.write_bytes(role.encode())
        artifacts[role] = path
    model = tmp_path / "model"
    model.mkdir()
    (model / "weights.bin").write_bytes(b"weights")
    (model / "escape").symlink_to(tmp_path / "profile")
    artifacts["model"] = model

    with pytest.raises(SpeakerRoutingError, match="tree contains a symlink"):
        build_provider_authority(
            name="fixture", algorithm_id="fixture-v1", artifact_paths=artifacts
        )


def test_fast_renderer_requires_verified_authority_and_uses_sapphire_style(
    tmp_path: Path,
) -> None:
    request_path, candidate = _request_fixture(tmp_path)
    provider_path = _provider_fixture(tmp_path, request_path, candidate)
    claim_path = tmp_path / "claim.json"
    claim = generate_speaker_routing(
        request_path=request_path,
        output_path=claim_path,
        provider_evidence_path=provider_path,
    )
    verified = _verify(claim, claim_path, candidate)
    media = tmp_path / "recut.mp4"
    media.write_bytes(b"final recut")
    text_srt = Path(str(candidate["bcut_srt_path"]))
    output_srt = tmp_path / "speaker.srt"
    output_ass = tmp_path / "speaker.ass"
    output_manifest = tmp_path / "speaker.json"

    manifest = finalize_fast_solo_subtitles(
        media_path=media,
        text_srt_path=text_srt,
        output_srt_path=output_srt,
        output_ass_path=output_ass,
        output_manifest_path=output_manifest,
        candidate_id="candidate-1",
        routing_claim_path=claim_path,
        verified_route=verified,
        fresh_derivation={
            "schema_version": "lidousha-speaker-fast-fresh-derivation.v1",
            "method": "canonical_accurate_recut_direct_from_claimed_segment",
            "cache_reused": False,
            "source_path": str(Path(candidate["segment_path"]).resolve()),
            "source_sha256": candidate["segment_binding_sha256"],
            "absolute_source_start_ms": 0,
            "absolute_source_end_ms": 4_000,
            "expected_duration_ms": 4_000,
            "actual_duration_ms": 4_000,
            "output_path": str(media.resolve()),
            "output_sha256": _sha(media),
        },
    )

    assert manifest["analysis"] == {
        "mode": "speaker_session_fast_solo_v1",
        "campp_invoked": False,
        "context_invoked": False,
    }
    assert manifest["fresh_fast_derivation"]["source_sha256"] == candidate[
        "segment_binding_sha256"
    ]
    assert manifest["fresh_fast_derivation"]["output_sha256"] == _sha(media)
    assert manifest["fresh_fast_derivation"]["cache_reused"] is False
    assert output_srt.read_text(encoding="utf-8").count(f"[{HOST_SPEAKER}]") == 2
    ass = output_ass.read_text(encoding="utf-8")
    assert "Style: LDS,Microsoft YaHei,72" in ass
    assert "Dialogue: 0,0:00:00.00,0:00:02.00,LDS" in ass

    with pytest.raises(SpeakerFinalizationError, match="requires a verified route"):
        finalize_fast_solo_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            output_srt_path=tmp_path / "unverified.srt",
            output_ass_path=tmp_path / "unverified.ass",
            output_manifest_path=tmp_path / "unverified.json",
            candidate_id="candidate-1",
            routing_claim_path=claim_path,
            verified_route=claim,
            fresh_derivation={},
        )
