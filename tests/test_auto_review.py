import pytest

from src.autoslice.auto_review import (
    CandidateReview,
    DecisionAction,
    ReviewDecision,
    AutoReviewManifest,
    JingtingProvenance,
    REQUIRED_PUBLISH_ARTIFACT_KEYS,
    evaluate_required_evidence,
    is_publish_gate_satisfied,
    review_candidate,
    select_auto_uploadable,
)


COMPLETE_ARTIFACTS = {
    artifact_key: f"sha256:{artifact_key.removesuffix('_sha256')}"
    for artifact_key in REQUIRED_PUBLISH_ARTIFACT_KEYS
}


def auto_upload_manifest(**overrides):
    data = {
        "candidate_id": "release-ready",
        "decision": ReviewDecision(action=DecisionAction.AUTO_UPLOAD, score=91.0),
        "artifacts": COMPLETE_ARTIFACTS,
        "jingting_provenance": good_jingting_provenance(),
    }
    data.update(overrides)
    return AutoReviewManifest(**data)


def good_jingting_provenance(**overrides):
    data = {
        "manifest_present": True,
        "provider": "agy",
        "agy_rc": 0,
        "model": "Gemini 3.6 Flash (Low)",
        "provider_fallback_used": False,
    }
    data.update(overrides)
    return JingtingProvenance(**data)


def base_candidate(**overrides):
    data = {
        "candidate_id": "clip-ok",
        "jingting_done": True,
        "release_ready": True,
        "review_required_findings": (),
        "foreground_song_overlap_seconds": 0.0,
        "song_complete": True,
        "lyrics_alignment_ready": True,
        "start_boundary_score": 0.97,
        "end_boundary_score": 0.98,
        "standalone_score": 0.94,
        "payoff_score": 0.95,
        "open_loop_count": 0,
        "editorial_score": 88.0,
        "duplicate_similarity": 0.10,
        "subtitle_alignment_p95_ms": 180.0,
        "actual_cut_error_ms": 40.0,
        "recut_attempt": 0,
        "jingting_provenance": good_jingting_provenance(),
    }
    data.update(overrides)
    return CandidateReview(**data)


def test_jingting_done_does_not_override_review_required_block():
    decision = review_candidate(
        base_candidate(
            candidate_id="song-review-required",
            jingting_done=True,
            release_ready=False,
            review_required_findings=("japanese_song_or_lyrics_alignment_required",),
        )
    )

    assert decision.action == DecisionAction.BLOCK
    assert "JINGTING_REVIEW_REQUIRED" in decision.reason_codes


def test_verified_song_lrc_authority_bypasses_only_jingting_provider_gate():
    decision = review_candidate(
        base_candidate(
            candidate_id="lrc-authoritative-song",
            jingting_done=False,
            jingting_provenance=None,
            release_ready=False,
            review_required_findings=("AGY_QUOTA_EXHAUSTED",),
            verified_song_lrc_authority=True,
        )
    )

    assert "JINGTING_PENDING" not in decision.reason_codes
    assert "JINGTING_PROVENANCE_MISSING" not in decision.reason_codes
    assert "JINGTING_REVIEW_REQUIRED" not in decision.reason_codes


def test_incomplete_foreground_song_blocks_instead_of_fixed_window_clip():
    decision = review_candidate(
        base_candidate(
            candidate_id="partial-song",
            foreground_song_overlap_seconds=12.0,
            song_complete=False,
            lyrics_alignment_ready=False,
        )
    )

    assert decision.action == DecisionAction.BLOCK
    assert "SONG_PARTIAL" in decision.reason_codes
    assert "LYRICS_ALIGNMENT_REQUIRED" in decision.reason_codes


def test_boundary_incomplete_returns_auto_recut_when_attempts_remain():
    decision = review_candidate(
        base_candidate(
            candidate_id="talk-needs-tail",
            end_boundary_score=0.80,
            recut_attempt=0,
            max_recut_attempts=2,
        )
    )

    assert decision.action == DecisionAction.AUTO_RECUT
    assert "END_BOUNDARY_LOW" in decision.reason_codes


def test_boundary_incomplete_drops_after_recut_budget_exhausted():
    decision = review_candidate(
        base_candidate(
            candidate_id="talk-still-bad",
            end_boundary_score=0.80,
            recut_attempt=2,
            max_recut_attempts=2,
        )
    )

    assert decision.action == DecisionAction.DROP
    assert "RECUT_BUDGET_EXHAUSTED" in decision.reason_codes


