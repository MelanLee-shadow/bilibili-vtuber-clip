"""Step 6 cover: no candidate fit because the picture itself was wrong (no face measured, the judge's identity / faithful /
no-fabrication axes) -> the routes are made again within the shared retry budget before the fallback may choose; the
fallback never waves a material-vetoed candidate through while another exists; a cover whose every candidate is vetoed on its
material is taken with the fewest vetoes and a loud non-blocking flag; every root command of the cover step hands `cover/`
back to the lane's user.  Parallelism of a round is proved with an event barrier, never with timing."""

from __future__ import annotations

import json
import re

import pytest
from fakes import FakeCpa, FakeRemote, cover_verdict, fake_cover, make_ctx, srt
from test_cover_candidates import FRAMES, RD, TITLE

from src.autoslice.manual_lane import cover_redo, step6_cover
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult
from src.autoslice.manual_lane.layout import layout

NO_FACE = ("fail", "HOST_FACE_NOT_FOUND")
LAYERS = ("fail", "PEOPLE_LAYERS_UNAVAILABLE:HOST_FACE_NOT_FOUND")
IDENTITY_WORDS = "发饰与源画面不同"
FAITHFUL_WORDS = "画面里多了源画面没有的物件"


@pytest.fixture(autouse=True)
def _not_equals(monkeypatch):
    """The redo mechanics are tested on a host and a guest who are not equals: the one generated picture is then the
    sticker (cover_stages; two equals get the cut-out, tested in test_cover_candidates)."""
    real = step6_cover.cover_peers
    monkeypatch.setattr(step6_cover, "cover_peers", lambda ctx: dict(real(ctx), peers=False))


def _verdict(*axes, face=(1300, 300, 1600, 650)):
    """A judge answer that says no on `axes` (identity, source_faithful, no_fabrication), with its own words."""
    verdict = cover_verdict(face=face)
    words = {"identity": IDENTITY_WORDS, "source_faithful": FAITHFUL_WORDS, "no_fabrication": "编造了字幕里没有的事"}
    for axis in axes:
        verdict[axis] = {"pass": False, "reason": words[axis]}
    return verdict


