"""Real bounded audio regression for inherited stdin at the final capture seam."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import wave

import pytest

from src.autoslice import final_subtitle_audio_gate as gate


def test_final_audio_extractor_disables_stdin_at_both_boundaries(tmp_path, monkeypatch):
    """A caller's JSON/PTY must not be interpreted as an FFmpeg command."""
    output = tmp_path / "audio.mp3"
    captured = {}

    def run(argv, **kwargs):
        if argv[0] == "ffprobe":
            assert kwargs["stdin"] == subprocess.DEVNULL
            assert "-count_frames" in argv
            stream = {"codec_type": "audio", "codec_name": "mp3", "duration": "1",
                      "sample_rate": "16000", "channels": 1, "nb_read_frames": "28"}
            return subprocess.CompletedProcess(argv, 0, json.dumps({"streams": [stream]}), "")
        captured.update(argv=argv, kwargs=kwargs)
        output.write_bytes(b"synthetic adapter output")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(gate.subprocess, "run", run)
    gate._default_extract_audio(tmp_path / "synthetic.mp4", output)
    assert "-nostdin" in captured["argv"]
    assert captured["kwargs"].get("stdin") == subprocess.DEVNULL


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="media tools absent")
@pytest.mark.parametrize("parent_input, readrate, protected", [
    (b"q\n", None, True),
    (b'{"candidate":"synthetic","q":true}\n', None, True),
    (b"q\n", 1, True),
    (b'{"candidate":"synthetic","q":true}\n', 1, True),
    (b"q\n", 1, False),
])
def test_real_final_audio_extraction_cannot_quit_on_parent_input(
    tmp_path, parent_input, readrate, protected,
):
    """Real PCM: 120 s fast control or 3 s at real time; no ASR/provider call."""
    source_seconds = 120 if readrate is None else 3
    source = tmp_path / "synthetic.wav"
    with wave.open(str(source), "wb") as out:
        out.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        # Alternating nonzero samples are deterministic and compress to a tiny fixture.
        out.writeframes((b"\x00\x08\x00\xf8" * 8_000) * source_seconds)
    target = tmp_path / "captured.mp3"
    root = Path(__file__).resolve().parents[1]
    # Pace one control input so even fast CI machines reach FFmpeg's keyboard
    # poll. The wrapper changes read speed only, not stdin or the capture API.
    child = (
        "from pathlib import Path\n"
        "import subprocess\n"
        "from src.autoslice.final_subtitle_audio_gate import _default_extract_audio\n"
        "original_run = subprocess.run\n"
        "def paced_run(argv, **kwargs):\n"
        f"    protected = {protected!r}\n"
        "    if not protected:\n"
        "        argv = [arg for arg in argv if arg != '-nostdin']\n"
        "        kwargs.pop('stdin', None)\n"
        f"    rate = {readrate!r}\n"
        "    if rate is not None and argv[0] == 'ffmpeg':\n"
        "        position = argv.index('-i')\n"
        "        argv = argv[:position] + ['-readrate', str(rate)] + argv[position:]\n"
        "    return original_run(argv, **kwargs)\n"
        "subprocess.run = paced_run\n"
        f"_default_extract_audio(Path({str(source)!r}), Path({str(target)!r}))\n"
    )
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
           "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen(
        [sys.executable, "-c", child], cwd=root, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    # This deadline bounds only the test-owned process cleanup, not the product
    # contract.  FFmpeg 8 on macOS can exceed the original eight seconds for a
    # three-second ``-readrate 1`` fixture while the full suite is spawning
    # subprocesses.  Keep a strict but scheduler-tolerant cap; exact completion
    # and full source duration are still asserted below.
    test_deadline_seconds = 20
    try:
        _stdout, stderr = process.communicate(
            parent_input, timeout=test_deadline_seconds
        )
    except subprocess.TimeoutExpired:
        # Reap only this test-owned process group, including its FFmpeg child.
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
        pytest.fail("real audio extraction exceeded the bounded test deadline")
    decoded_stderr = stderr.decode("utf-8", errors="replace")
    if protected:
        assert process.returncode == 0, decoded_stderr
    elif process.returncode != 0:
        # FFmpeg versions differ in how early a keyboard ``q`` is observed.
        # Ubuntu may exit before writing packets (nonzero), while macOS may
        # leave a short but valid MP3 with rc=0. Both are the unsafe behavior
        # this mutation control must demonstrate; the protected cases below
        # must always finish the complete source.
        assert "FINAL_SUBTITLE_AUDIO_FFMPEG_FAILED" in decoded_stderr
        assert not target.is_file() or target.stat().st_size == 0
        print(json.dumps({"readrate": readrate, "protected": protected,
                          "parent_input": parent_input.decode(),
                          "source_seconds": source_seconds,
                          "duration_seconds": None,
                          "extract_rc": process.returncode}))
        return
    inspected = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(target)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False,
    )
    assert inspected.returncode == 0, inspected.stderr
    duration = float(json.loads(inspected.stdout)["format"]["duration"])
    observation = {"readrate": readrate, "protected": protected,
                   "parent_input": parent_input.decode(), "source_seconds": source_seconds,
                   "duration_seconds": duration, "extract_rc": process.returncode}
    print(json.dumps(observation))
    if protected:
        assert source_seconds - 0.1 <= duration <= source_seconds + 0.2, observation
    else:
        # Mutation control removes only the two protections, reproducing the
        # original stdin behavior on the same 3 s input and real FFmpeg.
        assert duration < source_seconds - 0.2, observation
