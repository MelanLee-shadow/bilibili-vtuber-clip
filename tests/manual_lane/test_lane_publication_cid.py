"""A clip cut out of a finished one (`<cid>_trim`) is the same manuscript as `<cid>`: one function, `publication_id.publication_cid`,
decides it for the package directory name, the owner's chosen title and the published-title exclusion."""

from __future__ import annotations

import json
import sys

from fakes import FakeRemote, make_ctx
from test_title_approved import APPROVED, EVIDENCE, _asset

from src.autoslice.manual_lane import publication_id, title_history, title_verdicts, trim_run
from src.autoslice.manual_lane import stage_package as sp
from src.autoslice.manual_lane.layout import REPO_ROOT


def test_publication_cid_strips_only_the_trim_ending_and_the_suffix_is_trim_runs():
    assert trim_run.SUFFIX == publication_id.TRIM_SUFFIX == "_trim"
    assert publication_id.publication_cid("auto_1_trim") == "auto_1" and publication_id.publication_cid("auto_1") == "auto_1"
    assert publication_id.publication_cid("trim_x") == "trim_x" and publication_id.publication_cid("a_trim_b") == "a_trim_b"


def test_the_successor_is_packaged_under_the_originals_publication_name_in_its_own_directory(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=None)
    original = sp.package_dir(ctx)
    ctx.state.data["clip"]["cid"] = "clip_trim"
    ctx.clip_dir = tmp_path / "clips" / "clip_trim"
    successor = sp.package_dir(ctx)
    assert original.name == successor.name == "fm-20261024-clip" and sp.member_slug("clip_trim") == sp.member_slug("clip") == "clip"
    assert successor.parent.parent.parent.parent == tmp_path / "clips" / "clip_trim"      # clips/<cid>/fmr/out/<date>/
    assert original.parent.parent.parent.parent == tmp_path / "clips" / "clip"
    assert sp.member_slug("other_clip") == "other-clip"


def test_the_successor_gets_the_owners_chosen_title_of_the_original_and_is_kept_out_of_the_taste_texts(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch)
    verdicts = title_verdicts.load_verdicts()
    assert verdicts.approved("clip_trim") == verdicts.approved("clip") == {"title_body": APPROVED, "evidence": EVIDENCE}
    assert verdicts.approved("other") is None
    assert title_verdicts.excluded_cids("clip_trim") == frozenset({"clip", "clip_trim"}) == title_verdicts.excluded_cids("clip") | {"clip_trim"}
    for text in (verdicts.liked_and_disliked("clip_trim"), verdicts.corpus("clip_trim")):
        assert EVIDENCE not in text


def _registry(monkeypatch, *rows):
    monkeypatch.setattr(title_history, "load_publication_registry", lambda: {"entries": list(rows)})


def test_the_successor_does_not_meet_the_originals_published_title_as_a_duplicate(tmp_path, monkeypatch):
    _registry(monkeypatch,
              {"candidate_id": "fm-20261024-clip", "status": "published", "bvid": "BV1", "title": "【李豆沙】原片的标题"},
              {"candidate_id": "clip2", "status": "published", "bvid": "BV2", "title": "【李豆沙】别的片的标题"})
    assert title_history.same_clip("fm-20261024-clip", "clip_trim") and title_history.same_clip("clip", "clip_trim")
    assert not title_history.same_clip("fm-20261024-clip2", "clip_trim") and not title_history.same_clip("fm-20261024-clip", "clip2")
    titles, _note = title_history.published_titles("clip_trim")
    assert titles == ["【李豆沙】别的片的标题"]
    assert title_history.published_titles("clip")[0] == titles                              # the original asks the same
    assert title_history.published_titles("clip2")[0] == ["【李豆沙】原片的标题"]


def test_is_published_answers_only_published_rows_of_this_manuscript(monkeypatch):
    _registry(monkeypatch, {"candidate_id": "fm-20261024-clip", "status": "hold_pending_review"},
              {"candidate_id": "fm-20261024-clip2", "status": "published", "bvid": "BV2"})
    assert title_history.is_published("clip") is False and title_history.is_published("clip_trim") is False
    _registry(monkeypatch, {"candidate_id": "fm-20261024-clip", "status": "published", "bvid": "BV1"})
    assert title_history.is_published("clip") is True and title_history.is_published("clip_trim") is True
    assert title_history.is_published("clip2") is False


def test_the_script_on_the_media_host_answers_the_published_check(tmp_path, monkeypatch, capsys):
    import runpy

    from src.autoslice import publication_registry

    monkeypatch.setattr(publication_registry, "load_publication_registry", lambda *a, **k: {"entries": [
        {"candidate_id": "clip", "status": "published", "bvid": "BV1"}]})
    for cid, expected in (("clip_trim", True), ("clip2", False)):
        request = tmp_path / "request.json"
        request.write_text(json.dumps({"candidate_id": cid, "check_published": True}), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [title_history.__file__, str(request), str(REPO_ROOT)])
        runpy.run_path(title_history.__file__, run_name="__main__")
        assert json.loads(capsys.readouterr().out) == {"published": expected}
