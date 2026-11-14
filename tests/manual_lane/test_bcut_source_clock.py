"""BCUT source binding across real lane consumers, with synthetic cues and no model calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes import FakeCpa, FakeRemote, make_ctx
from test_fmr_calls import Recorder, _patch, _prepared

from src.autoslice.manual_lane import cli, stage_package
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.reselect import load_cues
from src.autoslice.manual_lane.state import ClipState
from src.autoslice.manual_lane.step_cut import step0_dedupe, step1_boundary

SRT = ("1\n00:00:00,500 --> 00:00:20,500\nalpha\n\n"
       "2\n00:00:20,500 --> 00:00:40,500\nbeta\n\n"
       "3\n00:00:40,500 --> 00:01:05,500\ngamma\n")


def test_legacy_source_clock_remains_unchanged(tmp_path):
    path = tmp_path / "source.srt"
    path.write_text(SRT)
    cues = load_cues(path)
    assert [(c["start_s"], c["end_s"]) for c in cues] == [(0.5, 20.5), (20.5, 40.5), (40.5, 65.5)]


@pytest.mark.parametrize("decision,allowed", [("UNKNOWN", True), ("FULL_DUPLICATE", False)])
def test_boundary_and_dedup_share_source_clock_without_modifying_input(tmp_path, decision, allowed):
    remote = FakeRemote(on_script=lambda n, b, r: RemoteResult(0, "", ""))
    remote.files["/r/clip/dedup/result.json"] = json.dumps({"decision": decision}).encode()
    cpa = FakeCpa([json.dumps({"verdict": "ACCEPT", "start_cue": 1, "end_cue": 3, "reason": "synthetic"})])
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa, bcut_text=SRT)
    clip = ctx.state.data["clip"]
    clip.update({"bcut_source_zero_ms": 223000, "start_cue": 1, "end_cue": 3})
    before = Path(clip["mac_bcut_srt"]).read_bytes()
    assert step1_boundary(ctx).ok
    assert (clip["start_ms"], clip["end_ms"]) == (223500, 288500)
    outcome = step0_dedupe(ctx)
    assert outcome.ok is allowed
    if not allowed:
        assert outcome.reason == "DEDUPE_FULL_DUPLICATE"
    pkg = json.loads((ctx.clip_dir / "dedup/package.json").read_text())
    assert (pkg["source_intervals"][0]["start_seconds"], pkg["source_intervals"][0]["end_seconds"]) == (223.5, 288.5)
    window = (ctx.clip_dir / "dedup/window.srt").read_text()
    assert "00:00:00,000 --> 00:00:20,000" in window and "alpha" in window and "gamma" in window
    assert Path(clip["mac_bcut_srt"]).read_bytes() == before


@pytest.mark.parametrize("origin,expected", [(None, 223500), (223000, 500)])
def test_step8_member_points_to_original_with_correct_srt_relative_start(tmp_path, monkeypatch, origin, expected):
    ctx, _ = _prepared(tmp_path)
    clip = ctx.state.data["clip"]
    clip["start_ms"] = 223500
    if origin is not None:
        clip["bcut_source_zero_ms"] = origin
    bcut = Path(clip["mac_bcut_srt"])
    bcut.write_text(SRT)
    before = bcut.read_bytes()
    _patch(monkeypatch, Recorder())
    assert stage_package.step8_package(ctx).ok
    member = json.loads((ctx.clip_dir / "pkg/clip.member.json").read_text())
    context = member["source"]["context_srt"]
    assert context == {"path": str(bcut), "source_zero_srt_ms": expected}
    assert bcut.read_bytes() == before


def test_reselect_cli_preserves_origin_and_source_absolute_cut(tmp_path):
    cid = "auto_010203_223_288"
    task = tmp_path / "task"
    path = tmp_path / "bcut.srt"
    path.write_text(SRT)
    state = ClipState.create(task / "state" / f"{cid}.json", clip={"cid": cid, "remote_dir": f"/r/{cid}",
                             "mac_bcut_srt": str(path), "start_cue": 1, "end_cue": 2,
                             "start_ms": 223500, "end_ms": 263500, "bcut_source_zero_ms": 223000}, session={})
    decision = task / "clips" / cid / "boundary/decision.json"
    decision.parent.mkdir(parents=True)
    decision.write_text(json.dumps({"adopted": {"start_cue": 2, "end_cue": 3}}))
    before = state.path.read_bytes()
    assert cli.main(["reselect", "--task-dir", str(task), "--cid", cid, "--from-counter",
                     "--authority", "synthetic", "--quote", "synthetic"]) == 0
    new = ClipState.load(task / "state/auto_010203_243_288.json").data["clip"]
    assert (new["start_ms"], new["end_ms"], new["bcut_source_zero_ms"]) == (243500, 288500, 223000)
    assert state.path.read_bytes() == before


def test_cli_init_accepts_explicit_origin_and_defaults_to_zero(tmp_path):
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"input_pool": [{"cid": "c", "segment_path": "/x/src.json", "start_ms": 1000,
                                                     "end_ms": 61000, "selection_scorecard": {
                                                         "start_cue": 1, "end_cue": 3}}]}))
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"recording_date": "2026-10-24", "participants": [["host"]],
                                   "own_uploader_mids": [1]}))
    path = tmp_path / "bcut.srt"
    path.write_text(SRT)
    for origin in (None, 223000):
        task = tmp_path / f"task-{origin}"
        args = ["init", "--task-dir", str(task), "--cid", "c", "--selection", str(selection),
                "--session", str(session), "--mac-bcut-srt", str(path)]
        if origin is not None:
            args += ["--bcut-source-zero-ms", str(origin)]
        assert cli.main(args) == 0
        assert ClipState.load(task / "state/c.json").data["clip"]["bcut_source_zero_ms"] == (origin or 0)


@pytest.mark.parametrize("invalid", ["223000", True, 12.5, None])
def test_invalid_persisted_origin_is_not_silently_coerced(invalid):
    from src.autoslice.manual_lane.state import StateError, bcut_source_zero_ms

    with pytest.raises(StateError, match="bcut_source_zero_ms"):
        bcut_source_zero_ms({"bcut_source_zero_ms": invalid})
