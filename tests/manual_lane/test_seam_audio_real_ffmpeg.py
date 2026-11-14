"""The audio filters of a cut have the product's clock (source_read.source_cut_script): time 0 is the first frame of the
piece, whatever coarse seek the reader takes before the cut point.  Real ffmpeg on a synthetic recording (a steady tone
over a black picture, made here with lavfi); the media host's `sudo -n` is the only thing replaced (by nothing).  Skipped
only when ffmpeg or ffprobe is not installed.

Fact it holds: with the preroll of 30 s in front of the cut and the fades timed from the piece's start, the fade-out
used to land one preroll early and everything after it was digital silence (-91 dB), the fade-in landed in the part
thrown away."""

from __future__ import annotations

import array
import shutil
import subprocess
from pathlib import Path

import pytest

from src.autoslice.manual_lane import compilation as comp
from src.autoslice.manual_lane import source_read
from src.autoslice.manual_lane.adapters import LocalRemote, RemoteResult

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")

RATE = 48000
FADE = comp.SEAM_FADE_S


class _NoSudoRemote(LocalRemote):
    """The local machine as the media host: the root wrapper's `sudo -n` runs the same command as this user."""

    def run_script(self, *, name: str, body: str, timeout: int | None) -> RemoteResult:
        return super().run_script(name=name, body=body.replace("sudo -n ", ""), timeout=timeout)


def _make_source(path: Path, seconds: int = 140) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={RATE}:duration={seconds}",
                    "-f", "lavfi", "-i", f"color=c=black:s=160x90:r=30:d={seconds}",
                    "-c:v", "libx264", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "flv", str(path)],
                   check=True, capture_output=True)


def _pcm(path: Path) -> array.array:
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-map", "0:a:0",
                          "-ac", "2", "-ar", str(RATE), "-f", "s16le", "-"], check=True, capture_output=True).stdout
    samples = array.array("h")
    samples.frombytes(raw)
    return samples


def _rms(samples: array.array, start_s: float, end_s: float) -> float:
    window = samples[2 * round(start_s * RATE):2 * round(end_s * RATE)]          # interleaved stereo
    assert len(window) > 0
    return (sum(s * s for s in window) / len(window)) ** 0.5


def _cut(tmp_path: Path, spans_s: list[tuple[float, float]]):
    source = tmp_path / "rec.flv"
    _make_source(source)
    cuts = [comp.PieceCut("rec.flv", str(source), round(a * 1000), round(b * 1000)) for a, b in spans_s]
    work = tmp_path / "work"
    durations, total = comp.build_media(_NoSudoRemote(scripts_dir=str(tmp_path / "scripts")), cuts,
                                        work=str(work), out=str(tmp_path / "out" / "joined.flv"))
    return [work / "pieces" / f"p{i}.mp4" for i in range(len(cuts))], durations, total


def test_the_fades_sit_at_the_head_and_tail_of_each_piece_and_nothing_else_is_silent(tmp_path):
    spans = [(50.0, 54.0), (70.0, 73.0)]                  # both starts are beyond the 30 s preroll
    assert spans[0][0] > source_read.PREROLL_S
    pieces, durations, total = _cut(tmp_path, spans)
    first, second = _pcm(pieces[0]), _pcm(pieces[1])
    body = _rms(first, 1.0, 2.0)
    assert body > 1000                                    # the tone itself (-91 dB would be about 1)
    # piece 0 (first): no fade-in; steady until its fade-out in the last 15 ms; the fade-out lands there
    assert _rms(first, 0.0, 0.005) > 0.8 * body
    assert min(_rms(first, t, t + 0.1) for t in (0.5, 1.5, 2.5, 3.5, 3.85)) > 0.8 * body
    tail = len(first) / 2 / RATE
    assert _rms(first, tail - 0.004, tail) < 0.5 * body
    # piece 1 (last): fade-in at the head, steady to the very end, no fade-out
    assert _rms(second, 0.0, 0.005) < 0.5 * body
    assert min(_rms(second, t, t + 0.1) for t in (0.1, 1.0, 2.0, 2.85)) > 0.8 * body
    assert _rms(second, len(second) / 2 / RATE - 0.1, len(second) / 2 / RATE - 0.02) > 0.8 * body
    # the joined clip has the sum of the pieces and no silence
    assert abs(total - sum(durations)) <= comp.CONCAT_TOLERANCE_MS
    assert abs(sum(durations) - 7000) <= 60


