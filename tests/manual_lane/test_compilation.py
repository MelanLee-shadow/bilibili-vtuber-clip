"""Same-session compilation entry (`manual_lane compile`): CPA's choice, the mechanical checks, the cut points, the joined
clock, the danmaku remap, and the whole entry through the CLI with a fake remote and a fake CPA.  No ssh, ffmpeg or
network.  The cue text is synthetic."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest
from fakes import FakeCpa, FakeRemote, root_commands

from src.autoslice.manual_lane import cli, parallel, source_read
from src.autoslice.manual_lane import compilation as comp
from src.autoslice.manual_lane.adapters import Adapters, RemoteResult
from src.autoslice.manual_lane.lane_rules import rules
from src.autoslice.manual_lane.srt_text import SrtCue, fmt_ts
from src.autoslice.manual_lane.state import ClipState

ROOT = "/r"                                   # the compilation's work directory on the (fake) media host
SOURCES = "/r/rec"                            # where the recordings are


def _cues(n: int, *, step: float = 3.0, length: float = 2.0, offset: float = 0.0) -> list[SrtCue]:
    return [SrtCue(id=i, text=f"句子{i}", start_s=offset + (i - 1) * step, end_s=offset + (i - 1) * step + length)
            for i in range(1, n + 1)]


def _srt(cues: list[SrtCue]) -> str:
    return "\n".join(f"{c['id']}\n{fmt_ts(c['start_s'])} --> {fmt_ts(c['end_s'])}\n{c['text']}\n" for c in cues)


def _answer(*segments, compilation=True, hook="一句看点", reason="synthetic") -> str:
    return json.dumps({"compilation": compilation, "reason": reason, "hook": hook if compilation else "",
                       "segments": [{"segment": s, "start_cue": a, "end_cue": b, "why": f"w{i}"}
                                    for i, (s, a, b) in enumerate(segments)] if compilation else []}, ensure_ascii=False)


def _parse(text: str, *cue_lists):
    return comp.parse_selection(text, cue_lists or (_cues(20), _cues(20)))


# ----- CPA's choice and the mechanical checks ---------------------------------------------------------------------
def test_disjoint_intervals_are_kept_in_cpa_order_across_recordings():
    chosen = _parse(_answer((2, 5, 8), (1, 12, 14), (2, 1, 2), (1, 2, 3)))
    assert chosen.compilation and chosen.hook == "一句看点"
    assert [(s["segment"], s["start_cue"], s["end_cue"]) for s in chosen.segments] == [
        (2, 5, 8), (1, 12, 14), (2, 1, 2), (1, 2, 3)]                  # CPA's order, not recording order


def test_no_quota_on_how_many_intervals():
    chosen = _parse(_answer(*[(1, i, i) for i in range(1, 21, 2)]))     # ten one-cue intervals
    assert len(chosen.segments) == 10


def test_cpa_may_answer_that_the_session_holds_no_compilation():
    chosen = _parse(_answer(compilation=False, reason="素材不够"))
    assert not chosen.compilation and chosen.segments == () and chosen.reason == "素材不够"


@pytest.mark.parametrize("text,code", [
    (_answer((1, 5, 99)), "COMPILATION_CUE_UNKNOWN"),
    (_answer((1, 0, 3)), "COMPILATION_CUE_UNKNOWN"),
    (_answer((3, 1, 2)), "COMPILATION_SEGMENT_UNKNOWN"),
    (_answer((1, 9, 4)), "COMPILATION_CUE_ORDER"),
    (_answer((1, 3, 6), (2, 1, 2), (1, 6, 8)), "COMPILATION_OVERLAP"),
    (_answer((1, 4, 6), (1, 4, 6)), "COMPILATION_OVERLAP"),
    ('{"compilation": "yes"}', "COMPILATION_ANSWER_SHAPE"),
    (_answer(), "COMPILATION_ANSWER_SHAPE"),                                  # true with nothing chosen
    (_answer((1, 1, 2), hook=""), "COMPILATION_ANSWER_SHAPE"),
    (json.dumps({"compilation": True, "hook": "h", "segments": [{"segment": 1, "start_cue": "x", "end_cue": 2}]}),
     "COMPILATION_ANSWER_SHAPE"),
    ("没有 JSON", "COMPILATION_ANSWER_UNPARSEABLE"),
])
def test_an_answer_the_code_cannot_use_is_refused_with_its_original_reason(text, code):
    with pytest.raises(comp.CompilationError) as err:
        _parse(text)
    assert err.value.code == code


def test_touching_intervals_are_not_an_overlap():
    assert len(_parse(_answer((1, 3, 5), (1, 6, 8))).segments) == 2


# ----- cut points ------------------------------------------------------------------------------------------------------
def test_cut_points_follow_the_cue_edges_with_a_head_and_tail_margin():
    start_ms, end_ms = comp.cut_window_ms(_cues(10), 3, 5)               # cues 3..5: 6.0 s .. 14.0 s, gaps of 1 s
    assert (start_ms, end_ms) == (round((6.0 - comp.HEAD_MARGIN_S) * 1000), round((14.0 + comp.TAIL_MARGIN_S) * 1000))


def test_a_margin_never_reaches_past_the_middle_of_the_gap_to_the_next_cue():
    cues = _cues(10, step=2.1, length=2.0)                                # 0.1 s gaps
    start_ms, end_ms = comp.cut_window_ms(cues, 3, 5)                    # kept 4.2 .. 10.4
    assert (start_ms, end_ms) == (4150, 10450)


def test_a_recording_edge_has_no_neighbour_to_clamp_to():
    assert comp.cut_window_ms(_cues(4), 1, 4)[0] == 0


# ----- the joined clock and the real intervals -----------------------------------------------------------------------
def _fake_media(remote: FakeRemote, *, jitter_ms: int = 12, shapes=None):
    """What ffmpeg/ffprobe on the media host would print for these scripts: each recut piece lasts its `-t` (plus
    a few ms of encoder tail), the joined file lasts the sum."""
    def probe(seconds: float, width: int = 1920) -> str:
        return json.dumps({"format": {"duration": f"{seconds:.6f}"}, "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": width, "height": 1080, "pix_fmt": "yuv420p",
             "r_frame_rate": "60/1"}, {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2}]})

    def on_script(name, body, rem):
        if name == "compile-cut":
            spans = [float(m) for m in re.findall(r" -t (\d+\.\d+) ", body)]
            return RemoteResult(0, "".join(f"PROBE {i} {probe(s + jitter_ms / 1000, (shapes or {}).get(i, 1920))}\n"
                                           for i, s in enumerate(spans)), "")
        if name == "compile-concat":
            seconds = sum(float(d) for d in re.findall(r"^duration (\d+\.\d+)$", body, re.M))
            return RemoteResult(0, f"PROBE final {probe(seconds)}\n", "")
        if name == "compile-asr":
            return RemoteResult(0, "ASR_OK\n", "")
        return RemoteResult(0, "", "")
    remote.on_script = on_script


def _virtual_bcut(n: int = 30) -> str:
    return _srt(_cues(n, step=2.0, length=1.8))


def _adapters(tmp_path, responses, *, xml_by_stem=None):
    remote = FakeRemote()
    _fake_media(remote)
    remote.files[f"{ROOT}/comp/compilation/asr/bcut.srt"] = _virtual_bcut().encode()

    class Danmaku:
        def __init__(self):
            self.calls = []

        def names(self, remote_dir):
            return []

        def fetch(self, remote_path):
            self.calls.append(remote_path)
            return (xml_by_stem or {}).get(Path(remote_path).stem, "<i></i>").encode()

    cpa = FakeCpa(responses)
    adapters = Adapters(remote=remote, cpa=cpa, danmaku=Danmaku())
    return adapters, remote, cpa


def _task(tmp_path):
    """Two recordings with their original BCUT files, and a session config."""
    names = ["rec-a.flv", "rec-b.flv"]
    for name, n in zip(names, (40, 30)):
        (tmp_path / f"{name}.srt").write_text(_srt(_cues(n)), encoding="utf-8")
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"recording_date": "2026-10-24", "participants": [["host"]], "own_uploader_mids": [1]}))
    return names, session


def _run_compile(tmp_path, adapters, answer_theme="theme", cid="comp"):
    from src.autoslice.manual_lane.config import load_session_config

    names, session = _task(tmp_path)
    return comp.compile_session(
        task=tmp_path / "task", cid=cid, theme=answer_theme, session=load_session_config(session),
        recordings=[comp.RecordingInput(n, tmp_path / f"{n}.srt") for n in names], source_dir=SOURCES, remote_root=ROOT,
        adapters=adapters)


def test_compile_session_cuts_two_recordings_and_writes_the_joined_clock(tmp_path):
    adapters, remote, cpa = _adapters(tmp_path, [_answer((2, 5, 8), (1, 12, 14))])
    # the entry's remote paths are fixed by the cid; the BCUT of the joined media sits where the ASR tool leaves it
    remote.files[f"{ROOT}/comp/compilation/asr/bcut.srt"] = _virtual_bcut().encode()
    result = _run_compile(tmp_path, adapters)
    assert result.compilation and result.manifest is not None
    pieces = result.manifest["pieces"]
    assert [(p["recording"], p["start_cue"], p["end_cue"]) for p in pieces] == [("rec-b.flv", 5, 8), ("rec-a.flv", 12, 14)]
    # cues 5..8 of recording b: 12.0 .. 23.0 s; margins 0.12 / 0.35; then cues 12..14 of a: 33.0 .. 41.0 s
    assert (pieces[0]["source_start_ms"], pieces[0]["source_end_ms"]) == (11880, 23350)
    assert (pieces[1]["source_start_ms"], pieces[1]["source_end_ms"]) == (32880, 41350)
    # the clock uses the measured durations, one after the other
    assert [p["measured_ms"] for p in pieces] == [11482, 8482]
    assert [(p["compiled_start_ms"], p["compiled_end_ms"]) for p in pieces] == [(0, 11482), (11482, 19964)]
    assert result.manifest["duration_ms"] == 19964
    saved = comp.load_manifest(tmp_path / "task/clips/comp")
    assert saved == result.manifest and saved is not None and saved["virtual_path"] == f"{ROOT}/sources/compilation-comp.flv"
    # the recordings are read from SOURCES, the joined file is written under the work directory's sources/
    cut_body = dict(remote.scripts)["compile-cut"]
    assert f"{SOURCES}/rec-b.flv" in cut_body and f"{SOURCES}/rec-a.flv" in cut_body and "ffmpeg" in cut_body
    concat_body = dict(remote.scripts)["compile-concat"]
    assert f"{ROOT}/sources/compilation-comp.flv" in concat_body and "-f flv" in concat_body
    assert cpa.calls == ["compilation-select"]


def test_the_compilation_looks_like_one_ordinary_candidate_to_init(tmp_path):
    adapters, _remote, _cpa = _adapters(tmp_path, [_answer((1, 3, 6), (2, 9, 12))])
    result = _run_compile(tmp_path, adapters)
    assert result.selection_file is not None and result.bcut_srt is not None
    entry = json.loads(result.selection_file.read_text(encoding="utf-8"))["input_pool"][0]
    assert entry["cid"] == "comp" and entry["start_ms"] == 0 and entry["hook"] == "一句看点"
    assert Path(entry["segment_path"]).stem + ".flv" == "compilation-comp.flv"
    assert entry["selection_scorecard"] == {"start_cue": 1, "end_cue": 30}
    assert entry["end_ms"] == result.manifest["duration_ms"]


def test_the_prompt_carries_the_theme_and_every_recording_without_a_quota(tmp_path):
    adapters, _remote, cpa = _adapters(tmp_path, [_answer((1, 1, 2))])
    _run_compile(tmp_path, adapters, answer_theme="主题词")
    prompt = cpa.prompts[0]
    assert "主题词" in prompt and "== 段 1 ==" in prompt and "== 段 2 ==" in prompt
    assert "句子40" in prompt and "{transcript}" not in prompt and "{theme}" not in prompt
    for forbidden in ("至少", "至多", "最多", "不少于"):       # the prompt sets no count or length bound
        assert forbidden not in prompt


def test_a_session_that_is_not_a_compilation_cuts_nothing(tmp_path):
    adapters, remote, cpa = _adapters(tmp_path, [_answer(compilation=False, reason="素材不够")])
    result = _run_compile(tmp_path, adapters)
    assert not result.compilation and result.reason == "素材不够"
    assert remote.scripts == [] and not (tmp_path / "task/clips/comp/compilation.json").exists()
    assert cpa.calls == ["compilation-select"]


def test_an_illegal_answer_stops_with_its_reason_before_any_media_work(tmp_path):
    adapters, remote, _cpa = _adapters(tmp_path, [_answer((1, 5, 99))])
    with pytest.raises(comp.CompilationError) as err:
        _run_compile(tmp_path, adapters)
    assert err.value.code == "COMPILATION_CUE_UNKNOWN" and remote.scripts == []


def test_a_failed_cpa_call_is_reported_as_it_is(tmp_path):
    adapters, remote, _cpa = _adapters(tmp_path, [None])
    with pytest.raises(comp.CompilationError) as err:
        _run_compile(tmp_path, adapters)
    assert err.value.code == "CPA_CALL_FAILED" and "CPA_HTTP_ERROR" in err.value.detail and remote.scripts == []


def test_pieces_that_do_not_share_a_format_are_not_joined(tmp_path):
    adapters, remote, _cpa = _adapters(tmp_path, [_answer((1, 3, 6), (2, 9, 12))])
    _fake_media(remote, shapes={1: 1280})
    with pytest.raises(comp.CompilationError) as err:
        _run_compile(tmp_path, adapters)
    assert err.value.code == "COMPILATION_PIECES_INCOMPATIBLE"
    assert [name for name, _ in remote.scripts if name != "source-exists"] == ["compile-cut"]


def test_a_joined_file_that_lost_time_is_reported(tmp_path):
    adapters, remote, _cpa = _adapters(tmp_path, [_answer((1, 3, 6), (2, 9, 12))])
    inner = remote.on_script

    def short_join(name, body, rem):
        res = inner(name, body, rem)
        if name != "compile-concat":
            return res
        return RemoteResult(res.rc, re.sub(r"\"duration\": \"[0-9.]+\"", "\"duration\": \"5.000000\"", res.stdout), "")
    remote.on_script = short_join
    with pytest.raises(comp.CompilationError) as err:
        _run_compile(tmp_path, adapters)
    assert err.value.code == "COMPILATION_CONCAT_DURATION"


def test_intervals_map_back_to_the_recordings_they_came_from(tmp_path):
    adapters, _remote, _cpa = _adapters(tmp_path, [_answer((2, 5, 8), (1, 12, 14))])
    manifest = _run_compile(tmp_path, adapters).manifest
    assert manifest is not None
    whole = comp.source_intervals(manifest, 0, manifest["duration_ms"])
    assert [(i["recording"], i["source_start_ms"], i["source_end_ms"]) for i in whole] == [
        ("rec-b.flv", 11880, 23350), ("rec-a.flv", 32880, 41350)]    # the end never passes the declared source end
    inner = comp.source_intervals(manifest, 11000, 12000)           # 482 ms in piece 0, 518 ms in piece 1
    assert [(i["recording"], i["source_start_ms"], i["source_end_ms"]) for i in inner] == [
        ("rec-b.flv", 22880, 23350), ("rec-a.flv", 32880, 33398)]
    assert comp.source_intervals(manifest, 20000, 21000) == []


# ----- danmaku on the joined clock --------------------------------------------------------------------------------------
def _d(t: float, text: str) -> str:
    return f'<d p="{t},1,25,16777215,1,0,0,0">{text}</d>'


def test_danmaku_of_each_recording_lands_on_the_joined_clock(tmp_path):
    xml = {"rec-a": "<i>" + _d(33.5, "A内") + _d(20.0, "A间") + _d(37.0, "A后") + "</i>",
           "rec-b": "<i>" + _d(12.5, "B内") + _d(500.0, "B外") + _d(15.0, "B内2") + "</i>"}
    adapters, _remote, _cpa = _adapters(tmp_path, [_answer((2, 5, 8), (1, 12, 14))], xml_by_stem=xml)
    result = _run_compile(tmp_path, adapters)
    text = (tmp_path / "task/clips/comp/danmaku/source.xml").read_text(encoding="utf-8")
    rows = re.findall(r'<d p="([\d.]+),[^"]*">([^<]*)</d>', text)
    # rec-b piece starts at 11.88 on its recording, 0 on the joined clock; rec-a piece starts at 32.88 -> 11.482
    assert [(round(float(t), 3), s) for t, s in rows] == [(0.62, "B内"), (3.12, "B内2"), (12.102, "A内"), (15.602, "A后")]
    assert result.manifest is not None and (tmp_path / "task/clips/comp/danmaku/source.xml").exists()
    assert "A间" not in text and "B外" not in text                  # sent between the pieces / far from any of them


def test_danmaku_just_before_the_first_piece_and_after_the_last_keeps_its_place_outside_the_clock(tmp_path):
    xml = {"rec-b": "<i>" + _d(11.0, "前") + "</i>", "rec-a": "<i>" + _d(41.8, "后") + "</i>"}
    adapters, _remote, _cpa = _adapters(tmp_path, [_answer((2, 5, 8), (1, 12, 14))], xml_by_stem=xml)
    _run_compile(tmp_path, adapters)
    text = (tmp_path / "task/clips/comp/danmaku/source.xml").read_text(encoding="utf-8")
    rows = re.findall(r'<d p="(-?[\d.]+),[^"]*">([^<]*)</d>', text)
    assert [(round(float(t), 3), s) for t, s in rows] == [(-0.88, "前"), (20.402, "后")]


# ----- the CLI: compile, then the ordinary init -----------------------------------------------------------------------
def _cli_args(tmp_path):
    names, session = _task(tmp_path)
    args = ["compile", "--task-dir", str(tmp_path / "task"), "--cid", "comp", "--session", str(session), "--theme", "t",
            "--source-dir", SOURCES, "--remote-root", ROOT]
    for n in names:
        args += ["--recording", f"{n}={tmp_path / (n + '.srt')}"]
    return args


def _patch_cli(monkeypatch, adapters):
    monkeypatch.setattr(cli, "SshRemote", lambda **_k: adapters.remote)
    monkeypatch.setattr(cli, "SshCpaJudge", lambda _remote: adapters.cpa)
    monkeypatch.setattr(cli, "DanmakuSource", lambda: adapters.danmaku)


def test_cli_compile_makes_a_candidate_the_ordinary_run_can_take(tmp_path, monkeypatch, capsys):
    adapters, _remote, cpa = _adapters(tmp_path, [_answer((2, 5, 8), (1, 12, 14))])
    _patch_cli(monkeypatch, adapters)
    assert cli.main(_cli_args(tmp_path)) == 0
    out = capsys.readouterr().out
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["compilation"] is True and summary["duration_ms"] == 19964
    assert [(p["recording"], p["source_start_ms"], p["source_end_ms"]) for p in summary["pieces"]] == [
        ("rec-b.flv", 11880, 23350), ("rec-a.flv", 32880, 41350)]
    clip = ClipState.load(tmp_path / "task/state/comp.json").data["clip"]
    assert clip["source_name"] == "compilation-comp.flv" and (clip["start_ms"], clip["end_ms"]) == (0, 19964)
    assert (clip["start_cue"], clip["end_cue"], clip["hook"]) == (1, 30, "一句看点")
    assert clip["remote_dir"] == f"{ROOT}/comp" and Path(clip["mac_bcut_srt"]).read_text(encoding="utf-8")
    # a rerun neither asks CPA again nor refuses the existing state
    assert cli.main(_cli_args(tmp_path)) == 0
    assert cpa.calls == ["compilation-select"]


def test_cli_compile_exits_three_when_cpa_says_no_compilation(tmp_path, monkeypatch, capsys):
    adapters, remote, _cpa = _adapters(tmp_path, [_answer(compilation=False, reason="素材不够")])
    _patch_cli(monkeypatch, adapters)
    assert cli.main(_cli_args(tmp_path)) == 3
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {
        "cid": "comp", "compilation": False, "reason": "素材不够"}
    assert not (tmp_path / "task/state/comp.json").exists() and remote.scripts == []


def test_cli_compile_reports_an_illegal_answer_as_an_error(tmp_path, monkeypatch, capsys):
    adapters, _remote, _cpa = _adapters(tmp_path, [_answer((1, 9, 4))])
    _patch_cli(monkeypatch, adapters)
    assert cli.main(_cli_args(tmp_path)) == 2
    assert "COMPILATION_CUE_ORDER" in capsys.readouterr().out


def test_the_selection_call_has_a_registered_effort_tier():
    entry = rules("effort")["purposes"][comp.SELECT_PURPOSE]
    assert entry["effort"] in {"low", "medium", "high"} and "U168" in entry["source"]


# ----- a BCUT on a session-cumulative clock ------------------------------------------------------------------------
def test_recording_argument_takes_an_optional_zero_of_the_bcut_clock():
    assert cli._recording_input("a.flv=/x/a.srt") == comp.RecordingInput("a.flv", Path("/x/a.srt"), 0)
    assert cli._recording_input("a.flv=/x/a.srt@-3029") == comp.RecordingInput("a.flv", Path("/x/a.srt"), -3029)
    assert cli._recording_input("a.flv=/x/y@z/a.srt").bcut_srt == Path("/x/y@z/a.srt")
    with pytest.raises(ValueError):
        cli._recording_input("a.flv")


def test_cut_points_are_on_the_recording_clock_when_the_bcut_is_cumulative(tmp_path):
    from src.autoslice.manual_lane.config import load_session_config

    adapters, _remote, _cpa = _adapters(tmp_path, [_answer((1, 5, 8))])
    names, session = _task(tmp_path)
    (tmp_path / "rec-a.flv.srt").write_text(_srt(_cues(40, offset=1807.194)), encoding="utf-8")   # the file starts at 1807.194 s of the session
    result = comp.compile_session(
        task=tmp_path / "task", cid="comp", theme="t", session=load_session_config(session),
        recordings=[comp.RecordingInput(names[0], tmp_path / "rec-a.flv.srt", -1807194),
                    comp.RecordingInput(names[1], tmp_path / "rec-b.flv.srt")], source_dir=SOURCES, remote_root=ROOT,
        adapters=adapters)
    assert result.manifest is not None
    assert (result.manifest["pieces"][0]["source_start_ms"], result.manifest["pieces"][0]["source_end_ms"]) == (11880, 23350)
    assert result.manifest["recordings"][0]["bcut_source_zero_ms"] == -1807194


# ----- compile_pieces: intervals the caller decided (the trim command) ---------------------------------------------------
def _pieces_task(tmp_path):
    """One recording (cue ids 1..40, 3 s apart) and the joined clock's BCUT the caller computed."""
    names, session = _task(tmp_path)
    recording = comp.RecordingInput(names[0], tmp_path / f"{names[0]}.srt", 7000)
    return recording, session


