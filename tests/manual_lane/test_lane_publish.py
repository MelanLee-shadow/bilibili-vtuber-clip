"""lane_publish, the public upload tool, against a fake Bilibili: a first publication, a video replacement and then a cover
replacement on the same BV, each through the command line step 10 sends, an edit of only the title, tags and description, and what the lane reads back from them (the receipt
chain, the packages used online, the same-BV authority, step 10's plan comparison).  Nothing reaches the network or biliup."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path

import pytest

from src.autoslice import bilibili_member_api
from src.autoslice import publication_registry
from src.autoslice.lane_publish import audit, authority, bili, cover_rotation, first, manifest, metadata, registry, repair
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
        self.stored_type: int | None = None   # the declared type Bilibili stores whatever was submitted (None: as submitted)
        self.public_without: tuple[str, ...] = ()    # fields the public page does not give

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
        kind, source = self.stored_type or int(flags["--copyright"]), flags.get("--source", "")
        self.archives[bvid] = {"aid": 7000 + len(self.runs), "bvid": bvid, "title": flags["--title"],
                               "desc": (source + "\n" if source else "") + flags["--desc"],   # Bilibili puts a repost's source ahead
                               "tag": flags["--tag"], "cover": "https://upload/first.png", "copyright": kind, "source": source,
                               "no_reprint": int(kind == 1), "tid": int(flags["--tid"]),
                               "videos": [{"cid": cid, "filename": "n1", "title": flags["--title"]}]}
        self._publish(bvid)
        Path(log).write_text(f'ResponseData {{ data: {{ "bvid": String("{bvid}") }} }}\n', encoding="utf-8")
        return 0

    def _publish(self, bvid: str) -> None:
        a = self.archives[bvid]
        self.public[bvid] = {"aid": a["aid"], "bvid": bvid, "cid": a["videos"][0]["cid"], "title": a["title"], "desc": a["desc"],
                             "pic": a["cover"].replace("https://upload/", "http://i0.hdslb.com/bfs/archive/"),
                             "copyright": a["copyright"], "rights": {"no_reprint": a.get("no_reprint", 0)},
                             "pages": [{"cid": v["cid"], "part": v["title"]} for v in a["videos"]]}
        for field in self.public_without:
            self.public[bvid].pop(field)

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


REPOST_SOURCE = "https://live.bilibili.com/"     # the source of the archives published as reposts (before)


def package(root: Path, *, video: bytes = b"video-1", cover: bytes = PNG, title: str = TITLE, extra: dict | None = None,
            name: str = "fm-20261114-clip-a", description: str = "简介第一行\n第二行\n") -> Path:
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
    (pkg / f"{name}.description.txt").write_text(description, encoding="utf-8")
    (pkg / "review_manifest.json").write_text(json.dumps({"schema_version": "finished-media-review-package.v1",
                                                          "candidate_id": name, "date": "2026-11-14", "items": [item]}))
    return pkg


def _first(host, tmp_path, capsys, title: str = TITLE, **package_fields) -> tuple[Path, dict]:
    pkg = package(tmp_path / "pkg1", title=title, **package_fields)
    name, out = pkg.name, tmp_path / "upload1"
    out.mkdir()
    assert main(["audit", str(pkg), "--json"]) == 0
    (out / "package_audit.json").write_text(capsys.readouterr().out)
    man = out / "authorized-upload-manifest.json"
    assert main(["make-manifest", "--video", str(pkg / f"{name}.mp4"), "--cover", str(pkg / f"{name}.cover.png"),
                 "--package-audit", str(out / "package_audit.json"), "--title", title, "--tags", "虚拟主播,切片",
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
    # posted without a declared type: copyright 3, no source given, and the description has no source line
    sent, online = host.runs[0], host.archives[receipt["bvid"]]
    assert sent[sent.index("--copyright") + 1] == "3" and "--source" not in sent
    assert (online["copyright"], online["source"], online["desc"]) == (3, "", "简介第一行\n第二行")
    assert (receipt["public"]["copyright"], receipt["public"]["no_reprint"]) == (3, 0)
    man = json.loads((tmp_path / "upload1/authorized-upload-manifest.json").read_text())
    assert man["publish_policy"] == {"tid": 27, "copyright": 3, "source": ""}


def test_a_package_written_for_a_repost_is_submitted_without_its_source_line(host, tmp_path, capsys):
    """A package sealed before carries the source line a reposted archive shows first; posted now, without a
    declared type, that line is not part of the description (sent along 11-15 to 11-18, it showed twice)."""
    _pkg, receipt = _first(host, tmp_path, capsys, description=REPOST_SOURCE + "\n简介第一行\n第二行\n")
    assert host.archives[receipt["bvid"]]["desc"] == "简介第一行\n第二行"


@pytest.mark.parametrize("stored,without,shown", [
    (1, (), "copyright=1"),                    # Bilibili took 3 as 自制: the archive now claims to be our own work
    (2, (), "copyright=2"),
    (None, ("copyright",), "copyright=None"),  # the page gives no type: not known is not right
    (None, ("rights",), "no_reprint=None"),
])
def test_a_bv_online_with_another_declared_type_than_the_rule_is_not_registered_and_stops_new_bvs(host, tmp_path, capsys,
                                                                                                  stored, without, shown):
    host.stored_type, host.public_without = stored, without
    pkg = package(tmp_path / "pkg1")
    out = tmp_path / "upload1"
    out.mkdir()
    (out / "a.json").write_text(json.dumps(audit.audit(pkg)))
    man = out / "m.json"
    manifest.make(video=pkg / f"{pkg.name}.mp4", cover=pkg / f"{pkg.name}.cover.png", package_audit=out / "a.json", title=TITLE,
                  tags="虚拟主播,切片", final_human_review=None, authorized_by="x", quote="y", out=man)
    assert main(["upload", "--manifest", str(man)]) == 5
    err = capsys.readouterr().err
    assert "与清单不符" in err and shown in err and "没有登记" in err
    bvid = next(iter(host.archives))
    assert not first.receipt_path(man).exists() and registry.rows_for(registry.load()["entries"], bvid=bvid) == []
    assert host.seasons == []                                      # nothing more was done to the archive
    row = [r for r in registry.ledger_rows() if r["event"] == first.TYPE_MISMATCH][-1]
    assert row["bvid"] == bvid and row["public"]["copyright"] == (None if "copyright" in without else stored or 3)
    # the same manifest again reads the same BV again (never a second post) and still does not register it
    assert main(["upload", "--manifest", str(man)]) == 5 and len(host.runs) == 1
    capsys.readouterr()
    # no other new BV while that one has not been looked at
    host.stored_type, host.public_without = None, ()
    other = package(tmp_path / "pkg2", name="fm-20261114-clip-b")
    (out / "b.json").write_text(json.dumps(audit.audit(other)))
    man2 = out / "m2.json"
    manifest.make(video=other / f"{other.name}.mp4", cover=other / f"{other.name}.cover.png", package_audit=out / "b.json",
                  title=TITLE, tags="虚拟主播,切片", final_human_review=None, authorized_by="x", quote="y", out=man2)
    assert main(["upload", "--manifest", str(man2)]) == 3 and len(host.runs) == 1
    assert bvid in capsys.readouterr().err
    registry.ledger_add(first.TYPE_CLEARED, bvid=bvid, note="looked at")
    assert main(["upload", "--manifest", str(man2)]) == 0 and len(host.runs) == 2


def _repost_rule(monkeypatch) -> None:
    """lane_rules upload.publish as it was before a repost of the live room."""
    import dataclasses

    from src.autoslice.lane_publish.common import Publish

    before = dataclasses.replace(Publish.load(), copyright=2, source=REPOST_SOURCE)
    monkeypatch.setattr(Publish, "load", classmethod(lambda _cls: before))


def _made(host, tmp_path, name: str = "fm-20261114-clip-a") -> Path:
    pkg = package(tmp_path / f"pkg-{name}", name=name)
    out = tmp_path / f"upload-{name}"
    out.mkdir()
    (out / "a.json").write_text(json.dumps(audit.audit(pkg)))
    man = out / "m.json"
    manifest.make(video=pkg / f"{pkg.name}.mp4", cover=pkg / f"{pkg.name}.cover.png", package_audit=out / "a.json", title=TITLE,
                  tags="虚拟主播,切片", final_human_review=None, authorized_by="x", quote="y", out=man)
    return man


def test_a_bv_sent_as_a_repost_before_the_rule_changed_is_checked_for_what_it_was_sent_as(host, tmp_path, capsys, monkeypatch):
    """A BV posted under the repost rule and still in review when the rule became 3: the run after the change reads it back
    against its manifest (2 and the live room), registers it, and new BVs are not stopped."""
    import itertools

    with monkeypatch.context() as before:
        _repost_rule(before)
        man = _made(host, tmp_path)
        assert json.loads(man.read_text())["publish_policy"] == {"tid": 27, "copyright": 2, "source": REPOST_SOURCE}
        post = host.run

        def in_review(argv, **kw):
            rc = post(argv, **kw)
            host.public.pop(next(reversed(host.archives)))
            return rc

        before.setattr(bili, "RUN", in_review)
        before.setattr(first, "CLOCK", itertools.count(0, 10_000).__next__)
        assert main(["upload", "--manifest", str(man)]) == 7
    bvid = next(iter(host.archives))
    assert host.archives[bvid]["copyright"] == 2
    sent = host.runs[0]
    assert sent[sent.index("--copyright") + 1] == "2" and sent[sent.index("--source") + 1] == REPOST_SOURCE
    host._publish(bvid)                                         # the review is over; the rule is now 3
    assert main(["upload", "--manifest", str(man)]) == 0 and len(host.runs) == 1
    receipt = json.loads(first.receipt_path(man).read_text())
    assert (receipt["bvid"], receipt["public"]["copyright"]) == (bvid, 2)
    assert [r for r in registry.ledger_rows() if r["event"] == first.TYPE_MISMATCH] == []
    capsys.readouterr()
    assert main(["upload", "--manifest", str(_made(host, tmp_path, "fm-20261114-clip-b"))]) == 0 and len(host.runs) == 2


def test_a_manifest_made_under_the_repost_rule_and_not_sent_is_refused_before_anything_is_sent(host, tmp_path, capsys,
                                                                                                 monkeypatch):
    with monkeypatch.context() as before:
        _repost_rule(before)
        man = _made(host, tmp_path)
    assert main(["upload", "--manifest", str(man)]) == 3
    assert first.STALE_POLICY in capsys.readouterr().err
    assert host.runs == [] and [r for r in registry.ledger_rows() if r.get("manifest_sha256") == _sha(man)] == []
    # made again under the rule as it is: posted as 3, and the dispatch row says what was sent
    again = _made(host, tmp_path, "fm-20261114-clip-b")
    assert main(["upload", "--manifest", str(again)]) == 0
    sent = host.runs[0]
    assert sent[sent.index("--copyright") + 1] == "3" and "--source" not in sent
    row = [r for r in registry.ledger_rows() if r["event"] == "dispatched"][-1]
    assert row["publish_policy"] == {"tid": 27, "copyright": 3, "source": ""}


def test_a_manifest_without_a_policy_a_new_bv_can_have_is_refused(host, tmp_path, capsys):
    man = _made(host, tmp_path)
    doc = json.loads(man.read_text())
    for policy in (None, {"tid": 27, "copyright": 1, "source": ""}, {"tid": 27, "copyright": 3, "source": REPOST_SOURCE}):
        doc["publish_policy"] = policy
        man.write_text(json.dumps(doc, ensure_ascii=False))
        assert main(["upload", "--manifest", str(man)]) == 2 and "publish_policy" in capsys.readouterr().err
    assert host.runs == []


def test_the_rule_posts_no_declared_type_and_never_a_claim_of_own_work(monkeypatch):
    from src.autoslice.lane_publish.common import Publish, Refused
    from src.autoslice.manual_lane import lane_rules

    policy = Publish.load()
    assert (policy.tid, policy.copyright, policy.source) == (27, 3, "")
    waits = {"public_wait_s": 1, "public_poll_s": 1, "new_part_wait_s": 1}
    for kind, source in ((1, ""), (3, REPOST_SOURCE), (2, "")):
        monkeypatch.setattr(lane_rules, "rules", lambda _section, kind=kind, source=source: {
            "publish": {"tid": 27, "copyright": kind, "source": source, **waits}})
        with pytest.raises(Refused):
            Publish.load()


def test_a_repair_keeps_the_source_line_of_an_archive_published_as_a_repost():
    repost = {"copyright": 2, "source": REPOST_SOURCE}
    assert repair.stored_description("甲\n乙", repost) == REPOST_SOURCE + "\n甲\n乙"
    assert repair.stored_description(REPOST_SOURCE + "\n甲\n乙", repost) == REPOST_SOURCE + "\n甲\n乙"    # a package sealed before
    undeclared = {"copyright": 3, "source": ""}
    assert repair.stored_description("甲\n乙", undeclared) == "甲\n乙"
    assert repair.stored_description(REPOST_SOURCE + "\n甲\n乙", undeclared) == "甲\n乙"


def test_the_channels_repair_authority_builder_takes_this_tools_receipt(host, tmp_path, capsys):
    """Step 8 of a revision builds its same-BV authority with src/autoslice/new_bv_repair_authority (
    semantictalk_1084420_r2 stopped there, 'first-publication evidence closure is missing')."""
    from src.autoslice import new_bv_repair_authority as channel_builder

    from src.autoslice.channel_envelope import render_talk_title

    titled = render_talk_title("假如我变成薯条，你也会喜欢我吗？")   # the channel's title policy applies here
    pkg, receipt = _first(host, tmp_path, capsys, title=titled)
    path = first.receipt_path(tmp_path / "upload1/authorized-upload-manifest.json")
    where = {"runtime_path": registry.path()}
    built = channel_builder.build_new_bv_repair_authority(path, candidate_id=pkg.name, title=titled, **where)
    assert built == authority.build(path, candidate_id=pkg.name, title=titled)
    assert channel_builder.validate_new_bv_repair_authority(built, candidate_id=pkg.name, **where) == built
    assert channel_builder.replay_first_publication_manifest(built, **where)["title"] == titled
    man = Path(receipt["evidence"]["manifest"]["path"])
    man.write_text(man.read_text(encoding="utf-8").replace(titled, "别的标题"), encoding="utf-8")
    with pytest.raises(ValueError):
        channel_builder.validate_new_bv_repair_authority(built, candidate_id=pkg.name, **where)


def test_upload_again_or_after_a_stop_never_posts_a_second_bv(host, tmp_path, capsys):
    _pkg, receipt = _first(host, tmp_path, capsys)
    man = tmp_path / "upload1/authorized-upload-manifest.json"
    assert main(["upload", "--manifest", str(man)]) == 0 and len(host.runs) == 1          # the receipt is there: nothing sent
    first.receipt_path(man).unlink()
    registry.path().write_text(json.dumps({"schema_version": registry.SCHEMA, "entries": []}))
    assert main(["upload", "--manifest", str(man)]) == 0 and len(host.runs) == 1          # posted before: resumes from the BV
    assert json.loads(first.receipt_path(man).read_text())["bvid"] == receipt["bvid"]


def test_pack_cover_is_checked_against_the_last_actual_lane_publication(host, tmp_path, capsys, monkeypatch):
    # Only this test's runtime rows, not the channel's pre-existing published history.
    monkeypatch.setattr(registry, "load", lambda root=None: json.loads(registry.path().read_text())
                        if registry.path().is_file() else {"entries": []})

    def make_clip(name, template):
        clip = tmp_path / "task" / "clips" / name
        pkg = package(clip / "fmr", name=f"fm-20261114-{name}", video=name.encode())
        (clip / "title").mkdir(parents=True)
        (clip / "cover").mkdir()
        (clip / "cover" / "cover.png").write_bytes((pkg / f"{pkg.name}.cover.png").read_bytes())
        cand_dir = clip / "cover" / "cand"
        cand_dir.mkdir()
        (cand_dir / "screenshot.png").write_bytes((clip / "cover" / "cover.png").read_bytes())
        choice = cand_dir / "choose.json"
        choice.write_text(json.dumps({"chosen": "screenshot", "rows": [{"id": "screenshot",
                                      "cover_layout": {"template_id": template}}]}), encoding="utf-8")
        candidate = clip / "title" / "candidates.json"
        candidate.write_text(json.dumps({"cover_route": {"method": "skill.candidates", "guests": {"source": "pack"},
                              "chosen": {"id": "screenshot", "template_id": template}}}), encoding="utf-8")
        out = clip / "upload"
        out.mkdir()
        (out / "audit.json").write_text(json.dumps(audit.audit(pkg)))
        man = out / "authorized-upload-manifest.json"
        manifest.make(video=pkg / f"{pkg.name}.mp4", cover=pkg / f"{pkg.name}.cover.png",
                      package_audit=out / "audit.json", title=TITLE, tags="虚拟主播,切片", final_human_review=None,
                      authorized_by="x", quote="y", out=man)
        return man, candidate, choice, pkg

    first_man, first_candidate, _, _ = make_clip("clip-a", "panels-top")
    assert main(["upload", "--manifest", str(first_man)]) == 0
    original = first_candidate.read_text()
    first_candidate.write_text("{}")
    first.receipt_path(first_man).unlink()
    registry.path().write_text(json.dumps({"schema_version": registry.SCHEMA, "entries": []}))
    assert main(["upload", "--manifest", str(first_man)]) == 0 and len(host.runs) == 1
    first_candidate.write_text(original)
    second_man, candidate, choice, pkg = make_clip("clip-b", "panels-diagonal")
    first_candidate.write_text(original + " ")
    assert main(["upload", "--manifest", str(second_man)]) == 3 and len(host.runs) == 1
    assert "sha256 不符" in capsys.readouterr().err
    first_candidate.write_text(original)
    assert main(["upload", "--manifest", str(second_man)]) == 3
    assert "都是 collab-split" in capsys.readouterr().err and len(host.runs) == 1
    data = json.loads(candidate.read_text())
    data["cover_route"]["chosen"]["template_id"] = "collab-dialogue"
    candidate.write_text(json.dumps(data), encoding="utf-8")
    assert main(["upload", "--manifest", str(second_man)]) == 3 and len(host.runs) == 1
    selected = json.loads(choice.read_text())
    selected["rows"][0]["cover_layout"]["template_id"] = "collab-dialogue"
    choice.write_text(json.dumps(selected), encoding="utf-8")
    manifest.make(video=pkg / f"{pkg.name}.mp4", cover=pkg / f"{pkg.name}.cover.png",
                  package_audit=second_man.parent / "audit.json", title=TITLE, tags="虚拟主播,切片", final_human_review=None,
                  authorized_by="x", quote="y", out=second_man)
    assert main(["upload", "--manifest", str(second_man)]) == 0 and len(host.runs) == 2


def test_pack_cover_allows_frozen_forced_adjacent_repeat_but_refuses_unforced_and_tampered(host, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(registry, "load", lambda root=None: json.loads(registry.path().read_text())
                        if registry.path().is_file() else {"entries": []})
    tick = iter(range(1, 30))

    def fixed_now():
        return f"2026-11-28T01:00:{next(tick):02d}Z"

    monkeypatch.setattr(registry, "now", fixed_now)
    monkeypatch.setattr(first, "now", fixed_now)

    def make_clip(name, template, *, fallback=None, record_layout=None):
        clip = tmp_path / "task" / "clips" / name
        pkg = package(clip / "fmr", name=f"fm-20261114-{name}", video=name.encode())
        (clip / "title").mkdir(parents=True)
        (clip / "cover").mkdir()
        (clip / "cover" / "cover.png").write_bytes((pkg / f"{pkg.name}.cover.png").read_bytes())
        cand_dir = clip / "cover" / "cand"
        cand_dir.mkdir()
        (cand_dir / "screenshot.png").write_bytes((clip / "cover" / "cover.png").read_bytes())
        choice = cand_dir / "choose.json"
        choice.write_text(json.dumps({"chosen": "screenshot", "rows": [{"id": "screenshot",
                                      "cover_layout": {"template_id": template, "fallback": fallback}}]}), encoding="utf-8")
        chosen = {"id": "screenshot", "template_id": template}
        if record_layout is not None:
            chosen["cover_layout"] = record_layout
        candidate = clip / "title" / "candidates.json"
        candidate.write_text(json.dumps({"cover_route": {"method": "skill.candidates", "guests": {"source": "pack"},
                                                         "chosen": chosen}}), encoding="utf-8")
        out = clip / "upload"
        out.mkdir()
        (out / "audit.json").write_text(json.dumps(audit.audit(pkg)))
        man = out / "authorized-upload-manifest.json"
        manifest.make(video=pkg / f"{pkg.name}.mp4", cover=pkg / f"{pkg.name}.cover.png",
                      package_audit=out / "audit.json", title=TITLE, tags="虚拟主播,切片", final_human_review=None,
                      authorized_by="x", quote="y", out=man)
        return man, choice

    first_man, _ = make_clip("clip-1", "panels-top", fallback=None)
    assert main(["upload", "--manifest", str(first_man)]) == 0 and len(host.runs) == 1

    # Unforced and non-adjacent fallbacks of the same family as the last published cover are still refused.
    for tag, fb in (("unforced", None), ("recent", "recent_repeat_forced"), ("below", "punch_below_floor")):
        bad_man, bad_choice = make_clip(f"clip-2-{tag}", "panels-bottom", fallback=fb)
        assert main(["upload", "--manifest", str(bad_man)]) == 3 and len(host.runs) == 1
        assert "都是 collab-split" in capsys.readouterr().err
        if tag == "unforced":
            # Tampering choose.json after freezing without re-freezing must fail hash verification.
            tampered = json.loads(bad_choice.read_text(encoding="utf-8"))
            tampered["rows"][0]["cover_layout"]["fallback"] = "adjacent_repeat_forced"
            bad_choice.write_text(json.dumps(tampered), encoding="utf-8")
            assert main(["upload", "--manifest", str(bad_man)]) == 3 and len(host.runs) == 1
            assert "sha256 不符" in capsys.readouterr().err

    # Mismatch between candidates.json cover_layout and choose.json cover_layout is refused at freeze/check.
    from src.autoslice.lane_publish.common import Refused
    with pytest.raises(Refused, match="回退记录不一致"):
        make_clip("clip-2-mismatch", "panels-bottom", fallback="adjacent_repeat_forced",
                  record_layout={"template_id": "panels-bottom", "fallback": None})

    # Properly frozen forced adjacent repeat passes (both pure and +punch_below_floor).
    forced_man, _ = make_clip("clip-2-forced", "panels-bottom", fallback="adjacent_repeat_forced")
    assert main(["upload", "--manifest", str(forced_man)]) == 0 and len(host.runs) == 2

    forced_below_man, _ = make_clip(
        "clip-3-forced-below", "panels-diagonal", fallback="adjacent_repeat_forced+punch_below_floor",
        record_layout={"template_id": "panels-diagonal", "fallback": "adjacent_repeat_forced+punch_below_floor"},
    )
    assert main(["upload", "--manifest", str(forced_below_man)]) == 0 and len(host.runs) == 3

    # An unforced same-family successor after a forced cover is still refused.
    unforced_after, _ = make_clip("clip-4-unforced-after-forced", "panels-top", fallback=None)
    assert main(["upload", "--manifest", str(unforced_after)]) == 3 and len(host.runs) == 3
    assert "都是 collab-split" in capsys.readouterr().err

    # Pending-upload checks stay strict even for forced-repeat manifests.
    with pytest.raises(Refused, match="实际先后尚未确定"):
        cover_rotation.check(forced_man, json.loads(forced_man.read_text(encoding="utf-8")),
                             [{"event": "posted", "manifest_sha256": "other"}], [])


def test_pack_cover_refuses_an_unresolved_prior_dispatch_and_a_changed_cover(tmp_path):
    clip = tmp_path / "task" / "clips" / "current"
    (clip / "title").mkdir(parents=True)
    (clip / "cover").mkdir()
    (clip / "upload").mkdir()
    (clip / "cover" / "cover.png").write_bytes(PNG)
    (clip / "cover" / "cand").mkdir()
    (clip / "cover" / "cand" / "screenshot.png").write_bytes(PNG)
    (clip / "cover" / "cand" / "choose.json").write_text(json.dumps({"chosen": "screenshot", "rows": [
        {"id": "screenshot", "cover_layout": {"template_id": "collab-pair"}}]}))
    (clip / "title" / "candidates.json").write_text(json.dumps({"cover_route": {
        "method": "skill.candidates", "guests": {"source": "pack"},
        "chosen": {"id": "screenshot", "template_id": "collab-pair"}}}))
    man = clip / "upload" / "manifest.json"
    man.write_text("{}")
    doc = {"cover": {"sha256": _sha(clip / "cover" / "cover.png")}}
    doc["cover_rotation"] = cover_rotation.freeze(man, doc)
    from src.autoslice.lane_publish.common import Refused

    with pytest.raises(Refused, match="实际先后尚未确定"):
        cover_rotation.check(man, doc, [{"event": "posted", "manifest_sha256": "other"}], [])
    prior = [{"event": "published", "manifest_sha256": "previous", "bvid": "BV1prior", "at": "2026-11-24T17:00:00Z"}]
    elsewhere = [{"status": "published", "bvid": "BV1other", "publication_reconciliation": {
        "status": "VERIFIED_PUBLIC", "reconciled_at": "2026-11-24T12:00:00-05:00"}}]
    with pytest.raises(Refused, match="另一个投稿入口"):
        cover_rotation.check(man, doc, prior, elsewhere)
    (clip / "cover" / "cover.png").write_bytes(b"different")
    with pytest.raises(Refused, match="文件变了"):
        cover_rotation.check(man, doc, [], [])


def test_lane_first_publication_uses_the_legacy_upload_lock_too(host):
    import fcntl

    from src.autoslice.lane_publish.common import Refused

    with (host.base / "upload.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Refused, match=LOCK_BUSY):
            with registry.first_upload_locked():
                pytest.fail("the first-publication gate entered while the older uploader held the lock")


def test_a_dispatch_without_a_bv_quota_and_a_held_lock_are_refused_with_step_10s_codes(host, tmp_path, capsys, monkeypatch):
    pkg = package(tmp_path / "p")
    out = tmp_path / "u"
    out.mkdir()
    (out / "a.json").write_text(json.dumps(audit.audit(pkg)))
    man = out / "m.json"
    manifest.make(video=pkg / f"{pkg.name}.mp4", cover=pkg / f"{pkg.name}.cover.png", package_audit=out / "a.json", title=TITLE,
                  tags="虚拟主播,切片", final_human_review=None, authorized_by="x", quote="y", out=man)

    def quota(argv, *, cwd, log, timeout):
        Path(log).write_text("code: 21566 投稿过于频繁")
        return 1

    monkeypatch.setattr(bili, "RUN", quota)
    assert main(["upload", "--manifest", str(man)]) == 8 and "21566" in capsys.readouterr().err
    with registry.locked():
        assert main(["upload", "--manifest", str(man)]) == 4 and LOCK_BUSY in capsys.readouterr().err
    registry.ledger_add("dispatched", manifest_sha256=_sha(man))
    assert main(["upload", "--manifest", str(man)]) == 3 and "没有 BV" in capsys.readouterr().err


def test_a_public_read_carries_the_upload_accounts_login_cookie(host, tmp_path, monkeypatch):
    #, the first real run: Bilibili answered the anonymous read-back from the server with HTTP 412
    import io
    import urllib.request

    cookies = Path(json.loads((host.base / "lane_publish.json").read_text())["biliup_cookies"])
    cookies.write_text(json.dumps({"cookie_info": {"cookies": [{"name": "SESSDATA", "value": "s"},
                                                               {"name": "bili_jct", "value": "j"}]}}))
    sent = []

    def urlopen(request, timeout):
        sent.append(request)
        return io.BytesIO(b'{"code": 0, "data": {"cid": 1}}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert bili._fetch(bili.VIEW.format(bvid="BV1x")) == {"code": 0, "data": {"cid": 1}}
    assert sent[0].unredirected_hdrs["Cookie"] == "SESSDATA=s; bili_jct=j"


def test_a_package_with_an_unresolved_subtitle_doubt_is_not_sent(host, tmp_path, capsys):

    doubt = {"final_review": {"unresolved_findings_disclosed": [{"cue_index": 7, "suspect": "李姐"}]}}
    pkg = package(tmp_path / "p", extra=doubt)
    out = tmp_path / "u"
    out.mkdir()
    (out / "a.json").write_text(json.dumps(dict(audit.audit(pkg), passed=True)))     # an audit that let it through
    args = dict(video=pkg / f"{pkg.name}.mp4", cover=pkg / f"{pkg.name}.cover.png", package_audit=out / "a.json", title=TITLE,
                tags="虚拟主播,切片", final_human_review=None, authorized_by="x", quote="y", out=out / "m.json")
    with pytest.raises(manifest.Refused, match="POSTBURN_UNRESOLVED_FINDINGS"):
        manifest.make(**args)
    clean = package(tmp_path / "q")
    (out / "b.json").write_text(json.dumps(audit.audit(clean)))
    manifest.make(**dict(args, video=clean / f"{clean.name}.mp4", cover=clean / f"{clean.name}.cover.png",
                         package_audit=out / "b.json"))
    record = clean / f"{clean.name}.record.json"
    record.write_text(json.dumps(dict(json.loads(record.read_text()), **doubt)))      # a doubt written after the manifest
    assert main(["upload", "--manifest", str(out / "m.json")]) != 0 and host.runs == []
    capsys.readouterr()


def test_the_collection_is_read_where_every_uploader_reads_it_and_a_channel_without_one_stops(host, tmp_path, monkeypatch):
    # the first real run refused the default channel, whose talk collection is clip_profile's own route
    from src.autoslice import clip_profile

    monkeypatch.undo()          # the real lookup: the default channel, no season_routes asset
    monkeypatch.setenv("AUTOSLICE_SEASON_IDS", json.dumps({"talk": {"season_id": 8110001, "section_id": 9110001}}))  # public build
    season = manifest._season()
    assert (season["season_title"], season["season_id"], season["section_id"]) == (
        clip_profile.SEASON_TITLES["talk"], *clip_profile.season_ids()["talk"].values())
    monkeypatch.setattr(clip_profile, "season_ids", lambda: {})      # a channel with no collections at all
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
                 str(pkg / f"{name}.finished-media-review.json"), "--authorized-by", "频道主", "--quote", "修好的稿件可以编辑上传（示例频道的授权；换成你自己的授权文字）",
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

    # then only the title and tags: the chain still says which video and cover are online and which completion to follow
    done4 = _metadata_edit(tmp_path, bvid, "--title", "【主播】改过的标题", "--tags", "虚拟主播,切片,嗦面")
    assert len(host.appends) == 1 and json.loads(done4.read_text())["unchanged_cid"] == completed["new_cid"]
    row = registry.rows_for(registry.load()["entries"], bvid=bvid)[0]
    chain = receipt_chain.resolve(row["publication_reconciliation"]["authority"])
    assert chain.bound == done4 and chain.predecessor_completed == done and chain.predecessor_new_cid == completed["new_cid"]
    assert chain.online_cover_sha256 == _sha(pkg3 / f"{pkg3.name}.cover.png") and chain.online_tags == ("虚拟主播", "切片", "嗦面")
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
    job2 = Job("replace_video", receipt["bvid"], pkg2, out2, TITLE, "", None, "频道主", "修好的稿件可以编辑上传（示例频道的授权；换成你自己的授权文字）", **tools)
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
    host.archives[bvid] = {"aid": aid, "bvid": bvid, "title": TITLE, "desc": "https://live.bilibili.com/\n简介第一行\n第二行", "tag": "虚拟主播,切片",
                           "cover": "https://upload/old.png", "copyright": 2, "source": REPOST_SOURCE, "tid": 27,
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
                 str(pkg / f"{name}.finished-media-review.json"), "--authorized-by", "频道主", "--quote", "修好的稿件可以编辑上传（示例频道的授权；换成你自己的授权文字）",
                 "--out", str(repl)]) == 0
    plan, journal, done = out / "plan.json", tmp_path / "base/reports/same_bv_repair_ledger.jsonl", out / "done.json"
    assert main(["cover-repair-plan", "--manifest", str(repl), "--bvid", bvid, "--out", str(plan), "--journal", str(journal)]) == 0
    walk = ["--plan", str(plan), "--journal", str(journal)]
    assert main(["cover-repair-run", *walk]) == 0
    assert main(["cover-repair-verify-live", *walk, "--out", str(done)]) == 0
    # the archive was published as a repost and stays one: its type, its source and the source line ahead of its description
    # are sent back as they are, though the successor package's own description has no such line
    assert "publish_policy" not in json.loads(repl.read_text())
    assert (host.edits[-1]["copyright"], host.edits[-1]["source"]) == (2, REPOST_SOURCE)
    assert host.edits[-1]["desc"] == host.archives[bvid]["desc"] == REPOST_SOURCE + "\n简介第一行\n第二行"
    rows = [r for r in load_publication_registry(runtime_path=registry.path())["entries"] if r.get("bvid") == bvid]
    assert [(r["candidate_id"], r["publication_reconciliation"]["status"], r["publication_reconciliation"]["cid"])
            for r in rows] == [(name, "VERIFIED_SAME_BV_COVER", cid)]
    chain = receipt_chain.resolve(rows[0]["publication_reconciliation"]["authority"])
    assert chain.first_receipt == receipt and chain.online_cover_sha256 == _sha(pkg / f"{name}.cover.png")


def _metadata_edit(tmp_path, bvid: str, *values: str, n: int = 1) -> Path:
    """metadata-plan, -run, -verify-live through the command line; the completion receipt."""
    work = tmp_path / f"meta{bvid}{n}"
    plan, journal, done = work / "plan.json", work / "journal.jsonl", work / "completed.json"
    assert main(["metadata-plan", "--bvid", bvid, *values, "--authorized-by", "维护者", "--quote", "标题为什么不加嗦",
                 "--out", str(plan), "--journal", str(journal)]) == 0
    walk = ["--plan", str(plan), "--journal", str(journal)]
    assert main(["metadata-run", *walk]) == 0 and main(["metadata-status", *walk]) == 0
    assert main(["metadata-verify-live", *walk, "--out", str(done)]) == 0
    return done


def test_a_metadata_edit_changes_only_the_title_tags_and_description_by_the_registry_row_alone(host, tmp_path, capsys):
    import shutil

    pkg, receipt = _first(host, tmp_path, capsys)
    bvid, first_receipt = receipt["bvid"], first.receipt_path(tmp_path / "upload1/authorized-upload-manifest.json")
    shutil.rmtree(pkg.parent)                     # the package, its video and cover are gone (the recording long before)
    src = "https://live.bilibili.com/\n"
    host.archives[bvid].update(copyright=2, source=REPOST_SOURCE)   # a repost, as every archive of that time
    host.archives[bvid]["desc"] = src + src + "简介第一行\n第二行"      # what 11-15 to 11-18 first publications show
    host._publish(bvid)
    before = dict(host.archives[bvid], videos=list(host.archives[bvid]["videos"]))
    capsys.readouterr()
    done = _metadata_edit(tmp_path, bvid, "--title", "【主播】哭着说的测试标题", "--tags", "虚拟主播,切片,嗦面", "--fix-source-line")
    after = host.archives[bvid]
    assert (after["title"], after["tag"], after["desc"]) == ("【主播】哭着说的测试标题", "虚拟主播,切片,嗦面", src + "简介第一行\n第二行")
    assert (after["cover"], after["videos"][0]["cid"], len(after["videos"])) == (before["cover"], before["videos"][0]["cid"], 1)
    assert host.appends == [] and len(host.edits) == 1 and len(host.runs) == 1 and host.edits[0]["videos"][0]["title"] == after["title"]
    completed = json.loads(done.read_text())
    assert completed["schema_version"] == receipt_chain.METADATA_COMPLETED and completed["status"] == "VERIFIED_FRESH_LIVE"
    assert completed["live_snapshot"]["public"]["tags"] == ["虚拟主播", "切片", "嗦面"] and completed["unchanged_cid"] == receipt["cid"]
    # the registry row binds the new receipt, as every reader validates it, and its title is the new one
    row = registry.rows_for(registry.load()["entries"], bvid=bvid)[0]
    assert row["publication_reconciliation"]["status"] == metadata.ROW_STATUS == "VERIFIED_SAME_BV_METADATA"
    assert row["publication_reconciliation"]["title"] == "【主播】哭着说的测试标题"
    seen = [r for r in load_publication_registry(runtime_path=registry.path())["entries"] if r.get("bvid") == bvid]
    assert [r["publication_reconciliation"]["authority"]["path"] for r in seen] == [str(done)]
    chain = receipt_chain.resolve(row["publication_reconciliation"]["authority"])
    assert chain.first_receipt == first_receipt and chain.predecessor_completed is None and chain.completions == ()
    assert (chain.online_tags, chain.online_desc) == (("虚拟主播", "切片", "嗦面"), src + "简介第一行\n第二行")
    # a stop and a run again: no second edit, the same receipt
    walk = ["--plan", str(done.parent / "plan.json"), "--journal", str(done.parent / "journal.jsonl")]
    assert main(["metadata-run", *walk]) == 0 and main(["metadata-verify-live", *walk, "--out", str(done)]) == 0
    assert len(host.edits) == 1 and json.loads(done.read_text()) == completed
    # a second edit on top: the chain walks both back to the first publication
    done2 = _metadata_edit(tmp_path, bvid, "--tags", "虚拟主播,嗦面", n=2)
    chain = receipt_chain.resolve(registry.rows_for(registry.load()["entries"], bvid=bvid)[0]["publication_reconciliation"]["authority"])
    assert chain.bound == done2 and chain.first_receipt == first_receipt and chain.online_tags == ("虚拟主播", "嗦面")
    assert host.archives[bvid]["title"] == "【主播】哭着说的测试标题" and json.loads(done2.read_text())["previous"] == bind_of(done)


def bind_of(path: Path) -> dict:
    return {"path": str(path), "sha256": _sha(path), "bytes": path.stat().st_size}


def _plan_args(tmp_path, *which: str) -> list[str]:
    return ["metadata-plan", *which, "--title", "【主播】新标题", "--authorized-by", "维护者", "--quote", "改",
            "--out", str(tmp_path / "p.json"), "--journal", str(tmp_path / "j.jsonl")]


def test_a_metadata_edit_of_a_bv_the_registry_does_not_hold_as_published_is_refused(host, tmp_path, capsys):
    _pkg, receipt = _first(host, tmp_path, capsys)
    for which in (["--bvid", "BV1notours001"], ["--cid", "clip_never_published"]):
        assert main(_plan_args(tmp_path, *which)) == 3 and "已发布行有 0 条" in capsys.readouterr().err
    registry.path().write_text("{not json")                         # unreadable is not "nothing published": refused too
    assert main(_plan_args(tmp_path, "--bvid", receipt["bvid"])) == 3 and "读不到出版登记" in capsys.readouterr().err
    assert host.edits == [] and not (tmp_path / "p.json").exists()


@pytest.mark.parametrize("hold", ["bvid", "candidate"])
def test_a_metadata_edit_of_a_held_bv_or_manuscript_is_refused(host, tmp_path, capsys, monkeypatch, hold):
    pkg, receipt = _first(host, tmp_path, capsys)
    held = {"candidate_id": pkg.name if hold == "candidate" else "fm-20261113-other", "recording_date": "2026-11-13",
            "status": "hold_pending_review", "note": "维护者 搁置"} | ({"bvid": receipt["bvid"]} if hold == "bvid" else {})
    committed = tmp_path / "committed_registry.json"
    committed.write_text(json.dumps({"schema_version": publication_registry.REGISTRY_SCHEMA, "entries": [held]}))
    monkeypatch.setattr(publication_registry, "DEFAULT_REGISTRY_PATH", committed)
    for which in (["--bvid", receipt["bvid"]], ["--cid", "clip-a"]):
        assert main(_plan_args(tmp_path, *which)) == 3 and "hold_pending_review" in capsys.readouterr().err
    assert host.edits == [] and not (tmp_path / "p.json").exists()


def test_a_metadata_readback_that_differs_from_the_plan_writes_no_receipt_and_moves_no_registry_row(host, tmp_path, capsys,
                                                                                                   monkeypatch):
    _pkg, receipt = _first(host, tmp_path, capsys)
    bvid = receipt["bvid"]
    authority_before = registry.rows_for(registry.load()["entries"], bvid=bvid)[0]["publication_reconciliation"]["authority"]
    plan, journal, done = tmp_path / "p.json", tmp_path / "j.jsonl", tmp_path / "done.json"
    assert main(["metadata-plan", "--bvid", bvid, "--title", "【主播】新标题", "--tags", "虚拟主播,切片,嗦面", "--authorized-by", "维护者",
                 "--quote", "改", "--out", str(plan), "--journal", str(journal)]) == 0
    walk = ["--plan", str(plan), "--journal", str(journal)]
    # Bilibili took the edit but dropped a tag: never live, and verify-live refuses
    edit = host.edit_archive

    def drops_a_tag(payload):
        return edit({**payload, "tag": payload["tag"].rsplit(",", 1)[0]})

    monkeypatch.setattr(host, "edit_archive", drops_a_tag)
    assert main(["metadata-run", *walk]) == 6 and main(["metadata-run", *walk]) == 6 and len(host.edits) == 1
    assert main(["metadata-verify-live", *walk, "--out", str(done)]) == 3
    # the tag comes back, the run sees it live; then the page changes again before the receipt: refused, nothing written
    host.archives[bvid]["tag"] = "虚拟主播,切片,嗦面"
    assert main(["metadata-run", *walk]) == 0 and len(host.edits) == 1
    host.public[bvid] = {**host.public[bvid], "title": "别人改的标题"}
    capsys.readouterr()
    assert main(["metadata-verify-live", *walk, "--out", str(done)]) == 5 and "读回与计划不符" in capsys.readouterr().err
    assert not done.exists()
    row = registry.rows_for(registry.load()["entries"], bvid=bvid)[0]
    assert row["publication_reconciliation"]["authority"] == authority_before
    assert row["publication_reconciliation"]["status"] == "VERIFIED_PUBLIC"


def test_a_metadata_edit_is_planned_only_against_a_public_single_part_bv_that_matches_its_row(host, tmp_path, capsys):
    _pkg, receipt = _first(host, tmp_path, capsys)
    bvid = receipt["bvid"]
    host.public[bvid] = None                                        # under review: not now
    assert main(_plan_args(tmp_path, "--bvid", bvid)) == 3 and "不是公开状态" in capsys.readouterr().err
    host._publish(bvid)
    host.public[bvid]["cid"] = 1                                    # online is not what the registry says
    assert main(_plan_args(tmp_path, "--bvid", bvid)) == 3 and "不符" in capsys.readouterr().err
    host._publish(bvid)
    args = _plan_args(tmp_path, "--bvid", bvid)
    args[args.index("【主播】新标题")] = receipt["title"]
    assert main(args) == 3 and "没有要改的" in capsys.readouterr().err
    args[args.index(receipt["title"])] = "没有前缀的标题"                # the channel's envelope stays
    assert main(args) == 2 and "【主播】" in capsys.readouterr().err
    assert host.edits == []
