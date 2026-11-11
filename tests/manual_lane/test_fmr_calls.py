"""The thin FMR calls in steps 8-9: init and check (check's findings only recorded), attest only after a CPA PASS,
seal after attest.  The lane's own logic is not tested here; the lane is replaced by a recorder."""

from __future__ import annotations

import json
import subprocess

from fakes import FakeCpa, FakeRemote, make_ctx

from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane.adapters import RemoteResult

RD = "/r/clip"


def _prepared(tmp_path, cpa_review=None):
    """A clip context with every step 8 input in place and the remote products it fetches."""
    remote = FakeRemote(on_script=lambda n, b, r: RemoteResult(0, "", ""))
    for name, data in (("burn/final.mp4", b"mp4"), ("burn/final.ass", b"ass"), ("cut/cut.mp4", b"cut")):
        remote.files[f"{RD}/{name}"] = data
    cpa = FakeCpa([json.dumps(cpa_review, ensure_ascii=False)] if cpa_review else [])
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa)
    c = ctx.clip_dir
    (c / "title").mkdir(parents=True)
    (c / "title/title.txt").write_text("【李豆沙】标题\n", encoding="utf-8")
    (c / "title/candidates.json").write_text(json.dumps({"cover_lines": ["甲", "乙"]}), encoding="utf-8")
    (c / "cover").mkdir(parents=True)
    (c / "cover/cover.png").write_bytes(b"png")
    (c / "subs").mkdir(parents=True)
    (c / "subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n句\n", encoding="utf-8")
    (c / "subs/doubts.json").write_text("[]", encoding="utf-8")
    (c / "speaker").mkdir(parents=True)
    (c / "speaker/speaker.srt").write_text("x", encoding="utf-8")
    (c / "tags").mkdir(parents=True)
    (c / "tags/tags.json").write_text("{}", encoding="utf-8")
    (c / "burn").mkdir(parents=True)
    (c / "burn/intro_result.json").write_text(json.dumps({"status": "PREPENDED", "intro_id": "Z1"}), encoding="utf-8")
    (c / "dedup").mkdir(parents=True)
    (c / "dedup/result.json").write_text(json.dumps({"decision": "UNKNOWN", "block_upload": False}), encoding="utf-8")
    (c / "cut").mkdir(parents=True)
    (c / "cut/probe.json").write_text(json.dumps({"format": {"duration": "60.0"}}), encoding="utf-8")
    (c / "asr").mkdir(parents=True)
    (c / "asr/bcut.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n句\n", encoding="utf-8")
    return ctx, cpa


class Recorder:
    """Stands in for the lane's CLI: records every call and answers with scripted return codes."""

    def __init__(self, codes=None, out_cid="clip"):  # the lane's slug for cid "clip" is "clip"
        self.calls: list[tuple[str, ...]] = []
        self.codes = codes or {}
        self.out_cid = out_cid

    def __call__(self, *args):
        self.calls.append(args)
        cmd = args[0]
        rc = self.codes.get(cmd, 0)
        if cmd == "init":
            root = args[args.index("--out-root") + 1]
            pkg = sp.Path(root) / "out" / "2026-10-24" / f"fm-20261024-{self.out_cid}"
            pkg.mkdir(parents=True, exist_ok=True)
            (pkg / "findings.md").write_text("F", encoding="utf-8")
        return subprocess.CompletedProcess(args, rc, "A1 FLAG 示例\n" if cmd == "check" else "", "")


def _patch(monkeypatch, recorder):
    monkeypatch.setattr(sp, "_fmr", recorder)
    monkeypatch.setattr(sp, "description_text", lambda title, p=None: "简介")


REVIEW_PASS = {"verdict": "PASS", "findings": [], "review_md": "复审通过"}
REVIEW_HOLD = {"fixes": {}, "holds": [{"cue_id": 1, "reason": "两种读法意思相反，上下文定不了"}], "review_md": "定不下"}


def test_step8_calls_init_then_check_and_check_findings_do_not_block(tmp_path, monkeypatch):
    ctx, _ = _prepared(tmp_path)
    rec = Recorder(codes={"check": 1})   # 1 = the lane found FLAG/FAIL items: recorded, not blocking
    _patch(monkeypatch, rec)
    out = sp.step8_package(ctx)
    assert out.ok, out.note
    assert [c[0] for c in rec.calls] == ["init", "check"]
    assert "FMR check rc=1" in out.note and "FLAG" in "\n".join(out.process)
    assert (ctx.clip_dir / "pkg" / "fmr_check.log").read_text(encoding="utf-8").startswith("A1 FLAG")