def test_duplicate_and_low_value_clip_drops_with_explicit_bad_evidence():
    decision = review_candidate(
        base_candidate(
            candidate_id="duplicate-low-value",
            duplicate_similarity=0.96,
            editorial_score=70.0,
            payoff_score=0.80,
        )
    )

    assert decision.action == DecisionAction.DROP
    assert "DUPLICATE" in decision.reason_codes
    assert "EDITORIAL_SCORE_LOW" in decision.reason_codes
    assert "PAYOFF_MISSING" in decision.reason_codes


def test_pts_cut_error_auto_recuts_or_blocks_with_explicit_bad_evidence():
    auto_recut = review_candidate(base_candidate(candidate_id="pts-recut", actual_cut_error_ms=150.0, recut_attempt=0))
    exhausted = review_candidate(
        base_candidate(candidate_id="pts-block", actual_cut_error_ms=150.0, recut_attempt=2, max_recut_attempts=2)
    )

    assert auto_recut.action == DecisionAction.AUTO_RECUT
    assert "ACTUAL_CUT_ERROR_HIGH" in auto_recut.reason_codes
    assert exhausted.action == DecisionAction.BLOCK
    assert "ACTUAL_CUT_ERROR_HIGH" in exhausted.reason_codes
    assert "RECUT_BUDGET_EXHAUSTED" in exhausted.reason_codes


def test_missing_jingting_provenance_blocks_auto_upload():
    decision = review_candidate(base_candidate(candidate_id="missing-prov", jingting_provenance=None))

    assert decision.action == DecisionAction.BLOCK
    assert "JINGTING_PROVENANCE_MISSING" in decision.reason_codes


def test_non_agy_provider_blocks_auto_upload():
    decision = review_candidate(
        base_candidate(candidate_id="wrong-provider", jingting_provenance=good_jingting_provenance(provider="gemini"))
    )

    assert decision.action == DecisionAction.BLOCK
    assert "JINGTING_PROVIDER_NOT_AGY" in decision.reason_codes


def test_gemini_api_fallback_provenance_is_rejected():
    decision = review_candidate(
        base_candidate(
            candidate_id="gemini-api-fallback",
            jingting_provenance=good_jingting_provenance(
                provider="gemini_api",
                agy_rc=None,
                provider_fallback_used=True,
                model="gemini-3.6-flash",
            ),
        )
    )

    assert decision.action == DecisionAction.BLOCK
    assert "JINGTING_PROVIDER_NOT_AGY" in decision.reason_codes
    assert "JINGTING_AGY_FAILED" in decision.reason_codes
    assert "JINGTING_PROVIDER_FALLBACK_USED" in decision.reason_codes


def test_provider_fallback_used_or_unknown_blocks_auto_upload():
    used = review_candidate(
        base_candidate(
            candidate_id="fallback-used",
            jingting_provenance=good_jingting_provenance(provider_fallback_used=True),
        )
    )
    unknown = review_candidate(
        base_candidate(
            candidate_id="fallback-unknown",
            jingting_provenance=good_jingting_provenance(provider_fallback_used=None),
        )
    )

    assert used.action == DecisionAction.BLOCK
    assert "JINGTING_PROVIDER_FALLBACK_USED" in used.reason_codes
    assert unknown.action == DecisionAction.BLOCK
    assert "JINGTING_PROVIDER_FALLBACK_UNKNOWN" in unknown.reason_codes


def test_nonzero_agy_result_or_missing_model_blocks_auto_upload():
    failed = review_candidate(
        base_candidate(candidate_id="agy-failed", jingting_provenance=good_jingting_provenance(agy_rc=2))
    )
    no_model = review_candidate(
        base_candidate(candidate_id="no-model", jingting_provenance=good_jingting_provenance(model=""))
    )

    assert failed.action == DecisionAction.BLOCK
    assert "JINGTING_AGY_FAILED" in failed.reason_codes
    assert no_model.action == DecisionAction.BLOCK
    assert "JINGTING_MODEL_MISSING" in no_model.reason_codes


