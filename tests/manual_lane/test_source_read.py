"""Reading a recording on the media host has one definition (source_read.py): where the recording is (the clip's own
record), who reads it (root, the product handed back), how it is encoded (one set of arguments), and what is said before
any cut when the file is not there.  The first cut, the pieces of a compilation and the pieces of a trim are checked
against it, and a scan of the lane's sources keeps any other module from putting a recording into an ffmpeg input.
No ssh, ffmpeg or network: the remote is the fake of fakes.py and records the script text it is given."""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
from fakes import FakeCpa, FakeRemote, make_ctx, root_commands
from test_compilation import ROOT, SOURCES, _adapters, _answer, _run_compile, _trim_pieces
from test_trim_run import X, Y, answer, make_x
from test_trim_run import _script as trim_script

from src.autoslice.manual_lane import source_read, trim_run
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.state import ClipState
from src.autoslice.manual_lane.step_cut import step2_cut

REPO = Path(__file__).resolve().parents[2]
SCANNED = ("src/autoslice/manual_lane", "assets/manual_lane/remote")


def _existing(*paths: str, inner=None):
    """A fake remote's script handler: the existence check (as root: `test -s <path>`) is true for `paths` only; any other
    script goes to `inner` (default: succeeds)."""
    def on_script(name, body, remote):
        if name == "source-exists":
            (command,) = root_commands(body)
            return RemoteResult(0 if shlex.split(command)[-1] in paths else 1, "", "")
        if inner is not None:
            return inner(name, body, remote)
        return RemoteResult(0, "", "")
    return on_script


def _cut_ctx(tmp_path, *paths: str, record: str | None = None, requested: str | None = None):
    def probe(name, body, remote):
        if name == "s2-cut":
            remote.files[f"{ctx.remote_dir}/cut/probe.json"] = json.dumps({
                "format": {"duration": "2.690"}, "streams": [{"codec_type": "video", "width": 160, "height": 90}]}).encode()
        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=_existing(*paths, inner=probe))
    ctx = make_ctx(tmp_path, remote=remote, cpa=FakeCpa([]))
    ctx.state.data["clip"].update(start_ms=0, end_ms=2690)
    if record:
        ctx.state.data["clip"]["source_dir"] = record
    ctx.source_dir = requested
    return ctx


def _bodies(ctx, *names: str) -> dict[str, str]:
    return {name: body for name, body in ctx.adapters.remote.scripts if name in names}


def _encoding(command: str) -> list[str]:
    """The encoder arguments of an ffmpeg command: from the stream mapping to the output path, the seam fade (and the clock
    moved around it) taken out of the audio filter (it is the one thing that differs between the pieces of a joined clip)."""
    words = shlex.split(command)
    words[words.index("-af") + 1] = words[words.index("-af") + 1].split(",asetpts")[0]
    del words[words.index("-t"):words.index("-t") + 2]                       # the length is the cut's own
    return words[words.index("-map"):-1]


# ----- the three readers: one wrapper, one encoding, the recorded place ------------------------------------------------
def test_the_first_cut_a_compilation_and_a_trim_read_as_root_with_one_encoding(tmp_path):
    for sub in "abc":
        (tmp_path / sub).mkdir()
    ctx = _cut_ctx(tmp_path / "a", "/rec/dir/src.flv", record="/rec/dir")
    assert step2_cut(ctx).ok
    first = _bodies(ctx, "s2-cut", "s2-snap")
    adapters, remote, _cpa = _adapters(tmp_path / "b", [_answer((2, 5, 8), (1, 12, 14))])
    _run_compile(tmp_path / "b", adapters)
    adapters_t, remote_t, _cpa_t = _adapters(tmp_path / "c", [])
    _trim_pieces(tmp_path / "c", adapters_t)
    scripts = [first["s2-cut"], dict(remote.scripts)["compile-cut"], dict(remote_t.scripts)["compile-cut"]]

    encodings = []
    for body in scripts:
        commands = [c for c in root_commands(body) if c.startswith("ffmpeg ")]
        assert commands and body.count("sudo -n bash -c '") == len(commands)         # every cut is a root command
        assert body.count("sudo -n chown -R ") == len(commands)                        # ... and hands its product back
        assert "COMPILATION_PIECE_FAILED" in body or body is scripts[0]
        encodings += [_encoding(c) for c in commands]
    assert len(encodings) == 5 and all(e == encodings[0] for e in encodings)
    assert encodings[0] == ["-map", "0:v:0", "-map", "0:a:0", *source_read.VIDEO_ENCODE, "-af", source_read.AUDIO_FILTER,
                            *source_read.AUDIO_ENCODE, "-movflags", "+faststart"]
    # where the recording is read from: the clip's record; the compilations' given directory
    assert "-i /rec/dir/src.flv" in root_commands(first["s2-cut"])[0] and "-i /rec/dir/src.flv" in first["s2-snap"].replace("'\\''", "")
    assert f"-i {SOURCES}/rec-b.flv " in root_commands(scripts[1])[0] and f"-i {SOURCES}/rec-a.flv " in root_commands(scripts[2])[0]


