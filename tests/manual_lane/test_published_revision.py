"""A published clip is revised (new subtitles, a new cover, a cut picture) in a successor clip of the same manuscript:
the successor id (`_r<N>`, `_trim`), the `revise` command, the same-BV repair authority step 8 builds for a successor and
passes to the finished-media lane's check and seal, what the successor changes against the published version, and the
imported (decided) cover.  The registry and the first-publication closure are fake files under tmp; the finished-media
lane is the recorder of test_fmr_calls; nothing reaches ssh, CPA or the network."""

from __future__ import annotations

import hashlib
import io
import json

import pytest
from fakes import FakeCpa, FakeRemote, make_ctx
from test_fmr_calls import RD, REVIEW_PASS, Recorder, _patch, _prepared

from src.autoslice import publication_registry
from src.autoslice.publication_reconciliation import authority_sidecar_path
from src.autoslice.manual_lane import cli, cover_import, dissent_register, publication_id, review_owner, revise_run, revision_authority, title_history
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane import step6_title
from src.autoslice.manual_lane.adapters import Adapters, RemoteResult
from src.autoslice.manual_lane.dissent_resolve import resolve_dissent
from src.autoslice.manual_lane.reselect import SUPERSEDED
from src.autoslice.manual_lane.state import ClipState, StateError
from tests.test_publication_reconciliation import TITLE, _new_bv_fixture, _reconcile_new
from tests.test_publication_reconciliation import first_publication_registries  # noqa: F401  (the fixture)

MANUSCRIPT = "fm-20261024-clip"     # the lane's candidate id of cid "clip"


# ----- the manuscript of a clip id ------------------------------------------------------------------------------------
@pytest.mark.parametrize("cid, manuscript", [("clip", "clip"), ("clip_trim", "clip"), ("clip_r2", "clip"),
                                             ("clip_trim_r3", "clip"), ("clip_r2_r3", "clip"), ("a_trim_b", "a_trim_b"),
                                             ("trim_x", "trim_x"), ("clip_r", "clip_r"), ("clip_rx2", "clip_rx2")])
def test_publication_cid_strips_the_successor_endings_and_only_those(cid, manuscript):
    assert publication_id.publication_cid(cid) == manuscript


def test_every_user_of_the_manuscript_follows_it(monkeypatch):
    monkeypatch.setattr(title_history, "load_publication_registry", lambda: {"entries": [
        {"candidate_id": MANUSCRIPT, "status": "published", "bvid": "BV1", "title": "T"}]})
    for cid in ("clip_r2", "clip_trim_r3"):
        assert sp.member_slug(cid) == "clip" and title_history.same_clip(MANUSCRIPT, cid) and title_history.is_published(cid)
        assert title_history.published_titles(cid)[0] == []
    assert not title_history.same_clip(MANUSCRIPT, "clip2_r2")
    assert publication_id.revision_base("clip_trim_r2") == "clip_trim" and publication_id.revision_base("clip_r2_r3") == "clip_r2"


# ----- step 8 of a successor of a published manuscript ----------------------------------------------------------------
def _closure(tmp_path, monkeypatch):
    """A first-publication closure for MANUSCRIPT (receipt, evidence, registry row) as fake files; its video is b"video"
    and its cover b"cover"."""
    fixture = _new_bv_fixture(tmp_path / "first", candidate=MANUSCRIPT, date="2026-10-24")
    _reconcile_new(fixture)
    monkeypatch.setattr(title_history, "load_publication_registry", publication_registry.load_publication_registry)
    return fixture


def _successor(tmp_path, monkeypatch, *, cid="clip_r2", video=b"video", cover=b"cover", review=None):
    ctx, cpa = _prepared(tmp_path / "task", cpa_review=review)
    ctx.state.data["clip"]["cid"] = cid
    ctx.adapters.remote.files[f"{RD}/burn/final.mp4"] = video
    (ctx.clip_dir / "cover/cover.png").write_bytes(cover)
    (ctx.clip_dir / "title/title.txt").write_text(TITLE + "\n", encoding="utf-8")
    rec = Recorder()
    _patch(monkeypatch, rec)
    return ctx, rec


def _arg(call, flag):
    return call[call.index(flag) + 1]


