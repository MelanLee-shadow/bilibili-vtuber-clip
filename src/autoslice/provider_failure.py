"""Provider-failure detail preservation and service-vs-quota classification.

2026-08-10 事故（8/7 四条复活件同批 `provider_transient` 全灭）暴露两个缺口：

1. **诊断盲区**：`llm_client` 已经把桥接脚本逐模型级联的 stderr（含每次尝试的
   HTTP 码）收进 ``LlmCallError`` 的消息里，但上一层
   （``final_review_auditor`` / ``pronoun_consistency``）只留
   ``type(exc).__name__``，于是盘上只剩一个 ``"LlmCallError"``——事后无法判断
   到底是 429 配额、503/408 服务故障，还是 400 请求被拒。
2. **归类混同**：三类失败共用一个 ``provider_transient``，运维看不出"该等"
   还是"该换 key"。

本模块只做两件小事：把异常消息**有界保真**地留下来，并从保真文本里解析出
状态码与一个信息性的 provider 类别。它**不改变** ``failure_kind`` 或
可恢复性——那是 ``talk_lane`` 的既有策略面，动它会连带改写
``INFRASTRUCTURE_WAIT_FAILURE_KINDS`` 的重试语义和历史账本。
"""

from __future__ import annotations

import json
import re

from src.autoslice.llm_client import LLM_JSON_PARSE_REASON_CODES

PROVIDER_DETAIL_LIMIT = 2000

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
    # 2026-08-10：CPA 上游 sudocode 的分组路由抽签（Ivan 裁定 #8：一个分组带
    # gpt-image 能力，另一个带 gpt-5.6-sol 能力）。落到没有该能力的分组就 400
    # group_capability_unavailable——**请求本身是合法的**，单次失败率约 15–17%，
    # 与 payload 大小、与模型都无关（实测：0B 失败而 2000B 成功，非单调）。
    # 桥接脚本已把它当瞬时故障退避重试，这里必须跟着归到 service（"该等/会自
    # 己好"），否则上层读到 class=rejected 会误判成"请求写错了"而放弃。
    # 线上响应体三种字样都认（机器码 / 中文 / metadata.message_en）。
    "group_capability_unavailable",
    "当前分组不支持",
    "current group does not support",
)

QUOTA = "quota"
SERVICE = "service"
REJECTED = "rejected"
UNKNOWN = "unknown"

# ``*_UNAVAILABLE:<ExcType>`` reason codes (boundary semantic review and its
# siblings) carry the *type name* of whatever escaped the LLM leg.  Only these
# names mean "the provider never rendered a verdict, so nothing was judged";
# every other name is a code defect in our own stage.
#
# The distinction is load-bearing, not cosmetic: a transport name routes the
# candidate into ``INFRASTRUCTURE_WAIT_FAILURE_KINDS`` (unbounded timed retry),
# and ``delivery_recovery`` warns in as many words that putting a deterministic
# defect on that lane makes it churn the identical fingerprint every tick
# forever.  So the allowlist stays closed: unknown名字一律按内容/代码缺陷终态。
TRANSPORT_EXCEPTION_NAMES = frozenset(
    {
        # llm_client raises this for every transport outcome it cannot use:
        # non-zero bridge rc, subprocess timeout, missing/empty completion
        # file, unparseable JSON in an otherwise-200 completion.  All four are
        # "ask again later", none is a verdict about the content.
        "LlmCallError",
        "TimeoutError",
        "TimeoutExpired",
        "SubprocessError",
        "CalledProcessError",
        "ConnectionError",
        "ConnectionAbortedError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "BrokenPipeError",
        "OSError",
        "IOError",
        "HTTPError",
        "URLError",
        "SSLError",
        "RemoteDisconnected",
        "IncompleteRead",
    }
)

_UNAVAILABLE_REASON_RX = re.compile(r"^[A-Z][A-Z0-9_]*UNAVAILABLE:(.+)$")


