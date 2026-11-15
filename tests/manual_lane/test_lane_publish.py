"""lane_publish, the public upload tool, against a fake Bilibili: a first publication, a video replacement and then a cover
replacement on the same BV, each through the command line step 10 sends, and what the lane reads back from them (the receipt
chain, the packages used online, the same-BV authority, step 10's plan comparison).  Nothing reaches the network or biliup."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path

import pytest

from src.autoslice import bilibili_member_api
from src.autoslice.lane_publish import audit, authority, bili, first, manifest, registry, repair
from src.autoslice.publication_registry import load_publication_registry
from src.autoslice.lane_publish.cli import main
from src.autoslice.lane_publish.common import LOCK_BUSY
from src.autoslice.manual_lane import receipt_chain, step10_check
from src.autoslice.manual_lane.step10_commands import Job

TITLE = "【主播】测试标题"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1146).to_bytes(4, "big") + (717).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
SEASON = {"lane": "talk", "season_title": "测试合集", "season_id": 11, "section_id": 22, "source": "profile:test"}


class FakeBili:
    """The creator centre, the public pages and biliup, as one account's archives."""

    def __init__(self):
        self.archives: dict[str, dict] = {}
        self.public: dict[str, dict | None] = {}
        self.runs, self.appends, self.edits, self.seasons = [], [], [], []
        self.next_cid = 1000
        self.hide_new_parts = 0        # how many archive views a freshly appended part stays unlisted

    def _cid(self) -> int:
        self.next_cid += 1
        return self.next_cid

    def fetch(self, url: str) -> dict:
        bvid = re.search(r"bvid=(BV\w+)", url).group(1)
        if "/tag/" in url:
            return {"code": 0, "data": [{"tag_name": t} for t in self.archives[bvid]["tag"].split(",")]}
        view = self.public.get(bvid)
        return {"code": 0, "data": view} if view else {"code": 62002, "message": "稿件不可见"}

    def run(self, argv, *, cwd, log, timeout):
        self.runs.append(argv)
        flags = dict(zip(argv[5::2], argv[6::2]))
        bvid = f"BV1test{len(self.runs):05d}"
        cid = self._cid()
        self.archives[bvid] = {"aid": 7000 + len(self.runs), "bvid": bvid, "title": flags["--title"], "desc": flags["--desc"],
                               "tag": flags["--tag"], "cover": "https://upload/first.png", "copyright": 2, "tid": 27,
                               "videos": [{"cid": cid, "filename": "n1", "title": flags["--title"]}]}
        self._publish(bvid)
        Path(log).write_text(f'ResponseData {{ data: {{ "bvid": String("{bvid}") }} }}\n', encoding="utf-8")
        return 0

    def _publish(self, bvid: str) -> None:
        a = self.archives[bvid]
        self.public[bvid] = {"aid": a["aid"], "bvid": bvid, "cid": a["videos"][0]["cid"], "title": a["title"],
                             "pic": a["cover"].replace("https://upload/", "http://i0.hdslb.com/bfs/archive/"),
                             "pages": [{"cid": v["cid"], "part": v["title"]} for v in a["videos"]]}

    # the creator-centre session
    def __call__(self, _config):
        return self

    def archive_view(self, bvid):
        a = self.archives[bvid]
        videos = a["videos"]
        if self.hide_new_parts:
            self.hide_new_parts -= 1
            videos = videos[:1]
        return {"archive": {k: v for k, v in a.items() if k != "videos"}, "videos": [dict(v) for v in videos]}

    def biliup_append(self, bvid, path):
        self.appends.append((bvid, str(path)))
        self.archives[bvid]["videos"].append({"cid": self._cid(), "filename": "n2", "title": "P2"})

    def cover_up(self, data):
        return f"https://upload/{hashlib.sha256(data).hexdigest()[:8]}.png"

    def build_edit_payload(self, view, **kwargs):
        return bilibili_member_api.build_edit_payload(view, csrf="csrf", **kwargs)

    def edit_archive(self, payload):
        self.edits.append(payload)
        a = self.archives[payload["bvid"]]
        a.update(title=payload["title"], cover=payload["cover"], tag=payload["tag"], desc=payload["desc"])
        a["videos"] = [{"cid": v["cid"], "filename": v["filename"], "title": v["title"]} for v in payload["videos"]]
        self._publish(payload["bvid"])
        return {"code": 0}

    def season_episode_add(self, section_id, *, aid, cid, title):
        self.seasons.append((section_id, aid, cid, title))
        return {"code": 0}


