#!/usr/bin/env python3
"""聚合免费 ASR 客户端：必剪(bcut) / 剪映(jianying) / 快手(kuaishou).

三家都是官方剪辑产品内置的免费转写接口（社区逆向，参考 SocialSisterYi/bcut-asr
与 WEIFENG2333/AsrTools），无需账号。统一输出句级(+词级，如有)毫秒时间戳。

- bcut     B站必剪，B站 AI 字幕同源，中文最强，句级+逐字毫秒时间戳。首选。
- jianying 剪映，质量相当；但 API 签名依赖第三方服务器 asrtools-update.bkfeng.top
           （bkfeng 自建），属不可控单点，只作备源。
- kuaishou 快手快影，**已下线**（服务端 501 效果禁用，实测）；不在 auto
           链上，仅保留实现供 --provider kuaishou 显式探测它是否复活。

Zero third-party deps (stdlib + ffmpeg on PATH)，可直接丢到 free 主机跑。

Usage:
  python3 free_asr_client.py input.(mp4|flv|flac|...) --srt out.srt --json out.json
  python3 free_asr_client.py input.mp4 --provider jianying --srt out.srt
  # --provider auto (default): bcut -> jianying 依次 failover（kuaishou 已下线不在链上）
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from pathlib import Path

UA_BILI = "Bilibili/1.0.0"
RETRYABLE_HTTP = {412, 429, 500, 502, 503, 504}
MAX_TRIES = 4


class AsrError(RuntimeError):
    pass


def _request(url: str, *, data: bytes | None = None, method: str = "GET",
             headers: dict | None = None,
             timeout: float = 120.0) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), dict(resp.headers)


def _request_json(url: str, *, data: bytes | None = None, method: str = "GET",
                  headers: dict | None = None, timeout: float = 120.0) -> dict:
    body, _ = _request(url, data=data, method=method, headers=headers,
                       timeout=timeout)
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise AsrError(f"non-JSON response from {url}: {body[:200]!r}") from e


def extract_audio_mp3(media: Path) -> bytes:
    """统一转 16k 单声道 mp3（三家接口都收）。"""
    if media.suffix.lower() == ".mp3":
        return media.read_bytes()
    with tempfile.NamedTemporaryFile(suffix=".mp3") as tmp:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "libmp3lame", "-b:a", "64k", tmp.name],
            check=True,
        )
        return Path(tmp.name).read_bytes()


# ---------------------------------------------------------------- bcut (必剪)

BCUT_BASE = "https://member.bilibili.com/x/bcut/rubick-interface"
BCUT_MODEL_ID = "7"


def _bcut_api(url: str, *, form: dict | None = None, js: dict | None = None,
              params: dict | None = None) -> dict:
    headers = {"User-Agent": UA_BILI, "Cache-Control": "no-cache"}
    data = None
    method = "GET"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        method = "POST"
    elif js is not None:
        data = json.dumps(js).encode()
        headers["Content-Type"] = "application/json"
        method = "POST"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last_err = None
    for attempt in range(MAX_TRIES):
        if attempt:
            time.sleep(2 ** attempt)
        try:
            payload = _request_json(url, data=data, method=method,
                                    headers=headers)
        except urllib.error.HTTPError as e:
            last_err = AsrError(f"HTTP {e.code} at {url}: {e.read()[:200]!r}")
            if e.code in RETRYABLE_HTTP:
                continue
            raise last_err from e
        code = payload.get("code")
        if code:
            last_err = AsrError(f"bcut API code {code}: {payload.get('message')}")
            if code == -509:  # 请求过于频繁
                continue
            raise last_err
        return payload["data"]
    raise last_err


def transcribe_bcut(sound: bytes, *, poll_interval: float = 3.0,
                    poll_timeout: float = 900.0, log=print) -> dict:
    name = f"{int(time.time())}.mp3"
    created = _bcut_api(f"{BCUT_BASE}/resource/create", form={
        "type": 2, "name": name, "size": len(sound),
        "resource_file_type": "mp3", "model_id": BCUT_MODEL_ID,
    })
    per_size, urls = created["per_size"], created["upload_urls"]
    log(f"[bcut] upload granted: {len(sound)//1024}KB in {len(urls)} part(s)")
    etags = []
    for i, part_url in enumerate(urls):
        chunk = sound[i * per_size:(i + 1) * per_size]
        _, headers = _request(part_url, data=chunk, method="PUT", timeout=300.0)
        etags.append((headers.get("Etag") or headers.get("ETag") or "").strip('"'))
    committed = _bcut_api(f"{BCUT_BASE}/resource/create/complete", form={
        "in_boss_key": created["in_boss_key"],
        "resource_id": created["resource_id"],
        "etags": ",".join(etags),
        "upload_id": created["upload_id"],
        "model_id": BCUT_MODEL_ID,
    })
    task = _bcut_api(f"{BCUT_BASE}/task", js={
        "resource": committed["download_url"], "model_id": BCUT_MODEL_ID,
    })
    task_id = task["task_id"]
    log(f"[bcut] task created: {task_id}")
    deadline = time.monotonic() + poll_timeout
    while time.monotonic() < deadline:
        rsp = _bcut_api(f"{BCUT_BASE}/task/result",
                        params={"model_id": BCUT_MODEL_ID, "task_id": task_id})
        if rsp.get("state") == 4:
            parsed = json.loads(rsp["result"])
            return {"utterances": [
                {"start_time": u["start_time"], "end_time": u["end_time"],
                 "transcript": u["transcript"], "words": u.get("words", [])}
                for u in parsed.get("utterances", [])
            ]}
        if rsp.get("state") == 3:
            raise AsrError(f"bcut task failed: {rsp.get('remark')}")
        time.sleep(poll_interval)
    raise AsrError(f"bcut poll timeout after {poll_timeout}s (task {task_id})")


# ------------------------------------------------------------ jianying (剪映)

JY_API = "https://lv-pc-api-sinfonlinec.ulikecam.com/lv/v1"
JY_SIGN_SERVER = "https://asrtools-update.bkfeng.top/sign"  # 第三方单点，见模块头注释
JY_APPVR = "6.6.0"  # 剪映客户端版本，服务端会校验，随 VideoCaptioner 追新
JY_UA = "Cronet/TTNetVersion:d4572e53 2024-06-12 QuicVersion:4bf243e0 2023-04-17"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")


def _jy_tdid() -> str:
    """VideoCaptioner 6.6.0 的 tdid 生成规则（年份末位定前缀）。"""
    i = int(str(datetime.datetime.now().year)[3])
    prefix = 390 + i
    tail = "3278516897751" if i % 2 else f"{uuid.getnode():013d}"
    return f"{prefix}{tail}"


JY_TDID = _jy_tdid()


def _jy_sign(path: str) -> tuple[str, str]:
    now = str(int(time.time()))
    payload = {"url": path, "current_time": now, "pf": "4",
               "appvr": JY_APPVR, "tdid": JY_TDID}
    # sign server 在 Cloudflare 后，非浏览器 UA 会 403（同 CPA error 1010 模式）
    rsp = _request_json(JY_SIGN_SERVER, data=json.dumps(payload).encode(),
                        method="POST",
                        headers={"Content-Type": "application/json",
                                 "User-Agent": BROWSER_UA,
                                 "tdid": JY_TDID, "t": now},
                        timeout=30.0)
    sign = rsp.get("sign")
    if not sign:
        raise AsrError(f"jianying sign server gave no sign: {rsp}")
    return sign.lower(), now


def _jy_headers(path: str) -> dict:
    sign, device_time = _jy_sign(path)
    return {"User-Agent": JY_UA, "appvr": JY_APPVR,
            "device-time": device_time, "pf": "4", "sign": sign,
            "sign-ver": "1", "tdid": JY_TDID,
            "Content-Type": "application/json"}


def _aws_sig_v4(secret_key: str, query: str, headers: dict) -> str:
    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    canonical_headers = "\n".join(f"{k}:{v}" for k, v in headers.items()) + "\n"
    signed_headers = ";".join(headers)
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical = f"GET\n/\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    amzdate = headers["x-amz-date"]
    datestamp = amzdate.split("T")[0]
    scope = f"{datestamp}/cn/vod/aws4_request"
    to_sign = ("AWS4-HMAC-SHA256\n" + amzdate + "\n" + scope + "\n"
               + hashlib.sha256(canonical.encode()).hexdigest())
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), datestamp),
                            "cn"), "vod"), "aws4_request")
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()


def transcribe_jianying(sound: bytes, *, poll_interval: float = 3.0,
                        poll_timeout: float = 900.0, log=print) -> dict:
    crc = f"{zlib.crc32(sound) & 0xFFFFFFFF:08x}"
    # 1. 拿上传临时凭证
    creds = _request_json(f"{JY_API}/upload_sign",
                          data=json.dumps({"biz": "pc-recognition"}).encode(),
                          method="POST", headers=_jy_headers("/lv/v1/upload_sign"))
    d = creds.get("data") or {}
    ak, sk, token = d.get("access_key_id"), d.get("secret_access_key"), d.get("session_token")
    if not (ak and sk and token):
        raise AsrError(f"jianying upload_sign failed: {creds}")
    # 2. ApplyUploadInner (AWS SigV4)
    query = ("Action=ApplyUploadInner&FileSize=" + str(len(sound))
             + "&FileType=object&IsInner=1&SpaceName=lv-mac-recognition"
             + "&Version=2020-11-19&s=5y0udbjapi")
    t = datetime.datetime.now(datetime.timezone.utc)
    aws_headers = {"x-amz-date": t.strftime("%Y%m%dT%H%M%SZ"),
                   "x-amz-security-token": token}
    sig = _aws_sig_v4(sk, query, aws_headers)
    datestamp = aws_headers["x-amz-date"].split("T")[0]
    auth = (f"AWS4-HMAC-SHA256 Credential={ak}/{datestamp}/cn/vod/aws4_request, "
            f"SignedHeaders=x-amz-date;x-amz-security-token, Signature={sig}")
    store = _request_json(f"https://vod.bytedanceapi.com/?{query}",
                          headers={**aws_headers, "authorization": auth})
    try:
        info = store["Result"]["UploadAddress"]["StoreInfos"][0]
        host = store["Result"]["UploadAddress"]["UploadHosts"][0]
    except (KeyError, IndexError) as e:
        raise AsrError(f"jianying ApplyUploadInner failed: {store}") from e
    store_uri, store_auth, upload_id = info["StoreUri"], info["Auth"], info["UploadID"]
    up_headers = {"User-Agent": "Mozilla/5.0", "Authorization": store_auth,
                  "Content-CRC32": crc}
    # 3. 传文件 + 校验 + 提交（AsrTools 同款流程）
    rsp = _request_json(f"https://{host}/{store_uri}?partNumber=1&uploadID={upload_id}",
                        data=sound, method="PUT", headers=up_headers, timeout=300.0)
    if rsp.get("success") != 0:
        raise AsrError(f"jianying upload failed: {rsp}")
    # check 成功即文件就位；后面这次 commit-PUT 服务端常回 400，客户端
    # (VideoCaptioner) 故意无视其结果，我们也 fail-soft。
    _request(f"https://{host}/{store_uri}?uploadID={upload_id}",
             data=f"1:{crc}".encode(), method="POST", headers=up_headers)
    try:
        _request(f"https://{host}/{store_uri}?uploadID={upload_id}&partNumber=1"
                 f"&x-amz-security-token={token}",
                 data=sound, method="PUT", headers=up_headers, timeout=300.0)
    except urllib.error.HTTPError:
        pass
    log(f"[jianying] uploaded: {store_uri}")
    # 4. 提交转写任务
    submit = _request_json(f"{JY_API}/audio_subtitle/submit", data=json.dumps({
        "adjust_endtime": 200, "audio": store_uri, "caption_type": 2,
        "client_request_id": str(uuid.uuid4()), "max_lines": 1,
        "songs_info": [{"end_time": 6000, "id": "", "start_time": 0}],
        "words_per_line": 16,
    }).encode(), method="POST", headers=_jy_headers("/lv/v1/audio_subtitle/submit"))
    query_id = (submit.get("data") or {}).get("id")
    if not query_id:
        raise AsrError(f"jianying submit failed: {submit}")
    log(f"[jianying] task created: {query_id}")
    # 5. 轮询
    deadline = time.monotonic() + poll_timeout
    while time.monotonic() < deadline:
        rsp = _request_json(f"{JY_API}/audio_subtitle/query", data=json.dumps({
            "id": query_id, "pack_options": {"need_attribute": True},
        }).encode(), method="POST", headers=_jy_headers("/lv/v1/audio_subtitle/query"))
        utterances = (rsp.get("data") or {}).get("utterances")
        if utterances:
            return {"utterances": [
                {"start_time": u["start_time"], "end_time": u["end_time"],
                 "transcript": u["text"],
                 "words": [{"label": w["text"].strip(),
                            "start_time": w["start_time"],
                            "end_time": w["end_time"]}
                           for w in u.get("words", [])]}
                for u in utterances
            ]}
        if utterances == []:  # 任务完成但空结果（无语音）
            return {"utterances": []}
        time.sleep(poll_interval)
    raise AsrError(f"jianying poll timeout after {poll_timeout}s (id {query_id})")


# ------------------------------------------------------------ kuaishou (快手)

KS_URL = "https://ai.kuaishou.com/api/effects/subtitle_generate"


def transcribe_kuaishou(sound: bytes, *, poll_interval: float = 0.0,
                        poll_timeout: float = 0.0, log=print) -> dict:
    boundary = uuid.uuid4().hex
    parts = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="typeId"\r\n\r\n1\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="audio.mp3"\r\n'
        "Content-Type: audio/mpeg\r\n\r\n"
    ).encode() + sound + f"\r\n--{boundary}--\r\n".encode()
    log(f"[kuaishou] uploading {len(sound)//1024}KB")
    rsp = _request_json(
        KS_URL, data=parts, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "Mozilla/5.0"},
        timeout=600.0)
    data = rsp.get("data")
    if not isinstance(data, dict) or "text" not in data:
        raise AsrError(f"kuaishou unexpected response: {str(rsp)[:300]}")
    return {"utterances": [
        {"start_time": u["start_time"], "end_time": u["end_time"],
         "transcript": u["text"], "words": []}
        for u in data["text"]
    ]}


# ----------------------------------------------------------------- 聚合与输出

PROVIDERS = {
    "bcut": transcribe_bcut,
    "jianying": transcribe_jianying,
    "kuaishou": transcribe_kuaishou,
}
# kuaishou is server-side dead (501 效果禁用) — kept in PROVIDERS for explicit
# revival probes, but excluded from auto failover so error output stays clean.
AUTO_CHAIN = ["bcut", "jianying"]


def transcribe(sound: bytes, provider: str = "auto", *,
               poll_interval: float = 3.0, poll_timeout: float = 900.0,
               log=print) -> dict:
    chain = AUTO_CHAIN if provider == "auto" else [provider]
    errors = []
    for name in chain:
        try:
            t0 = time.monotonic()
            result = PROVIDERS[name](sound, poll_interval=poll_interval,
                                     poll_timeout=poll_timeout, log=log)
            result["provider"] = name
            result["elapsed_s"] = round(time.monotonic() - t0, 1)
            return result
        except Exception as e:  # noqa: BLE001 - failover 后统一上报
            errors.append(f"{name}: {type(e).__name__}: {e}")
            log(f"[{name}] failed, trying next provider: {e}")
    raise AsrError("all providers failed:\n  " + "\n  ".join(errors))


def _srt_ts(ms: int) -> str:
    return (f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:"
            f"{ms // 1000 % 60:02d},{ms % 1000:03d}")


def to_srt(result: dict, offset_ms: int = 0) -> str:
    lines = []
    for n, seg in enumerate(result.get("utterances", []), 1):
        lines.append(f"{n}\n{_srt_ts(seg['start_time'] + offset_ms)} --> "
                     f"{_srt_ts(seg['end_time'] + offset_ms)}\n"
                     f"{seg['transcript']}\n")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", type=Path, help="media file (any ffmpeg-readable)")
    ap.add_argument("--provider", default="auto",
                    choices=["auto", *PROVIDERS], help="default: auto failover")
    ap.add_argument("--srt", type=Path, help="write sentence-level SRT here")
    ap.add_argument("--json", type=Path, dest="json_out",
                    help="write raw result JSON (keeps word timings if any)")
    ap.add_argument("--offset-ms", type=int, default=0,
                    help="shift all timestamps (for chunked transcription)")
    ap.add_argument("--poll-interval", type=float, default=3.0)
    ap.add_argument("--poll-timeout", type=float, default=900.0)
    args = ap.parse_args()

    sound = extract_audio_mp3(args.input)
    result = transcribe(sound, args.provider,
                        poll_interval=args.poll_interval,
                        poll_timeout=args.poll_timeout)
    n = len(result["utterances"])
    print(f"[{result['provider']}] done in {result['elapsed_s']}s: {n} utterances")
    if not n:
        print("WARNING: empty recognition result", file=sys.stderr)
    if args.json_out:
        args.json_out.write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.srt:
        args.srt.write_text(to_srt(result, args.offset_ms), encoding="utf-8")
    if not args.srt and not args.json_out:
        print(to_srt(result, args.offset_ms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
