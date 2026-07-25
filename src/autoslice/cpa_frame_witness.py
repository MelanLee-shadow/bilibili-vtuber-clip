"""CPA 画面见证（2026-07-25 Ivan：看画面的任务交给 CPA，AGY 主听音频）。

屏上文字（游戏玩家 ID、面板名、字卡）是逐字可读的硬证据——温柔型李豆沙案
里游戏 ID 直接显示在画面上，一次视觉抄录即可终结「主播名指第三者是否矛盾」
的争议。本模块提供 hash-bound 的单帧视觉问答：ffmpeg 截帧 → base64 →
CPA 多模态 chat 调用，返回绑定 frame/prompt/response 哈希的审计收据。

模型选型（2026-07-25 四模型基准，Ivan 规则=准确度优先、同准确度才降级）：
gpt-5.6-terra 默认——唯一完整读出游戏 ID「温柔型李豆沙」且零 OCR 错字、
无字帧诚实答无；gpt-5.5 漏关键 ID；gpt-5.6-luna 有无中生有幻觉（编造不存在
的字幕），证据场景禁用；gpt-5.6-sol 基准时网关 408/503，恢复后可复测。

角色定位：**证据输入**，不是裁决者——收据由调用方放进裁决 request 的上下文
或审计披露；失败（截帧/网络/空答）返回 UNAVAILABLE 收据，绝不抛出阻塞。
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path

SCHEMA_VERSION = "cpa-frame-witness.v1"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"  # CPA Cloudflare 1010


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _extract_frame_jpeg(media_path: Path, ms: int, *, max_width: int = 1280) -> bytes:
    # JPEG + 降采样：网关对 multi-MB data URI 常 502；1280 宽足够读屏上 ID 文字。
    with tempfile.NamedTemporaryFile(suffix=".jpg") as handle:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{ms / 1000:.3f}", "-i", str(media_path),
                "-frames:v", "1",
                "-vf", f"scale=min({max_width}\\,iw):-2",
                "-q:v", "4",
                handle.name,
            ],
            capture_output=True, check=True,
        )
        return Path(handle.name).read_bytes()


def frame_vision_probe(
    media_path: Path,
    ms: int,
    question: str,
    *,
    api_base: str,
    api_key: str,
    model: str = "gpt-5.6-terra",
    timeout_seconds: float = 90.0,
    max_tokens: int = 1024,
) -> dict[str, object]:
    """One hash-bound visual Q&A about a single frame. Never raises."""

    receipt: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "media_path": str(media_path),
        "frame_ms": int(ms),
        "question": question,
        "model": model,
        "provider": "cpa",
    }
    try:
        frame_png = _extract_frame_jpeg(Path(media_path), int(ms))
    except Exception as exc:
        receipt.update(status="UNAVAILABLE", reason_code="FRAME_EXTRACT_FAILED",
                       error=f"{type(exc).__name__}: {exc}")
        return receipt
    receipt["frame_sha256"] = _sha256_bytes(frame_png)
    data_uri = "data:image/jpeg;base64," + base64.b64encode(frame_png).decode("ascii")
    # CPA 目录只有 gpt-5.x（Responses-API 原生；chat/completions 会误路由
    # 503/502——既有 cpa-gpt5-responses-api 规则），vision 走 /responses 的
    # input_image。
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": question},
                    {"type": "input_image", "image_url": data_uri},
                ],
            }
        ],
        "reasoning": {"effort": "low"},
        "max_output_tokens": max_tokens,
    }
    request_bytes = json.dumps(body).encode("utf-8")
    receipt["prompt_sha256"] = _sha256_bytes(
        json.dumps({"question": question, "frame_sha256": receipt["frame_sha256"],
                    "model": model}, sort_keys=True).encode("utf-8")
    )
    request = urllib.request.Request(
        api_base.rstrip("/") + "/responses",
        data=request_bytes,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": _UA,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        answer = payload.get("output_text")
        if not answer:
            parts = []
            for item in payload.get("output") or []:
                if isinstance(item, dict) and item.get("type") == "message":
                    for chunk in item.get("content") or []:
                        if isinstance(chunk, dict) and chunk.get("type") in (
                            "output_text", "text"
                        ) and chunk.get("text"):
                            parts.append(chunk["text"])
            answer = "".join(parts)
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError(
                f"empty vision completion (status={payload.get('status')})"
            )
    except Exception as exc:
        receipt.update(status="UNAVAILABLE", reason_code="VISION_CALL_FAILED",
                       error=f"{type(exc).__name__}: {exc}")
        return receipt
    receipt.update(
        status="OBSERVED",
        answer=answer.strip(),
        response_sha256=_sha256_bytes(raw),
    )
    return receipt
