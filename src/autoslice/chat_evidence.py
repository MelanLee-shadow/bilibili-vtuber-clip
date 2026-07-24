"""Structured danmaku/Super Chat evidence and exact read-aloud finalization.

Chat text is untrusted *content*, but when the streamer demonstrably reads it
aloud its wording is a stronger transcription source than ASR.  This module
keeps acquisition, candidate matching, and final-text application deterministic:
an LLM may use the evidence as context, but it cannot be the component that
decides whether the exact source survived into the delivered subtitle.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.chat_event_timing import (
    danmaku_text as _danmaku_text,
    event_epoch_ms,
    recording_start_epoch_ms as recording_start_epoch_ms,
    send_time_ms as _send_time_ms,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.surface_canon import (
    canonicalize_hard_meme_surfaces as canonicalize_hard_meme_surfaces,
    normalize_hard_meme_surfaces as normalize_hard_meme_surfaces,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id
_NON_TEXT = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", re.IGNORECASE)
_QUESTION_TAIL = frozenset("吗呢吧嘛呀啊？?")
_CANONICAL_SURFACE_RULES = CHANNEL_PROFILE.canonical_surface_rules


def canonicalize_hard_surfaces(text: str) -> str:
    """对普通字符串（标题/封面文案/hook）应用同一套无条件表面规范。"""
    normalized = text
    for rule in _CANONICAL_SURFACE_RULES:
        if rule.surface in normalized:
            normalized = normalized.replace(rule.surface, rule.canonical)
    return normalized


@dataclass(frozen=True)
class ChatEvidence:
    kind: str  # danmaku | superchat | gift | guard
    offset_ms: int
    text: str
    sender: str = ""
    source: str = ""
    source_sha256: str = ""
    source_event_id: str = ""

    @property
    def evidence_id(self) -> str:
        raw = (
            f"{self.kind}\0{self.offset_ms}\0{self.sender}\0{self.text}\0{self.source_event_id}"
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


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
    # 方向性 fail-closed（2026-07-13 kmx 案）：列在此处的 canonical 是组内
    # "默认可信方"——文本已是它、音频又 UNCERTAIN 时保留原文不阻塞；
    # 未列出的（如真实词「乒乓球」）UNCERTAIN 仍阻塞待裁。
    uncertain_keep_canonicals: tuple[str, ...] = ()
    # 位置门控（2026-07-13 彩排「大家/但是」案）：非空时该组只在全文本审计的
    # 指定位置生效（"clip_initial"=片首 cue 且必须在句首），并且完全不进入
    # chat 证据路径——组内是普通高频词，全局匹配会引发裁决风暴。
    positions: tuple[str, ...] = ()
    # 面级 UNCERTAIN 保留（2026-07-14 理论上/留下→李豆沙、苏人→素惹案）：
    # 已知误听面本身可能是真话（「留下来」「理论上」），音频拿不准时保留
    # 原文、绝不阻塞；只有音频确证听到某 canonical 才改写。
    uncertain_keep_surfaces: tuple[str, ...] = ()
    # 别名组标记（2026-07-14 梦限大 cue31 案）：话题图动态组的 surface 全是
    # 正当别名（海铃/八幡海铃），不含误听面——多槽位命中=一句提了多个角色，
    # 无可改写直接放行；静态组不打此标（母鸡卡类误听面多槽仍歧义熔断）。
    alias_surfaces: bool = False


EntityVerifier = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]


# 念读因果下界（Ivan 2026-07-20）：事件时间戳＝发送时刻，经 渲染→看到→
# 开口→推流 每环只加正延迟（繁忙房渲染实测 ~15s）。cue 早于发送+2s 的
# "念读"物理不可能，确定性排除；弱界宁松勿枉，非渲染延迟估计。
READ_ALOUD_MIN_DELAY_MS = 2_000
_event_epoch_ms = event_epoch_ms


def normalize_chat_text(text: str) -> str:
    return _NON_TEXT.sub("", str(text)).lower()


_SPEAKER_LABEL = re.compile(
    r"^\[(?:"
    + "|".join(
        re.escape(value)
        for value in (
            CHANNEL_PROFILE.host_speaker_label,
            CHANNEL_PROFILE.guest_speaker_label,
        )
    )
    + r")\]\s*"
)


def normalize_srt_payload_text(srt_text: str, *, strip_speaker_labels: bool = False) -> str:
    """Normalize only subtitle payload, never SRT indices/timestamps."""

    return normalize_chat_text(
        "".join(
            _SPEAKER_LABEL.sub("", cue.text) if strip_speaker_labels else cue.text
            for cue in parse_srt_cues(srt_text)
            if cue.text.strip()
        )
    )


def normalize_srt_payload_window(
    srt_text: str,
    *,
    start_ms: int,
    end_ms: int,
    strip_speaker_labels: bool = False,
    tolerance_ms: int = 250,
) -> str:
    """Normalize payload from a fuzzy evidence span.

    The tolerance is intentional for discovery/evidence matching.  Exact
    source-truth and reviewed-baseline ownership must instead use
    :func:`normalize_srt_owner_payload_window`; otherwise a cue that merely
    touches, or sits just outside, an owner boundary contaminates the payload.
    """

    texts = []
    for cue in parse_srt_cues(srt_text):
        if cue.end_ms < start_ms - tolerance_ms or cue.start_ms > end_ms + tolerance_ms:
            continue
        text = _SPEAKER_LABEL.sub("", cue.text) if strip_speaker_labels else cue.text
        if text.strip():
            texts.append(text)
    return normalize_chat_text("".join(texts))


def normalize_srt_owner_payload_window(
    srt_text: str,
    *,
    start_ms: int,
    end_ms: int,
    min_overlap_ms: int,
    strip_speaker_labels: bool = False,
) -> str:
    """Normalize cues materially owned by one exact half-open time window.

    Exact ownership is deliberately not a fuzzy evidence lookup.  A cue is
    included only when its real overlap with ``[start_ms, end_ms)`` reaches the
    same minimum used when the owning authority selected its target cues.
    Boundary-touching neighbours therefore contribute zero milliseconds and
    cannot make an otherwise exact ``replace_cue`` or ``drop_cue`` contract
    fail.
    """

    if isinstance(min_overlap_ms, bool) or min_overlap_ms <= 0:
        raise ValueError("min_overlap_ms must be a positive integer")
    texts = []
    for cue in parse_srt_cues(srt_text):
        overlap_ms = max(
            0,
            min(cue.end_ms, end_ms) - max(cue.start_ms, start_ms),
        )
        if overlap_ms < min_overlap_ms:
            continue
        text = _SPEAKER_LABEL.sub("", cue.text) if strip_speaker_labels else cue.text
        if text.strip():
            texts.append(text)
    return normalize_chat_text("".join(texts))


def normalize_code_switch_surfaces(srt_text: str) -> tuple[str, dict[str, Any]]:
    """Canonicalize narrow phonetic spellings of known Japanese insertions."""

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    repairs: list[dict[str, Any]] = []
    for offset, before in enumerate(list(texts)):
        after = before
        replaced: list[dict[str, str]] = []
        for rule in _CANONICAL_SURFACE_RULES:
            surface = rule.surface
            canonical = rule.canonical
            authority = rule.authority
            if surface not in after:
                continue
            after = after.replace(surface, canonical)
            replaced.append({"surface": surface, "canonical": canonical, "authority": authority})
        if after == before:
            continue
        texts[offset] = after
        repairs.append(
            {
                "cue_index": offset + 1,
                "matched_start_ms": cues[offset].start_ms,
                "matched_end_ms": cues[offset].end_ms,
                "before": before,
                "after": after,
                "replacements": replaced,
                "authority": f"{PROFILE_ID}-code-switch-canon.v1",
            }
        )
    output = _render_srt(cues, texts) if repairs else srt_text
    return output, {
        "schema_version": "code-switch-surface-audit.v1",
        "status": "APPLIED" if repairs else "NO_CHANGE",
        "repairs": repairs,
        "input_srt_sha256": hashlib.sha256(srt_text.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(output.encode()).hexdigest(),
    }


def sanitize_chat_display_text(text: str, *, max_chars: int = 500) -> str:
    """Render-safe chat data; never treat source text as prompt instructions."""

    value = str(text).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    value = re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", value
    )
    value = value.replace("-->", "→").replace("{", "｛").replace("}", "｝")
    return value[:max_chars].strip()


def load_referent_groups(path: str | Path) -> list[ReferentGroup]:
    """Load canonical/surface-aware mutually-confusable entity groups."""

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
        if len(parsed) >= 2 and len(canonicals) == len(parsed):
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
            groups.append(
                ReferentGroup(
                    tuple(parsed),
                    sanitize_chat_display_text(row.get("reason", ""), max_chars=500),
                    row.get("audio_verify_all_surfaces") is True,
                    keep,
                    positions,
                    keep_surfaces,
                )
            )
    return groups


def _coerce_referent_groups(
    groups: Sequence[ReferentGroup | Sequence[str]],
) -> list[ReferentGroup]:
    out: list[ReferentGroup] = []
    for raw_group in groups:
        if isinstance(raw_group, ReferentGroup):
            out.append(raw_group)
            continue
        parsed = tuple(
            ReferentEntity(str(value), (str(value),)) for value in raw_group if str(value)
        )
        if len(parsed) >= 2:
            out.append(ReferentGroup(parsed))
    return out


def load_clip_opening_address_config(path: str | Path) -> dict[str, Any] | None:
    """Load the clip-opening positional-prior config (connectives + addresses)."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != f"{PROFILE_ID}-clip-opening-address.v1"
    ):
        return None
    connectives = [
        {
            "surface": str(row.get("surface")),
            "readings": [str(r) for r in (row.get("readings") or []) if str(r)],
        }
        for row in (payload.get("connectives") or [])
        if isinstance(row, dict) and str(row.get("surface") or "").strip()
    ]
    addresses = [
        {
            "canonical": str(row.get("canonical")),
            "readings": [str(r) for r in (row.get("readings") or []) if str(r)],
        }
        for row in (payload.get("addresses") or [])
        if isinstance(row, dict) and str(row.get("canonical") or "").strip()
    ]
    if not connectives or not addresses:
        return None
    return {
        "connectives": connectives,
        "addresses": addresses,
        "reason": str(payload.get("reason") or ""),
    }


