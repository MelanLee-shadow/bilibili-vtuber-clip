"""确定性拼音混淆候选发现层（2026-07-19，欠账 #5/#0 落地）。

两条扫描 lane，都只做「发现」，绝不裁决（发现≠裁决，保向铁律）：

1. ``glossary_phonetic_candidate_groups`` —— 把 7/18 五件套审片里人工判出
   kmx 误听变体（皮毛熊/K头小/Q我熊）的思路机制化：对每个带 readings 的
   注册实体，逐 cue 滑窗做拼音音节序列相似度匹配，命中即编一个临时混淆
   组送声学仲裁。人工判断当时依赖的是「拼音近似 + 弹幕零背书」；这里
   的确定性替身是「拼音近似 + 该面不是任何注册词面」。
2. ``phrase_repetition_divergence_groups`` —— 欠账 #0 的短语级重复分歧
   编译器（抱/帮案）：同一 3-6 字 CJK n-gram 跨句重现、仅一字之差且该字
   近音 → 编混淆组。补 ``chat_evidence.repetition_divergence_groups``
   只抓句级复读的盲区。

裁决语义与重复一致性组相同：audio_verify_all_surfaces、UNCERTAIN 双向
保留绝不阻塞（uncertain_keep_canonicals 含两侧）。误报的代价只是一次
声学证据与 CPA 裁决；漏报的代价是错字上线——阈值按「宁多送裁决、绝不自动改写」
校准，并用 7/18 实案文本做回归夹具。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Iterable, Sequence

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.chat_evidence import (
    ReferentEntity,
    ReferentGroup,
    normalize_chat_text,
)

_CJK_RUN = re.compile(r"[一-鿿A-Za-z]+")
_CJK_ONLY = re.compile(r"^[一-鿿]+$")
_CJK_CHAR = re.compile(r"[一-鿿]")

# 单个拉丁字母的口语字母名近似音（K头小 的 K、Q我熊 的 Q）。
_LATIN_LETTER_SYLLABLES = {
    "a": "ei", "b": "bi", "c": "sei", "d": "di", "e": "yi", "f": "aif",
    "g": "ji", "h": "eiqu", "i": "ai", "j": "jei", "k": "kei", "l": "el",
    "m": "em", "n": "en", "o": "ou", "p": "pi", "q": "kiu", "r": "ar",
    "s": "es", "t": "ti", "u": "you", "v": "wei", "w": "dabliu",
    "x": "eks", "y": "wai", "z": "zei",
}

# 每个片段最多送裁决的候选组数（成本护栏：每组是一次声学仲裁）。
MAX_PHONETIC_GROUPS = 4
MAX_PHRASE_GROUPS = 4

MIN_SEQUENCE_SCORE = 0.55
MIN_PAIR_SCORE = 0.30
MIN_BEST_PAIR_SCORE = 0.75
# ≥3 音节还要求过半音节对 ≥0.65（K头小 2/3 过、李说熊 1/3 拒）。
MIN_STRONG_PAIR_SCORE = 0.65
# 2 音节实体（奶P/立希类）太容易撞高频词（有人→素人、件事→恋死），
# 逐对与均值下限单独收紧（卖批 0.835 过、件事 0.775 拒——7/18 实案校准）。
MIN_PAIR_SCORE_SHORT = 0.65
MIN_SEQUENCE_SCORE_SHORT = 0.80
PHRASE_CHAR_MIN_RATIO = 0.5
# 两侧都是句尾语气词的分歧（就太好了/就太好啦）不值一次仲裁。
_PHRASE_PARTICLE_CHARS = frozenset("了啦呢呀啊哦吧嘛吗")


@lru_cache(maxsize=4096)
def _char_syllable(char: str) -> str:
    if char.lower() in _LATIN_LETTER_SYLLABLES:
        return _LATIN_LETTER_SYLLABLES[char.lower()]
    try:
        from pypinyin import lazy_pinyin  # type: ignore

        result = lazy_pinyin(char, errors="ignore")
    except Exception:
        return ""
    return result[0] if result else ""


def _syllables(text: str) -> list[str]:
    return [s for s in (_char_syllable(c) for c in text) if s]


@lru_cache(maxsize=8192)
def _pair_score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _aligned_score(window: Sequence[str], reading: Sequence[str]) -> float:
    """按位对齐评分。

    窗口只允许与 reading 等长或多一个音节（ASR 把 N 音节专名听成 N-1 字的
    情形交给已注册误听面的精确匹配，这里放开会撞太多短高频词）。2 音节
    reading 的逐对下限更严——校准实证：放 0.3 会把「有人」判成素人候选。
    """

    if not window or not reading:
        return 0.0
    if len(window) - len(reading) not in (0, 1):
        return 0.0
    short_reading = len(reading) == 2
    pair_floor = MIN_PAIR_SCORE_SHORT if short_reading else MIN_PAIR_SCORE
    score_floor = MIN_SEQUENCE_SCORE_SHORT if short_reading else 0.0

    def score_equal(xs: Sequence[str], ys: Sequence[str]) -> float:
        pairs = [_pair_score(x, y) for x, y in zip(xs, ys)]
        if not pairs or min(pairs) < pair_floor:
            return 0.0
        if max(pairs) < MIN_BEST_PAIR_SCORE:
            return 0.0
        strong = sum(1 for p in pairs if p >= MIN_STRONG_PAIR_SCORE)
        if not short_reading and strong * 2 < len(pairs):
            return 0.0
        value = sum(pairs) / len(pairs)
        return value if value >= score_floor else 0.0

    if len(window) == len(reading):
        return score_equal(window, reading)
    best = 0.0
    for drop in range(len(window)):
        trimmed = list(window[:drop]) + list(window[drop + 1 :])
        best = max(best, score_equal(trimmed, reading))
    return best


def _entity_readings(entity: ReferentEntity) -> list[list[str]]:
    """实体的音节序列参照：空格分隔的 readings 优先，canonical 自身兜底。"""

    readings: list[list[str]] = []
    for raw in entity.readings:
        text = str(raw).strip().lower()
        if " " in text:
            parts = [p for p in text.split() if p]
            if len(parts) >= 2:
                readings.append(parts)
    fallback = _syllables(entity.canonical)
    if len(fallback) >= 2 and fallback not in readings:
        readings.append(fallback)
    return [r for r in readings if 2 <= len(r) <= 8]


def glossary_phonetic_candidate_groups(
    srt_text: str,
    referent_groups: Iterable[ReferentGroup],
    *,
    min_score: float = MIN_SEQUENCE_SCORE,
    max_groups: int = MAX_PHONETIC_GROUPS,
    protected_faces: frozenset[str] | None = None,
) -> list[ReferentGroup]:
    """对注册实体做逐 cue 拼音滑窗，产出「实体 vs 疑似误听面」候选组。

    ``protected_faces``：钦定词面集合；None 时读 term_authority 全量词表。
    已注册的误听面（皮毛熊等）走精确匹配通道，本扫描器只管词表没见过的
    新变体——所以命中注册面/钦定面的窗口一律跳过。
    """

    entities: list[ReferentEntity] = []
    registered_faces: set[str] = set()
    for group in referent_groups:
        for entity in group.entities:
            entities.append(entity)
            registered_faces.add(entity.canonical.lower())
            registered_faces.update(s.lower() for s in entity.surfaces)
    targets = [
        (entity, readings)
        for entity in entities
        if (readings := _entity_readings(entity))
    ]
    if not targets:
        return []

    if protected_faces is not None:
        protected = {t.lower() for t in protected_faces}
    else:
        try:
            from src.autoslice.term_authority import protected_terms

            protected = {t.lower() for t in protected_terms()}
        except Exception:
            protected = set()
    # 词面包含检查（≥2字）：窗口把注册面/钦定词整个含在里面时（「以理论上」
    # 含「理论上」），那段文本已归精确匹配通道管辖，扫描器不重复编组。
    owned_faces = [f for f in (registered_faces | protected) if len(f) >= 2]

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    seen: set[tuple[str, str]] = set()
    candidates: list[tuple[float, str, ReferentEntity]] = []
    for cue in cues:
        for run in _CJK_RUN.findall(cue.text):
            # 注册面的出现位置（叠名守卫，2026-07-20 小李小李→小立希李案）：
            # 滑窗骑在注册面出现区间上（「小李小李」中段的「李小」）不是新
            # 误听面，是窗口切进了已知实体本身——按位置重叠一律跳过。
            face_spans: list[tuple[int, int]] = []
            lowered_run = run.lower()
            for face in owned_faces:
                probe = 0
                while True:
                    hit = lowered_run.find(face, probe)
                    if hit < 0:
                        break
                    face_spans.append((hit, hit + len(face)))
                    probe = hit + 1
            for entity, readings in targets:
                lengths = {len(r) for r in readings}
                min_len = min(lengths) - 1
                max_len = max(lengths) + 1
                for size in range(max(2, min_len), max_len + 1):
                    for start in range(0, len(run) - size + 1):
                        surface = run[start : start + size]
                        lowered = surface.lower()
                        if lowered in registered_faces or lowered in protected:
                            continue
                        if any(face in lowered for face in owned_faces):
                            continue
                        if any(
                            start < face_end and face_start < start + size
                            for face_start, face_end in face_spans
                        ):
                            continue
                        if surface in entity.canonical or entity.canonical in surface:
                            continue
                        # 纯拉丁窗口（如"live"）不构成中文误听面。
                        if not _CJK_CHAR.search(surface):
                            continue
                        key = (entity.canonical, surface)
                        if key in seen:
                            continue
                        # 小窗口先扫：同一实体已命中的更短面被包含时不再重复
                        # （卖批 命中后不再追加 猫卖批）。
                        if any(
                            prev in surface
                            for canonical, prev in seen
                            if canonical == entity.canonical
                        ):
                            continue
                        window = _syllables(surface)
                        score = max(_aligned_score(window, r) for r in readings)
                        if score < min_score:
                            continue
                        seen.add(key)
                        candidates.append((score, surface, entity))
    # 全量收集后按分数取前 N：cap 若按扫描顺序截断，前部噪声会饿死后部
    # 高分真命中（七星案：李嚼大果 0.6x 占位，林更多 0.89 反被挤掉）。
    candidates.sort(key=lambda row: (-row[0], row[1]))
    groups: list[ReferentGroup] = []
    for score, surface, entity in candidates[:max_groups]:
        groups.append(
            ReferentGroup(
                (
                    ReferentEntity(
                        entity.canonical,
                        (entity.canonical,),
                        entity.readings,
                    ),
                    ReferentEntity(surface, (surface,)),
                ),
                reason=(
                    f"拼音候选发现：「{surface}」音似注册实体"
                    f"「{entity.canonical}」(score={score:.2f})，送声学仲裁。"
                ),
                audio_verify_all_surfaces=True,
                uncertain_keep_canonicals=(entity.canonical, surface),
                positions=("transcript_only", "phonetic_candidate"),
            )
        )
    return groups


def phrase_repetition_divergence_groups(
    srt_text: str,
    *,
    ngram_min: int = 4,
    ngram_max: int = 6,
    max_groups: int = MAX_PHRASE_GROUPS,
) -> list[ReferentGroup]:
    """短语级重复分歧编译器（欠账 #0）：跨句 n-gram 重复 + 单字近音差。

    与句级 ``repetition_divergence_groups`` 互补：那边要求整句高相似且相邻，
    这里只要求同一短语在任意两句重现、槽位一字近音之差（帮小李准备花篮 vs
    抱小李准备那个花）。n-gram 从长到短枚举，命中即弃短，避免同一分歧重复
    编组。
    """

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    norms = [normalize_chat_text(text) for text in texts]

    try:
        from src.autoslice.term_authority import protected_terms

        protected = [t for t in protected_terms() if len(t) >= 2]
    except Exception:
        protected = []

    def diff_char_in_protected(cue_index: int, gram: str, slot: int) -> bool:
        """只有当**分歧字位本身**落在钦定词面里（侄/世 在「侄女」中、和/成 在
        「和成天下」中）才放弃这一对——那些词已有专门权威，重复仲裁纯烧
        额度。词面只是路过 gram 其他位置（帮小李 的「小李」）不影响编组。"""

        raw = texts[cue_index]
        start = raw.find(gram)
        if start < 0:
            return True
        diff_pos = start + slot
        for term in protected:
            probe = 0
            while True:
                hit = raw.find(term, probe)
                if hit < 0:
                    break
                if hit <= diff_pos < hit + len(term):
                    return True
                probe = hit + 1
        return False

    groups: list[ReferentGroup] = []
    claimed_slots: set[tuple[int, int]] = set()
    seen_pairs: set[frozenset[str]] = set()
    for size in range(ngram_max, ngram_min - 1, -1):
        buckets: dict[tuple[int, str], list[tuple[str, int, int]]] = {}
        for cue_index, norm in enumerate(norms):
            for match_start in range(0, len(norm) - size + 1):
                gram = norm[match_start : match_start + size]
                if not _CJK_ONLY.match(gram):
                    continue
                for slot in range(size):
                    key_text = gram[:slot] + "□" + gram[slot + 1 :]
                    buckets.setdefault((slot, key_text), []).append(
                        (gram[slot], cue_index, match_start + slot)
                    )
        for (_slot, key_text), members in sorted(
            buckets.items(), key=lambda item: item[0][1]
        ):
            chars = {char for char, _index, _pos in members}
            if len(chars) < 2:
                continue
            for char_a in sorted(chars):
                for char_b in sorted(chars):
                    if char_a >= char_b:
                        continue
                    hits_a = [(i, p) for c, i, p in members if c == char_a]
                    hits_b = [(i, p) for c, i, p in members if c == char_b]
                    cues_a = {i for i, _p in hits_a}
                    cues_b = {i for i, _p in hits_b}
                    if not cues_a or not cues_b or cues_a == cues_b:
                        # 同一批句子两个字都出现：槽位不唯一，宁缺毋滥。
                        continue
                    if char_a in _PHRASE_PARTICLE_CHARS and char_b in _PHRASE_PARTICLE_CHARS:
                        continue
                    if (
                        _pair_score(_char_syllable(char_a), _char_syllable(char_b))
                        < PHRASE_CHAR_MIN_RATIO
                    ):
                        continue
                    gram_a = key_text.replace("□", char_a)
                    gram_b = key_text.replace("□", char_b)
                    if frozenset((gram_a, gram_b)) in seen_pairs:
                        continue
                    index_a, pos_a = min(hits_a)
                    index_b, pos_b = min(hits_b)
                    # 长 n-gram 先行；同一分歧字位已编组的短版本不再重复。
                    if (index_a, pos_a) in claimed_slots or (index_b, pos_b) in claimed_slots:
                        continue
                    if gram_a not in texts[index_a] or gram_b not in texts[index_b]:
                        continue
                    if diff_char_in_protected(index_a, gram_a, _slot) or diff_char_in_protected(
                        index_b, gram_b, _slot
                    ):
                        continue
                    seen_pairs.add(frozenset((gram_a, gram_b)))
                    claimed_slots.add((index_a, pos_a))
                    claimed_slots.add((index_b, pos_b))
                    groups.append(
                        ReferentGroup(
                            (
                                ReferentEntity(gram_a, (gram_a,)),
                                ReferentEntity(gram_b, (gram_b,)),
                            ),
                            reason=(
                                f"短语级重复分歧：「{gram_a}/{gram_b}」跨句重现仅差一近音字，"
                                "两处各自由音频裁决。"
                            ),
                            audio_verify_all_surfaces=True,
                            uncertain_keep_canonicals=(gram_a, gram_b),
                            positions=("transcript_only", "phrase_divergence"),
                        )
                    )
                    if len(groups) >= max_groups:
                        return groups
    return groups