def _trim_pieces(tmp_path, adapters, *, pieces=((1000, 11000), (14000, 30000)), cues=None, cid="trimmed"):
    recording, _session = _pieces_task(tmp_path)
    return comp.compile_pieces(
        task=tmp_path / "task", cid=cid, recording=recording, recording_date="2026-10-24", pieces_ms=list(pieces),
        hook="原片的选题理由", origin={"cid": "orig", "removed": [[3, 4]]},
        bcut_cues=_cues(6, step=3.0, length=2.0) if cues is None else cues,
        source_dir=SOURCES, remote_root=ROOT, adapters=adapters)


def test_compile_pieces_writes_a_trim_manifest_without_asking_cpa_or_running_asr(tmp_path):
    adapters, remote, cpa = _adapters(tmp_path, [])                  # a scripted answer would be popped by any CPA call
    result = _trim_pieces(tmp_path, adapters)
    assert result.compilation and result.manifest is not None
    manifest = result.manifest
    assert (manifest["kind"], manifest["origin"], manifest["theme"], manifest["hook"]) == (
        "trim", {"cid": "orig", "removed": [[3, 4]]}, "", "原片的选题理由")
    # the intervals are cut exactly as given, on the one recording; the clock follows the measured durations (+12 ms each)
    assert [(p["recording"], p["source_start_ms"], p["source_end_ms"], p["why"]) for p in manifest["pieces"]] == [
        ("rec-a.flv", 1000, 11000, ""), ("rec-a.flv", 14000, 30000, "")]
    assert [(p["compiled_start_ms"], p["compiled_end_ms"]) for p in manifest["pieces"]] == [(0, 10012), (10012, 26024)]
    assert manifest["duration_ms"] == 26024
    assert manifest["recordings"] == [{"segment": 1, "name": "rec-a.flv", "path": f"{SOURCES}/rec-a.flv",
                                       "bcut_srt": str(tmp_path / "rec-a.flv.srt"), "bcut_source_zero_ms": 7000}]
    assert comp.load_manifest(tmp_path / "task/clips/trimmed") == manifest
    assert cpa.calls == [] and "compile-asr" not in [name for name, _ in remote.scripts]
    cut_body = dict(remote.scripts)["compile-cut"]
    assert " -t 10.000 " in cut_body and " -t 16.000 " in cut_body
    # the joined file's BCUT is the caller's, renumbered; the entry is an ordinary candidate
    assert result.bcut_srt is not None and result.bcut_srt.read_text(encoding="utf-8") == comp.format_srt(_cues(6, step=3.0, length=2.0))
    entry = json.loads(result.selection_file.read_text(encoding="utf-8"))["input_pool"][0]
    assert (entry["cid"], entry["start_ms"], entry["end_ms"], entry["hook"]) == ("trimmed", 0, 26024, "原片的选题理由")
    assert entry["selection_scorecard"] == {"start_cue": 1, "end_cue": 6}
    assert (tmp_path / "task/clips/trimmed/compilation/recordings/1.srt").exists()
    assert not (tmp_path / "task/clips/trimmed/danmaku").exists()      # a trim successor fetches no danmaku