@pytest.fixture
def host(tmp_path, monkeypatch):
    base = tmp_path / "base"
    (base / "state").mkdir(parents=True)
    biliup = tmp_path / "bin/biliup"
    biliup.parent.mkdir()
    biliup.write_text("#!/bin/sh\nexit 99\n")
    biliup.chmod(0o755)
    cookies = tmp_path / "account/biliup_cookies.json"
    cookies.parent.mkdir()
    cookies.write_text("{}")
    (base / "lane_publish.json").write_text(json.dumps({"biliup_bin": str(biliup), "biliup_cookies": str(cookies)}))
    monkeypatch.setenv("AUTOSLICE_BASE", str(base))
    fake = FakeBili()
    monkeypatch.setattr(bili, "FETCH", fake.fetch)
    monkeypatch.setattr(bili, "RUN", fake.run)
    monkeypatch.setattr(bili, "SESSION", fake)
    monkeypatch.setattr(manifest, "_season", lambda: dict(SEASON))
    monkeypatch.setattr(audit, "_streams", lambda _video: ({"video", "audio"}, 12.5))
    monkeypatch.setattr(first, "SLEEP", lambda _s: None)
    monkeypatch.setattr(repair, "SLEEP", lambda _s: None)
    fake.base = base
    return fake


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def package(root: Path, *, video: bytes = b"video-1", cover: bytes = PNG, title: str = TITLE, extra: dict | None = None,
            name: str = "fm-20261114-clip-a") -> Path:
    """A package as step 9 seals it: the files, review_manifest.json binding them, the finished-media review receipt."""
    pkg = root / name
    pkg.mkdir(parents=True)
    files = {"video": (f"{name}.mp4", video), "cover": (f"{name}.cover.png", cover), "ass_path": (f"{name}.ass", b"ass"),
             "subtitle_srt": (f"{name}.srt", b"srt"), "speaker_srt": (f"{name}.speaker.srt", b"spk"),
             "review": ("REVIEW.md", b"review"),
             "receipt": (f"{name}.finished-media-review.json", json.dumps({"verdict": "PASS"}).encode()),
             "record": (f"{name}.record.json", json.dumps({"recording_date": "2026-11-14", "upload_tags": {
                 "final_tags": ["虚拟主播", "切片"]}, **(extra or {})}, ensure_ascii=False).encode())}
    item = {"stem": name, "candidate_id": name, "title": title, "title_txt": f"{name}.title.txt",
            "description_txt": f"{name}.description.txt"}
    for key, (file, data) in files.items():
        (pkg / file).write_bytes(data)
        item[key], item[f"{key}_sha256"] = file, _sha(pkg / file)
    (pkg / f"{name}.title.txt").write_text(title + "\n", encoding="utf-8")
    (pkg / f"{name}.description.txt").write_text("简介第一行\n第二行\n", encoding="utf-8")
    (pkg / "review_manifest.json").write_text(json.dumps({"schema_version": "finished-media-review-package.v1",
                                                          "candidate_id": name, "date": "2026-11-14", "items": [item]}))
    return pkg


