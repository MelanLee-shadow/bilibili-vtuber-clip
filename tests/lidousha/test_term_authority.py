from src.autoslice.term_authority import (
    expected_value_respell_pairs,
    protected_terms,
    registered_terms,
    respell_pairs,
    terminology_prompt_context,
)


def test_selected_profile_static_rules_and_confusables_reach_shared_authority():
    pairs = respell_pairs()

    assert ("直女", "侄女") in pairs
    assert ("哇库哇库", "ワクワク") in pairs
    assert ("boku", "ぼく") in pairs
    assert ("梦现代", "梦限大") in pairs
    assert ("林墨", "礼墨") in pairs


def test_selected_profile_terms_are_protected_from_downstream_rewrites():
    terms = protected_terms()

    assert {
        "侄女",
        "直女",
        "梦限大",
        "梦现代",
        "林墨",
        "礼墨",
        "礼豆沙",
        "李墨",
        "kmx",
        "做0.4",
    } <= terms


def test_expected_value_lane_is_explicit_and_mishear_is_not_registered_peer():
    pairs = expected_value_respell_pairs()
    assert {
        ("林墨", "礼墨"),
        ("下斗里", "沙豆李"),
        ("粉丝灯牌", "粉丝团灯牌"),
        ("粉团灯牌", "粉丝团灯牌"),
    } <= pairs
    terms = registered_terms()
    assert "礼墨" in terms
    assert "礼豆沙" in terms
    assert "李墨" in terms
    assert "粉丝团灯牌" in terms
    assert "恋青" in terms
    assert "恋死" in terms
    assert "林墨" not in terms
    assert "下斗里" not in terms
    assert "粉丝灯牌" not in terms
    assert "粉团灯牌" not in terms


def test_local_term_context_preserves_meaning_and_real_common_word_exception():
    context = terminology_prompt_context("我会说立语", "我会说日语")
    assert "日语的戏仿说法" in context
    assert "她认真说「日语」时保持日语原样" in context
    assert "普通词或另一个登记实体若有更强的本句证据" in context
    assert "两个登记专名平等" in context
    assert "不能仅因专名陌生" in context
    assert "「直女」一律写作「侄女」" in context
    assert "七星" not in context


def test_local_term_context_uses_profile_assets_without_mutating_candidates():
    current = "我去找小室"
    proposed = "我去找小时"
    context = terminology_prompt_context(current, proposed)
    assert "人名：小室" in context
    assert "小时" in context
    assert current == "我去找小室" and proposed == "我去找小时"


def test_local_pronoun_judge_receives_active_writing_policy():
    context = terminology_prompt_context("然后她当时就是在做梦的时候", "然后TA当时就是在做梦的时候")
    assert "查不到才用 TA" in context
    assert "匿名且性别无从判断" in context
    assert "名人先查证" in context
    assert "policy_sha256=sha256:" in context
    assert "不授权把 ta 改为我、删字或补字" in context


def test_pronoun_policy_change_changes_judge_prompt_context(tmp_path, monkeypatch):
    from src.autoslice import term_authority

    original_profile = term_authority.CHANNEL_PROFILE
    policy = tmp_path / "principles.md"

    class Profile:
        def __getattr__(self, name):
            return getattr(original_profile, name)

        def asset_file(self, key):
            if key == "subtitle_correction_principles":
                return policy
            return original_profile.asset_file(key)

    monkeypatch.setattr(term_authority, "CHANNEL_PROFILE", Profile())
    policy.write_text("## 代词与指代\n本频道未知人物写TA。\n\n## 别的规则\n不要泄入本段。\n")
    first = terminology_prompt_context("她", "TA")
    policy.write_text("## 代词与指代\n本频道未知人物写中性代词。\n\n## 别的规则\n不要泄入本段。\n")
    second = terminology_prompt_context("她", "TA")
    assert first != second
    assert "本频道未知人物写TA" in first
    assert "本频道未知人物写中性代词" in second
    assert "不要泄入本段" not in first + second
