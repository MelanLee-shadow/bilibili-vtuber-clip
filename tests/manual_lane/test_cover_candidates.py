"""TASK-10 problem 2 and TASK-11 item 4: step 6's cover candidates (70-cover: screenshot, emote, cutout, ai_sticker
or ai_scene), all with the same cover lines, scored with the repo's choose_cover rules
(title_cover_candidates.score_candidate) on CPA's per-candidate vision reasons.  Since the cover-subject brief every
feasible route is made at once and compared together (no cheapest-first stages that end at the first passing one; the old
tests of those stages now say that).  Every paid image and vision request is recorded.  Ordinary path:
step6_title_cover on the fake remote."""

from __future__ import annotations

import json
import re

from fakes import UNDERSTANDING_CALLS, FakeCpa, FakeRemote, cover_verdict, fake_cover, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

RD = "/r/clip"
TITLE = json.dumps({"candidates": [{"title": "【李豆沙×雾深Girimi】半分钟后她破防了", "on_stage": ["李豆沙", "雾深Girimi"],
                                    "punchline_person": "她破防了",
                                    "swap_check": {"holds_with_other_host": False, "reason": "测试"}}],
                    "recommended": 0, "cover_lines": ["她破防了", "怎么搞"]}, ensure_ascii=False)
FRAMES = {"chosen": 12.0, "checked": [{"t": 12.0, "verdict": "CLEAN"}],
          "boxes": {"danmaku": [[1650, 700, 1830, 1080]], "person": [[1256, 298, 1755, 909]],
                    "guest": [[35, 178, 710, 910]]}}


def _ctx(tmp_path, *, rows=None, answers=(), renders=None, clip="clip", drop_verdict=False, frame_rc=0, extra=None, hook=None):
    """`renders[n]` = (rc, note) of every route job of the n-th cover attempt (an attempt = one frame extraction);
    `frame_rc` the exit of the frame extraction; `extra[route]` adds fields to that route's result row; `hook(name, body,
    remote)` sees every script first and may return its RemoteResult (a route job that fails, one that waits)."""
    def script(name, body, remote):
        if hook is not None and (made := hook(name, body, remote)) is not None:
            return made
        if name == "s6-frame":
            remote.files[f"{RD}/cover/frames.json"] = json.dumps(FRAMES).encode()
        if name == "s6-cover-frame" and frame_rc:
            return RemoteResult(frame_rc, "no frame", "")
        if name.startswith("s6-cover-job"):
            attempt = sum(n == "s6-cover-frame" for n, _ in remote.scripts) - 1
            if renders and attempt < len(renders) and renders[attempt][0]:
                return RemoteResult(renders[attempt][0], renders[attempt][1], "")
            fake_cover(remote, RD, rows=rows, extra=extra)
            if drop_verdict:
                path = f"{RD}/cover/cand/result-{re.search(r'job-([^ ]+)[.]json', body).group(1)}.json"
                result = json.loads(remote.files[path])
                for row in result["rows"]:
                    row["verdict"] = None
                remote.files[path] = json.dumps(result).encode()
        if name == "s6-cover-fallback":
            job = json.loads(remote.files[f"{RD}/cover/fallback.job.json"])
            choice = {"chosen": job["candidates"][0]["id"], "reason": "图像最贴近源画面，脸仍偏小"}
            remote.files[f"{RD}/cover/cand/fallback.result.json"] = json.dumps({"choice": choice, "calls": [
                {"kind": "vision", "candidate": "fallback-choose", "model": "gpt-6.1-sol", "rc": 0}]}).encode()
            remote.files[f"{RD}/cover/cand/fallback-choose.response.json"] = b'{}'
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    ctx = make_ctx(tmp_path / clip, remote=remote, cpa=FakeCpa([TITLE, *answers]))
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
    return ctx, remote


def _stages(remote):
    """The routes of each step-6 cover run, in order (job.json as uploaded before each run)."""
    return [json.loads(body)["routes"] for body in remote.job_uploads]


def _track_jobs(remote):
    remote.job_uploads = []
    put = remote.put

    def tracking_put(local, remote_path):
        put(local, remote_path)
        if remote_path.endswith("/cover/job.json"):
            remote.job_uploads.append(remote.files[remote_path].decode("utf-8"))
    remote.put = tracking_put


