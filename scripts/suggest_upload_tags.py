#!/usr/bin/env python3
"""Suggest B站 upload tags for delivered slices (快速原型,).

当前上传链路 (部署宿主的 do_upload.sh) 的 tag 是写死的 6 个基础位:
    虚拟UP主,VTuber,直播切片,李豆沙,虚拟主播,VUP
本原型为每条切片自动补充内容相关 tag, 两层来源:

* Layer A 确定性专名层 — 所选 profile 的 ``upload_tag_policy`` 资产中
  手工整理的「口播表面形式 → 可搜索 tag」规则表（可由 glossary/roster
  和频道编辑裁定构建）
  (侄女→侄女/百合/女同, 142→伊索尔, lmsm→礼墨Sumi, 大N老师→南町, 梦限大→
  梦限大/日文名/BanG Dream …)。专名只走这一层, 绝不让 LLM 发明专名。
* Layer B LLM 内容层 — 经 CPA (llm_via_cpa.sh, 与标题/精听同一链路) 从标题+
  字幕全文提出 3~6 个通用内容词 (可爱/撒娇/破防/吐槽…)。输出过校验:
  长度、去重、且不得撞已知专名表面形式 (防幻觉专名混入)。

合并: 基础位 > 人工裁定 > 专名/IP(标题命中优先, 次数排序) > 内容；固定 4 个基础位+
最多 6 个 dynamic 位，默认封顶 10 个，单 tag ≤20 字符、无逗号。

维护者 审查拍板的口径(已固化):
* 基础位砍成 4 个(李豆沙/虚拟主播/虚拟UP主/直播切片), 其余给内容位。
* 专名 tag 只出可搜索正主名(南町), 梗形态(大N老师/豆町)只作触发面。
* 内容词必须"贴内容 × 足够通用可搜"; 过专一没人搜的词(彩排/宠粉/玩梗/
  热情邀约/初次登场/脑补剧情/粉丝互动/线下合照 类)硬毙。
* 坏女人/宿敌恋人 这类半梗半内容词放行。
* tag 必须按最终成品字幕出——字幕修复(换源)后 tag 要重算重审; 对已知
  字幕误听的临时裁定走 batch 条目的 suppress_tags/add_tags 人工通道。

原型只产出建议(JSON + markdown 审查表), 不改上传链路; 接入 authorized_upload/
do_upload 是下一步。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.llm_client import (  # noqa: E402
    LlmCallError,
    LlmConfig,
    build_llm_call,
    extract_json_object,
)

# Channel names, proper-noun mappings, and prompt policy are selected through
# the active profile. Keep these compatibility constants so existing callers
# do not need to know where the policy bytes live.
from src.autoslice.upload_tag_policy import (  # noqa: E402
    TermRule,
    load_selected_upload_tag_policy,
)

_UPLOAD_TAG_POLICY = load_selected_upload_tag_policy()
BASE_TAGS = _UPLOAD_TAG_POLICY.base_tags
MAX_TAGS_DEFAULT = _UPLOAD_TAG_POLICY.max_tags_default
MAX_DYNAMIC_TAGS = _UPLOAD_TAG_POLICY.max_dynamic_tags
MAX_TAG_CHARS = _UPLOAD_TAG_POLICY.max_tag_chars
TERM_RULES = _UPLOAD_TAG_POLICY.term_rules
IMPORTANT_CONTENT_IP_RULES = _UPLOAD_TAG_POLICY.important_content_ips
_THEME_ALLOWED = set(_UPLOAD_TAG_POLICY.theme_allowed)
_BANNED_CONTENT_TAGS = set(_UPLOAD_TAG_POLICY.banned_content_tags)
CONTENT_PROMPT = _UPLOAD_TAG_POLICY.content_prompt_template

CPA_COMMAND = f"bash {ROOT}/scripts/llm_via_cpa.sh {{prompt_file}} {{completion_file}} 'gpt-5.6-sol gpt-5.5 gpt-5.4' low"

_KNOWN_PROPER_SURFACES: tuple[str, ...] = tuple(
    sorted(
        {
            surface
            for rule in (*TERM_RULES, *IMPORTANT_CONTENT_IP_RULES)
            for surface in rule.tags
        }
        | set(_UPLOAD_TAG_POLICY.known_proper_surfaces_extra),
        key=len,
        reverse=True,
    )
)


@dataclass
class ProperHit:
    tag: str
    rules: list[str] = field(default_factory=list)
    evidence: dict[str, int] = field(default_factory=dict)
    hits: int = 0
    title_hit: bool = False
    notes: list[str] = field(default_factory=list)


def parse_srt_text(srt_path: Path) -> str:
    lines: list[str] = []
    for raw in srt_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        lines.append(line)
    return "\n".join(lines)


def scan_proper_nouns(
    title: str,
    body: str,
    *,
    term_rules: tuple[TermRule, ...] | None = None,
    important_content_ip_rules: tuple[TermRule, ...] | None = None,
) -> list[ProperHit]:
    # 专名常被字幕跨 cue 截断(实案: "梦\n限大"), 逐行文本会漏; 额外在去空白
    # 连体文本上匹配一次, 每个 pattern 取两种视图的最大命中数(不相加防重复计数)。
    condensed_body = re.sub(r"[\s，。？！—…·]+", "", body)
    merged: dict[str, ProperHit] = {}
    selected_term_rules = TERM_RULES if term_rules is None else term_rules
    selected_ip_rules = (
        IMPORTANT_CONTENT_IP_RULES
        if important_content_ip_rules is None
        else important_content_ip_rules
    )
    for rule in (*selected_term_rules, *selected_ip_rules):
        evidence: dict[str, int] = {}
        title_hit = False
        for pattern in rule.patterns:
            regex = re.compile(pattern, re.IGNORECASE)
            body_hits = regex.findall(body)
            condensed_hits = regex.findall(condensed_body)
            title_hits = regex.findall(title)
            count = max(len(body_hits), len(condensed_hits)) + len(title_hits)
            if count:
                # findall with lookarounds returns matched strings; use pattern as surface key
                surface = next((h for h in (*body_hits, *condensed_hits, *title_hits) if isinstance(h, str) and h), pattern)
                evidence[surface] = evidence.get(surface, 0) + count
                title_hit = title_hit or bool(title_hits)
        total = sum(evidence.values())
        if total < rule.min_hits:
            continue
        for tag in rule.tags:
            hit = merged.setdefault(tag, ProperHit(tag=tag))
            hit.rules.append(rule.name)
            for surface, count in evidence.items():
                hit.evidence[surface] = hit.evidence.get(surface, 0) + count
            hit.hits += total
            hit.title_hit = hit.title_hit or title_hit
            if rule.note and rule.note not in hit.notes:
                hit.notes.append(rule.note)
    ranked = sorted(merged.values(), key=lambda h: (h.title_hit, h.hits), reverse=True)
    return ranked


def _valid_tag(tag: str) -> bool:
    if not tag or len(tag) > MAX_TAG_CHARS:
        return False
    return not any(ch in tag for ch in ",，\n\t")


def llm_content_tags(
    title: str, srt_text: str, existing: list[str], timeout: float, llm_call=None
) -> tuple[list[dict], list[str]]:
    """Returns (content_tags, warnings); LLM failure degrades to ([], [reason])."""
    if not srt_text.strip():
        srt_text = "（本条没有字幕存档，只有标题。只在标题本身能支撑时出词，出不了就给空列表。）"
    prompt = CONTENT_PROMPT.format(existing_tags="、".join(existing), title=title, srt_text=srt_text)
    call = llm_call or build_llm_call(
        LlmConfig(transport="command", command_template=CPA_COMMAND, timeout_seconds=timeout)
    )
    try:
        completion = call(prompt)
        payload = extract_json_object(completion)
    except (LlmCallError, ValueError) as exc:
        return [], [f"LLM内容层失败(降级为仅专名): {exc}"]
    warnings: list[str] = []
    accepted: list[dict] = []
    seen = {tag.casefold() for tag in existing}
    for item in payload.get("tags") or []:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or "").strip().strip("#＃")
        why = str(item.get("why") or "").strip()
        if not _valid_tag(tag) or len(tag) > 10:
            warnings.append(f"弃(格式/长度): {tag!r}")
            continue
        if tag in _BANNED_CONTENT_TAGS:
            warnings.append(f"弃(维护者硬毙: 过专一没人搜): {tag!r}")
            continue
        if tag.casefold() in seen:
            continue
        if tag not in _THEME_ALLOWED and any(
            surface.casefold() in tag.casefold() for surface in _KNOWN_PROPER_SURFACES if len(surface) >= 2
        ):
            warnings.append(f"弃(疑似专名, 专名只走确定层): {tag!r}")
            continue
        seen.add(tag.casefold())
        accepted.append({"tag": tag, "why": why})
        if len(accepted) >= 6:
            break
    return accepted, warnings


def merge_tags(
    base: tuple[str, ...],
    proper: list[ProperHit],
    content: list[dict],
    max_total: int,
    *,
    max_dynamic: int = MAX_DYNAMIC_TAGS,
) -> list[str]:
    final: list[str] = []
    seen: set[str] = set()
    effective_limit = min(max_total, len(base) + max_dynamic)
    for tag in list(base) + [h.tag for h in proper] + [c["tag"] for c in content]:
        key = tag.casefold()
        if key in seen or not _valid_tag(tag):
            continue
        seen.add(key)
        final.append(tag)
        if len(final) >= effective_limit:
            break
    return final


def suggest_for_slice(
    slice_id: str,
    title: str,
    srt_path: Path | None,
    *,
    bvid: str = "",
    use_llm: bool = True,
    max_tags: int = MAX_TAGS_DEFAULT,
    timeout: float = 240.0,
    suppress_tags: dict[str, str] | None = None,
    add_tags: dict[str, str] | None = None,
    important_content_ip_rules: tuple[TermRule, ...] | None = None,
    llm_call=None,
) -> dict:
    """suppress_tags/add_tags: 人工裁定通道 {tag: 理由}。

    用于成品字幕尚未修复、但 维护者 已裁定事实的场合(例: 03 全片"安晚"=
    大N老师误听且安晚未出场 → suppress 安晚awa + add 南町)。裁定记录进
    输出; 字幕修复换源后应去掉裁定重算。
    """
    suppress_tags = suppress_tags or {}
    add_tags = add_tags or {}
    # 早期已发布切片可能没有字幕存档(成品字幕只烧在视频里) — title-only 模式:
    # 专名层只扫标题, LLM 层被明确告知没有字幕、宁缺毋滥。
    srt_text = parse_srt_text(srt_path) if srt_path else ""
    active_ip_rules = (
        IMPORTANT_CONTENT_IP_RULES
        if important_content_ip_rules is None
        else important_content_ip_rules
    )
    proper = scan_proper_nouns(
        title,
        srt_text,
        important_content_ip_rules=active_ip_rules,
    )
    overridden = [h.tag for h in proper if h.tag in suppress_tags]
    proper = [h for h in proper if h.tag not in suppress_tags]
    # 人工裁定的补充 tag 排在专名最前(人工判断优先于统计排序)。
    proper = [
        ProperHit(tag=tag, rules=["维护者-override"], notes=[reason], title_hit=True)
        for tag, reason in add_tags.items()
        if tag not in {h.tag for h in proper}
    ] + proper
    existing = list(BASE_TAGS) + [h.tag for h in proper]
    content: list[dict] = []
    warnings: list[str] = []
    if use_llm:
        content, warnings = llm_content_tags(title, srt_text, existing, timeout, llm_call=llm_call)
    for tag in overridden:
        warnings.append(f"人工裁定移除: {tag!r} — {suppress_tags[tag]}")
    final = merge_tags(BASE_TAGS, proper, content, max_tags)
    return {
        "id": slice_id,
        "bvid": bvid,
        "title": title,
        "srt": str(srt_path) if srt_path else "",
        "base_tags": list(BASE_TAGS),
        "proper_noun_tags": [
            {
                "tag": h.tag,
                "rules": h.rules,
                "hits": h.hits,
                "title_hit": h.title_hit,
                "evidence": h.evidence,
                "notes": h.notes,
            }
            for h in proper
        ],
        "important_content_ips": [
            h.tag
            for h in proper
            if any(
                rule_name in {rule.name for rule in active_ip_rules}
                for rule_name in h.rules
            )
        ],
        "content_tags": content,
        "warnings": warnings,
        "overrides": {"suppressed": suppress_tags, "added": add_tags},
        "final_tags": final,
        "final_tag_line": ",".join(final),
        "overflow_tags": [h.tag for h in proper if h.tag not in final]
        + [c["tag"] for c in content if c["tag"] not in final],
    }


ENGINE_VERSION = "suggest-upload-tags.v2"


def generate_upload_tags(
    title: str,
    srt_path: Path | None,
    *,
    use_llm: bool = True,
    llm_call=None,
    max_tags: int = MAX_TAGS_DEFAULT,
    timeout: float = 240.0,
) -> dict:
    """流水线入口(produce_slice_package / apply_subtitle_correction 用)。

    针对成品标题+成品字幕生成 record.json 可存的 tag 块。**fail-safe, 永不
    raise**: tag 是增强项, 任何失败都不许阻塞交付 — 失败返回 status=FAILED
    并带原因, 上传侧回退基础位。status:
      OK        — 专名层+LLM 内容层齐全
      OK_NO_LLM — LLM 内容层失败, 仅专名层(降级可用)
      FAILED    — 引擎级失败, final_tags 为空
    """
    try:
        result = suggest_for_slice(
            "package", title, srt_path, use_llm=use_llm, max_tags=max_tags,
            timeout=timeout, llm_call=llm_call,
        )
        degraded = any("LLM内容层失败" in w for w in result["warnings"])
        return {
            "engine": ENGINE_VERSION,
            "status": "OK_NO_LLM" if (use_llm and degraded) else "OK",
            "final_tags": result["final_tags"],
            "final_tag_line": result["final_tag_line"],
            "proper_noun_tags": result["proper_noun_tags"],
            "important_content_ips": result["important_content_ips"],
            "content_tags": result["content_tags"],
            "warnings": result["warnings"],
        }
    except Exception as exc:  # noqa: BLE001 — 交付路径上的兜底边界
        return {
            "engine": ENGINE_VERSION,
            "status": "FAILED",
            "error": f"{type(exc).__name__}: {exc}",
            "final_tags": [],
            "final_tag_line": "",
        }


def render_markdown(results: list[dict]) -> str:
    lines = [
        "# 切片 tag 建议 — 维护者 2026-07-13 口径",
        "",
        f"- 基础位(维护者 拍板 4 个; free:do_upload.sh 仍是旧 6 位, 接入时改): `{','.join(BASE_TAGS)}`",
        "- 专名层=确定性规则(glossary/roster, 只出可搜索正主名), 内容层=CPA LLM(贴内容×通用可搜); 专名绝不由 LLM 产出。",
        f"- 封顶 {MAX_TAGS_DEFAULT} 个: 基础位占 {len(BASE_TAGS)}, 内容位 {MAX_TAGS_DEFAULT - len(BASE_TAGS)} 个。",
        "- tag 以最终成品字幕为准; 字幕待修条目的人工裁定见各条「人工裁定」标注, 换源后去裁定重算。",
        "",
    ]
    for r in results:
        lines.append(f"## {r['id']} {r['bvid']} {r['title']}")
        lines.append("")
        if r["proper_noun_tags"]:
            lines.append("**专名层**:")
            for h in r["proper_noun_tags"]:
                ev = " ".join(f"{s}×{c}" for s, c in h["evidence"].items())
                extra = f"（{'; '.join(h['notes'])}）" if h["notes"] else ""
                star = "★标题命中 " if h["title_hit"] else ""
                lines.append(f"- `{h['tag']}` — {star}{ev}{extra}")
        else:
            lines.append("**专名层**: 无命中")
        lines.append("")
        if r["content_tags"]:
            lines.append("**内容层(LLM)**:")
            for c in r["content_tags"]:
                lines.append(f"- `{c['tag']}` — {c['why']}")
        if r["warnings"]:
            lines.append("")
            lines.append("**告警**: " + "; ".join(r["warnings"]))
        lines.append("")
        lines.append(f"**合并后(≤{MAX_TAGS_DEFAULT})**: `{r['final_tag_line']}`")
        if r["overflow_tags"]:
            lines.append(f"　被挤掉: {', '.join(r['overflow_tags'])}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True, help="JSON: [{id,title,srt,bvid?}, ...]")
    parser.add_argument("--out-json", default="")
    parser.add_argument("--out-md", default="")
    parser.add_argument("--no-llm", action="store_true", help="仅专名层(离线/测试)")
    parser.add_argument("--max-tags", type=int, default=MAX_TAGS_DEFAULT)
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()

    entries = json.loads(Path(args.batch).read_text(encoding="utf-8"))
    results = []
    for entry in entries:
        result = suggest_for_slice(
            str(entry["id"]),
            entry["title"],
            Path(entry["srt"]) if entry.get("srt") else None,
            bvid=entry.get("bvid", ""),
            use_llm=not args.no_llm,
            max_tags=args.max_tags,
            timeout=args.timeout,
            suppress_tags=entry.get("suppress_tags"),
            add_tags=entry.get("add_tags"),
        )
        results.append(result)
        print(f"[{result['id']}] {result['final_tag_line']}", file=sys.stderr)
    if args.out_json:
        Path(args.out_json).write_text(
            json.dumps(results, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    markdown = render_markdown(results)
    if args.out_md:
        Path(args.out_md).write_text(markdown + "\n", encoding="utf-8")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
