from pathlib import Path

from scripts.lidousha_glossary_terms import (
    FALLBACK_CANON,
    GlossaryTerms,
    load_glossary_terms,
    parse_glossary_expected_value_pairs,
    parse_glossary_terms,
)

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY = ROOT / "assets" / "lidousha" / "glossary.txt"


def test_parse_real_glossary_extracts_multiple_canon_and_blacklist():
    terms = load_glossary_terms(GLOSSARY)

    assert isinstance(terms, GlossaryTerms)
    # The point of the fix: the terminology gate now knows many proper nouns,
    # not just kmx.
    assert len(terms.canon) > 5
    for canon in (
        "kmx",
        "142",
        "小室",
        "Ado",
        "沙豆李",
        "掏兜",
        "倒反天罡",
        "奶油苏打",
        "小李",
        "和成天下",
        "粉丝团灯牌",
    ):
        assert canon in terms.canon, canon
    # ASR mishearing variants are harvested for the blacklist.
    for variant in (
        "停放熊",
        "康姆叉",
        "沙特琳",
        "一四二",
        "苏丹",
        "阿朵",
        "小寺",
        "大爽天高",
        "合成天下",
        "何成天下",
        "下斗里",
        "粉丝灯牌",
        "粉团灯牌",
    ):
        assert variant in terms.mishear_blacklist, variant
    # A canonical spelling is never simultaneously flagged as a violation.
    assert not (set(terms.canon) & set(terms.mishear_blacklist))


def test_parse_glossary_terms_pairs_canon_with_its_mishearings():
    text = (
        "专名优先使用以下写法：\n"
        "- 人名：小室。不要写成小寺、小时、小师。\n"
        "- 人名/ID：142（读音近“一四二”）。一律写成 142，不要写成一四二、伊索尔。\n"
    )
    terms = parse_glossary_terms(text)

    assert "小室" in terms.canon
    assert "142" in terms.canon
    assert {"小寺", "小时", "小师"} <= set(terms.mishear_blacklist)
    assert {"一四二", "伊索尔"} <= set(terms.mishear_blacklist)


def test_mishear_list_stops_before_semicolon_explanation():
    terms = parse_glossary_terms(
        "- 礼物名：粉丝团灯牌。不要写成 粉丝灯牌、粉团灯牌；"
        "后面的解释不是误听面。\n"
    )

    assert {"粉丝灯牌", "粉团灯牌"} <= set(terms.mishear_blacklist)
    assert all("解释" not in term for term in terms.mishear_blacklist)


def test_parser_does_not_split_canonical_names_that_begin_with_he():
    terms = parse_glossary_terms(
        "- 品牌：和成天下。ASR 常误写成“合成天下/何成天下”，一律写成和成天下。\n"
    )

    assert "和成天下" in terms.canon
    assert "成天下" not in terms.canon
    assert {"合成天下", "何成天下"} <= set(terms.mishear_blacklist)


def test_expected_value_pairs_require_one_unambiguous_canonical():
    pairs = parse_glossary_expected_value_pairs(
        "- 主播玩梗名：ASR 常把它听成 下斗里（事故注）等——一律修正为"
        "“沙豆李”。\n"
        "- 人名：恋青、恋死。ASR 常听成 连情 等。\n"
    )

    assert ("下斗里", "沙豆李") in pairs
    assert all(surface != "连情" for surface, _ in pairs)


def test_load_glossary_terms_is_fail_safe_on_missing_file(tmp_path):
    terms = load_glossary_terms(tmp_path / "does-not-exist.txt")
    assert terms == GlossaryTerms(canon=FALLBACK_CANON, mishear_blacklist=())


def test_parse_empty_text_yields_no_canon():
    assert parse_glossary_terms("") == GlossaryTerms(canon=(), mishear_blacklist=())