def _first(host, tmp_path, capsys) -> tuple[Path, dict]:
    pkg = package(tmp_path / "pkg1")
    name, out = pkg.name, tmp_path / "upload1"
    out.mkdir()
    assert main(["audit", str(pkg), "--json"]) == 0
    (out / "package_audit.json").write_text(capsys.readouterr().out)
    man = out / "authorized-upload-manifest.json"
    assert main(["make-manifest", "--video", str(pkg / f"{name}.mp4"), "--cover", str(pkg / f"{name}.cover.png"),
                 "--package-audit", str(out / "package_audit.json"), "--title", TITLE, "--tags", "虚拟主播,切片",
                 "--authorized-by", "频道主", "--quote", "默认发布", "--out", str(man)]) == 0
    assert main(["verify", "--manifest", str(man)]) == 0
    assert main(["upload", "--manifest", str(man)]) == 0
    capsys.readouterr()
    return pkg, json.loads(first.receipt_path(man).read_text())


def test_a_first_publication_is_one_bv_in_the_collection_with_a_receipt_the_lane_can_walk(host, tmp_path, capsys):
    pkg, receipt = _first(host, tmp_path, capsys)
    assert receipt["status"] == "VERIFIED_PUBLIC" and receipt["schema_version"] == receipt_chain.FIRST_SCHEMA
    assert len(host.runs) == 1 and host.runs[0][host.runs[0].index("--title") + 1] == TITLE
    assert Path(host.runs[0][4]).name == f"{TITLE}.mp4" and not Path(host.runs[0][4]).exists()   # the part link is gone
    assert host.seasons == [(22, receipt["aid"], receipt["cid"], TITLE)]
    rows = registry.rows_for(registry.load()["entries"], candidate_id=pkg.name)
    assert [(r["candidate_id"], r["bvid"], r["status"]) for r in rows] == [(pkg.name, receipt["bvid"], "published")]
    # one registry: the channel's reader (title_history, the upload gate) sees the row this tool wrote
    seen = [r for r in load_publication_registry(runtime_path=registry.path())["entries"] if r.get("bvid") == receipt["bvid"]]
    assert [(r["candidate_id"], r["publication_reconciliation"]["status"]) for r in seen] == [(pkg.name, "VERIFIED_PUBLIC")]
    chain = receipt_chain.resolve(rows[0]["publication_reconciliation"]["authority"])
    assert chain.first_receipt == first.receipt_path(tmp_path / "upload1/authorized-upload-manifest.json")
    assert receipt_chain.used_packages(chain)[0]["package_root"] == str(pkg)
    assert receipt_chain.first_manifest_meta(chain.first_receipt) == (("虚拟主播", "切片"), "简介第一行\n第二行")


def test_upload_again_or_after_a_stop_never_posts_a_second_bv(host, tmp_path, capsys):
    _pkg, receipt = _first(host, tmp_path, capsys)
    man = tmp_path / "upload1/authorized-upload-manifest.json"
    assert main(["upload", "--manifest", str(man)]) == 0 and len(host.runs) == 1          # the receipt is there: nothing sent
    first.receipt_path(man).unlink()
    registry.path().write_text(json.dumps({"schema_version": registry.SCHEMA, "entries": []}))
    assert main(["upload", "--manifest", str(man)]) == 0 and len(host.runs) == 1          # posted before: resumes from the BV
    assert json.loads(first.receipt_path(man).read_text())["bvid"] == receipt["bvid"]


