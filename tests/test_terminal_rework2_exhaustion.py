"""D: target-level terminal material exhaustion and ordinary owner completion."""

from copy import deepcopy
from pathlib import Path

import pytest

from src.autoslice.review_errors import ProviderFailure

from src.autoslice import targeted_relisten as target_material
from src.autoslice.exact_final_terminal_funnel import review_pass_receipt
from tests.test_targeted_relisten_public_contract import acquire_narrow, build_offline_rig


def _srt(current):
    return (
        "1\n00:00:00,500 --> 00:00:02,000\n前一句问法\n\n"
        f"2\n00:00:02,000 --> 00:00:04,000\n{current}\n\n"
        "3\n00:00:04,000 --> 00:00:07,000\n相邻回答提供语境\n\n"
        "4\n00:00:07,000 --> 00:00:09,000\n后一句继续说明\n"
    )


def _rig(tmp_path, monkeypatch, current="我听到了，呜呜", proposed="我听到了，哼唱", why=None):
    finding = {
        "cue_index": 2,
        "suspect": current,
        "suggestion": proposed,
        "proposed_full_cue": proposed,
        "repair_class": "spoken_unit",
        "content_risk": "factual_error",
        "risk_basis": "只验证目标级听音通道，不把文字候选当作声音证据。",
    }
    if why is not None:
        finding["why"] = why
    return build_offline_rig(
        tmp_path,
        monkeypatch,
        srt=_srt(current),
        finding=finding,
        source_duration_ms=10_000,
    )


def _marked(check):
    marked = deepcopy(check)
    marked["target_material_contract"] = target_material.TARGET_MATERIAL_CONTRACT
    marked.pop("request_sha256", None)
    marked["request_sha256"] = target_material.digest(marked)
    return marked


def test_zero_and_narrow_materials_still_require_wide_candidate_blind_channel(tmp_path, monkeypatch):
    rig = _rig(tmp_path / "zero", monkeypatch)
    zero = target_material.target_material_exhaustion(rig.check)
    assert zero["required_channels"] == ["WIDE_CANDIDATE_BLIND_AUDIO"]
    assert zero["missing_channels"] == ["WIDE_CANDIDATE_BLIND_AUDIO"]
    assert zero["exhausted"] is False

    witness = acquire_narrow(rig)
    narrow_projection = target_material.evidence_projection(rig.check, witness=witness)
    narrow = target_material.target_material_exhaustion(rig.check, narrow_projection)
    assert narrow["missing_channels"] == ["WIDE_CANDIDATE_BLIND_AUDIO"]
    assert narrow["completed_channels"]["WIDE_CANDIDATE_BLIND_AUDIO"] == []
    assert narrow["exhausted"] is False

    finding = deepcopy(rig.finding)
    receipt = review_pass_receipt(
        pass_index=1,
        srt_text=rig.srt,
        audit={"findings": [finding]},
        repaired_text=rig.srt,
        repairs=[],
    )
    with pytest.raises(ProviderFailure, match="TARGET_MATERIAL_CHANNELS_REQUIRED:WIDE_CANDIDATE_BLIND_AUDIO"):
        target_material.compile_loop_stop_no_gain(
            check=_marked(rig.check), finding=finding, pass_receipts=[receipt]
        )


def test_single_cue_widest_real_owner_channel_is_not_rejected_for_source_end_padding(tmp_path, monkeypatch):
    srt = '1\n00:00:00,500 --> 00:00:02,000\n我听到了\n'
    finding = {'cue_index': 1, 'suspect': '我听到了', 'suggestion': '我已经听到',
        'proposed_full_cue': '我已经听到', 'repair_class': 'spoken_unit'}
    rig = build_offline_rig(tmp_path, monkeypatch, srt=srt, finding=finding,
        source_duration_ms=2_000)
    assert rig.check['context_end_ms'] > 2_000
    check = target_material.complete_target_materials(check=rig.check,
        finding=deepcopy(finding), adapters={'entity_verifier': rig.verifier,
            'provider_calls_allowed': True})
    proof = target_material.require_target_material_exhaustion(check)
    assert proof['exhausted'] is True
    assert proof['completed_channels']['WIDE_CANDIDATE_BLIND_AUDIO']
    assert rig.transport.calls


