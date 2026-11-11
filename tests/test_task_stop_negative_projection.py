from copy import deepcopy

import pytest

from src.autoslice import finding_context, targeted_relisten as owner
from tests.test_task_stop_no_gain import prepared, rig  # noqa: F401


def negative_case(rig, kind, include_target_text):  # noqa: F811
    check, old_materials, version, old_proof = prepared(rig)
    original = deepcopy(check)
    if kind == "range":
        check.setdefault("context_only_range_acoustic_observations", []).append({})
    else:
        check.setdefault("prior_acoustic_observations", []).append(
            {
                "schema_version": "subtitle-span-acoustic-witness.v1",
                "status": "OBSERVED",
                "witness_protocol": "blind_pinyin",
                "request_sha256": "b" * 64,
                "audio_clip_sha256": "c" * 64,
                "response_sha256": "d" * 64,
                "source_media_sha256": "a" * 64,
            }
        )
    check.pop("request_sha256", None)
    check["request_sha256"] = owner.digest(check)
    projection = owner.evidence_projection(check, include_target_text=include_target_text)
    materials = finding_context.materials_for_check(
        check,
        contracts=old_materials["contracts"],
        model_identity=old_materials["model_identity"],
        observation_ids=[row["observation_id"] for row in projection["observations"]],
    )
    return check, materials, version, projection, old_proof, original


@pytest.mark.parametrize("kind", ["narrow", "range"])
@pytest.mark.parametrize("include_text", [False, True])
def test_exhausted_task_reopens_original_rejected_inputs(rig, kind, include_text):  # noqa: F811
    check, materials, version, projection, old_proof, original = negative_case(
        rig, kind, include_text
    )
    reason = (
        "SPAN_BLIND_PHONEME_RECEIPT_INVALID"
        if kind == "narrow"
        else "RANGE_ACOUSTIC_MANIFEST_MISSING"
    )
    assert reason in [row["reason_code"] for row in projection["rejected_observations"]]
    assert (
        projection["effective_distinct_observations"]
        == old_proof["projection"]["effective_distinct_observations"]
    )
    proof = owner.compile_task_stop_no_gain(
        check=check, projection=projection, finding_materials=materials, text_version_sha256=version
    )
    assert proof["projection"] == projection
    assert (
        owner.validate_no_gain(
            proof, check, finding_materials=materials, text_version_sha256=version
        )
        == proof
    )
    assert original != check and old_proof["mutation_authorized"] is False


@pytest.mark.parametrize("field", ["reason", "observation", "count", "accepted"])
def test_self_consistent_forged_projection_is_rejected(rig, field):  # noqa: F811
    check, materials, version, projection, _, _ = negative_case(rig, "range", True)
    forged = deepcopy(projection)
    if field == "reason":
        forged["rejected_observations"][-1]["reason_code"] = "FORGED"
    elif field == "observation":
        forged["rejected_observations"][-1]["observation_id"] = "0" * 64
    elif field == "count":
        forged["effective_distinct_observations"] += 1
    else:
        forged["observations"].pop()
    with pytest.raises(ValueError, match="TASK_STOP_OBSERVATION_DRIFT"):
        owner.compile_task_stop_no_gain(
            check=check, projection=forged, finding_materials=materials, text_version_sha256=version
        )


def test_no_projection_uses_same_full_owner_view_and_revalidates_signed_negatives(rig):  # noqa: F811
    check, materials, version, projection, _, _ = negative_case(rig, "narrow", True)
    implicit = owner.compile_task_stop_no_gain(
        check=check, finding_materials=materials, text_version_sha256=version
    )
    explicit = owner.compile_task_stop_no_gain(
        check=check, projection=projection, finding_materials=materials, text_version_sha256=version
    )
    assert implicit == explicit
    forged = deepcopy(implicit)
    forged["projection"]["rejected_observations"].clear()
    forged["proof_sha256"] = owner.digest({k: v for k, v in forged.items() if k != "proof_sha256"})
    with pytest.raises(ValueError, match="TASK_STOP_OBSERVATION_DRIFT"):
        owner.validate_no_gain(
            forged, check, finding_materials=materials, text_version_sha256=version
        )


@pytest.mark.parametrize("field", ["source", "window", "text"])
def test_proof_does_not_authorize_changed_current_input(rig, field):  # noqa: F811
    check, materials, version, projection, _, _ = negative_case(rig, "range", True)
    proof = owner.compile_task_stop_no_gain(
        check=check, projection=projection, finding_materials=materials, text_version_sha256=version
    )
    altered = deepcopy(check)
    if field == "source":
        altered["clip_context_binding"]["source_media_sha256s"] = ["0" * 64]
    elif field == "window":
        altered["matched_start_ms"] += 1
    else:
        altered["whole_clip_current_srt"] += "\n"
    altered["request_sha256"] = owner.digest(
        {k: v for k, v in altered.items() if k != "request_sha256"}
    )
    with pytest.raises(ValueError, match="RELISTEN_NO_GAIN_BINDING_DRIFT"):
        owner.validate_no_gain(
            proof, altered, finding_materials=materials, text_version_sha256=version
        )


def test_missing_original_audio_response_still_rejects(rig):  # noqa: F811
    from pathlib import Path

    check, materials, version, projection, _, _ = negative_case(rig, "narrow", True)
    proof = owner.compile_task_stop_no_gain(
        check=check, projection=projection, finding_materials=materials, text_version_sha256=version
    )
    witness = next(
        row for row in projection["observations"] if row["receipt_kind"] == "blind_pinyin"
    )
    Path(witness["raw_observation"]["relisten_artifacts"]["response_path"]).unlink()
    with pytest.raises(ValueError, match="TASK_STOP_OBSERVATION_DRIFT"):
        owner.validate_no_gain(
            proof, check, finding_materials=materials, text_version_sha256=version
        )