def test_a_dispatch_without_a_bv_quota_and_a_held_lock_are_refused_with_step_10s_codes(host, tmp_path, capsys, monkeypatch):
    pkg = package(tmp_path / "p")
    out = tmp_path / "u"
    out.mkdir()
    (out / "a.json").write_text(json.dumps(audit.audit(pkg)))
    man = out / "m.json"
    manifest.make(video=pkg / f"{pkg.name}.mp4", cover=pkg / f"{pkg.name}.cover.png", package_audit=out / "a.json", title=TITLE,
                  tags="a,b", final_human_review=None, authorized_by="x", quote="y", out=man)

    def quota(argv, *, cwd, log, timeout):
        Path(log).write_text("code: 21566 投稿过于频繁")
        return 1

    monkeypatch.setattr(bili, "RUN", quota)
    assert main(["upload", "--manifest", str(man)]) == 8 and "21566" in capsys.readouterr().err
    with registry.locked():
        assert main(["upload", "--manifest", str(man)]) == 4 and LOCK_BUSY in capsys.readouterr().err
    registry.ledger_add("dispatched", manifest_sha256=_sha(man))
    assert main(["upload", "--manifest", str(man)]) == 3 and "没有 BV" in capsys.readouterr().err


def test_a_channel_without_a_talk_collection_stops_at_the_manifest(host, tmp_path, monkeypatch):
    monkeypatch.undo()          # the real profile lookup: the default test profile has no season_routes asset
    with pytest.raises(manifest.Refused, match="season_routes"):
        manifest._season()


def _successor(host, tmp_path, capsys, *, first_receipt: Path, video: bytes, cover: bytes, n: int) -> tuple[Path, Path]:
    auth = authority.build(first_receipt, candidate_id="fm-20261114-clip-a", title=TITLE)
    pkg = package(tmp_path / f"pkg{n}", video=video, cover=cover, extra={"recovery_publication_authority": auth})
    name, out = pkg.name, tmp_path / f"upload{n}"
    out.mkdir()
    capsys.readouterr()
    assert main(["audit", str(pkg), "--json"]) == 0
    (pkg / "package_audit.json").write_text(capsys.readouterr().out)
    man = out / "replace-manifest.json"
    assert main(["make-manifest", "--video", str(pkg / f"{name}.mp4"), "--cover", str(pkg / f"{name}.cover.png"),
                 "--package-audit", str(pkg / "package_audit.json"), "--title", TITLE, "--final-human-review",
                 str(pkg / f"{name}.finished-media-review.json"), "--authorized-by", "频道主", "--quote", "修复完的可以编辑上传",
                 "--out", str(man)]) == 0
    capsys.readouterr()
    return pkg, man


def _revision_json(clip: Path, pkg: Path) -> None:
    clip.mkdir(parents=True, exist_ok=True)
    (clip / "revision.json").write_text(json.dumps({"revision": {
        "video_sha256": _sha(pkg / f"{pkg.name}.mp4"), "cover_sha256": _sha(pkg / f"{pkg.name}.cover.png")}}))