@pytest.mark.parametrize("cid", ["clip_r2", "clip_trim", "clip_trim_r2"])
def test_a_successor_of_a_published_manuscript_carries_the_authority_through_check_and_seal(
        tmp_path, monkeypatch, first_publication_registries, cid):  # noqa: F811
    fixture = _closure(tmp_path, monkeypatch)
    ctx, rec = _successor(tmp_path, monkeypatch, cid=cid, review=REVIEW_PASS)
    out = sp.step8_package(ctx)
    assert out.ok, out.note
    authority_file = ctx.clip_dir / revision_authority.AUTHORITY_FILE
    authority = json.loads(authority_file.read_text(encoding="utf-8"))
    assert authority["bvid"] == fixture["bvid"] and authority["candidate_id"] == MANUSCRIPT and authority["title"] == TITLE
    assert out.products["repair_authority"] == str(authority_file) and out.products["revision"].endswith("revision.json")
    assert sp.step9_review(ctx).ok
    assert [c[0] for c in rec.calls] == ["init", "check", "attest", "seal"]
    check, attest, seal = rec.calls[1], rec.calls[2], rec.calls[3]
    assert _arg(check, "--repair-authority") == _arg(seal, "--repair-authority") == str(authority_file)
    assert "--repair-authority" not in attest


def _damaged(fixture, damage):
    registry = json.loads(fixture["registry_path"].read_text(encoding="utf-8"))
    damage(fixture, registry["entries"])
    fixture["registry_path"].write_text(json.dumps(registry), encoding="utf-8")


def _drop_receipt_binding(fixture, rows):
    (fixture["base"] / "state" / "publication_registry.runtime.v1.json").unlink(missing_ok=True)    # it would merge the row back
    rows[0]["publication_reconciliation"].pop("authority")


def _change_receipt(fixture, rows):
    receipt = authority_sidecar_path(fixture["manifest_path"])
    receipt.write_text(receipt.read_text(encoding="utf-8") + "\n", encoding="utf-8")


def _delete_receipt(fixture, rows):
    authority_sidecar_path(fixture["manifest_path"]).unlink()


@pytest.mark.parametrize("name, damage, reason", [
    ("two published rows", lambda fx, rows: rows.append({**rows[0], "recording_date": "2026-10-25"}), "2 published rows"),
    ("receipt hash differs", _change_receipt, "PUBLICATION_RUNTIME_REGISTRY_INVALID"),      # the registry's own read refuses it
    ("receipt gone", _delete_receipt, "PUBLICATION_RUNTIME_REGISTRY_INVALID"),
    ("no receipt bound", _drop_receipt_binding, "binds no first-publication receipt"),
])
def test_a_successor_without_a_buildable_authority_is_not_packaged_whatever_the_cause(
        tmp_path, monkeypatch, first_publication_registries, name, damage, reason):  # noqa: F811
    """The guard: a successor of a published manuscript stops at the start of step 8 with the builder's reason."""
    fixture = _closure(tmp_path, monkeypatch)
    _damaged(fixture, damage)
    ctx, rec = _successor(tmp_path, monkeypatch)
    out = sp.step8_package(ctx)
    assert not out.ok and out.reason == "REVISION_AUTHORITY_UNBUILT" and reason in out.note, out.note
    assert "clip_r2" in out.note and rec.calls == [] and not (ctx.clip_dir / revision_authority.AUTHORITY_FILE).exists()


def test_a_registry_that_cannot_be_read_stops_a_successor_before_anything_is_built(tmp_path, monkeypatch):
    ctx, rec = _successor(tmp_path, monkeypatch)
    monkeypatch.setattr(title_history, "load_publication_registry", lambda: (_ for _ in ()).throw(OSError("gone")))
    out = sp.step8_package(ctx)
    assert not out.ok and out.reason == "REVISION_AUTHORITY_UNBUILT" and "读不到出版登记" in out.note and rec.calls == []


