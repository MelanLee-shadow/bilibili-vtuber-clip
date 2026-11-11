"""What the channel owner decided follows the manuscript, not one clip id: a finished cover imported into X (`import-cover`)
is in every successor made from it, `trim` (`<X>_trim`) as well as `revise` (`<X>_r<N>`), at any generation, and step 6 of the
successor takes it as it takes a directly imported one: no picture is drawn or looked at, no model asked about it.  An
`import-cover` on the successor afterwards replaces it.  The title half of step 6 is not what is tested."""

from __future__ import annotations

import hashlib
import json

from fakes import FakeCpa, FakeRemote
from test_published_revision import _adapters_for, _cover_push, _png, _published_clip
from test_trim_run import Y_RD, Y, answer, make_x, run

from src.autoslice.manual_lane import cover_import, revise_run, step6_title
from src.autoslice.manual_lane.runner import Ctx
from src.autoslice.manual_lane.state import ClipState

SHA = hashlib.sha256(_png()).hexdigest()


def _imported_into(ctx, tmp_path, **kw):
    return cover_import.import_cover(ctx.state, ctx.clip_dir, _cover_push(tmp_path / "push", **kw), decided_by="owner",
                                     authority="频道主定稿：封面")


def _step6_of(tmp_path, cid, remote, monkeypatch, *, remote_dir):
    """Step 6 of the successor `cid`, the title half stubbed (as test_published_revision._step6 does)."""
    state = ClipState.load(tmp_path / "state" / f"{cid}.json")
    cpa = FakeCpa([])
    ctx = Ctx(state=state, session=state.data["session"], clip_dir=tmp_path / "clips" / cid, remote_dir=remote_dir,
              adapters=_adapters(remote, cpa))

    def pick_title(c):
        path = c.clip_dir / "title/candidates.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"cover_lines": ["模型封面字"]}), encoding="utf-8")
        (c.clip_dir / "title/title.txt").write_text("标题\n", encoding="utf-8")
        return "标题", ["模型封面字"], ["标题已定"], [], path

    monkeypatch.setattr(step6_title, "pick_title", pick_title)
    return ctx, cpa


def _adapters(remote, cpa):
    from src.autoslice.manual_lane.adapters import Adapters

    return Adapters(remote=remote, cpa=cpa)


def _assert_taken_as_imported(ctx, cpa, remote, *, lines=("第一行", "第二行"), sha=SHA, decided_by="owner"):
    out = step6_title.step6_title_cover(ctx)
    assert out.ok, out.note
    assert cpa.calls == [] and remote.scripts == []                            # no picture drawn or looked at, no model asked
    cover = ctx.clip_dir / "cover/cover.png"
    assert hashlib.sha256(cover.read_bytes()).hexdigest() == sha
    cands = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))
    assert cands["cover_lines"] == list(lines) and cands["cover_route"]["method"] == "imported"
    assert cands["cover_route"]["image_generation_used"] is False and cands["cover_route"]["decided_by"] == decided_by


def test_a_trim_successor_takes_the_imported_cover_and_step_6_asks_nothing(tmp_path, monkeypatch):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]))
    rec = _imported_into(ctx, tmp_path)
    assert run(ctx)["status"] == "TRIMMED"
    y = ClipState.load(tmp_path / "state/clip_trim.json").data["imported_cover"]
    assert {k: y[k] for k in ("png_sha256", "lines", "decided_by", "authority", "note", "at", "source")} == {
        k: rec[k] for k in ("png_sha256", "lines", "decided_by", "authority", "note", "at", "source")}      # basis and words as X's
    assert y["files"] == {"cover.png": str(tmp_path / "clips/clip_trim/import_cover/cover.png"),
                          "cover.json": str(tmp_path / "clips/clip_trim/import_cover/cover.json")}
    assert (tmp_path / "clips/clip/import_cover/cover.png").read_bytes() == _png()                       # X's own files stay
    remote = FakeRemote()
    ctx_y, cpa = _step6_of(tmp_path, Y, remote, monkeypatch, remote_dir=Y_RD)
    _assert_taken_as_imported(ctx_y, cpa, remote)