def transport_unavailable_reason(reason_codes: object) -> str | None:
    """Return the ``*_UNAVAILABLE:<ExcType>`` code naming a transport failure.

    ``None`` means: no such code, or the exception name is not on the closed
    transport allowlist — i.e. do not treat this as an infrastructure wait.
    """

    if isinstance(reason_codes, str):
        candidates: list[str] = [reason_codes]
    elif isinstance(reason_codes, (list, tuple)):
        candidates = [str(code) for code in reason_codes if str(code)]
    else:
        return None
    for code in candidates:
        match = _UNAVAILABLE_REASON_RX.match(code.strip())
        if match is None:
            continue
        # ``socket.timeout`` and friends arrive dotted; compare on the leaf.
        exception_name = match.group(1).strip().rsplit(".", 1)[-1]
        if exception_name in TRANSPORT_EXCEPTION_NAMES:
            return code
    return None


def provider_failure_detail(
    exc: BaseException, *, limit: int = PROVIDER_DETAIL_LIMIT
) -> str:
    """Return the exception message, tail-truncated to ``limit`` characters.

    The tail is kept for the same reason ``llm_client`` keeps the last 4000
    characters of bridge stderr: a multi-model failover cascade puts the
    decisive "failed on all models" line last, and a head truncation would drop
    it.  A leading ellipsis marks that earlier attempts were cut.
    """

    message = str(exc).strip()
    if not message:
        return ""
    if len(message) <= limit:
        return message
    return "…" + message[-limit:]


def provider_failure_detail_from_cause(
    exc: BaseException, *, limit: int = PROVIDER_DETAIL_LIMIT
) -> str:
    """Bounded provider message taken from a typed audit error's cause.

    The audit errors (``FinalReviewAuditError``,
    ``CandidatePronounAuditError``) deliberately keep a short ``detail`` — it
    feeds the failure fingerprint — and are raised ``from`` the underlying
    ``LlmCallError``.  The verbatim bridge cascade therefore lives on
    ``__cause__``; read it there instead of widening the audit error itself.
    """

    cause = exc.__cause__
    return provider_failure_detail(cause, limit=limit) if cause else ""


_MARKER_REASON_RX = re.compile(r":\s*(\[[^\r\n]*?\])")


def marker_transport_unavailable(text: str, marker: str) -> str | None:
    """producer 的 ``<marker>: ["REASON", …]`` 行里是否只是 provider 打不通。

    producer 的致命 marker 把 reason_codes 以 JSON 数组原样打在同一行上，所以
    "边界复核为什么没通过"这条信息在子进程输出里是可读的——读最后一条同名
    marker（append-only 日志里，最后一条才是本次致命的那条），解析它自己的
    reason_codes，再交给闭集 :func:`transport_unavailable_reason` 判定。
    """

    last = None
    for line in text.splitlines():
        if marker in line:
            last = line
    if last is None:
        return None
    match = _MARKER_REASON_RX.search(last[last.index(marker) + len(marker):])
    if match is None:
        return None
    try:
        codes = json.loads(match.group(1))
    except ValueError:
        return None
    return transport_unavailable_reason(codes)


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
    would have served.  That is exactly the 2026-08-10 shape (all ChatGPT
    OAuth credentials ``usage_limit_reached`` → traffic falls through to the
    secondary leg → ``group_capability_unavailable`` 400).

    2026-08-10 补测（free 直打 CPA）修正了那条级联里 400 的读法：
    ``group_capability_unavailable`` **不是**次级 leg 的拒绝，而是上游 sudocode
    的分组路由抽签（Ivan 裁定 #8：一组带 gpt-image 能力、一组带 gpt-5.6-sol
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


def describe_provider_failure(text: str) -> dict[str, object]:
    """Compact, JSON-safe provider-failure descriptor for receipts/state."""

    return {
        "provider_class": classify_provider_failure(text),
        "provider_status_codes": provider_failure_status_codes(text),
    }