class Cover:
    """A fake media host for the cover step: `rounds[n]` says what each route yields the (n+1)-th time it is made (the last
    one repeats): ("fail", code) or a judge verdict.  Every job file and every script is kept for the assertions."""

    def __init__(self, tmp_path, rounds, *, too_long_first=False, answers=(), barrier=None, fallback_pick=None):
        self.rounds, self.jobs, self.too_long_first, self.barrier = rounds, [], too_long_first, barrier
        self.fallback_jobs, self.fallback_pick, self.made = [], fallback_pick, {}
        remote = FakeRemote(on_script=self.script)
        self.ctx = make_ctx(tmp_path, remote=remote, cpa=FakeCpa([TITLE, *answers]))
        (self.ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
        (self.ctx.clip_dir / "subs/final.srt").write_text(srt(3), encoding="utf-8")
        self.remote = remote

    def script(self, name, body, remote):
        if name == "s6-frame":
            remote.files[f"{RD}/cover/frames.json"] = json.dumps(FRAMES).encode()
        if name == "s6-cover-fallback":
            job = json.loads(remote.files[f"{RD}/cover/fallback.job.json"])
            self.fallback_jobs.append(job)
            choice = {"chosen": (self.fallback_pick or job["candidates"][0]["id"]), "reason": "图像最贴近源画面"}
            remote.files[f"{RD}/cover/cand/fallback.result.json"] = json.dumps({"choice": choice, "calls": []}).encode()
            remote.files[f"{RD}/cover/cand/fallback-choose.response.json"] = b"{}"
        if not name.startswith("s6-cover-job"):
            return RemoteResult(0, "", "")
        job_name = re.search(r"cover/(job-[^ ]+)\.json", body).group(1)
        job = json.loads(remote.files[f"{RD}/cover/{job_name}.json"])
        tag = re.search(r"-r(\d+)$", job_name)
        number = int(tag.group(1)) if tag else 1
        self.jobs.append(dict(job, name=job_name, round=number))
        if self.too_long_first and sum(n == "s6-cover-frame" for n, _ in remote.scripts) == 1:
            fake_cover(remote, RD, rows=[], calls=[], body=body)
            return RemoteResult(23, "文字放不下", "")
        if self.barrier is not None and number > 1:
            self.barrier.wait()            # the round's jobs must be in flight together, or the barrier breaks
        attempt = self.made.get(job["routes"][0], 0)
        for route in job["routes"]:
            self.made[route] = attempt + 1
        plan = self.rounds[min(attempt, len(self.rounds) - 1)]
        wanted = [r for r in job["routes"] if r in plan]
        fake_cover(remote, RD, rows=[(r, plan[r]) for r in wanted if not isinstance(plan[r], tuple)], body=body)
        result_path = f"{RD}/cover/cand/{json.loads(remote.files[f'{RD}/cover/{job_name}.json'])['result']}"
        result = json.loads(remote.files[result_path])
        result["rows"] += [{"id": r, "route": r, "failure": plan[r][1]} for r in wanted if isinstance(plan[r], tuple)]
        remote.files[result_path] = json.dumps(result).encode()
        for r in wanted:
            if not isinstance(plan[r], tuple):
                remote.files[f"{RD}/cover/cand/{r}.png"] = f"png-{r}-try{attempt + 1}".encode()
        return RemoteResult(0, "", "")

    def run(self):
        out = si.step6_title_cover(self.ctx)
        assert out.ok, (out.reason, out.note)
        return out

    def choose(self):
        return json.loads((self.ctx.clip_dir / "cover/cand/choose.json").read_text(encoding="utf-8"))

    def names(self):
        return [n for n, _ in self.remote.scripts]

    def jobs_of(self, number):
        return {j["name"]: j for j in self.jobs if j["round"] == number}


def test_a_free_route_that_found_no_face_is_made_again_on_another_frame_before_any_picture_is_drawn(tmp_path):
    host = Cover(tmp_path, [{"screenshot": NO_FACE}, {"screenshot": cover_verdict()}])
    out = host.run()
    # round 2 is made on another frame (cut out of the same video) with its own spec, work directory and files
    assert host.names().count("s6-cover-redo-frame-2") == 1
    assert f"{RD}/cover/frame-r2.png" in dict(host.remote.scripts)["s6-cover-redo-frame-2"]
    (job,) = host.jobs_of(2).values()
    assert job["name"] == "job-screenshot-r2" and job["frame"] == f"{RD}/cover/frame-r2.png"
    assert job["screenshot_spec"] == f"{RD}/cover/spec-r2.json" and job["work"].endswith("-r2")
    assert json.loads(host.remote.files[f"{RD}/cover/spec-r2.json"])["frame"] == f"{RD}/cover/frame-r2.png"
    assert host.jobs_of(1)["job-screenshot"]["frame"] == f"{RD}/cover/frame.png"
    assert not any(set(j["routes"]) & {"cutout", "ai_sticker", "ai_scene"} for j in host.jobs)   # no picture generated
    route = json.loads((host.ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))["cover_route"]
    assert route["chosen"]["id"] == "screenshot" and route["frame_s"] != FRAMES["chosen"]
    assert route["image_generation_used"] is False
    choose = host.choose()
    assert {r["id"] for r in choose["rows"]} == {"screenshot", "screenshot-r1"} and choose["stages"] == [["screenshot", "emote"]]
    assert (host.ctx.clip_dir / "cover/cover.png").read_bytes() == b"png-screenshot-try2"
    first_round, second_round = choose["rounds"]
    assert first_round["frame_t"] == FRAMES["chosen"] and first_round["stage"] == "free"
    assert second_round["frame_changed"] is True and second_round["redone"]["screenshot"]["regenerated"] is False
    assert choose["redo"] == {"limit": 2, "used": 1} and choose["material_flag"] is None
    assert any(line.startswith("封面重做 第 1/2 次") for line in out.process)
    assert host.ctx.adapters.cpa.calls.count("cover-emote") == 1          # the emote is an official picture: not asked again


def test_the_one_drawn_picture_vetoed_on_its_material_is_drawn_again_with_the_reviewers_words(tmp_path):
    small = (1380, 470, 1600, 650)        # the screenshot's face is too small: not its material, so no other frame
    host = Cover(tmp_path, [{"screenshot": cover_verdict(face=small), "ai_sticker": _verdict("identity", "source_faithful")},
                            {"ai_sticker": cover_verdict()}])
    host.run()
    assert "s6-cover-redo-frame-3" not in host.names()
    (again,) = host.jobs_of(3).values()
    assert again["name"] == "job-ai_sticker-r3" and again["frame"] == f"{RD}/cover/frame.png"
    action = again["brief"]["action"]
    assert IDENTITY_WORDS in action and FAITHFUL_WORDS in action and action.startswith("talking")
    choose = host.choose()
    assert choose["stages"] == [["screenshot", "emote"], ["ai_sticker"]]
    assert [r["stage"] for r in choose["rounds"][:2]] == ["free", "paid"] and choose["rounds"][2]["frame_changed"] is False
    assert choose["rounds"][2]["redone"]["ai_sticker"] == {"vetoes": ["CPA_VETO:identity", "CPA_VETO:source_faithful"],
                                                          "rejection": choose["rounds"][2]["redone"]["ai_sticker"]["rejection"],
                                                          "regenerated": True, "reasons_in_prompt": True}
    assert choose["chosen"] == "ai_sticker" and {r["id"] for r in choose["rows"]} == {"screenshot", "ai_sticker-r2", "ai_sticker"}
    assert "s6-cover-fallback" not in host.names()


def test_a_drawn_picture_that_failed_on_a_missing_face_is_drawn_again_on_another_frame_without_a_code_in_its_prompt(tmp_path):
    small = (1380, 470, 1600, 650)
    host = Cover(tmp_path, [{"screenshot": cover_verdict(face=small), "ai_sticker": ("fail", "HOST_FACE_NOT_IN_FRAME")},
                            {"ai_sticker": cover_verdict()}])
    host.run()
    (job,) = host.jobs_of(3).values()
    assert job["frame"] == f"{RD}/cover/frame-r3.png" and job["brief"]["action"] == "talking"      # no judge words, so none sent
    assert host.choose()["rounds"][2]["redone"]["ai_sticker"]["reasons_in_prompt"] is False


def test_when_the_redo_is_spent_the_fewest_vetoes_is_taken_with_a_loud_flag_and_the_step_does_not_fail(tmp_path):
    # the screenshot finds no face on three frames (the two redos are spent on it, free); the one drawn picture is wrong too
    host = Cover(tmp_path, [{"screenshot": NO_FACE, "ai_sticker": _verdict("identity", "source_faithful")}])
    out = host.run()
    assert [j["name"] for j in host.jobs] == ["job-screenshot", "job-screenshot-r2", "job-screenshot-r3", "job-ai_sticker"]
    assert "s6-cover-fallback" not in host.names()                       # every candidate is material-vetoed: no look-and-pick
    choose = host.choose()
    assert choose["chosen"] == "ai_sticker" and choose["redo"] == {"limit": 2, "used": 2}
    flag = choose["material_flag"]
    assert flag["id"] == "ai_sticker" and flag["axes"] == ["CPA_VETO:identity", "CPA_VETO:source_faithful"]
    assert IDENTITY_WORDS in flag["judge_words"] and choose["fallback_choice"]["method"] == "fewest_vetoes"
    flagged = [line for line in out.process if line.startswith("【封面素材被否决·非阻塞")]
    assert len(flagged) == 1 and "CPA_VETO:identity" in flagged[0] and IDENTITY_WORDS in flagged[0]
    route = json.loads((host.ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))["cover_route"]
    assert route["material_flag"] == flag and route["chosen"]["route"] == "ai_sticker"
    assert "非阻塞" in out.note


def test_the_fallback_never_picks_a_candidate_vetoed_on_its_material_while_another_exists(tmp_path):
    small = (1380, 470, 1600, 650)
    rounds = [{"screenshot": cover_verdict(face=small), "ai_sticker": _verdict("identity", "source_faithful")}]
    host = Cover(tmp_path, rounds, fallback_pick="screenshot")
    out = host.run()
    (job,) = host.fallback_jobs
    assert {c["id"] for c in job["candidates"]} == {"screenshot"}        # no ai_sticker, from any round
    assert host.choose()["chosen"] == "screenshot" and host.choose()["material_flag"] is None
    assert not any(line.startswith("【封面素材被否决") for line in out.process)


def test_the_redo_and_the_cover_text_shortening_spend_one_budget(tmp_path):
    rounds = [{"ai_sticker": _verdict("identity")}]
    host = Cover(tmp_path, rounds, too_long_first=True,
                 answers=[json.dumps({"cover_lines": ["不会饿死"]}, ensure_ascii=False)])
    out = host.run()
    assert "title-cover-retry-1" in host.ctx.adapters.cpa.calls          # one of the two spent on the text ...
    assert len([j for j in host.jobs if re.search(r"-r\d+$", j["name"])]) == 1      # ... leaves one redo, not two
    assert host.choose()["redo"] == {"limit": 2, "used": 2}
    assert any(line.startswith("封面重做 第 1/2 次") for line in out.process)


def test_every_root_command_of_the_cover_step_hands_cover_back_to_the_lane_user(tmp_path):
    first = {"screenshot": NO_FACE, "ai_sticker": _verdict("identity")}
    host = Cover(tmp_path, [first, first, first, {"screenshot": cover_verdict(), "ai_sticker": cover_verdict()}])
    host.run()
    user = layout().remote_user
    handed_back = f"sudo -n chown -R {user}:{user} {RD}/cover 2>/dev/null || true"
    read_only = {"s6-published-titles"}      # the registry reader prints JSON and writes nothing
    root_scripts = {name: body for name, body in host.remote.scripts if "sudo -n" in body}
    kinds = {re.sub(r"^(s6-cover-job|s6-cover-thumb)-.*", r"\1", name) for name in root_scripts}
    assert {"s6-frame", "s6-cover-job", "s6-cover-thumb"} <= kinds       # pick_frame, the route jobs, the thumbnail check
    for name, body in root_scripts.items():
        assert name in read_only or handed_back in body.splitlines(), name


def test_the_fallback_script_and_every_root_command_write_only_under_the_cover_directory_they_hand_back(tmp_path):
    small = (1380, 470, 1600, 650)
    host = Cover(tmp_path, [{"screenshot": cover_verdict(face=small), "ai_sticker": cover_verdict(face=small)}])
    host.run()
    user = layout().remote_user
    root = {name: body for name, body in host.remote.scripts if "sudo -n" in body and name != "s6-published-titles"}
    assert "s6-cover-fallback" in root
    for name, body in root.items():
        assert f"sudo -n chown -R {user}:{user} {RD}/cover " in body, name
        written = re.findall(rf"{re.escape(RD)}/[^\s'\"]+", body.split("sudo -n chown")[0])
        assert all(path.startswith(f"{RD}/cover/") or path.startswith(f"{RD}/cut/") for path in written), (name, written)


# ----- the pure parts --------------------------------------------------------------------------------------------------
def _row(rid, route, vetoes, verdict=None):
    return {"id": rid, "route": route, "status": "RENDERED", "score": {"status": "VETO", "vetoes": vetoes},
            "verdict": verdict}


def test_only_the_material_vetoes_count_as_material():
    material = ["CPA_VETO:identity", "CPA_VETO:source_faithful", "CPA_VETO:no_fabrication", "HOST_MISSING",
                "HOST_FACE_NOT_FOUND", "PEOPLE_LAYERS_UNAVAILABLE:HOST_FACE_NOT_FOUND", "PEOPLE_LAYERS_UNAVAILABLE",
                "HOST_FACE_NOT_ON_HER_FIGURE", "PEOPLE_FACE_OUTSIDE_MASK", "HOST_FACE_NOT_IN_FRAME"]
    other = ["MAIN_FACE_BELOW_25_PERCENT", "CPA_VETO:clean_layers", "PUNCH_BELOW_120", "IMAGE_EDIT_RATE_LIMITED",
             "PEER_FACE_SIZE_UNEQUAL", "MAIN_EMOTE_REPEATED"]
    assert all(cover_redo.is_material(v) for v in material) and not any(cover_redo.is_material(v) for v in other)


def test_the_fewest_vetoes_wins_and_a_tie_goes_to_the_later_attempt():
    rows = [_row("a", "ai_sticker", ["x", "y"]), _row("b", "cutout", ["x"]), _row("c", "screenshot", ["x"])]
    assert cover_redo.fewest_vetoes(rows)["id"] == "c"


def test_the_frame_after_the_cover_moment_is_the_nearest_unused_one_that_was_not_judged_unrelated():
    times = [35.0, 31.0, 39.0, 27.0]
    checked = [{"t": 31.0, "verdict": "NOT_CLEAN"}, {"t": 35.0, "verdict": "CLEAN"}]
    assert cover_redo.alternate_frame(times, [35.0], checked) == 39.0
    assert cover_redo.alternate_frame(times, [35.0, 39.0, 27.0], checked) is None


def test_the_rejection_text_is_the_judges_own_words_per_axis():
    row = _row("a", "ai_sticker", ["CPA_VETO:identity", "HOST_FACE_NOT_FOUND", "MAIN_FACE_BELOW_25_PERCENT"], _verdict("identity"))
    assert cover_redo.rejection_text(row) == f"identity: {IDENTITY_WORDS} | HOST_FACE_NOT_FOUND"
    assert cover_redo.retry_brief({"action": "talking"}, "x")["action"].startswith("talking. A previous attempt")