def test_the_silence_probe_reads_as_root_from_the_recorded_directory(tmp_path):
    ctx = _cut_ctx(tmp_path, "/rec/dir/src.flv", record="/rec/dir")
    assert step2_cut(ctx).ok
    (probe,) = root_commands(_bodies(ctx, "s2-snap")["s2-snap"])
    assert probe.count("ffmpeg ") == 2 and probe.count("-i /rec/dir/src.flv") == 2 and "silencedetect" in probe
    assert "sudo -n chown -R " in _bodies(ctx, "s2-snap")["s2-snap"]


def test_a_quote_in_a_path_survives_the_root_wrapper(tmp_path, monkeypatch):
    shim = tmp_path / "bin"                  # `sudo -n` stands in as the plain command
    shim.mkdir()
    (shim / "sudo").write_text('#!/bin/sh\nshift\nexec "$@"\n')
    (shim / "sudo").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    quoted = tmp_path / "it's.flv"
    quoted.write_text("x")
    script = tmp_path / "check.sh"
    script.write_text(source_read.source_exists_script(str(quoted)))
    assert subprocess.run(["bash", str(script)], check=False).returncode == 0
    script.write_text(source_read.source_exists_script(str(tmp_path / "none's.flv")))
    assert subprocess.run(["bash", str(script)], check=False).returncode == 1


# ----- where the recording is: record, this run's argument, the default ------------------------------------------------
def test_the_recorded_directory_is_used_without_any_argument(tmp_path):
    ctx = _cut_ctx(tmp_path, "/rec/dir/src.flv", "/r/sources/src.flv", record="/rec/dir")
    assert step2_cut(ctx).ok
    assert "-i /rec/dir/src.flv" in root_commands(_bodies(ctx, "s2-cut")["s2-cut"])[0]


def test_a_clip_without_a_record_takes_the_argument_and_keeps_it(tmp_path):
    ctx = _cut_ctx(tmp_path, "/given/src.flv", requested="/given")
    assert "source_dir" not in ctx.state.data["clip"]
    assert step2_cut(ctx).ok
    assert "-i /given/src.flv" in root_commands(_bodies(ctx, "s2-cut")["s2-cut"])[0]
    assert ClipState.load(ctx.state.path).data["clip"]["source_dir"] == "/given"       # on disk: the next run needs no argument


def test_a_clip_without_record_or_argument_is_looked_for_in_the_default_directory_and_keeps_it(tmp_path):
    ctx = _cut_ctx(tmp_path, "/r/sources/src.flv")
    assert step2_cut(ctx).ok
    assert ClipState.load(ctx.state.path).data["clip"]["source_dir"] == "/r/sources"


def test_the_argument_is_tried_when_the_record_holds_no_file_and_the_record_stays(tmp_path):
    ctx = _cut_ctx(tmp_path, "/given/src.flv", record="/old", requested="/given")
    assert step2_cut(ctx).ok
    assert "-i /given/src.flv" in root_commands(_bodies(ctx, "s2-cut")["s2-cut"])[0]
    assert ClipState.load(ctx.state.path).data["clip"].get("source_dir") == "/old"


def test_no_file_anywhere_stops_before_any_cut_and_names_the_clip_and_the_directories(tmp_path):
    ctx = _cut_ctx(tmp_path, record="/old", requested="/given")
    outcome = step2_cut(ctx)
    assert not outcome.ok and outcome.reason == "SOURCE_RECORDING_MISSING"
    for expected in ("clip", "src.flv", "/old", "/given", "/r/sources"):
        assert expected in outcome.note
    assert {name for name, _ in ctx.adapters.remote.scripts} == {"source-exists"}      # no snap, no cut


def test_a_compilation_without_the_recording_stops_before_any_cut(tmp_path):
    adapters, remote, _cpa = _adapters(tmp_path, [_answer((2, 5, 8), (1, 12, 14))])
    remote.on_script = _existing(inner=remote.on_script)
    with pytest.raises(source_read.SourceMissing) as err:
        _run_compile(tmp_path, adapters)
    assert "comp" in str(err.value) and SOURCES in str(err.value) and "rec-b.flv" in str(err.value)
    assert "compile-cut" not in [name for name, _ in remote.scripts]


# ----- the trim: X's record is where its pieces are cut from -----------------------------------------------------------
def _trim_script_with_files(*paths: str):
    return _existing(*paths, inner=trim_script())


