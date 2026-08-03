import hashlib
import json
from pathlib import Path

import pytest

import scripts.produce_slice_package as producer
from src.autoslice.speaker_session_router import (
    SpeakerRoutingError,
    VerifiedFastSoloRoute,
    VerifiedSpeakerRoute,
)


def _inputs(tmp_path: Path) -> dict[str, object]:
    media = tmp_path / "media.mp4"
    media.write_bytes(b"final recut media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n第一句\n",
        encoding="utf-8",
    )
    route = tmp_path / "speaker-routing.json"
    route.write_text('{"decision":"FAST_SOLO"}\n', encoding="utf-8")
    segment = tmp_path / "segment.mp4"
    segment.write_bytes(b"source segment")
    bcut = tmp_path / "segment.bcut.srt"
    bcut.write_text(text_srt.read_text(encoding="utf-8"), encoding="utf-8")
    source_sha = hashlib.sha256(segment.read_bytes()).hexdigest()
    piece_output = tmp_path / "piece.mp4"
    piece_output.write_bytes(b"exact piece")
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(piece_output.read_bytes())
    spec = {
        "date": "2026-07-10",
        "speaker_routing_claim": str(route),
        "speaker_routing_claim_sha256": hashlib.sha256(route.read_bytes()).hexdigest(),
        "speaker_routing_candidate": {
            "segment_path": str(segment),
            "segment_binding_sha256": source_sha,
            "segment_stat_signature": {},
            "bcut_srt_path": str(bcut),
            "bcut_srt_sha256": hashlib.sha256(bcut.read_bytes()).hexdigest(),
            "start_ms": 0,
            "end_ms": 2_000,
            "pipeline_fingerprint": "sha256:" + "b" * 64,
        },
        "pieces": [
            {"remote_media": str(segment), "start_ms": 0, "end_ms": 2_000}
        ],
    }
    provenance = tmp_path / "media.provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "schema_version": producer.RECUT_PROVENANCE_SCHEMA,
                "source_piece": {
                    "source_path": str(segment.resolve()),
                    "source_sha256": source_sha,
                    "start_ms": 0,
                    "end_ms": 2_000,
                    "output_path": str(piece_output.resolve()),
                    "output_sha256": hashlib.sha256(piece_output.read_bytes()).hexdigest(),
                },
                "padded": {
                    "inputs": [
                        {
                            "path": str(piece_output.resolve()),
                            "sha256": hashlib.sha256(piece_output.read_bytes()).hexdigest(),
                        }
                    ],
                    "output_path": str(padded.resolve()),
                    "output_sha256": hashlib.sha256(padded.read_bytes()).hexdigest(),
                },
                "final_recut": {
                    "source_path": str(padded.resolve()),
                    "source_sha256": hashlib.sha256(padded.read_bytes()).hexdigest(),
                    "start_ms": 0,
                    "end_ms": 2_000,
                    "absolute_source_start_ms": 0,
                    "absolute_source_end_ms": 2_000,
                    "output_path": str(media.resolve()),
                    "output_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "media": media,
        "text_srt": text_srt,
        "route": route,
        "spec": spec,
        "provenance": provenance,
    }


def _run(
    tmp_path: Path,
    inputs: dict[str, object],
    *,
    mode: str,
    final_source_start_ms: int = 0,
    final_source_end_ms: int = 2_000,
):
    return producer.run_producer_speaker_finalization(
        speaker_mode=mode,
        host="localhost",
        candidate_id="candidate-1",
        media_path=inputs["media"],
        text_srt_path=inputs["text_srt"],
        output_srt_path=tmp_path / "speaker-final.srt",
        output_ass_path=tmp_path / "speaker-final.ass",
        output_manifest_path=tmp_path / "speaker-final.json",
        work_dir=tmp_path / "speaker-work",
        spec=inputs["spec"],
        spec_parent=tmp_path,
        override_path=None,
        source_session_anchor_path=None,
        mixed_overlap_evidence_path=None,
        speaker_python=Path("/must-not-run/model-python"),
        final_source_start_ms=final_source_start_ms,
        final_source_end_ms=final_source_end_ms,
    )