def test_a_successor_of_an_unpublished_manuscript_gets_no_authority_and_no_flag(tmp_path, monkeypatch):
    ctx, cpa = _prepared(tmp_path / "task", cpa_review=REVIEW_PASS)
    ctx.state.data["clip"]["cid"] = "clip_trim"
    (ctx.clip_dir / revision_authority.AUTHORITY_FILE).write_text("stale", encoding="utf-8")
    (ctx.clip_dir / revision_authority.REVISION_FILE).write_text("stale", encoding="utf-8")
    monkeypatch.setattr(title_history, "load_publication_registry", lambda: {"entries": [
        {"candidate_id": "fm-20261024-other", "status": "published", "bvid": "BV2"},
        {"candidate_id": MANUSCRIPT, "status": "hold_pending_review", "bvid": ""}]})
    rec = Recorder()
    _patch(monkeypatch, rec)
    assert sp.step8_package(ctx).ok and sp.step9_review(ctx).ok
    assert [c[0] for c in rec.calls] == ["init", "check", "attest", "seal"]
    assert all("--repair-authority" not in c for c in rec.calls)
    assert not (ctx.clip_dir / revision_authority.AUTHORITY_FILE).exists() and not (ctx.clip_dir / revision_authority.REVISION_FILE).exists()


def test_a_clip_that_is_not_a_successor_never_reads_the_registry_for_an_authority(tmp_path, monkeypatch):
    ctx, _ = _prepared(tmp_path / "task")
    monkeypatch.setattr(title_history, "load_publication_registry", lambda: pytest.fail("the registry was read"))
    rec = Recorder()
    _patch(monkeypatch, rec)
    assert sp.step8_package(ctx).ok and all("--repair-authority" not in c for c in rec.calls)


def test_the_published_original_itself_is_refused_with_the_way_to_redo_it(tmp_path, monkeypatch):
    ctx, _ = _prepared(tmp_path / "task")
    old = sp.package_dir(ctx)
    old.mkdir(parents=True)
    (old / "review_manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(title_history, "load_publication_registry", lambda: {"entries": [
        {"candidate_id": MANUSCRIPT, "status": "published", "bvid": "BV1"}]})
    rec = Recorder()
    _patch(monkeypatch, rec)
    out = sp.step8_package(ctx)
    assert out.reason == "PUBLISHED_PACKAGE_PROTECTED" and "`revise` 建后继片" in out.note and rec.calls == []


# ----- what the successor changes against the published version -------------------------------------------------------
@pytest.mark.parametrize("video, cover, changed", [(b"video", b"another cover", {"video": False, "cover": True}),
                                                   (b"another video", b"cover", {"video": True, "cover": False}),
                                                   (b"video", b"cover", {"video": False, "cover": False})])
def test_the_package_record_says_whether_video_and_cover_differ_from_the_published_ones(
        tmp_path, monkeypatch, first_publication_registries, video, cover, changed):  # noqa: F811
    fixture = _closure(tmp_path, monkeypatch)
    ctx, _rec = _successor(tmp_path, monkeypatch, video=video, cover=cover)
    assert sp.step8_package(ctx).ok
    doc = json.loads((ctx.clip_dir / revision_authority.REVISION_FILE).read_text(encoding="utf-8"))
    assert doc["changed"] == changed and doc["bvid"] == fixture["bvid"] and doc["manuscript"] == "clip"
    assert doc["published"] == {"video_sha256": hashlib.sha256(b"video").hexdigest(),
                                "cover_sha256": hashlib.sha256(b"cover").hexdigest()}
    assert doc["revision"] == {"video_sha256": hashlib.sha256(video).hexdigest(),
                               "cover_sha256": hashlib.sha256(cover).hexdigest()}


# ----- revise ---------------------------------------------------------------------------------------------------------
REVISED_KEY = {"position": "kept by whatever the state holds"}


def _remote_of(scripts):
    return FakeRemote(on_script=lambda name, body, remote: (scripts.append((name, body)), RemoteResult(0, "", ""))[1])


