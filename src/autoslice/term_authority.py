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

import hashlib
from pathlib import Path
import re
from typing import Mapping

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
            (rule.surface, rule.canonical) for rule in CHANNEL_PROFILE.canonical_surface_rules
        )
        for group in load_referent_groups(_ASSET_CONFUSABLES, include_singletons=True):
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
        for surface, canonical in [*canonical_pair_sources(),
                                   *((rule.surface, rule.canonical) for rule in spelling_equivalence_rules())]
        if surface and canonical and surface != canonical
    )


def spelling_equivalence_rules() -> tuple[CanonicalSurfaceRule, ...]:
    """Explicit glossary spelling equivalences, separate from ASR confusables.

    The prose marker declares equality, unlike the nearby list of uncertain
    sound-alikes. Only its slash-delimited literal spellings are consumed.
    """
    try:
        text = CHANNEL_PROFILE.asset_file("glossary").read_text(encoding="utf-8")
    except (OSError, UnicodeError, KeyError, ValueError):
        return ()
    rules = []
    for aliases, canonical in re.findall(
        r"(?:^|[。；])\s*([^。\n；]+/[^。\n；]+?)等写法统一为\s*([A-Za-z0-9]+)", text,
    ):
        for surface in aliases.split("/"):
            surface = surface.strip(" *`")
            if 2 <= len(surface) <= 32 and surface != canonical:
                rules.append(CanonicalSurfaceRule(
                    surface=surface, canonical=canonical,
                    authority=f"{CHANNEL_PROFILE.profile_id}-glossary-spelling-equivalence.v1",
                ))
    return tuple(rules)


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
                authority=(f"{CHANNEL_PROFILE.profile_id}-glossary-expected-value-canon.v1"),
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

    return frozenset((rule.surface, rule.canonical) for rule in expected_value_surface_rules())


def exact_cue_canons() -> frozenset[str]:
    """Explicit whole-cue truths allowed to bypass per-name CPA selection."""

    try:
        from scripts.profile_glossary_terms import load_glossary_exact_cues

        return frozenset(load_glossary_exact_cues(CHANNEL_PROFILE.asset_file("glossary")))
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
        rule.canonical for rule in CHANNEL_PROFILE.canonical_surface_rules if rule.canonical
    }
    try:
        from src.autoslice.chat_authority import load_referent_groups

        for group in load_referent_groups(_ASSET_CONFUSABLES, include_singletons=True):
            for entity in group.entities:
                if entity.canonical:
                    terms.add(entity.canonical)
    except Exception:
        pass
    try:
        from scripts.profile_glossary_terms import load_glossary_terms

        terms.update(load_glossary_terms(CHANNEL_PROFILE.asset_file("glossary")).canon)
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

        for group in load_referent_groups(_ASSET_CONFUSABLES, include_singletons=True):
            for entity in group.entities:
                terms.add(entity.canonical)
    except Exception:
        pass
    try:
        from scripts.profile_glossary_terms import load_glossary_terms

        terms.update(load_glossary_terms(CHANNEL_PROFILE.asset_file("glossary")).canon)
    except Exception:
        pass
    return frozenset(t for t in terms if t and len(t) >= 2)


def registered_person_names() -> frozenset[str]:
    """Explicit person entries; a glossary common word is not a person name."""
    from scripts.profile_glossary_terms import parse_glossary_terms

    names: set[str] = set()
    try:
        lines = CHANNEL_PROFILE.asset_file("glossary").read_text(encoding="utf-8").splitlines()
        person_lines = [line for line in lines if re.match(
            r"^-\s*(?:\*\*)?(?:人名(?:/ID)?|主播名)[：:]", line.strip()
        )]
        names.update(parse_glossary_terms("\n".join(person_lines)).canon)
    except (OSError, KeyError, ValueError):
        pass
    try:
        lines = CHANNEL_PROFILE.asset_file("psplive_roster").read_text(encoding="utf-8").splitlines()
        for line in lines:
            if not line.startswith("| "):
                continue
            name = line.split("|")[1].strip().split("（")[0].strip(" *")
            if name not in {"艺名", "---"} and len(name) >= 2:
                names.add(name)
    except (OSError, KeyError, ValueError):
        pass
    return frozenset(name for name in names if len(name) >= 2)