def test_auto_fast_branch_never_invokes_binary_finalizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    verified = VerifiedFastSoloRoute(
        claim_sha256=inputs["spec"]["speaker_routing_claim_sha256"],
        candidate_id="candidate-1",
        pipeline_fingerprint="sha256:" + "b" * 64,
        routing_runtime_fingerprint="sha256:" + "e" * 64,
        request_sha256="c" * 64,
        provider_evidence_sha256="d" * 64,
        decision="FAST_SOLO",
        candidate_result={"verdict": "SOLO_HOST"},
        segment_path=inputs["spec"]["speaker_routing_candidate"]["segment_path"],
        segment_binding_sha256=inputs["spec"]["speaker_routing_candidate"][
            "segment_binding_sha256"
        ],
        start_ms=0,
        end_ms=2_000,
    )
    monkeypatch.setattr(
        producer,
        "verify_speaker_routing_claim",
        lambda *_args, **_kwargs: verified,
    )
    monkeypatch.setattr(
        producer,
        "run_speaker_finalizer",
        lambda **_kwargs: pytest.fail("FAST branch must not run the binary finalizer"),
    )
    derivation_calls = []

    def derive(**kwargs):
        derivation_calls.append(kwargs)
        inputs["media"].write_bytes(b"fresh direct recut")
        return {
            "schema_version": producer.FAST_FRESH_DERIVATION_SCHEMA,
            "method": "canonical_accurate_recut_direct_from_claimed_segment",
            "cache_reused": False,
            "source_path": verified.segment_path,
            "source_sha256": verified.segment_binding_sha256,
            "absolute_source_start_ms": 0,
            "absolute_source_end_ms": 2_000,
            "expected_duration_ms": 2_000,
            "actual_duration_ms": 2_000,
            "output_path": str(inputs["media"].resolve()),
            "output_sha256": hashlib.sha256(
                inputs["media"].read_bytes()
            ).hexdigest(),
        }

    monkeypatch.setattr(producer, "_derive_fresh_fast_media", derive)

    manifest = _run(tmp_path, inputs, mode="auto")

    assert len(derivation_calls) == 1
    assert inputs["media"].read_bytes() == b"fresh direct recut"
    assert manifest["fresh_fast_derivation"]["cache_reused"] is False
    assert manifest["analysis"]["mode"] == "speaker_session_fast_solo_v1"
    assert not list(tmp_path.glob(".media.mp4.fast-backup-*"))
    assert not list(tmp_path.glob(".media.mp4.fast-derive-*"))


@pytest.mark.parametrize("failure", ["missing", "invalid"])
def test_auto_uncertainty_falls_back_to_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    inputs = _inputs(tmp_path)
    if failure == "missing":
        inputs["spec"].pop("speaker_routing_claim")
        inputs["spec"].pop("speaker_routing_claim_sha256")
    else:
        monkeypatch.setattr(
            producer,
            "verify_speaker_routing_claim",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                SpeakerRoutingError("uncertain provider")
            ),
        )
    monkeypatch.setattr(
        producer,
        "finalize_fast_solo_subtitles",
        lambda **_kwargs: pytest.fail("uncertain route must not use FAST renderer"),
    )
    binary_calls = []
    monkeypatch.setattr(
        producer,
        "run_speaker_finalizer",
        lambda **kwargs: binary_calls.append(kwargs)
        or {"status": "READY", "production_ready": True},
    )

    manifest = _run(tmp_path, inputs, mode="auto")

    assert len(binary_calls) == 1
    assert manifest["speaker_routing"]["decision"] == "RUN_BINARY_FINALIZER"
    assert manifest["speaker_routing"]["reason"].startswith(
        "ROUTING_CLAIM_MISSING" if failure == "missing" else "ROUTING_UNCERTAIN"
    )


def test_required_mode_ignores_fast_claim_and_runs_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    monkeypatch.setattr(
        producer,
        "verify_speaker_routing_claim",
        lambda *_args, **_kwargs: pytest.fail("required mode must not inspect FAST claim"),
    )
    binary_calls = []
    monkeypatch.setattr(
        producer,
        "run_speaker_finalizer",
        lambda **kwargs: binary_calls.append(kwargs)
        or {"status": "READY", "production_ready": True},
    )

    manifest = _run(tmp_path, inputs, mode="required")

    assert len(binary_calls) == 1
    assert manifest["speaker_routing"]["reason"] == "SPEAKER_MODE_REQUIRED"


