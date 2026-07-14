"""词表权威的唯一加载源（2026-07-14 屎山盘点整改第一刀）。

盘点实证：`subtitle_fidelity.sanctioned_respell_pairs` 与
`final_review_auditor.protected_terms` 函数体近乎复制——都各自读
硬梗表/代码切换表/混淆组/时效词，glossary 行首术语解析只有后者有。
两把词表尺一旦漂移，就会重演「立语→俚语」险案（一个加载器认识的钦定词
另一个不认识）。本模块收敛为单一来源；消费方只组合，不再各自读表。

约定：所有加载失败只让结果变小（更保守），绝不抛错——词表权威缺席时
守卫更严、审片员更多披露，这是安全方向。
"""

from __future__ import annotations

import re
from pathlib import Path

_GLOSSARY_TERM_RX = re.compile(r"^[-*]\s*(?:梗词：)?\*{0,2}([^：:（(＝=，,。\s*]{2,12})")

_ASSET_CONFUSABLES = (
    Path(__file__).resolve().parents[2] / "assets/lidousha/entity_confusables.json"
)


def canonical_pair_sources() -> list[tuple[str, str]]:
    """(surface, canonical) 有据改写对：硬梗/代码切换/混淆组面→canonical、
    时效词 alias/confusable→canonical/display。"""

    pairs: list[tuple[str, str]] = []
    try:
        from src.autoslice.chat_authority import (
            _CODE_SWITCH_CANONICAL_SURFACES,
            _HARD_MEME_CANONICAL_SURFACES,
            load_referent_groups,
        )

        pairs.extend(_CODE_SWITCH_CANONICAL_SURFACES)
        pairs.extend(_HARD_MEME_CANONICAL_SURFACES)
        for group in load_referent_groups(_ASSET_CONFUSABLES):
            for entity in group.entities:
                for surface in entity.surfaces:
                    if surface != entity.canonical:
                        pairs.append((surface, entity.canonical))
    except Exception:
        pass
    try:
        from scripts.gemini_slice_jingting import approved_timely_terms

        for record in approved_timely_terms():
            canonical = str(record.get("canonical") or "")
            display = str(record.get("display_name") or "")
            for source_key in ("aliases", "confusables"):
                for raw in record.get(source_key) or []:
                    surface = str(raw)
                    for target in (canonical, display):
                        if surface and target and surface != target:
                            pairs.append((surface, target))
    except Exception:
        pass
    return pairs


def respell_pairs() -> frozenset[tuple[str, str]]:
    """忠实性守卫的白名单改写对（表本身即证据）。"""

    return frozenset(
        (surface, canonical)
        for surface, canonical in canonical_pair_sources()
        if surface and canonical and surface != canonical
    )


def protected_terms() -> frozenset[str]:
    """钦定词面集合：审片员等自动改写层绝不允许碰的词。

    = 改写对两侧 + 混淆组全部 canonical + glossary.txt 行首术语
    （立语/做0.4 这类只在 glossary 里的梗词由此覆盖）。
    """

    terms: set[str] = set()
    for surface, canonical in canonical_pair_sources():
        terms.add(surface)
        terms.add(canonical)
    try:
        from src.autoslice.chat_authority import load_referent_groups

        for group in load_referent_groups(_ASSET_CONFUSABLES):
            for entity in group.entities:
                terms.add(entity.canonical)
    except Exception:
        pass
    try:
        from scripts.gemini_slice_jingting import glossary as _glossary

        for line in _glossary().splitlines():
            match = _GLOSSARY_TERM_RX.match(line.strip())
            if match:
                terms.add(match.group(1).strip("*"))
    except Exception:
        pass
    return frozenset(t for t in terms if t and len(t) >= 2)