@pytest.mark.parametrize("pieces", [[], [(5000, 5000)], [(-1, 4000)], [(1000, 9000), (8000, 12000)], [(9000, 12000), (1000, 4000)]])
def test_compile_pieces_refuses_pieces_that_are_empty_reversed_or_overlapping(tmp_path, pieces):
    adapters, remote, _cpa = _adapters(tmp_path, [])
    with pytest.raises(comp.CompilationError) as err:
        _trim_pieces(tmp_path, adapters, pieces=pieces)
    assert err.value.code == "COMPILATION_PIECES_INVALID" and remote.scripts == []


def test_compile_pieces_without_any_bcut_cue_is_refused(tmp_path):
    adapters, _remote, _cpa = _adapters(tmp_path, [])
    with pytest.raises(comp.CompilationError) as err:
        _trim_pieces(tmp_path, adapters, cues=[])
    assert err.value.code == "COMPILATION_ASR_EMPTY"


def test_an_ordinary_compilation_manifest_has_no_kind_or_origin(tmp_path):
    adapters, _remote, _cpa = _adapters(tmp_path, [_answer((1, 3, 6), (2, 9, 12))])
    manifest = _run_compile(tmp_path, adapters).manifest
    assert manifest is not None and "kind" not in manifest and "origin" not in manifest