def removed_registered_names(current: str, proposed: str) -> tuple[str, ...]:
    return tuple(sorted(name for name in registered_person_names()
                        if current.count(name) > proposed.count(name)))


def registered_name_decision_prompt(current: str, proposed: str) -> str:
    names = removed_registered_names(current, proposed)
    if not names:
        return ""
    return (
        "\n## 已登记人名的指称与写法\n"
        f"本次 PROPOSED 会减少这些已登记人名的出现：{list(names)}。"
        "词表只证明这个人及写法存在，不证明本句一定在说她。先辨指称，再选字。"
        "若语境仍指同一个已登记的人，s/sh、n/l 等近音或一次 ASR/拼音差异不能"
        "凭空发明另一个人；‘她/他’只证明有人，不能证明另一个名字。应保留规范写法"
        "或继续调查具体疑点，不把音节相似度当人物身份根。"
        "确实是普通词用法（如等了两小时）仍可修复；确实是另一个人也可修复，"
        "但必须有独立于本次争议转写的近窗文字/绑定弹幕，明确出现新指称。"
        "选择 PROPOSED 时，JSON 另给 registered_name_decisions 数组，每个上述人名一行："
        '{"name":"原人名","usage":"LITERAL_NONENTITY 或 DIFFERENT_REFERENT",'
        '"replacement":"实际新词面","context_quote":"现有语境的逐字片段",'
        '"reason":"为什么是普通词或另一个人"}。'
        "LITERAL_NONENTITY 的 quote 可来自 CURRENT 和前后文；DIFFERENT_REFERENT 的 quote"
        "必须来自未改动的前后文或绑定弹幕，并包含新词面。不能引用 PROPOSED、自身拼音"
        "或审片员推测充当独立证据。没有这种依据不要选择 PROPOSED。\n"
    )


def registered_name_change_allowed(
    current: str, proposed: str, *, judge: object,
    context_before: str = "", context_after: str = "", structured_chat: str = "",
) -> bool:
    """Validate CPA's explicit referent decision, including old receipt replay.

    CPA still interprets literal usage. Code only requires the promised bound
    context and forbids pinyin or a generic pronoun becoming a new identity.
    A rejected mutation is unresolved, never proof that CURRENT is correct.
    """
    names = removed_registered_names(current, proposed)
    if not names:
        return True
    rows = judge.get("registered_name_decisions") if isinstance(judge, Mapping) else None
    if not isinstance(rows, list):
        return False
    independent_context = "\n".join((context_before, context_after, structured_chat))
    literal_context = "\n".join((current, independent_context))
    for name in names:
        decisions = [row for row in rows if isinstance(row, Mapping) and row.get("name") == name]
        if len(decisions) != 1:
            return False
        row = decisions[0]
        replacement, quote, reason = (row.get(key) for key in ("replacement", "context_quote", "reason"))
        if not all(isinstance(value, str) and value.strip() for value in (replacement, quote, reason)):
            return False
        if replacement == name or replacement not in proposed:
            return False
        if row.get("usage") == "LITERAL_NONENTITY":
            if quote not in literal_context:
                return False
        elif row.get("usage") == "DIFFERENT_REFERENT":
            if replacement not in quote or quote not in independent_context:
                return False
        else:
            return False
    return True


def _pronoun_policy_prompt_context(text: str) -> str:
    """Load the active writing policy, distinct from a prior CPA observation."""
    if re.search(r"(?<!其)[他她它]|(?<![A-Za-z0-9_])TA(?:们)?(?![A-Za-z0-9_])", text) is None:
        return ""
    try:
        policy = CHANNEL_PROFILE.asset_file("subtitle_correction_principles").read_text(encoding="utf-8")
    except (OSError, KeyError, ValueError):
        return ""
    match = re.search(r"^##[^\n]*代词[^\n]*\n.*?(?=^## |\Z)", policy, flags=re.MULTILINE | re.DOTALL)
    if match is None:
        return ""
    section = match.group(0).strip()
    return (
        "## 当前项目代词书写规则（实际资产，非声学证词）\n"
        f"policy_sha256=sha256:{hashlib.sha256(policy.encode('utf-8')).hexdigest()}\n"
        + section
        + "\n先依据全文/绑定语境确定实际指代，再按本规则选择字形；"
        "本规则不证明目标说了某个音节，不授权把 ta 改为我、删字或补字。"
        "ASR默认字形和旧CPA审计都不能覆盖当前书写规则；"
        "指代仍无法确定时可保持NEITHER并披露缺口，不用通用写作习惯代替项目规则。\n"
    )


