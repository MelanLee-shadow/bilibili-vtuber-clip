"""成品自审员（Ivan 2026-07-14「发现环节不能永远是我」通病级机制）。

今晚全部机制的共同缺陷：错误的发现向量始终是 Ivan 的眼睛——流水线里没有
任何一层用"审片员视角"看过最终成品。本层补上这只眼睛：在全部证据车道之后
用 LLM 扫终稿字幕，**只报不改**；每条发现按证据纪律路由：

- ``homophone_fix``：建议与原文去声调同音（声学保真）→ 自动应用。这等价
  于"修正器改对了 + 守卫放行"的正规路径，只是发现向量换成了审片员；
  chat 证据拥有的 cue 一律不动（外层终审的面不可被扰动）。
- ``disclosure``：其余一切（语境怀疑、专名怀疑、非同音建议）保留原文，
  只落工件与日报。Ivan 读清单，而不是逐帧看片。

审片员是发现器不是改写器：无声学等价的建议永不落盘。它的价值在于把
「季下」「苏人」这类人眼一秒识别的胡话在交付前暴露出来——修不修由证据
决定，但绝不允许"无人知晓地交付"。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.subtitle_fidelity import _homophone_equal

MAX_FINDINGS = 12

_GLOSSARY_TERM_RX = re.compile(r"^[-*]\s*(?:梗词：)?\*{0,2}([^：:（(＝=，,。\s*]{2,12})")


def protected_terms() -> frozenset[str]:
    """钦定词面集合——委托唯一加载源 term_authority（见该模块 docstring）。"""

    from src.autoslice.term_authority import protected_terms as _load

    return _load()


_AUDIT_PROMPT = """你是李豆沙切片的终审审片员。下面是一条成品切片的最终字幕（观众将看到的原文）。
你的任务是**只挑出可疑处，绝不改写**。可疑类别：
- nonword：读起来不是词的胡话/生造词（如「季下」「苏人」——多为语音误听残留）；
- context：与前后语境明显矛盾、说不通的词；
- self_ref：主播自称混乱可疑处（她的自称专名是「李豆沙」和「小李」，两者平等；
  出现疑似自称却写成别的词的地方报出来）；
- entity：疑似专名/人名/作品名被写错的地方。

已知梗词与专名表（钦定写法，一律不要报）：
{glossary}

规则：
1. 宁缺毋滥：只报你有把握可疑的，正常口语、脏话、语气词、网络梗不要报。
2. suggestion 只在你能从语境合理推断出原话时给出，否则为 null。
3. 不确定就不报。最多 {max_findings} 条。

字幕（每行：编号. 文本）：
{numbered}

只输出一个 JSON 对象：
{{"findings": [{{"cue": 编号, "suspect": "原文中的可疑片段(逐字)", "kind": "nonword|context|self_ref|entity", "suggestion": "推断的原话或 null", "why": "一句话理由"}}]}}
没有可疑处就输出 {{"findings": []}}。
"""


def audit_final_subtitles(
    srt_text: str,
    *,
    llm_call: Callable[[str], str],
    extract_json: Callable[[str], Any],
    glossary_text: str = "",
) -> list[dict[str, Any]]:
    """One reviewer pass over the final SRT; returns validated findings only."""

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not cues:
        return []
    numbered = "\n".join(f"{index}. {cue.text}" for index, cue in enumerate(cues, start=1))
    prompt = _AUDIT_PROMPT.format(
        max_findings=MAX_FINDINGS,
        numbered=numbered,
        glossary=(glossary_text.strip() or "（无）"),
    )
    try:
        payload = extract_json(llm_call(prompt))
    except Exception:
        return []
    raw = payload.get("findings") if isinstance(payload, dict) else None
    findings: list[dict[str, Any]] = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            cue_index = int(row.get("cue"))
        except (TypeError, ValueError):
            continue
        suspect = str(row.get("suspect") or "").strip()
        if not (1 <= cue_index <= len(cues)) or not suspect:
            continue
        if suspect not in cues[cue_index - 1].text:
            continue
        kind = str(row.get("kind") or "")
        if kind not in {"nonword", "context", "self_ref", "entity"}:
            kind = "context"
        suggestion_raw = row.get("suggestion")
        suggestion = str(suggestion_raw).strip() if isinstance(suggestion_raw, str) else ""
        findings.append(
            {
                "cue_index": cue_index,
                "suspect": suspect,
                "kind": kind,
                "suggestion": suggestion or None,
                "why": str(row.get("why") or "")[:120],
            }
        )
        if len(findings) >= MAX_FINDINGS:
            break
    return findings


def route_findings(
    srt_text: str,
    findings: Iterable[dict[str, Any]],
    *,
    protected_cue_indexes: Iterable[int] = (),
    protected_term_set: frozenset[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Apply only sound-faithful suggestions; disclose everything else."""

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    protected = {int(v) for v in protected_cue_indexes}
    guarded_terms = protected_terms() if protected_term_set is None else protected_term_set
    rows: list[dict[str, Any]] = []
    applied = 0
    for finding in findings:
        row = dict(finding)
        cue_index = int(row["cue_index"])
        suspect = str(row["suspect"])
        suggestion = row.get("suggestion")
        if any(term in suspect for term in guarded_terms):
            # 词典权威高于审片直觉（立语/做0.4 案）：钦定词面永不自动改写。
            row["routed"] = "disclosure_protected_term"
            rows.append(row)
            continue
        if (
            suggestion
            and cue_index not in protected
            and suspect in texts[cue_index - 1]
            and _homophone_equal(suspect, str(suggestion))
        ):
            texts[cue_index - 1] = texts[cue_index - 1].replace(suspect, str(suggestion), 1)
            row["routed"] = "homophone_fix"
            applied += 1
        else:
            row["routed"] = "disclosure" if cue_index not in protected else "disclosure_protected"
        rows.append(row)
    output_lines = []
    for index, (cue, text) in enumerate(zip(cues, texts), start=1):
        output_lines.append(f"{index}\n{_ms(cue.start_ms)} --> {_ms(cue.end_ms)}\n{text}\n")
    audit = {
        "schema_version": "final-review-audit.v1",
        "status": "APPLIED" if applied else ("FLAGGED" if rows else "CLEAN"),
        "applied_count": applied,
        "findings": rows,
    }
    return ("\n".join(output_lines) if applied else srt_text), audit


def _ms(value_ms: int) -> str:
    hours, rem = divmod(int(value_ms), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def persist_review_audit(path: Path, audit: dict[str, Any]) -> None:
    try:
        path.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
