import json
import os
from pathlib import Path

import pytest
import stat

import src.autoslice.mai_transcription as mai
import src.autoslice.moss_transcription as moss
from src.autoslice.native_audio_runtime import (
    NativeAudioRuntimeError,
    resolve_native_audio_environment,
    install_native_audio_configuration, native_audio_readiness,
)


def test_protected_install_is_provider_scoped_atomic_and_preserves_node_keys(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'; runtime.mkdir()
    payload = {'mai': {'AZURE_ENDPOINT': 'https://resource.services.ai.azure.com', 'AZURE_API_KEY': 'test-mai'},
               'moss': {'AUTOSLICE_MOSS_API_KEY': 'test-moss'}}
    monkeypatch.setattr(mai.urllib.request, 'build_opener', lambda *_: pytest.fail('no HTTP in readiness'))
    first = install_native_audio_configuration(runtime, payload)
    assert first['created_providers'] == ['mai', 'moss']
    assert all(r['ready'] for r in native_audio_readiness(runtime))
    before = {p: p.read_bytes() for p in runtime.iterdir()}
    payload['mai']['AZURE_API_KEY'] = 'different-controller-key'
    assert install_native_audio_configuration(runtime, payload)['created_providers'] == []
    assert all(p.read_bytes() == raw and stat.S_IMODE(p.stat().st_mode) == 0o600 for p,raw in before.items())
    assert all(key not in json.dumps(first) for key in ('test-mai', 'test-moss'))
    assert not list(runtime.glob('.*.env.*'))


@pytest.mark.parametrize('fault', ['extra-provider', 'cpa-key', 'invalid-endpoint', 'symlink', 'permissions'])
def test_protected_install_rejects_invalid_payload_and_unsafe_existing_config(tmp_path, fault):
    runtime=tmp_path/'runtime';runtime.mkdir()
    payload={'mai':{'AZURE_ENDPOINT':'https://resource.services.ai.azure.com','AZURE_API_KEY':'mai'},
             'moss':{'AUTOSLICE_MOSS_API_KEY':'moss'}}
    if fault=='extra-provider':payload['cpa']={'CPA_API_KEY':'forbidden'}
    if fault=='cpa-key':payload['moss']['CPA_API_KEY']='forbidden'
    if fault=='invalid-endpoint':payload['mai']['AZURE_ENDPOINT']='http://unsafe.example'
    if fault in {'symlink','permissions'}:
        target=runtime/'moss.env';target.write_text('AUTOSLICE_MOSS_API_KEY=existing\n');target.chmod(0o644)
        if fault=='symlink':
            target.rename(runtime/'original');target.symlink_to(runtime/'original')
    with pytest.raises(Exception):install_native_audio_configuration(runtime,payload)
    if fault in {'extra-provider','cpa-key','invalid-endpoint'}:assert list(runtime.iterdir())==[]


class _Response:
    def __init__(self, payload):
        self._content = json.dumps(payload).encode()
        self.closed = False

    def getcode(self):
        return 200

    def read(self):
        return self._content

    def close(self):
        self.closed = True


class _Opener:
    def __init__(self, response, calls):
        self._response = response
        self._calls = calls

    def open(self, request, *, timeout):
        self._calls.append((request, timeout))
        return self._response


def _runtime(tmp_path: Path, *, mai_text: str, moss_text: str = "") -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "mai.env").write_text(mai_text, encoding="utf-8")
    (runtime / "moss.env").write_text(moss_text, encoding="utf-8")
    (runtime / "mai.env").chmod(0o600)
    (runtime / "moss.env").chmod(0o600)
    return runtime


def test_runtime_environment_is_provider_scoped_and_does_not_mutate_process_env(tmp_path):
    runtime = _runtime(
        tmp_path,
        mai_text=(
            "AZURE_ENDPOINT=https://resource.services.ai.azure.com\n"
            "AZURE_API_KEY=mai-secret\n"
            "AUTOSLICE_MOSS_API_KEY=moss-secret\n"
            "CPA_API_KEY=cpa-secret\n"
        ),
        moss_text="MOSS_API_KEY=moss-secret\nCPA_API_KEY=cpa-secret\n",
    )
    before = dict(os.environ)
    environment = {
        "AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT": str(runtime),
        "AZURE_API_KEY": "ambient-mai",
        "AUTOSLICE_MOSS_API_KEY": "ambient-moss",
    }

    mai_values = resolve_native_audio_environment("mai", environ=environment)
    moss_values = resolve_native_audio_environment("moss", environ=environment)

    assert mai_values == {
        "AZURE_ENDPOINT": "https://resource.services.ai.azure.com",
        "AZURE_API_KEY": "mai-secret",
    }
    assert moss_values == {"AUTOSLICE_MOSS_API_KEY": "moss-secret"}
    assert dict(os.environ) == before


