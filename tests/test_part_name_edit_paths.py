"""Single-P part name == final archive title on every edit path (offline).

The part name may move from the upload file name to the final title and nothing
else may drift; each path's completion check requires the part name to equal
the final title.  All Creator/public data here is constructed.
"""

from __future__ import annotations

import copy

import pytest

import test_authorized_upload_policy_repair as policy_tests
import test_authorized_upload_tags_repair as tags_tests
import test_authorized_upload_title_repair as title_tests
from src.autoslice import authorized_upload_policy_repair as policy_repair
from src.autoslice import authorized_upload_tags_repair as tags_repair
from src.autoslice import authorized_upload_title_repair as title_repair
from src.autoslice import same_bv_cover_repair as cover_repair
from src.autoslice.bilibili_member_api import build_edit_payload
from src.autoslice.part_name_contract import same_stable
from test_same_bv_cover_repair import NEW_COVER, _snapshot as cover_snapshot

FILE_NAME_PART = "n261110ad1007o8648xaz22x1v7mkjfb"


class _TitleSession:
    def __init__(self, raw):
        self.raw = raw

    def build_edit_payload(self, current, **kwargs):
        return build_edit_payload(current, csrf="c", **kwargs)


def _raw(part=FILE_NAME_PART, title="旧标题", cid=22):
    return {
        "archive": {"bvid": "BV1x", "aid": 11, "title": title, "tag": "a", "desc": "d",
                    "cover": "c", "tid": 21, "copyright": 2, "source": "s"},
        "videos": [{"cid": cid, "filename": "f", "title": part}],
    }


# ---- title only ------------------------------------------------------------

def test_title_only_payload_moves_part_name_to_final_title_without_drift():
    payload = title_repair._title_only_payload(
        session=_TitleSession(None), raw_view=_raw(), final_title="新标题", bvid="BV1x")
    assert payload["title"] == "新标题"
    assert payload["videos"] == [{"filename": "f", "title": "新标题", "cid": 22}]


@pytest.mark.parametrize("mutation", ["tid", "part_other_name", "filename", "cid"])
def test_title_only_payload_still_reports_any_other_change(mutation):
    class Drifting(_TitleSession):
        def build_edit_payload(self, current, **kwargs):
            payload = super().build_edit_payload(current, **kwargs)
            if kwargs.get("title") is None:
                return payload
            if mutation == "tid":
                payload["tid"] = 22
            elif mutation == "part_other_name":
                payload["videos"][0]["title"] = "不是标题"
            else:
                payload["videos"][0][mutation] = "x" if mutation == "filename" else 99
            return payload

    with pytest.raises(title_repair.TitleRepairError, match="NON_TITLE_PAYLOAD_DRIFT"):
        title_repair._title_only_payload(
            session=Drifting(None), raw_view=_raw(), final_title="新标题", bvid="BV1x")


def test_title_run_completes_only_when_part_name_is_final_title(tmp_path):
    args, adapter, dependencies, _ = title_tests._fixture(tmp_path)
    # A Creator Center that accepts the title but keeps the file-name part.
    real_edit = adapter.session.edit_archive

    def keeps_part_name(payload):
        before_videos = copy.deepcopy(adapter.session.raw["videos"])
        response = real_edit(payload)
        adapter.session.raw["videos"] = before_videos
        return response

    adapter.session.edit_archive = keeps_part_name
    code, receipt = title_repair.run(args, **dependencies)
    assert code == 6 and receipt["status"] == "PUBLIC_PENDING"
    assert adapter.session.edits == 1


def test_title_run_extra_non_title_change_still_blocks(tmp_path):
    args, adapter, dependencies, _ = title_tests._fixture(tmp_path)
    real_edit = adapter.session.edit_archive

    def also_changes_tid(payload):
        response = real_edit(payload)
        adapter.session.raw["archive"]["tid"] = 99
        return response

    adapter.session.edit_archive = also_changes_tid
    code, receipt = title_repair.run(args, **dependencies)
    assert receipt["status"] == "BLOCKED_NON_TITLE_DRIFT" and code != 0


