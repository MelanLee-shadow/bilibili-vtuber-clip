"""F2 class members through real consumers; no provider/network calls.

Two guards restore the exact responsible main generator, not a reconstructed
old predicate. The same ordinary input must then fail the repaired contract.
"""
from copy import deepcopy
import json
import re

import pytest

from tests.f2_baseline import baseline_module


@pytest.mark.parametrize("heard, rendered", [
    ("えー、それは", "呃，それは"),
    ("あの、不要走", "呃，不要走"),
    ("えっと、不要走", "呃，不要走"),
    ("うーん、不要走", "嗯，不要走"),
    ("uh, 不要走", "呃，不要走"),
    ("um, 不要走", "嗯，不要走"),
    ("er, 不要走", "呃，不要走"),
    ("hmm, 不要走", "嗯，不要走"),
    ("어, 아, 아", "呃，啊，啊"),
])
def test_foreign_hesitations_reach_the_clear_audio_consumer(heard, rendered):
    from src.autoslice.targeted_relisten import protect_clear_transcript
    protect_clear_transcript(heard, rendered)


def test_kana_guard_restores_main_sound_generator(monkeypatch):
    from src.autoslice import nonsemantic_sound as sounds
    from src.autoslice.targeted_relisten import protect_clear_transcript
    old = baseline_module("nonsemantic_sound")
    def old_comparison(text, proposed, *, policy=None):
        # Only adapt the new keyword; execute the frozen old generator itself.
        assert policy is None
        return old.clear_comparison(text, proposed)
    with monkeypatch.context() as original:
        original.setattr(sounds, "clear_comparison", old_comparison)
        with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
            protect_clear_transcript("えー、それは", "呃，それは")
    protect_clear_transcript("えー、それは", "呃，それは")
    protect_clear_transcript("えー、不要走", "不要走")
    # The requested Japanese/Chinese examples share only the hesitation.
    # Sound spelling grants no permission to translate clear lexical words.
    assert sounds.hesitation_spelling("えー、それは") == "呃、それは"
    assert sounds.hesitation_spelling("呃，那是") == "呃，那是"
    with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
        protect_clear_transcript("えー、それは", "呃，那是")


@pytest.mark.parametrize("word, wrong", [
    ("あの人", "呃人"), ("あーちゃん", "啊ちゃん"),
    ("えーそれは", "呃それは"), ("아이", "啊"),
    ("아니", "啊"), ("uhuru", "呃"), ("uh_1", "呃"),
    ("哈哈镜", "镜"), ("不要走", "要走"), ("啊", ""),
])
def test_word_boundaries_and_audible_lexical_guards_survive(word, wrong):
    from src.autoslice.nonsemantic_sound import hesitation_spelling, sound_only, substantive_text
    from src.autoslice.targeted_relisten import protect_clear_transcript
    assert hesitation_spelling(word) == word
    assert substantive_text(word) == word
    if word != "啊":
        assert not sound_only(word)
    with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
        protect_clear_transcript(word, wrong)


@pytest.mark.parametrize("sound", ["哈哈", "嘿嘿嘿", "呵呵", "嘻嘻", "uh", "えー", "あの", "うーん"])
def test_new_and_existing_sound_members_reach_discovery_consumers(sound):
    from src.autoslice.nonsemantic_sound import sound_only
    from src.autoslice.restatement_recall import is_nonlexical_interjection
    assert sound_only(sound)
    assert is_nonlexical_interjection(sound)
    assert not sound_only(sound + "人")


def test_singleton_additions_and_prefix_scoring_do_not_become_omission_authority():
    from src.autoslice.nonsemantic_sound import substantive_text
    from src.autoslice.restatement_recall import strip_leading_connectors
    from src.autoslice.speaker_context import _singleton_nonlexical_dominant
    from src.autoslice.targeted_relisten import protect_clear_transcript
    assert _singleton_nonlexical_dominant("我这，哈哈")
    assert _singleton_nonlexical_dominant("我这，笑死")
    assert not _singleton_nonlexical_dominant("你好哈哈")
    assert strip_leading_connectors("然后呃我看就是") == "我看就是"
    for word in ("哈利波特", "了不起", "那个人", "hahahand"):
        assert strip_leading_connectors(word) == word
    assert substantive_text("笑死") == "笑死"
    for original in ("笑死", "但是", "然后", "就是"):
        with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
            protect_clear_transcript(original, "")


