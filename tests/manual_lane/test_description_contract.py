"""Step 8 writes the uploader's own description (scripts/authorized_upload.description_for_title), byte for byte.

The FMR A9 check recomputes that same function, and the uploaded v3 manifests of clip-2/4/6 (live-20261030-pilot) carry
exactly its output.  A partner the reviewed registry does not know makes the uploader refuse the title, so step 8 must
stop there instead of writing a description that differs from what the uploader would verify."""

from __future__ import annotations

import json
import pathlib

import pytest
from fakes import FakeCpa, FakeRemote, make_ctx

from scripts import authorized_upload as au
from src.autoslice.channel_envelope import EnvelopeError
from src.autoslice.manual_lane import stage_package as sp

REVIEWED_TITLE = "【李豆沙×礼墨Sumi×安晚awa】笑死，李豆沙说自己只是吃点“无害的醋”"
UNREVIEWED_TITLE = "【李豆沙×星野未央Mio】漫画设定从开除升级杀人，李豆沙却说“我还以为只是开除”"


def test_description_is_the_uploader_rendering_with_reviewed_partner_lines():
    text = sp.description_text(REVIEWED_TITLE)
    assert text == au.description_for_title(REVIEWED_TITLE)
    assert text.startswith("https://live.bilibili.com/\n本切片由 bilibili-vtuber-clip 项目提供：")
    assert text.endswith("相关主播：礼墨Sumi https://space.bilibili.com/435243735\n"
                         "相关主播：安晚awa https://space.bilibili.com/1377219279")
    assert not text.endswith("\n")   # the uploader's bytes carry no trailing newline


def test_unreviewed_partner_is_refused_like_the_uploader_refuses_it():
    with pytest.raises(EnvelopeError, match="TALK_PARTNER_UNRESOLVED:星野未央Mio"):
        sp.description_text(UNREVIEWED_TITLE)
    with pytest.raises(EnvelopeError):
        au.description_for_title(UNREVIEWED_TITLE)


def _step8_ctx(tmp_path, title: str):
    from src.autoslice.manual_lane.adapters import RemoteResult
    rd = "/r/clip"
    remote = FakeRemote(on_script=lambda n, b, r: RemoteResult(0, "", ""))
    for name in ("burn/final.mp4", "burn/final.ass", "cut/cut.mp4"):
        remote.files[f"{rd}/{name}"] = b"x"
    ctx = make_ctx(tmp_path, remote=remote, cpa=FakeCpa([]))
    c = ctx.clip_dir
    files = {"title/title.txt": title + "\n", "title/candidates.json": json.dumps({"cover_lines": ["甲"]}),
             "cover/cover.png": "png", "subs/final.srt": "1\n00:00:00,000 --> 00:00:01,000\n句\n",
             "speaker/speaker.srt": "x", "tags/tags.json": "{}",
             "burn/intro_result.json": json.dumps({"status": "PREPENDED", "intro_id": "Z1"}),
             "dedup/result.json": json.dumps({"decision": "UNKNOWN", "block_upload": False}),
             "cut/probe.json": json.dumps({"format": {"duration": "60.0"}}), "subs/doubts.json": "[]"}
    for name, body in files.items():
        (c / name).parent.mkdir(parents=True, exist_ok=True)
        (c / name).write_text(body, encoding="utf-8")
    return ctx


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        import subprocess
        self.calls.append(args)
        if args[0] == "init":   # the real init creates the package directory the step then reads
            root = pathlib.Path(args[args.index("--out-root") + 1])
            pkg = root / "out" / "2026-10-24" / "fm-20261024-clip"
            pkg.mkdir(parents=True, exist_ok=True)
            (pkg / "findings.md").write_text("F", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "A1 FLAG 示例\n" if args[0] == "check" else "", "")


def test_step8_writes_the_uploader_description_for_a_reviewed_partner(tmp_path, monkeypatch):
    ctx = _step8_ctx(tmp_path, REVIEWED_TITLE)
    rec = _Recorder()
    monkeypatch.setattr(sp, "_fmr", rec)
    out = sp.step8_package(ctx)
    assert out.ok, out.reason
    written = (ctx.clip_dir / "pkg" / "description.txt").read_text(encoding="utf-8")
    assert written == au.description_for_title(REVIEWED_TITLE)


def test_step8_stops_before_fmr_when_a_partner_is_not_in_the_reviewed_registry(tmp_path, monkeypatch):
    ctx = _step8_ctx(tmp_path, UNREVIEWED_TITLE)
    rec = _Recorder()
    monkeypatch.setattr(sp, "_fmr", rec)
    out = sp.step8_package(ctx)
    assert not out.ok
    assert out.reason == "DESCRIPTION_ENVELOPE_UNRESOLVED"
    assert "星野未央Mio" in (out.note or "")
    assert rec.calls == []   # FMR init/check never ran on a package the uploader would refuse
