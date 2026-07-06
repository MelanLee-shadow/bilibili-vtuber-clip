from pathlib import Path

from scripts.lidousha_glossary_terms import (
    FALLBACK_CANON,
    GlossaryTerms,
    load_glossary_terms,
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
    for canon in ("kmx", "142", "小室", "Ado", "沙豆李", "掏兜", "倒反天罡", "奶油苏打", "小李"):
        assert canon in terms.canon, canon
    # ASR mishearing variants are harvested for the blacklist.
    for variant in ("停放熊", "康姆叉", "沙特琳", "一四二", "苏丹", "阿朵", "小寺", "大爽天高"):
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


def test_load_glossary_terms_is_fail_safe_on_missing_file(tmp_path):
    terms = load_glossary_terms(tmp_path / "does-not-exist.txt")
    assert terms == GlossaryTerms(canon=FALLBACK_CANON, mishear_blacklist=())


def test_parse_empty_text_yields_no_canon():
    assert parse_glossary_terms("") == GlossaryTerms(canon=(), mishear_blacklist=())
