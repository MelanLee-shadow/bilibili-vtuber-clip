# 2026-06-30 CPA semantic QA JSON lane

> **状态更新 (2026-07-03)**：真实 CPA judge 已落地——`scripts/cpa_semantic_qa_llm.py --transport direct --model gpt-5.4-mini --api-base $CPA_BASE_URL --api-key-env CPA_API_KEY`。按 Ivan 指令，工作流/e2e 测试也必须走真实 CPA；本文的 mock-local wrapper 仅保留给 pytest 单测。已验证的完整命令见 `docs/spark/2026-06-30-future-live-e2e-runbook.md` § "Canonical validated song e2e command"。

## 目标

为 unattended 模式定义脚本可调用、fail-closed、可落盘的 CPA semantic QA JSON 协议，并把结果接到 `ReviewEvidence.checks` / `metadata`。

本 lane **只实现本地 mock wrapper**，不发真实外部请求。

## 现有仓库习惯

- `source_context_planner.py` / `source_context_executor.py` 已采用 `schema_version` + artifact JSON 的做法。
- `review_evidence.py` 用 `checks` / `metadata` 作为附加机器校验入口。
- `auto_review.py` 的 gate 明确要求 fail-closed reason codes。
- 仓库内尚无可复用的 CPA 调用封装；已有 provider 轨迹主要是 `agy` manifest，CPA 目前只在文档/spec 中出现。

## Request schema

```json
{
  "schema_version": "cpa-semantic-review-request.v1",
  "candidate_id": "clip-cpa",
  "room_id": "22966160",
  "source": {
    "video_path": ".../source.mp4",
    "srt_path": ".../source.srt",
    "start_ms": 1000,
    "end_ms": 6000,
    "source_cues_path": ".../source-cues.json",
    "review_evidence_path": ".../clip.evidence.json"
  },
  "candidate_text": "原始候选文本",
  "normalized_text": "术语归一化后的 QA 文本",
  "terminology": {
    "schema_version": "lidousha-terminology.v1",
    "applied_terms": ["kmx"],
    "evidence_paths": ["lidousha/...manual-edited.zh.srt"]
  },
  "checks_requested": [
    "semantic_completeness",
    "context_dependency",
    "terminology_correctness",
    "title_hook_quality",
    "unsafe_upload_risk"
  ],
  "artifact_paths": {
    "request_path": ".../cpa.request.json",
    "response_path": ".../cpa.response.json"
  },
  "metadata": {}
}
```

### Request fail-closed

- `schema_version` 不匹配：`CPA_SEMANTIC_QA_REQUEST_SCHEMA_MISMATCH`
- `response_path` 缺失：`CPA_SEMANTIC_QA_REQUEST_RESPONSE_PATH_MISSING`
- `source.start_ms/end_ms` 非法：`CPA_SEMANTIC_QA_REQUEST_RANGE_INVALID`
- `checks_requested` 缺失或未知：`CPA_SEMANTIC_QA_REQUEST_CHECKS_MISSING` / `CPA_SEMANTIC_QA_REQUEST_CHECK_UNKNOWN`

## Response schema

```json
{
  "schema_version": "cpa-semantic-review-response.v1",
  "candidate_id": "clip-cpa",
  "request_sha256": "sha256:...",
  "release_ready": true,
  "semantic_complete": true,
  "terminology_ok": true,
  "title_hook_score": 0.86,
  "context_dependency_score": 0.18,
  "unsafe_upload_risk_score": 0.10,
  "reason_codes": [],
  "required_fixes": [],
  "provider": {
    "name": "mock-local-cpa",
    "request_id": null
  },
  "artifact_paths": {
    "request_path": ".../cpa.request.json",
    "response_path": ".../cpa.response.json"
  },
  "evidence": {
    "summary": "mock-local CPA semantic QA pass",
    "terminology_findings": [],
    "semantic_findings": []
  },
  "metadata": {
    "mode": "mock-local"
  }
}
```

### Response fail-closed

- 文件缺失：`CPA_SEMANTIC_QA_MISSING`
- JSON 不可解析：`CPA_SEMANTIC_QA_INVALID_JSON`
- schema/candidate/request hash/path 任一不匹配：直接 BLOCK
- 分数字段必须是 `0.0..1.0`
- `release_ready=false` 时，如果 response 没给充分 reason/fixes，会派生：
  - `CPA_RELEASE_NOT_READY`
  - `CPA_REASON_CODES_MISSING`
  - `CPA_REQUIRED_FIXES_MISSING`

## ReviewEvidence 接入

评估后统一附加一条检查：

```json
{
  "code": "CPA_SEMANTIC_QA",
  "pass": false,
  "severity": "BLOCK",
  "reason_codes": ["CPA_SEMANTIC_INCOMPLETE"],
  "evidence": {
    "request_path": ".../cpa.request.json",
    "response_path": ".../cpa.response.json",
    "request_sha256": "sha256:..."
  }
}
```

并把完整评估摘要放到：

- `ReviewEvidence.metadata["cpa_semantic_qa"]`
- `ReviewEvidence.evidence_gaps += reason_codes`

## 本地 wrapper

CLI：

```bash
python3 scripts/run_cpa_semantic_qa.py \
  --request /path/to/cpa.request.json \
  --response /path/to/cpa.response.json
```

当前仅支持：

- `--mode mock-local`
- 离线规则化 mock 生成 response
- 不访问外部网络，不消费付费 provider

后续若接真 CPA，必须保留相同 request/response artifact contract，且继续 fail-closed。
