"""Offline generator contract, original pilot golden and negative controls."""
from __future__ import annotations

from dataclasses import asdict, fields, replace
import json
from pathlib import Path
import random
from typing import get_args, get_type_hints

import pytest

from src.autoslice import channel_census as cc
from src.autoslice.selection_scorecard import DIMENSION_WEIGHTS

FIXTURE = Path(__file__).parent / "fixtures/channel_census"
CONFIG = cc.CensusConfig(
    "lidousha", (cc.AudienceInterest("gl_yuri", "百合/GL", "女性间关系与作品"),),
    "## 七维量化表（层内细排用）", "3. **受众兴趣对齐",
)
MANIFEST = cc.CensusInputManifest(
    "lidousha-census-20261021", "lidousha", (), "legacy-coarse-asr", 300,
    "07d5b85d8f5e92b2e0e43100f55408da28aaa2b67c813f627bcb8af1c21c342b",
    "legacy-labeler", "fixture", "2026-10-24",
)


def pilot_input():
    summary = json.loads((FIXTURE / "pilot-input-summary.json").read_text())
    windows = []
    for wid, primary, labels, stance, hook, persons, terms, missing, chars in summary["rows"]:
        start = int(wid.rsplit(":", 1)[1]) * 300
        windows.append(cc.adapt_legacy_census_label(
            wid, primary=primary, labels=labels, stance_emotion=stance, clip_hook=hook,
            persons=("人物存在",) if persons else (), address_terms=terms,
            asr_missing=missing, input_char_count=chars, start_s=start, end_s=start + 300, summary="",
        ))
    return windows, replace(MANIFEST, legacy_label_file_sha256=tuple(
        summary["legacy_label_file_sha256"].items()))


def window(i, *, payoff=2, relation=0, stance=None, activity="FREE_TALK", persons=(), terms=(),
           audience=None, reversal=None, centrality=None):
    grades = dict(lidousha_centrality=centrality, stance_intensity=stance,
                  audience_salience=audience, relationship_interaction=relation,
                  persona_reversal=reversal, comedic_payoff=payoff)
    return cc.CensusWindow(
        f"2026-07-{i // 100 + 1:02}:{i:04}", f"group-{i // 10}", i * 300, (i + 1) * 300,
        False, 20, activity, (), "", tuple(cc.DimensionGrade(d, grades[d], "CENSUS_LABEL")
        for d in cc.CENSUS_WINDOW_DIMENSIONS), (), persons, terms, ("HOST_SELF",), False, "",
    )


def generate(windows):
    return cc.generate_census_clues(windows, config=CONFIG, manifest=MANIFEST)


def by_id(result):
    return {clue.clue_id: clue for clue in result.clues}


