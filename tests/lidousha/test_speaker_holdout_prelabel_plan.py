from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from scripts import build_speaker_holdout_prelabel_plan as plan
from tests.lidousha.test_freeze_speaker_holdout_sources import _fixture, _run


def _inputs(tmp_path: Path) -> dict[str, object]:
    source_paths = _fixture(tmp_path)
    first = _run(source_paths)
    second = _run(source_paths)
    replay_one = tmp_path / "source-freeze-one.json"
    replay_two = tmp_path / "source-freeze-two.json"
    replay_one.write_text(json.dumps(first), encoding="utf-8")
    replay_two.write_text(json.dumps(second), encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    scratch.chmod(0o700)
    asr_script = tmp_path / "free_asr_client.py"
    asr_script.write_text("# fixture\n", encoding="utf-8")
    manifests = [replay_one, replay_two]
    acceptance = {
        "acceptance_id": "fixture.v0",
        "room_id": first["room_id"],
        "date_roles": first["date_roles"],
        "source_inventory_count_by_date": first["source_inventory_count_by_date"],
        "deterministic_payload_sha256": first["deterministic_payload_sha256"],
        "replay_file_sha256": {plan._sha256(path) for path in manifests},
    }
    return {
        "manifests": manifests,
        "first": first,
        "second": second,
        "scratch": scratch,
        "asr_script": asr_script,
        "acceptance": acceptance,
        "run_id": "fixture-run",
    }


def _build(values: dict[str, object]) -> tuple[dict[str, object], Path, dict[Path, str]]:
    return plan.build_plan(
        source_freeze_paths=values["manifests"],
        scratch_root=values["scratch"],
        run_id=values["run_id"],
        asr_script=values["asr_script"],
        acceptance=values["acceptance"],
        allowed_scratch_root=values["scratch"],
    )


def _refresh_replay_acceptance(values: dict[str, object]) -> None:
    values["acceptance"] = {
        **values["acceptance"],
        "replay_file_sha256": {plan._sha256(path) for path in values["manifests"]},
    }


def test_plan_binds_two_replays_without_running_or_opening_truth(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    payload, output_path, _ = _build(values)

    assert payload["status"] == "EXTRACTION_PLAN_FROZEN_EXECUTION_NOT_AUTHORIZED"
    assert payload["schema_version"] == "speaker-holdout-extraction-plan.v1"
    assert payload["session_count"] == 2
    assert payload["segment_count"] == 2
    assert output_path == values["scratch"] / "fixture-run.plan.json"
    assert payload["policy"]["asr_provider"] == "bcut"
    assert payload["policy"]["asr_model_id"] == "7"
    assert payload["policy"]["threshold_state"] is None
    assert payload["toolchain"]["hash_bound_run_one_wrapper"] is None
    assert payload["toolchain"]["planner"]["sha256"].startswith("sha256:")
    assert payload["toolchain"]["free_asr_client"]["sha256"].startswith("sha256:")
    assert payload["execution_contract"]["plan_only"] is True
    assert payload["execution_contract"]["real_provider_execution_authorized"] is False
    assert payload["execution_contract"]["external_audio_upload_authorized"] is False
    assert payload["execution_contract"]["production_runner_allowed"] is False
    assert payload["execution_contract"]["human_truth_allowed"] is False
    assert payload["execution_contract"]["speaker_prediction_allowed"] is False
    assert payload["authority"]["asr_frozen"] is False
    assert payload["authority"]["human_truth_opened"] is False
    assert payload["authority"]["production_authority"] is False
    assert all(
        segment["state"] == "PLANNED_NOT_EXECUTED"
        for session in payload["sessions"]
        for segment in session["segments"]
    )
    assert list(values["scratch"].iterdir()) == []


def test_plan_is_deterministic_for_unchanged_inputs(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    first, _, _ = _build(values)
    second, _, _ = _build(values)
    assert first == second
    assert first["deterministic_payload_sha256"] == plan._canonical_sha256(
        {key: value for key, value in first.items() if key != "deterministic_payload_sha256"}
    )


def test_plan_rejects_unaccepted_but_self_consistent_replay(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    second = copy.deepcopy(values["second"])
    second["observation"]["observed_at"] = "different-but-nondeterministic"
    values["manifests"][1].write_text(json.dumps(second), encoding="utf-8")
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="file hashes are not accepted v3"):
        _build(values)


def test_plan_rejects_nonidentical_source_freeze_payloads(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    second = copy.deepcopy(values["second"])
    second["segments"][0]["source_media"]["mtime_ns"] += 1
    deterministic = plan._deterministic_source_payload(second)
    second["deterministic_payload_sha256"] = plan._canonical_sha256(deterministic)
    values["manifests"][1].write_text(json.dumps(second), encoding="utf-8")
    values["acceptance"] = {
        **values["acceptance"],
        "deterministic_payload_sha256": second["deterministic_payload_sha256"],
    }
    _refresh_replay_acceptance(values)
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="not the accepted v3 payload"):
        _build(values)


def test_plan_rejects_tampered_source_freeze_hash(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    second = json.loads(values["manifests"][1].read_text(encoding="utf-8"))
    second["segments"][0]["source_media"]["mtime_ns"] += 1
    values["manifests"][1].write_text(json.dumps(second), encoding="utf-8")
    _refresh_replay_acceptance(values)
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="payload hash drifted"):
        _build(values)


@pytest.mark.parametrize("forbidden_key", ["human_truth_opened", "production_authority"])
def test_plan_rejects_manifest_that_claims_truth_or_production(
    tmp_path: Path,
    forbidden_key: str,
) -> None:
    values = _inputs(tmp_path)
    changed = copy.deepcopy(values["first"])
    changed["authority"][forbidden_key] = True
    deterministic = plan._deterministic_source_payload(changed)
    changed["deterministic_payload_sha256"] = plan._canonical_sha256(deterministic)
    values["manifests"][0].write_text(json.dumps(changed), encoding="utf-8")
    values["acceptance"] = {
        **values["acceptance"],
        "deterministic_payload_sha256": changed["deterministic_payload_sha256"],
    }
    _refresh_replay_acceptance(values)
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="authority is not source-only"):
        _build(values)


def test_plan_rejects_injected_prelabel_material_field(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    changed = copy.deepcopy(values["first"])
    changed["human_labels"] = []
    deterministic = plan._deterministic_source_payload(changed)
    changed["deterministic_payload_sha256"] = plan._canonical_sha256(deterministic)
    values["manifests"][0].write_text(json.dumps(changed), encoding="utf-8")
    values["acceptance"] = {
        **values["acceptance"],
        "deterministic_payload_sha256": changed["deterministic_payload_sha256"],
    }
    _refresh_replay_acceptance(values)
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="field set drifted"):
        _build(values)


def test_plan_rejects_wrong_room_or_inventory_count(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    values["acceptance"] = {**values["acceptance"], "room_id": "wrong"}
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="room ID drifted"):
        _build(values)

    values = _inputs(tmp_path / "count")
    values["acceptance"] = {
        **values["acceptance"],
        "source_inventory_count_by_date": {"2026-08-10": 10, "2026-08-11": 5},
    }
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="accepted inventory counts drifted"):
        _build(values)


def test_plan_rejects_source_bytes_drift_before_any_execution(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    source = Path(values["first"]["segments"][0]["source_media"]["path"])
    source.write_bytes(source.read_bytes() + b"drift")
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="stat drifted"):
        _build(values)


def test_plan_rejects_nonexact_or_overpermissive_scratch(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="exact allowed"):
        plan.build_plan(
            source_freeze_paths=values["manifests"],
            scratch_root=other,
            run_id=values["run_id"],
            asr_script=values["asr_script"],
            acceptance=values["acceptance"],
            allowed_scratch_root=values["scratch"],
        )

    values["scratch"].chmod(0o755)
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="mode 0700"):
        _build(values)