def _published_clip(tmp_path, monkeypatch, *, published=True):
    """Clip "clip" of a task, finished through step 9 and packaged, with a sealed package; the registry says published."""
    scripts: list[tuple[str, str]] = []
    ctx = make_ctx(tmp_path, remote=_remote_of(scripts), cpa=None)
    c = ctx.clip_dir
    for rel, text in {"boundary/decision.json": "{}", "dedup/result.json": "{}", "cut/probe.json": "{}",
                      "asr/bcut.srt": "1\n00:00:00,000 --> 00:00:01,000\n句\n", "subs/final.srt": "1\n00:00:00,000 --> 00:00:01,000\n句\n",
                      "speaker/speaker.srt": "1\n00:00:00,000 --> 00:00:01,000\n[李豆沙] 句\n", "subs/doubts.json": "[]",
                      "title/title.txt": "标题\n", "burn/final.ass": "ass",
                      "fmr/out/2026-10-24/fm-20261024-clip/review_manifest.json": "{}",
                      "fmr/out/2026-10-24/fm-20261024-clip/final.mp4": "mp4"}.items():
        (c / rel).parent.mkdir(parents=True, exist_ok=True)
        (c / rel).write_text(text, encoding="utf-8")
    for key in ("boundary", "dedupe", "cut", "subtitles", "speaker", "burn", "title_cover", "tags", "package", "review"):
        ctx.state.record(key, ok=True, reason="", products={"x": str(c / "cut/probe.json")}, cpa=[{"call_id": "k"}], note="n")
    ctx.state.data["imported"] = {"source": "push", "from": "/p", "authority": "U1", "files": {}, "at": "t", "decided_by": "owner"}
    ctx.state.data["position"] = REVISED_KEY
    ctx.state.data["review"] = {"call": None}
    ctx.state.data["upload"] = {"enabled": True, "authorized_quote": "q"}
    ctx.state.save()
    rows = [{"candidate_id": MANUSCRIPT, "status": "published" if published else "hold_pending_review", "bvid": "BV1"}]
    monkeypatch.setattr(title_history, "load_publication_registry", lambda: {"entries": rows})
    return ctx, scripts


def _tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def _adapters_for(remote):
    return lambda remote_dir: Adapters(remote=remote)


def test_revise_refuses_a_clip_that_is_not_published_and_leaves_everything(tmp_path, monkeypatch):
    ctx, scripts = _published_clip(tmp_path, monkeypatch, published=False)
    before, state_before = _tree(tmp_path / "clips"), (tmp_path / "state/clip.json").read_bytes()
    with pytest.raises(StateError, match="no published row"):
        revise_run.revise_clip(task=tmp_path, cid="clip", reason="r", adapters_for=_adapters_for(ctx.adapters.remote))
    assert _tree(tmp_path / "clips") == before and (tmp_path / "state/clip.json").read_bytes() == state_before
    assert scripts == [] and sorted(p.name for p in (tmp_path / "state").iterdir()) == ["clip.json"]


def test_revise_builds_the_next_successor_and_leaves_the_original_package_and_tree_alone(tmp_path, monkeypatch):
    ctx, scripts = _published_clip(tmp_path, monkeypatch)
    before = _tree(tmp_path / "clips" / "clip")
    got = revise_run.revise_clip(task=tmp_path, cid="clip", reason="字幕错字", adapters_for=_adapters_for(ctx.adapters.remote))
    assert got == "clip_r2"
    assert _tree(tmp_path / "clips" / "clip") == before                         # X's tree: not one file changed
    x = ClipState.load(tmp_path / "state/clip.json").data["clip"]
    assert x["status"] == SUPERSEDED and x["superseded_by"] == "clip_r2" and "字幕错字" in x["superseded"]["quote"]
    y = ClipState.load(tmp_path / "state/clip_r2.json")
    clip = y.data["clip"]
    assert clip["cid"] == "clip_r2" and clip["remote_dir"] == "/r/clip_r2" and "status" not in clip
    assert (clip["start_ms"], clip["end_ms"], clip["mac_bcut_srt"]) == (10_000, 80_000, ctx.state.data["clip"]["mac_bcut_srt"])
    assert set(y.data["steps"]) == {"boundary", "dedupe", "cut"}                                           # steps 1, 0, 2 only
    assert all(rec["status"] == "DONE" and str(tmp_path / "clips/clip_r2/") in rec["products"]["x"] for rec in y.data["steps"].values())
    assert y.data["position"] == REVISED_KEY and y.data["revised_from"]["cid"] == "clip" and "review" not in y.data
    assert y.data["upload"] == {"enabled": False, "authorized_quote": None}
    imported = y.data["imported"]
    assert imported["source"] == "revise" and imported["verbatim"] is True and imported["decided_by"] == "owner"
    for name in ("final.srt", "speaker.srt", "doubts.json"):
        assert (tmp_path / "clips/clip_r2/import" / name).is_file()
    for rel in ("boundary/decision.json", "cut/probe.json", "asr/bcut.srt"):
        assert (tmp_path / "clips/clip_r2" / rel).is_file()
    assert not (tmp_path / "clips/clip_r2/fmr").exists()
    (name, body), = scripts
    assert name == "revise-copy-cut" and "/r/clip/cut" in body and "/r/clip_r2/cut" in body


