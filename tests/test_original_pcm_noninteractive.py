"""A caller's JSON/PTY stdin must not become interactive FFmpeg commands."""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from src.autoslice import original_patch_package as owner


def test_pcm_command_explicitly_disconnects_interactive_input(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "SHA256=" + "a" * 64 + "\n", "")

    monkeypatch.setattr(owner.subprocess, "run", run)
    assert owner._pcm_hash(Path("fixture.wav")) == "SHA256=" + "a" * 64
    command, kwargs = calls[0]
    assert "-nostdin" in command
    assert kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="actual FFmpeg unavailable")
def test_real_pcm_full_hash_and_parent_json_survive_q_on_stdin(tmp_path):
    payload = b"".join(struct.pack("<h", (i * 997) % 60001 - 30000) for i in range(32000))
    media = tmp_path / "two_seconds.wav"
    with wave.open(str(media), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(payload)
    code = (
        "import sys\n"
        "from src.autoslice.original_patch_package import _pcm_hash\n"
        "print(_pcm_hash(sys.argv[1]))\n"
        'print(sys.stdin.read(), end="")\n'
    )
    incoming = 'q\n{"quote": "must remain caller input"}\n'
    result = subprocess.run(
        [sys.executable, "-c", code, str(media)],
        input=incoming,
        capture_output=True,
        text=True,
        timeout=20,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "SHA256=" + hashlib.sha256(payload).hexdigest() + "\n" + incoming
