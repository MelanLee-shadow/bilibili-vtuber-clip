"""Shared Bilibili transport: anonymous visitor cookie and Referer validation.

No real network: the client's opener is replaced by a scripted fake that routes
by full URL and consumes one outcome per request.
"""

import datetime as dt
import email.message
import json
import urllib.error

import pytest

from src.autoslice.streamer_dynamics import ALLOWED_HOSTS
from src.autoslice.timely_term_crawler import BoundedHttpClient, CrawlError

NOW = dt.datetime(2026, 10, 31, 1, 0, tzinfo=dt.timezone.utc)
SPI = "https://api.bilibili.com/x/frontend/finger/spi"
VIEW = "https://api.bilibili.com/x/web-interface/view?bvid=BV1uW421R79X"
COOKIE = "buvid3=B3TOKEN-infoc; buvid4=B4TOKEN=="
SPI_OK = json.dumps({"code": 0, "data": {"b_3": "B3TOKEN-infoc", "b_4": "B4TOKEN=="}}).encode()
VIEW_OK = json.dumps({"code": 0, "data": {"title": "VirtuaReal新成员加入！"}}).encode()


def _precondition_failed(url):
    return urllib.error.HTTPError(url, 412, "Precondition Failed", None, None)


class _FakeResponse:
    def __init__(self, body):
        self._body = body
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "application/json"

    def read(self, size=-1):
        if size is None or size < 0:
            data, self._body = self._body, b""
        else:
            data, self._body = self._body[:size], self._body[size:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _ScriptedOpener:
    """Routes by full URL; each route is consumed in order (bytes or exception)."""

    def __init__(self, routes):
        self.routes = {url: list(outcomes) for url, outcomes in routes.items()}
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        outcomes = self.routes.get(request.full_url)
        if not outcomes:
            raise AssertionError(f"unexpected request: {request.full_url}")
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _FakeResponse(outcome)

    def calls(self, url):
        return [request for request in self.requests if request.full_url == url]


def _client(opener, *, allowed_hosts=frozenset({"api.bilibili.com"}), max_requests=3):
    client = BoundedHttpClient(allowed_hosts=allowed_hosts, max_requests=max_requests, now=NOW)
    client._opener = opener
    return client


def test_bilibili_412_is_retried_once_with_fresh_visitor_cookie():
    opener = _ScriptedOpener({
        SPI: [SPI_OK, SPI_OK],
        VIEW: [_precondition_failed(VIEW), VIEW_OK],
    })
    client = _client(opener)

    response = client.fetch(VIEW)

    assert response.body == VIEW_OK
    assert len(opener.calls(SPI)) == 2  # bootstrap, then one refresh after the 412
    view_calls = opener.calls(VIEW)
    assert [request.get_header("Cookie") for request in view_calls] == [COOKIE, COOKIE]
    assert client.requests_made == 2  # the bootstrap does not spend the request budget


def test_first_bilibili_request_carries_visitor_cookie_from_spi():
    opener = _ScriptedOpener({SPI: [SPI_OK], VIEW: [VIEW_OK]})
    client = _client(opener)

    assert client.fetch(VIEW).body == VIEW_OK
    assert len(opener.calls(SPI)) == 1
    assert opener.calls(VIEW)[0].get_header("Cookie") == COOKIE


def test_failed_visitor_bootstrap_is_not_fatal_and_stays_bounded():
    opener = _ScriptedOpener({
        SPI: [_precondition_failed(SPI), _precondition_failed(SPI)],
        VIEW: [_precondition_failed(VIEW), _precondition_failed(VIEW), _precondition_failed(VIEW)],
    })
    client = _client(opener, max_requests=10)

    for _ in range(3):
        with pytest.raises(urllib.error.HTTPError) as raised:
            client.fetch(VIEW)
        assert raised.value.code == 412

    # Each request still goes out (no cookie, no crash); bootstraps stop at the cap.
    assert len(opener.calls(SPI)) == 2
    assert len(opener.calls(VIEW)) == 3
    assert all(request.get_header("Cookie") is None for request in opener.calls(VIEW))
    assert client.requests_made == 3


def test_visitor_cookie_is_never_sent_to_non_bilibili_hosts():
    vrp = "https://vrp.live/api/members/search/list?size=1000"
    opener = _ScriptedOpener({vrp: [VIEW_OK]})
    client = _client(opener, allowed_hosts=frozenset({"vrp.live"}))

    client.fetch(vrp)

    assert opener.calls(SPI) == []
    assert [request.get_header("Cookie") for request in opener.calls(vrp)] == [None]


def test_referer_is_validated_as_header_not_against_fetch_host_allowlist():
    live = "https://api.live.bilibili.com/room/v1/Room/get_info?room_id=1"
    opener = _ScriptedOpener({SPI: [SPI_OK], live: [VIEW_OK], live + "&x=1": [VIEW_OK]})
    client = _client(opener, allowed_hosts=frozenset({"api.live.bilibili.com", "api.bilibili.com"}),
                     max_requests=4)

    client.fetch(live, headers={"Referer": "https://www.bilibili.com/"})
    client.fetch(live + "&x=1", headers={"Referer": "https://space.bilibili.com/413748120"})

    assert opener.calls(live)[0].get_header("Referer") == "https://www.bilibili.com/"
    assert opener.calls(live + "&x=1")[0].get_header("Referer") == "https://space.bilibili.com/413748120"


def test_referer_must_still_be_safe_https_without_control_characters():
    client = _client(_ScriptedOpener({}), allowed_hosts=frozenset({"api.bilibili.com"}))

    for referer in ("http://www.bilibili.com/", "https://user@www.bilibili.com/", "https://www.bilibili.com/\r\nX: y"):
        with pytest.raises(CrawlError):
            client.fetch(VIEW, headers={"Referer": referer})
    assert client.requests_made == 0


def test_streamer_dynamics_wbi_bootstrap_passes_its_real_allowlist():
    from src.autoslice.community_comment_discovery import fetch_wbi_mixin_key

    nav = "https://api.bilibili.com/x/web-interface/nav"
    payload = {
        "code": 0,
        "data": {"wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
        }},
    }
    opener = _ScriptedOpener({SPI: [SPI_OK], nav: [json.dumps(payload).encode()]})
    client = _client(opener, allowed_hosts=ALLOWED_HOSTS, max_requests=3)

    key, fresh = fetch_wbi_mixin_key(client, endpoint=nav)

    assert len(key) == 32 and fresh is True
    assert opener.calls(nav)[0].get_header("Referer") == "https://www.bilibili.com/"