def test_a_trim_cuts_from_the_directory_the_original_records_and_the_successor_keeps_its_own(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]),
                 script=_trim_script_with_files("/elsewhere/sources/src.flv", f"{ROOT}/sources/compilation-clip_trim.flv"))
    ctx.state.data["clip"]["source_dir"] = "/elsewhere/sources"
    ctx.state.save()
    result = trim_run.trim_clip(task=tmp_path, cid=X, adapters_for=lambda _rd: ctx.adapters)      # no --source-dir
    assert result["status"] == "TRIMMED", result
    manifest = json.loads((tmp_path / "clips/clip_trim/compilation.json").read_text(encoding="utf-8"))
    assert manifest["recordings"][0]["path"] == "/elsewhere/sources/src.flv"
    assert "-i /elsewhere/sources/src.flv" in root_commands(dict(ctx.adapters.remote.scripts)["compile-cut"])[0]
    y = ClipState.load(tmp_path / "state/clip_trim.json").data["clip"]
    assert y["source_dir"] == f"{ROOT}/sources" and y["source_name"] == "compilation-clip_trim.flv"
    assert "-i /r/sources/compilation-clip_trim.flv" in root_commands(_bodies(ctx, "s2-cut")["s2-cut"])[0]


def test_a_trim_records_the_directory_it_was_given_in_the_original(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]),
                 script=_trim_script_with_files("/given/src.flv", f"{ROOT}/sources/compilation-clip_trim.flv"))
    result = trim_run.trim_clip(task=tmp_path, cid=X, adapters_for=lambda _rd: ctx.adapters, source_dir="/given")
    assert result["status"] == "TRIMMED", result
    assert ClipState.load(tmp_path / "state/clip.json").data["clip"]["source_dir"] == "/given"
    assert "-i /given/src.flv" in root_commands(dict(ctx.adapters.remote.scripts)["compile-cut"])[0]


def test_a_trim_without_the_recording_fails_before_any_cut_naming_the_clip(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]), script=_trim_script_with_files())
    result = trim_run.trim_clip(task=tmp_path, cid=X, adapters_for=lambda _rd: ctx.adapters)
    assert result["status"] == "FAILED"
    assert "clip" in result["reason"] and "/r/sources" in result["reason"] and "src.flv" in result["reason"]
    assert "compile-cut" not in [name for name, _ in ctx.adapters.remote.scripts]
    assert not (tmp_path / "state/clip_trim.json").exists() and Y not in result["reason"]


# ----- the scan: no other module puts a recording into an ffmpeg input -------------------------------------------------
RECORDING_NAME = re.compile(r"source|src|recording", re.IGNORECASE)         # what a recording is called where it is a variable
SHELL_INPUT = re.compile(r"""-i\s+["']?([^\s"']+)""")


def _inputs(path: Path) -> list[tuple[int, str]]:
    """Every ffmpeg `-i <expression>` of a file as (line, expression text): the argument after a "-i" element of an argv
    list, and the `{expression}` after "-i " inside an f-string; for a shell script the word after -i."""
    text = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        return [(text.count("\n", 0, m.start()) + 1, m.group(1)) for m in SHELL_INPUT.finditer(text)]
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.List, ast.Tuple)):
            for here, after in zip(node.elts, node.elts[1:]):
                if isinstance(here, ast.Constant) and here.value == "-i":
                    found.append((after.lineno, ast.unparse(after)))
        elif isinstance(node, ast.JoinedStr):
            for here, after in zip(node.values, node.values[1:]):
                if (isinstance(here, ast.Constant) and isinstance(here.value, str) and re.search(r"-i\s*$", here.value)
                        and isinstance(after, ast.FormattedValue)):
                    found.append((after.lineno, ast.unparse(after.value)))
    return found


def _recording_inputs(root: Path, *, skip: str = "") -> list[str]:
    hits = []
    for scanned in SCANNED:
        for path in sorted((root / scanned).rglob("*")):
            if path.suffix in {".py", ".sh"} and path.name != skip:
                hits += [f"{path.relative_to(root)}:{line}: -i {expr}" for line, expr in _inputs(path)
                         if RECORDING_NAME.search(expr)]
    return hits


def test_only_source_read_puts_a_recording_into_an_ffmpeg_input():
    assert _recording_inputs(REPO, skip="source_read.py") == []
    # the scan sees what it should: the definition itself has the recording as input
    assert len(_recording_inputs(REPO)) == 2
    # ... and the lane's other readers (the cut product, never the recording) are in the scan's view at all
    assert any(expr in {"cut", "cut_video", "$CUT", '"$CUT"'} or "cut" in expr
               for path in (REPO / "src/autoslice/manual_lane").glob("*.py") for _line, expr in _inputs(path))


def test_the_scan_reports_a_recording_input_written_anywhere_else(tmp_path):
    lane = tmp_path / "src/autoslice/manual_lane"
    lane.mkdir(parents=True)
    (tmp_path / "assets/manual_lane/remote").mkdir(parents=True)
    (lane / "stray.py").write_text('cmd = f"ffmpeg -y -i {src} -t 1 out.mp4"\nargv = ["ffmpeg", "-i", str(source_video)]\n'
                                   'ok = f"ffmpeg -i {cut}"\n')
    (tmp_path / "assets/manual_lane/remote/stray.sh").write_text('ffmpeg -i "$RECORDING" out.mp3\nffmpeg -i "$CUT" ok.mp3\n')
    assert sorted(hit.split(": ", 1)[1] for hit in _recording_inputs(tmp_path)) == [
        "-i $RECORDING", "-i src", "-i str(source_video)"]
