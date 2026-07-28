"""bilibili_member_api 离线测试：cookie 双形态、edit 载荷、配额/幂等语义。"""

import json
import subprocess
import urllib.request

import pytest

from src.autoslice.bilibili_member_api import (
    QUOTA_FREQUENCY_CODE,
    SEASON_ALREADY_IN_CODE,
    BiliSession,
    CookieSchemaError,
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


def test_cookie_pairs_reject_ambiguous_shape_without_leaking_values(cookie_file):
    path = cookie_file(
        {
            "cookie_info": {"cookies": [{"name": "bili_jct", "value": "top-secret-value"}]},
            "data": {
                "cookie_info": {"cookies": [{"name": "bili_jct", "value": "nested-secret-value"}]}
            },
        }
    )

    with pytest.raises(CookieSchemaError, match="ambiguous") as raised:
        load_cookie_pairs(path)

    assert "top-secret-value" not in str(raised.value)
    assert "nested-secret-value" not in str(raised.value)


@pytest.mark.parametrize(
    ("shape", "message"),
    [
        ([], "root must be an object"),
        ({"data": "secret-data"}, "data must be an object"),
        ({}, "no supported cookie_info path"),
        ({"cookie_info": []}, "cookie_info must be an object"),
        (
            {"cookie_info": {"cookies": "secret-cookie-list"}},
            "cookies must be a non-empty list",
        ),
        (
            {"cookie_info": {"cookies": [{"name": "SESSDATA"}]}},
            "has no non-empty string value",
        ),
        (
            {
                "cookie_info": {
                    "cookies": [
                        {"name": "SESSDATA", "value": "secret-one"},
                        {"name": "SESSDATA", "value": "secret-two"},
                    ]
                }
            },
            "duplicates a name",
        ),
    ],
)
def test_cookie_pairs_reject_malformed_shapes_without_leaking_values(
    cookie_file,
    shape,
    message,
):
    with pytest.raises(CookieSchemaError, match=message) as raised:
        load_cookie_pairs(cookie_file(shape))

    error = str(raised.value)
    for secret in (
        "secret-data",
        "secret-cookie-list",
        "secret-one",
        "secret-two",
    ):
        assert secret not in error


def test_session_requires_bili_jct(cookie_file):
    path = cookie_file({"cookie_info": {"cookies": [{"name": "SESSDATA", "value": "x"}]}})
    with pytest.raises(CookieSchemaError, match="bili_jct"):
        BiliSession(cookie_path=path)


def test_biliup_append_uses_explicit_top_level_cookie(
    tmp_path,
    monkeypatch,
):
    api_cookie = tmp_path / "app-cookie.json"
    api_cookie.write_text(json.dumps(APP_SHAPE), encoding="utf-8")
    biliup_cookie = tmp_path / "biliup-cookie.json"
    biliup_cookie.write_text(json.dumps(BILIUP_SHAPE), encoding="utf-8")
    media = tmp_path / "new.mp4"
    media.write_bytes(b"video")
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "src.autoslice.bilibili_member_api.subprocess.run",
        fake_run,
    )
    session = BiliSession(
        cookie_path=api_cookie,
        biliup_cookie_path=biliup_cookie,
    )

    session.biliup_append("BV1TEST", media)

    assert seen["command"][2] == biliup_cookie.name
    assert seen["command"][-1] == str(media)
    assert seen["kwargs"]["cwd"] == str(biliup_cookie.parent)


def test_explicit_biliup_cookie_rejects_nested_app_shape(tmp_path):
    api_cookie = tmp_path / "app-cookie.json"
    api_cookie.write_text(json.dumps(APP_SHAPE), encoding="utf-8")
    nested_biliup_cookie = tmp_path / "nested-biliup-cookie.json"
    nested_biliup_cookie.write_text(json.dumps(APP_SHAPE), encoding="utf-8")

    with pytest.raises(CookieSchemaError, match="top-level cookie_info"):
        BiliSession(
            cookie_path=api_cookie,
            biliup_cookie_path=nested_biliup_cookie,
        )