def test_a_trim_clip_is_initialised_and_taken_by_steps_one_zero_and_two(tmp_path):
    from src.autoslice.manual_lane.runner import Ctx, run_clip
    from src.autoslice.manual_lane.stages import STEPS

    adapters, remote, cpa = _adapters(tmp_path, [])

    def script(name, body, rem):
        if name == "s0-dedupe":
            rem.files[f"{ROOT}/trimmed/dedup/result.json"] = json.dumps({"decision": "UNKNOWN", "block_upload": False}).encode()
        elif name == "s2-cut":
            rem.files[f"{ROOT}/trimmed/cut/probe.json"] = json.dumps({
                "format": {"duration": "26.024"}, "streams": [{"codec_type": "video", "width": 1920, "height": 1080},
                                                              {"codec_type": "audio"}]}).encode()
        return RemoteResult(0, "", "")
    inner = remote.on_script
    remote.on_script = lambda name, body, rem: inner(name, body, rem) if name.startswith("compile-") else script(name, body, rem)
    result = _trim_pieces(tmp_path, adapters)
    _recording, session = _pieces_task(tmp_path)
    import argparse
    assert cli.cmd_init(argparse.Namespace(task_dir=str(tmp_path / "task"), cid="trimmed", selection=str(result.selection_file),
                                           session=str(session), mac_bcut_srt=str(result.bcut_srt), bcut_source_zero_ms=0,
                                           remote_root=ROOT, source_dir=f"{ROOT}/sources")) == 0
    state = ClipState.load(tmp_path / "task/state/trimmed.json")
    assert state.data["clip"]["hook"] == "原片的选题理由" and state.data["clip"]["remote_dir"] == f"{ROOT}/trimmed"
    assert state.data["clip"]["source_dir"] == f"{ROOT}/sources"
    ctx = Ctx(state=state, session=state.data["session"], clip_dir=tmp_path / "task/clips/trimmed",
              remote_dir=f"{ROOT}/trimmed", adapters=adapters)
    report = run_clip(ctx, STEPS, until=2)
    assert report.passed == [1, 0, 2] and report.stopped_at is None, report.reason
    assert cpa.calls == []                                        # step 1 took the trim manifest without a boundary review
    decision = json.loads((ctx.clip_dir / "boundary/decision.json").read_text(encoding="utf-8"))
    assert "片中剪除" in decision["skipped"] and "orig" in decision["skipped"] and "同场合集" not in decision["skipped"]
    package = json.loads((ctx.clip_dir / "dedup/package.json").read_text(encoding="utf-8"))
    assert [(i["recording"], i["start_seconds"], i["end_seconds"]) for i in package["source_intervals"]] == [
        ("rec-a.flv", 1.0, 11.0), ("rec-a.flv", 14.0, 30.0)]