def test_a_video_then_a_cover_replacement_on_the_same_bv(host, tmp_path, capsys, monkeypatch):
    _pkg, receipt = _first(host, tmp_path, capsys)
    bvid, first_receipt = receipt["bvid"], first.receipt_path(tmp_path / "upload1/authorized-upload-manifest.json")
    pkg, man = _successor(host, tmp_path, capsys, first_receipt=first_receipt, video=b"video-2", cover=PNG + b"2", n=2)
    out, journal = man.parent, tmp_path / "base/reports/same_bv_repair_ledger.jsonl"
    plan = out / "same-bv-repair-plan.json"
    common = ["--manifest", str(man), "--bvid", bvid, "--out", str(plan), "--journal", str(journal)]
    assert main(["repair-plan", *common, "--preserve-existing-tags", "--dry-run"]) == 0
    (out / "plan-dry.log").write_text(capsys.readouterr().out)
    job = Job("replace_video", bvid, pkg, out, TITLE, "", None, "", "", (), ())
    _revision_json(tmp_path / "clip2", pkg)
    assert step10_check.plan_problems(job, tmp_path / "clip2") == [] and not plan.exists()
    assert main(["repair-plan", *common, "--preserve-existing-tags"]) == 0
    walk = ["--plan", str(plan), "--journal", str(journal)]
    assert main(["repair-status", *walk]) == 6
    host.hide_new_parts = 10**6                                    # appended, never listed within the wait
    clock = iter(range(0, 10**7, 100))
    monkeypatch.setattr(repair, "CLOCK", lambda: next(clock))
    assert main(["repair-run", *walk]) == 6 and len(host.appends) == 1
    host.hide_new_parts = 0
    assert main(["repair-run", *walk]) == 0 and len(host.appends) == 1           # looked again: no second append
    assert main(["repair-status", *walk]) == 0
    done = out / "same-bv-repair-completed.json"
    assert main(["repair-verify-live", *walk, "--out", str(done)]) == 0
    completed = json.loads(done.read_text())
    assert completed["status"] == "VERIFIED_FRESH_LIVE" and completed["new_cid"] != receipt["cid"]
    assert [v["cid"] for v in host.archives[bvid]["videos"]] == [completed["new_cid"]]
    assert host.archives[bvid]["tag"] == "虚拟主播,切片"
    row = registry.rows_for(registry.load()["entries"], bvid=bvid)[0]
    chain = receipt_chain.resolve(row["publication_reconciliation"]["authority"])
    assert chain.first_receipt == first_receipt and chain.predecessor_completed == done
    assert chain.predecessor_new_cid == completed["new_cid"] and chain.online_video_sha256 == _sha(pkg / f"{pkg.name}.mp4")
    assert authority.replay_first(authority.build(first_receipt, candidate_id=pkg.name, title=TITLE))["video"]["sha256"] \
        == _sha(tmp_path / "pkg1" / pkg.name / f"{pkg.name}.mp4")

    # a cover alone after it: must name the video completion, then only the cover changes
    pkg3, man3 = _successor(host, tmp_path, capsys, first_receipt=first_receipt, video=b"video-2", cover=PNG + b"3", n=3)
    plan3 = man3.parent / "same-bv-cover-repair-plan.json"
    base3 = ["--manifest", str(man3), "--bvid", bvid, "--out", str(plan3), "--journal", str(journal)]
    assert main(["cover-repair-plan", *base3]) == 3 and "完成回执" in capsys.readouterr().err
    assert main(["cover-repair-plan", *base3, "--predecessor-completed", str(done)]) == 0
    walk3 = ["--plan", str(plan3), "--journal", str(journal)]
    assert main(["cover-repair-run", *walk3]) == 0 and len(host.appends) == 1
    done3 = man3.parent / "same-bv-cover-repair-completed.json"
    assert main(["cover-repair-verify-live", *walk3, "--out", str(done3)]) == 0
    assert json.loads(done3.read_text())["unchanged_cid"] == completed["new_cid"]
    row = registry.rows_for(registry.load()["entries"], bvid=bvid)[0]
    chain = receipt_chain.resolve(row["publication_reconciliation"]["authority"])
    assert chain.predecessor_completed == done and chain.online_cover_sha256 == _sha(pkg3 / f"{pkg3.name}.cover.png")
    assert {p["package_root"] for p in receipt_chain.used_packages(chain)} == {str(tmp_path / f"pkg{n}" / pkg.name) for n in (1, 2, 3)}


def test_an_audit_finds_a_changed_file_and_a_review_that_did_not_pass(host, tmp_path):
    pkg = package(tmp_path / "p")
    (pkg / f"{pkg.name}.srt").write_bytes(b"changed")
    result = audit.audit(pkg)
    assert not result["passed"] and any("subtitle_srt" in i for i in result["issues"])
    pkg2 = package(tmp_path / "q", cover=b"not a png")
    assert any("PNG" in i for i in audit.audit(pkg2)["issues"])