def test_revising_again_numbers_beside_and_continues_from_the_latest_successor(tmp_path, monkeypatch):
    ctx, _ = _published_clip(tmp_path, monkeypatch)
    adapters_for = _adapters_for(ctx.adapters.remote)
    assert revise_run.revise_clip(task=tmp_path, cid="clip", reason="一", adapters_for=adapters_for) == "clip_r2"
    with pytest.raises(StateError, match="step subtitles of clip_r2 is not DONE"):          # it has not been run yet
        revise_run.revise_clip(task=tmp_path, cid="clip_r2", reason="二", adapters_for=adapters_for)
    r2 = ClipState.load(tmp_path / "state/clip_r2.json")
    r2.record("subtitles", ok=True, reason="", products={}, cpa=[], note="")
    (tmp_path / "clips/clip_r2/subs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "clips/clip_r2/subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n新句\n", encoding="utf-8")
    with pytest.raises(StateError, match="superseded by clip_r2"):
        revise_run.revise_clip(task=tmp_path, cid="clip", reason="二", adapters_for=adapters_for)
    assert revise_run.revise_clip(task=tmp_path, cid="clip_r2", reason="二", adapters_for=adapters_for) == "clip_r3"
    assert ClipState.load(tmp_path / "state/clip_r2.json").data["clip"]["superseded_by"] == "clip_r3"
    assert revise_run.successor_id(tmp_path, "clip_trim") == "clip_trim_r2"


def test_a_failed_copy_on_the_media_host_leaves_no_successor(tmp_path, monkeypatch):
    ctx, _ = _published_clip(tmp_path, monkeypatch)
    remote = FakeRemote(on_script=lambda name, body, r: RemoteResult(1, "", "no space"))
    with pytest.raises(StateError, match="no space"):
        revise_run.revise_clip(task=tmp_path, cid="clip", reason="r", adapters_for=_adapters_for(remote))
    assert not (tmp_path / "state/clip_r2.json").exists() and not (tmp_path / "clips/clip_r2").exists()
    assert "superseded_by" not in ClipState.load(tmp_path / "state/clip.json").data["clip"]


def test_the_two_commands_are_in_the_parser():
    parser = cli.build_parser()
    assert parser.parse_args(["revise", "--task-dir", "T", "--cid", "c", "--reason", "r"]).func is cli.cmd_revise
    args = parser.parse_args(["import-cover", "--task-dir", "T", "--cid", "c", "--source-dir", "D", "--decided-by", "push",
                              "--authority", "a"])
    assert args.func is cli.cmd_import_cover and args.decided_by == "push"
    with pytest.raises(SystemExit):
        parser.parse_args(["import-cover", "--task-dir", "T", "--cid", "c", "--source-dir", "D", "--decided-by", "agent",
                           "--authority", "a"])


# ----- an imported cover ----------------------------------------------------------------------------------------------
def _png(size=(1920, 1080), color="red"):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _cover_push(root, *, size=(1920, 1080), lines=("第一行", "第二行"), **meta):
    (root / "cover").mkdir(parents=True)
    (root / "cover/cover.png").write_bytes(_png(size))
    (root / "cover/cover.json").write_text(json.dumps({"lines": list(lines), **meta}, ensure_ascii=False), encoding="utf-8")
    return root


def test_import_cover_refuses_a_wrong_size_a_non_png_and_empty_lines_and_changes_nothing(tmp_path):
    ctx = make_ctx(tmp_path / "task", remote=None, cpa=None)
    ctx.state.record("title_cover", ok=True, reason="", products={}, cpa=[], note="")
    before = (tmp_path / "task/state/clip.json").read_bytes()
    kw = {"decided_by": "owner", "authority": "a"}
    with pytest.raises(StateError, match="1280x720"):
        cover_import.import_cover(ctx.state, ctx.clip_dir, _cover_push(tmp_path / "p1", size=(1280, 720)), **kw)
    (tmp_path / "p2/cover").mkdir(parents=True)
    (tmp_path / "p2/cover/cover.png").write_bytes(b"not a png at all")
    (tmp_path / "p2/cover/cover.json").write_text('{"lines": ["甲"]}', encoding="utf-8")
    with pytest.raises(StateError, match="not a PNG"):
        cover_import.import_cover(ctx.state, ctx.clip_dir, tmp_path / "p2", **kw)
    with pytest.raises(StateError, match="lines"):
        cover_import.import_cover(ctx.state, ctx.clip_dir, _cover_push(tmp_path / "p3", lines=[]), **kw)
    with pytest.raises(StateError, match="decided_by"):
        cover_import.import_cover(ctx.state, ctx.clip_dir, _cover_push(tmp_path / "p4", decided_by="push"), **kw)
    assert (tmp_path / "task/state/clip.json").read_bytes() == before and not (ctx.clip_dir / "import_cover").exists()


def test_import_cover_records_the_push_and_forgets_step_6_and_later_only(tmp_path):
    ctx = make_ctx(tmp_path / "task", remote=None, cpa=None)
    for key in ("subtitles", "speaker", "burn", "title_cover", "tags", "package", "review", "upload"):
        ctx.state.record(key, ok=True, reason="", products={}, cpa=[], note="")
    ctx.state.data["review"] = {"call": None}
    rec = cover_import.import_cover(ctx.state, ctx.clip_dir, _cover_push(tmp_path / "p", note="n"), decided_by="owner",
                                    authority="U7")
    state = ClipState.load(ctx.state.path).data
    assert state["imported_cover"] == rec and set(state["steps"]) == {"subtitles", "speaker", "burn"} and "review" not in state
    assert rec["lines"] == ["第一行", "第二行"] and rec["decided_by"] == "owner" and rec["authority"] == "U7" and rec["note"] == "n"
    assert rec["png_sha256"] == hashlib.sha256(_png()).hexdigest() and rec["at"]
    assert (ctx.clip_dir / "import_cover/cover.png").read_bytes() == _png()


def _step6(tmp_path, monkeypatch, *, lines=("甲", "乙")):
    cpa = FakeCpa([])
    remote = FakeRemote()
    ctx = make_ctx(tmp_path / "task", remote=remote, cpa=cpa)
    (ctx.clip_dir / "subs").mkdir(parents=True)
    (ctx.clip_dir / "subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n句\n", encoding="utf-8")
    cover_import.import_cover(ctx.state, ctx.clip_dir, _cover_push(tmp_path / "push", lines=lines), decided_by="owner",
                              authority="U7")

    def pick_title(c):      # the title half is not what is tested: it hands over the generated title and its model cover lines
        path = c.clip_dir / "title/candidates.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cover_lines": ["模型封面字"]}), encoding="utf-8")
        (c.clip_dir / "title/title.txt").write_text("标题\n", encoding="utf-8")
        return "标题", ["模型封面字"], ["标题已定"], [], path

    monkeypatch.setattr(step6_title, "pick_title", pick_title)
    return ctx, cpa, remote


