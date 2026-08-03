"""词表权威的唯一加载源（屎山盘点整改第一刀）。

盘点实证：`subtitle_fidelity.sanctioned_respell_pairs` 与
`final_review_auditor.protected_terms` 函数体近乎复制——都各自读
硬梗表/代码切换表/混淆组/时效词，glossary 行首术语解析只有后者有。
两把词表尺一旦漂移，就会重演「立语→俚语」险案（一个加载器认识的钦定词
另一个不认识）。本模块收敛为单一来源；消费方只组合，不再各自读表。

约定：所有加载失败只让结果变小（更保守），绝不抛错——词表权威缺席时
守卫更严、审片员更多披露，这是安全方向。
"""

from __future__ import annotations

from pathlib import Path

from src.autoslice.channel_profile import CanonicalSurfaceRule, load_channel_profile


REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
_ASSET_CONFUSABLES = CHANNEL_PROFILE.asset_file("entity_confusables")


def canonical_pair_sources() -> list[tuple[str, str]]:
    """(surface, canonical) 有据改写对：硬梗/代码切换/混淆组面→canonical、
    时效词 alias/confusable→canonical/display。"""

    pairs: list[tuple[str, str]] = []
    try:
        from src.autoslice.chat_authority import load_referent_groups

        pairs.extend(
            (rule.surface, rule.canonical)
            for rule in CHANNEL_PROFILE.canonical_surface_rules
        )
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


def expected_value_surface_rules() -> tuple[CanonicalSurfaceRule, ...]:
    """Profile rules plus unambiguous, explicitly listed glossary variants.

    The glossary parser admits no fuzzy discoveries and no multi-canonical
    bullet.  This gives common known ASR variants the expected-value behavior
    selected by 维护者 without making two registered proper names unequal.
    """

    rules = [
        rule
        for rule in CHANNEL_PROFILE.canonical_surface_rules
        if rule.authority.endswith("-expected-value-canon.v1")
        and rule.surface
        and rule.canonical
        and rule.surface != rule.canonical
    ]
    try:
        from scripts.profile_glossary_terms import (
            load_glossary_expected_value_pairs,
        )

        rules.extend(
            CanonicalSurfaceRule(
                surface=surface,
                canonical=canonical,
                authority=(
                    f"{CHANNEL_PROFILE.profile_id}-glossary-"
                    "expected-value-canon.v1"
                ),
            )
            for surface, canonical in load_glossary_expected_value_pairs(
                CHANNEL_PROFILE.asset_file("glossary")
            )
        )
    except Exception:
        pass
    unique: dict[tuple[str, str], CanonicalSurfaceRule] = {}
    for rule in rules:
        unique.setdefault((rule.surface, rule.canonical), rule)
    return tuple(unique.values())


def expected_value_respell_pairs() -> frozenset[tuple[str, str]]:
    """Known wrong surfaces allowed to bypass CPA on expected-value grounds."""

    return frozenset(
        (rule.surface, rule.canonical)
        for rule in expected_value_surface_rules()
    )


def exact_cue_canons() -> frozenset[str]:
    """Explicit whole-cue truths allowed to bypass per-name CPA selection."""

    try:
        from scripts.profile_glossary_terms import load_glossary_exact_cues

        return frozenset(
            load_glossary_exact_cues(CHANNEL_PROFILE.asset_file("glossary"))
        )
    except Exception:
        return frozenset()


def registered_terms() -> frozenset[str]:
    """Canonical glossary/entity terms that are peers, not typo surfaces.

    If both sides of a proposed correction are in this set, term frequency
    cannot decide between them.  In particular, adding a new canonical name to
    the glossary automatically removes same-name corrections from the
    expected-value bypass.
    """

    terms: set[str] = {
        rule.canonical
        for rule in CHANNEL_PROFILE.canonical_surface_rules
        if rule.canonical
    }
    try:
        from src.autoslice.chat_authority import load_referent_groups

        for group in load_referent_groups(_ASSET_CONFUSABLES):
            for entity in group.entities:
                if entity.canonical:
                    terms.add(entity.canonical)
    except Exception:
        pass
    try:
        from scripts.profile_glossary_terms import load_glossary_terms

        terms.update(
            load_glossary_terms(
                CHANNEL_PROFILE.asset_file("glossary")
            ).canon
        )
    except Exception:
        pass
    try:
        from scripts.gemini_slice_jingting import approved_timely_terms

        for record in approved_timely_terms():
            for field in ("canonical", "display_name"):
                value = str(record.get(field) or "").strip()
                if value:
                    terms.add(value)
    except Exception:
        pass
    return frozenset(term for term in terms if term)


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
        from scripts.profile_glossary_terms import load_glossary_terms

        terms.update(
            load_glossary_terms(
                CHANNEL_PROFILE.asset_file("glossary")
            ).canon
        )
    except Exception:
        pass
    return frozenset(t for t in terms if t and len(t) >= 2)


def foreign_insert_entities() -> list[tuple[str, tuple[str, ...]]]:
    """注册的外语插话实体（canonical 含假名，如 ありがとう/おめでとう）。

    语言保真门用它做拼音见证：draft 里被替换的中文近音段与这些实体的
    readings 对齐即构成"有见证的转写修复"。加载失败返回空（门更严）。"""

    out: list[tuple[str, tuple[str, ...]]] = []
    try:
        import re as _re

        from src.autoslice.chat_authority import load_referent_groups

        kana = _re.compile(r"[぀-ヿ]")
        for group in load_referent_groups(_ASSET_CONFUSABLES):
            for entity in group.entities:
                if kana.search(entity.canonical) and entity.readings:
                    out.append((entity.canonical, tuple(entity.readings)))
    except Exception:
        return []
    return out