def test_every_route_is_made_in_one_go_and_the_best_eligible_candidate_is_chosen(tmp_path):
    # was: "generates a picture only when the free candidates fail" (staged, stopped at the first passing stage).  Now the
    # screenshot (small face, vetoed), the cut-out and the sticker are all made and scored together; the cut-out wins on C
    ctx, remote = _ctx(tmp_path)
    _track_jobs(remote)
    out = si.step6_title_cover(ctx)
    assert out.ok, (out.reason, out.note)
    assert _stages(remote) == [["screenshot", "emote", "cutout", "ai_sticker"]]       # one stage: every route
    route = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))["cover_route"]
    scores = {c["id"]: c["score"] for c in route["candidates"]}
    assert set(scores) == {"screenshot", "cutout", "ai_sticker"}
    assert scores["screenshot"]["status"] == "VETO" and "MAIN_FACE_BELOW_25_PERCENT" in scores["screenshot"]["vetoes"]
    assert scores["cutout"]["status"] == scores["ai_sticker"]["status"] == "ELIGIBLE"
    assert route["chosen"]["id"] == "cutout"
    assert route["image_generation_used"] is True and route["image_gen_model"] == "gpt-image-2.5"
    assert (ctx.clip_dir / "cover/cover.png").read_bytes() == b"png-cutout"
    job = json.loads(remote.files[f"{RD}/cover/job.json"].decode("utf-8"))
    assert job["lines"] == ["她破防了", "怎么搞"]                      # one set of lines for every candidate
    assert job["host_box"] == [1256, 298, 1755, 909] and job["guest_box"] == [35, 178, 710, 910]
    ids = [str(c.get("call_id")) for c in out.cpa]                     # every paid request is recorded
    assert sum(i.startswith("cover-image-") for i in ids) == 2 and sum(i.startswith("cover-judge-") for i in ids) == 3
    assert "cover-brief" in ids and "cover-emote" in ids
    assert any("不可行或没做出的路线" in line and "emote" in line for line in out.process)


def test_a_passing_screenshot_does_not_stop_the_other_routes(tmp_path):
    # was: "a passing screenshot costs no picture".  The routes are made together, so the generated ones are made (and
    # paid for) too; the screenshot still wins on the realism axis C
    ctx, remote = _ctx(tmp_path, rows=[("screenshot", cover_verdict()), ("cutout", cover_verdict()),
                                       ("ai_sticker", cover_verdict())])
    _track_jobs(remote)
    out = si.step6_title_cover(ctx)
    assert out.ok, (out.reason, out.note)
    assert _stages(remote) == [["screenshot", "emote", "cutout", "ai_sticker"]]
    route = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))["cover_route"]
    assert route["chosen"]["id"] == "screenshot" and route["image_generation_used"] is False
    assert sum(str(c.get("call_id")).startswith("cover-image-") for c in out.cpa) == 2


def test_two_vetoed_routes_still_leave_the_third_to_be_chosen(tmp_path):
    # was: "a second picture is drawn only when the first one fails too" (staged)
    small = (1380, 470, 1600, 650)
    ctx, remote = _ctx(tmp_path, rows=[("screenshot", cover_verdict(face=small)), ("cutout", cover_verdict(face=small)),
                                       ("ai_sticker", cover_verdict())])
    out = si.step6_title_cover(ctx)
    assert out.ok, (out.reason, out.note)
    route = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))["cover_route"]
    assert route["chosen"]["id"] == "ai_sticker"
    assert sum(str(c.get("call_id")).startswith("cover-image-") for c in out.cpa) == 2


def test_all_vetoed_rendered_candidates_go_to_cpa_choice(tmp_path):
    small = (1380, 470, 1600, 650)                                     # every face under 25 % of the height
    ctx, _ = _ctx(tmp_path, rows=[("screenshot", cover_verdict(face=small)), ("cutout", cover_verdict(face=small))])
    out = si.step6_title_cover(ctx)
    assert out.ok, (out.reason, out.note)
    assert any("MAIN_FACE_BELOW_25_PERCENT" in line for line in out.process)
    choose = json.loads((ctx.clip_dir / "cover/cand/choose.json").read_text(encoding="utf-8"))
    assert choose["chosen"] == "screenshot"
    vetoed = {v["id"]: v["vetoes"] for v in choose["vetoed"]}                  # every candidate's own reasons are kept
    assert set(vetoed) == {"screenshot", "cutout"} and all("MAIN_FACE_BELOW_25_PERCENT" in v for v in vetoed.values())
    assert choose["fallback_choice"]["reason"] == "图像最贴近源画面，脸仍偏小"
    assert any("CPA 看图选择 screenshot" in line for line in out.process)


def test_no_rendered_image_remains_a_tool_failure(tmp_path):
    ctx, remote = _ctx(tmp_path, rows=[])
    out = si.step6_title_cover(ctx)
    assert not out.ok and out.reason == "COVER_NO_ELIGIBLE_CANDIDATE"
    assert not any(name == "s6-cover-fallback" for name, _body in remote.scripts)


def test_rendered_candidate_without_first_vision_verdict_can_still_be_chosen(tmp_path):
    ctx, _ = _ctx(tmp_path, rows=[("screenshot", cover_verdict())], drop_verdict=True)
    out = si.step6_title_cover(ctx)
    assert out.ok, (out.reason, out.note)
    assert any("未评分" in line for line in out.process)


