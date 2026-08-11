from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import bind_source_fact_scorecard_rescore as binder
from scripts.bind_source_fact_scorecard_rescore import (
    SourceFactRescoreBindError,
    bind_rescored_spec,
)
from scripts.manual_source_fact_scorecard_rescore import (
    CPA_CALLER_TIMEOUT_SECONDS,
    CPA_COMMAND_TEMPLATE,
    CPA_MODEL,
    CPA_PROVIDER,
    CPA_TRANSPORT,
    EXECUTION_AUTHORITY_ENV,
    EXECUTION_AUTHORITY_VALUE,
    run_manual_rescore,
)
from src.autoslice.source_fact_rescore_provenance import (
    PROVENANCE_FIELD,
    SourceFactRescoreProvenanceError,
    canonical_sha256,
    validate_rebound_spec_provenance,
)


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_ID = "auto_223750_578_734"
ORIGINAL_HOOK = (
    "小李可怜巴巴求莉亚即使是狼也放过自己，刚结伴就突然连声道歉，"
    "场面瞬间从求生撒娇变成事故现场。"
)
CORRECTED_HOOK = (
    "莉娅求小李‘就算你是狼也放过我’，小李让她放心并提议一起走，"
    "莉娅答应后小李突然连声道歉。"
)


def _file_sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _stale_scorecard() -> dict[str, object]:
    return {
        "dimensions": {
            "audience_salience": 2,
            "comedic_payoff": 3,
            "lidousha_centrality": 3,
            "persona_reversal": 4,
            "relationship_interaction": 4,
            "self_contained": 3,
            "stance_intensity": 2,
        },
        "effective_score": 69.5,
        "fatigue_penalty": 0.0,
        "raw_score": 72.5,
        "reason_codes": [],
        "requested_tier": 1,
        "schema_version": "lidousha-selection-scorecard.v1",
        "status": "VALID",
        "tier": 1,
        "tier_basis": "relationship_chain",
        "tier_evidence_cues": [250, 255, 256, 258, 259, 262, 263, 264, 265],
        "tier_reason": (
            "片内有小李向莉亚求饶、对方答应、两人结伴以及随后连续道歉的"
            "关系互动和强烈反转，画面可补足具体游戏动作。"
        ),
        "uncertainty_penalty": 3.0,
        "weights": {
            "audience_salience": 15,
            "comedic_payoff": 10,
            "lidousha_centrality": 25,
            "persona_reversal": 10,
            "relationship_interaction": 15,
            "self_contained": 5,
            "stance_intensity": 20,
        },
    }


def _provider_payload() -> str:
    return json.dumps(
        {
            "status": "SUPPORTED",
            "selection_scorecard": {
                "tier": 1,
                "tier_basis": "relationship_chain",
                "tier_reason": (
                    "莉娅向小李求放过、两人结伴、小李突然道歉，形成完整关系链和反转。"
                ),
                "tier_evidence_cues": [1, 4, 15, 23, 33, 52],
                "dimensions": {
                    "lidousha_centrality": 4,
                    "stance_intensity": 3,
                    "audience_salience": 4,
                    "relationship_interaction": 4,
                    "persona_reversal": 4,
                    "comedic_payoff": 4,
                    "self_contained": 4,
                },
                "uncertainty_penalty": 0,
                "fatigue_penalty": 0,
            },
        },
        ensure_ascii=False,
    )