def clip_opening_address_group(
    srt_text: str, config: Mapping[str, Any] | None
) -> ReferentGroup | None:
    """位置先验怀疑编译器（2026-07-13，由「大家→但是」彩排实例抽象通用化）。

    切片首句以转折/承接连词开头，这个位置本身就可疑——开场位置更可能是
    称呼语；ASR 没有切片边界概念，只会写声学最近的常见词。任何命中的连词
    都会编译成一个 clip_initial 混淆组交给音频仲裁引擎：候选=该连词+全部
    称呼语。只有音频确证听到称呼语才改写；UNCERTAIN（含供应商故障）一律
    保留原文、绝不阻塞——连词开场是合法高频口语，fail-closed 只约束改写。
    """

    if not config:
        return None
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    if not cues:
        return None
    first = cues[0].text.strip()
    hit: Mapping[str, Any] | None = None
    for row in config.get("connectives") or []:
        surface = str(row.get("surface") or "")
        if surface and first.startswith(surface):
            if hit is None or len(surface) > len(str(hit["surface"])):
                hit = row
    if hit is None:
        return None
    connective = str(hit["surface"])
    entities = [ReferentEntity(connective, (connective,), tuple(hit.get("readings") or ()))]
    for row in config.get("addresses") or []:
        canonical = str(row["canonical"])
        if canonical.lower() == connective.lower():
            continue
        entities.append(ReferentEntity(canonical, (canonical,), tuple(row.get("readings") or ())))
    if len(entities) < 2:
        return None
    return ReferentGroup(
        tuple(entities),
        reason=str(config.get("reason") or "")
        or "片首连词开场位置先验：开场更可能是称呼语；仅音频确证才改写。",
        audio_verify_all_surfaces=True,
        uncertain_keep_canonicals=(connective,),
        positions=("clip_initial",),
    )