def _send(body: str, capsys, journal: Path) -> int:
    """Run one command step 10 would send (step10_commands) with lane_publish as the tool, its output where the body sends it."""
    from fakes import root_commands

    (command,) = root_commands(body)
    words = shlex.split(command)
    argv = words[words.index("scripts/lane_publish.py") + 1:words.index(">")]
    if "--journal" in argv:                  # the host's ledger path; here the test's
        argv[argv.index("--journal") + 1] = str(journal)
    capsys.readouterr()
    rc = main(argv)
    said = capsys.readouterr()
    rest = words[words.index(">") + 1:]
    if rest[1:] == ["2>&1"]:
        Path(rest[0]).write_text(said.out + said.err)
    else:
        Path(rest[0]).write_text(said.out)
        Path(rest[2]).write_text(said.err)
    return rc


def test_the_commands_step_10_sends_are_lane_publish_commands(host, tmp_path, capsys):
    from src.autoslice.manual_lane import step10_commands as commands

    tools = {"upload_tool": ("lane_publish.py",), "audit_tool": ("lane_publish.py", "audit")}
    journal = tmp_path / "journal.jsonl"
    pkg = package(tmp_path / "pkg1")
    out = tmp_path / "clip1/upload"
    out.mkdir(parents=True)
    job = Job("first_publication", None, pkg, out, TITLE, "虚拟主播,切片", None, "频道主", "默认发布", **tools)
    for body in (commands.audit(job), commands.make_manifest(job), commands.verify_manifest(job), commands.upload(job)):
        assert _send(body, capsys, journal) == 0, (out / "upload.log").read_text() if (out / "upload.log").exists() else body
    receipt = json.loads(job.uploaded.read_text())
    assert receipt["status"] == "VERIFIED_PUBLIC" and receipt["bvid"]

    auth = authority.build(job.uploaded, candidate_id=pkg.name, title=TITLE)
    pkg2 = package(tmp_path / "pkg2", video=b"video-2", extra={"recovery_publication_authority": auth})
    out2 = tmp_path / "clip2/upload"
    out2.mkdir(parents=True)
    job2 = Job("replace_video", receipt["bvid"], pkg2, out2, TITLE, "", None, "频道主", "修复完的可以编辑上传", **tools)
    assert _send(commands.audit(job2), capsys, journal) == 0
    (pkg2 / "package_audit.json").write_text((out2 / "package_audit.json").read_text())
    for body, rc in ((commands.make_manifest(job2), 0), (commands.verify_manifest(job2), 0),
                     (commands.plan(job2, dry_run=True), 0), (commands.plan(job2, dry_run=False), 0),
                     (commands.status(job2), 6), (commands.run(job2, log="run-dry.log", dry_run=True), 6),
                     (commands.run(job2, log="run-1.log"), 0), (commands.verify_live(job2), 0)):
        assert _send(body, capsys, journal) == rc, body
    _revision_json(tmp_path / "clip2", pkg2)
    assert step10_check.plan_problems(job2, tmp_path / "clip2") == []
    done = json.loads(job2.completed.read_text())
    assert done["status"] == "VERIFIED_FRESH_LIVE" and done["new_cid"] != receipt["cid"]