def test_step8_stops_only_when_the_lane_refuses_init_or_check(tmp_path, monkeypatch):
    ctx, _ = _prepared(tmp_path / "a")
    _patch(monkeypatch, Recorder(codes={"init": 2}))
    assert sp.step8_package(ctx).reason == "FMR_INIT_REFUSED"
    ctx, _ = _prepared(tmp_path / "b")
    _patch(monkeypatch, Recorder(codes={"check": 2}))
    assert sp.step8_package(ctx).reason == "FMR_CHECK_REFUSED"


def test_member_is_written_in_the_pilot_shape(tmp_path, monkeypatch):
    ctx, _ = _prepared(tmp_path)
    _patch(monkeypatch, Recorder())
    assert sp.step8_package(ctx).ok
    member = json.loads((ctx.clip_dir / "pkg" / "clip.member.json").read_text(encoding="utf-8"))
    assert member["schema_version"] == "finished-media-member.v1" and member["member_slug"] == "clip"
    assert member["maker"] == {"kind": "AGENT", "id": "manual-lane-maker-clip"}
    assert sp.grade_of({"state": "UNRESOLVED"}) == "D" and sp.grade_of({"state": "KEEP", "maintained": True}) == "A"
    assert sp.grade_of({"state": "ADOPT", "maintained": False}) == "B" and sp.grade_of({"state": "KEEP"}) == "C"
    assert all(d["grade"] in {"A", "B", "C", "D"} for d in member["doubts"])
    assert member["cover_claims"] == {"lines": ["甲", "乙"]}
    assert member["files"]["intro_receipt"].endswith("burn/intro_result.json")
    assert member["duplicate_check"]["status"] == "UNKNOWN" and member["duplicate_check"]["block_upload"] is False


def test_step9_pass_attests_with_a_different_reviewer_then_seals(tmp_path, monkeypatch):
    ctx, cpa = _prepared(tmp_path, cpa_review=REVIEW_PASS)
    rec = Recorder()
    _patch(monkeypatch, rec)
    assert sp.step8_package(ctx).ok
    out = sp.step9_review(ctx)
    assert out.ok and "已封存" in out.note
    names = [c[0] for c in rec.calls]
    assert names == ["init", "check", "attest", "seal"]
    attest = rec.calls[2]
    assert attest[attest.index("--reviewer-id") + 1] == sp.reviewer_id()
    assert attest[attest.index("--verdict") + 1] == "PASS" and attest[attest.index("--reviewer-kind") + 1] == "AGENT"
    assert sp.reviewer_id() != sp.maker_id(ctx)


def test_step9_a_hold_is_never_attested_or_sealed(tmp_path, monkeypatch):
    # TASK-9: the one interception is doubt_policy.HOLD_RULE; a sentence the review holds keeps the package unsealed
    # The post-review listening rounds are covered separately with scripted providers and both CPA outcomes.
    monkeypatch.setattr(sp, "_finish_or_wide", lambda ctx, record, process:
                        sp._finish(ctx, cpa=record["cpa"], process=process))
    ctx, _ = _prepared(tmp_path, cpa_review=REVIEW_HOLD)
    rec = Recorder()
    _patch(monkeypatch, rec)
    assert sp.step8_package(ctx).ok
    out = sp.step9_review(ctx)
    assert out.ok and out.rewind == sp.REWIND_TAGS            # the doubt grades changed: the package is rebuilt
    ctx.state.data["rewind"] = {"by": "review", "steps": list(out.rewind)}   # as the runner records it
    assert sp.step8_package(ctx).ok
    member = json.loads((ctx.clip_dir / "pkg" / "clip.member.json").read_text(encoding="utf-8"))
    assert "D" in {d["grade"] for d in member["doubts"]}
    out = sp.step9_review(ctx)
    assert out.ok and "未封存" in out.note
    assert ctx.adapters.cpa.calls.count("review") == 1         # finishing does not ask the reviewer again
    assert [c[0] for c in rec.calls] == ["init", "check", "init", "check"]


def test_seal_block_is_recorded_and_the_package_is_reported_unsealed(tmp_path, monkeypatch):
    ctx, _ = _prepared(tmp_path, cpa_review=REVIEW_PASS)
    _patch(monkeypatch, Recorder(codes={"seal": 1}))
    assert sp.step8_package(ctx).ok
    out = sp.step9_review(ctx)
    assert out.ok and "未封存" in out.note


def test_attest_refusal_stops_step9(tmp_path, monkeypatch):
    ctx, _ = _prepared(tmp_path, cpa_review=REVIEW_PASS)
    _patch(monkeypatch, Recorder(codes={"attest": 2}))
    assert sp.step8_package(ctx).ok
    assert sp.step9_review(ctx).reason == "FMR_ATTEST_REFUSED"