@pytest.mark.parametrize("run_id", ["../escape", "/absolute", "UPPER", "", "."])
def test_plan_rejects_unsafe_run_ids(tmp_path: Path, run_id: str) -> None:
    values = _inputs(tmp_path)
    values["run_id"] = run_id
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="safe lowercase"):
        _build(values)


def test_plan_rejects_existing_run_root(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    (values["scratch"] / values["run_id"]).mkdir()
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="already exists"):
        _build(values)


def test_plan_records_raw_pcm_dedup_and_relative_outputs(tmp_path: Path) -> None:
    payload, _, _ = _build(_inputs(tmp_path))
    assert payload["policy"]["canonical_pcm_cross_session_dedup_required"] is True
    assert payload["policy"]["canonical_pcm"]["container"] == "raw_s16le"
    for session in payload["sessions"]:
        for segment in session["segments"]:
            assert segment["outputs"] == {
                "attempt_root_template": f"segments/{segment['segment_id']}/{{attempt_id}}",
                "canonical_pcm_s16le": "canonical-pcm.s16le",
                "asr_input_mp3": "asr-input-16k-mono-64k.mp3",
                "asr_normalized_json": "asr.normalized.json",
                "cue_table_json": "cue-table.json",
                "asr_srt": "asr.srt",
                "extraction_receipt": "extraction-receipt.json",
            }


def test_dirfd_create_only_plan_cannot_be_overwritten(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    payload, output_path, bindings = _build(values)
    written = plan._write_create_only(
        scratch_root=values["scratch"],
        output_name=output_path.name,
        payload=payload,
        bound_inputs=bindings,
    )
    assert written == output_path
    assert stat_mode(output_path) == 0o600
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="create-only"):
        plan._write_create_only(
            scratch_root=values["scratch"],
            output_name=output_path.name,
            payload=payload,
            bound_inputs=bindings,
        )
    assert json.loads(output_path.read_text(encoding="utf-8")) == payload


def test_writer_revalidates_inputs_before_namespace_commit(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    payload, output_path, bindings = _build(values)
    values["asr_script"].write_text("# drift\n", encoding="utf-8")
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="bound input drifted"):
        plan._write_create_only(
            scratch_root=values["scratch"],
            output_name=output_path.name,
            payload=payload,
            bound_inputs=bindings,
        )
    assert not output_path.exists()
    assert list(values["scratch"].iterdir()) == []


def test_exactly_two_source_freeze_replays_are_required(tmp_path: Path) -> None:
    values = _inputs(tmp_path)
    values["manifests"] = values["manifests"][:1]
    with pytest.raises(plan.HoldoutPrelabelPlanError, match="exactly two"):
        _build(values)


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
