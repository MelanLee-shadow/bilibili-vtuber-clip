"""Post-publish check: a single-P archive's part name is the manifest title.

Creator Center and public pages are constructed fake responses; no network.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path

from scripts import authorized_upload as au
from src.autoslice.bilibili_member_api import EDIT_CLONE_FIELDS

BVID = "BV1Ab411c7Xy"
TITLE = "【测试】用于核验分P名的假标题"
DESC = "假简介"
TAGS = ["标签甲", "标签乙"]
FILE_NAME_PART = "n261110ad1007o8648xaz22x1v7mkjfb"
ROOT = Path(__file__).resolve().parents[1]


class FakeBilibili:
    """Serves the three read surfaces and records Creator edits."""

    def __init__(self, parts, *, applies_edit=True, edit_raises=False):
        self.archive = {
            "aid": 11, "bvid": BVID, "title": TITLE, "desc": DESC, "tag": ",".join(TAGS),
            "tid": 27, "copyright": 2, "source": "https://live.bilibili.com/", "cover": "c",
        }
        self.videos = [{"cid": 100 + i, "filename": f"f{i}", "title": name} for i, name in enumerate(parts)]
        self.applies_edit = applies_edit
        self.edit_raises = edit_raises
        self.edits: list[dict] = []

    def __call__(self, url, data=None, _is_json=False):
        if "x/vu/web/edit" in url:
            self.edits.append(copy.deepcopy(data))
            if self.edit_raises:
                raise TimeoutError("response lost")
            if self.applies_edit:
                self.videos = [dict(row) for row in data["videos"]]
            return {"code": 0}
        if "web-interface/view" in url:
            return {"code": 0, "data": {
                "state": 0, "aid": 11, "cid": 100, "title": TITLE, "desc": DESC, "tid": 27, "copyright": 2}}
        if "tag/archive/tags" in url:
            return {"code": 0, "data": [{"tag_name": tag} for tag in TAGS]}
        if "vupre/web/archive/view" in url:
            return {"code": 0, "data": {"archive": copy.deepcopy(self.archive), "videos": copy.deepcopy(self.videos)}}
        raise AssertionError(f"unexpected url {url}")


def _verify(http, *, csrf="fake-csrf", part_name=True, wait=0.0):
    manifest = {"title": TITLE, "description": DESC, "tags": TAGS, "season": None}
    return au.public_verify_flow(
        manifest, BVID, http=http, season_result={}, wait_seconds=wait, poll_seconds=0,
        sleeper=lambda _s: None, csrf=csrf, part_name=part_name)


def test_part_name_already_the_title_verifies_without_edit():
    http = FakeBilibili([TITLE])
    result = _verify(http)
    assert result["status"] == "VERIFIED_PUBLIC" and http.edits == []
    assert "part_name_evidence" not in result


def test_part_name_is_file_name_gets_exactly_one_part_only_edit_then_verifies():
    http = FakeBilibili([FILE_NAME_PART])
    before_archive = copy.deepcopy(http.archive)
    result = _verify(http, wait=5.0)
    assert result["status"] == "VERIFIED_PUBLIC", result["problems"]
    assert len(http.edits) == 1
    payload = http.edits[0]
    expected_keys = {key for key in EDIT_CLONE_FIELDS if key in before_archive} | {"aid", "bvid", "videos", "csrf"}
    assert set(payload) == expected_keys
    for key in EDIT_CLONE_FIELDS:
        if key in before_archive:
            assert payload[key] == before_archive[key]
    assert payload["videos"] == [{"filename": "f0", "title": TITLE, "cid": 100}]
    events = [row["event"] for row in result["part_name_evidence"]]
    assert events == ["PART_NAME_EDIT_INTENT", "PART_NAME_EDIT_RESPONSE", "PART_NAME_READBACK"]
    intent, _, readback = result["part_name_evidence"]
    assert intent["part_name_before"] == FILE_NAME_PART and intent["part_name_target"] == TITLE
    assert readback["part_name_after"] == TITLE and intent["at"] and readback["at"]


def test_part_name_still_wrong_after_the_edit_fails_with_the_new_code_after_one_edit():
    http = FakeBilibili([FILE_NAME_PART], applies_edit=False)
    polls = {"n": 0}

    def sleeper(_seconds):
        polls["n"] += 1

    manifest = {"title": TITLE, "description": DESC, "tags": TAGS, "season": None}
    result = au.public_verify_flow(
        manifest, BVID, http=http, season_result={}, wait_seconds=1e-9, poll_seconds=0,
        sleeper=sleeper, csrf="fake-csrf")
    assert result["status"] == "PUBLIC_VERIFY_FAILED"
    assert len(http.edits) == 1
    problem = " ".join(result["problems"])
    assert "PART_NAME_NOT_TITLE" in problem
    assert FILE_NAME_PART in problem and TITLE in problem  # actual and expected are both named


def test_part_name_edit_exception_is_one_attempt_and_stays_unverified():
    http = FakeBilibili([FILE_NAME_PART], edit_raises=True)
    result = _verify(http, wait=0.0)
    assert result["status"] == "PUBLIC_VERIFY_FAILED" and len(http.edits) == 1
    assert any("PART_NAME_NOT_TITLE" in problem for problem in result["problems"])


def test_multi_part_archive_is_not_checked_or_edited():
    http = FakeBilibili(["甲", "乙"])
    result = _verify(http)
    assert result["status"] == "VERIFIED_PUBLIC" and http.edits == []


def test_no_part_list_is_a_hard_failure():
    http = FakeBilibili([])
    result = _verify(http)
    assert result["status"] == "PUBLIC_VERIFY_FAILED"
    assert any("PART_NAME_NOT_TITLE" in problem for problem in result["problems"])


def test_without_edit_credential_the_mismatch_is_reported_and_nothing_is_edited():
    http = FakeBilibili([FILE_NAME_PART])
    result = _verify(http, csrf=None)
    assert result["status"] == "PUBLIC_VERIFY_FAILED" and http.edits == []
    assert any("PART_NAME_NOT_TITLE" in problem for problem in result["problems"])


def test_read_only_reverify_skips_the_check_and_never_edits():
    http = FakeBilibili([FILE_NAME_PART])
    result = _verify(http, part_name=False)
    assert result["status"] == "VERIFIED_PUBLIC" and http.edits == []


def test_edit_never_happens_while_another_problem_is_unresolved():
    http = FakeBilibili([FILE_NAME_PART])
    http.archive["title"] = "别的标题"
    result = _verify(http)
    assert result["status"] == "PUBLIC_VERIFY_FAILED" and http.edits == []


# ---- the videos payload has exactly one constructor ------------------------

# Dict literals shaped like a Creator video row (filename + title + cid).  The
# first two are the constructor and the read-side snapshot projection; the rest
# are standalone operator scripts that predate the rule and are not changed here.
_ROW_KEYS = {"filename", "title", "cid"}
_ALLOWED = {
    "src/autoslice/bilibili_member_api.py",
    "src/autoslice/same_bv_repair.py",
}
_KNOWN_STANDALONE_SCRIPTS = {
    "scripts/bili_cover_edit.py",
    "scripts/bili_update_tags.py",
    "scripts/swap_video_p.py",
}


def _video_row_literals(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if '"filename"' not in text and "'filename'" not in text:
        return 0
    tree = ast.parse(text)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
            if keys >= _ROW_KEYS:
                count += 1
    return count


def test_videos_payload_is_constructed_only_in_build_edit_payload():
    sources = sorted(path.relative_to(ROOT).as_posix() for top in ("src", "scripts") for path in (ROOT / top).rglob("*.py"))
    found = {rel for rel in sources if _video_row_literals(ROOT / rel)}
    assert found - _ALLOWED == _KNOWN_STANDALONE_SCRIPTS, sorted(found)
    assert _video_row_literals(ROOT / "src/autoslice/bilibili_member_api.py") == 1
