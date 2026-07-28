from src.autoslice.term_authority import (
    expected_value_respell_pairs,
    protected_terms,
    registered_terms,
    respell_pairs,
)


def test_selected_profile_static_rules_and_confusables_reach_shared_authority():
    pairs = respell_pairs()

    assert ("直女", "侄女") in pairs
    assert ("哇库哇库", "wakuwaku") in pairs
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
        "kmx",
        "做0.4",
    } <= terms


def test_expected_value_lane_is_explicit_and_mishear_is_not_registered_peer():
    pairs = expected_value_respell_pairs()
    assert {("林墨", "礼墨"), ("下斗里", "沙豆李")} <= pairs
    terms = registered_terms()
    assert "礼墨" in terms
    assert "恋青" in terms
    assert "恋死" in terms
    assert "林墨" not in terms
    assert "下斗里" not in terms