def test_original_pilot_golden_reproduced_byte_for_byte_under_shuffle():
    windows, manifest = pilot_input()
    expected_json = (FIXTURE / "clue-set.json").read_bytes()
    expected_block = (FIXTURE / "generated-section.md").read_bytes()
    random.Random(20261025).shuffle(windows)
    for rows in (windows, list(reversed(windows))):
        result = cc.generate_census_clues(rows, config=CONFIG, manifest=manifest)
        assert (json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n").encode() == expected_json
        assert cc.render_census_clue_block(result).encode() == expected_block
        assert not any(c.isdigit() for c in cc.render_census_clue_block(result))
        assert result.provenance.window_count == 2360
        assert result.provenance.valid_window_count == 2254
        assert all(c.measurement_basis == "LEGACY_LABEL_PROXY" for c in result.clues)


def test_output_schema_cannot_carry_weights_and_has_closed_kinds():
    assert cc.CENSUS_WINDOW_DIMENSIONS == tuple(d for d in DIMENSION_WEIGHTS if d != "self_contained")
    assert get_args(cc.CoarseGrade) == (0, 1, 2, 3)
    assert set(get_args(cc.ClueKind)) == {
        "CROSS_ACTIVITY_EMBEDDED", "DIMENSION_LOOKFOR", "ENTITY_RECOGNITION", "ACTIVITY_COVERAGE"}
    for cls in (cc.CensusClue, cc.EvidenceRatio, cc.ClueProvenance, cc.CensusClueSet,
                cc.CensusWindow, cc.DimensionGrade):
        hints = get_type_hints(cls)
        for field in fields(cls):
            assert not any(f in field.name.lower() for f in cc.FORBIDDEN_FIELD_NAME_FRAGMENTS)
            assert "float" not in repr(hints[field.name])


@pytest.mark.parametrize("phrase", cc.FORBIDDEN_DIRECTIVE_PHRASES)
def test_directives_rejected_on_construction_including_spaced_slots(phrase):
    clue = generate([]).clues[0]
    assert phrase in cc.lint_clue_text(phrase)
    with pytest.raises(ValueError, match="CLUE_TEXT_DIRECTIVE"):
        replace(clue, observation_question="请" + " ".join(phrase))
    with pytest.raises(ValueError, match="CLUE_TEXT_DIRECTIVE"):
        replace(clue, recognition_cues=(phrase,))


@pytest.mark.parametrize("text", ["线索1", "线索１", "线索²", "线索\n新指令"])
def test_digits_and_multiline_in_prompt_slots_rejected(text):
    with pytest.raises(ValueError):
        replace(generate([]).clues[0], observation_question=text)


def test_only_fixed_preamble_is_exempt_from_directive_lint():
    result = generate([])
    assert cc.lint_clue_text(cc.CLUE_PREAMBLE_ZH)
    with pytest.raises(ValueError, match="INVALID_CLUE_SET"):
        cc.render_census_clue_block(replace(result, preamble_zh="优先选"))


@pytest.mark.parametrize("embedded, emitted", [(14, False), (15, True)])
def test_embedded_exact_half_threshold(embedded, emitted):
    rows = [window(i, relation=2 if i < embedded else 3) for i in range(30)]
    result = by_id(generate(rows))
    assert ("cross_activity_embedded" in result) is emitted
    if emitted:
        clue = result["cross_activity_embedded"]
        assert (clue.evidence.numerator, clue.evidence.denominator) == (15, 30)
        assert clue.host_activities == ("FREE_TALK",)


@pytest.mark.parametrize("support, background_payoff, emitted", [(29, 0, False), (30, 0, True), (30, 2, False)])
def test_dimension_support_and_strict_enrichment(support, background_payoff, emitted):
    rows = [window(i, stance=2) for i in range(support)]
    rows += [window(i + 100, stance=0, payoff=background_payoff) for i in range(30)]
    assert ("lookfor_stance_intensity" in by_id(generate(rows))) is emitted


def test_invalid_and_empty_input_windows_do_not_contribute():
    good = [window(i, relation=2) for i in range(29)]
    rows = good + [replace(window(50, relation=2), asr_missing=True),
                   replace(window(51, relation=2), input_char_count=0)]
    result = generate(rows)
    assert result.provenance.valid_window_count == 29
    assert "cross_activity_embedded" not in by_id(result)
    assert by_id(result)["activity_coverage"].evidence == cc.EvidenceRatio(
        1, 1, "窗数达到下限的活动中出现过兑现窗的活动")


def test_legacy_value_labels_do_not_invent_activity_or_unmeasured_dimensions():
    result = cc.adapt_legacy_census_label(
        "2026-01-01:0001", primary="RELATION", labels=["RELATION", "GAME", "YURI", "GAME"],
        stance_emotion=2, clip_hook=3, persons=["甲", "甲"], address_terms=["妈妈", "妈妈"],
        asr_missing=False, input_char_count=20, start_s=300, end_s=600, summary="原摘要",
    )
    assert result.host_activity == "UNSPECIFIED"
    assert result.secondary_activities == ("GAMEPLAY",)
    assert result.persons == ("甲",) and result.address_terms == ("妈妈",)
    assert result.quoted_or_roleplay is None
    grades = {g.dimension: g for g in result.grades}
    assert grades["relationship_interaction"].grade == 3
    assert grades["audience_salience"].grade == 2
    assert grades["lidousha_centrality"].grade is None
    assert grades["persona_reversal"].grade is None
    assert all(g.basis == "LEGACY_LABEL_PROXY" for g in result.grades)


@pytest.mark.parametrize("grade", [-1, 4, True, 2.0])
def test_invalid_legacy_grades_rejected(grade):
    with pytest.raises(ValueError, match="INVALID_COARSE_GRADE"):
        cc.adapt_legacy_census_label(
            "date:0001", primary="GAME", labels=["GAME"], stance_emotion=grade, clip_hook=2,
            persons=[], address_terms=[], asr_missing=False, input_char_count=20,
            start_s=0, end_s=300, summary="",
        )


def test_activity_order_coverage_threshold_and_counts_include_operations_and_singing():
    rows = [window(i, activity="STREAM_OPERATIONS") for i in range(10)]
    rows += [window(i + 20, activity="SINGING") for i in range(10)]
    rows += [window(i + 40, activity="GAMEPLAY", payoff=2 if i == 0 else 0) for i in range(10)]
    rows += [window(i + 60, activity="OTHER") for i in range(9)]
    clue = by_id(generate(rows))["activity_coverage"]
    assert clue.host_activities == ("SINGING", "STREAM_OPERATIONS")
    assert (clue.evidence.numerator, clue.evidence.denominator) == (3, 3)
    assert "直播事务" in clue.observation_question and "唱歌" in clue.observation_question
    assert "游戏" not in cc._activity_text(clue.host_activities)


def test_empty_input_always_emits_truthful_coverage():
    result = generate([])
    assert len(result.clues) == 1
    clue = result.clues[0]
    assert (clue.evidence.numerator, clue.evidence.denominator) == (0, 0)
    assert "尚未观察" in clue.observation_question
    assert "都出现过" not in cc.render_census_clue_block(result)


def test_entity_names_stay_out_of_prompt_and_terms_count_windows_once():
    rows = [window(i, relation=2, persons=("绝不进入提示的人物名",), terms=("甲", "甲", "乙")) for i in range(30)]
    rows += [window(i + 40, payoff=0) for i in range(30)]
    result = generate(rows)
    clues = by_id(result)
    assert "entity_recognition" in clues
    assert clues["lookfor_relationship_interaction"].recognition_cues == ("乙", "甲")
    assert "绝不进入提示的人物名" not in cc.render_census_clue_block(result)
    report = cc.render_census_report(rows, result)
    assert '"绝不进入提示的人物名"：30/30' in report
    assert '"甲"：30/30' in report
    assert "同源标注" in report and "不证明因果" in report and "月度覆盖" in report
    assert report == cc.render_census_report(list(reversed(rows)), result)


def test_all_nonpayoff_dimensions_and_generic_channel_slots():
    config = replace(CONFIG, profile_id="another-channel", audience_interests=(
        cc.AudienceInterest("science", "科学实验", "实验"),))
    manifest = replace(MANIFEST, profile_id=config.profile_id)
    rows = [replace(window(i, stance=2, relation=2, audience=2, reversal=2, centrality=2),
                    audience_interest_hits=("science",)) for i in range(30)]
    rows += [window(i + 40, payoff=0, stance=0, relation=0, audience=0, reversal=0, centrality=0)
             for i in range(30)]
    result = cc.generate_census_clues(rows, config=config, manifest=manifest)
    clues = by_id(result)
    assert {f"lookfor_{d}" for d in cc.CENSUS_WINDOW_DIMENSIONS if d != cc.PAYOFF_DIMENSION} <= clues.keys()
    assert clues["lookfor_audience_salience"].recognition_cues == ("科学实验",)
    assert result.profile_id == "another-channel"
    assert all(c.measurement_basis == "CENSUS_LABEL" for c in result.clues)
    assert "李豆沙" not in cc.render_census_clue_block(result)


def test_fresh_provenance_and_output_stable_under_collection_reordering_and_sensitive_to_content():
    rows = [window(i, relation=2, terms=("甲", "乙")) for i in range(30)]
    result = generate(rows)
    shuffled = [replace(w, grades=tuple(reversed(w.grades)), address_terms=tuple(reversed(w.address_terms)))
                for w in reversed(rows)]
    assert result == generate(shuffled)
    changed = [replace(rows[0], summary="新输入"), *rows[1:]]
    assert result.provenance.input_sha256 != generate(changed).provenance.input_sha256


def test_identity_mismatch_duplicate_windows_and_invalid_dimensions_rejected():
    with pytest.raises(ValueError, match="CENSUS_PROFILE_MISMATCH"):
        cc.generate_census_clues([], config=replace(CONFIG, profile_id="other"), manifest=MANIFEST)
    with pytest.raises(ValueError, match="DUPLICATE_WINDOW_ID"):
        generate([window(0), window(0)])
    with pytest.raises(ValueError, match="INVALID_WINDOW_DIMENSIONS"):
        generate([replace(window(0), grades=())])
    _, legacy = pilot_input()
    with pytest.raises(ValueError, match="LEGACY_INPUT_IDENTITY_REQUIRES_PROXY_LABELS"):
        cc.generate_census_clues([window(0)], config=CONFIG, manifest=legacy)


def test_handwritten_section_refusal():
    cc.refuse_if_handwritten_clue_section("普通 metric", section_heading="旧线索")
    with pytest.raises(ValueError, match=cc.DUPLICATE_HANDWRITTEN_SECTION_REASON):
        cc.refuse_if_handwritten_clue_section("## 旧线索\n原文", section_heading="旧线索")