# ----- the segment encodes take the media host's encode gate -----------------------------------------------------------------
def test_build_media_cuts_inside_the_host_encode_gate(monkeypatch):
    monkeypatch.setattr(parallel, "limit", lambda name: 2 if name == source_read.HOST_ENCODE else 1)
    meter, cuts = {"now": 0, "peak": 0}, [comp.PieceCut("rec-a.flv", f"{SOURCES}/rec-a.flv", 1000, 11_000)]
    lock, both_in, third_in, release = threading.Lock(), threading.Event(), threading.Event(), threading.Event()
    remotes = []

    def build(_n):
        remote = FakeRemote()
        _fake_media(remote)
        media = remote.on_script

        def on_script(name, body, rem):
            if name == "compile-cut":
                with lock:
                    meter["now"] += 1
                    meter["peak"] = max(meter["peak"], meter["now"])
                    if meter["now"] >= 2:
                        both_in.set()
                    if meter["now"] > 2:
                        third_in.set()
                release.wait(10)           # the first two stay inside the cut until the test has looked for a third
                with lock:
                    meter["now"] -= 1
            return media(name, body, rem)
        remote.on_script = on_script
        remotes.append(remote)
        comp.build_media(remote, cuts, work="/r/w", out="/r/sources/joined.flv")

    threads = [threading.Thread(target=build, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    assert both_in.wait(10) and not third_in.wait(0.5)      # a gate that lets a third in shows at once; only passing waits
    release.set()
    for t in threads:
        t.join()
    assert meter["peak"] == 2 and all(dict(r.scripts).get("compile-concat") for r in remotes)


# ----- the audio fade on each side of an internal seam ----------------------------------------------------------------
def _piece_commands(count: int, *, length_ms: int = 10_000) -> list[str]:
    """The cut command line of every piece, as build_media sends them (inside the root wrapper) to the (fake) media host."""
    remote = FakeRemote()
    _fake_media(remote)
    cuts = [comp.PieceCut("rec-a.flv", f"{SOURCES}/rec-a.flv", 1000 + n * 20_000, 1000 + n * 20_000 + length_ms)
            for n in range(count)]
    durations, _total = comp.build_media(remote, cuts, work="/r/w", out="/r/sources/joined.flv")
    assert durations == [length_ms + 12] * count                     # the fades change neither the cuts nor the durations
    return root_commands(dict(remote.scripts)["compile-cut"])


def test_the_seam_fade_is_fifteen_milliseconds():
    assert comp.SEAM_FADE_S == 0.015


def test_a_single_piece_has_no_fade():
    (only,) = _piece_commands(1)
    assert "afade" not in only and " -af aresample=async=1:first_pts=0 " in only


def test_two_pieces_fade_out_at_the_end_of_the_first_and_in_at_the_head_of_the_second():
    first, second = _piece_commands(2)
    # the fade's times are the piece's own (0 = its first frame); the reader moves the clock around them (source_read)
    assert " -af aresample=async=1:first_pts=0,asetpts=PTS-1.000000/TB,afade=t=out:st=9.985:d=0.015,asetpts=PTS+1.000000/TB " in first
    assert "afade=t=in" not in first
    assert " -af aresample=async=1:first_pts=0,asetpts=PTS-21.000000/TB,afade=t=in:st=0:d=0.015,asetpts=PTS+21.000000/TB " in second
    assert "afade=t=out" not in second


def test_three_pieces_the_middle_one_fades_in_and_out_and_the_ends_only_on_the_seam_side():
    first, middle, last = _piece_commands(3, length_ms=4_000)
    assert "afade=t=in" not in first and first.count("afade=t=out:st=3.985:d=0.015") == 1
    assert middle.count("asetpts=PTS-21.000000/TB,afade=t=in:st=0:d=0.015,afade=t=out:st=3.985:d=0.015,asetpts=PTS+21.000000/TB ") == 1
    assert "afade=t=out" not in last and last.count("afade=t=in:st=0:d=0.015") == 1


def test_the_fade_extends_the_audio_filter_and_the_cut_arguments_are_the_first_cuts():
    import shlex

    first, second = _piece_commands(2)
    for command, fine_start in ((first, "1.000"), (second, "21.000")):         # the preroll is 30 s: both seek exactly from 0
        words = shlex.split(command)
        assert words[words.index("-i") - 1] == "0.000" and words[words.index("-i") + 1] == f"{SOURCES}/rec-a.flv"
        assert words[words.index("-i") + 3] == fine_start and words[words.index("-t") + 1] == "10.000"
        assert words.index("-af") < words.index("-c:a") and words[-1].endswith(".mp4") and "-vf" not in words