_CJK_ONLY = re.compile(r"^[一-鿿]+$")


def repetition_divergence_groups(
    srt_text: str,
    *,
    window: int = 3,
    min_norm_len: int = 6,
    min_ratio: float = 0.72,
    span_min: int = 2,
    span_max: int = 4,
    max_groups: int = 3,
) -> list[ReferentGroup]:
    """重复一致性怀疑编译器（2026-07-13，由「睡衣/素颜」新衣服实例抽象通用化）。

    邻近两句高相似、仅差一个 2-4 字连续 CJK 片段——复读/自我重复里同一
    指称不应变词。每对分歧编译成一个 transcript_only 混淆组：两处 cue 各自
    送音频裁决，谁的音频赢谁留下；UNCERTAIN 双向保留、绝不阻塞。真·不同
    的复读（谢谢A/谢谢B 或刻意对比句）会被音频各自确认，零改写。完全无
    词表，自动覆盖未见过的实例。
    """

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    norms = [normalize_chat_text(text) for text in texts]
    seen: set[frozenset[str]] = set()
    groups: list[ReferentGroup] = []
    for i in range(len(cues)):
        if len(norms[i]) < min_norm_len:
            continue
        for j in range(i + 1, min(i + 1 + window, len(cues))):
            if len(groups) >= max_groups:
                return groups
            a, b = norms[i], norms[j]
            if len(b) < min_norm_len or a == b:
                continue
            matcher = SequenceMatcher(None, a, b, autojunk=False)
            if matcher.ratio() < min_ratio:
                continue
            opcodes = matcher.get_opcodes()
            replaces = [op for op in opcodes if op[0] == "replace"]
            if len(replaces) != 1 or any(op[0] in ("insert", "delete") for op in opcodes):
                continue
            _, a1, a2, b1, b2 = replaces[0]
            span_a, span_b = a[a1:a2], b[b1:b2]
            if not (span_min <= len(span_a) <= span_max and span_min <= len(span_b) <= span_max):
                continue
            if not _CJK_ONLY.match(span_a) or not _CJK_ONLY.match(span_b):
                continue
            if span_a in span_b or span_b in span_a:
                continue
            # 变体必须能在各自原始 cue 里按面匹配，且不出现在对方句中——
            # 否则槽位不唯一，放弃这一对（宁缺毋滥）。
            if span_a not in texts[i].lower() and span_a not in texts[i]:
                continue
            if span_b not in texts[j].lower() and span_b not in texts[j]:
                continue
            if span_a in texts[j].lower() or span_b in texts[i].lower():
                continue
            key = frozenset((span_a, span_b))
            if key in seen:
                continue
            seen.add(key)
            groups.append(
                ReferentGroup(
                    (
                        ReferentEntity(span_a, (span_a,)),
                        ReferentEntity(span_b, (span_b,)),
                    ),
                    reason=f"重复一致性：邻近复读句仅差「{span_a}/{span_b}」，两处各自由音频裁决。",
                    audio_verify_all_surfaces=True,
                    uncertain_keep_canonicals=(span_a, span_b),
                    positions=("transcript_only",),
                )
            )
    return groups


