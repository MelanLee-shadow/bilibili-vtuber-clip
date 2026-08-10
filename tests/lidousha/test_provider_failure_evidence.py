"""2026-08-10 provider 失败保真与归类。

事故实况：8/7 四条复活件同批 ``provider_transient`` 全灭，盘上只留一个
``"detail": "LlmCallError"``——既看不出是 429 配额、503/408 服务故障还是 400
请求被拒，也就无从判断"该等"还是"该换 key"。取证靠去上游 nginx/CLIProxyAPI
的日志与 usage 库反查（三把 ChatGPT OAuth 凭据同时 usage_limit_reached，流量
落到次级 leg 后 400/408/5xx 混着来）。这些用例把"回执自带诊断"变成契约。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner  # noqa: F401  (binds talk_lane's RunnerProxy)
from src.autoslice import provider_failure as pf
from src.autoslice import infra_retry_policy
from src.autoslice import talk_lane
from src.autoslice.final_review_auditor import (
    FinalReviewAuditError,
    _request_final_review_findings,
)
from src.autoslice.llm_client import LlmCallError
from src.autoslice.pronoun_consistency import (
    CandidatePronounAuditError,
    discover_candidate_pronoun_findings,
)

_auditor_unavailable_discovery = pf.auditor_unavailable_discovery


BRIDGE_CASCADE = (
    "llm command failed rc=1: "
    "[cpa] model=gpt-5.6-sol attempt=1 http=503 curl_exit=22 "
    "empty_completion=0 result=failed\n"
    "[cpa] model=gpt-5.5 attempt=1 http=408 curl_exit=22 "
    "empty_completion=0 result=failed\n"
    "CPA /responses failed on all models: gpt-5.6-sol gpt-5.5 gpt-5.4"
)


def test_detail_keeps_the_bridge_cascade_bounded():
    exc = LlmCallError(BRIDGE_CASCADE)
    detail = pf.provider_failure_detail(exc)

    assert "http=503" in detail
    assert "http=408" in detail
    assert "failed on all models" in detail


def test_detail_truncation_keeps_the_decisive_tail():
    exc = LlmCallError("x" * 5000 + "failed on all models: a b c")
    detail = pf.provider_failure_detail(exc, limit=200)

    assert len(detail) == 201  # ellipsis + limit
    assert detail.startswith("…")
    assert detail.endswith("failed on all models: a b c")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("[cpa] http=503", pf.SERVICE),
        ("[cpa] http=408", pf.SERVICE),
        ("[cpa] http=500", pf.SERVICE),
        ("llm command timed out after 600s", pf.SERVICE),
        ("llm command wrote an empty completion", pf.SERVICE),
        ("[cpa] http=429", pf.QUOTA),
        ('{"type":"usage_limit_reached"}', pf.QUOTA),
        ("RESOURCE_EXHAUSTED", pf.QUOTA),
        ("[cpa] http=400 group_capability_unavailable", pf.REJECTED),
        ("[cpa] http=403", pf.REJECTED),
        ("", pf.UNKNOWN),
        ("something entirely unclassifiable", pf.UNKNOWN),
    ],
)
def test_provider_class_is_derived_from_the_preserved_text(text, expected):
    assert pf.classify_provider_failure(text) == expected


def test_quota_outranks_a_rejection_in_the_same_cascade():
    """8/10 的真实形状：主 leg 配额耗尽 → 次级 leg 用 400 拒掉。

    这条级联的正确读法是"等配额窗口/换 key"，不是"请求本身写错了"。
    """

    cascade = (
        "[cpa] model=gpt-5.6-sol attempt=1 http=429 curl_exit=22\n"
        "[cpa] model=gpt-5.5 attempt=1 http=400 curl_exit=22"
    )

    assert pf.classify_provider_failure(cascade) == pf.QUOTA
    assert pf.provider_failure_status_codes(cascade) == [400, 429]


def test_legacy_curl_surface_is_still_parsed():
    legacy = "curl: (22) The requested URL returned error: 503"

    assert pf.provider_failure_status_codes(legacy) == [503]
    assert pf.classify_provider_failure(legacy) == pf.SERVICE


def test_final_review_provider_error_carries_the_cascade():
    def failing_call(_prompt: str) -> str:
        raise LlmCallError(BRIDGE_CASCADE)

    with pytest.raises(FinalReviewAuditError) as excinfo:
        _request_final_review_findings(
            "prompt", llm_call=failing_call, extract_json=json.loads
        )

    error = excinfo.value
    assert error.reason_code == "FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"
    # The short, fingerprint-bearing identity must not drift...
    assert error.detail == "LlmCallError"
    # ...and the verbatim cascade stays reachable through the exception chain.
    assert "http=503" in pf.provider_failure_detail_from_cause(error)


def test_candidate_pronoun_provider_error_carries_the_cascade():
    def failing_call(_prompt: str) -> str:
        raise LlmCallError(BRIDGE_CASCADE)

    with pytest.raises(CandidatePronounAuditError) as excinfo:
        discover_candidate_pronoun_findings(
            "1\n00:00:00,000 --> 00:00:02,000\n她说她来了\n",
            policy_text="（无）",
            candidate_context_text="（无）",
            llm_call=failing_call,
            extract_json=json.loads,
        )

    error = excinfo.value
    assert error.reason_code == "CANDIDATE_PRONOUN_PROVIDER_OR_JSON_UNAVAILABLE"
    assert error.detail == "LlmCallError"
    assert "http=408" in pf.provider_failure_detail_from_cause(error)


def test_auditor_unavailable_discovery_labels_the_provider_failure():
    discovery = _auditor_unavailable_discovery("LlmCallError", BRIDGE_CASCADE)

    assert discovery["status"] == "AUDITOR_UNAVAILABLE"
    assert discovery["detail"] == "LlmCallError"
    assert discovery["provider_class"] == pf.SERVICE
    assert discovery["provider_status_codes"] == [408, 503]
    assert "http=503" in discovery["provider_detail"]


def test_auditor_unavailable_discovery_shape_is_unchanged_without_provider_text():
    assert _auditor_unavailable_discovery("LlmCallError") == {
        "status": "AUDITOR_UNAVAILABLE",
        "detail": "LlmCallError",
    }


def _authority_with_discovery(
    tmp_path: Path, candidate_id: str, discovery: dict
) -> str:
    audit = {
        "schema_version": "final-review-audit.v2",
        "status": "AUDITOR_UNAVAILABLE",
        "release_gate": "BLOCK",
        "reason_codes": ["FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"],
        "reviewed_srt_sha256": "sha256:" + "c" * 64,
        "discovery": discovery,
        "findings": [],
        "validated_finding_count": 0,
        "boundary_semantic_review": {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "PASS",
        },
    }
    directory = tmp_path / candidate_id
    directory.mkdir(parents=True, exist_ok=True)
    authority = directory / f"{candidate_id}.chat-authority.json"
    authority.write_text(
        json.dumps({"final_review_audit": audit}, ensure_ascii=False),
        encoding="utf-8",
    )
    (directory / f"{candidate_id}.review-flags.json").write_text(
        json.dumps(audit, ensure_ascii=False), encoding="utf-8"
    )
    return str(authority)


def test_state_labels_the_provider_class_without_moving_failure_identity(
    tmp_path: Path,
):
    """归类是信息性附加层：failure_kind / 可恢复性 / fingerprint 全部不动。

    fingerprint 必须与"没有 provider 保真文本"时逐字节相同——否则同一个故障
    每轮都换身份，dedup / carryover / rescore 的绑定全部失效。
    """

    plain = talk_lane.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_DISCOVERY_INCOMPLETE: "
        + _authority_with_discovery(
            tmp_path,
            "auto_plain",
            {"status": "AUDITOR_UNAVAILABLE", "detail": "LlmCallError"},
        )
    )
    labelled = talk_lane.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_DISCOVERY_INCOMPLETE: "
        + _authority_with_discovery(
            tmp_path,
            "auto_plain",
            {
                "status": "AUDITOR_UNAVAILABLE",
                "detail": "LlmCallError",
                "provider_detail": BRIDGE_CASCADE,
                **pf.describe_provider_failure(BRIDGE_CASCADE),
            },
        )
    )

    assert labelled["failure_kind"] == "provider_transient"
    assert labelled["failure_recoverable"] is True
    assert labelled["failure_provider_class"] == pf.SERVICE
    assert labelled["failure_provider_status_codes"] == [408, 503]
    assert "http=503" in labelled["failure_evidence"]["discovery"]["provider_detail"]
    assert labelled["failure_fingerprint"] == plain["failure_fingerprint"]


def test_quota_class_survives_the_correction_pass_surface(tmp_path: Path):
    """CORRECTION_DISCOVERY_INCOMPLETE 那条路径的 discovery 会被压缩，
    provider 证据必须一起带过去，否则 8/7 三条候选的真因照样看不见。"""

    cascade = "[cpa] model=gpt-5.6-sol attempt=1 http=429 curl_exit=22"
    candidate_id = "auto_correction"
    audit = {
        "schema_version": "final-review-audit.v2",
        "status": "FLAGGED",
        "release_gate": "BLOCK",
        "reason_codes": [
            "FINAL_REVIEW_UNRESOLVED_FINDINGS",
            "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID",
        ],
        "reviewed_srt_sha256": "sha256:" + "d" * 64,
        "discovery": {"status": "COMPLETE"},
        "findings": [],
        "validated_finding_count": 0,
        "boundary_semantic_review": {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "PASS",
        },
        "correction_pass": {
            "schema_version": "final-review-audit.v1",
            "status": "AUDITOR_UNAVAILABLE",
            "reason_codes": ["FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"],
            "discovery": {
                "status": "AUDITOR_UNAVAILABLE",
                "detail": "LlmCallError",
                "provider_detail": cascade,
                **pf.describe_provider_failure(cascade),
            },
            "error_type": "FinalReviewAuditError",
            "applied_count": 0,
        },
        "correction_mutation_authority": {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "BLOCK",
            "applied_count": 0,
            "failures": [
                {
                    "reason_code": "CORRECTION_DISCOVERY_INCOMPLETE",
                    "upstream_reason_codes": [
                        "FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"
                    ],
                }
            ],
        },
    }
    directory = tmp_path / candidate_id
    directory.mkdir(parents=True, exist_ok=True)
    authority = directory / f"{candidate_id}.chat-authority.json"
    authority.write_text(
        json.dumps({"final_review_audit": audit}, ensure_ascii=False),
        encoding="utf-8",
    )
    (directory / f"{candidate_id}.review-flags.json").write_text(
        json.dumps(audit, ensure_ascii=False), encoding="utf-8"
    )

    classified = talk_lane.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: "
        "FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID: "
        f"{authority}"
    )

    assert classified["failure_kind"] == "provider_transient"
    assert classified["failure_stage"] == "final_review_correction_discovery"
    assert classified["failure_provider_class"] == pf.QUOTA
    correction_discovery = classified["failure_evidence"]["correction_pass"][
        "discovery"
    ]
    assert correction_discovery["provider_detail"] == cascade


def test_non_provider_failures_keep_their_state_shape(tmp_path: Path):
    classified = talk_lane.classify_talk_failure(
        "RuntimeError: BOUNDARY_UNREPAIRABLE: nothing provider shaped here"
    )

    assert classified["failure_kind"] == "content_boundary"
    assert "failure_provider_class" not in classified
    assert "failure_provider_status_codes" not in classified


# ---------------------------------------------------------------------------
# 2026-08-10 Ivan #9 第二半：「CPA请求失败的逻辑是积极重试，而不是判候选死」
# ---------------------------------------------------------------------------


def _authority_with_boundary_review(
    tmp_path: Path, candidate_id: str, boundary_review: dict
) -> str:
    """终审面通过、只有边界语义复核 BLOCK 的最小 audit 对。"""

    audit = {
        "schema_version": "final-review-audit.v2",
        "status": "FLAGGED",
        "release_gate": "BLOCK",
        "reason_codes": ["FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED"],
        "reviewed_srt_sha256": "sha256:" + "e" * 64,
        "discovery": {"status": "COMPLETE"},
        "findings": [],
        "validated_finding_count": 0,
        "boundary_semantic_review": boundary_review,
    }
    directory = tmp_path / candidate_id
    directory.mkdir(parents=True, exist_ok=True)
    authority = directory / f"{candidate_id}.chat-authority.json"
    authority.write_text(
        json.dumps({"final_review_audit": audit}, ensure_ascii=False),
        encoding="utf-8",
    )
    (directory / f"{candidate_id}.review-flags.json").write_text(
        json.dumps(audit, ensure_ascii=False), encoding="utf-8"
    )
    return str(authority)


def _classify_boundary(tmp_path: Path, candidate_id: str, review: dict) -> dict:
    return talk_lane.classify_talk_failure(
        "FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED: "
        + _authority_with_boundary_review(tmp_path, candidate_id, review)
    )


def test_boundary_review_transport_failure_waits_instead_of_dying(
    tmp_path: Path,
):
    """provider 打不通 ≠ 边界内容不合格。

    修复前：``BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmCallError`` 落
    ``content_boundary`` + ``failure_recoverable=False``，于是一次 CPA 请求失败
    把 15–55 分钟的 produce 判成内容缺陷，只能等代码波改 fingerprint 才醒。
    """

    classified = _classify_boundary(
        tmp_path,
        "auto_boundary_transport",
        {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "BLOCK",
            "reason_codes": ["BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmCallError"],
        },
    )

    assert classified["failure_kind"] == "provider_transient"
    assert classified["failure_stage"] == "final_review_boundary_semantic"
    assert classified["failure_recoverable"] is True


def test_boundary_review_code_defect_stays_terminal(tmp_path: Path):
    """反向门：确定性代码缺陷不许混进基础设施等待车道。

    ``INFRASTRUCTURE_WAIT_FAILURE_KINDS`` 是无上限定时重试；把 TypeError 之流
    放进去 = 同一 fingerprint 每 tick 空转到天荒地老。
    """

    classified = _classify_boundary(
        tmp_path,
        "auto_boundary_bug",
        {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "BLOCK",
            "reason_codes": ["BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:TypeError"],
        },
    )

    assert classified["failure_kind"] == "content_boundary"
    assert classified["failure_recoverable"] is False


def test_boundary_review_content_verdict_stays_terminal(tmp_path: Path):
    """内容门一个字没放松：真实的边界内容裁决照旧终态。"""

    classified = _classify_boundary(
        tmp_path,
        "auto_boundary_content",
        {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "BLOCK",
            "reason_codes": ["BOUNDARY_SEMANTIC_REVIEW_REQUIRED"],
            "syntax_complete": False,
            "story_closed": False,
        },
    )

    assert classified["failure_kind"] == "content_boundary"
    assert classified["failure_recoverable"] is False


def test_transport_unavailable_reason_allowlist_is_closed():
    """未知异常名一律不算 transport——allowlist 是白名单不是黑名单。"""

    assert (
        pf.transport_unavailable_reason(
            ["BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmCallError"]
        )
        == "BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmCallError"
    )
    assert (
        pf.transport_unavailable_reason(
            ["AUDITOR_UNAVAILABLE:TimeoutExpired"]
        )
        == "AUDITOR_UNAVAILABLE:TimeoutExpired"
    )
    for hostile in (
        ["BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:KeyError"],
        ["BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:AssertionError"],
        ["BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE"],  # 无异常名
        ["BOUNDARY_SEMANTIC_REVIEW_REQUIRED"],
        [],
        None,
        {"status": "BLOCK"},
    ):
        assert pf.transport_unavailable_reason(hostile) is None


def test_infra_retry_delay_escalates_instead_of_reburning_every_15_minutes():
    """固定 15 分钟 + 15–55 分钟的 produce = 故障期间机时全烧在必然的重复失败上。

    复用歌 lane 的同一条指数曲线：间隔变长，但**永不判死**。
    """

    delays = [
        infra_retry_policy.talk_infra_retry_delay_seconds(
            {"talk_transient_retry_count": n}
        )
        for n in range(5)
    ]

    assert delays[0] == runner.SONG_INFRA_RETRY_BASE_SECONDS
    assert delays == sorted(delays)
    assert delays[3] > delays[0]
    assert all(delay <= runner.SONG_INFRA_RETRY_MAX_SECONDS for delay in delays)
    # 上限存在，且是"等更久"而不是"不再等"
    assert (
        infra_retry_policy.talk_infra_retry_delay_seconds(
            {"talk_transient_retry_count": 99}
        )
        == runner.SONG_INFRA_RETRY_MAX_SECONDS
    )


def test_quota_class_starts_one_step_further_out():
    """配额窗口按小时/天计，用 15 分钟去撞一个还要几小时才开的窗口是纯浪费。"""

    service = infra_retry_policy.talk_infra_retry_delay_seconds(
        {"talk_transient_retry_count": 0, "failure_provider_class": pf.SERVICE}
    )
    quota = infra_retry_policy.talk_infra_retry_delay_seconds(
        {"talk_transient_retry_count": 0, "failure_provider_class": pf.QUOTA}
    )

    assert service == runner.SONG_INFRA_RETRY_BASE_SECONDS
    assert quota > service