def test_runtime_alias_and_relative_key_file_are_normalized(tmp_path):
    key = tmp_path / "mai.key"
    key.write_text("mai-secret\n", encoding="utf-8")
    key.chmod(0o600)
    runtime = _runtime(
        tmp_path,
        mai_text=(
            "AZURE_ENDPOINT=https://resource.services.ai.azure.com\n"
            "MAI_KEY_FILE=mai.key\n"
        ),
        moss_text="MOSS_API_KEY=moss-secret\n",
    )
    # The runtime-relative key path is inside the bound runtime for a real
    # deployment; this assertion only covers normalization without exposing it.
    (runtime / "mai.key").write_text("mai-secret\n", encoding="utf-8")
    (runtime / "mai.key").chmod(0o600)

    mai_values = resolve_native_audio_environment(
        "mai", environ={"AUTOSLICE_BASE": str(runtime)}
    )
    assert mai_values == {
        "AZURE_ENDPOINT": "https://resource.services.ai.azure.com",
        "AUTOSLICE_MAI_API_KEY_FILE": str(runtime / "mai.key"),
    }


@pytest.mark.parametrize(
    ("filename", "content", "mode", "reason"),
    [
        ("mai.env", "AZURE_ENDPOINT=https://resource.services.ai.azure.com\n", 0o644,
         "NATIVE_AUDIO_RUNTIME_CONFIG_UNSAFE"),
        ("mai.env", "AZURE_ENDPOINT=https://resource.services.ai.azure.com\n", 0o600,
         "NATIVE_AUDIO_RUNTIME_CONFIG_FIELDS_MISSING"),
    ],
)
def test_runtime_config_failures_are_typed_and_fail_closed(
    tmp_path, filename, content, mode, reason
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / filename).write_text(content, encoding="utf-8")
    (runtime / filename).chmod(mode)
    (runtime / "moss.env").write_text("AUTOSLICE_MOSS_API_KEY=moss\n", encoding="utf-8")
    (runtime / "moss.env").chmod(0o600)

    with pytest.raises(NativeAudioRuntimeError) as caught:
        resolve_native_audio_environment(
            "mai", environ={"AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT": str(runtime)}
        )
    assert caught.value.reason_code == reason


def test_missing_runtime_file_does_not_fall_back_to_ambient(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "moss.env").write_text("AUTOSLICE_MOSS_API_KEY=moss\n", encoding="utf-8")
    (runtime / "moss.env").chmod(0o600)
    with pytest.raises(NativeAudioRuntimeError) as caught:
        resolve_native_audio_environment(
            "mai",
            environ={
                "AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT": str(runtime),
                "AZURE_ENDPOINT": "https://resource.services.ai.azure.com",
                "AZURE_API_KEY": "ambient",
            },
        )
    assert caught.value.reason_code == "NATIVE_AUDIO_RUNTIME_CONFIG_MISSING"


def test_mai_fetch_uses_bound_runtime_without_ambient_credentials(monkeypatch, tmp_path):
    runtime = _runtime(
        tmp_path,
        mai_text=(
            "AZURE_ENDPOINT=https://resource.services.ai.azure.com\n"
            "AZURE_API_KEY=runtime-mai\n"
            "AUTOSLICE_MOSS_API_KEY=other-provider\n"
            "CPA_API_KEY=should-not-be-used\n"
        ),
        moss_text="AUTOSLICE_MOSS_API_KEY=moss\n",
    )
    monkeypatch.delenv("AZURE_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_API_KEY", raising=False)
    monkeypatch.delenv("AUTOSLICE_MOSS_API_KEY", raising=False)
    monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", str(runtime))
    calls = []
    response = _Response({"segments": []})
    monkeypatch.setattr(
        mai.urllib.request,
        "build_opener",
        lambda *handlers: _Opener(response, calls),
    )
    before = dict(os.environ)

    metadata, payload = mai._fetch_payload(b"audio", environment=os.environ)

    assert payload == {"segments": []}
    assert metadata["provider"] == "mai"
    assert calls[0][0].full_url.startswith("https://resource.services.ai.azure.com/")
    assert next(
        value for key, value in calls[0][0].header_items()
        if key.lower() == "ocp-apim-subscription-key"
    ) == "runtime-mai"
    assert dict(os.environ) == before


def test_moss_fetch_uses_bound_runtime_and_not_mai_or_cpa(monkeypatch, tmp_path):
    runtime = _runtime(
        tmp_path,
        mai_text=(
            "AZURE_ENDPOINT=https://resource.services.ai.azure.com\n"
            "AZURE_API_KEY=mai\n"
        ),
        moss_text=(
            "AUTOSLICE_MOSS_API_KEY=runtime-moss\n"
            "AZURE_API_KEY=mai-must-not-leak\n"
            "CPA_API_KEY=cpa-must-not-leak\n"
        ),
    )
    monkeypatch.delenv("AUTOSLICE_MOSS_API_KEY", raising=False)
    monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", str(runtime))
    calls = []
    response = _Response({"segments": []})
    monkeypatch.setattr(
        moss.urllib.request,
        "build_opener",
        lambda *handlers: _Opener(response, calls),
    )

    metadata, payload = moss._fetch_payload(b"audio", environment=os.environ)

    assert payload == {"segments": []}
    assert metadata["provider"] == "moss"
    assert calls[0][0].get_header("Authorization") == "Bearer runtime-moss"