def test_step_6_takes_the_imported_cover_as_it_is_and_asks_no_model(tmp_path, monkeypatch):
    ctx, cpa, remote = _step6(tmp_path, monkeypatch)
    out = step6_title.step6_title_cover(ctx)
    assert out.ok, out.note
    assert cpa.calls == [] and remote.scripts == []                      # no image, no vision, no text model
    assert (ctx.clip_dir / "cover/cover.png").read_bytes() == _png() and out.products["cover"] == str(ctx.clip_dir / "cover/cover.png")
    choose = json.loads((ctx.clip_dir / "cover/cand/choose.json").read_text(encoding="utf-8"))
    assert choose["chosen"] == "imported" and choose["source"] == "imported" and choose["authority"] == "U7"
    assert choose["cover_lines"] == ["甲", "乙"] and choose["decided_by"] == "owner"
    cands = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert cands["cover_lines"] == ["甲", "乙"] and cands["cover_route"]["method"] == "imported"
    assert cands["cover_route"]["image_generation_used"] is False and "导入的定稿封面" in "\n".join(out.process)


def test_the_imported_picture_changed_after_import_stops_step_6(tmp_path, monkeypatch):
    ctx, cpa, _ = _step6(tmp_path, monkeypatch)
    (ctx.clip_dir / "import_cover/cover.png").write_bytes(_png(color="blue"))
    out = step6_title.step6_title_cover(ctx)
    assert not out.ok and out.reason == "IMPORTED_COVER_CHANGED" and cpa.calls == []


