"""T12 clear-audio contract: observations and literal constraints are distinct."""

from src.autoslice import targeted_relisten as relisten
from src.autoslice.editorial_subtitle import _decision
from src.autoslice.semantic_terminal_adjudication import terminal_observation_projection
import pytest
from tests import test_targeted_relisten_public_contract as protocol
from tests.test_terminal_contract_roundtrip import c_reply, mutation_reply, produce
from tests.test_targeted_relisten_public_contract import rig  # noqa: F401


T12_SRT = ("1\n00:00:00,500 --> 00:00:02,000\n豆沙老师问了一句\n\n"
           "2\n00:00:02,000 --> 00:00:04,000\n我说啊啊啊\n\n"
           "3\n00:00:04,000 --> 00:00:07,000\n豆沙回答稍微有点犹豫\n")
T12_FINDING = {
    "cue_index": 2,
    "suspect": "我说啊啊啊",
    "suggestion": "呃，啊，啊",
    "proposed_full_cue": "呃，啊，啊",
    "repair_class": "disclosure_only",
    "content_risk": "noncritical_uncertainty",
    "risk_basis": "发声字面归属不确定。",
}
ENGLISH_SRT = T12_SRT.replace("我说啊啊啊", "hello")
ENGLISH_FINDING = {
    **T12_FINDING,
    "suspect": "hello",
    "suggestion": "hullo",
    "proposed_full_cue": "hullo",
}
PREFIX_SRT = T12_SRT.replace("我说啊啊啊", "别去啊")
PREFIX_FINDING = {
    **T12_FINDING,
    "suspect": "别去啊",
    "suggestion": "快去啊",
    "proposed_full_cue": "快去啊",
}


@pytest.mark.parametrize('native', [('你听到了，呜呜',), ('你听到了，呜呜', '你听到了，呜呜')])
def test_bound_narrow_asr_is_evidence_without_independent_wide_clear_authority(rig, native):
    from src.autoslice.final_review_contract import is_release_resolved_keep
    audit, captured = produce(rig, c_reply, native=native)
    assert audit.get('policy_branch') == 'SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP', audit
    assert captured['request']['terminal_contract_version'] == 4
    assert not captured['data']['clear_audio_constraints']
    observations = captured['data']['observations']
    assert sum(row.get('receipt_kind') == 'native_transcript' for row in observations) == len(native)
    assert any(row.get('receipt_kind') == 'blind_range' and row.get('target_text') is None
               for row in observations)
    assert is_release_resolved_keep({**audit['rebuilt_finding'], 'exact_release_adjudication': audit})


@pytest.mark.parametrize('current, changed', [('我听到了', '我没听到了'), ('我没听到了', '我听到了')])
def test_two_real_wide_target_projections_keep_polarity_guard(tmp_path, monkeypatch, current, changed):
    source = protocol.SRT.replace('我听到了，呜呜', current)
    finding = {**protocol.FINDING, 'suspect': current, 'suggestion': current + '呀',
               'proposed_full_cue': current + '呀'}
    monkeypatch.setattr(protocol, 'SRT', source)
    rig = protocol.build_offline_rig(tmp_path, monkeypatch, srt=source, finding=finding)
    audit, captured = produce(rig, c_reply, wide=(current, current))
    request = captured['request']
    assert audit.get('policy_branch') == 'SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP', audit
    constraints = captured['data']['clear_audio_constraints']
    assert len(constraints) == 2
    projection = terminal_observation_projection(request)
    qualified = relisten.terminal_clear_audio_catalogue(projection, current_text=current,
        qualification_policy=request['terminal_policy'])
    assert qualified == constraints
    ids = {row['observation_id'] for row in constraints}
    originals = [row for row in request['audio_gain_termination']['projection']['observations']
                 if row['observation_id'] in ids]
    assert {row['raw_observation']['provider'] for row in originals} == {'mai', 'moss'}
    assert all(row['receipt_kind'] == 'blind_range' and row['target_text'] == current for row in originals)
    for preserve_mode in (False, True):
        with pytest.raises(ValueError, match='EDITORIAL_CLEAR_AUDIO_CONFLICT'):
            relisten.protect_clear_audio({**request['check_request'], 'current_cue': changed},
                request['audio'], changed, projection=projection, equivalent_current=preserve_mode,
                policy=request['terminal_policy'])
    edit = {**mutation_reply(captured['data'], captured['refs']), 'edit_start': 0,
        'edit_end': len(current), 'replacement': changed, 'comparison_text': changed,
        'reliable_fragments': [{'text': changed, 'basis': '拒绝含义翻转的负控'}]}
    with pytest.raises(ValueError, match='EDITORIAL_CLEAR_AUDIO_CONFLICT'):
        _decision(request, edit)
    # Repeating the same real provider/model is not two independent routes.
    duplicate = {**projection, 'observations': [originals[0], originals[0]]}
    assert not relisten.terminal_clear_audio_catalogue(duplicate, current_text=current,
        qualification_policy=request['terminal_policy'])


