import json
from pathlib import Path

from src.autoslice import entity_audio_verifier as verifier_module
from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.read_aloud_llm_verifier import (
    build_cpa_read_aloud_verifier,
)


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _observation():
    return json.dumps(
        {
            "schema_version": verifier_module.WITNESS_SCHEMA,
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": "hao piao liang o",
            "uncertain_positions": [],
            "syllable_count": 4,
            "confidence": 0.94,
            "reason": "clear blind dictation",
        }
    )


def _request(*, evidence_id, start_ms, offset_ms, text="你好"):
    return build_witness_request(
        {
            "evidence_id": evidence_id,
            "cue_indexes": [70],
            "matched_start_ms": start_ms,
            "matched_end_ms": start_ms + 1_500,
            "context_start_ms": start_ms - 1_000,
            "context_end_ms": start_ms + 2_500,
            "source_media_timeline_offset_ms": offset_ms,
            "current_cue": text,
            "proposed_cue": text,
        }
    )


def test_metadata_probe_rebinds_same_source_window_and_rejects_drift(tmp_path, monkeypatch):
    source = tmp_path / "base/recordings/source.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable source bytes")
    provider_calls = []

    def initial_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"sealed physical witness clip")
            return _Completed()
        provider_calls.append(command)
        Path(kwargs["cwd"], "verdict.json").write_text(_observation(), encoding="utf-8")
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", initial_run)
    output_dir = tmp_path / "base/out/2026-08-09/auto_probe"
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=output_dir,
        recording_date="2026-08-09",
        source_duration_ms=600_000,
        agy_bin="agy-test",
    )
    historical = _request(evidence_id="a" * 64, start_ms=180_950, offset_ms=0)
    assert verify(historical)["status"] == "OBSERVED"
    assert len(provider_calls) == 1

    def io_forbidden(*_args, **_kwargs):
        raise AssertionError("metadata probe performed subprocess I/O")

    monkeypatch.setattr(verifier_module.subprocess, "run", io_forbidden)
    original_sha256 = verifier_module._sha256

    def source_read_forbidden(path):
        if Path(path) == source:
            raise AssertionError("metadata probe reread the full source")
        return original_sha256(path)

    monkeypatch.setattr(verifier_module, "_sha256", source_read_forbidden)
    current = _request(evidence_id="b" * 64, start_ms=171_200, offset_ms=9_750)
    replayed = verify.probe_witness_cache(current)

    assert replayed["request_sha256"] == current["request_sha256"]
    assert replayed["served_from_cache"] is True
    assert replayed["timeline_binding"]["delivery_local"]["target_start_ms"] == 171_200
    assert replayed["timeline_binding"]["source_media"]["target_start_ms"] == 180_950
    receipt = replayed["witness_cache_replay"]
    assert receipt["provider_call_count"] == 0
    assert receipt["replayed_manifest_request_sha256"] == historical["request_sha256"]

    assert (
        verify.probe_witness_cache(
            _request(evidence_id="c" * 64, start_ms=171_201, offset_ms=9_750)
        )
        is None
    )
    assert (
        verify.probe_witness_cache(
            _request(
                evidence_id="d" * 64,
                start_ms=171_200,
                offset_ms=9_750,
                text="你好吗",
            )
        )
        is None
    )
    job_dir = next((output_dir / "entity_verdicts").iterdir())
    for artifacts in (
        (job_dir / "input.mp4",),
        (job_dir / "prompt.md",),
        (job_dir / "verdict.json", job_dir / "verdict.raw.json"),
    ):
        originals = [artifact.read_bytes() for artifact in artifacts]
        for artifact, original in zip(artifacts, originals, strict=True):
            artifact.write_bytes(original + b"tampered")
        assert verify.probe_witness_cache(current) is None
        for artifact, original in zip(artifacts, originals, strict=True):
            artifact.write_bytes(original)

    monkeypatch.setattr(verifier_module, "_sha256", original_sha256)
    different_model = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=output_dir,
        recording_date="2026-08-09",
        source_duration_ms=600_000,
        agy_bin="agy-test",
        model="different-current-model",
    )
    monkeypatch.setattr(verifier_module, "_sha256", source_read_forbidden)
    assert different_model.probe_witness_cache(current) is None
    source.write_bytes(b"source drift")
    assert verify.probe_witness_cache(current) is None
    assert len(provider_calls) == 1


def test_cpa_wrapper_preserves_metadata_only_probe():
    class AudioProvider:
        def __call__(self, request):
            return {"normal": request}

        def probe_witness_cache(self, request):
            return {"cached": request, "served_from_cache": True}

    verify = build_cpa_read_aloud_verifier(lambda _prompt: "{}", next_verifier=AudioProvider())
    request = {"schema_version": "subtitle-span-acoustic-witness-request.v1"}

    assert verify.probe_witness_cache(request) == {
        "cached": request,
        "served_from_cache": True,
    }
