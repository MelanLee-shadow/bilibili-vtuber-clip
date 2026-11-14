"""TASK-10 problem 3 (TASK-9 25.4): the words of a doubt row, as the package member, the review prompt and the hold
lines show them, are the words of its final cues in the current track.  A writer after step 3 (TASK-9's review
rewrites before the doubt table was synced; step 4's lone-character join now) can change the track; a stored copy
in doubts.json then disagrees with the subtitles.  Ordinary path: steps 8-9 run again on a track another writer
changed."""

from __future__ import annotations

import json

from test_pipeline_fake_e2e import ScriptedCpa
from test_review_once import BOUNDARY, TITLE, _review, _run

from src.autoslice.manual_lane import subtitle_track as st
from src.autoslice.manual_lane.runner import run_clip
from src.autoslice.manual_lane.stages import STEPS


def test_the_package_and_the_review_show_a_doubt_with_the_words_of_the_current_track(tmp_path, monkeypatch):
    cpa = ScriptedCpa({"boundary": BOUNDARY, "title": [TITLE], "review": [_review(verdict="PASS")] * 2})
    ctx, _remote, report = _run(tmp_path, monkeypatch, cpa)
    assert report.stopped_at is None, (report.stopped_at, report.reason)
    # another writer rewrote line 2 after step 3 and did not touch doubts.json (the TASK-9 witness shape)
    track = st.read_track(ctx.clip_dir)
    old = st.plain_text(track[1]["text"])
    track[1]["text"] = "她说的是他"
    st.write_track(ctx.clip_dir, track, speakers=True)
    for key in ("package", "review"):
        ctx.state.data["steps"].pop(key, None)
    ctx.state.data.pop("review", None)
    ctx.state.save()
    again = run_clip(ctx, STEPS)
    assert again.stopped_at is None, (again.stopped_at, again.reason)
    stored = json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
    assert old != "她说的是他" and all(d.get("final_text") != "她说的是他" for d in stored)   # the table is not rewritten
    member = json.loads((ctx.clip_dir / "pkg" / "clip.member.json").read_text(encoding="utf-8"))
    row = next(d for d in member["doubts"] if d["cue_index"] == 2)
    assert row["final_text"] == "她说的是他"
    prompt = (ctx.clip_dir / "cpa" / "review.prompt.txt").read_text(encoding="utf-8")
    assert '"final_text": "她说的是他"' in prompt