def t12_reply(data, refs):
    base = mutation_reply(data, refs)
    replacement = "呃，啊，啊"
    return {
        **base,
        "edit_start": 0,
        "edit_end": len(data["current"]),
        "replacement": replacement,
        "comparison_text": replacement,
        "reliable_fragments": [{
            "text": replacement,
            "basis": "实际音频只支持三段迟疑发声。",
        }],
        "meaning_basis": "只改写不确定的发声字面，不补造语义。",
        "literal_uncertainty": "跨文字转写不能确定中文字形。",
    }


def build_t12_rig(tmp_path, monkeypatch):
    monkeypatch.setattr(protocol, "SRT", T12_SRT)
    monkeypatch.setattr(protocol, "FINDING", T12_FINDING)
    return protocol.build_offline_rig(tmp_path, monkeypatch,
                                      srt=T12_SRT, finding=T12_FINDING)


def test_cross_script_native_producer_roundtrip_keeps_observations_out_of_catalogue(
        tmp_path, monkeypatch):
    """Korean/Latin pronunciation spellings remain evidence, not literal Chinese."""
    rig = build_t12_rig(tmp_path, monkeypatch)
    audit, captured = produce(rig, t12_reply, native=("어, 아, 아,", "Oh, ah, ah."))

    assert audit.get("repaired") is True, audit
    projection = terminal_observation_projection(captured["request"])
    native = [row for row in projection["observations"]
              if row.get("receipt_kind") == "native_transcript"]
    assert {row["target_text"] for row in native} >= {"어, 아, 아,", "Oh, ah, ah."}
    assert captured["data"]["clear_audio_constraints"] == []
    assert relisten.terminal_clear_audio_catalogue(
        projection, current_text="我说啊啊啊") == []


def test_native_hesitation_target_can_change_script_through_typed_editor(tmp_path, monkeypatch):
    current = '어, 아, 아'
    source = T12_SRT.replace('我说啊啊啊', current)
    finding = {**T12_FINDING, 'suspect': current}
    monkeypatch.setattr(protocol, 'SRT', source)
    monkeypatch.setattr(protocol, 'FINDING', finding)
    rig = protocol.build_offline_rig(tmp_path, monkeypatch, srt=source, finding=finding)
    audit, captured = produce(rig, t12_reply, native=(current, current))
    assert audit.get('repaired') is True, audit
    assert audit['editorial_authority']['proposed_cue'] == '呃，啊，啊'
    projection = terminal_observation_projection(captured['request'])
    assert current in {row.get('target_text') for row in projection['observations']}


def test_chinese_native_producer_roundtrip_still_rejects_conflicting_edit(
        tmp_path, monkeypatch):
    """Filtering foreign scripts must not weaken a genuine Chinese transcript guard."""
    rig = build_t12_rig(tmp_path, monkeypatch)
    audit, captured = produce(rig, c_reply, wide=("我说啊啊啊", "我说啊啊啊"))

    assert audit.get("policy_branch") == "SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP", audit
    request = captured["request"]
    projection = terminal_observation_projection(request)
    assert captured["data"]["clear_audio_constraints"]
    with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
        _decision(request, t12_reply(captured["data"], captured["refs"]))
    with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
        relisten.protect_clear_audio(request["check_request"], request["audio"],
                                     "呃，啊，啊", projection=projection)