def witness_disagreement_cues(draft_srt: str, final_srt: str, group: ReferentGroup) -> list[int]:
    """证人引入仲裁的怀疑编译器（2026-07-14 生日结婚「小李」案抽象）。

    返回 final 中出现组内形态、而同时轴 draft cue 无该形态的 cue_index
    （1-based，按 final 非空 cue 序）。这是语义专名修正的来源差异审计，
    不是把终稿重新交给黑帧 Gemini、强制回归 draft 的许可。两侧都在场的
    形态不打扰；时轴对不上的 cue 宁缺毋滥直接跳过。
    """

    draft_by_span = {
        (cue.start_ms, cue.end_ms): normalize_chat_text(cue.text)
        for cue in parse_srt_cues(draft_srt)
        if cue.text.strip()
    }
    out: list[int] = []
    final_cues = [cue for cue in parse_srt_cues(final_srt) if cue.text.strip()]
    for index, cue in enumerate(final_cues, start=1):
        draft_norm = draft_by_span.get((cue.start_ms, cue.end_ms))
        if draft_norm is None:
            continue
        final_norm = normalize_chat_text(cue.text)
        for entity in group.entities:
            for surface in entity.surfaces:
                normalized = normalize_chat_text(surface)
                if normalized and normalized in final_norm and normalized not in draft_norm:
                    out.append(index)
                    break
            else:
                continue
            break
    return out


def introduced_term_cues(
    draft_srt: str,
    final_srt: str,
    protected: Iterable[str],
    *,
    max_rows: int = 4,
) -> list[dict[str, Any]]:
    """引入词来源差异探测器（2026-07-14 乐队番 Ave Mujica/睦睦 案抽象）。

    凡语义终稿 cue 出现钦定词而同时轴 draft cue 没有，就对齐记录 draft
    对应片段。它只证明“专名修正导致文本与初始听写不同”，供审计披露；
    初始 ASR 不是终稿的后置硬门，禁止据此再让声学模型二选一改回去。
    """

    draft_by_span = {
        (cue.start_ms, cue.end_ms): cue.text
        for cue in parse_srt_cues(draft_srt)
        if cue.text.strip()
    }
    final_cues = [cue for cue in parse_srt_cues(final_srt) if cue.text.strip()]
    terms = sorted({t for t in protected if t and len(str(t)) >= 2}, key=len, reverse=True)
    rows: list[dict[str, Any]] = []
    for index, cue in enumerate(final_cues, start=1):
        draft_text = draft_by_span.get((cue.start_ms, cue.end_ms))
        if draft_text is None:
            continue
        final_norm = normalize_chat_text(cue.text)
        draft_norm = normalize_chat_text(draft_text)
        for term in terms:
            term_norm = normalize_chat_text(str(term))
            if not term_norm or term_norm not in final_norm or term_norm in draft_norm:
                continue
            position = cue.text.find(str(term))
            if position < 0:
                continue
            span = None
            matcher = SequenceMatcher(None, draft_text, cue.text, autojunk=False)
            for op, a1, a2, b1, b2 in matcher.get_opcodes():
                if op in ("replace", "insert") and b1 <= position < b2:
                    span = draft_text[a1:a2]
                    # 单字对齐段向左扩一字（cue8 案：的梦→，Ave Mujica 的
                    # draft 侧只剩「梦」，单字面成不了仲裁槽）。
                    if len(span.strip()) == 1 and a1 > 0:
                        span = draft_text[a1 - 1 : a2]
                    break
            span = (span or "").strip("，。！？,.!? ")
            if len(span) < 2 or span == str(term):
                continue
            rows.append({"cue_index": index, "term": str(term), "draft_span": span[:10]})
            break
        if len(rows) >= max_rows:
            break
    return rows


def _entity_occurrences(text: str, group: ReferentGroup) -> list[dict[str, Any]]:
    """Find longest non-overlapping surfaces and retain canonical identity."""

    lowered = str(text).lower()
    occupied: set[int] = set()
    found: list[dict[str, Any]] = []
    candidates = [
        (surface, entity.canonical)
        for entity in group.entities
        for surface in entity.surfaces
        # 单字面禁止成槽（2026-07-14 乐队番案：话题图组的「灯」把「粉丝
        # 灯牌」命中成高松灯候选并阻塞整条）——实体面最短两字。
        if surface and len(surface) >= 2
    ]
    for surface, canonical in sorted(candidates, key=lambda pair: len(pair[0]), reverse=True):
        for match in re.finditer(re.escape(surface.lower()), lowered):
            indexes = set(range(match.start(), match.end()))
            if indexes & occupied:
                continue
            occupied.update(indexes)
            found.append(
                {
                    "canonical": canonical,
                    "surface": text[match.start() : match.end()],
                    "start": match.start(),
                    "end": match.end(),
                }
            )
    return sorted(found, key=lambda row: row["start"])


def _request_sha256(request: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in request.items() if key != "request_sha256"}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _valid_sha256(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "")))