@pytest.mark.parametrize("text", ["咳咳", "咳嗯", "额额", "额啊"])
@pytest.mark.parametrize("audible", [False, True])
def test_microcue_vocalizations_still_reach_the_blind_witness(text, audible):
    from src.autoslice.microcue_acoustic_discovery import discover_microcue_findings
    from src.autoslice.nonsemantic_sound import sound_only
    assert sound_only(text)  # Sound identity is not permission to skip review.
    source = f"1\n00:00:00,000 --> 00:00:01,121\n{text}\n"
    calls = []
    def verify(request):
        assert "current_cue" not in request and "proposed_cue" not in request
        calls.append(request)
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED", "target_audible": audible,
            "heard_pinyin": "ke ke" if audible else "",
            "syllable_count": 2 if audible else 0,
            "uncertain_positions": [], "confidence": 1.0,
        }
    findings, audit = discover_microcue_findings(
        source, timeline_offset_ms=9_660, entity_verifier=verify)
    assert len(calls) == 1 and audit["eligible_count"] == 1
    assert audit["mutation_authorized"] is False
    assert source.endswith(text + "\n")
    if audible:
        assert all(row["repair_class"] != "acoustic_drop_cue" for row in findings)
    else:
        assert len(findings) == 1
        assert findings[0]["repair_class"] == "acoustic_drop_cue"
        assert findings[0]["proposed_full_cue"] == ""
        assert findings[0]["suspect"] == text
        assert audit["eligible"][0]["status"] == "INAUDIBLE_DROP_PROPOSED_TO_CPA"


@pytest.mark.parametrize("text", ["欸欸", "唉唉", "哼哼", "嘿嘿", "嘛嘛", "嗯嗯"])
def test_microcue_filler_role_still_skips_blind_audio(text):
    from src.autoslice.microcue_acoustic_discovery import discover_microcue_findings
    def forbidden(_request):
        raise AssertionError("Existing filler exclusion must not acquire audio")
    findings, audit = discover_microcue_findings(
        f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n",
        timeline_offset_ms=0, entity_verifier=forbidden)
    assert findings == [] and audit["eligible"] == []
    assert audit["reason_code"] == "NO_ELIGIBLE_MICROCUES"


def _topic_arguments(prefix):
    return {"current": prefix + "提自己", "proposed": prefix + "买东西",
        "evidence": {"evidence_level": "CURRENT_TEXT_PINYIN_WEAK"},
        "topic_context": "中文讨论"}


def _phoneme_spy(calls):
    def pinyin(text):
        calls.append(text)
        return {"提自己": "ti zi ji", "买东西": "mai dong xi"}[text]
    return pinyin


@pytest.mark.parametrize("prefix", ["kmx说", "B站说", "OK，", "えー、", "아이说"])
def test_unchanged_foreign_context_keeps_the_mandarin_phoneme_gate(monkeypatch, prefix):
    from src.autoslice import closed_set_proposal_rebuild as rebuild
    calls = []
    monkeypatch.setattr(rebuild, "_pinyin", _phoneme_spy(calls))
    payload = {"proposal_basis": "topic_homophone", "topic_support": ["中文讨论"]}
    assert rebuild.topic_proposal_error(payload, **_topic_arguments(prefix)) == "TOPIC_PROPOSAL_PHONEME_MISMATCH"
    assert calls == ["提自己", "买东西"]


def test_kmx_guard_restores_main_nomination_generator(monkeypatch):
    from src.autoslice import closed_set_proposal_rebuild as rebuild
    payload = {"proposal_basis": "topic_homophone", "topic_support": ["中文讨论"]}
    calls = []
    monkeypatch.setattr(rebuild, "_pinyin", _phoneme_spy(calls))
    old = baseline_module("closed_set_proposal_rebuild")
    with monkeypatch.context() as original:
        original.setattr(rebuild, "requires_original_language_evidence", old.requires_original_language_evidence)
        # The old ordinary producer bypasses the gate; it violates the new
        # assertion for precisely the unchanged-context reason under repair.
        assert rebuild.topic_proposal_error(payload, **_topic_arguments("kmx说")) is None
        assert calls == []
    assert rebuild.topic_proposal_error(payload, **_topic_arguments("kmx说")) == "TOPIC_PROPOSAL_PHONEME_MISMATCH"
    assert calls == ["提自己", "买东西"]


@pytest.mark.parametrize("current, proposed, expected", [
    ("提自己", "きつい", True), ("提自己", "hello", True),
    ("提自己", "아이", True), ("きつい", "提自己", True),
    ("kmx，提自己，OK", "kmx，买东西，OK", False),
    ("kmx，提自己，OK", "kmx，提自己，OK", False),
    ("他说提自己", "他说提自己uh", True),
    ("他说uh提自己", "他说提自己", True),
    ("kmx提自己，OK提自己", "kmx买东西，OK买东西", False),
])
def test_each_non_equal_opcode_is_the_nomination_scope(current, proposed, expected):
    from src.autoslice.closed_set_proposal_rebuild import requires_original_language_evidence
    assert requires_original_language_evidence(current, proposed) is expected