def test_a_successor_inherits_the_imported_cover_of_its_original(tmp_path, monkeypatch):
    ctx, scripts = _published_clip(tmp_path, monkeypatch)
    cover_import.import_cover(ctx.state, ctx.clip_dir, _cover_push(tmp_path / "push"), decided_by="push", authority="a")
    assert revise_run.revise_clip(task=tmp_path, cid="clip", reason="r", adapters_for=_adapters_for(ctx.adapters.remote)) == "clip_r2"
    y = ClipState.load(tmp_path / "state/clip_r2.json").data["imported_cover"]
    x = ClipState.load(tmp_path / "state/clip.json").data["imported_cover"]
    assert y["png_sha256"] == x["png_sha256"] and y["lines"] == x["lines"] and y["decided_by"] == "push"
    assert y["files"]["cover.png"] == str(tmp_path / "clips/clip_r2/import_cover/cover.png")
    assert (tmp_path / "clips/clip_r2/import_cover/cover.png").read_bytes() == _png()


def test_a_review_that_wants_another_cover_is_an_objection_in_the_register_and_nothing_is_replaced(tmp_path):
    ctx, _ = _prepared(tmp_path / "task")
    cover_import.import_cover(ctx.state, ctx.clip_dir, _cover_push(tmp_path / "push"), decided_by="push", authority="a")
    (ctx.clip_dir / "cover/cover.png").write_bytes(_png())
    before = (ctx.clip_dir / "cover/cover.png").read_bytes(), (ctx.clip_dir / "title/candidates.json").read_bytes()
    answer, dissent = review_owner.split(ctx, {"fixes": {"cover": ["新字", "另一行"], "tags": None}}, [])
    assert dissent == [{"kind": "cover", "cue": None, "text": "第一行 / 第二行", "proposed": "新字 / 另一行",
                        "value": "新字 / 另一行", "reason": review_owner.NO_REASON}]
    assert "cover" not in answer["fixes"] and answer["fixes"]["tags"] is None
    applied = sp.apply_fixes(ctx, {"fixes": {"cover": ["新字", "另一行"]}})
    assert applied["dissent"] == dissent and applied["title"] is False
    assert ((ctx.clip_dir / "cover/cover.png").read_bytes(), (ctx.clip_dir / "title/candidates.json").read_bytes()) == before
    # the same register as the subtitles' objections: kept by default (never stops the package), a cover can only be kept
    [item] = dissent_register.items(ctx.state)
    assert item["kind"] == "cover" and item["n"] == 1 and "改封面" in dissent_register.line(item)
    assert item["resolution"]["default"] is True and item["resolution"]["by"] == dissent_register.DEFAULT_BY
    ctx.state.save()
    with pytest.raises(StateError, match="只能 --keep"):
        resolve_dissent(tmp_path / "task", "clip", item=1, how="apply", by="u", reason="r")
    resolve_dissent(tmp_path / "task", "clip", item=1, how="keep", by="u", reason="封面定稿")
    [named] = dissent_register.items(ClipState.load(tmp_path / "task/state/clip.json"))
    assert named["resolution"]["by"] == "u" and "default" not in named["resolution"]
    same, none = review_owner.split(ctx, {"fixes": {"cover": ["第一行", "第二行"]}}, [])
    assert none == []                                   # the same lines: nothing to object to


def test_only_a_clip_with_an_imported_cover_offers_the_review_a_cover_slot(tmp_path):
    ctx, _ = _prepared(tmp_path / "task")
    assert cover_import.review_slot(ctx) == ""
    cover_import.import_cover(ctx.state, ctx.clip_dir, _cover_push(tmp_path / "push"), decided_by="owner", authority="a")
    assert '"cover"' in cover_import.review_slot(ctx)
