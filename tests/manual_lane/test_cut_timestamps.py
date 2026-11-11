"""Real step 2 media checks: a seek can land in a timestamp gap in a recording."""

import json
import os
import shlex
import shutil
import subprocess

import pytest

from fakes import FakeCpa, FakeRemote, make_ctx, root_commands
from src.autoslice.manual_lane.adapters import LocalRemote
from src.autoslice.manual_lane.step_cut import step2_cut


@pytest.mark.parametrize("start_ms,input_seek,exact_seek", [
    (0, 0.0, 0.0), (8000, 0.0, 8.0), (30000, 0.0, 30.0), (9000012, 8970.012, 30.0),
])
def test_cut_seeks_before_input_then_exactly_in_its_relative_clock(tmp_path, start_ms, input_seek, exact_seek):
    def run(name, body, remote):
        if name == "s2-cut":
            remote.files[f"{ctx.remote_dir}/cut/probe.json"] = json.dumps({
                "format": {"duration": "2.690"},
                "streams": [{"codec_type": "video", "width": 160, "height": 90}],
            }).encode()
        from src.autoslice.manual_lane.adapters import RemoteResult

        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=run)
    ctx = make_ctx(tmp_path, remote=remote, cpa=FakeCpa([]))
    ctx.state.data["clip"].update(start_ms=start_ms, end_ms=start_ms + 2690)
    assert step2_cut(ctx).ok
    body = next(body for name, body in remote.scripts if name == "s2-cut")
    args = shlex.split(next(cmd for cmd in root_commands(body) if cmd.startswith("ffmpeg ")))
    seeks = [i for i, arg in enumerate(args) if arg == "-ss"]
    assert len(seeks) == 2
    assert seeks[0] < args.index("-i") < seeks[1] < args.index("-t")
    assert float(args[seeks[0] + 1]) == input_seek
    assert float(args[seeks[1] + 1]) == exact_seek
    assert float(args[seeks[0] + 1]) + float(args[seeks[1] + 1]) == start_ms / 1000
    assert args[args.index("-t") + 1] == "2.690"
    assert args[args.index("-af") + 1] == "aresample=async=1:first_pts=0"
    assert "sudo -n chown" in body             # what the root cut wrote goes back to the lane's user


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="requires ffmpeg/ffprobe")
@pytest.mark.parametrize("gap", [0.16, 0.24, 0.0])
def test_cut_bounds_output_and_preserves_the_clock_across_a_seek_gap(tmp_path, gap, monkeypatch):
    shim = tmp_path / "bin"                    # the cut runs under `sudo -n`: this machine's stand-in runs the command as is
    shim.mkdir()
    (shim / "sudo").write_text('#!/bin/sh\nshift\nexec "$@"\n')
    (shim / "sudo").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    ctx = make_ctx(tmp_path, remote=LocalRemote(scripts_dir=str(tmp_path / "steps")), cpa=FakeCpa([]))
    ctx.remote_dir = str(ctx.clip_dir)
    sources = ctx.clip_dir.parent / "sources"
    sources.mkdir(parents=True)
    source = sources / "src.flv"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=60:duration=5",
        "-itsoffset", str(gap),
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=5",
        "-vsync", "vfr", "-c:v", "libx264", "-preset", "ultrafast", "-g", "60",
        "-c:a", "aac", "-f", "flv", str(source),
    ], check=True, capture_output=True, timeout=30)
    ctx.state.data["clip"].update(start_ms=0, end_ms=2690)
    outcome = step2_cut(ctx)
    assert outcome.ok, (outcome.reason, outcome.note)
    probe = json.loads((ctx.clip_dir / "cut/probe.json").read_text())
    assert abs(float(probe["format"]["duration"]) - 2.690) <= 0.1
    for stream in probe["streams"]:
        assert abs(float(stream["start_time"])) <= 0.05
    # Keep the source clock: fill a missing audio prelude, do not shift later speech earlier.
    audio = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(ctx.clip_dir / "cut/cut.mp4"),
        "-map", "0:a:0", "-t", "0.08", "-f", "f32le", "-ac", "1", "-",
    ])
    if gap:
        import array

        samples = array.array("f", audio)
        assert max(map(abs, samples)) < 0.01
