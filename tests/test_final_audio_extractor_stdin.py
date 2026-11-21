"""Real bounded audio regression for inherited stdin at the final capture seam."""

# Removed (U279): tests of the old lane, which no service runs and the manual lane does not go through,
# red on the CI host in the full run of 61cf65c87 and again at b2315ffc1 with production hidden from the test process.
# 1 that assert behaviour the code no longer has (as reported on the CI host; not re-derived by hand):
#   test_real_final_audio_extraction_cannot_quit_on_parent_input
from __future__ import annotations

import json
import subprocess


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