def terminology_prompt_context(*candidate_texts: str) -> str:
    """Give local CPA consumers the same relevant terminology as discovery.

    These entries establish meanings and spellings, never that a particular
    utterance contains an entity. Keep them out of candidate-blind witnesses.
    The existing hard-canon policy remains distinct from ordinary terms.
    """
    from src.autoslice.surface_canon import _hard_meme_canon_prompt_block

    text = "\n".join(value for value in candidate_texts if isinstance(value, str))

    def contains(value: str, term: str) -> bool:
        if not term or len(term) < 2:
            return False
        if term.isascii():
            return re.search(
                r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])",
                value, re.IGNORECASE,
            ) is not None
        return term.casefold() in value.casefold()

    pairs = canonical_pair_sources()
    known = registered_terms() | protected_terms()
    relevant = {term for term in known if contains(text, term)}
    for surface, canonical in pairs:
        if contains(text, surface) or contains(text, canonical):
            relevant.update((surface, canonical))
    entries: list[str] = []
    for key in ("glossary", "psplive_roster"):
        try:
            asset = CHANNEL_PROFILE.asset_file(key)
            lines = asset.read_text(encoding="utf-8").splitlines()
        except (OSError, KeyError, ValueError):
            continue
        for line in lines:
            if line.strip().startswith("- ") and any(
                contains(line, term) for term in relevant
            ):
                entries.append(line.strip())
    listed_pairs = [
        f"{surface} → {canonical}"
        for surface, canonical in pairs
        if contains(text, surface) or contains(text, canonical)
    ]
    parts = [
        "## 当前候选相关的频道术语（项目词表数据）\n"
        "词条说明实体、昵称、梗的含义和规范写法，不证明目标时窗念出了该词。"
        "不能仅因专名陌生、普通词更常见或更顺口，就把专名改回普通词。"
        "若原音与语境确认同一实体/梗，使用词条中的合法写法并保留合法昵称；"
        "普通词或另一个登记实体若有更强的本句证据，仍应正常当选。"
        "两个登记专名平等，不按出现频率机械替换。只有下列明确hard-canon"
        "是不可绕过的输出规范；其他词条不享有该强制优先级。\n"
    ]
    if entries:
        parts.append("\n".join(dict.fromkeys(entries)) + "\n")
    if listed_pairs:
        parts.append("已有别名/校正方向（仍须判断本句指代）：\n"
                     + "\n".join(dict.fromkeys(listed_pairs)) + "\n")
    parts.append(_hard_meme_canon_prompt_block())
    pronoun_policy = _pronoun_policy_prompt_context(text)
    if pronoun_policy:
        parts.append(pronoun_policy)
    return "\n".join(parts)


def foreign_insert_entities() -> list[tuple[str, tuple[str, ...]]]:
    """注册的外语插话实体（canonical 含假名，如 ありがとう/おめでとう）。

    语言保真门用它做拼音见证：draft 里被替换的中文近音段与这些实体的
    readings 对齐即构成"有见证的转写修复"。加载失败返回空（门更严）。"""

    out: list[tuple[str, tuple[str, ...]]] = []
    try:
        import re as _re

        from src.autoslice.chat_authority import load_referent_groups

        kana = _re.compile(r"[぀-ヿ]")
        for group in load_referent_groups(_ASSET_CONFUSABLES, include_singletons=True):
            for entity in group.entities:
                if kana.search(entity.canonical) and entity.readings:
                    out.append((entity.canonical, tuple(entity.readings)))
    except Exception:
        return []
    return out
