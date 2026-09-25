from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import producer_native_witness as routing


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"real-media-bytes")
    return path


def test_automatic_route_selects_only_first_configured_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routing,
        "_provider_configuration",
        lambda provider, _environ: {
            "configured": provider in {"mai", "moss"},
            "reason_code": "CONFIGURED",
        },
    )
    receipt = routing.build_audio_witness_routing(
        {}, candidate_id="candidate", source_media=_source(tmp_path), environ={}
    )

    assert receipt["stages"]["local_entity"]["selected_provider"] == "mai"
    assert receipt["stages"]["foreign_script"]["selected_provider"] == "mai"
    assert receipt["policy"]["native_provider_fallback"] == "NONE"
    assert receipt["policy"]["text_decision_authority"] == "CPA"
    assert receipt["policy"]["uniform_host_is_identity_evidence"] is False
    assert receipt["source_media_sha256"] == hashlib.sha256(
        b"real-media-bytes"
    ).hexdigest()


def test_policy_order_and_explicit_override_are_disclosed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routing,
        "_provider_configuration",
        lambda provider, _environ: {
            "configured": provider == "moss",
            "reason_code": "CONFIGURED" if provider == "moss" else "MISSING",
        },
    )
    receipt = routing.build_audio_witness_routing(
        {
            "audio_witness_routing": {"provider_order": ["moss", "mai"]},
            "local_audio_witness_provider": "mai",
        },
        candidate_id="candidate",
        source_media=_source(tmp_path),
        environ={},
    )

    local = receipt["stages"]["local_entity"]
    foreign = receipt["stages"]["foreign_script"]
    assert local["selection_mode"] == "EXPLICIT_NATIVE_OVERRIDE"
    assert local["selected_provider"] == "mai"
    assert local["selected_provider_configured"] is False
    assert foreign["selection_mode"] == "AUTOMATIC_CONFIGURED_PROVIDER"
    assert foreign["selected_provider"] == "moss"


def test_no_configuration_keeps_existing_legacy_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routing,
        "_provider_configuration",
        lambda provider, _environ: {
            "configured": False,
            "reason_code": f"{provider.upper()}_MISSING",
        },
    )
    receipt = routing.build_audio_witness_routing(
        {}, candidate_id="candidate", source_media=_source(tmp_path), environ={}
    )

    assert receipt["stages"]["local_entity"]["selected_provider"] is None
    assert receipt["stages"]["foreign_script"]["selected_provider"] is None
    assert routing.native_foreign_script_kwargs({}, routing=receipt) == {}


def test_consumption_receipt_binds_native_evidence_and_cpa_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routing,
        "_provider_configuration",
        lambda provider, _environ: {
            "configured": provider == "mai",
            "reason_code": "CONFIGURED" if provider == "mai" else "MISSING",
        },
    )
    planned = routing.build_audio_witness_routing(
        {}, candidate_id="candidate", source_media=_source(tmp_path), environ={}
    )
    audit = {
        "entity_repairs": [
            {
                "request_sha256": "a" * 64,
                "cue_indexes": [7],
                "acoustic_witness": {
                    "request_sha256": "b" * 64,
                    "status": "OBSERVED",
                    "provider": "mai",
                    "model": "MAI-Transcribe-2",
                    "audio_start_ms": 1200,
                    "audio_end_ms": 3400,
                    "source_media_sha256": "1" * 64,
                    "audio_clip_sha256": "2" * 64,
                    "response_sha256": "3" * 64,
                    "candidate_exposure": "none",
                    "authority": "EVIDENCE_ONLY",
                    "mutation_authorized": False,
                    "served_from_cache": False,
                },
                "text_first_judge": {
                    "status": "JUDGED",
                    "decision_authority": "CPA_JUDGE",
                    "needs_audio": True,
                    "prompt_sha256": "4" * 64,
                    "completion_sha256": "5" * 64,
                    "check_request_sha256": "a" * 64,
                },
                "judge": {
                    "status": "JUDGED",
                    "decision_authority": "CPA_JUDGE",
                    "choice": "CURRENT",
                },
            }
        ]
    }

    consumed = routing.finalize_audio_witness_routing(planned, audit)
    row = consumed["observed_consumption"][0]
    assert consumed["status"] == "CONSUMED"
    assert row["provider"] == "mai"
    assert row["audio_start_ms"] == 1200
    assert row["audio_end_ms"] == 3400
    assert row["cpa_decision_authority"] == "CPA_JUDGE"
    assert row["cpa_needs_audio"] is True
    assert consumed["stages"]["local_entity"]["observed_route_match"] is True
    unsigned = dict(consumed)
    signature = unsigned.pop("receipt_sha256")
    assert signature == routing._digest(unsigned)


def test_routing_config_rejects_duplicates_and_unknown_keys() -> None:
    with pytest.raises(ValueError):
        routing.validate_audio_witness_routing_config(
            {"provider_order": ["mai", "mai"]}
        )
    with pytest.raises(ValueError):
        routing.validate_audio_witness_routing_config({"fallback": "moss"})


def test_routing_receipt_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "routing.json"
    routing.write_audio_witness_routing(
        path,
        {
            "schema_version": routing.ROUTING_SCHEMA_VERSION,
            "status": "PLANNED",
        },
    )
    loaded = json.loads(path.read_text())
    assert loaded["schema_version"] == routing.ROUTING_SCHEMA_VERSION
    assert len(loaded["receipt_sha256"]) == 64


def test_provider_programming_error_is_not_collapsed_to_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import mai_transcription

    def broken_reader(_environ):
        raise RuntimeError("implementation bug")

    monkeypatch.setattr(mai_transcription, "_read_api_key", broken_reader)
    with pytest.raises(RuntimeError, match="implementation bug"):
        routing._provider_configuration("mai", {})