def build_human_text_entity_verifier(
    document_path: str | Path,
    *,
    candidate_id: str,
) -> EntityVerifier:
    """Build a deferred verifier from one hash-bound Ivan text decision asset.

    The decision is allowed to suppress a conflicting exact-chat proposal now,
    but it does not become complete authority until
    :func:`reconcile_pending_text_overrides` proves the declared source and
    final SRT hashes after the normal text-override stage.
    """

    source = Path(document_path)
    raw = source.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2, 3}:
        raise ValueError("text override schema_version must be 1, 2, or 3")
    if payload.get("candidate_id") != candidate_id:
        raise ValueError("text override candidate_id mismatch")
    override_schema_version = int(payload["schema_version"])
    if override_schema_version == 1:
        binding = {
            "source_srt_sha256": str(payload.get("source_srt_sha256") or ""),
            "text_final_srt_sha256": str(payload.get("text_final_srt_sha256") or ""),
        }
        binding_error = "text override must bind source and final SRT SHA256"
    else:
        binding = {
            "source_cue_witness_sha256": str(payload.get("source_cue_witness_sha256") or ""),
            "decision_output_witness_sha256": str(
                payload.get("decision_output_witness_sha256") or ""
            ),
        }
        binding_error = "cue-bound text override must bind source and decision witnesses"
    if not all(_valid_sha256(value) for value in binding.values()):
        raise ValueError(binding_error)
    rows = payload.get("chat_entity_verdicts") or []
    if not isinstance(rows, list):
        raise ValueError("chat_entity_verdicts must be a list")
    by_evidence: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            raise ValueError("chat_entity_verdict row must be an object")
        evidence_id = str(raw_row.get("evidence_id") or "")
        canonical = sanitize_chat_display_text(raw_row.get("canonical_entity", ""), max_chars=80)
        if not _valid_sha256(evidence_id) or not canonical or evidence_id in by_evidence:
            raise ValueError("invalid or duplicate chat entity verdict")
        by_evidence[evidence_id] = {**raw_row, "canonical_entity": canonical}
    document_hash = hashlib.sha256(raw).hexdigest()

    def verifier(request: Mapping[str, Any]) -> Mapping[str, Any] | None:
        row = by_evidence.get(str(request.get("evidence_id") or ""))
        if row is None:
            return None
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request.get("request_sha256"),
            "status": "RESOLVED",
            "canonical_entity": row["canonical_entity"],
            "authority_kind": "ivan_text_override",
            "defer_to_text_override": True,
            "candidate_id": candidate_id,
            "override_document_sha256": document_hash,
            "override_schema_version": override_schema_version,
            **binding,
            "authority": str(row.get("authority") or "Ivan direct correction"),
        }

    return verifier