def _fixture(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    stale_card_path = tmp_path / "stale-scorecard.json"
    _write_json(stale_card_path, _stale_scorecard())
    provider_path = tmp_path / "provider.json"
    _write_json(
        provider_path,
        {
            "schema_version": "manual-source-fact-rescore-provider.v1",
            "provider": CPA_PROVIDER,
            "model": CPA_MODEL,
            "llm_config": {
                "transport": CPA_TRANSPORT,
                "command_template": CPA_COMMAND_TEMPLATE,
                "timeout_seconds": CPA_CALLER_TIMEOUT_SECONDS,
            },
        },
    )
    authority_path = (
        ROOT
        / "assets/lidousha/authorities/"
        "auto_223750_578_734.source-fact-rescore-authority.v1.json"
    )
    reviewed_path = (
        ROOT
        / "assets/lidousha/reviewed_subtitle_baselines/"
        "auto_223750_578_734.reviewed.srt"
    )
    rescore_path = tmp_path / "rescore-receipt.json"
    run_manual_rescore(
        candidate_id=CANDIDATE_ID,
        reviewed_final_srt=reviewed_path,
        reviewed_final_srt_sha256=_file_sha(reviewed_path),
        corrected_hook=CORRECTED_HOOK,
        source_fact_receipt=None,
        source_fact_receipt_sha256=None,
        stale_scorecard=stale_card_path,
        stale_scorecard_sha256=_file_sha(stale_card_path),
        source_start_ms=577_780,
        source_end_ms=735_090,
        provider_config=provider_path,
        provider_config_sha256=_file_sha(provider_path),
        correction_authority=authority_path,
        correction_authority_sha256=_file_sha(authority_path),
        stale_hook=ORIGINAL_HOOK,
        output=rescore_path,
        execute_provider_call=True,
        environ={EXECUTION_AUTHORITY_ENV: EXECUTION_AUTHORITY_VALUE},
        llm_call=lambda _prompt: _provider_payload(),
    )
    source_spec = {
        "candidate_id": CANDIDATE_ID,
        "selection_hook": CORRECTED_HOOK,
        "selection_scorecard": _stale_scorecard(),
        "subtitle_redelivery_baseline": {
            "schema_version": "subtitle-redelivery-baseline.v2",
            "path": str(reviewed_path),
            "sha256": "6d79fdab105d4c7b5edffec66fe526aa01839de342a7c96b8784f4d0100a8062",
            "source_recording_basename": "22966160_20260807-22-37-50.mp4",
            "source_sha256": "66e9ec0707fe8d7f38cf8a552e863dafbba34fb7eadaece496adb30d04b1ad1e",
            "absolute_source_start_ms": 577_780,
            "absolute_source_end_ms": 735_090,
        },
    }
    source_spec_path = tmp_path / "source-spec.json"
    _write_json(source_spec_path, source_spec)
    return {
        "source_spec": source_spec_path,
        "source_spec_sha256": _file_sha(source_spec_path),
        "rescore_receipt": rescore_path,
        "rescore_receipt_sha256": _file_sha(rescore_path),
        "correction_authority": authority_path,
        "correction_authority_sha256": _file_sha(authority_path),
        "output_spec": tmp_path / "rebound-spec.json",
        "output_rebind_receipt": tmp_path / "rebind-receipt.json",
        "repo_root": ROOT,
    }


def test_create_only_binder_emits_self_contained_valid_provenance(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    original_bytes = fixture["source_spec"].read_bytes()

    rebound, receipt = bind_rescored_spec(**fixture)

    assert fixture["source_spec"].read_bytes() == original_bytes
    assert fixture["output_spec"].is_file()
    assert fixture["output_rebind_receipt"].is_file()
    assert receipt["status"] == "REBOUND_CREATE_ONLY"
    assert receipt["old_selection_scorecard_sha256"] == (
        "sha256:b16649c47ce299821b0ab0b83ed7e168846beed282afaafc817631f340081e0a"
    )
    provenance = validate_rebound_spec_provenance(rebound, repo_root=ROOT)
    assert provenance is not None
    assert provenance["provenance_sha256"] == canonical_sha256(
        {key: value for key, value in provenance.items() if key != "provenance_sha256"}
    )
    assert provenance["rescore_receipt"]["input_bindings"]["input_mode"] == (
        "candidate_correction_authority"
    )
    assert rebound["selection_scorecard"] == provenance["rescore_receipt"][
        "selection_scorecard"
    ]


def test_candidate_new_card_without_receipt_provenance_fails_closed(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    rebound, _receipt = bind_rescored_spec(**fixture)
    unbound = dict(rebound)
    unbound.pop(PROVENANCE_FIELD)

    with pytest.raises(
        SourceFactRescoreProvenanceError,
        match="changed without a rescore receipt",
    ):
        validate_rebound_spec_provenance(unbound, repo_root=ROOT)

    legacy = json.loads(fixture["source_spec"].read_text(encoding="utf-8"))
    assert validate_rebound_spec_provenance(legacy, repo_root=ROOT) is None


def test_binder_never_overwrites_an_existing_output(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["output_spec"].write_text("operator bytes\n", encoding="utf-8")

    with pytest.raises(SourceFactRescoreBindError, match="create-only"):
        bind_rescored_spec(**fixture)

    assert fixture["output_spec"].read_text(encoding="utf-8") == "operator bytes\n"
    assert not fixture["output_rebind_receipt"].exists()


def test_rebind_success_receipt_is_published_only_after_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    real_create = binder._create_file

    def fail_receipt(path: Path, payload: bytes, *, label: str) -> None:
        if label == "rebind receipt":
            raise SourceFactRescoreBindError("injected receipt commit failure")
        real_create(path, payload, label=label)

    monkeypatch.setattr(binder, "_create_file", fail_receipt)
    with pytest.raises(SourceFactRescoreBindError, match="injected"):
        bind_rescored_spec(**fixture)

    assert fixture["output_spec"].is_file()
    assert not fixture["output_rebind_receipt"].exists()


def test_binder_rejects_source_spec_or_embedded_receipt_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["source_spec"].write_text("{}\n", encoding="utf-8")
    with pytest.raises(SourceFactRescoreBindError, match="bytes drifted"):
        bind_rescored_spec(**fixture)

    fixture = _fixture(tmp_path / "receipt")
    receipt = json.loads(fixture["rescore_receipt"].read_text(encoding="utf-8"))
    receipt["selection_scorecard"]["tier_reason"] = "tampered"
    _write_json(fixture["rescore_receipt"], receipt)
    fixture["rescore_receipt_sha256"] = _file_sha(fixture["rescore_receipt"])
    with pytest.raises(SourceFactRescoreBindError, match="output hash"):
        bind_rescored_spec(**fixture)
