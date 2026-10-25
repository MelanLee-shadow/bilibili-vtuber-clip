import pytest

from src.autoslice.meaning_difference import classify_difference
from src.autoslice.subtitle_acceptability_policy import load_policy


@pytest.mark.parametrize(
    "a,b,feature",
    [
        ("不许跟别人说话了", "不许跟别人讲话", "synonym"),
        ("我听，听到你不许哦", "我听，听到你不是呃", "unfinished_restart"),
        ("他听见我不许啊", "他听见我不是哦", "unfinished_restart"),
        ("哦，我知道你跟别人说的", "啊，我知道", "shortening_core_retained"),
        ("她明白我和大家讲的", "她明白", "shortening_core_retained"),
        ("我我我今天去", "我今天去", "stutter_repetition"),
        ("喝口水", "喝一口水", "synonym"),
        ("然后我去", "我去", "filler"),
        ("我也去", "也我去", "word_order"),
        ("你好！", "你好。", "punctuation"),
    ],
)
def test_narrow_harmless_differences(a, b, feature):
    for left, right in [(a, b), (b, a)]:
        r = classify_difference(left, right, glossary_terms=(), policy=load_policy())
        assert not r["meaning_carrying"], r
        assert feature in r["features"]


@pytest.mark.parametrize(
    "a,b,feature",
    [
        ("不许跟别人说话了", "不是跟你说话", "negation_polarity"),
        ("喜欢她", "不喜欢她", "negation_polarity"),
        ("是他", "不是他", "negation_polarity"),
        ("我去", "你去", "referent"),
        ("明天买三个", "今天买四个", "number_time"),
        ("我买11个", "我买1个", "number_time"),
        ("两两一组", "两一组", "number_time"),
        ("拿-1分", "拿1分", "number_time"),
        ("买1.5斤", "买15斤", "number_time"),
        ("我要买", "我要卖", "action"),
        ("我支持", "我反对", "evaluation_stance"),
        ("她是朋友", "她是废物", "derogatory"),
        ("我听到你不许他去", "我听到你不是他去", "negation_polarity"),
        ("我听到你不许", "我听到他不是", "referent"),
        ("我知道他反对", "我知道", "evaluation_stance"),
        ("那个朋友来了", "朋友来了", "unclassified_lexical"),
        ("没有听过的词", "另一未知词", "unclassified_lexical"),
    ],
)
def test_meaning_changes_cannot_be_erased(a, b, feature):
    r = classify_difference(a, b, glossary_terms=(), policy=load_policy())
    assert r["meaning_carrying"] and feature in r["features"], r


def test_proper_names_come_from_caller_and_preserve_homophone_identity():
    r = classify_difference(
        "跟王慧说话", "跟王惠说话", glossary_terms=["王慧", "王惠"], policy=load_policy()
    )
    assert r["meaning_carrying"] and "proper_name" in r["features"]
    r = classify_difference(
        "王慧，王慧来了", "王慧来了", glossary_terms=["王慧"], policy=load_policy()
    )
    assert not r["meaning_carrying"]
    r = classify_difference(
        "MyGO!!!!!来了", "Ado来了", glossary_terms=["MyGO!!!!!", "Ado"], policy=load_policy()
    )
    assert r["meaning_carrying"] and "proper_name" in r["features"]
    r = classify_difference("我见到了", "我见到", glossary_terms=["了"], policy=load_policy())
    assert r["meaning_carrying"] and "proper_name" in r["features"]
