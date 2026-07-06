#!/usr/bin/env python3
"""Real CPA semantic QA judge: an LLM fills the locked response contract.

Replaces ``run_cpa_semantic_qa.py --mode mock-local`` in production runs.  The
LLM only supplies *judgment* fields (semantic completeness, terminology,
scores, reason codes, summary); every binding field — candidate_id,
request_sha256, artifact paths — is forced from the request artifact so the
hash gate still proves the response answers exactly this request.

Fail-closed: any transport/parse/normalization error exits non-zero, which the
``cpa_semantic_review.py`` wrapper reports and the shadow pipeline turns into
a CPA BLOCK.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.cpa_semantic_qa import (
    CpaSemanticQaResponse,
    load_request_artifact,
    write_cpa_semantic_response_artifact,
)
from src.autoslice.llm_client import LlmCall, LlmCallError, LlmConfig, build_llm_call, extract_json_object

try:  # normal import path (pytest / `python -m`, ROOT on sys.path)
    from scripts.lidousha_glossary_terms import load_glossary_terms
except ImportError:  # run as a top-level script: scripts/ is sys.path[0]
    from lidousha_glossary_terms import load_glossary_terms

JUDGMENT_REASON_CODES = (
    "CPA_SEMANTIC_INCOMPLETE",
    "CONTEXT_DEPENDENCY_HIGH",
    "TERMINOLOGY_QA_FAILED",
    "UNSAFE_UPLOAD_RISK",
    "NOT_INTERESTING",
    "VIEWER_CONTEXT_INCOMPLETE",
)

VIEWER_CONTEXT_MAX_EXPAND_MS = 300_000


def _resolve_terminology(request) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (canon proper nouns, ASR mishearing blacklist) for terminology_ok.

    Prefer what the request carries: ``applied_terms`` plus the metadata
    ``terminology_blacklist`` that scripts/cpa_semantic_review.py fills from the
    glossary.  Fall back to parsing the repo glossary directly so the gate is
    still populated for requests built by other runners.  load_glossary_terms is
    fail-safe, so a missing glossary degrades to ("kmx",)/() rather than raising.
    """

    canon = tuple(str(term) for term in request.terminology.applied_terms if str(term).strip())
    raw_blacklist = request.metadata.get("terminology_blacklist")
    blacklist: tuple[str, ...] = ()
    if isinstance(raw_blacklist, (list, tuple)):
        blacklist = tuple(str(item) for item in raw_blacklist if isinstance(item, str) and item.strip())
    if not canon or not blacklist:
        fallback = load_glossary_terms()
        if not canon:
            canon = fallback.canon
        if not blacklist:
            blacklist = fallback.mishear_blacklist
    return canon, blacklist