def test_original_language_ascii_target_remains_a_literal_constraint():
    """An explicitly labelled English target keeps the established exact guard."""
    projection = {
        "observations": [{
            "observation_id": "english-original",
            "receipt_kind": "original_language",
            "target_text": "hello",
        }],
    }
    catalogue = relisten.terminal_clear_audio_catalogue(projection)
    assert [row["text"] for row in catalogue] == ["hello"]
    relisten.protect_clear_transcript("hello", "hello", receipt_kind="original_language")
    with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
        relisten.protect_clear_transcript("hello", "hullo", receipt_kind="original_language")


def test_native_english_target_remains_a_literal_constraint(tmp_path, monkeypatch):
    """Target-aware matching preserves an actual English native transcript."""
    monkeypatch.setattr(protocol, "SRT", ENGLISH_SRT)
    monkeypatch.setattr(protocol, "FINDING", ENGLISH_FINDING)
    rig = protocol.build_offline_rig(tmp_path, monkeypatch,
                                     srt=ENGLISH_SRT, finding=ENGLISH_FINDING)
    audit, captured = produce(rig, c_reply, wide=("hello", "hello"))

    assert audit.get("policy_branch") == "SUB18_CONTEXT_MEANING_EQUIVALENT_KEEP", audit
    assert {row["text"] for row in captured["data"]["clear_audio_constraints"]} == {"hello"}
    assert len(captured["data"]["clear_audio_constraints"]) == 2
    request = captured["request"]
    conflicting = t12_reply(captured["data"], captured["refs"])
    conflicting.update({"replacement": "hullo", "comparison_text": "hullo",
                        "reliable_fragments": [{"text": "hullo", "basis": "负控"}]})
    with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
        _decision(request, conflicting)


def test_mixed_han_target_keeps_literal_guard_strict():
    """A mixed Chinese/Latin target still rejects a changed Chinese word."""
    current = "谢谢小鸡，总能不懂得让人打call"
    projection = {
        "observations": [{
            "observation_id": "mixed-native",
            "receipt_kind": "native_transcript",
            "target_text": current,
        }],
    }
    assert relisten.terminal_clear_audio_catalogue(
        projection, current_text=current)[0]["text"] == current
    assert relisten.terminal_clear_audio_catalogue({
        "observations": [{**projection["observations"][0], "target_text": "call"}],
    }, current_text=current) == []
    with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
        relisten.protect_clear_transcript(current, current.replace("打call", "打卡"),
                                          target_text=current)


def test_mixed_han_foreign_producer_keeps_reliable_prefix_guard(tmp_path, monkeypatch):
    """An unsupported suffix becomes a gap; a clear Han prefix still vetoes edits."""
    monkeypatch.setattr(protocol, "SRT", PREFIX_SRT)
    monkeypatch.setattr(protocol, "FINDING", PREFIX_FINDING)
    rig = protocol.build_offline_rig(tmp_path, monkeypatch,
                                     srt=PREFIX_SRT, finding=PREFIX_FINDING)

    def reply(data, refs):
        answer = t12_reply(data, refs)
        return {**answer, "replacement": "快去啊", "comparison_text": "快去啊",
                "reliable_fragments": [{"text": "快去啊", "basis": "负控"}]}

    audit, captured = produce(rig, reply, wide=("别去Ω", "别去Ω"))
    assert audit.get("repaired") is False
    assert audit.get("status") == "UNCERTAIN"
    request = captured["request"]
    projection = terminal_observation_projection(request)
    catalogue = relisten.terminal_clear_audio_catalogue(
        projection, current_text="别去啊")
    assert catalogue and catalogue[0]["text"].startswith("别去")
    assert "[unclear]" in catalogue[0]["text"]
    with pytest.raises(ValueError, match="EDITORIAL_CLEAR_AUDIO_CONFLICT"):
        relisten.protect_clear_audio(request["check_request"], request["audio"],
                                     "快去啊", projection=projection)
