"""Structured danmaku/Super Chat evidence and exact read-aloud finalization.

Chat text is untrusted *content*, but when the streamer demonstrably reads it
aloud its wording is a stronger transcription source than ASR.  This module
keeps acquisition, candidate matching, and final-text application deterministic:
an LLM may use the evidence as context, but it cannot be the component that
decides whether the exact source survived into the delivered subtitle.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from src.autoslice.channel_profile import load_channel_profile


REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id


@dataclass(frozen=True)
class ReferentEntity:
    """One canonical entity and every surface that may denote it.

    Surfaces are intentionally separate from canonicals.  For example
    ``母鸡卡`` and ``Mujica`` are two spellings of the same ``Ave Mujica``
    entity, not two competing referents.
    """

    canonical: str
    surfaces: tuple[str, ...]
    readings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReferentGroup:
    entities: tuple[ReferentEntity, ...]
    reason: str = ""
    audio_verify_all_surfaces: bool = False
    # 方向性 fail-closed：列在此处的 canonical 是组内
    # "默认可信方"——文本已是它、音频又 UNCERTAIN 时保留原文不阻塞；
    # 未列出的（如真实词「乒乓球」）UNCERTAIN 仍阻塞待裁。
    uncertain_keep_canonicals: tuple[str, ...] = ()
    # 位置门控：非空时该组只在全文本审计的
    # 指定位置生效（"clip_initial"=片首 cue 且必须在句首），并且完全不进入
    # chat 证据路径——组内是普通高频词，全局匹配会引发裁决风暴。
    positions: tuple[str, ...] = ()
    # 面级 UNCERTAIN 保留：
    # 已知误听面本身可能是真话（「留下来」「理论上」），音频拿不准时保留
    # 原文、绝不阻塞；只有音频确证听到某 canonical 才改写。
    uncertain_keep_surfaces: tuple[str, ...] = ()
    # 别名组标记：话题图动态组的 surface 全是
    # 正当别名（海铃/八幡海铃），不含误听面——多槽位命中=一句提了多个角色，
    # 无可改写直接放行；静态组不打此标（母鸡卡类误听面多槽仍歧义熔断）。
    alias_surfaces: bool = False
    # 语境关联召回：误听面清单永远追不上 ASR
    # 打散专名的新变体。组内 canonical 语义核字（kmx→熊）在 cue 里以关联词
    # 出现（坏熊）、且组 canonical 已在本片其他 cue 字面确认时，句首杂段成为
    # 仲裁候选送音频强裁；UNCERTAIN 一律保留原文不阻塞。
    association_core_chars: tuple[str, ...] = ()


def sanitize_chat_display_text(text: str, *, max_chars: int = 500) -> str:
    """Render-safe chat data; never treat source text as prompt instructions."""

    value = str(text).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    value = re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", value
    )
    value = value.replace("-->", "→").replace("{", "｛").replace("}", "｝")
    return value[:max_chars].strip()


def load_referent_groups(
    path: str | Path, *, include_singletons: bool = False
) -> list[ReferentGroup]:
    """Load confusable groups; optionally expose singleton surface registries."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict) or payload.get("schema_version") not in {
        f"{PROFILE_ID}-referent-groups.v1",
        f"{PROFILE_ID}-referent-groups.v2",
    }:
        return []
    groups: list[ReferentGroup] = []
    for row in payload.get("groups") or []:
        entities = row.get("entities") if isinstance(row, dict) else None
        if not isinstance(entities, list):
            continue
        parsed: list[ReferentEntity] = []
        for value in entities:
            if isinstance(value, str):
                canonical = sanitize_chat_display_text(value, max_chars=80)
                if canonical:
                    parsed.append(ReferentEntity(canonical, (canonical,)))
                continue
            if not isinstance(value, dict):
                continue
            canonical = sanitize_chat_display_text(value.get("canonical", ""), max_chars=80)
            raw_surfaces = value.get("surfaces") or []
            raw_readings = value.get("readings") or []
            if (
                not canonical
                or not isinstance(raw_surfaces, list)
                or not isinstance(raw_readings, list)
            ):
                continue
            surfaces = [canonical]
            surfaces.extend(
                sanitize_chat_display_text(surface, max_chars=80) for surface in raw_surfaces
            )
            surfaces = list(dict.fromkeys(surface for surface in surfaces if surface))
            readings = tuple(
                dict.fromkeys(
                    sanitize_chat_display_text(reading, max_chars=120)
                    for reading in raw_readings
                    if sanitize_chat_display_text(reading, max_chars=120)
                )
            )
            parsed.append(ReferentEntity(canonical, tuple(surfaces), readings))
        canonicals = {entity.canonical.lower() for entity in parsed}
        if len(parsed) >= (1 if include_singletons else 2) and len(canonicals) == len(parsed):
            raw_keep = row.get("uncertain_keep_canonicals") or []
            keep = tuple(
                dict.fromkeys(
                    sanitize_chat_display_text(value, max_chars=80)
                    for value in (raw_keep if isinstance(raw_keep, list) else [])
                    if sanitize_chat_display_text(value, max_chars=80)
                    and any(
                        entity.canonical.lower()
                        == sanitize_chat_display_text(value, max_chars=80).lower()
                        for entity in parsed
                    )
                )
            )
            raw_positions = row.get("positions") or []
            positions = tuple(
                dict.fromkeys(
                    str(value)
                    for value in (raw_positions if isinstance(raw_positions, list) else [])
                    if str(value) in {"clip_initial", "transcript_only", "witness_disagreement"}
                )
            )
            all_surfaces = {surface.lower() for entity in parsed for surface in entity.surfaces}
            raw_keep_surfaces = row.get("uncertain_keep_surfaces") or []
            keep_surfaces = tuple(
                dict.fromkeys(
                    sanitize_chat_display_text(value, max_chars=80)
                    for value in (raw_keep_surfaces if isinstance(raw_keep_surfaces, list) else [])
                    if sanitize_chat_display_text(value, max_chars=80)
                    and sanitize_chat_display_text(value, max_chars=80).lower() in all_surfaces
                )
            )
            raw_assoc = row.get("association_core_chars") or []
            association = tuple(
                dict.fromkeys(
                    str(value)
                    for value in (raw_assoc if isinstance(raw_assoc, list) else [])
                    if isinstance(value, str) and len(value) == 1
                )
            )
            groups.append(
                ReferentGroup(
                    tuple(parsed),
                    sanitize_chat_display_text(row.get("reason", ""), max_chars=500),
                    row.get("audio_verify_all_surfaces") is True,
                    keep,
                    positions,
                    keep_surfaces,
                    association_core_chars=association,
                )
            )
    return groups