def test_final_recut_outside_claim_coverage_falls_back_before_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    monkeypatch.setattr(
        producer,
        "verify_speaker_routing_claim",
        lambda *_args, **_kwargs: pytest.fail("out-of-coverage recut must not verify FAST"),
    )
    binary_calls = []
    monkeypatch.setattr(
        producer,
        "run_speaker_finalizer",
        lambda **kwargs: binary_calls.append(kwargs)
        or {"status": "READY", "production_ready": True},
    )

    manifest = _run(
        tmp_path,
        inputs,
        mode="auto",
        final_source_end_ms=2_001,
    )

    assert len(binary_calls) == 1
    assert manifest["speaker_routing"]["reason"] == "FINAL_RECUT_OUTSIDE_ROUTING_COVERAGE"


def test_fast_ignores_mutable_provenance_and_freshly_replaces_unrelated_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    inputs["media"].write_bytes(b"reviewer PoC unrelated final bytes")
    inputs["spec"]["pieces"][0]["remote_media"] = str(tmp_path / "unrelated-piece.mp4")
    (tmp_path / "unrelated-piece.mp4").write_bytes(b"unrelated piece bytes")
    inputs["provenance"].write_text(
        json.dumps(
            {
                "schema_version": producer.RECUT_PROVENANCE_SCHEMA,
                "source_piece": {"attacker": "recomputed"},
                "padded": {"attacker": "recomputed"},
                "final_recut": {
                    "output_sha256": hashlib.sha256(
                        inputs["media"].read_bytes()
                    ).hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )
    source = Path(
        inputs["spec"]["speaker_routing_candidate"]["segment_path"]
    )
    commands = []

    def fake_command(**kwargs):
        commands.append(kwargs)
        return ["canonical-recut", str(kwargs["output_media"])]

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"fresh bytes derived only from claimed segment")

    monkeypatch.setattr(producer, "_accurate_reencode_recut_command", fake_command)
    monkeypatch.setattr(producer, "run", fake_run)
    monkeypatch.setattr(producer, "ffprobe_duration_ms", lambda _path: 2_000)

    derivation = producer._derive_fresh_fast_media(
        host="localhost",
        media_path=inputs["media"],
        claimed_segment_path=source,
        expected_segment_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        final_source_start_ms=0,
        final_source_end_ms=2_000,
    )

    assert len(commands) == 1
    assert commands[0]["source_video"] == source.resolve()
    assert inputs["media"].read_bytes() == b"fresh bytes derived only from claimed segment"
    assert derivation["cache_reused"] is False
    assert derivation["output_sha256"] == hashlib.sha256(
        inputs["media"].read_bytes()
    ).hexdigest()


def test_fast_fresh_derivation_failure_keeps_existing_media_and_cleans_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    original = inputs["media"].read_bytes()
    source = Path(
        inputs["spec"]["speaker_routing_candidate"]["segment_path"]
    )

    def fake_command(**kwargs):
        return ["canonical-recut", str(kwargs["output_media"])]

    def failing_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"partial unsafe output")
        raise RuntimeError("recut failed")

    monkeypatch.setattr(producer, "_accurate_reencode_recut_command", fake_command)
    monkeypatch.setattr(producer, "run", failing_run)

    with pytest.raises(SpeakerRoutingError, match="recut failed"):
        producer._derive_fresh_fast_media(
            host="localhost",
            media_path=inputs["media"],
            claimed_segment_path=source,
            expected_segment_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            final_source_start_ms=0,
            final_source_end_ms=2_000,
        )

    assert inputs["media"].read_bytes() == original
    assert not list(tmp_path.glob(".media.mp4.fast-derive-*"))


def test_fast_derivation_failure_falls_back_binary_without_partial_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    original = inputs["media"].read_bytes()
    verified = VerifiedFastSoloRoute(
        claim_sha256=inputs["spec"]["speaker_routing_claim_sha256"],
        candidate_id="candidate-1",
        pipeline_fingerprint="sha256:" + "b" * 64,
        routing_runtime_fingerprint="sha256:" + "e" * 64,
        request_sha256="c" * 64,
        provider_evidence_sha256="d" * 64,
        decision="FAST_SOLO",
        candidate_result={"verdict": "SOLO_HOST"},
        segment_path=inputs["spec"]["speaker_routing_candidate"]["segment_path"],
        segment_binding_sha256=inputs["spec"]["speaker_routing_candidate"][
            "segment_binding_sha256"
        ],
        start_ms=0,
        end_ms=2_000,
    )
    monkeypatch.setattr(
        producer,
        "verify_speaker_routing_claim",
        lambda *_args, **_kwargs: verified,
    )
    monkeypatch.setattr(
        producer,
        "_derive_fresh_fast_media",
        lambda **_kwargs: (_ for _ in ()).throw(
            SpeakerRoutingError("fresh derivation failed")
        ),
    )
    binary_calls = []
    monkeypatch.setattr(
        producer,
        "run_speaker_finalizer",
        lambda **kwargs: binary_calls.append(kwargs)
        or {"status": "READY", "production_ready": True},
    )

    manifest = _run(tmp_path, inputs, mode="auto")

    assert len(binary_calls) == 1
    assert manifest["speaker_routing"]["reason"].startswith("ROUTING_UNCERTAIN")
    assert inputs["media"].read_bytes() == original


def test_fast_transaction_restores_original_when_derivation_mapping_is_tampered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    original_sha256 = hashlib.sha256(inputs["media"].read_bytes()).hexdigest()
    candidate = inputs["spec"]["speaker_routing_candidate"]
    verified = VerifiedFastSoloRoute(
        claim_sha256=inputs["spec"]["speaker_routing_claim_sha256"],
        candidate_id="candidate-1",
        pipeline_fingerprint="sha256:" + "b" * 64,
        routing_runtime_fingerprint="sha256:" + "e" * 64,
        request_sha256="c" * 64,
        provider_evidence_sha256="d" * 64,
        decision="FAST_SOLO",
        candidate_result={"verdict": "SOLO_HOST"},
        segment_path=candidate["segment_path"],
        segment_binding_sha256=candidate["segment_binding_sha256"],
        start_ms=0,
        end_ms=2_000,
    )
    monkeypatch.setattr(
        producer,
        "verify_speaker_routing_claim",
        lambda *_args, **_kwargs: verified,
    )
    monkeypatch.setattr(
        producer,
        "_accurate_reencode_recut_command",
        lambda **kwargs: ["canonical-recut", str(kwargs["output_media"])],
    )

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"fresh transaction media")

    monkeypatch.setattr(producer, "run", fake_run)
    monkeypatch.setattr(producer, "ffprobe_duration_ms", lambda _path: 2_000)
    real_derive = producer._derive_fresh_fast_media

    def tampered_derivation(**kwargs):
        derivation = real_derive(**kwargs)
        derivation["output_sha256"] = "0" * 64
        return derivation

    monkeypatch.setattr(producer, "_derive_fresh_fast_media", tampered_derivation)
    real_finalize = producer.finalize_fast_solo_subtitles

    def finalize_with_partial_outputs(**kwargs):
        kwargs["output_srt_path"].write_text("partial-fast-srt", encoding="utf-8")
        kwargs["output_ass_path"].write_text("partial-fast-ass", encoding="utf-8")
        kwargs["output_manifest_path"].write_text(
            '{"partial":"fast"}', encoding="utf-8"
        )
        return real_finalize(**kwargs)

    monkeypatch.setattr(
        producer, "finalize_fast_solo_subtitles", finalize_with_partial_outputs
    )
    binary_seen = []

    def binary(**kwargs):
        assert hashlib.sha256(kwargs["media_path"].read_bytes()).hexdigest() == (
            original_sha256
        )
        assert not kwargs["output_srt_path"].exists()
        assert not kwargs["output_ass_path"].exists()
        assert not kwargs["output_manifest_path"].exists()
        binary_seen.append(original_sha256)
        return {"status": "READY", "production_ready": True}

    monkeypatch.setattr(producer, "run_speaker_finalizer", binary)

    manifest = _run(tmp_path, inputs, mode="auto")

    assert binary_seen == [original_sha256]
    assert hashlib.sha256(inputs["media"].read_bytes()).hexdigest() == original_sha256
    assert manifest["speaker_routing"]["decision"] == "RUN_BINARY_FINALIZER"
    assert not (tmp_path / "speaker-final.srt").exists()
    assert not (tmp_path / "speaker-final.ass").exists()
    assert "partial" not in (tmp_path / "speaker-final.json").read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".media.mp4.fast-backup-*"))
    assert not list(tmp_path.glob(".media.mp4.fast-derive-*"))