def test_offline_preflight_returns_required_channel_and_rejects_inconsistent_report(tmp_path, monkeypatch):
    from scripts.predeploy_contract_gauntlet import verify_material_return, replay_exit_code
    rig = _rig(tmp_path, monkeypatch)
    check = _marked(rig.check)
    finding = deepcopy(rig.finding)
    receipt = review_pass_receipt(pass_index=1, srt_text=rig.srt,
        audit={'findings': [finding]}, repaired_text=rig.srt, repairs=[])
    report = verify_material_return(check, finding, [receipt])
    assert report['requested_channels'] == ['WIDE_CANDIDATE_BLIND_AUDIO']
    assert report['target_material'] == target_material.target_material_exhaustion(check)
    row = {'status': 'EXPECTED_MATERIAL_RETURN', 'target_material_return': report}
    assert replay_exit_code([row]) == 0
    assert rig.transport.calls == []
    assert 'target_material_attempts' not in finding
    for field, value in [('requested_channels', ['ARGON_UPGRADE']),
                         ('judge_dispatched', True), ('audio_owner_dispatched', True),
                         ('terminal_admission_reason', 'OTHER_FAILURE')]:
        bad = deepcopy(row)
        bad['target_material_return'][field] = value
        assert replay_exit_code([bad]) == 1
    assert replay_exit_code([{'status': 'PREPARE_REJECTED',
        'reason': report['completion_reason']}]) == 1
    def wrong_completion(**kwargs):
        raise ValueError('TARGET_MATERIAL_CHANNELS_REQUIRED:ARGON_UPGRADE')
    with pytest.raises(ValueError, match='GAUNTLET_MATERIAL_RETURN_CONTRACT_MISMATCH'):
        verify_material_return(check, finding, [receipt], completion=wrong_completion)


