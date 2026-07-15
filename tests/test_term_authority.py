from src.autoslice.term_authority import protected_terms, respell_pairs


def test_selected_profile_static_rules_and_confusables_reach_shared_authority():
    pairs = respell_pairs()

    assert ("直女", "侄女") in pairs
    assert ("哇库哇库", "wakuwaku") in pairs
    assert ("梦现代", "梦限大") in pairs


def test_selected_profile_terms_are_protected_from_downstream_rewrites():
    terms = protected_terms()

    assert {"侄女", "直女", "梦限大", "梦现代", "kmx", "做0.4"} <= terms
