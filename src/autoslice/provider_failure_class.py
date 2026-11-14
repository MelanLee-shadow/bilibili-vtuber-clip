"""Which kind a provider failure is, from its preserved text: quota, service, rejected or unknown.

Split out of provider_failure so the lane classifies a failed call the same way without loading the LLM client;
provider_failure re-exports the names for its existing importers."""
from __future__ import annotations

import re

# The bridge (scripts/llm_via_cpa.sh) emits one of these per attempt; the legacy
# curl surface (`curl: (22) The requested URL returned error: 503`) is still
# produced by older deployments and by curl itself, so both are parsed.
_BRIDGE_STATUS_RX = re.compile(r"\bhttp=(\d{3})\b")

_CURL_STATUS_RX = re.compile(r"returned error:\s*(\d{3})\b")

_GENERIC_STATUS_RX = re.compile(r"\bHTTP(?:\s+Error)?[ =:]+(\d{3})\b")

_QUOTA_MARKERS = (
    "usage_limit_reached",
    "usage limit has been reached",
    "resource_exhausted",
    "insufficient_quota",
    "quota exceeded",
    "individual quota reached",
    "too many requests",
    "rate limit",
)

_SERVICE_MARKERS = (
    "timed out",
    "timeout",
    "empty completion",
    "connection reset",
    "connection refused",
    "could not resolve host",
    "auth_unavailable",
    "no auth available",


    "group_capability_unavailable",
    "当前分组不支持",
    "current group does not support",
)

QUOTA = "quota"

SERVICE = "service"

REJECTED = "rejected"

UNKNOWN = "unknown"


def provider_failure_status_codes(text: str) -> list[int]:
    """Every HTTP status observed in a preserved provider-failure text."""

    if not text:
        return []
    codes: set[int] = set()
    for pattern in (_BRIDGE_STATUS_RX, _CURL_STATUS_RX, _GENERIC_STATUS_RX):
        for match in pattern.finditer(text):
            code = int(match.group(1))
            if 100 <= code <= 599:
                codes.add(code)
    return sorted(codes)


def classify_provider_failure(text: str) -> str:
    """Label a preserved provider-failure text as quota / service / rejected.

 Precedence is quota > service > rejected: a cascade that hit even one
 quota signal must read as "wait for the window / rotate the key" rather
 than "the request itself is wrong", because the later 4xx in such a
 cascade is usually the fallback leg refusing what the exhausted primary
 would have served. That is exactly the shape (all ChatGPT
 OAuth credentials ``usage_limit_reached`` → traffic falls through to the
 secondary leg → ``group_capability_unavailable`` 400).

 补测（free 直打 CPA）修正了那条级联里 400 的读法：
 ``group_capability_unavailable`` **不是**次级 leg 的拒绝，而是上游 sudocode
 的分组路由抽签（公开规则裁定 #8：一组带 gpt-image 能力、一组带 gpt-5.6-sol
 能力）。同一个请求原样重发就可能落到对的分组：单次失败率 ~15–17%，与
 payload 大小无关（0B 失败而 2000B 成功）、与模型无关。所以它现在归
 **service**（等/重试）而不是 ``rejected``——桥接脚本已按瞬时故障退避重试，
 分类必须一致，否则上层看到 ``class=rejected`` 会把一次抽签失败读成"请求本
 身写错了"。precedence 不变：级联里只要出现配额信号仍然是 quota。
 """

    if not text:
        return UNKNOWN
    lowered = text.lower()
    codes = set(provider_failure_status_codes(text))
    if 429 in codes or any(marker in lowered for marker in _QUOTA_MARKERS):
        return QUOTA
    if (
        any(500 <= code <= 599 for code in codes)
        or 408 in codes
        or any(marker in lowered for marker in _SERVICE_MARKERS)
    ):
        return SERVICE
    if any(400 <= code <= 499 for code in codes):
        return REJECTED
    return UNKNOWN