def build_judge_prompt(request) -> str:
    canon_terms, blacklist_terms = _resolve_terminology(request)
    terminology_terms = "、".join(canon_terms) or "(无)"
    terminology_blacklist = "、".join(blacklist_terms) or "(无)"
    metadata = dict(request.metadata)
    metadata.pop("terminology_blacklist", None)
    surrounding = metadata.pop("surrounding_context", None) if isinstance(metadata.get("surrounding_context"), dict) else None
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata else "{}"
    if isinstance(surrounding, dict):
        before_text = str(surrounding.get("before_text") or "(无)")
        after_text = str(surrounding.get("after_text") or "(无)")
        window_ms = surrounding.get("window_ms")
        surrounding_block = (
            f"切片前 {window_ms}ms 内的直播原文(不在切片里,观众看不到): {before_text}\n"
            f"切片后 {window_ms}ms 内的直播原文(不在切片里,观众看不到): {after_text}"
        )
    else:
        surrounding_block = "(没有提供切片前后的邻近文本)"
    return f"""你是一个严格的直播切片质量评审(CPA semantic QA)。评审对象是从直播录像里选出的候选切片文本。

候选信息:
- candidate_id: {request.candidate_id}
- room_id: {request.room_id}
- 源区间: {request.source.start_ms}ms - {request.source.end_ms}ms
- 原始文本: {request.candidate_text}
- 术语归一后文本: {request.normalized_text}
- 必须使用的规范术语: {terminology_terms}
- 机器证据 metadata: {metadata_json}

切片外的邻近上下文(仅供你判断切片是否漏了上下文,观众看不到这些):
{surrounding_block}

评审标准(全部要认真判断,不要客气):
1. semantic_complete: 这段内容作为独立切片语义是否完整——开头不是悬空的接续语,结尾有交代/包袱落地/歌唱完,观众不看上下文也能看懂。
2. terminology_ok: 归一后文本里是否已经没有违规术语变体。
   - 规范专名(canon)以上面"必须使用的规范术语"为准。
   - 已知 ASR 误听/错写变体(这些写法是错的,归一后不应再出现): {terminology_blacklist}
   - 逐一检查"术语归一后文本",只要还残留上面任意一个误听变体、且语境确实是在指对应的规范专名(却没有被归一) → terminology_ok=false, 且 reason_codes 必须含 TERMINOLOGY_QA_FAILED。
   - 这些只是"疑似误听"提示:若某个词在语境里明显是它本来的意思、并不是在指表内专名,不要误判,terminology_ok 保持 true。
   - 规范术语表只在内容确实涉及那些对象时才适用;如果内容完全没有涉及任何表内对象,terminology_ok 直接给 true,不要因为"没有出现术语"而扣分。
3. title_hook_score (0-1): 这段内容做成切片对路人观众的吸引力(有趣程度)。0.85 以上=明显有梗/有反差/唱得动人; 0.5 以下=平淡日常。注意有趣不只是"讲了个段子":与弹幕打闹、对屏幕上的东西连续吐槽、绕口令翻车、破防都算。
4. context_dependency_score (0-1): 依赖直播上下文的程度。越低越好, >0.45 说明单独看会困惑。
5. unsafe_upload_risk_score (0-1): 上传风险,只针对这些真实风险:隐私泄露(真实姓名/住址/联系方式/账号金额)、辱骂引战对线、平台违规内容(违法/色情/暴力)、观看的第三方视频画面构成主体内容。
   **频道政策(硬口径): 主播翻唱歌曲不算上传风险**——本频道"豆沙歌"系列长期公开发布翻唱切片(已有多条先例),平台对 VTuber 翻唱亦是常态;绝不因为"演唱了受版权保护的歌曲/翻唱未授权"给高分或 UNSAFE_UPLOAD_RISK。日常唱歌聊天一般 <=0.2。
6. viewer_context_ok(观众视角上下文审查,重点): 假设你是一个没看过直播的观众,只看到这个切片。
   - metadata 里如有 danmaku_context,那是切片时间窗内观众发的真实弹幕(直播画面上滚动可见,观众看得到):判断"弹幕起头"是否已包含在切片内、弹幕互动是否有梗时,以它为准;主播突然接话但对应弹幕在窗内时,观众是能看懂的。
   - 切片里发生的事有没有让你摸不着头脑的部分(在回应什么?在看什么?在接谁的话?)。
   - 对照上面"切片外的邻近上下文":如果切片开头其实在接前面的话头(弹幕、上一个话题、正在看的东西),而观众从切片内部无法合理推测出来,viewer_context_ok=false,并给出扩窗建议:context_expand_before_ms=把这些上下文包进来需要向前扩展的毫秒数(看前文里触发点大概在多久之前),context_expand_after_ms 同理(比如包袱落点在切片结束之后)。
   - 如果观众虽然缺少背景但能从切片内容合理推测出来(比如明显是在看图评论,不需要知道具体哪张图),viewer_context_ok=true。
   - 上下文完整或可推测时 expand 两项都填 0。
7. release_ready: 综合判断是否可以进入发布流程(以上都过关才 true)。viewer_context_ok=false 时 release_ready 必须是 false。

硬规则: 如果 metadata 证明这是完整歌曲切片（例如 content_type_hint/song_candidate 为 song，且 song_complete=true、lyrics_alignment_ready=true，或 full_song_ready=true），不要因为"无聊"、"不像段子"、"没有聊天包袱"给 NOT_INTERESTING / CONTEXT_DEPENDENCY_HIGH / CPA_SEMANTIC_INCOMPLETE / VIEWER_CONTEXT_INCOMPLETE；完整歌本身就是完整内容，按形式证据评审。仍然必须因为不完整歌曲、歌词/字幕证据缺失、术语错误、真实上传风险(见第5条口径;翻唱本身不构成风险)等问题阻断。

reason_codes 只能从这些里选(没有问题就给空数组): {list(JUDGMENT_REASON_CODES)}
required_fixes: 每个 reason_code 对应一条可执行的修复建议(中文)。

只输出一个 JSON 对象,不要任何其他文字,字段:
{{"release_ready": bool, "semantic_complete": bool, "terminology_ok": bool, "title_hook_score": float, "context_dependency_score": float, "unsafe_upload_risk_score": float, "viewer_context_ok": bool, "context_expand_before_ms": int, "context_expand_after_ms": int, "reason_codes": [string], "required_fixes": [string], "summary": "一句话中文评审结论"}}"""


