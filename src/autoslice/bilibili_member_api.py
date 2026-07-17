"""bilibili 创作中心（member）API 的唯一封装 —— 2026-07-14 实战定稿。

此前 view/编辑/换封面/换源/入合集散落在 free 上的一次性脚本里
（edit_replace_20260714.py、cover_only_edit.py、bandfan_finish.py …），
cookie 格式、端点、编码的坑每写一次踩一次。硬结论全部固化在这里：

- 详情端点是 ``x/vupre/web/archive/view``；``x/vu/web/archive/view`` 是 404。
- 封面上传 ``x/vu/web/cover/up`` 必须用 form-urlencoded 整体编码
  （手拼 ``cover=data:image/png;base64,...`` 会被 ; : 断参数 → -400）。
- ``x/vu/web/edit`` 传 JSON；videos 只留新 P = UI「换视频」同效；
  编辑不占投稿配额（投稿配额是滚动 24h 窗，约 10 条，超限 code 21566）。
- 给已有稿件追加文件的唯一 CLI 入口是 ``biliup append -v <BV>``
  （cwd 必须是 cookies 所在目录）。
- 合集加集 ``x2/creative/web/season/section/episodes/add``；20080=已在集，
  幂等成功。删稿有 geetest 验证码墙（340022），只能人工。
- cookie 文件存在两种真实形态：biliup_cookies.json 的
  ``{cookie_info:{cookies:[...]}}`` 与 app/cookie.json 的
  ``{data:{cookie_info:{...}}}``；两种都要认。
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

MEMBER = "https://member.bilibili.com"
ARCHIVE_VIEW = f"{MEMBER}/x/vupre/web/archive/view"
COVER_UP = f"{MEMBER}/x/vu/web/cover/up"
ARCHIVE_EDIT = f"{MEMBER}/x/vu/web/edit"
SEASON_EPISODES_ADD = f"{MEMBER}/x2/creative/web/season/section/episodes/add"
SEASON_EPISODE_DEL = f"{MEMBER}/x2/creative/web/season/section/episode/del"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"

QUOTA_FREQUENCY_CODE = 21566  # 投稿过于频繁：滚动 24h 投稿窗已满（编辑不受此限）
SEASON_ALREADY_IN_CODE = 20080  # episodes/add：已在合集，幂等视为成功
DELETE_CAPTCHA_CODE = 340022  # 删稿 geetest 墙：程序化删除走不通，人工处理

DEFAULT_BILIUP_COOKIES = Path("/opt/bilive/app/tmp_manual_upload/biliup_cookies.json")
BILIUP_BIN = Path("/opt/bilive/bin/biliup")

# 编辑时从 view 原样回传的 archive 字段（漏传会被服务端重置为默认值）。
EDIT_CLONE_FIELDS = (
    "copyright",
    "source",
    "tid",
    "cover",
    "title",
    "desc",
    "desc_format_id",
    "dynamic",
    "interactive",
    "no_reprint",
    "subtitle",
    "tag",
    "up_selection_reply",
    "up_close_reply",
    "up_close_danmu",
)

Transport = Callable[[urllib.request.Request], Mapping[str, Any]]


def _default_transport(request: urllib.request.Request) -> Mapping[str, Any]:
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def load_cookie_pairs(cookie_path: Path) -> list[dict[str, str]]:
    """两种真实 cookie 文件形态都解析成 [{name, value}, ...]。"""

    raw = json.loads(Path(cookie_path).read_text(encoding="utf-8"))
    info = raw.get("cookie_info") or (raw.get("data") or {}).get("cookie_info") or {}
    cookies = info.get("cookies") or []
    pairs = [
        {"name": str(c["name"]), "value": str(c["value"])}
        for c in cookies
        if isinstance(c, Mapping) and c.get("name") is not None
    ]
    if not pairs:
        raise ValueError(f"no cookies found in {cookie_path}")
    return pairs


def is_quota_rejection(payload: Mapping[str, Any] | None) -> bool:
    return bool(payload) and payload.get("code") == QUOTA_FREQUENCY_CODE


@dataclass
class BiliSession:
    """一个登录态 + 传输层。transport 可注入用于离线测试。"""

    cookie_path: Path = DEFAULT_BILIUP_COOKIES
    transport: Transport | None = None

    def __post_init__(self) -> None:
        pairs = load_cookie_pairs(self.cookie_path)
        self.cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in pairs)
        try:
            self.csrf = next(c["value"] for c in pairs if c["name"] == "bili_jct")
        except StopIteration as exc:
            raise ValueError(f"bili_jct missing in {self.cookie_path}") from exc
        self._send = self.transport or _default_transport

    # ---- 传输原语 -------------------------------------------------------

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Cookie": self.cookie_header,
            "User-Agent": UA,
            "Referer": f"{MEMBER}/",
            "Origin": MEMBER,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def get(self, url: str) -> Mapping[str, Any]:
        return self._send(urllib.request.Request(url, headers=self._headers()))

    def post_json(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers=self._headers("application/json;charset=UTF-8"),
        )
        return self._send(request)

    def post_form(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers=self._headers("application/x-www-form-urlencoded"),
        )
        return self._send(request)

    # ---- 业务操作 -------------------------------------------------------

    def archive_view(self, bvid: str) -> Mapping[str, Any]:
        payload = self.get(f"{ARCHIVE_VIEW}?bvid={urllib.parse.quote(bvid)}")
        if payload.get("code") != 0:
            raise RuntimeError(f"archive view {bvid} failed: {payload}")
        return payload["data"]

    def cover_up(self, png_bytes: bytes) -> str:
        b64 = base64.b64encode(png_bytes).decode("ascii")
        payload = self.post_form(
            f"{COVER_UP}?csrf={urllib.parse.quote(self.csrf)}",
            {"cover": "data:image/png;base64," + b64, "csrf": self.csrf},
        )
        if payload.get("code") != 0:
            raise RuntimeError(f"cover up failed: {payload}")
        return str(payload["data"]["url"])

    def build_edit_payload(
        self,
        view_data: Mapping[str, Any],
        *,
        title: str | None = None,
        cover_url: str | None = None,
        keep_only_cid: int | None = None,
        tag: str | None = None,
        video_title: str | None = None,
    ) -> dict[str, Any]:
        """从 view 数据构造 edit 载荷；纯函数，离线可测。

        keep_only_cid：videos 只留这个 cid（append 后换源的关键一步）；
        None = 原样回传全部 P。video_title 显式覆盖保留分 P 的标题，避免
        biliup append 的技术文件名泄露到公开稿件元数据。
        """

        archive = view_data["archive"]
        videos = view_data.get("videos") or []
        if keep_only_cid is not None:
            videos = [v for v in videos if v.get("cid") == keep_only_cid]
            if not videos:
                raise ValueError(f"cid {keep_only_cid} not among archive videos")
        payload: dict[str, Any] = {
            key: archive[key] for key in EDIT_CLONE_FIELDS if key in archive
        }
        payload.update(
            {
                "aid": archive["aid"],
                "bvid": archive.get("bvid"),
                "videos": [
                    {
                        "filename": v["filename"],
                        "title": video_title if video_title is not None else (v.get("title") or "P1"),
                        "cid": v.get("cid"),
                    }
                    for v in videos
                ],
                "csrf": self.csrf,
            }
        )
        if title is not None:
            payload["title"] = title
        if cover_url is not None:
            payload["cover"] = cover_url
        if tag is not None:
            payload["tag"] = tag
        return payload

    def edit_archive(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self.post_json(
            f"{ARCHIVE_EDIT}?csrf={urllib.parse.quote(self.csrf)}", payload
        )
        if response.get("code") != 0:
            raise RuntimeError(f"edit failed: {response}")
        return response

    def season_episode_add(
        self, section_id: int, *, aid: int, cid: int, title: str
    ) -> Mapping[str, Any]:
        response = self.post_json(
            f"{SEASON_EPISODES_ADD}?csrf={urllib.parse.quote(self.csrf)}",
            {
                "sectionId": section_id,
                "episodes": [
                    {"aid": aid, "cid": cid, "title": title, "charging_pay": 0}
                ],
            },
        )
        if response.get("code") not in (0, SEASON_ALREADY_IN_CODE):
            raise RuntimeError(f"season add failed: {response}")
        return response

    # ---- 子进程/等待类操作（不进单测） -----------------------------------

    def biliup_append(self, bvid: str, media_path: Path, *, timeout: int = 1800) -> None:
        completed = subprocess.run(
            [str(BILIUP_BIN), "-u", self.cookie_path.name, "append", "-v", bvid, str(media_path)],
            cwd=str(self.cookie_path.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"biliup append rc={completed.returncode}: "
                f"{completed.stdout[-400:]}{completed.stderr[-200:]}"
            )

    def wait_new_cid(
        self,
        bvid: str,
        known_cids: Sequence[int],
        *,
        attempts: int = 40,
        interval_seconds: float = 15.0,
    ) -> Mapping[str, Any]:
        known = set(known_cids)
        for _ in range(attempts):
            time.sleep(interval_seconds)
            data = self.archive_view(bvid)
            fresh = [v for v in (data.get("videos") or []) if v.get("cid") not in known]
            if fresh:
                return fresh[0]
        raise TimeoutError(f"{bvid}: new P never appeared after append")


def replace_archive_source(
    session: BiliSession,
    bvid: str,
    *,
    new_media: Path,
    new_cover_png: Path | None = None,
    new_title: str | None = None,
) -> Mapping[str, Any]:
    """整套零配额换源：append → 等新 P → (换封面) → edit 只留新 P。"""

    before = session.archive_view(bvid)
    old_cids = [v.get("cid") for v in (before.get("videos") or [])]
    session.biliup_append(bvid, new_media)
    new_video = session.wait_new_cid(bvid, old_cids)
    cover_url = (
        session.cover_up(new_cover_png.read_bytes()) if new_cover_png is not None else None
    )
    current = session.archive_view(bvid)
    payload = session.build_edit_payload(
        current,
        title=new_title,
        cover_url=cover_url,
        keep_only_cid=new_video.get("cid"),
        video_title=new_title or str((before.get("archive") or {}).get("title") or "P1"),
    )
    return session.edit_archive(payload)