def auditor_unavailable_discovery(
    detail: str, provider_detail: str = ""
) -> dict[str, object]:
    """AUDITOR_UNAVAILABLE discovery block with the provider evidence kept.

    ``detail`` stays the short, stable identity that failure fingerprints and
    the existing contract readers already consume.  The verbatim provider text
    (plus the class/status codes parsed out of it) is added alongside so a
    provider outage stops reading as a bare ``LlmCallError`` on disk.
    """

    discovery: dict[str, object] = {
        "status": "AUDITOR_UNAVAILABLE",
        "detail": detail,
    }
    parse_code = next(
        (
            value
            for value in (detail, provider_detail)
            if isinstance(value, str) and value in LLM_JSON_PARSE_REASON_CODES
        ),
        None,
    )
    if parse_code is not None:
        # A completion must never cross the review boundary. The typed parser
        # code alone distinguishes malformed output from a transport outage.
        discovery["provider_error_code"] = parse_code
        return discovery
    if provider_detail:
        discovery["provider_detail"] = provider_detail
        discovery.update(describe_provider_failure(provider_detail))
    return discovery


# Failure-evidence surfaces copy these discovery fields through; the provider
# ones are diagnostics only.
DISCOVERY_EVIDENCE_FIELDS: tuple[str, ...] = (
    "status",
    "detail",
    "provider_error_code",
    "provider_detail",
    "provider_class",
    "provider_status_codes",
)

# provider 保真证据不参与失败身份：它逐次尝试都不同（HTTP 码组合、上游
# request id），一旦进 fingerprint 就会把"同一个故障"每轮伪装成新故障，
# 破坏 dedup / carryover / rescore 的绑定。
FINGERPRINT_VOLATILE_KEYS = frozenset(
    {
        "surface_files",
        "provider_detail",
        "provider_class",
        "provider_status_codes",
        "provider_error_code",
    }
)

_CLASS_PRECEDENCE = (QUOTA, SERVICE, REJECTED, UNKNOWN)


def _collect_provider_details(value: object, sink: list[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "provider_detail" and isinstance(nested, str) and nested:
                sink.append(nested)
            else:
                _collect_provider_details(nested, sink)
    elif isinstance(value, list):
        for item in value:
            _collect_provider_details(item, sink)


def provider_failure_label(
    failure_evidence: object, attempt_tail: str = ""
) -> dict[str, object]:
    """Informational provider class/status codes for a failed produce.

    Reads the preserved provider text out of the failure evidence first (that is
    where the bridge's per-attempt HTTP codes now land), then falls back to the
    producer tail so a failure surfaced only through stderr still gets labelled.
    Returns ``{}`` when nothing provider-shaped was observed, so non-provider
    failures keep their existing state shape byte-for-byte.
    """

    details: list[str] = []
    _collect_provider_details(failure_evidence, details)
    codes: set[int] = set()
    classes: set[str] = set()
    for detail in details:
        codes.update(provider_failure_status_codes(detail))
        classes.add(classify_provider_failure(detail))
    if not details:
        tail_codes = provider_failure_status_codes(attempt_tail)
        tail_class = classify_provider_failure(attempt_tail)
        if not tail_codes and tail_class == UNKNOWN:
            return {}
        codes.update(tail_codes)
        classes.add(tail_class)
    classes.discard(UNKNOWN)
    resolved = next(
        (item for item in _CLASS_PRECEDENCE if item in classes), UNKNOWN
    )
    return {
        "failure_provider_class": resolved,
        "failure_provider_status_codes": sorted(codes),
    }


__all__ = [
    "DISCOVERY_EVIDENCE_FIELDS",
    "FINGERPRINT_VOLATILE_KEYS",
    "PROVIDER_DETAIL_LIMIT",
    "QUOTA",
    "REJECTED",
    "SERVICE",
    "TRANSPORT_EXCEPTION_NAMES",
    "UNKNOWN",
    "LLM_JSON_PARSE_REASON_CODES",
    "transport_unavailable_reason",
    "auditor_unavailable_discovery",
    "classify_provider_failure",
    "marker_transport_unavailable",
    "describe_provider_failure",
    "provider_failure_detail",
    "provider_failure_detail_from_cause",
    "provider_failure_label",
    "provider_failure_status_codes",
]
