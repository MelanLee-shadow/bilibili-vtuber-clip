from src.autoslice.boundary_resolver import AnchorCandidate
from src.autoslice.source_context_planner import (
    JingtingJobProvenance,
    plan_source_context_jingting_jobs,
)


def provenance(**overrides):
    data = {
        "recording_id": "room123456-20260625",
        "source_sha256": "sha256:source",
        "source_uri": "file:///recordings/source.mp4",
        "planner_version": "test-planner",
    }
    data.update(overrides)
    return JingtingJobProvenance(**data)


def test_context_window_clips_at_source_beginning_with_default_pre_post():
    jobs = plan_source_context_jingting_jobs(
        [AnchorCandidate(candidate_id="near-start", anchor_start_ms=20_000, anchor_end_ms=40_000)],
        source_duration_ms=600_000,
        provenance=provenance(),
    )

    job = jobs[0]
    manifest = job.to_manifest()

    assert job.context_start_ms == 0
    assert job.context_end_ms == 190_000
    assert job.source_offset_ms == 0
    assert manifest["schema_version"] == "source-context-jingting-job.v1"
    assert manifest["source_offset_ms"] == 0
    assert manifest["timeline"]["anchor_start_ms"] == 20_000
    assert manifest["timeline"]["context_anchor_start_ms"] == 20_000
    assert manifest["provenance"]["recording_id"] == "room123456-20260625"
    assert manifest["provenance"]["source_sha256"] == "sha256:source"


def test_context_window_clips_at_source_end_with_default_pre_post():
    jobs = plan_source_context_jingting_jobs(
        [AnchorCandidate(candidate_id="near-end", anchor_start_ms=100_000, anchor_end_ms=190_000)],
        source_duration_ms=200_000,
        provenance=provenance(),
    )

    job = jobs[0]
    manifest = job.to_manifest()

    assert job.context_start_ms == 10_000
    assert job.context_end_ms == 200_000
    assert job.source_offset_ms == 10_000
    assert manifest["timeline"]["context_duration_ms"] == 190_000
    assert manifest["timeline"]["context_anchor_start_ms"] == 90_000
    assert manifest["timeline"]["context_anchor_end_ms"] == 180_000


def test_source_context_jingting_job_ids_are_stable_for_same_anchor_and_provenance():
    anchor = AnchorCandidate(candidate_id="stable", anchor_start_ms=123_456, anchor_end_ms=234_567)
    prov = provenance()

    first = plan_source_context_jingting_jobs([anchor], source_duration_ms=900_000, provenance=prov)[0]
    second = plan_source_context_jingting_jobs([anchor], source_duration_ms=900_000, provenance=prov)[0]
    reordered_provenance = provenance(
        planner_version="test-planner",
        source_uri="file:///recordings/source.mp4",
        source_sha256="sha256:source",
        recording_id="room123456-20260625",
    )
    third = plan_source_context_jingting_jobs([anchor], source_duration_ms=900_000, provenance=reordered_provenance)[0]

    assert first.job_id == second.job_id == third.job_id
    assert first.to_manifest() == second.to_manifest() == third.to_manifest()
    assert first.job_id.startswith("scj_")


def test_source_context_manifest_is_local_planning_only_for_agy_context():
    job = plan_source_context_jingting_jobs(
        [AnchorCandidate(candidate_id="local-only", anchor_start_ms=300_000, anchor_end_ms=320_000)],
        source_duration_ms=900_000,
        provenance=provenance(),
    )[0]

    manifest = job.to_manifest()

    assert manifest["job_kind"] == "SOURCE_CONTEXT_JINGTING"
    assert manifest["local_only"] is True
    assert manifest["provider"] == "agy"
    assert manifest["input"]["source_uri"] == "file:///recordings/source.mp4"
    assert manifest["input"]["source_offset_ms"] == job.source_offset_ms
    assert manifest["input"]["duration_ms"] == job.context_duration_ms
    assert manifest["input"]["write_outputs"] is False