def test_missing_frame_remains_a_tool_failure(tmp_path):
    ctx, remote = _ctx(tmp_path, frame_rc=22)
    out = si.step6_title_cover(ctx)
    assert not out.ok and out.reason == "COVER_RENDER_FAILED_RC22"
    assert not any(name == "s6-cover-fallback" for name, _body in remote.scripts)


def test_text_that_fits_no_picture_is_shortened_once_and_every_candidate_is_rendered_again(tmp_path):
    ctx, remote = _ctx(tmp_path, renders=[(23, "最多只能放到 114px（下限 120px）"), (0, "")],
                       answers=[json.dumps({"cover_lines": ["破防了"]}, ensure_ascii=False)])
    out = si.step6_title_cover(ctx)
    assert out.ok, (out.reason, out.note)
    assert ctx.adapters.cpa.calls == [*UNDERSTANDING_CALLS, "title", "title-judge-r0-1", "title-judge-r0-2", "cover-brief",
                                      "cover-emote", "cover-emote", "title-cover-retry-1"]    # the new lines ask the emote again (listed by family)
    job = json.loads(remote.files[f"{RD}/cover/job.json"].decode("utf-8"))
    assert job["lines"] == ["破防了"]
    assert json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))["cover_lines"] == ["破防了"]


def test_the_emote_of_an_earlier_cover_of_the_same_session_is_not_the_main_emote_again(tmp_path):
    first, _ = _ctx(tmp_path, clip="a")
    plan = si.cover_plan(first, ["她破防了", "被吓到"], {"quotes": []}, ["李豆沙", "雾深Girimi"])
    assert plan["emote"] and plan["emote"]["id"] == "05"               # 被吓到: the repo's keyword pick
    # a sibling clip of the same recording date already used 05 as its main emote
    sibling = first.clip_dir.parent / "other/title"
    sibling.mkdir(parents=True)
    (sibling / "candidates.json").write_text(json.dumps({"cover_route": {
        "method": "skill.candidates", "at": "2026-11-02T00:00:00Z", "recording_date": first.session["recording_date"],
        "chosen": {"id": "emote", "route": "emote", "main_emote": "05"}}}), encoding="utf-8")
    again = si.cover_plan(first, ["她破防了", "被吓到"], {"quotes": []}, ["李豆沙", "雾深Girimi"])
    assert again["emote"] is None or again["emote"]["id"] != "05"
    assert again["recent"]["previous_route"] == "emote" and "05" in again["recent"]["source_session_main_emotes"]


class _OneAnswer:
    def __init__(self, text):
        self.text, self.calls = text, []

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        self.calls.append(call_id)
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": self.text, "stderr_tail": ""}


def test_a_scene_is_drawn_only_when_the_brief_gives_the_reason(tmp_path):
    ctx, remote = _ctx(tmp_path)
    assert si.step6_title_cover(ctx).ok
    job = json.loads(remote.files[f"{RD}/cover/job.json"].decode("utf-8"))
    assert job["brief"]["scene_needed"] is False                       # chat and reaction: the sticker, no scene
    cases = [({"scene_needed": True, "scene_reason": "她在讲桌上那台电脑坏了"}, True),
             ({"scene_needed": True, "scene_reason": ""}, False),          # no reason, no scene
             ({"scene_needed": "yes", "scene_reason": "x"}, False)]        # only a real true
    for i, (answer, expected) in enumerate(cases):
        ctx2 = make_ctx(tmp_path / f"b{i}", remote=FakeRemote(), cpa=_OneAnswer(json.dumps(answer, ensure_ascii=False)))
        assert si._cover_brief(ctx2, [], "标题", ["甲"], ["李豆沙"])["scene_needed"] is expected


def test_an_unchanged_frame_reading_is_not_asked_again(tmp_path):
    ctx, remote = _ctx(tmp_path)
    FRAMES_WITH_TIMES = dict(FRAMES, question=si._box_question())
    first = si.step6_title_cover(ctx)
    assert first.ok
    doc = json.loads((ctx.clip_dir / "cover/frames.json").read_text(encoding="utf-8"))
    times = doc.get("times")
    assert times is None                    # the fake tool did not record its times: the next run asks again
    (ctx.clip_dir / "cover/frames.json").write_text(json.dumps(dict(FRAMES_WITH_TIMES, times=_times(remote))),
                                                   encoding="utf-8")
    (ctx.clip_dir / "cover/frames.json").chmod(0o444)     # on the media host the record belongs to root
    before = [n for n, _ in remote.scripts].count("s6-frame")
    again = si.step6_title_cover(ctx)
    assert again.ok and [n for n, _ in remote.scripts].count("s6-frame") == before
    assert json.loads((ctx.clip_dir / "cover/frames.json").read_text(encoding="utf-8"))["reused"] is True
    assert not any(str(c.get("call_id", "")).startswith("cover-vision") for c in again.cpa)


def _times(remote):
    body = next(b for n, b in reversed(remote.scripts) if n == "s6-frame")
    return [float(t) for t in body.split("frames.json ")[1].split("'")[0].split(",")]