def test_preflight_does_not_reuse_old_terminal_review_after_missing_channel_rejection(tmp_path, monkeypatch):
    import hashlib
    import json
    from types import SimpleNamespace
    from scripts import predeploy_contract_gauntlet as gate
    rig = _rig(tmp_path / 'rig', monkeypatch)
    finding = deepcopy(rig.finding)
    finding['exact_release_adjudication'] = {'terminal_review': {
        'prompt': 'OLD_TERMINAL_REVIEW_IS_EVIDENCE_ONLY', 'request': {}}}
    audit = {'findings': [finding], 'unresolved_findings_disclosed': []}
    evidence = tmp_path / 'frozen'
    source = evidence / 'clip' / 'candidate.review-flags.json'
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(audit, ensure_ascii=False))
    case = {'id': 'clip:0', 'cue_index': finding['cue_index'], 'old_reason': None,
        'file': str(source), 'row_index': 0, 'audit': audit, 'finding': finding,
        'audit_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    monkeypatch.setattr(gate, 'original_cases', lambda *args: iter([case]))
    monkeypatch.setattr(gate, 'saved_inputs', lambda *args: (
        rig.srt, rig.context, rig.offset_ms, finding, []))
    state = tmp_path / 'replay'
    state.mkdir()
    rows = gate.prepare_cases(SimpleNamespace(state=state, evidence=evidence,
        contexts=None, exclude_group=[]))
    assert len(rows) == 1 and rows[0]['status'] == 'EXPECTED_MATERIAL_RETURN'
    assert gate.replay_exit_code(rows) == 0
    directory = Path(rows[0]['directory'])
    assert (directory / 'material-return-check.json').is_file()
    assert not (directory / 'request.json').exists()
    assert not (directory / 'prompt.txt').exists()
    assert rig.transport.calls == []


def test_padding_only_wider_window_is_not_a_wide_candidate_channel(tmp_path, monkeypatch):
    """Forty milliseconds around one cue does not replace the owner context contract."""
    rig = _rig(tmp_path, monkeypatch)
    start, end = target_material.physical_target(rig.check)["physical_target_ms"]
    projection = {
        "physical_target": target_material.physical_target(rig.check),
        "observations": [{
            "observation_id": "padding-only",
            "input_information": {
                "source_window_ms": [start - 40, end + 40],
                "cue_indexes": [2],
                "observation_class": "CANDIDATE_BLIND_AUDIO",
            },
            "receipt_kind": "blind_range",
        }],
        "effective_distinct_observations": 1,
    }
    result = target_material.target_material_exhaustion(rig.check, projection)
    assert result["completed_channels"]["WIDE_CANDIDATE_BLIND_AUDIO"] == []
    assert result["missing_channels"] == ["WIDE_CANDIDATE_BLIND_AUDIO"]
    assert result["exhausted"] is False


def test_argon_channel_is_added_only_after_three_opportunities_and_old_trigger(tmp_path, monkeypatch):
    from tests.test_targeted_relisten_public_contract import three_observations

    rig = build_offline_rig(tmp_path, monkeypatch)
    witness, check, _, _ = three_observations(rig)
    check = deepcopy(check)
    check["argon_upgrade_available"] = True
    check.pop("request_sha256", None)
    check["request_sha256"] = target_material.digest(check)
    projection = target_material.evidence_projection(check, witness=witness)
    result = target_material.target_material_exhaustion(check, projection)
    assert projection["effective_distinct_observations"] >= target_material.DEFAULT_OPPORTUNITIES
    assert result["required_channels"] == ["WIDE_CANDIDATE_BLIND_AUDIO", "ARGON_UPGRADE"]
    assert result["completed_channels"]["WIDE_CANDIDATE_BLIND_AUDIO"]
    assert result["missing_channels"] == ["ARGON_UPGRADE"]

    terminal = deepcopy(check)
    terminal["target_argon_state"] = {"state": "TERMINAL", "reason": "synthetic terminal hearing"}
    terminal.pop("request_sha256", None)
    terminal["request_sha256"] = target_material.digest(terminal)
    terminal_projection = target_material.evidence_projection(terminal, witness=witness)
    exhausted = target_material.target_material_exhaustion(terminal, terminal_projection)
    assert exhausted["completed_channels"]["ARGON_UPGRADE"] == [
        "owner:TERMINAL"
    ]
    assert exhausted["missing_channels"] == []
    assert exhausted["exhausted"] is True


def _historical_argon_request(rig):
    request = deepcopy(rig.check)
    request["argon_upgrade_available"] = True
    request.pop("request_sha256", None)
    request["request_sha256"] = target_material.digest(request)
    return request


def _wide_three_projection(check):
    target = target_material.physical_target(check)
    start, end = target["physical_target_ms"]
    info = {"source_window_ms": [start - 100, end + 100],
            "cue_indexes": [1, 2, 3, 4],
            "observation_class": "CANDIDATE_BLIND_AUDIO"}
    return {
        "physical_target": target,
        "observations": [
            {"observation_id": "wide-1", "input_information": info,
             "receipt_kind": "blind_range"},
            {"observation_id": "wide-2", "input_information": {**info,
             "source_window_ms": [start - 200, end + 200]}, "receipt_kind": "blind_range"},
            {"observation_id": "wide-3", "input_information": {**info,
             "source_window_ms": [start - 300, end + 300]}, "receipt_kind": "blind_range"},
        ],
        "effective_distinct_observations": 3,
    }


def _historical_argon_finding(request, *, request_field, state=None, status="UNCERTAIN"):
    owner = {
        request_field: request,
        "argon_escalation": {
            "status": status,
            "dispatches_this_advance": 0,
            "new_usable_observations_this_advance": 0,
        },
    }
    if state is not None:
        owner["argon_state"] = state
    return {"exact_release_adjudication": {"semantic_audio_recovery": owner}}


@pytest.mark.parametrize("request_field", ["consumer_check_request", "request"])
def test_historical_argon_terminal_owner_projects_without_fabricating_observation(
    tmp_path, monkeypatch, request_field
):
    """Frozen sister289/9e28 shapes carry a real terminal owner through G6."""
    from src.autoslice.exact_final_terminal_funnel import terminal_check

    rig = _rig(tmp_path / request_field, monkeypatch)
    request = _historical_argon_request(rig)
    state = {"state": "TERMINAL", "status": "UNCERTAIN", "reason": "overlap"}
    historical = _historical_argon_finding(
        request, request_field=request_field, state=state, status="UNCERTAIN"
    )
    projected = terminal_check(
        rig.srt,
        deepcopy(rig.finding),
        clip_context=rig.context,
        offset_ms=rig.offset_ms,
        historical_findings=[historical],
    )
    owner = projected["target_argon_state"]
    assert owner["state"] == "TERMINAL"
    assert owner["owner_audit"]["status"] == "UNCERTAIN"
    assert owner["source_request"] == request
    assert owner["provenance"]["source_field"] == request_field
    assert owner["provenance"]["request_sha256"] == request["request_sha256"]
    assert not projected["prior_acoustic_observations"]
    material = target_material.target_material_exhaustion(
        projected, _wide_three_projection(projected)
    )
    assert material["completed_channels"]["ARGON_UPGRADE"] == ["owner:TERMINAL"]
    assert material["missing_channels"] == []


@pytest.mark.parametrize(
    ("status", "expected_state"),
    [("UNCERTAIN", "TERMINAL"), ("NO_SPEECH", "TERMINAL"),
     ("FAILED_PRE_DISPATCH", "STRUCTURALLY_UNAVAILABLE")],
)
def test_historical_argon_status_only_owner_uses_shared_terminal_contract(
    tmp_path, monkeypatch, status, expected_state
):
    """The sister-9e28 shape has a direct request and only owner status."""
    from src.autoslice.exact_final_terminal_funnel import terminal_check

    rig = _rig(tmp_path / status, monkeypatch)
    request = _historical_argon_request(rig)
    historical = {
        "exact_release_adjudication": {
            "stopped_adjudication": {
                "request": request,
                "argon_escalation": {
                    "status": status,
                    "dispatches_this_advance": 0,
                    "new_usable_observations_this_advance": 0,
                },
            },
        },
    }
    projected = terminal_check(
        rig.srt,
        deepcopy(rig.finding),
        clip_context=rig.context,
        offset_ms=rig.offset_ms,
        historical_findings=[historical],
    )
    owner = projected["target_argon_state"]
    assert owner["state"] == expected_state
    assert owner["owner_audit"]["status"] == status
    assert owner["source_request"] == request
    assert owner["provenance"] == {
        "source_field": "request",
        "request_sha256": request["request_sha256"],
        "owner_audit_sha256": target_material.digest(owner["owner_audit"]),
    }
    assert not projected["prior_acoustic_observations"]
    material = target_material.target_material_exhaustion(
        projected, _wide_three_projection(projected)
    )
    assert material["completed_channels"]["ARGON_UPGRADE"] == [
        "owner:" + expected_state
    ]
    assert material["missing_channels"] == []


def test_pending_not_triggered_owner_stays_missing_argon_channel(tmp_path, monkeypatch):
    """Frozen sister-b1 cue25 has no Argon dispatch and remains blocked."""
    from src.autoslice.exact_final_terminal_funnel import terminal_check

    rig = _rig(tmp_path / "pending", monkeypatch)
    request = _historical_argon_request(rig)
    historical = _historical_argon_finding(
        request,
        request_field="consumer_check_request",
        state={"state": "NOT_TRIGGERED"},
        status="ADMISSION_PENDING",
    )
    projected = terminal_check(
        rig.srt,
        deepcopy(rig.finding),
        clip_context=rig.context,
        offset_ms=rig.offset_ms,
        historical_findings=[historical],
    )
    material = target_material.target_material_exhaustion(
        projected, _wide_three_projection(projected)
    )
    assert "target_argon_state" not in projected
    assert material["missing_channels"] == ["ARGON_UPGRADE"]
    assert material["exhausted"] is False


def test_bare_terminal_state_is_not_an_argon_attempt(tmp_path, monkeypatch):
    from src.autoslice.exact_final_terminal_funnel import terminal_check
    rig = _rig(tmp_path / 'bare-state', monkeypatch)
    request = _historical_argon_request(rig)
    historical = {'exact_release_adjudication': {'semantic_audio_recovery': {
        'consumer_check_request': request, 'argon_state': {'state': 'TERMINAL'}}}}
    projected = terminal_check(rig.srt, deepcopy(rig.finding), clip_context=rig.context,
        offset_ms=rig.offset_ms, historical_findings=[historical])
    assert 'target_argon_state' not in projected
    assert target_material.target_material_exhaustion(projected,
        _wide_three_projection(projected))['missing_channels'] == ['ARGON_UPGRADE']


def test_explicit_unknown_argon_state_without_owner_audit_stays_missing(
    tmp_path, monkeypatch
):
    from src.autoslice.exact_final_terminal_funnel import terminal_check

    rig = _rig(tmp_path / "explicit-unknown", monkeypatch)
    request = _historical_argon_request(rig)
    historical = {
        "exact_release_adjudication": {
            "semantic_audio_recovery": {
                "consumer_check_request": request,
                "argon_state": {"state": "TERMINAL", "status": "UNKNOWN"},
            },
        },
    }
    projected = terminal_check(
        rig.srt,
        deepcopy(rig.finding),
        clip_context=rig.context,
        offset_ms=rig.offset_ms,
        historical_findings=[historical],
    )
    material = target_material.target_material_exhaustion(
        projected, _wide_three_projection(projected)
    )
    assert "target_argon_state" not in projected
    assert material["missing_channels"] == ["ARGON_UPGRADE"]
    assert material["exhausted"] is False


@pytest.mark.parametrize(
    "case", ["other_g6", "source_drift", "changed_request_hash", "unknown", "failed"]
)
def test_historical_argon_terminal_projection_has_g6_and_owner_negative_controls(
    tmp_path, monkeypatch, case
):
    from src.autoslice.exact_final_terminal_funnel import terminal_check

    rig = _rig(tmp_path / case, monkeypatch)
    request = _historical_argon_request(rig)
    status = "UNCERTAIN"
    if case == "other_g6":
        request["current_cue"] = "另一道问题"
        request.pop("request_sha256", None)
        request["request_sha256"] = target_material.digest(request)
    elif case == "source_drift":
        request["clip_context_binding"] = deepcopy(request["clip_context_binding"])
        request["clip_context_binding"]["source_media_sha256s"] = ["f" * 64]
        request.pop("request_sha256", None)
        request["request_sha256"] = target_material.digest(request)
    elif case == "changed_request_hash":
        request["request_sha256"] = "f" * 64
    elif case == "unknown":
        status = "UNKNOWN"
    elif case == "failed":
        status = "FAILED"
    historical = _historical_argon_finding(
        request, request_field="consumer_check_request", status=status
    )
    projected = terminal_check(
        rig.srt,
        deepcopy(rig.finding),
        clip_context=rig.context,
        offset_ms=rig.offset_ms,
        historical_findings=[historical],
    )
    material = target_material.target_material_exhaustion(
        projected, _wide_three_projection(projected)
    )
    assert "target_argon_state" not in projected
    if case in {"unknown", "failed"}:
        assert material["missing_channels"] == ["ARGON_UPGRADE"]
        assert material["exhausted"] is False
    else:
        # A foreign/changed request cannot even make Argon available for this
        # target; it must never smuggle an owner terminal into the check.
        assert material["missing_channels"] == []
        assert material["exhausted"] is True


@pytest.mark.parametrize(
    "case_id, current, proposed, why, narrow",
    [
        ("xiamen-9e28f907-cue5-cough-narrow", "咳咳", "咳嗽", "只有咳嗽窄窗拼音，未覆盖完整问答。", True),
        ("xiamen-9e28f907-cue43-zero", "哈？咳", "哈？咳咳", "目标只保留零条宽窗观察。", False),
        ("xiamen-0d19eb72-cue5-zero", "咳咳", "咳嗽", "原始目标没有任何听音观察。", False),
        (
            "stomach-0d19eb72-cue57-zero-language-diagnostic",
            "谢谢小鸡，总能不懂得让人打call",
            "谢谢小鸡，总能不懂得让人打call啊",
            "Audio language discovery left this source window unresolved.",
            False,
        ),
    ],
)
def test_foreign_language_or_production_shape_does_not_prove_target_exhaustion(
    tmp_path, monkeypatch, case_id, current, proposed, why, narrow
):
    rig = _rig(tmp_path, monkeypatch, current=current, proposed=proposed, why=why)
    assert case_id and rig.finding["why"] == why
    if narrow:
        witness = acquire_narrow(rig)
        projection = target_material.evidence_projection(rig.check, witness=witness)
    else:
        projection = target_material.evidence_projection(rig.check)
    result = target_material.target_material_exhaustion(rig.check, projection)
    assert result["missing_channels"] == ["WIDE_CANDIDATE_BLIND_AUDIO"]
    assert result["exhausted"] is False


def test_real_owner_scheduler_dispatches_one_wide_range_and_cold_loop_stop_roundtrips(
    tmp_path, monkeypatch
):
    from src.autoslice.exact_final_terminal_funnel import terminal_check

    rig = _rig(tmp_path, monkeypatch)
    check = terminal_check(
        rig.srt,
        deepcopy(rig.finding),
        clip_context=rig.context,
        offset_ms=rig.offset_ms,
    )
    finding = deepcopy(rig.finding)
    adapters = {"entity_verifier": rig.verifier}

    completed = target_material.complete_target_materials(
        check=check, finding=finding, adapters=adapters
    )
    material = target_material.target_material_exhaustion(completed)
    assert material["exhausted"] is True
    assert material["completed_channels"]["WIDE_CANDIDATE_BLIND_AUDIO"]
    assert len(rig.transport.calls) == 1
    assert len(finding["target_material_attempts"]) == 1
    assert finding["target_material_attempts"][0]["channel"] == "WIDE_CANDIDATE_BLIND_AUDIO"
    assert finding['target_material_attempts'][0]['execution_status'] == 'RETURNED'

    receipt = review_pass_receipt(
        pass_index=1,
        srt_text=rig.srt,
        audit={"findings": [finding]},
        repaired_text=rig.srt,
        repairs=[],
    )
    proof = target_material.compile_loop_stop_no_gain(
        check=completed, finding=finding, pass_receipts=[receipt]
    )
    assert proof["material_exhaustion"]["scope"] == "TARGET_REQUIRED_CHANNELS"
    assert proof["material_exhaustion"]["target_material"]["exhausted"] is True
    assert target_material.validate_no_gain(proof, completed)["proof_sha256"] == proof["proof_sha256"]

    calls_before = len(rig.transport.calls)
    repeated = target_material.complete_target_materials(
        check=completed, finding=finding, adapters=adapters
    )
    assert repeated == completed
    assert len(rig.transport.calls) == calls_before


@pytest.mark.parametrize("status", ["UNKNOWN", "FAILED"])
def test_unknown_or_failed_wide_owner_result_cannot_exhaust_target(
    tmp_path, monkeypatch, status
):
    rig = _rig(tmp_path, monkeypatch)
    finding = deepcopy(rig.finding)
    calls = []

    def unusable(**kwargs):
        calls.append(kwargs["range_witness_request"])
        return [{"status": status, "reason_code": "SYNTHETIC_WIDE_" + status}]

    unusable.available_providers = ("native",)
    monkeypatch.setattr(rig.verifier, "observe_range_context", unusable)
    with pytest.raises(
        ProviderFailure, match="TARGET_MATERIAL_CHANNEL_OWNER_RECOVERY_REQUIRED:WIDE_CANDIDATE_BLIND_AUDIO"
    ) as exc:
        target_material.complete_target_materials(
            check=rig.check,
            finding=finding,
            adapters={"entity_verifier": rig.verifier},
        )
    assert exc.value.material_recovery['kind']==('IN_FLIGHT_OR_UNKNOWN' if status=='UNKNOWN' else 'TRANSPORT_OR_SCHEMA_FAILURE')
    assert len(calls) == 1
    assert target_material.target_material_exhaustion(rig.check)["exhausted"] is False


def test_missing_target_channel_keeps_terminal_closed_without_provider_or_judge(
    tmp_path, monkeypatch
):
    from src.autoslice.exact_final_terminal_funnel import terminalize_review

    rig = _rig(tmp_path, monkeypatch)
    finding = deepcopy(rig.finding)
    receipt = review_pass_receipt(
        pass_index=1,
        srt_text=rig.srt,
        audit={"findings": [finding]},
        repaired_text=rig.srt,
        repairs=[],
    )
    judge_calls = []
    result = terminalize_review(
        rig.srt,
        {"findings": [finding], "unresolved_findings_disclosed": [], "reason_codes": ["PENDING"]},
        pass_receipts=[receipt],
        adapters={
            "clip_context": rig.context,
            "source_media_timeline_offset_ms": rig.offset_ms,
            "entity_verifier": None,
            "provider_calls_allowed": False,
            "judge_llm_call": lambda prompt: judge_calls.append(prompt),
        },
    )
    assert not judge_calls
    assert result["findings"]
    assert result["findings"][0]["terminal_funnel_error"]["reason_code"] == (
        "TARGET_MATERIAL_CHANNELS_REQUIRED:WIDE_CANDIDATE_BLIND_AUDIO"
    )


def test_new_terminal_check_marks_target_material_contract(tmp_path, monkeypatch):
    from src.autoslice.exact_final_terminal_funnel import terminal_check

    rig = _rig(tmp_path, monkeypatch)
    check = terminal_check(
        rig.srt,
        deepcopy(rig.finding),
        clip_context=rig.context,
        offset_ms=rig.offset_ms,
    )
    assert check["target_material_contract"] == target_material.TARGET_MATERIAL_CONTRACT
    assert check["request_sha256"] == target_material.digest(
        {key: value for key, value in check.items() if key != "request_sha256"}
    )


def test_legacy_loop_stop_proof_remains_readable_without_target_contract(tmp_path, monkeypatch):
    from tests.test_targeted_relisten_public_contract import three_observations

    rig = build_offline_rig(tmp_path, monkeypatch)
    _, check, finding, _ = three_observations(rig)
    assert "target_material_contract" not in check
    receipt = review_pass_receipt(
        pass_index=1,
        srt_text=rig.srt,
        audit={"findings": [finding]},
        repaired_text=rig.srt,
        repairs=[],
    )
    proof = target_material.compile_loop_stop_no_gain(
        check=check, finding=finding, pass_receipts=[receipt]
    )
    assert proof["material_exhaustion"]["scope"] == "THIS_RUN_NO_NEW_MATERIAL"
    assert "target_material" not in proof["material_exhaustion"]
    assert target_material.validate_no_gain(proof, check)["proof_sha256"] == proof["proof_sha256"]
