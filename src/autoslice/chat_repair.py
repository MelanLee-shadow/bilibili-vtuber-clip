"""Deterministic chat/entity repair primitives."""

from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache, partial
import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence

from src.autoslice.chat_alignment_context import (
    context_owned_internal_gap_rebase,
    fragment_spoken_in as _fragment_spoken_in,
    normalize_with_map,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.cue_split_hygiene import (
    _shift_boundary_punct as _shift_boundary_punct,
    _snap_split_to_punct as _snap_split_to_punct,
)
from src.autoslice.chat_evidence import (
    ChatEvidence,
    EntityVerifier,
    ReferentGroup,
    _QUESTION_TAIL,
    _coerce_referent_groups,
    _entity_occurrences,
    _render_srt,
    _request_sha256,
    _validated_entity_verdict,
    _validated_read_aloud_verdict,
    normalize_chat_text,
)
from src.autoslice.chat_span_alignment import (
    strip_interjections_once as _strip_interjections_once,  # noqa: F401 - compatibility re-export
)

_norm_with_map = partial(normalize_with_map, normalize=normalize_chat_text)

_THANK_PREFIX_PATTERN = r"(?:谢谢|感谢|谢)(?:一下)?"
_THANK_ACTION_SUFFIX_PATTERN = (
    r"(?:送的|的\s*SC|的醒目留言|的双目钢镚|的钢镚|的光棒)"
)
_LEADING_THANK_SENDER_ACTION = re.compile(
    rf"^(?P<head>{_THANK_PREFIX_PATTERN}"
    rf"(?P<name>[^，。！？!?\s]{{1,32}}?)"
    rf"(?P<suffix>{_THANK_ACTION_SUFFIX_PATTERN}|的(?:舰长|提督|总督))"
    r"[，,。！？!?\s]*)",
    re.IGNORECASE,
)


def registered_entity_names(groups: Sequence[ReferentGroup | Sequence[str]]) -> set[str]:
    """Lower-cased canonicals+surfaces of every registered (graph/static) entity."""

    names: set[str] = set()
    for group in _coerce_referent_groups(groups):
        for entity in group.entities:
            for value in (entity.canonical, *entity.surfaces):
                text = str(value).strip().lower()
                if text:
                    names.add(text)
    return names


def revert_unregistered_entity_repairs(
    srt_text: str,
    entity_repairs: Sequence[Mapping[str, Any]],
    registered_names: set[str],
) -> tuple[str, list[dict[str, Any]]]:
    """An entity repair may only land on a REGISTERED entity name.

    The repetition/divergence compiler builds ad-hoc confusable groups out of
    raw draft fragments, so a nondeterministic ASR roll can elect a mishearing
    (练死) or a plain function word (到时) as the "winning canonical" and
    rewrite a correct registered entity away (2026-07-14 恋青→到时 case).
    Fail-safe: revert any repair whose expected entity is not a registered
    graph/static entity name, mark the row, and disclose.
    """

    lowered = {name.lower() for name in registered_names}
    disclosures: list[dict[str, Any]] = []
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    rewritten = False
    for row in entity_repairs:
        if not isinstance(row, dict) or row.get("reconciliation"):
            continue
        expected = str(row.get("expected_entity") or row.get("resolved_canonical") or "")
        if not expected or expected.strip().lower() in lowered:
            continue
        row_indexes = list(row.get("cue_indexes") or [])
        befores = list(row.get("before") or [])
        afters = list(row.get("after") or [])
        reverted_cues: list[int] = []
        for position, index in enumerate(row_indexes):
            if not (0 < int(index) <= len(texts) and position < len(befores)):
                continue
            if position < len(afters) and texts[int(index) - 1] == str(afters[position]):
                texts[int(index) - 1] = str(befores[position])
                reverted_cues.append(int(index))
                rewritten = True
        row["reconciliation"] = "UNREGISTERED_ENTITY_REVERTED"
        disclosures.append(
            {
                "cue_indexes": row_indexes,
                "expected_entity": expected,
                "reverted_cues": reverted_cues,
            }
        )
    if rewritten:
        srt_text = _render_srt(cues, texts)
    return srt_text, disclosures


def reconcile_contradictory_entity_repairs(
    srt_text: str,
    entity_repairs: Sequence[Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Same-slot contradictory RESOLVED verdicts are untrustworthy evidence.

    Two entity repairs whose cue sets intersect but whose expected entities
    differ mean the raw-audio ear returned confident opposite answers about
    the same speech (2026-07-14 恋死/恋青/练死 case: "clearly lian qing twice"
    and "clearly lian si twice" over one window).  Last-writer-wins is never
    acceptable there: restore each disputed cue to the earliest repair's
    pre-repair text (closest to the independent BCUT witness) and disclose.
    Rows touching a disputed cue are marked so the final-surface gate stops
    requiring their (mutually exclusive) outcomes to survive.
    """

    rows = [
        row
        for row in entity_repairs
        if isinstance(row, dict) and not row.get("reconciliation")
    ]
    by_cue: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        for index in row.get("cue_indexes") or []:
            by_cue.setdefault(int(index), []).append(row)
    disputed: dict[int, list[dict[str, Any]]] = {}
    for index, cue_rows in by_cue.items():
        expected = {
            str(r.get("expected_entity") or r.get("resolved_canonical") or "")
            for r in cue_rows
        }
        if len(expected - {""}) > 1:
            disputed[index] = cue_rows
    if not disputed:
        return srt_text, []

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    disclosures: list[dict[str, Any]] = []
    for index in sorted(disputed):
        cue_rows = disputed[index]
        earliest = cue_rows[0]
        try:
            before_text = str(
                (earliest.get("before") or [])[list(earliest.get("cue_indexes") or []).index(index)]
            )
        except (ValueError, IndexError):
            before_text = ""
        reverted_to = None
        if 0 < index <= len(texts):
            known_afters = set()
            for r in cue_rows:
                row_indexes = list(r.get("cue_indexes") or [])
                if index in row_indexes:
                    afters = r.get("after") or []
                    position = row_indexes.index(index)
                    if position < len(afters):
                        known_afters.add(str(afters[position]))
            if before_text and texts[index - 1] in known_afters:
                texts[index - 1] = before_text
                reverted_to = before_text
        for row in cue_rows:
            row["reconciliation"] = "CONTRADICTORY_VERDICTS_REVERTED"
        disclosures.append(
            {
                "cue_index": index,
                "expected_entities": sorted(
                    {
                        str(r.get("expected_entity") or r.get("resolved_canonical") or "")
                        for r in cue_rows
                    }
                    - {""}
                ),
                "reverted_to": reverted_to,
            }
        )
    if any(row["reverted_to"] is not None for row in disclosures):
        srt_text = _render_srt(cues, texts)
    return srt_text, disclosures


def _match_metrics(authority: str, candidate: str) -> tuple[float, float, float, float, int]:
    left, right = normalize_chat_text(authority), normalize_chat_text(candidate)
    if not left or not right:
        return 0.0, 0.0, 0.0, 0.0, 0
    matcher = SequenceMatcher(None, left, right)
    common = sum(block.size for block in matcher.get_matching_blocks())
    coverage = common / len(left)
    precision = common / len(right)
    ratio = matcher.ratio()
    score = 0.50 * ratio + 0.35 * coverage + 0.15 * precision
    return score, ratio, coverage, precision, common


def _best_text_split(authority: str, cue_texts: Sequence[str]) -> list[str]:
    """Split exact source text across an already-authoritative cue timeline."""

    if len(cue_texts) <= 1:
        return [authority]
    length = len(authority)

    @lru_cache(maxsize=None)
    def solve(part: int, start: int) -> tuple[float, tuple[str, ...]]:
        remaining = len(cue_texts) - part
        if remaining == 1:
            segment = authority[start:]
            return SequenceMatcher(None, normalize_chat_text(segment), normalize_chat_text(cue_texts[part])).ratio(), (segment,)
        best = (-1.0, tuple())
        for end in range(start + 1, length - remaining + 2):
            segment = authority[start:end]
            local = SequenceMatcher(None, normalize_chat_text(segment), normalize_chat_text(cue_texts[part])).ratio()
            tail_score, tail = solve(part + 1, end)
            candidate = (local + tail_score, (segment, *tail))
            if candidate[0] > best[0]:
                best = candidate
        return best

    return list(solve(0, 0)[1])


_EMOTE_PLACEHOLDER_RUN = re.compile(r"[；;]{2,}")


def _strip_unrenderable_for_subtitle(text: str) -> str:
    """SC/弹幕原文里的表情符号在字幕字体下渲染成乱码（2026-07-10 伊依 SC 实案：
    平台把 emote 记成「；；」占位、颜文字用生僻区字符）。拼进字幕前剥离：
    ①两个以上连续分号的 emote 占位串→顿号化为一个停顿；②Symbol/emoji/私有区
    及 BMP 外非 CJK 字符丢弃。证据匹配仍用原文（本函数只作用于写入字幕的文本）。"""
    import unicodedata

    cleaned = _EMOTE_PLACEHOLDER_RUN.sub("，", text)
    out_chars: list[str] = []
    for char in cleaned:
        code = ord(char)
        if code > 0xFFFF and not (0x20000 <= code <= 0x2FA1F):  # 保留 CJK 扩展
            continue
        if 0x1400 <= code <= 0x167F:  # UCAS 颜文字（ᗜ 类）
            continue
        category = unicodedata.category(char)
        if category in {"So", "Sk", "Cs", "Co"}:
            continue
        out_chars.append(char)
    result = "".join(out_chars)
    result = re.sub(r"，{2,}", "，", result)
    return result.strip("，, ") or text


def _excess_is_mid_read_interjection(authority: str, candidate: str) -> bool:
    """span 比 authority 长时，多出的部分是否全部是念读**中途**的插话。

    头/尾悬出仍然算吞邻句（长度守卫继续拦）；只有 authority 覆盖率高、且
    多出的字都落在 authority 两个匹配块之间零缺字的间隙里（她停下来回应
    「谢谢你」再继续念）才放行。"""
    auth_norm = normalize_chat_text(authority)
    span_norm = normalize_chat_text(candidate)
    if not auth_norm or not span_norm:
        return False
    blocks = [
        block
        for block in SequenceMatcher(None, auth_norm, span_norm).get_matching_blocks()
        if block.size
    ]
    while len(blocks) > 1 and blocks[0].size < 2:
        blocks.pop(0)
    while len(blocks) > 1 and blocks[-1].size < 2:
        blocks.pop()
    if not blocks:
        return False
    common = sum(block.size for block in blocks)
    if common / len(auth_norm) < 0.82:
        return False
    head_overhang = blocks[0].b
    tail_overhang = len(span_norm) - (blocks[-1].b + blocks[-1].size)
    if head_overhang > 1 or tail_overhang > 1:
        return False
    return True


def _aligned_span_replacements(
    authority: str,
    before: Sequence[str],
    *,
    prev_context: str = "",
    next_context: str = "",
) -> tuple[list[str], dict] | None:
    """Verbatim-splice the authority into the span WITHOUT destroying real
    speech around it (2026-07-11 乐队番实案：整段覆盖把主播自己的「算什么」
    吃掉、把她在上一条 cue 已说过的「好冷的笑话」重复注入)。

    规则（对齐 = SequenceMatcher matching blocks on normalized text）：
    - span 首 cue 的未对齐开头 / 末 cue 的未对齐结尾，只有当 authority 对应侧
      已完全消耗时才是「念读之外的真实语音」，原样保留；否则属于听错替换区，
      用 authority 对应侧覆盖。
    - authority 的未对齐开头/结尾若已在相邻 cue 出现过（她刚说过/接着说），
      不重复注入。
    返回 (replacements, audit) 或 None（对齐太弱，调用方回退旧行为）。
    """
    auth_norm, auth_map = normalize_with_map(authority, normalize_chat_text)
    span_raw = "".join(before)
    # A structured SC body never owns the acoustic “谢谢+发送者+动作” head that
    # precedes it.  Aligning “姐姐大人…” against “…光棒，姐大人…” otherwise
    # treats the missing first “姐” as an authority-head substitution and
    # deletes the real thanks.  Remove this narrow grammar from the alignment
    # domain, then prepend it unchanged after the body splice.
    protected_thank_head = ""
    acoustic_thank = _LEADING_THANK_SENDER_ACTION.match(span_raw)
    authority_thank = _LEADING_THANK_SENDER_ACTION.match(authority)
    if acoustic_thank is not None and authority_thank is None:
        protected_thank_head = acoustic_thank.group("head")
        span_raw = span_raw[len(protected_thank_head) :]
    span_norm, span_map = normalize_with_map(span_raw, normalize_chat_text)
    if not auth_norm or not span_norm:
        return None
    blocks = [
        block
        for block in SequenceMatcher(None, auth_norm, span_norm).get_matching_blocks()
        if block.size
    ]
    # 首尾锚块至少 2 字符：孤字块（「唱不了」里的「了」偶然对上 authority 尾部
    # 的「了」）会把对齐边界拖到错误位置，吃掉真实回话。
    while len(blocks) > 1 and blocks[0].size < 2:
        blocks.pop(0)
    while len(blocks) > 1 and blocks[-1].size < 2:
        blocks.pop()
    if not blocks or (len(blocks) == 1 and blocks[0].size < 2):
        return None
    common = sum(block.size for block in blocks)
    if common / len(auth_norm) < 0.5:
        return None

    # Fuzzy prefix ownership can silently lose polarity words such as “不是”.
    context_head_rebase, context_owned_gap = (
        context_owned_internal_gap_rebase(
            authority,
            auth_map,
            blocks,
            prev_context,
            normalize=normalize_chat_text,
            fragment_spoken_in=_fragment_spoken_in,
        )
    )
    if context_head_rebase:
        blocks = blocks[context_head_rebase:]

    a_lo, a_hi = blocks[0].a, blocks[-1].a + blocks[-1].size
    s_lo, s_hi = blocks[0].b, blocks[-1].b + blocks[-1].size

    _spoken_nearby = _fragment_spoken_in

    audit: dict = {}
    if context_owned_gap:
        audit["context_owned_internal_authority_gap"] = context_owned_gap
    if protected_thank_head:
        audit["preserved_thank_sender_action_head"] = protected_thank_head
    # raw 边界：对齐区两端顶到 raw 端点，normalize 后不可见的首尾字符
    # （空格、箭头等 sanitizer 产物）跟随对齐区，不算「未对齐头尾」。
    auth_raw_lo = 0 if a_lo == 0 else auth_map[a_lo]
    auth_raw_hi = len(authority) if a_hi == len(auth_norm) else auth_map[a_hi - 1] + 1
    span_raw_lo = 0 if s_lo == 0 else span_map[s_lo]
    span_raw_hi = len(span_raw) if s_hi == len(span_norm) else span_map[s_hi - 1] + 1
    auth_head_raw = authority[:auth_raw_lo]
    auth_tail_raw = authority[auth_raw_hi:]
    span_head_raw = span_raw[:span_raw_lo]
    span_tail_raw = span_raw[span_raw_hi:]

    head = ""
    if s_lo and a_lo == 0 and len(normalize_chat_text(span_head_raw)) >= 2:
        # authority 从头就对齐，span 开头是念读之外的真实语音（谢谢+sender 等）
        head = span_head_raw
        audit["preserved_span_head"] = span_head_raw
    elif a_lo:
        if _spoken_nearby(auth_head_raw, prev_context):
            audit["dropped_duplicate_authority_head"] = auth_head_raw
            # Exact context ownership protects even a one-character hesitation.
            min_span_head_length = 1 if context_head_rebase else 2
            if s_lo and len(normalize_chat_text(span_head_raw)) >= min_span_head_length:
                head = span_head_raw
                audit["preserved_span_head"] = span_head_raw
        else:
            head = auth_head_raw
    tail = ""
    if s_hi < len(span_norm) and a_hi == len(auth_norm) and len(normalize_chat_text(span_tail_raw)) >= 2:
        # authority 已全部消耗，span 结尾是主播自己的后续（「算什么」）
        tail = span_tail_raw
        audit["preserved_span_tail"] = span_tail_raw
    elif a_hi < len(auth_norm):
        substituted_tail = "" if _spoken_nearby(auth_tail_raw, next_context) else auth_tail_raw
        if not substituted_tail:
            audit["dropped_duplicate_authority_tail"] = auth_tail_raw
        # 替换区尾部若有强标点边界，标点后的是 ASR 并进同 cue 的真实回话
        # （「唱不|同的，我唱不了高音」）：只替换标点前的听错段，回话保留。
        reply_match = re.search(r"[，。！？!?…]", span_tail_raw)
        if reply_match is not None:
            reply = span_tail_raw[reply_match.start() :].lstrip("，,。！？!? ")
            if len(normalize_chat_text(reply)) >= 2:
                tail = f"{substituted_tail}，{reply}" if substituted_tail else reply
                audit["preserved_span_reply"] = reply
            else:
                tail = substituted_tail
        else:
            tail = substituted_tail
    # 对齐区内部按块交错重建：authority 两块之间若无缺字（a_gap==0）而 span
    # 多出 ≥2 字，那是她念读中途的插话（「谢谢你」等，2026-07-10 伊依 SC 实案）
    # ——原样保留；authority 有缺字的间隙是听错替换区，用 authority 原文覆盖。
    aligned_parts: list[str] = []
    for index, block in enumerate(blocks):
        block_a_lo = auth_raw_lo if index == 0 else auth_map[block.a]
        block_a_hi = auth_raw_hi if index == len(blocks) - 1 else auth_map[block.a + block.size - 1] + 1
        if index:
            prev = blocks[index - 1]
            prev_a_hi = auth_map[prev.a + prev.size - 1] + 1
            prev_s_hi = span_map[prev.b + prev.size - 1] + 1
            a_gap_norm = block.a - (prev.a + prev.size)
            span_gap_raw = span_raw[prev_s_hi : span_map[block.b]]
            if a_gap_norm == 0 and len(normalize_chat_text(span_gap_raw)) >= 2:
                aligned_parts.append(span_gap_raw)
                audit.setdefault("preserved_span_interjections", []).append(span_gap_raw)
            else:
                aligned_parts.append(authority[prev_a_hi : auth_map[block.a]])
        aligned_parts.append(authority[block_a_lo:block_a_hi])
    aligned_raw = "".join(aligned_parts)
    # Strip only the leading tail closers needed to balance this splice.
    if aligned_raw.endswith("》") and tail.startswith("》"):
        combined = f"{head}{aligned_raw}{tail}"
        surplus = combined.count("》") - combined.count("《")
        leading_closers = len(tail) - len(tail.lstrip("》"))
        if 0 < surplus <= leading_closers:
            candidate = f"{head}{aligned_raw}{tail[surplus:]}"
            if candidate.count("》") == candidate.count("《"):
                tail = tail[surplus:]
                audit["deduplicated_title_close_at_splice"] = surplus
    desired = _strip_unrenderable_for_subtitle(
        f"{protected_thank_head}{head}{aligned_raw}{tail}"
    )
    if not normalize_chat_text(desired):
        return None
    replacements = _shift_boundary_punct(_best_text_split(desired, list(before)))
    if len(replacements) != len(before):
        return None
    return replacements, audit


def _partial_question_patch(authority: str, candidate: str) -> str | None:
    """Restore a dropped final particle without deleting a same-cue reply."""

    left = normalize_chat_text(authority)
    right = normalize_chat_text(candidate)
    if len(left) < 4 or not left or left[-1] not in _QUESTION_TAIL:
        return None
    prefix = 0
    for expected, actual in zip(left, right):
        if expected != actual:
            break
        prefix += 1
    if prefix < 3 or prefix != len(left) - 1 or right.startswith(left):
        return None
    # Find the equivalent normalized prefix in the original candidate.  This
    # keeps punctuation and the reply that ASR merged into the same cue.
    consumed = 0
    cut = 0
    for cut, char in enumerate(candidate, start=1):
        if normalize_chat_text(char):
            consumed += 1
        if consumed >= prefix:
            break
    suffix = candidate[cut:].lstrip("，,。！？!? ")
    return authority + (f"，{suffix}" if suffix else "")


def _matched_read_prefix(
    authority: str,
    candidate: str,
    *,
    cue_boundaries: Iterable[int] = (),
) -> tuple[str, str] | None:
    """Split a fuzzy read prefix from an acoustic same-cue reply.

    Returning ``None`` means there is no defensible suffix boundary.  This is
    preferable to deleting a reply merely because the combined cue happened to
    clear a fuzzy whole-cue similarity threshold.
    """

    authority_norm = normalize_chat_text(authority)
    candidate_norm = normalize_chat_text(candidate)
    if len(candidate_norm) <= len(authority_norm) + 1:
        return None
    best: tuple[float, str, str] | None = None
    structural_cuts = set(cue_boundaries)
    punctuation = "，,。！？!?；;：:"
    for cut in range(1, len(candidate)):
        # A fuzzy maximum inside a word is not a defensible read/reply
        # boundary.  In particular, `唱不同的，我...` used to be cut after
        # `唱不`, leaving the fabricated suffix `同的，我...` behind after the
        # exact SC body was restored.  Preserve a suffix only at an original
        # cue edge or a visible clause boundary.
        structural = (
            cut in structural_cuts
            or candidate[cut - 1] in punctuation
            or candidate[cut] in punctuation
        )
        prefix, suffix = candidate[:cut], candidate[cut:].lstrip("，,。！？!? ")
        prefix_extent = len(normalize_chat_text(prefix)) / max(1, len(authority_norm))
        score, _ratio, coverage, precision, common = _match_metrics(authority, prefix)
        if not structural and not (
            prefix_extent >= 0.88
            and coverage >= 0.72
            and precision >= 0.75
        ):
            # A punctuation-free ASR merge can still have a defensible split
            # when the prefix covers almost the entire message.  This admits
            # `...恋死我自己...` after the near-complete read, but rejects the
            # old mid-word `...唱不|同的...` cut.
            continue
        if len(normalize_chat_text(suffix)) < 2:
            continue
        if (
            score < 0.68
            or coverage < 0.60
            or precision < 0.52
            or common < min(6, len(authority_norm))
        ):
            continue
        candidate_row = (score, prefix, suffix)
        if best is None or candidate_row[0] > best[0]:
            best = candidate_row
    return (best[1], best[2]) if best is not None else None


def _authority_tail_continues_in_next_cue(
    authority: str,
    candidate: str,
    next_cue: str,
) -> bool:
    """Detect an exact message tail split across the next subtitle cue.

    A nearly complete first cue must not win merely because it clears fuzzy
    thresholds: replacing it with the full message would duplicate the last
    particle/character that already opens the following cue.
    """

    authority_norm = normalize_chat_text(authority)
    candidate_norm = normalize_chat_text(candidate)
    next_norm = normalize_chat_text(next_cue)
    if not candidate_norm or not authority_norm.startswith(candidate_norm):
        return False
    tail = authority_norm[len(candidate_norm) :]
    return bool(tail and next_norm.startswith(tail))


def _spoken_sender_alias(sender: str) -> str:
    value = sender.strip()
    if not value:
        return ""
    # Do not equate “first Han run” with “spoken name”: punctuation and kana
    # are legitimate username characters (寒-歌、小凑るう子).  Only explicitly
    # registered display tags are non-spoken; this preserves the historical
    # 十麻乃orient behavior without truncating arbitrary mixed-script names.
    for suffix in ("orient",):
        if value.lower().endswith(suffix):
            prefix = value[: -len(suffix)]
            if re.fullmatch(r"[\u3400-\u9fff]+", prefix):
                return prefix
    return value


_THANK_NAME = re.compile(
    rf"(?P<prefix>{_THANK_PREFIX_PATTERN})"
    r"(?P<name>[^，。！？!?\s]{1,32}?)"
    rf"(?P<suffix>{_THANK_ACTION_SUFFIX_PATTERN})",
    re.IGNORECASE,
)


def _mask_compatible_sender(full_sender: str, masked_sender: str) -> bool:
    """平台把弹幕发送者打码成「首字+***」；与 SC 的完整发送者名做兼容匹配。"""
    if not full_sender or not masked_sender:
        return False
    if full_sender == masked_sender:
        return True
    if masked_sender.endswith("***"):
        prefix = masked_sender[:-3]
        return bool(prefix) and full_sender.startswith(prefix)
    return False


def _sender_thank_anchor(text: str, sender: str) -> bool:
    """该字幕行是否在答谢这位 SC 发送者（听岔的名字也算，发送者名是强键）。"""
    alias = _spoken_sender_alias(sender)
    if not alias:
        return False
    match = _THANK_NAME.search(text)
    if match is None:
        return False
    heard = match.group("name")
    if alias.lower() == heard.lower():
        return True
    score, _ratio, coverage, _precision, _common = _match_metrics(alias, heard)
    return score >= 0.5 or coverage >= 0.6


def _repair_sc_sender(text: str, sender: str) -> str | None:
    alias = _spoken_sender_alias(sender)
    if not alias:
        return None
    match = _THANK_NAME.search(text)
    if match is None or alias.lower() == match.group("name").lower():
        return None
    return text[: match.start("name")] + alias + text[match.end("name") :]


_SC_ACTION_SENDER = re.compile(
    r"^(?P<name>[^，。！？!?\s]{1,10}?)"
    r"(?P<action>(?:SC|sc)(?:啊)?|(?:苏|斯)(?:恰|擦)(?:啊)?)"
)


def _repair_sc_action_sender(text: str, sender: str) -> str | None:
    """Repair a spoken sender only when the same cue explicitly says SC.

    The platform sender does not prove that a name was spoken.  A matched SC
    body plus an acoustic action anchor (``SC`` or a bounded ASR rendering such
    as ``苏恰``) does prove the narrow sender slot.  Display suffixes such as
    ``orient`` are intentionally stripped by ``_spoken_sender_alias``.
    """

    alias = _spoken_sender_alias(sender)
    match = _SC_ACTION_SENDER.search(text)
    if not alias or match is None:
        return None
    heard_name = match.group("name")
    action = match.group("action")
    canonical_action = "SC啊" if action.lower().endswith("啊") else "SC"
    replacement = alias + canonical_action
    if heard_name.lower() == alias.lower() and action == canonical_action:
        return None
    return replacement + text[match.end() :]


# Gift-thanks grammar is looser than the SC thank-name grammar above: there is
# no fixed suffix keyword (「送的」/「的SC」), just "谢(谢)?...的<TAIL>" where
# TAIL is whatever ASR heard as the gift name, up to the end of the cue (minus
# trailing punctuation).  ``name`` is only the text between the thanks marker
# and the last 「的」 — it is never a gift name candidate, only used for the
# informational sender-first-char check (the platform masks gift senders, so
# the name itself cannot be repaired).
_GIFT_THANK_CUE = re.compile(
    r"(?:谢谢|感谢|谢)(?P<name>.+)的(?P<tail>[^，。！？!?\s]+)[，。！？!?\s]*$"
)

GIFT_ARBITRATION_WINDOW_BEFORE_MS = 5_000
GIFT_ARBITRATION_WINDOW_AFTER_MS = 120_000
GIFT_ARBITRATION_CAP_PER_CLIP = 2


def _gift_thank_cue_match(text: str) -> tuple[str, str] | None:
    match = _GIFT_THANK_CUE.search(text)
    if match is None:
        return None
    return match.group("name"), match.group("tail")


def _apply_gift_name_repairs(
    evidence: Sequence[ChatEvidence],
    cues: Sequence[Any],
    texts: list[str],
    *,
    entity_verifier: EntityVerifier | None,
) -> list[dict[str, Any]]:
    """礼物答谢的礼物名必须来自结构化 SEND_GIFT/COMBO_SEND 事件，不许靠听——
    平台把赠送者昵称脱敏成"某***"（uid 也是 0），礼物名字段本身不脱敏。真实
    2026-07-10 案例：她念读被 ASR 听成「谢谢有人看到你的人鱼」，结构化事件里
    giftName 是「流星雨」。只对念读 cue 里「谢(谢)?…的<TAIL>」语法抓到的
    TAIL，且 TAIL 既不等于 giftName、也不是别处 SC/弹幕的真实原文（避免误伤
    真实朗读）时，才把两者交给原始音频做二选一仲裁；只有 RESOLVED 且置信度
    ≥0.80 才落地这一处最小文本替换。每个切片最多仲裁
    ``GIFT_ARBITRATION_CAP_PER_CLIP`` 次，用本函数自己的计数器（不与近失配
    弹幕仲裁的计数器共用）。赠送者名字首字核对纯记录，不驱动任何文本改动。
    """

    gift_events = [item for item in evidence if item.kind == "gift" and item.text.strip()]
    if not gift_events:
        return []
    other_texts = [
        normalize_chat_text(item.text)
        for item in evidence
        if item.kind in ("superchat", "danmaku") and item.text.strip()
    ]
    rows: list[dict[str, Any]] = []
    attempts = 0
    for item in gift_events:
        gift_name = item.text.strip()
        window_lo = item.offset_ms - GIFT_ARBITRATION_WINDOW_BEFORE_MS
        window_hi = item.offset_ms + GIFT_ARBITRATION_WINDOW_AFTER_MS
        for index, cue in enumerate(cues):
            if not (window_lo <= cue.start_ms <= window_hi):
                continue
            match = _gift_thank_cue_match(texts[index])
            if match is None:
                continue
            name_slot, tail = match
            tail_norm = normalize_chat_text(tail)
            if not tail_norm or tail_norm == normalize_chat_text(gift_name):
                continue  # ASR already got the gift name right; nothing to repair.
            if any(tail_norm in other_norm for other_norm in other_texts):
                continue  # matches a genuine SC/danmaku read, not this gift's name.
            spoken_alias = _spoken_sender_alias(item.sender)
            sender_first_char_match = bool(spoken_alias) and bool(name_slot) and (
                name_slot[0] == spoken_alias[0]
            )
            row: dict[str, Any] = {
                "evidence_id": item.evidence_id,
                "source_event_id": item.source_event_id,
                "gift_name": gift_name,
                "sender": item.sender,
                "cue_index": index + 1,
                "matched_start_ms": cue.start_ms,
                "matched_end_ms": cue.end_ms,
                "before": texts[index],
                "asr_tail": tail,
                "sender_first_char_match": sender_first_char_match,
            }
            if entity_verifier is None:
                row["after"] = texts[index]
                row["outcome"] = "no_verifier_no_change"
                rows.append(row)
                break
            if attempts >= GIFT_ARBITRATION_CAP_PER_CLIP:
                row["after"] = texts[index]
                row["outcome"] = "gift_arbitration_cap_exceeded_no_change"
                rows.append(row)
                break
            attempts += 1
            evidence_id = hashlib.sha256(
                (
                    f"gift-name\0{item.evidence_id}\0{index}\0{cue.start_ms}\0{cue.end_ms}\0{tail}"
                ).encode("utf-8")
            ).hexdigest()
            request: dict[str, Any] = {
                "schema_version": "chat-read-aloud-verification-request.v1",
                "evidence_id": evidence_id,
                "kind": "gift",
                "source": item.source,
                "source_sha256": item.source_sha256,
                "source_event_id": item.source_event_id,
                "source_offset_ms": item.offset_ms,
                "exact_text": gift_name,
                "cue_indexes": [index + 1],
                "matched_start_ms": cue.start_ms,
                "matched_end_ms": cue.end_ms,
                "matched_audio_text": texts[index],
                "candidate_entities": [
                    {"canonical": gift_name, "surfaces": [], "readings": []},
                    {"canonical": tail, "surfaces": [], "readings": []},
                ],
                "context_before": texts[index - 1] if index > 0 else "",
                "context_after": texts[index + 1] if index + 1 < len(texts) else "",
                "reason": "gift-name read-aloud arbitration",
            }
            request["request_sha256"] = _request_sha256(request)
            try:
                raw_verdict = entity_verifier(request)
            except Exception as exc:  # verifier failure is evidence, never permission
                raw_verdict = {
                    "schema_version": "chat-entity-verdict.v1",
                    "request_sha256": request["request_sha256"],
                    "status": "UNCERTAIN",
                    "reason_code": "GIFT_NAME_VERIFIER_ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            verdict = _validated_read_aloud_verdict(raw_verdict, request=request)
            row["request_sha256"] = request["request_sha256"]
            row["verdict"] = verdict if verdict is not None else raw_verdict
            tail_start = texts[index].rfind(tail)
            if verdict is not None and verdict.get("canonical_entity") == gift_name and tail_start != -1:
                after_text = texts[index][:tail_start] + gift_name + texts[index][tail_start + len(tail) :]
                texts[index] = after_text
                row["after"] = after_text
                row["outcome"] = "gift_name_repaired"
            else:
                row["after"] = texts[index]
                row["outcome"] = "uncertain_no_change" if verdict is None else "asr_win_no_change"
            rows.append(row)
            break
    return rows


def apply_audio_entity_verification(
    srt_text: str,
    *,
    referent_groups: Sequence[ReferentGroup | Sequence[str]],
    entity_verifier: EntityVerifier | None,
    excluded_cue_indexes: Iterable[int] = (),
) -> tuple[str, dict[str, Any]]:
    """Independently arbitrate confusable entities not owned by exact chat.

    This closes the ordinary-speech path (for example ``立希`` vs ``Saki``),
    where there may be no danmaku at all.  Only one narrow entity surface can be
    changed; all surrounding words/timing remain untouched.
    """

    groups = _coerce_referent_groups(referent_groups)
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    excluded = {int(value) for value in excluded_cue_indexes}
    confirmed: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    required: list[dict[str, Any]] = []
    for cue_offset, cue in enumerate(cues):
        cue_index = cue_offset + 1
        if cue_index in excluded:
            continue
        matches: list[tuple[ReferentGroup, list[dict[str, Any]]]] = []
        for group in groups:
            occurrences = _entity_occurrences(texts[cue_offset], group)
            if group.positions:
                # "clip_initial"=片首 cue 的句首槽位；"transcript_only" 不限
                # 位置——positions 非空的共同作用是把组挡在 chat 证据路径之外。
                if "clip_initial" in group.positions:
                    if cue_offset != 0:
                        continue
                    occurrences = [row for row in occurrences if row["start"] == 0]
                elif "transcript_only" not in group.positions:
                    continue
            if not occurrences:
                continue
            suspicious = group.audio_verify_all_surfaces or any(
                str(row["surface"]).lower() != str(row["canonical"]).lower()
                for row in occurrences
            )
            if suspicious:
                matches.append((group, occurrences))
        if not matches:
            continue
        # 按组独立仲裁（2026-07-14 梦限大坏女人案）：一 cue 多组各自单槽正常
        # 裁——高松灯与海铃各是一组，不构成歧义；同组多次出现且全为规范形
        # = 文本无可改写，直接放行（kmx 案的 keep 表放行是其特例，不再要求
        # 成员资格）；同组多次含误听形仍 fail-closed（裁决无法映射到槽位）。
        # 已裁定的字符串不再被后续组重复仲裁。
        claimed_strings: set[str] = set()
        for group, occurrences in matches:
            occurrences = [
                row
                for row in occurrences
                if str(row["surface"]).lower() not in claimed_strings
            ]
            if not occurrences:
                continue
            if len(occurrences) > 1:
                if group.alias_surfaces or all(
                    str(row["surface"]).lower() == str(row["canonical"]).lower()
                    for row in occurrences
                ):
                    confirmed.append(
                        {
                            "cue_index": cue_index,
                            "matched_start_ms": cue.start_ms,
                            "matched_end_ms": cue.end_ms,
                            "reason_code": "ENTITY_ALREADY_CANONICAL_EVERYWHERE",
                        }
                    )
                else:
                    required.append(
                        {
                            "cue_index": cue_index,
                            "matched_start_ms": cue.start_ms,
                            "matched_end_ms": cue.end_ms,
                            "reason_code": "TRANSCRIPT_ENTITY_SLOT_AMBIGUOUS",
                            # 取证（2026-07-14）：无面无由的歧义行没法排障。
                            "surfaces": [str(row["surface"]) for row in occurrences],
                            "canonicals": [str(row["canonical"]) for row in occurrences],
                        }
                    )
                continue
            occurrence = occurrences[0]
            evidence_id = hashlib.sha256(
                (
                    f"transcript-entity\0{hashlib.sha256(srt_text.encode()).hexdigest()}\0"
                    f"{cue_index}\0{cue.start_ms}\0{cue.end_ms}\0{occurrence['canonical']}"
                ).encode("utf-8")
            ).hexdigest()
            request: dict[str, Any] = {
                "schema_version": "transcript-entity-verification-request.v1",
                "evidence_id": evidence_id,
                "kind": "transcript_entity",
                "cue_indexes": [cue_index],
                "matched_start_ms": cue.start_ms,
                "matched_end_ms": cue.end_ms,
                "matched_audio_text": texts[cue_offset],
                "transcript_canonical": occurrence["canonical"],
                "transcript_surface": occurrence["surface"],
                "candidate_entities": [
                    {
                        "canonical": entity.canonical,
                        "surfaces": list(entity.surfaces),
                        "readings": list(entity.readings),
                    }
                    for entity in group.entities
                ],
                "reason": group.reason,
            }
            request["request_sha256"] = _request_sha256(request)
            try:
                raw_verdict = entity_verifier(request) if entity_verifier is not None else None
            except Exception as exc:
                raw_verdict = {
                    "schema_version": "chat-entity-verdict.v1",
                    "request_sha256": request["request_sha256"],
                    "status": "UNCERTAIN",
                    "reason_code": "ENTITY_VERIFIER_ERROR",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            verdict = _validated_entity_verdict(raw_verdict, request=request, group=group)
            if verdict is None or verdict.get("status") != "RESOLVED":
                # 方向性 fail-closed（2026-07-13 kmx 案）：文本已是组内"默认可信方"
                # 的规范形而音频拿不准（3 字母含混音常 UNCERTAIN）→ 保留原文、留痕
                # 不阻塞；其余（真实词「乒乓球」、误听形「梦现代」…）UNCERTAIN 仍阻塞。
                if str(occurrence["surface"]).lower() == str(occurrence["canonical"]).lower() and any(
                    str(occurrence["canonical"]).lower() == keep.lower()
                    for keep in group.uncertain_keep_canonicals
                ):
                    confirmed.append(
                        {
                            "cue_index": cue_index,
                            "matched_start_ms": cue.start_ms,
                            "matched_end_ms": cue.end_ms,
                            "transcript_canonical": occurrence["canonical"],
                            "reason_code": "ENTITY_CANONICAL_KEPT_ON_UNCERTAIN",
                            "verdict": verdict or raw_verdict,
                        }
                    )
                    claimed_strings.add(str(occurrence["surface"]).lower())
                    continue
                # 面级保留（2026-07-14 理论上/留下、苏人案）：列入
                # uncertain_keep_surfaces 的误听面本身可能是真话，UNCERTAIN
                # 时保留原文不阻塞；改写只发生在音频确证 RESOLVED 时。
                if any(
                    str(occurrence["surface"]).lower() == keep.lower()
                    for keep in group.uncertain_keep_surfaces
                ):
                    confirmed.append(
                        {
                            "cue_index": cue_index,
                            "matched_start_ms": cue.start_ms,
                            "matched_end_ms": cue.end_ms,
                            "transcript_surface": occurrence["surface"],
                            "reason_code": "ENTITY_SURFACE_KEPT_ON_UNCERTAIN",
                            "verdict": verdict or raw_verdict,
                        }
                    )
                    claimed_strings.add(str(occurrence["surface"]).lower())
                    continue
                required.append(
                    {
                        "cue_index": cue_index,
                        "matched_start_ms": cue.start_ms,
                        "matched_end_ms": cue.end_ms,
                        "request": request,
                        "verdict": verdict or raw_verdict,
                        "reason_code": str((verdict or {}).get("reason_code") or "ENTITY_VERDICT_REQUIRED"),
                    }
                )
                continue
            resolved = str(verdict["canonical_entity"])
            base_row = {
                "evidence_id": evidence_id,
                "cue_indexes": [cue_index],
                "matched_start_ms": cue.start_ms,
                "matched_end_ms": cue.end_ms,
                "transcript_canonical": occurrence["canonical"],
                "transcript_surface": occurrence["surface"],
                "resolved_canonical": resolved,
                "verdict": verdict,
            }
            claimed_strings.add(str(occurrence["surface"]).lower())
            claimed_strings.add(resolved.lower())
            if resolved == occurrence["canonical"]:
                surface_value = str(occurrence["surface"])
                if surface_value.lower() != resolved.lower() and any(
                    surface_value.lower() == keep.lower()
                    for keep in group.uncertain_keep_surfaces
                ):
                    # 软误听面（理论上/留下/苏人…）+ 音频确证其宿主实体 →
                    # 规范化改写。合法别名（椎名立希）不在 keep_surfaces，
                    # 维持原状只 confirm——别名不是误听。
                    before = texts[cue_offset]
                    after = re.sub(
                        re.escape(surface_value), resolved, before, count=1, flags=re.IGNORECASE
                    )
                    if before != after:
                        texts[cue_offset] = after
                        repairs.append(
                            {
                                **base_row,
                                "mode": "transcript_entity_only",
                                "expected_entity": resolved,
                                "before": [before],
                                "after": [after],
                                "survived": resolved.lower() in after.lower(),
                            }
                        )
                        continue
                confirmed.append(base_row)
                continue
            before = texts[cue_offset]
            after = re.sub(
                re.escape(str(occurrence["surface"])),
                resolved,
                before,
                count=1,
                flags=re.IGNORECASE,
            )
            if before == after:
                required.append({**base_row, "reason_code": "ENTITY_SLOT_NOT_FOUND_FOR_MINIMAL_REPAIR"})
                continue
            texts[cue_offset] = after
            repairs.append(
                {
                    **base_row,
                    "mode": "transcript_entity_only",
                    "expected_entity": resolved,
                    "before": [before],
                    "after": [after],
                    "survived": resolved.lower() in after.lower(),
                }
            )
    output = _render_srt(cues, texts) if repairs else srt_text
    return output, {
        "schema_version": "transcript-entity-audit.v1",
        "status": "ENTITY_VERDICT_REQUIRED" if required else "APPLIED_AND_VERIFIED" if repairs else "VERIFIED" if confirmed else "NO_ENTITY",
        "input_srt_sha256": hashlib.sha256(srt_text.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(output.encode()).hexdigest(),
        "confirmed": confirmed,
        "repairs": repairs,
        "entity_verdict_required": required,
    }
