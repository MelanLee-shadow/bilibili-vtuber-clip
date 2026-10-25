"""Synthetic metadata and offline admission fixture; no saved provider records."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import wave
import pytest
from src.autoslice import argon_escalation_agy as native
from src.autoslice.argon_escalation_contract import sha
from src.autoslice.argon_escalation_runtime import ArgonRuntime
from src.autoslice.argon_escalation_witness import HttpResponse
from tests.test_argon_consumer_integration import NativeProvider

SYNTHETIC_METADATA = {
    "native": {"stdout": "gemini-fixture-other\tSynthetic other model\n"},
    "api": {"body": json.dumps({"name": "models/gemini-4-argon",
        "supportedGenerationMethods": ["generateContent", "batchGenerateContent"]})},
}

@pytest.fixture
def admission(case, tmp_path, monkeypatch):
    _, spec, _, _ = case
    base, root = tmp_path / "base", tmp_path / "normal-runtime"
    monkeypatch.setenv("AUTOSLICE_BASE", str(base))
    monkeypatch.setenv("AUTOSLICE_PROVIDER_CONCURRENCY", "1")
    provider = NativeProvider()
    # Test world: these ladder/consumer mechanics model a free key that serves
    # Argon, contrary to the provider tier table (the real table is
    # asserted in tests/test_argon_tier_routing.py).
    from src.autoslice import argon_model_capability as _capability
    monkeypatch.setattr(_capability, "TIER_INFERENCE", {"free": "AVAILABLE", "paid_backup": "AVAILABLE"})
    provider.metadata_response = HttpResponse(200, SYNTHETIC_METADATA["api"]["body"].encode())
    state = SimpleNamespace(root=root, base=base, spec=spec, provider=provider,
        catalog_calls=[], audio_calls=[], loader_calls=0, catalog_outcome="unsupported")
    monkeypatch.setattr(native, "availability", lambda: ("/offline-protocol/agy", True))

    def catalog(argv, **kwargs):
        assert argv == ["/offline-protocol/agy", "models"]
        assert not (Path(kwargs["cwd"]) / "agy-input").exists()
        state.catalog_calls.append({"argv": argv, "cwd": kwargs["cwd"]})
        outcome = state.catalog_outcome
        if outcome == "crash":
            raise KeyboardInterrupt("synthetic metadata interruption")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, 1, output=b"synthetic prefix")
        if outcome == "quota_error":
            return subprocess.CompletedProcess(argv, 1, b"", b"quota exhausted")
        raw = {
            "unsupported": SYNTHETIC_METADATA["native"]["stdout"].encode(),
            "supported": b"gemini-4-argon\tGemini 4 Argon\n",
            "malformed": b"Gemini 4 Argon is available",
            "renamed_38": b"gemini-3.8-flash-high\tGemini 4 Argon\n",
            "wrong_label": b"gemini-4-argon\tGemini 3.8 Flash (High)\n",
            "duplicate": b"gemini-4-argon\tGemini 4 Argon\ngemini-4-argon\tGemini 4 Argon\n",
            "empty": b"",
        }[outcome]
        return subprocess.CompletedProcess(argv, 0, raw, b"Fetching available models...\n")

    def audio(argv, **kwargs):
        directory = Path(kwargs["cwd"])
        assert argv[argv.index("--model") + 1] == native.AGY_MODEL
        prompt = (directory / "prompt.md").read_text()
        raw_audio = (directory / "input.wav").read_bytes()
        with wave.open(str(directory / "input.wav")) as stream:
            assert stream.getnframes() > 0 and stream.getnchannels() == 1
        state.audio_calls.append({"argv": argv, "audio_sha256": sha(raw_audio),
                                  "prompt_sha256": sha(prompt.encode())})
        if getattr(state, "audio_quota", False):
            return subprocess.CompletedProcess(argv, 1, "", "quota exhausted")
        if getattr(state, "audio_unknown", False):
            raise subprocess.TimeoutExpired(argv, 1, output=b"synthetic inference prefix")
        binding = json.loads(prompt[prompt.rfind("{"):])
        response = {"schema_version": "argon-blind-hearing.v1", **binding,
            "status": "OBSERVED", "transcript": "合成协议词句", "uncertainties": [],
            "speech_role": "unknown", "reason": None}
        return subprocess.CompletedProcess(argv, 0, json.dumps(response, ensure_ascii=False), "")

    def loader():
        state.loader_calls += 1
        return {"GEMINI_API_KEY": "fictional-free-one", "GEMINI_API_KEY_2": "fictional-free-two",
                "GEMINI_KEY_BACKUP": "fictional-paid"}
    monkeypatch.setattr(native, "execute_model_catalog", catalog)
    monkeypatch.setattr(native, "execute_command", audio)
    state.config = {"enabled": True, "allow_paid": True}
    state.new_runtime = lambda: ArgonRuntime(runtime_root=base, config=state.config,
        credential_loader=loader, transport_factory=provider.factory)
    state.advance = lambda: state.new_runtime().advance(root=root, spec=spec)
    return state