def test_a_bv_the_older_upload_tool_registered_is_repaired_in_the_same_one_registry(host, tmp_path, capsys):
    """B8: one registry.  A BV published and reconciled by the older tool (its manifest v3, its first receipt, its runtime row
    written as that tool writes it) takes a cover repair from this tool; after it the channel's reader sees one row for the BV,
    bound to this tool's completion receipt, and the receipt chain still starts at the older tool's first receipt."""
    name, bvid, cid, aid = "fm-20261114-clip-old", "BV1oldtool001", 4242, 9090
    old = tmp_path / "old"
    old.mkdir()
    media = {k: old / f"{name}.{ext}" for k, ext in (("video", "mp4"), ("cover", "cover.png"))}
    media["video"].write_bytes(b"old-video")
    media["cover"].write_bytes(PNG + b"old")
    bind = {k: {"path": str(p), "sha256": _sha(p), "bytes": p.stat().st_size} for k, p in media.items()}
    man = old / "authorized-upload-manifest.json"
    man.write_text(json.dumps({"schema_version": "authorized-upload-manifest.v3", "title": TITLE, "description": "旧简介",
                               "tags": ["虚拟主播", "切片"], **bind}, ensure_ascii=False))
    receipt = old / "publication-reconciliation-authority.json"
    receipt.write_text(json.dumps({"schema_version": receipt_chain.FIRST_SCHEMA, "status": "VERIFIED_PUBLIC", "aid": aid,
                                   "bvid": bvid, "candidate_id": name, "cid": cid, "created_at": "2026-11-14T00:00:00Z",
                                   "recording_date": "2026-11-14", "title": TITLE,
                                   "evidence": {"manifest": {"path": str(man), "sha256": _sha(man), "bytes": man.stat().st_size}}},
                                  ensure_ascii=False))
    row_authority = {"path": str(receipt), "sha256": _sha(receipt), "bytes": receipt.stat().st_size}
    registry.path().write_text(json.dumps({"schema_version": registry.SCHEMA, "entries": [{
        "candidate_id": name, "recording_date": "2026-11-14", "bvid": bvid, "status": "published",
        "publication_reconciliation": {"schema_version": "publication-reconciliation.v1", "status": "VERIFIED_PUBLIC",
                                       "candidate_id": name, "recording_date": "2026-11-14", "bvid": bvid, "aid": aid,
                                       "cid": cid, "title": TITLE, "authority": row_authority,
                                       "reconciled_at": "2026-11-14T00:00:00Z"}}]}, ensure_ascii=False))
    host.archives[bvid] = {"aid": aid, "bvid": bvid, "title": TITLE, "desc": "简介第一行\n第二行", "tag": "虚拟主播,切片",
                           "cover": "https://upload/old.png", "copyright": 2, "tid": 27,
                           "videos": [{"cid": cid, "filename": "n1", "title": TITLE}]}
    host._publish(bvid)

    auth = authority.build(receipt, candidate_id=name, title=TITLE)
    pkg = package(tmp_path / "pkgN", video=b"old-video", cover=PNG + b"new", name=name, extra={"recovery_publication_authority": auth})
    out = tmp_path / "uploadN"
    out.mkdir()
    capsys.readouterr()
    assert main(["audit", str(pkg), "--json"]) == 0
    (pkg / "package_audit.json").write_text(capsys.readouterr().out)
    repl = out / "replace-manifest.json"
    assert main(["make-manifest", "--video", str(pkg / f"{name}.mp4"), "--cover", str(pkg / f"{name}.cover.png"),
                 "--package-audit", str(pkg / "package_audit.json"), "--title", TITLE, "--final-human-review",
                 str(pkg / f"{name}.finished-media-review.json"), "--authorized-by", "频道主", "--quote", "修复完的可以编辑上传",
                 "--out", str(repl)]) == 0
    plan, journal, done = out / "plan.json", tmp_path / "base/reports/same_bv_repair_ledger.jsonl", out / "done.json"
    assert main(["cover-repair-plan", "--manifest", str(repl), "--bvid", bvid, "--out", str(plan), "--journal", str(journal)]) == 0
    walk = ["--plan", str(plan), "--journal", str(journal)]
    assert main(["cover-repair-run", *walk]) == 0
    assert main(["cover-repair-verify-live", *walk, "--out", str(done)]) == 0
    rows = [r for r in load_publication_registry(runtime_path=registry.path())["entries"] if r.get("bvid") == bvid]
    assert [(r["candidate_id"], r["publication_reconciliation"]["status"], r["publication_reconciliation"]["cid"])
            for r in rows] == [(name, "VERIFIED_SAME_BV_COVER", cid)]
    chain = receipt_chain.resolve(rows[0]["publication_reconciliation"]["authority"])
    assert chain.first_receipt == receipt and chain.online_cover_sha256 == _sha(pkg / f"{name}.cover.png")
