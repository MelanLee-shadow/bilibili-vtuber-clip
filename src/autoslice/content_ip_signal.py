"""Deterministic important-content-IP detection for tags and prompt signals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.upload_tag_policy import TermRule, load_selected_upload_tag_policy


@dataclass(frozen=True)
class ImportantContentIpMatch:
    rule_name: str
    canonical_name: str
    hits: int
    title_hit: bool
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ImportantContentIpSignal:
    matches: tuple[ImportantContentIpMatch, ...]

    @property
    def canonical_names(self) -> tuple[str, ...]:
        return tuple(match.canonical_name for match in self.matches)

    @property
    def prompt_block(self) -> str:
        names = "、".join(self.canonical_names) if self.canonical_names else "无白名单命中"
        return (
            "\n内容提及的重要 IP（白名单确定性信号）: "
            + names
            + "\n只作 audience_salience / hook / 标题选材信号，不自动加分，也不要求机械插词；"
            "若只是偶然提及可忽略。\n"
        )

    def as_receipt(self) -> list[dict[str, object]]:
        return [
            {
                "rule_name": match.rule_name,
                "canonical_name": match.canonical_name,
                "hits": match.hits,
                "title_hit": match.title_hit,
                "evidence": list(match.evidence),
            }
            for match in self.matches
        ]


def detect_important_content_ips(
    *,
    title: str,
    body: str,
    rules: Sequence[TermRule] | None = None,
) -> ImportantContentIpSignal:
    """Detect only explicitly registered program/IP rules; ``rules=()`` disables it."""

    selected_rules = (
        tuple(rules)
        if rules is not None
        else load_selected_upload_tag_policy().important_content_ips
    )
    condensed_body = re.sub(r"[\s，。？！—…·]+", "", str(body or ""))
    matches: list[ImportantContentIpMatch] = []
    seen_names: set[str] = set()
    for rule in selected_rules:
        evidence: list[str] = []
        total = 0
        title_hit = False
        for pattern in rule.patterns:
            regex = re.compile(pattern, re.IGNORECASE)
            body_hits = [match.group(0) for match in regex.finditer(str(body or ""))]
            condensed_hits = [match.group(0) for match in regex.finditer(condensed_body)]
            title_hits = [match.group(0) for match in regex.finditer(str(title or ""))]
            total += max(len(body_hits), len(condensed_hits)) + len(title_hits)
            title_hit = title_hit or bool(title_hits)
            evidence.extend(body_hits or condensed_hits)
            evidence.extend(title_hits)
        canonical_name = rule.tags[0] if rule.tags else ""
        if total < rule.min_hits or not canonical_name or canonical_name in seen_names:
            continue
        seen_names.add(canonical_name)
        matches.append(
            ImportantContentIpMatch(
                rule_name=rule.name,
                canonical_name=canonical_name,
                hits=total,
                title_hit=title_hit,
                evidence=tuple(dict.fromkeys(evidence)),
            )
        )
    matches.sort(key=lambda match: (match.title_hit, match.hits), reverse=True)
    return ImportantContentIpSignal(tuple(matches))


def important_content_ip_signal_from_srt(
    *,
    subtitle_path: object,
    fallback_body: str,
    title: str,
    rules: Sequence[TermRule] | None = None,
) -> ImportantContentIpSignal:
    """Read the full final SRT when available so a late/high-frequency IP is not lost."""

    body = str(fallback_body or "")
    if isinstance(subtitle_path, str) and Path(subtitle_path).is_file():
        try:
            srt_text = Path(subtitle_path).read_text(encoding="utf-8")
            body = "\n".join(cue.text for cue in parse_srt_cues(srt_text) if cue.text.strip())
        except OSError:
            pass
    return detect_important_content_ips(title=title, body=body, rules=rules)