@pytest.mark.parametrize(
    ("missing_field", "expected_reason"),
    [
        ("release_ready", "RELEASE_READY_MISSING"),
        ("review_required_findings", "REVIEW_REQUIRED_FINDINGS_MISSING"),
        ("foreground_song_overlap_seconds", "FOREGROUND_SONG_OVERLAP_MISSING"),
        ("song_complete", "SONG_COMPLETENESS_MISSING"),
        ("lyrics_alignment_ready", "LYRICS_ALIGNMENT_MISSING"),
        ("start_boundary_score", "START_BOUNDARY_MISSING"),
        ("end_boundary_score", "END_BOUNDARY_MISSING"),
        ("standalone_score", "STANDALONE_MISSING"),
        ("open_loop_count", "OPEN_LOOP_EVIDENCE_MISSING"),
        ("payoff_score", "PAYOFF_MISSING_EVIDENCE"),
        ("editorial_score", "EDITORIAL_SCORE_MISSING"),
        ("duplicate_similarity", "DUPLICATE_SIMILARITY_MISSING"),
        ("subtitle_alignment_p95_ms", "SUBTITLE_ALIGNMENT_MISSING"),
        ("actual_cut_error_ms", "ACTUAL_CUT_ERROR_MISSING"),
    ],
)
def test_missing_required_evidence_never_auto_uploads(missing_field, expected_reason):
    decision = review_candidate(base_candidate(candidate_id=f"missing-{missing_field}", **{missing_field: None}))

    assert decision.action == DecisionAction.BLOCK
    assert expected_reason in decision.reason_codes


def test_minimal_candidate_without_required_content_or_pts_evidence_never_auto_uploads():
    decision = review_candidate(
        CandidateReview(
            candidate_id="no-content-pts-or-quality-evidence",
            jingting_done=True,
            jingting_provenance=good_jingting_provenance(),
        )
    )

    assert decision.action == DecisionAction.BLOCK
    assert "FOREGROUND_SONG_OVERLAP_MISSING" in decision.reason_codes
    assert "ACTUAL_CUT_ERROR_MISSING" in decision.reason_codes
    assert "DUPLICATE_SIMILARITY_MISSING" in decision.reason_codes
    assert "EDITORIAL_SCORE_MISSING" in decision.reason_codes


def test_required_evidence_checks_serialize_missing_gate_codes_for_replay_manifests():
    checks = [check.to_manifest_check() for check in evaluate_required_evidence(CandidateReview(candidate_id="missing"))]

    failed_by_code = {check["code"]: check for check in checks if not check["pass"]}
    assert failed_by_code["FOREGROUND_SONG_OVERLAP_RECORDED"]["reason_code"] == "FOREGROUND_SONG_OVERLAP_MISSING"
    assert failed_by_code["ACTUAL_CUT_ERROR_RECORDED"]["reason_code"] == "ACTUAL_CUT_ERROR_MISSING"
    assert failed_by_code["DUPLICATE_SIMILARITY_RECORDED"]["reason_code"] == "DUPLICATE_SIMILARITY_MISSING"
    assert failed_by_code["EDITORIAL_SCORE_RECORDED"]["reason_code"] == "EDITORIAL_SCORE_MISSING"


def test_global_selection_allows_fewer_than_maximum():
    candidates = [
        base_candidate(candidate_id="good-one", editorial_score=90.0),
        base_candidate(candidate_id="blocked-song", foreground_song_overlap_seconds=9.0, song_complete=False),
        base_candidate(candidate_id="weak", editorial_score=70.0),
    ]

    selected = select_auto_uploadable(candidates, max_count=10)

    assert [item.candidate_id for item in selected] == ["good-one"]


def test_publish_gate_rejects_jingting_done_without_auto_upload_manifest():
    assert not is_publish_gate_satisfied(
        jingting_done=True,
        manifest=None,
        expected_artifacts={"video_sha256": "sha256:video"},
    )


def test_publish_gate_rejects_non_upload_manifest_decision():
    manifest = AutoReviewManifest(
        candidate_id="needs-recut",
        decision=ReviewDecision(
            action=DecisionAction.AUTO_RECUT,
            reason_codes=("END_BOUNDARY_LOW",),
            score=89.0,
        ),
        artifacts={"video_sha256": "sha256:video"},
    )

    assert not is_publish_gate_satisfied(
        jingting_done=True,
        manifest=manifest,
        expected_artifacts={"video_sha256": "sha256:video"},
    )


def test_publish_gate_accepts_auto_upload_manifest_with_complete_matching_artifacts():
    manifest = auto_upload_manifest()

    assert is_publish_gate_satisfied(
        jingting_done=True,
        manifest=manifest,
        expected_artifacts=COMPLETE_ARTIFACTS,
    )


