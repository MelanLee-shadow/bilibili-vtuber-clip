#!/usr/bin/env python3
"""Suggest B站 upload tags for delivered slices (快速原型, 2026-07-13).

当前上传链路 (free:do_upload.sh) 的 tag 是写死的 6 个基础位:
    虚拟UP主,VTuber,直播切片,李豆沙,虚拟主播,VUP
本原型为每条切片自动补充内容相关 tag, 两层来源:

* Layer A 确定性专名层 — 手工整理的「口播表面形式 → 可搜索 tag」规则表,
  来源 assets/lidousha/glossary.txt + psplive_roster.v1.md + Ivan 点名映射
  (侄女→侄女/百合/女同, 142→伊索尔, lmsm→礼墨Sumi, 大N老师→南町, 梦限大→
  梦限大/日文名/BanG Dream …)。专名只走这一层, 绝不让 LLM 发明专名。
* Layer B LLM 内容层 — 经 CPA (llm_via_cpa.sh, 与标题/精听同一链路) 从标题+
  字幕全文提出 3~6 个通用内容词 (可爱/撒娇/破防/吐槽…)。输出过校验:
  长度、去重、且不得撞已知专名表面形式 (防幻觉专名混入)。

合并: 基础位 > 人工裁定 > 专名(标题命中优先, 次数排序) > 内容, 默认封顶 12 个
(2026-07-13 编辑模式实测 12 个提交+读回成功), 单 tag ≤20 字符、无逗号。

Ivan 2026-07-13 审查拍板的口径(已固化):
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

# Ivan 2026-07-13 拍板: 砍掉 VUP(与虚拟UP主全重复)/VTuber(与虚拟主播近重复),
# 4 个基础位, 其余内容位(上限实测12)。上传侧无 tags 时的回退在 free_do_upload.sh。
BASE_TAGS = ("李豆沙", "虚拟主播", "虚拟UP主", "直播切片")
MAX_TAGS_DEFAULT = 12  # 2026-07-13 实测: BV1EQNk6KErE 12 tag 编辑提交+读回全成功
MAX_TAG_CHARS = 20  # B站单 tag 长度上限

CPA_COMMAND = f"bash {ROOT}/scripts/llm_via_cpa.sh {{prompt_file}} {{completion_file}} 'gpt-5.6-sol gpt-5.5 gpt-5.4' medium"


@dataclass(frozen=True)
class TermRule:
    """One deterministic mention→tags rule.

    patterns are regexes matched over 标题+字幕全文 (IGNORECASE); the rule
    fires when total hits >= min_hits.  Tags are the B站-searchable canonical
    forms, NOT necessarily the subtitle canonical spelling (e.g. subtitle
    writes 142 but the searchable tag is the vtuber's name 伊索尔).
    """

    name: str
    patterns: tuple[str, ...]
    tags: tuple[str, ...]
    min_hits: int = 1
    note: str = ""


# 每条规则的 note 记录来源/口径, 审查表会展示证据 (命中表面形式 x 次数)。
TERM_RULES: tuple[TermRule, ...] = (
    # --- Ivan 点名的映射 ---
    TermRule(
        "zhinv",
        ("侄女", "直女"),
        ("侄女", "百合", "女同"),
        note="Ivan: 提到侄女→侄女/百合/女同; 铁律: 字幕中「直女」永远是「侄女」误听, 同样触发",
    ),
    TermRule(
        "mengxianda",
        ("梦限大", "夢限大", "みゅーたいぷ", "MewType"),
        ("梦限大", "夢限大みゅーたいぷ", "BanG Dream", "邦多利"),
        note="glossary: BanG Dream 企划 梦限大MewType",
    ),
    TermRule("bangdream", ("邦多利", r"BanG\s*Dream", "バンドリ"), ("BanG Dream", "邦多利")),
    TermRule("mujica", (r"Mujica",), ("Ave Mujica", "BanG Dream", "邦多利"), note="glossary: Mujica 与梦限大是不同实体, tag 可并存"),
    TermRule("popipa", (r"Popipa", "ポピパ"), ("Poppin'Party", "BanG Dream", "邦多利")),
    TermRule("bushiroad", ("武士道",), ("武士道",), note="Bushiroad 公司, 邦多利语境"),
    TermRule("mygo", (r"MyGO",), ("MyGO!!!!!", "BanG Dream", "邦多利")),
    TermRule("takamatsu_tomori", ("高松灯",), ("高松灯", "MyGO!!!!!", "BanG Dream", "邦多利")),
    TermRule("rana", ("要乐奈",), ("要乐奈", "MyGO!!!!!", "BanG Dream", "邦多利")),
    TermRule("taki", ("椎名立希", "立希"), ("椎名立希", "MyGO!!!!!", "BanG Dream", "邦多利")),
    TermRule("sakiko", ("祥子",), ("丰川祥子", "BanG Dream", "邦多利"), note="祥子=丰川祥子(待Ivan确认口径)"),
    TermRule("142", (r"(?<!\d)142(?!\d)", "伊索尔", "一四二", "幺四二"), ("伊索尔",), note="Ivan: 142→伊索尔"),
    TermRule("limo", ("礼墨", "lmsm", "Sumi"), ("礼墨Sumi",), note="Ivan: lmsm→礼墨Sumi"),
    # Ivan 2026-07-13: 专名 tag 只出可搜索的正主名, 不出梗形态(大N老师/豆町只作触发面)。
    TermRule("nanmachi", ("南町", "大N老师", "大N"), ("南町",), note="Ivan: 大N老师→南町, 只出正主名"),
    TermRule("douting_cp", ("豆町",), ("南町", "百合"), note="CP名豆町只作触发面, 出南町+百合"),
    # --- PSPLive roster (psplive_roster.v1.md) ---
    TermRule("anwan", ("安晚", "awawa", r"(?<![A-Za-z])awa(?![A-Za-z])"), ("安晚awa",)),
    TermRule("xingxi", ("星汐", "七宝", "小七", "邪哥", "香香烧烤", "山猪王", "小山猪", "xxsk"), ("星汐Seki",)),
    TermRule("baishenyao", ("白神遥", "小海豹", "豹豹", "豹总", r"(?<!\d)5835(?!\d)", "五八三十五"), ("白神遥",)),
    TermRule("dongaili", ("东爱璃", "狍子", "大璃"), ("东爱璃",)),
    TermRule("wuqian", (r"(?<![毫从并绝])无前",), ("无前",), min_hits=2, note="短词易误命中, 要求≥2次"),
    TermRule("byz_rei", ("病院坂",), ("病院坂Rei",)),
    TermRule("qiulinzi", ("秋凛子",), ("秋凛子",)),
    TermRule("ayana", ("绫奈奈奈",), ("绫奈奈奈",)),
    TermRule("miya", ("星之谷米娅",), ("星之谷米娅",)),
    TermRule("yizhi", ("亦枝YY", "亦枝"), ("亦枝YY",)),
    TermRule("beiyouxiang", ("北柚香",), ("北柚香",)),
    TermRule("shengge", ("笙歌",), ("笙歌",)),
    TermRule("taomuq", ("桃姆Q",), ("桃姆Q",)),
    TermRule("hongxiaoyin", ("红晓音",), ("红晓音",)),
    TermRule("cantony", ("残Tony", "残托尼"), ("残Tony",)),
    TermRule("psplive", ("psplive", "披萨盘", r"Project\s*SP", r"(?<![A-Za-z])P-SP(?![A-Za-z])"), ("PSPLive",)),
    # --- 团体/活动/作品/场景 ---
    TermRule("snh48", ("河粉", "塞纳河", "SNH48", "左婧媛"), ("SNH48",), note="Ivan: 河粉=SNH48粉丝"),
    TermRule("bw", (r"(?<![A-Za-z])BW(?![A-Za-z0-9])", "BilibiliWorld", "哔哩哔哩世界"), ("BilibiliWorld",)),
    TermRule("lycoris", ("露蒂丝", "lycoris", "莉可丽丝"), ("莉可丽丝",)),
    TermRule("botan", ("上伊那牡丹",), ("上伊那牡丹", "百合"), note="glossary: 她聊的百合作品"),
    TermRule(
        "yuri_signal",
        ("百合", "百破图", "百乃工", "宿敌恋人", r"女同(?!\s*[事学胞])"),
        ("百合", "女同"),
        note="百合信号词; 女同事/女同学不触发",
    ),
    TermRule("xiantong", ("仙童数学",), ("仙童数学",)),
    TermRule("changsha", ("长沙话", "湖南话", "塑普"), ("长沙话",)),
    TermRule("yinghuochong", ("萤火虫",), ("萤火虫漫展",), note="05实测: 3D线下见面=萤火虫live; tag命名口径待Ivan定"),
)

# LLM 内容层禁止产出专名 — 已知专名表面形式黑名单 (校验 LLM 输出用)。
_KNOWN_PROPER_SURFACES: tuple[str, ...] = tuple(
    sorted(
        {
            surface
            for rule in TERM_RULES
            for surface in rule.tags
        }
        | {"kmx", "沙豆李", "shadowlee", "Ado", "小室", "奶油苏打", "恋青", "恋死", "十麻乃"},
        key=len,
        reverse=True,
    )
)
# 主题词白名单: 这些词形式上是"圈层词"但属于内容主题, LLM 允许产出。
_THEME_ALLOWED = {"百合", "女同", "磕糖", "长沙话", "熊猫"}

# Ivan 2026-07-13 硬毙词: 贴内容但"没人会搜"的过专一描述词。tag 必须既贴合
# 内容又是观众真的会搜/点的通用入口词; 这类词即使 LLM 再产出也过滤。
_BANNED_CONTENT_TAGS = {
    "彩排", "宠粉", "玩梗", "热情邀约", "初次登场", "脑补剧情", "粉丝互动", "线下合照",
    "暴力女",  # Ivan 2026-07-13 二轮点名
}

CONTENT_PROMPT = """你在为B站虚拟主播「李豆沙」的直播切片选投稿标签(tag)。
李豆沙: B站虚拟主播, 虚拟熊猫少女(白发+熊猫耳), 湖南长沙人会飙长沙话; 温柔声线唱歌+高能杂谈双修, 容易破防、一本正经犯傻、嘴快爱吐槽、也有温柔哄睡面。

下面是一条切片的标题和完整字幕。请提出 3~6 个「内容标签」: 描述这条切片的情绪/行为/场景/话题的词。

最重要的一条(Ivan 口径): 标签必须**既贴合这条切片的内容, 又足够通用**——是B站观众真的会在搜索框里搜、或看到会点的现成入口词。太专一于本条内容的描述词没人会搜, 一律不要。
- 好的例子: 可爱 撒娇 嘴硬 破防 吐槽 社死 名场面 搞笑 沙雕 反差萌 姨母笑 治愈 温柔 唱歌 跳舞 3D 漫展 百合 磕CP 磕糖 方言 长沙话 坏女人 宿敌恋人
- 坏的例子(全部是"贴内容但没人搜"的过专一词, 不要出这类): 彩排 宠粉 玩梗 热情邀约 初次登场 脑补剧情 粉丝互动 线下合照 角色分析 催更续写 动漫杂谈

其余硬性规则:
- 禁止输出任何人名、角色名、作品名、企划名、团体名、活动名(专名由另一套确定性规则处理, 你绝对不要出)。
- 每个标签2~6个字, 最长不超过10个字符, 不带标点。
- 不要与这些已定标签重复: {existing_tags}
- 每个标签配一句依据(引用切片里的具体内容)。宁缺毋滥, 达不到通用度就少出。

只输出严格 JSON: {{"tags": [{{"tag": "...", "why": "..."}}]}}

标题: {title}

字幕全文:
{srt_text}
"""


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


def scan_proper_nouns(title: str, body: str) -> list[ProperHit]:
    # 专名常被字幕跨 cue 截断(实案: "梦\n限大"), 逐行文本会漏; 额外在去空白
    # 连体文本上匹配一次, 每个 pattern 取两种视图的最大命中数(不相加防重复计数)。
    condensed_body = re.sub(r"[\s，。？！—…·]+", "", body)
    merged: dict[str, ProperHit] = {}
    for rule in TERM_RULES:
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


def llm_content_tags(title: str, srt_text: str, existing: list[str], timeout: float) -> tuple[list[dict], list[str]]:
    """Returns (content_tags, warnings); LLM failure degrades to ([], [reason])."""
    prompt = CONTENT_PROMPT.format(existing_tags="、".join(existing), title=title, srt_text=srt_text)
    call = build_llm_call(LlmConfig(transport="command", command_template=CPA_COMMAND, timeout_seconds=timeout))
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
            warnings.append(f"弃(Ivan硬毙: 过专一没人搜): {tag!r}")
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


def merge_tags(base: tuple[str, ...], proper: list[ProperHit], content: list[dict], max_total: int) -> list[str]:
    final: list[str] = []
    seen: set[str] = set()
    for tag in list(base) + [h.tag for h in proper] + [c["tag"] for c in content]:
        key = tag.casefold()
        if key in seen or not _valid_tag(tag):
            continue
        seen.add(key)
        final.append(tag)
        if len(final) >= max_total:
            break
    return final


def suggest_for_slice(
    slice_id: str,
    title: str,
    srt_path: Path,
    *,
    bvid: str = "",
    use_llm: bool = True,
    max_tags: int = MAX_TAGS_DEFAULT,
    timeout: float = 240.0,
    suppress_tags: dict[str, str] | None = None,
    add_tags: dict[str, str] | None = None,
) -> dict:
    """suppress_tags/add_tags: 人工裁定通道 {tag: 理由}。

    用于成品字幕尚未修复、但 Ivan 已裁定事实的场合(例: 03 全片"安晚"=
    大N老师误听且安晚未出场 → suppress 安晚awa + add 南町)。裁定记录进
    输出; 字幕修复换源后应去掉裁定重算。
    """
    suppress_tags = suppress_tags or {}
    add_tags = add_tags or {}
    srt_text = parse_srt_text(srt_path)
    proper = scan_proper_nouns(title, srt_text)
    overridden = [h.tag for h in proper if h.tag in suppress_tags]
    proper = [h for h in proper if h.tag not in suppress_tags]
    # 人工裁定的补充 tag 排在专名最前(人工判断优先于统计排序)。
    proper = [
        ProperHit(tag=tag, rules=["ivan-override"], notes=[reason], title_hit=True)
        for tag, reason in add_tags.items()
        if tag not in {h.tag for h in proper}
    ] + proper
    existing = list(BASE_TAGS) + [h.tag for h in proper]
    content: list[dict] = []
    warnings: list[str] = []
    if use_llm:
        content, warnings = llm_content_tags(title, srt_text, existing, timeout)
    for tag in overridden:
        warnings.append(f"人工裁定移除: {tag!r} — {suppress_tags[tag]}")
    final = merge_tags(BASE_TAGS, proper, content, max_tags)
    return {
        "id": slice_id,
        "bvid": bvid,
        "title": title,
        "srt": str(srt_path),
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
        "content_tags": content,
        "warnings": warnings,
        "overrides": {"suppressed": suppress_tags, "added": add_tags},
        "final_tags": final,
        "final_tag_line": ",".join(final),
        "overflow_tags": [h.tag for h in proper if h.tag not in final]
        + [c["tag"] for c in content if c["tag"] not in final],
    }


def render_markdown(results: list[dict]) -> str:
    lines = [
        "# 切片 tag 建议 — Ivan 2026-07-13 口径",
        "",
        f"- 基础位(Ivan 拍板 4 个; free:do_upload.sh 仍是旧 6 位, 接入时改): `{','.join(BASE_TAGS)}`",
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
            Path(entry["srt"]),
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
