"""Package-audit canonicality bindings (content verdict vs auditor identity)."""

from __future__ import annotations


def audit_binding(audit: dict) -> dict:
    """The canonical fields that make an audit replayable, not self-asserted."""

    return {
        key: audit.get(key)
        for key in (
            "schema_version",
            "policy_epoch",
            "policy_fingerprint",
            "auditor_source_sha256",
            "passed",
            "root",
            "audited_inputs",
            "issues",
            "issue_count",
            "blocking_issue_count",
        )
    }


def audit_content_binding(audit: dict) -> dict:
    """The audit's content verdict, minus auditor-identity churn.

    1573 在飞事务案（2026-07-27）：B 站审核窗横跨数小时，期间每次部署都
    改 policy_fingerprint/auditor_source_sha256——冻结审计与现行审计对同
    一批字节给出**逐字相同的判决**却被判过期，恢复永久卡死。现行审计员
    已实跑通过（上一行门），内容判决（inputs/issues/verdict）相等即
    canonical；字节漂移或新 issue 仍然精确拒绝。
    """

    binding = audit_binding(audit)
    binding.pop("policy_fingerprint", None)
    binding.pop("auditor_source_sha256", None)
    return binding
