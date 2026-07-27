#!/usr/bin/env python3
"""Standalone danmaku/gift/SC backup listener for one Bilibili live room.

Ivan 2026-07-27：录播姬挂掉的那几小时（7/25 案）礼物/弹幕记录全失——
感谢线修名等记录权威通道全部断粮。本监听器完全独立于录播姬/adapter 链，
直连 B 站直播 ws，把**全量** cmd 原文写成按日 jsonl，作为第二记录源。

- 登录态连接（bilitool config 的 cookies）：拿未打码用户名；匿名连接的
  uname 会是「A***」形态，修名价值大减。
- 不做任何过滤/解读：备份的职责是保真落盘，消费端自己挑 cmd。
- 崩溃/断线自动重连（指数退避）；UTC 日切换滚动文件；45 天保留。

运行（free 宿主，cron 每 5 分钟 flock -n 拉活）：
  /opt/bilive/danmaku-backup/venv/bin/python3 danmaku_backup_listener.py \
      --room 22966160 --out-root /opt/bilive/danmaku-backup \
      --cookie-config /opt/bilive/app/src/upload/bilitool/bilitool/model/config.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import brotli  # type: ignore[import-not-found]
import websocket  # type: ignore[import-not-found]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DANMU_INFO_URL = (
    "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
)
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
# WBI mixin 重排表（B 站 2023-09 起 getDanmuInfo 无签名回 -352 风控）
_WBI_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43,
    5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16,
    24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59,
    6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
HEARTBEAT_INTERVAL_S = 30
RETENTION_DAYS = 45
_HEADER = struct.Struct(">IHHII")

OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_NOTIFICATION = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8


def _log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {message}", flush=True)


def _load_cookies(config_path: Path) -> dict[str, str]:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _log(f"cookie config unreadable ({exc}); connecting anonymously")
        return {}
    cookies = payload.get("cookies")
    if not isinstance(cookies, dict):
        return {}
    return {str(k): str(v) for k, v in cookies.items() if isinstance(v, str)}


def _api_get(url: str, cookies: dict[str, str], *, referer: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Referer": referer,
            **(
                {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
                if cookies
                else {}
            ),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _ensure_buvid3(cookies: dict[str, str]) -> dict[str, str]:
    if cookies.get("buvid3"):
        return cookies
    try:
        payload = _api_get(SPI_URL, {}, referer="https://www.bilibili.com/")
        buvid = str((payload.get("data") or {}).get("b_3") or "")
        if buvid:
            return {**cookies, "buvid3": buvid}
    except Exception as exc:  # noqa: BLE001 — spi 只是加分项
        _log(f"buvid3 spi fetch failed: {exc}")
    return cookies


def _wbi_mixin_key(cookies: dict[str, str]) -> str:
    payload = _api_get(NAV_URL, cookies, referer="https://www.bilibili.com/")
    wbi = (payload.get("data") or {}).get("wbi_img") or {}
    img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    raw = img + sub
    return "".join(raw[i] for i in _WBI_MIXIN_TAB if i < len(raw))[:32]


def _wbi_sign(params: dict[str, object], mixin_key: str) -> dict[str, object]:
    import hashlib

    signed = dict(params)
    signed["wts"] = int(time.time())
    query = urllib.parse.urlencode(
        {
            key: "".join(
                ch for ch in str(value) if ch not in "!'()*"
            )
            for key, value in sorted(signed.items())
        }
    )
    signed["w_rid"] = hashlib.md5(
        (query + mixin_key).encode("utf-8")
    ).hexdigest()
    return signed


def _danmu_info(room: int, cookies: dict[str, str]) -> tuple[str, list[dict]]:
    mixin_key = _wbi_mixin_key(cookies)
    params = _wbi_sign({"id": room, "type": 0, "web_location": 444.8}, mixin_key)
    url = DANMU_INFO_URL + "?" + urllib.parse.urlencode(params)
    payload = _api_get(
        url, cookies, referer=f"https://live.bilibili.com/{room}"
    )
    if payload.get("code") != 0:
        raise RuntimeError(f"getDanmuInfo code={payload.get('code')}")
    data = payload["data"]
    return str(data["token"]), list(data.get("host_list") or [])


def _packet(op: int, body: bytes, *, ver: int = 1) -> bytes:
    return _HEADER.pack(_HEADER.size + len(body), _HEADER.size, ver, op, 0) + body


def _iter_frames(blob: bytes):
    offset = 0
    while offset + _HEADER.size <= len(blob):
        total, header_len, ver, op, _seq = _HEADER.unpack_from(blob, offset)
        if total < header_len:
            return
        body = blob[offset + header_len : offset + total]
        offset += total
        if op == OP_NOTIFICATION and ver == 3:
            yield from _iter_frames(brotli.decompress(body))
        elif op == OP_NOTIFICATION and ver == 2:
            import zlib

            yield from _iter_frames(zlib.decompress(body))
        else:
            yield op, ver, body


class DailyJsonlWriter:
    def __init__(self, out_root: Path, room: int) -> None:
        self.out_root = out_root
        self.room = room
        self.current_date: str | None = None
        self.handle = None
        self.connected_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _rotate_if_needed(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today == self.current_date and self.handle is not None:
            return
        if self.handle is not None:
            self.handle.close()
        day_dir = self.out_root / today
        day_dir.mkdir(parents=True, exist_ok=True)
        path = day_dir / f"{self.room}_{self.connected_at}.jsonl"
        self.handle = path.open("a", encoding="utf-8")
        self.current_date = today
        _log(f"writing {path}")

    def write(self, row: dict) -> None:
        self._rotate_if_needed()
        assert self.handle is not None
        self.handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.handle.flush()

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def _purge_old(out_root: Path) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    for child in sorted(out_root.iterdir()):
        if not child.is_dir():
            continue
        try:
            day = datetime.strptime(child.name, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            _log(f"purged {child}")


def _run_once(room: int, out_root: Path, cookies: dict[str, str]) -> None:
    token, hosts = _danmu_info(room, cookies)
    host = (hosts[0]["host"], int(hosts[0]["wss_port"])) if hosts else (
        "broadcastlv.chat.bilibili.com",
        443,
    )
    url = f"wss://{host[0]}:{host[1]}/sub"
    uid = 0
    try:
        uid = int(cookies.get("DedeUserID", "0") or "0")
    except ValueError:
        uid = 0
    auth = {
        "uid": uid,
        "roomid": room,
        "protover": 3,
        "platform": "web",
        "type": 2,
        "key": token,
    }
    if cookies.get("buvid3"):
        auth["buvid"] = cookies["buvid3"]
    _log(f"connecting {url} (uid={'anon' if uid == 0 else uid})")
    conn = websocket.create_connection(
        url,
        timeout=45,
        header=[f"User-Agent: {UA}", "Origin: https://live.bilibili.com"],
        cookie=(
            "; ".join(f"{k}={v}" for k, v in cookies.items())
            if cookies
            else None
        ),
    )
    writer = DailyJsonlWriter(out_root, room)
    try:
        conn.send_binary(
            _packet(OP_AUTH, json.dumps(auth, ensure_ascii=False).encode("utf-8"))
        )
        last_beat = 0.0
        while True:
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_INTERVAL_S:
                conn.send_binary(_packet(OP_HEARTBEAT, b"[object Object]"))
                last_beat = now
            try:
                frame = conn.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if isinstance(frame, str):
                frame = frame.encode("utf-8")
            if not frame:
                raise ConnectionError("empty ws frame (server closed)")
            recv_ms = int(time.time() * 1000)
            for op, _ver, body in _iter_frames(frame):
                if op == OP_AUTH_REPLY:
                    _log(f"auth reply: {body[:120]!r}")
                elif op == OP_NOTIFICATION:
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        payload = {"_undecodable": body.hex()[:2000]}
                    writer.write(
                        {"recv_ms": recv_ms, **(
                            payload
                            if isinstance(payload, dict)
                            else {"_payload": payload}
                        )}
                    )
    finally:
        writer.close()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", type=int, required=True)
    parser.add_argument("--out-root", type=Path, required=True)
    parser.add_argument("--cookie-config", type=Path, required=True)
    args = parser.parse_args()

    args.out_root.mkdir(parents=True, exist_ok=True)
    _purge_old(args.out_root)
    backoff = 5
    while True:
        cookies = _ensure_buvid3(_load_cookies(args.cookie_config))
        try:
            _run_once(args.room, args.out_root, cookies)
            backoff = 5
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001 — long-lived daemon must survive anything
            _log(f"listener error: {type(exc).__name__}: {exc}; retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(120, backoff * 2)


if __name__ == "__main__":
    socket.setdefaulttimeout(60)
    sys.exit(main())