def test_publish_gate_rejects_auto_upload_manifest_without_expected_artifacts():
    manifest = auto_upload_manifest()

    assert not is_publish_gate_satisfied(
        jingting_done=True,
        manifest=manifest,
        expected_artifacts=None,
    )
    assert not is_publish_gate_satisfied(
        jingting_done=True,
        manifest=manifest,
        expected_artifacts={},
    )


def test_publish_gate_rejects_auto_upload_manifest_missing_any_expected_artifact():
    manifest = auto_upload_manifest()

    for missing_key in REQUIRED_PUBLISH_ARTIFACT_KEYS:
        expected_artifacts = {key: value for key, value in COMPLETE_ARTIFACTS.items() if key != missing_key}

        assert not is_publish_gate_satisfied(
            jingting_done=True,
            manifest=manifest,
            expected_artifacts=expected_artifacts,
        ), missing_key


def test_publish_gate_rejects_auto_upload_manifest_with_empty_artifacts():
    manifest = auto_upload_manifest(artifacts={})

    assert not is_publish_gate_satisfied(
        jingting_done=True,
        manifest=manifest,
        expected_artifacts=COMPLETE_ARTIFACTS,
    )


def test_publish_gate_rejects_auto_upload_manifest_missing_any_required_artifact():
    for missing_key in REQUIRED_PUBLISH_ARTIFACT_KEYS:
        manifest_artifacts = {key: value for key, value in COMPLETE_ARTIFACTS.items() if key != missing_key}
        manifest = auto_upload_manifest(artifacts=manifest_artifacts)

        assert not is_publish_gate_satisfied(
            jingting_done=True,
            manifest=manifest,
            expected_artifacts=COMPLETE_ARTIFACTS,
        ), missing_key


def test_publish_gate_rejects_auto_upload_manifest_with_any_stale_required_artifact():
    for stale_key in REQUIRED_PUBLISH_ARTIFACT_KEYS:
        manifest_artifacts = dict(COMPLETE_ARTIFACTS)
        manifest_artifacts[stale_key] = f"sha256:stale-{stale_key}"
        manifest = auto_upload_manifest(artifacts=manifest_artifacts)

        assert not is_publish_gate_satisfied(
            jingting_done=True,
            manifest=manifest,
            expected_artifacts=COMPLETE_ARTIFACTS,
        ), stale_key


def test_publish_gate_rejects_auto_upload_without_valid_jingting_provenance():
    no_manifest = auto_upload_manifest(
        candidate_id="no-prov",
        jingting_provenance=None,
    )
    fallback_unknown = auto_upload_manifest(
        candidate_id="unknown-fallback",
        jingting_provenance=good_jingting_provenance(provider_fallback_used=None),
    )

    assert not is_publish_gate_satisfied(
        jingting_done=True,
        manifest=no_manifest,
        expected_artifacts=COMPLETE_ARTIFACTS,
    )
    assert not is_publish_gate_satisfied(
        jingting_done=True,
        manifest=fallback_unknown,
        expected_artifacts=COMPLETE_ARTIFACTS,
    )


def test_auto_review_manifest_serializes_provenance_checks_for_failures():
    manifest = AutoReviewManifest(
        candidate_id="wrong-provider",
        decision=ReviewDecision(action=DecisionAction.AUTO_UPLOAD, score=91.0),
        artifacts={"video_sha256": "sha256:video"},
        jingting_provenance=good_jingting_provenance(provider="gemini", provider_fallback_used=True),
    )

    data = manifest.to_dict()

    assert data["metadata"]["jingting_provenance"]["provider"] == "gemini"
    assert data["metadata"]["jingting_provenance"]["model"] == "Gemini 3.6 Flash (Low)"
    failed_codes = [check["code"] for check in data["checks"] if not check["pass"]]
    assert "JINGTING_PROVIDER_AGY" in failed_codes
    assert "JINGTING_PROVIDER_FALLBACK_NOT_USED" in failed_codes
    assert "JINGTING_PROVIDER_NOT_AGY" in data["decision"]["reason_codes"]
    assert "JINGTING_PROVIDER_FALLBACK_USED" in data["decision"]["reason_codes"]


def test_publish_gate_rejects_auto_upload_manifest_with_stale_artifacts():
    manifest_artifacts = dict(COMPLETE_ARTIFACTS)
    manifest_artifacts["video_sha256"] = "sha256:old-video"
    manifest = auto_upload_manifest(
        candidate_id="stale-video",
        artifacts=manifest_artifacts,
    )

    assert not is_publish_gate_satisfied(
        jingting_done=True,
        manifest=manifest,
        expected_artifacts=COMPLETE_ARTIFACTS,
    )