def test_original_language_nomination_still_requires_source_and_speech():
    from src.autoslice.closed_set_proposal_rebuild import topic_proposal_error
    payload = {"proposal_basis": "topic_homophone", "topic_support": ["日语感叹"]}
    args = {"current": "提自己", "proposed": "きつい", "topic_context": "日语感叹",
            "evidence": {"evidence_level": "CURRENT_TEXT_PINYIN_WEAK"}}
    assert topic_proposal_error(payload, **args) is None
    assert topic_proposal_error({**payload, "topic_support": ["无来源"]}, **args) == "TOPIC_PROPOSAL_SUPPORT_MISSING"
    assert topic_proposal_error(payload, **{**args, "evidence": {
        "evidence_level": "NO_SPEECH_OBSERVED"}}) == "TOPIC_PROPOSAL_NO_SPEECH"


def test_temporary_policy_change_reaches_sound_consumers(tmp_path, monkeypatch):
    from src.autoslice import nonsemantic_sound as sounds
    from src.autoslice import subtitle_acceptability_policy as policies
    from src.autoslice.meaning_difference import classify_difference
    from src.autoslice.microcue_acoustic_discovery import discover_microcue_findings, _eligibility_scope
    from src.autoslice.restatement_recall import is_nonlexical_interjection, strip_leading_connectors
    from src.autoslice.semantic_terminal_adjudication import written_meaning_conflict
    from src.autoslice.speaker_context import _singleton_nonlexical_dominant
    from src.autoslice.targeted_relisten import protect_clear_transcript
    policy = deepcopy(policies.load_policy())
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sounds, "load_policy", lambda: policies.load_policy(path))
    assert not is_nonlexical_interjection("嘟")
    assert not _singleton_nonlexical_dominant("我这，嘟")
    assert _eligibility_scope("嘟", 1000) == "standard"
    diff = classify_difference("嘟", "我", glossary_terms=[], policy=policy)
    assert written_meaning_conflict("嘟", diff, policy)
    policy["features"]["modal_particles"].append("嘟")
    policy["features"]["fillers"].append("咕噜")
    path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    assert sounds.sound_only("嘟") and sounds.sound_only("咕噜")
    assert is_nonlexical_interjection("嘟") and is_nonlexical_interjection("咕噜")
    assert strip_leading_connectors("えー、我知道") == "我知道"
    assert _singleton_nonlexical_dominant("我这，嘟")
    assert not written_meaning_conflict("嘟", diff, policies.load_policy(path))
    def forbidden(_request):
        raise AssertionError("A policy-defined sound cue must not call the acoustic provider")
    findings, audit = discover_microcue_findings(
        "1\n00:00:00,000 --> 00:00:01,000\n嘟\n", timeline_offset_ms=0, entity_verifier=forbidden)
    assert findings == [] and audit["eligible"] == []
    assert audit["reason_code"] == "NO_ELIGIBLE_MICROCUES"
    # Removal must propagate too: the ASR asset cannot override the policy.
    protect_clear_transcript("uh", "呃", policy=policies.load_policy(path))
    policy["features"]["modal_particles"].remove("呃")
    path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    assert sounds.hesitation_spelling("uh") == "uh"
    with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
        protect_clear_transcript("uh", "呃", policy=policies.load_policy(path))


def test_script_distribution_and_existing_chat_normalization_scope():
    from src.autoslice.text_scripts import script_profile
    from src.autoslice.chat_evidence import normalize_chat_text
    assert script_profile("Abあア아이中文7!") == {
        "latin": 2, "kana": 2, "hangul": 2, "han": 2, "other": 2}
    legacy = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", re.I)
    for text in ("kmx，B站，OK", "あの人", "아이啊", "İıſK", "㐀中文１２3！"):
        assert normalize_chat_text(text) == legacy.sub("", text).lower()


def test_shared_script_detector_preserves_clear_audio_projection_scope():
    from src.autoslice.targeted_relisten import _clear_audio_constraint_projection
    old = baseline_module("targeted_relisten")
    for heard in ("别去Ω", "别去啊", "hello", "아이", "あの人", "１２3", "[unclear]", "㐀中"):
        for target in (None, "别去啊", "hello", "あの人", "１２3"):
            for comparable in (False, True):
                args = {"target_text": target, "guard_comparable": comparable}
                assert _clear_audio_constraint_projection(heard, **args) == old._clear_audio_constraint_projection(heard, **args)