def test_same_stable_allows_only_the_part_name_to_become_the_newer_title():
    def snap(part, title="标题", cid=22):
        return {"creator": {"metadata": {"title": title}, "videos": [{"cid": cid, "filename": "f", "title": part}]}}

    identity = lambda value: value
    assert same_stable(snap("文件名", "标题"), snap("标题"), identity)
    # not the title of the newer snapshot -> still a difference
    assert not same_stable(snap("文件名"), snap("别的名字"), identity)
    # the title changed in the same step but the part name did not follow
    assert not same_stable(snap("文件名"), snap("文件名", "新标题"), identity)
    # any other video-row change is never tolerated
    assert not same_stable(snap("文件名"), snap("标题", cid=23), identity)
    # multi-P is untouched
    multi_old = {"creator": {"metadata": {"title": "标题"}, "videos": [{"cid": 1, "title": "甲"}, {"cid": 2, "title": "乙"}]}}
    multi_new = copy.deepcopy(multi_old)
    multi_new["creator"]["videos"][0]["title"] = "标题"
    assert not same_stable(multi_old, multi_new, identity)


# ---- cover only ------------------------------------------------------------

def _cover_pair(*, part_after, cover_after=NEW_COVER, extra=None):
    before = cover_snapshot()
    before["creator"]["videos"][0]["title"] = FILE_NAME_PART
    after = cover_snapshot(cover=cover_after)
    after["creator"]["videos"][0]["title"] = part_after
    if extra:
        extra(after)
    return before, after


def test_cover_only_accepts_part_name_moving_to_title_and_reaches_target():
    before, after = _cover_pair(part_after="示例封面")
    assert cover_repair._transition_projection(after, {"before": before}, NEW_COVER) == ("target", [])


def test_cover_only_is_not_complete_while_part_name_is_not_the_title():
    before, after = _cover_pair(part_after=FILE_NAME_PART)
    projection, _ = cover_repair._transition_projection(after, {"before": before}, NEW_COVER)
    assert projection == "pending"


@pytest.mark.parametrize("what", ["other_part_name", "tid"])
def test_cover_only_other_creator_change_is_still_drift(what):
    def extra(after):
        if what == "tid":
            after["creator"]["metadata"]["tid"] = 22

    before, after = _cover_pair(
        part_after="不是标题的名字" if what == "other_part_name" else "示例封面", extra=extra)
    projection, problems = cover_repair._transition_projection(after, {"before": before}, NEW_COVER)
    assert projection == "drift" and problems == ["Creator changed outside cover"]


# ---- tags ------------------------------------------------------------------

def test_tags_edit_names_part_after_title_and_requires_it_to_complete(tmp_path):
    args, adapter, dependencies = tags_tests.setup(tmp_path)
    original_edit = adapter.edit_archive
    kept = adapter.raw["videos"][0]["title"]

    def keeps_part_name(payload):
        response = original_edit(payload)
        adapter.raw["videos"][0]["title"] = kept
        return response

    adapter.edit_archive = keeps_part_name
    code, receipt = tags_repair.run(args, **dependencies)
    assert code == 6 and receipt["status"] == "PUBLIC_PENDING"


def test_tags_edit_with_part_name_moved_to_title_verifies(tmp_path):
    args, adapter, dependencies = tags_tests.setup(tmp_path)
    assert adapter.raw["videos"][0]["title"] == "P1"
    code, receipt = tags_repair.run(args, **dependencies)
    assert code == 0 and receipt["status"] == "VERIFIED_TAGS_ONLY"
    assert adapter.raw["videos"][0]["title"] == "月饼"


def test_tags_edit_extra_change_still_blocks(tmp_path):
    args, adapter, dependencies = tags_tests.setup(tmp_path)
    original_edit = adapter.edit_archive

    def also_changes_desc(payload):
        response = original_edit(payload)
        adapter.raw["archive"]["desc"] = "被改动"
        return response

    adapter.edit_archive = also_changes_desc
    code, receipt = tags_repair.run(args, **dependencies)
    assert code == 2 and receipt["status"] == "BLOCKED_NON_TAG_DRIFT"


# ---- policy ----------------------------------------------------------------

def test_policy_edit_requires_part_name_to_be_title(tmp_path):
    args, adapter, deps, _manifest, _, _ = policy_tests.setup(tmp_path)
    original_edit = adapter.session.edit_archive

    def keeps_part_name(payload):
        before_videos = copy.deepcopy(adapter.session.raw["videos"])
        response = original_edit(payload)
        adapter.session.raw["videos"] = before_videos
        return response

    adapter.session.edit_archive = keeps_part_name
    code, receipt = policy_repair.run(args, **deps)
    assert code == 6 and receipt["status"] == "PUBLIC_PENDING"