def test_fast_transaction_rollback_removes_install_when_original_was_absent(
    tmp_path: Path,
) -> None:
    media = tmp_path / "media.mp4"
    output_srt = tmp_path / "speaker-final.srt"
    output_ass = tmp_path / "speaker-final.ass"
    output_manifest = tmp_path / "speaker-final.json"
    transaction = producer._begin_fast_media_transaction(media)

    media.write_bytes(b"fresh media with no predecessor")
    output_srt.write_text("partial-fast-srt", encoding="utf-8")
    output_ass.write_text("partial-fast-ass", encoding="utf-8")
    output_manifest.write_text('{"partial":"fast"}', encoding="utf-8")

    producer._rollback_fast_media_transaction(
        transaction,
        output_srt_path=output_srt,
        output_ass_path=output_ass,
        output_manifest_path=output_manifest,
    )

    assert not media.exists()
    assert not output_srt.exists()
    assert not output_ass.exists()
    assert not output_manifest.exists()
    assert not list(tmp_path.glob(".media.mp4.fast-backup-*"))


def test_provider_mixed_route_is_preserved_as_bound_review_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    inputs["route"].write_text('{"decision":"RUN_BINARY_FINALIZER"}\n')
    inputs["spec"]["speaker_routing_claim_sha256"] = hashlib.sha256(
        inputs["route"].read_bytes()
    ).hexdigest()
    verified = VerifiedSpeakerRoute(
        claim_sha256=inputs["spec"]["speaker_routing_claim_sha256"],
        candidate_id="candidate-1",
        pipeline_fingerprint="sha256:" + "b" * 64,
        routing_runtime_fingerprint="sha256:" + "e" * 64,
        request_sha256="c" * 64,
        provider_evidence_sha256="d" * 64,
        decision="RUN_BINARY_FINALIZER",
        candidate_result={
            "verdict": "MULTI_SPEAKER",
            "mixed_or_overlap_detected": True,
            "evidence": {"overlap_detected": True},
        },
        segment_path=inputs["spec"]["speaker_routing_candidate"]["segment_path"],
        segment_binding_sha256=inputs["spec"]["speaker_routing_candidate"][
            "segment_binding_sha256"
        ],
        start_ms=0,
        end_ms=2_000,
    )
    monkeypatch.setattr(
        producer,
        "verify_speaker_routing_claim_for_candidate",
        lambda *_args, **_kwargs: verified,
    )
    generated = tmp_path / "bound-mixed.json"
    generated.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        producer,
        "_write_route_mixed_overlap_evidence",
        lambda **_kwargs: generated,
    )
    binary_calls = []
    monkeypatch.setattr(
        producer,
        "run_speaker_finalizer",
        lambda **kwargs: binary_calls.append(kwargs)
        or {"status": "SPEAKER_REVIEW_REQUIRED", "production_ready": False},
    )

    manifest = _run(tmp_path, inputs, mode="auto")

    assert len(binary_calls) == 1
    assert binary_calls[0]["mixed_overlap_evidence_path"] == generated
    assert manifest["speaker_routing"] == {
        "requested_mode": "auto",
        "decision": "RUN_BINARY_FINALIZER",
        "reason": "ROUTER_MIXED_OVERLAP_REVIEW_REQUIRED",
    }


def test_cached_piece_provenance_rejects_source_or_output_hash_change(
    tmp_path: Path,
) -> None:
    output = tmp_path / "piece.mp4"
    output.write_bytes(b"piece-v1")
    provenance = tmp_path / "piece.provenance.json"
    expected = {
        "source_path": "/sealed/source.mp4",
        "source_sha256": "a" * 64,
        "start_ms": 100,
        "end_ms": 200,
        "output_path": str(output.resolve()),
    }
    provenance.write_text(
        json.dumps(
            {
                **expected,
                "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    assert producer._valid_cached_provenance(
        provenance, expected_without_output_hash=expected, output=output
    )

    changed_source = {**expected, "source_sha256": "b" * 64}
    assert not producer._valid_cached_provenance(
        provenance, expected_without_output_hash=changed_source, output=output
    )
    output.write_bytes(b"piece-v2")
    assert not producer._valid_cached_provenance(
        provenance, expected_without_output_hash=expected, output=output
    )