def normalize_judgment(payload: Mapping[str, object]) -> dict[str, object]:
    """Strictly normalize the LLM judgment; raise LlmCallError on bad shape."""

    result: dict[str, object] = {}
    for field in ("release_ready", "semantic_complete", "terminology_ok"):
        value = payload.get(field)
        if not isinstance(value, bool):
            raise LlmCallError(f"judgment field {field} must be a JSON boolean, got {value!r}")
        result[field] = value
    for field in ("title_hook_score", "context_dependency_score", "unsafe_upload_risk_score"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LlmCallError(f"judgment field {field} must be a number, got {value!r}")
        result[field] = min(1.0, max(0.0, float(value)))
    raw_reasons = payload.get("reason_codes")
    if not isinstance(raw_reasons, list):
        raise LlmCallError("reason_codes must be a JSON array")
    result["reason_codes"] = tuple(str(item) for item in raw_reasons if isinstance(item, str) and item)
    raw_fixes = payload.get("required_fixes")
    if not isinstance(raw_fixes, list):
        raise LlmCallError("required_fixes must be a JSON array")
    result["required_fixes"] = tuple(str(item) for item in raw_fixes if isinstance(item, str) and item)
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise LlmCallError("summary must be a non-empty string")
    result["summary"] = summary.strip()

    viewer_context_ok = payload.get("viewer_context_ok")
    if not isinstance(viewer_context_ok, bool):
        raise LlmCallError(f"judgment field viewer_context_ok must be a JSON boolean, got {viewer_context_ok!r}")
    result["viewer_context_ok"] = viewer_context_ok
    for field in ("context_expand_before_ms", "context_expand_after_ms"):
        value = payload.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LlmCallError(f"judgment field {field} must be a number, got {value!r}")
        result[field] = int(min(VIEWER_CONTEXT_MAX_EXPAND_MS, max(0, int(value))))

    # A viewer who cannot follow the clip is a release blocker, whatever the
    # judge put in release_ready — and it must carry its reason code.
    if not viewer_context_ok:
        result["release_ready"] = False
        if "VIEWER_CONTEXT_INCOMPLETE" not in result["reason_codes"]:
            result["reason_codes"] = tuple(result["reason_codes"]) + ("VIEWER_CONTEXT_INCOMPLETE",)
        if not result["required_fixes"]:
            result["required_fixes"] = (
                f"向前扩窗 {result['context_expand_before_ms']}ms / 向后扩窗 {result['context_expand_after_ms']}ms 把上下文触发点包进切片",
            )
    # An LLM that says not-ready but names no reason still has to block loudly.
    if not result["release_ready"] and not result["reason_codes"]:
        result["reason_codes"] = ("CPA_SEMANTIC_INCOMPLETE",)
    return result


def judge_request(request, llm_call: LlmCall, *, provider_label: str, retries: int = 1) -> CpaSemanticQaResponse:
    last_error: Exception | None = None
    for _attempt in range(retries + 1):
        try:
            completion = llm_call(build_judge_prompt(request))
            judgment = normalize_judgment(extract_json_object(completion))
            break
        except LlmCallError as exc:
            last_error = exc
    else:
        raise LlmCallError(f"LLM judge failed after {retries + 1} attempts: {last_error}")
    return CpaSemanticQaResponse(
        candidate_id=request.candidate_id,
        request_sha256=request.canonical_sha256(),
        release_ready=judgment["release_ready"],
        semantic_complete=judgment["semantic_complete"],
        terminology_ok=judgment["terminology_ok"],
        title_hook_score=judgment["title_hook_score"],
        context_dependency_score=judgment["context_dependency_score"],
        unsafe_upload_risk_score=judgment["unsafe_upload_risk_score"],
        reason_codes=judgment["reason_codes"],
        required_fixes=judgment["required_fixes"],
        summary=judgment["summary"],
        response_path=request.response_path,
        request_path=request.request_path,
        provider=provider_label,
        metadata={
            "mode": "llm",
            "provider": provider_label,
            "viewer_context": {
                "viewer_context_ok": judgment["viewer_context_ok"],
                "expand_before_ms": judgment["context_expand_before_ms"],
                "expand_after_ms": judgment["context_expand_after_ms"],
            },
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM-backed CPA semantic QA response writer.")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--transport", choices=("direct", "command"), required=True)
    parser.add_argument("--model", help="Model id for direct transport; also used as the provider label.")
    parser.add_argument("--api-base", help="OpenAI-compatible base URL, e.g. https://api.groq.com/openai/v1")
    parser.add_argument("--api-key-env", default="CPA_API_KEY")
    parser.add_argument("--llm-command", help="Command template with {prompt_file} {completion_file} for command transport.")
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    args = parser.parse_args(argv)

    config = LlmConfig(
        transport=args.transport,
        model=args.model,
        api_base=args.api_base,
        api_key_env=args.api_key_env,
        command_template=args.llm_command,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        llm_call = build_llm_call(config)
        request = load_request_artifact(args.request)
        provider_label = f"llm:{args.model or 'command-bridge'}"
        response = judge_request(request, llm_call, provider_label=provider_label, retries=max(0, args.retries))
    except (LlmCallError, ValueError) as exc:
        sys.stderr.write(f"CPA_LLM_JUDGE_FAILED: {exc}\n")
        return 3
    write_cpa_semantic_response_artifact(response, args.response)
    print(f"cpa llm judge ok: release_ready={response.release_ready} reasons={list(response.reason_codes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