def test_a_trim_of_a_clip_without_an_imported_cover_makes_no_cover_record(tmp_path):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]))
    assert run(ctx)["status"] == "TRIMMED"
    assert "imported_cover" not in ClipState.load(tmp_path / "state/clip_trim.json").data
    assert not (tmp_path / "clips/clip_trim/import_cover").exists()


def test_an_import_into_the_successor_afterwards_is_the_one_that_counts(tmp_path, monkeypatch):
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]))
    _imported_into(ctx, tmp_path)
    assert run(ctx)["status"] == "TRIMMED"
    state_y = ClipState.load(tmp_path / "state/clip_trim.json")
    new = cover_import.import_cover(state_y, tmp_path / "clips/clip_trim", _cover_push(tmp_path / "push2", lines=("新一行",)),
                                    decided_by="push", authority="新定稿")
    assert new["png_sha256"] == SHA                                        # the same bytes, its own record and lines
    assert ClipState.load(tmp_path / "state/clip_trim.json").data["imported_cover"]["lines"] == ["新一行"]
    assert ClipState.load(tmp_path / "state/clip.json").data["imported_cover"]["lines"] == ["第一行", "第二行"]
    remote = FakeRemote()
    ctx_y, cpa = _step6_of(tmp_path, Y, remote, monkeypatch, remote_dir=Y_RD)
    _assert_taken_as_imported(ctx_y, cpa, remote, lines=("新一行",), decided_by="push")
    assert json.loads((ctx_y.clip_dir / "cover/cand/choose.json").read_text(encoding="utf-8"))["authority"] == "新定稿"


def test_a_revise_successor_and_its_successor_take_the_imported_cover_and_step_6_asks_nothing(tmp_path, monkeypatch):
    ctx, _scripts = _published_clip(tmp_path, monkeypatch)
    _imported_into(ctx, tmp_path)
    adapters_for = _adapters_for(ctx.adapters.remote)
    assert revise_run.revise_clip(task=tmp_path, cid="clip", reason="一", adapters_for=adapters_for) == "clip_r2"
    r2 = ClipState.load(tmp_path / "state/clip_r2.json")
    r2.record("subtitles", ok=True, reason="", products={}, cpa=[], note="")
    (tmp_path / "clips/clip_r2/subs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "clips/clip_r2/subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n句\n", encoding="utf-8")
    assert revise_run.revise_clip(task=tmp_path, cid="clip_r2", reason="二", adapters_for=adapters_for) == "clip_r3"
    for cid in ("clip_r2", "clip_r3"):
        remote = FakeRemote()
        ctx_y, cpa = _step6_of(tmp_path, cid, remote, monkeypatch, remote_dir=f"/r/{cid}")
        (ctx_y.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
        (ctx_y.clip_dir / "subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n句\n", encoding="utf-8")
        assert ClipState.load(tmp_path / "state" / f"{cid}.json").data["imported_cover"]["files"]["cover.png"] == str(
            tmp_path / "clips" / cid / "import_cover/cover.png")
        _assert_taken_as_imported(ctx_y, cpa, remote)


def test_a_second_generation_successor_takes_the_record_its_original_holds(tmp_path):
    """The carrying is of the record the predecessor holds, whoever made the predecessor: here the trim `clip_trim` of a trim."""
    ctx = make_x(tmp_path, FakeCpa([answer((5, 6)), answer((5, 6))]))
    _imported_into(ctx, tmp_path)
    assert run(ctx)["status"] == "TRIMMED"
    first = ClipState.load(tmp_path / "state/clip_trim.json")
    second_dir = tmp_path / "clips/clip_trim_trim"
    second = ClipState.create(tmp_path / "state/clip_trim_trim.json", clip=dict(first.data["clip"], cid="clip_trim_trim"),
                              session=first.data["session"])
    assert cover_import.carry(first, second, tmp_path / "clips/clip_trim", second_dir)
    assert second.data["imported_cover"]["png_sha256"] == SHA and (second_dir / "import_cover/cover.png").read_bytes() == _png()