def test_biliup_failure_does_not_echo_process_output(
    tmp_path,
    monkeypatch,
):
    cookie = tmp_path / "biliup-cookie.json"
    cookie.write_text(json.dumps(BILIUP_SHAPE), encoding="utf-8")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="top-secret-value",
            stderr="nested-secret-value",
        )

    monkeypatch.setattr(
        "src.autoslice.bilibili_member_api.subprocess.run",
        fake_run,
    )
    session = BiliSession(cookie_path=cookie)

    with pytest.raises(RuntimeError, match="rc=1") as raised:
        session.biliup_append("BV1TEST", tmp_path / "new.mp4")

    assert "top-secret-value" not in str(raised.value)
    assert "nested-secret-value" not in str(raised.value)


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


def test_build_edit_payload_can_replace_part_title(cookie_file):
    session, _ = make_session(cookie_file(BILIUP_SHAPE), [])
    view_data = {
        "archive": {"aid": 42, "bvid": "BV1xx", "title": "正式标题"},
        "videos": [
            {
                "filename": "content.burned-final-sapphire72",
                "title": "content.burned-final-sapphire72",
                "cid": 2,
            }
        ],
    }

    payload = session.build_edit_payload(view_data, video_title="正式标题")

    assert payload["videos"] == [
        {
            "filename": "content.burned-final-sapphire72",
            "title": "正式标题",
            "cid": 2,
        }
    ]


def test_season_add_treats_already_in_as_success(cookie_file):
    session, _ = make_session(cookie_file(BILIUP_SHAPE), [{"code": SEASON_ALREADY_IN_CODE}])
    response = session.season_episode_add(9320779, aid=1, cid=2, title="t")
    assert response["code"] == SEASON_ALREADY_IN_CODE

    session2, _ = make_session(cookie_file(BILIUP_SHAPE), [{"code": -400}])
    with pytest.raises(RuntimeError, match="season add failed"):
        session2.season_episode_add(9320779, aid=1, cid=2, title="t")


def test_season_episode_edit_preserves_episode_and_page_order(cookie_file):
    session, calls = make_session(cookie_file(BILIUP_SHAPE), [{"code": 0, "message": "0"}])

    response = session.season_episode_edit(
        episode_id=210909973,
        title="【李豆沙】新标题",
        aid=116969558771366,
        cid=40389051822,
        season_id=8383206,
        section_id=9320779,
        order=68,
        page_cids=[40389051822],
    )

    assert response["code"] == 0
    request = calls[0]
    assert request.full_url.endswith("/x2/creative/web/season/section/episode/edit?csrf=csrf-token")
    assert json.loads(request.data) == {
        "id": 210909973,
        "title": "【李豆沙】新标题",
        "aid": 116969558771366,
        "cid": 40389051822,
        "seasonId": 8383206,
        "sectionId": 9320779,
        "sorts": [{"id": 40389051822, "sort": 1}],
        "order": 68,
    }


@pytest.mark.parametrize(
    "override",
    [
        {"episode_id": 0},
        {"title": ""},
        {"page_cids": []},
        {"page_cids": [2, 2]},
        {"page_cids": [3]},
    ],
)
def test_season_episode_edit_rejects_unsafe_identity(cookie_file, override):
    session, calls = make_session(cookie_file(BILIUP_SHAPE), [])
    kwargs = {
        "episode_id": 1,
        "title": "标题",
        "aid": 1,
        "cid": 2,
        "season_id": 3,
        "section_id": 4,
        "order": 5,
        "page_cids": [2],
    }
    kwargs.update(override)

    with pytest.raises(ValueError):
        session.season_episode_edit(**kwargs)

    assert calls == []


def test_quota_rejection_classifier():
    assert is_quota_rejection({"code": QUOTA_FREQUENCY_CODE})
    assert not is_quota_rejection({"code": 0})
    assert not is_quota_rejection(None)
