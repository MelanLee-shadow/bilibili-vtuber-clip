"""bilibili_member_api 离线测试：cookie 双形态、edit 载荷、配额/幂等语义。"""

import json
import urllib.request

import pytest

from src.autoslice.bilibili_member_api import (
    QUOTA_FREQUENCY_CODE,
    SEASON_ALREADY_IN_CODE,
    BiliSession,
    is_quota_rejection,
    load_cookie_pairs,
)

BILIUP_SHAPE = {
    "cookie_info": {
        "cookies": [
            {"name": "SESSDATA", "value": "s1"},
            {"name": "bili_jct", "value": "csrf-token"},
        ]
    }
}
APP_SHAPE = {"data": BILIUP_SHAPE}


@pytest.fixture
def cookie_file(tmp_path):
    def write(shape):
        path = tmp_path / "cookies.json"
        path.write_text(json.dumps(shape), encoding="utf-8")
        return path

    return write


def make_session(cookie_path, responses):
    """构造带记录型 transport 的会话；responses 按调用顺序弹出。"""

    calls = []

    def transport(request: urllib.request.Request):
        calls.append(request)
        return responses.pop(0)

    session = BiliSession(cookie_path=cookie_path, transport=transport)
    return session, calls


def test_cookie_pairs_parse_both_real_shapes(cookie_file):
    for shape in (BILIUP_SHAPE, APP_SHAPE):
        pairs = load_cookie_pairs(cookie_file(shape))
        assert {"name": "bili_jct", "value": "csrf-token"} in pairs


def test_session_requires_bili_jct(cookie_file):
    path = cookie_file({"cookie_info": {"cookies": [{"name": "SESSDATA", "value": "x"}]}})
    with pytest.raises(ValueError, match="bili_jct"):
        BiliSession(cookie_path=path)


def test_cover_up_uses_form_urlencoded_body(cookie_file):
    session, calls = make_session(
        cookie_file(BILIUP_SHAPE),
        [{"code": 0, "data": {"url": "https://img/x.png"}}],
    )
    url = session.cover_up(b"\x89PNG fake")
    assert url == "https://img/x.png"
    request = calls[0]
    assert request.headers["Content-type"] == "application/x-www-form-urlencoded"
    body = request.data.decode("ascii")
    # 整体 urlencode：data:image/png;base64 前缀必须被转义，否则服务端 -400
    assert body.startswith("cover=data%3Aimage%2Fpng%3Bbase64%2C")
    assert "csrf=csrf-token" in body


def test_build_edit_payload_keep_only_new_cid(cookie_file):
    session, _ = make_session(cookie_file(BILIUP_SHAPE), [])
    view_data = {
        "archive": {
            "aid": 42,
            "bvid": "BV1xx",
            "title": "旧标题",
            "tid": 21,
            "tag": "a,b",
            "desc": "",
            "copyright": 2,
        },
        "videos": [
            {"filename": "old", "title": "旧P", "cid": 1},
            {"filename": "new", "title": "新P", "cid": 2},
        ],
    }
    payload = session.build_edit_payload(view_data, title="新标题", keep_only_cid=2)
    assert payload["title"] == "新标题"
    assert payload["videos"] == [{"filename": "new", "title": "新P", "cid": 2}]
    assert payload["aid"] == 42 and payload["tag"] == "a,b"
    assert payload["csrf"] == "csrf-token"

    with pytest.raises(ValueError, match="not among"):
        session.build_edit_payload(view_data, keep_only_cid=999)


def test_season_add_treats_already_in_as_success(cookie_file):
    session, _ = make_session(
        cookie_file(BILIUP_SHAPE), [{"code": SEASON_ALREADY_IN_CODE}]
    )
    response = session.season_episode_add(9320779, aid=1, cid=2, title="t")
    assert response["code"] == SEASON_ALREADY_IN_CODE

    session2, _ = make_session(cookie_file(BILIUP_SHAPE), [{"code": -400}])
    with pytest.raises(RuntimeError, match="season add failed"):
        session2.season_episode_add(9320779, aid=1, cid=2, title="t")


def test_quota_rejection_classifier():
    assert is_quota_rejection({"code": QUOTA_FREQUENCY_CODE})
    assert not is_quota_rejection({"code": 0})
    assert not is_quota_rejection(None)