def _srt_clock_ms(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value)
    if match is None:
        raise ValueError(f"invalid SRT timestamp: {value!r}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def reconcile_pending_text_overrides(
    audit: dict[str, Any],
    text_manifest: Mapping[str, Any] | None,
    *,
    delivery_start_ms: int,
) -> bool:
    """Close deferred human entity verdicts against the actual override result."""

    pending = audit.get("pending_text_overrides") or []
    if not pending:
        return True
    if not isinstance(text_manifest, Mapping) or text_manifest.get("status") != "READY":
        return False
    decisions = text_manifest.get("decisions") or []
    if not isinstance(decisions, list):
        return False
    document_hash = str(text_manifest.get("override_document_sha256") or "")
    source_hash = str(text_manifest.get("source_srt_sha256") or "")
    final_hash = str(text_manifest.get("output_srt_sha256") or "")
    override_schema_version = int(text_manifest.get("override_schema_version") or 1)
    if override_schema_version == 1:
        manifest_binding = {
            "source_srt_sha256": source_hash,
            "text_final_srt_sha256": final_hash,
        }
    elif override_schema_version in {2, 3}:
        manifest_binding = {
            "source_cue_witness_sha256": str(text_manifest.get("source_cue_witness_sha256") or ""),
            "decision_output_witness_sha256": str(
                text_manifest.get("decision_output_witness_sha256") or ""
            ),
        }
    else:
        return False
    rebound_document: dict[str, Any] | None = None

    def verdict_matches_manifest(verdict: Mapping[str, Any]) -> bool:
        return (
            int(verdict.get("override_schema_version") or 1) == override_schema_version
            and verdict.get("override_document_sha256") == document_hash
            and all(verdict.get(key) == value for key, value in manifest_binding.items())
        )

    def rebind_deferred_verdict(
        pending_row: Mapping[str, Any], verdict: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Move an unchanged Ivan entity verdict onto a newly frozen ASR variant.

        A generative text pass may drift between attempts even though the
        evidence id and Ivan's entity decision do not.  Rebinding is allowed
        only through the exact decision document that produced this READY
        text manifest; the entity itself may not change.
        """

        nonlocal rebound_document
        if (
            verdict.get("authority_kind") != "ivan_text_override"
            or verdict.get("defer_to_text_override") is not True
            or not _valid_sha256(document_hash)
            or not _valid_sha256(source_hash)
            or not _valid_sha256(final_hash)
        ):
            return None
        document_value = text_manifest.get("override_document")
        if not isinstance(document_value, str) or not document_value:
            return None
        document_path = Path(document_value)
        try:
            raw = document_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != document_hash:
                return None
            if rebound_document is None:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    return None
                rebound_document = payload
        except (OSError, ValueError):
            return None
        document = rebound_document
        if document.get("candidate_id") != verdict.get("candidate_id"):
            return None
        if int(document.get("schema_version") or 0) != override_schema_version:
            return None
        if override_schema_version == 1:
            document_binding = {
                "source_srt_sha256": document.get("source_srt_sha256"),
                "text_final_srt_sha256": document.get("text_final_srt_sha256"),
            }
        else:
            document_binding = {
                "source_cue_witness_sha256": document.get("source_cue_witness_sha256"),
                "decision_output_witness_sha256": document.get("decision_output_witness_sha256"),
            }
        if document_binding != manifest_binding:
            return None
        evidence_id = str(pending_row.get("evidence_id") or "")
        rows = [
            row
            for row in document.get("chat_entity_verdicts") or []
            if isinstance(row, dict) and str(row.get("evidence_id") or "") == evidence_id
        ]
        if len(rows) != 1 or rows[0].get("canonical_entity") != verdict.get("canonical_entity"):
            return None
        return {
            **verdict,
            "override_document_sha256": document_hash,
            "override_schema_version": override_schema_version,
            **manifest_binding,
            "authority": str(rows[0].get("authority") or verdict.get("authority") or ""),
        }

    repaired: list[dict[str, Any]] = []
    used_decisions: set[int] = set()
    for pending_row in pending:
        verdict = dict(pending_row.get("verdict") or {})
        if not verdict_matches_manifest(verdict):
            rebound = rebind_deferred_verdict(pending_row, verdict)
            if rebound is None:
                return False
            verdict_events = audit.get("entity_verdicts")
            if isinstance(verdict_events, list) and verdict_events:
                event_matches = [
                    event
                    for event in verdict_events
                    if isinstance(event, dict)
                    and event.get("evidence_id") == pending_row.get("evidence_id")
                    and isinstance(event.get("verdict"), dict)
                    and event["verdict"].get("request_sha256") == verdict.get("request_sha256")
                    and event["verdict"].get("canonical_entity") == verdict.get("canonical_entity")
                ]
                if len(event_matches) != 1:
                    return False
                event_matches[0]["verdict"] = dict(rebound)
            pending_row["verdict_rebinding"] = {
                "status": "UNCHANGED_ENTITY_REBOUND_TO_FROZEN_SOURCE",
                "previous_override_document_sha256": verdict.get("override_document_sha256"),
                "previous_source_srt_sha256": verdict.get("source_srt_sha256"),
                "previous_text_final_srt_sha256": verdict.get("text_final_srt_sha256"),
                "override_document_sha256": document_hash,
                "override_schema_version": override_schema_version,
                "source_srt_sha256": source_hash,
                "text_final_srt_sha256": final_hash,
                **manifest_binding,
            }
            pending_row["verdict"] = rebound
            verdict = rebound
        evidence_id = str(pending_row.get("evidence_id") or "")
        match_index = next(
            (
                index
                for index, decision in enumerate(decisions)
                if index not in used_decisions
                and isinstance(decision, dict)
                and decision.get("supersedes_chat_evidence_id") == evidence_id
            ),
            None,
        )
        if match_index is None:
            return False
        decision = decisions[match_index]
        used_decisions.add(match_index)
        source_cue = decision.get("source") or {}
        before = str(source_cue.get("text") or "")
        after = str(decision.get("output_text") or "")
        expected = str(verdict.get("canonical_entity") or "")
        request = pending_row.get("request") or {}
        entities = request.get("candidate_entities") or []
        decision_group = ReferentGroup(
            tuple(
                ReferentEntity(
                    str(entity.get("canonical") or ""),
                    tuple(str(surface) for surface in entity.get("surfaces") or [] if str(surface)),
                    tuple(str(reading) for reading in entity.get("readings") or [] if str(reading)),
                )
                for entity in entities
                if isinstance(entity, dict) and str(entity.get("canonical") or "")
            )
        )
        occurrences = _entity_occurrences(before, decision_group)
        minimal = (
            len(occurrences) == 1
            and re.sub(
                re.escape(str(occurrences[0]["surface"])),
                expected,
                before,
                count=1,
                flags=re.IGNORECASE,
            )
            == after
        )
        # A read-chat cue can legitimately combine two independent authorities:
        # the exact platform text owns the sentence scaffold, while Ivan's
        # hash-bound listening verdict owns only the confusable entity slot.
        # Example: ASR ``是Mujica的风险`` + danmaku ``有母鸡卡的风险``
        # becomes ``有梦限大的风险``.  This is not an arbitrary whole-cue
        # override: replacing the decided entity in the output with the exact
        # chat surface must reconstruct a contiguous substring of the platform
        # message, and every surface must belong to the same declared group.
        after_occurrences = _entity_occurrences(after, decision_group)
        exact_chat = str(pending_row.get("exact_text") or "")
        chat_occurrences = _entity_occurrences(exact_chat, decision_group)
        chat_scaffold = False
        structured_expectation = ""
        if (
            len(occurrences) == 1
            and len(after_occurrences) == 1
            and after_occurrences[0]["canonical"] == expected
            and len(chat_occurrences) == 1
            and chat_occurrences[0]["canonical"] != expected
        ):
            after_entity = after_occurrences[0]
            projected = (
                after[: int(after_entity["start"])]
                + str(chat_occurrences[0]["surface"])
                + after[int(after_entity["end"]) :]
            )
            projected_normalized = normalize_chat_text(projected)
            chat_scaffold = bool(
                projected_normalized and projected_normalized in normalize_chat_text(exact_chat)
            )
            if chat_scaffold:
                chat_entity = chat_occurrences[0]
                structured_expectation = (
                    exact_chat[: int(chat_entity["start"])]
                    + expected
                    + exact_chat[int(chat_entity["end"]) :]
                )
        if not (minimal or chat_scaffold):
            return False
        relative_start = _srt_clock_ms(str(source_cue.get("start") or ""))
        relative_end = _srt_clock_ms(str(source_cue.get("end") or ""))
        absolute_start = delivery_start_ms + relative_start
        absolute_end = delivery_start_ms + relative_end
        if (
            absolute_end < int(pending_row["matched_start_ms"]) - 500
            or absolute_start > int(pending_row["matched_end_ms"]) + 500
        ):
            return False
        pending_row["reconciliation_status"] = "APPLIED_AND_HASH_VERIFIED"
        pending_row["text_override_decision_index"] = match_index
        verification_start = absolute_start
        verification_end = absolute_end
        if chat_scaffold:
            # The exact platform message may span adjacent subtitle cues.  The
            # full read window, not only the cue whose entity slot changed,
            # owns the final grammar scaffold (including edge particles such
            # as ``吗`` and prefixes such as ``还没看``).
            verification_start = int(pending_row["matched_start_ms"])
            verification_end = int(pending_row["matched_end_ms"])
        repaired.append(
            {
                "evidence_id": evidence_id,
                "mode": (
                    "entity_only_human_text_override"
                    if minimal
                    else "chat_scaffold_plus_human_entity_override"
                ),
                "expected_entity": expected,
                "cue_indexes": [int(source_cue.get("source_index") or 0)],
                "matched_start_ms": verification_start,
                "matched_end_ms": verification_end,
                "override_cue_start_ms": absolute_start,
                "override_cue_end_ms": absolute_end,
                "before": [before],
                "after": [after],
                "structured_exact_text": structured_expectation or None,
                "verdict": verdict,
                "text_override_document_sha256": document_hash,
                "survived": True,
            }
        )
    audit.setdefault("entity_repairs", []).extend(repaired)
    audit["pending_text_override_reconciliation"] = {
        "status": "APPLIED_AND_HASH_VERIFIED",
        "override_document_sha256": document_hash,
        "source_srt_sha256": source_hash,
        "final_srt_sha256": final_hash,
        "repaired_count": len(repaired),
    }
    audit["status"] = "APPLIED_AND_VERIFIED"
    return True


def _validated_read_aloud_verdict(
    verdict: Mapping[str, Any] | None,
    *,
    request: Mapping[str, Any],
) -> dict[str, Any] | None:
    """近失配念读仲裁 verdict：只接受绑定本 request、在两个候选文本之内、
    高置信的 RESOLVED；其余一律当 UNCERTAIN（不改字幕）。"""
    if not isinstance(verdict, Mapping):
        return None
    row = dict(verdict)
    if row.get("schema_version") != "chat-entity-verdict.v1":
        return None
    if row.get("request_sha256") != request.get("request_sha256"):
        return None
    if row.get("status") != "RESOLVED":
        return None
    allowed = {
        str(candidate.get("canonical") or "")
        for candidate in request.get("candidate_entities", ())
        if isinstance(candidate, Mapping)
    }
    if row.get("canonical_entity") not in allowed:
        return None
    confidence = row.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or confidence < 0.80
    ):
        return None
    return row


def _validated_entity_verdict(
    verdict: Mapping[str, Any] | None,
    *,
    request: Mapping[str, Any],
    group: ReferentGroup,
) -> dict[str, Any] | None:
    if not isinstance(verdict, Mapping):
        return None
    row = dict(verdict)
    if row.get("schema_version") != "chat-entity-verdict.v1":
        return None
    if row.get("request_sha256") != request.get("request_sha256"):
        return None
    if row.get("status") not in {"RESOLVED", "UNCERTAIN"}:
        return None
    if row.get("status") == "UNCERTAIN":
        return row
    canonicals = {entity.canonical for entity in group.entities}
    if row.get("canonical_entity") not in canonicals:
        return None
    authority_kind = row.get("authority_kind")
    if authority_kind == "audio_forced_choice":
        required_hashes = (
            "source_media_sha256",
            "audio_clip_sha256",
            "prompt_sha256",
            "response_sha256",
        )
        if not all(_valid_sha256(row.get(key)) for key in required_hashes):
            return None
        confidence = row.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or confidence < 0.80
        ):
            return {**row, "status": "UNCERTAIN", "reason_code": "ENTITY_AUDIO_CONFIDENCE_LOW"}
    elif authority_kind == "ivan_text_override":
        if not row.get("defer_to_text_override"):
            return None
        override_schema_version = int(row.get("override_schema_version") or 1)
        binding_keys = {
            1: ("source_srt_sha256", "text_final_srt_sha256"),
            2: (
                "source_cue_witness_sha256",
                "decision_output_witness_sha256",
            ),
            3: (
                "source_cue_witness_sha256",
                "decision_output_witness_sha256",
            ),
        }.get(override_schema_version)
        if binding_keys is None or not all(
            _valid_sha256(row.get(key)) for key in ("override_document_sha256", *binding_keys)
        ):
            return None
        if not str(row.get("candidate_id") or ""):
            return None
    else:
        return None
    return row


def load_chat_jsonl(
    path: str | Path,
    *,
    recording_start_ms: int | None = None,
) -> list[ChatEvidence]:
    """Load exact danmaku, SC, gift, and guard evidence from recorder JSONL."""
    source = Path(path)
    if not source.is_file():
        return []
    parsed: list[tuple[int, str, str, str, str, bool]] = []
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    earliest: int | None = None
    for raw_line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(raw_line)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        command = str(payload.get("cmd") or "")
        event_ms = _send_time_ms(payload, command)
        if event_ms is None:
            continue
        earliest = event_ms if earliest is None else min(earliest, event_ms)
        if command.startswith("DANMU_MSG"):
            text = _danmaku_text(payload)
            if text:
                parsed.append((event_ms, "danmaku", "", text, "", False))
        elif command.startswith("SUPER_CHAT_MESSAGE"):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            text = data.get("message")
            user_info = data.get("user_info") if isinstance(data.get("user_info"), dict) else {}
            sender = user_info.get("uname") or ""
            if isinstance(text, str) and text.strip():
                event_id = data.get("id") or payload.get("msg_id") or ""
                precise = (
                    isinstance(payload.get("send_time"), (int, float))
                    and payload["send_time"] >= 100_000_000_000
                )
                parsed.append(
                    (
                        event_ms,
                        "superchat",
                        str(sender).strip(),
                        text.strip(),
                        str(event_id),
                        precise,
                    )
                )
        elif command in ("SEND_GIFT", "COMBO_SEND"):
            # Sender is masked, but giftName remains exact structured evidence.
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            gift_name = data.get("giftName")
            sender = data.get("uname") or ""
            if isinstance(gift_name, str) and gift_name.strip():
                parsed.append((event_ms, "gift", str(sender).strip(), gift_name.strip(), "", False))
        elif command.startswith("GUARD_BUY"):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            sender = data.get("username") or data.get("uname") or ""
            uid = data.get("uid")
            try:
                guard_level = int(data.get("guard_level"))
            except (TypeError, ValueError):
                continue
            level_name = {1: "总督", 2: "提督", 3: "舰长"}.get(guard_level, "")
            gift_name = data.get("gift_name") or data.get("giftName") or level_name
            valid_uid = (
                not isinstance(uid, bool) and isinstance(uid, (int, str)) and bool(str(uid).strip())
            )
            if not (
                isinstance(sender, str)
                and sender.strip()
                and level_name
                and valid_uid
                and isinstance(gift_name, str)
                and gift_name.strip()
            ):
                continue
            parsed.append(
                (event_ms, "guard", sender.strip(), gift_name.strip(), str(uid).strip(), False)
            )
    base = recording_start_ms if recording_start_ms is not None else earliest
    if base is None:
        return []
    # Prefer precise CN twins; distinct nonempty SC ids never collapse.
    deduped: list[tuple[int, str, str, str, str, bool]] = []
    for row in sorted(parsed):
        event_ms, kind, sender, text, event_id, precise = row
        twin_index = None
        if kind == "superchat":
            for index in range(len(deduped) - 1, -1, -1):
                prior_ms, prior_kind, prior_sender, prior_text, prior_id, prior_precise = deduped[
                    index
                ]
                if event_ms - prior_ms > 2_000:
                    break
                sender_compatible = prior_sender == sender or not prior_sender or not sender
                if prior_kind != kind or not sender_compatible or prior_text != text:
                    continue
                if prior_id and event_id and prior_id != event_id:
                    continue
                twin_index = index
                if (precise and not prior_precise) or (not prior_sender and bool(sender)):
                    deduped[index] = row
                break
        if twin_index is None:
            deduped.append(row)

    out: list[ChatEvidence] = []
    seen: set[tuple[str, str, str, int]] = set()
    for event_ms, kind, sender, text, event_id, _precise in sorted(deduped):
        key = (kind, sender, text, event_ms)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            ChatEvidence(
                kind,
                event_ms - base,
                text,
                sender,
                str(source),
                source_sha256,
                event_id,
            )
        )
    return out


def _srt_timestamp(ms: int) -> str:
    hours, rem = divmod(max(0, int(ms)), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _render_srt(cues: Sequence[object], texts: Sequence[str]) -> str:
    blocks = []
    for index, (cue, text) in enumerate(zip(cues, texts), start=1):
        blocks.append(
            f"{index}\n{_srt_timestamp(cue.start_ms)} --> {_srt_timestamp(cue.end_ms)}\n{text.strip()}"
        )
    return "\n\n".join(blocks) + "\n"