def test_the_cut_point_is_exact_with_and_without_a_preroll(tmp_path):
    """A recording whose tone alternates between a quiet and a loud level every 10 s, and a cut 100 ms before a change: the
    level changes about 100 ms into the piece, whether the coarse seek lands 30 s before the cut point (59.9 s) or is
    clipped at the recording's start (9.9 s).  A cut that is a second off, or a clock shifted by the preroll, shows at once."""
    source = tmp_path / "steps.flv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "aevalsrc='(0.1+0.4*mod(floor(t/10),2))*sin(2*PI*440*t)':s=48000:d=100", "-f", "lavfi", "-i",
                    "color=c=black:s=160x90:r=30:d=100", "-c:v", "libx264", "-g", "30", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-f", "flv", str(source)], check=True, capture_output=True)
    remote = _NoSudoRemote(scripts_dir=str(tmp_path / "scripts"))
    for start, ratio in ((59.9, 5.0), (9.9, 0.2)):           # (loud then quiet: 0.5 / 0.1), (quiet then loud)
        out = tmp_path / f"cut{start}.mp4"
        script = source_read.source_cut_script(str(source), start_s=start, duration_s=2.0, output=str(out),
                                               fade=f"afade=t=out:st=1.985:d={FADE}")
        assert remote.run_script(name=f"cut{start}", body=script, timeout=300).rc == 0
        samples = _pcm(out)
        assert _rms(samples, 0.02, 0.08) / _rms(samples, 0.14, 0.2) == pytest.approx(ratio, rel=0.2)
        assert _rms(samples, 1.9, 1.98) > 0.5 * _rms(samples, 0.5, 1.0)        # the fade-out is at the tail, not before


def test_the_audio_check_of_step_8_finds_the_silent_stretch_the_old_cut_made(tmp_path):
    """The host tool (audio_silences.py) on a real clip: a tone, 6 s of digital silence, a tone.  The stretch comes back, and
    the subtitle that lies whole in it is the fault; a subtitle that has the tone in part of it, and the quiet between two
    subtitles, are not."""
    import json
    import sys

    from src.autoslice.manual_lane import package_audio

    clip = tmp_path / "final.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc='if(between(t,4,10),0,0.3*sin(2*PI*440*t))':s={RATE}:d=14", "-f", "lavfi", "-i",
                    "color=c=black:s=160x90:r=30:d=14", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(clip)], check=True, capture_output=True)
    tool = Path(__file__).resolve().parents[2] / "assets/manual_lane/remote/audio_silences.py"
    run = subprocess.run([sys.executable, "-I", str(tool), str(clip), "-80", "0.8"], check=True, capture_output=True, text=True)
    found = json.loads(run.stdout)
    assert len(found["silences"]) == 1 and abs(found["silences"][0][0] - 4.0) < 0.15 and abs(found["silences"][0][1] - 10.0) < 0.15
    cues = [{"id": 1, "text": "a", "start_s": 0.5, "end_s": 3.5}, {"id": 2, "text": "b", "start_s": 4.2, "end_s": 9.8},
            {"id": 3, "text": "c", "start_s": 9.0, "end_s": 12.0}, {"id": 4, "text": "d", "start_s": 5.0, "end_s": 5.5}]
    lines = package_audio.silent_lines(cues, found["silences"], offset_s=0.0, min_subtitle_s=1.0, edge_tolerance_s=0.1)
    assert [x["number"] for x in lines] == [2]                    # 3 has sound from 10 s on, 4 is under a second, 1 is before
    missing = subprocess.run([sys.executable, "-I", str(tool), str(tmp_path / "none.mp4"), "-80", "0.8"], capture_output=True, text=True)
    assert missing.returncode != 0 and "ffmpeg rc=" in missing.stderr
