import hashlib
import json
from pathlib import Path

import pytest

import src.autoslice.host_vocal_proof as host_vocal


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _fixture(tmp_path: Path, *, passes: tuple[bool, ...] = (True, False, True, True, False, True, True)):
    assert len(passes) == 7
    candidate_id = "song-voice-proof"
    source = tmp_path / "candidate.mp4"
    source.write_bytes(b"source-media-bound-to-proof")
    alignment = tmp_path / "lyrics-alignment-report.json"
    lyric_lines = [{"lrc_time_ms": index * 12_000, "text": f"lyric-{index}"} for index in range(8)]
    alignment_rows = [
        {
            "lrc_time_ms": row["lrc_time_ms"],
            "lrc_text": row["text"],
            "matched_cue_id": f"cue-{index}",
            "cue_start_ms": 10_000 + index * 12_000,
            "cue_end_ms": 15_000 + index * 12_000,
            **host_vocal.READY_SINGING_ASSERTIONS,
        }
        for index, row in enumerate(lyric_lines)
    ]
    alignment_payload = {
        "schema_version": "lyrics-alignment-report.v1",
        "candidate_id": candidate_id,
        "first_lyric_start_ms": 10_000,
        "last_lyric_end_ms": alignment_rows[-1]["cue_end_ms"],
        "lyric_lines": lyric_lines,
        "alignment": alignment_rows,
        "post_song_talk_start_ms": alignment_rows[-1]["cue_end_ms"] + 1_000,
        "audio_alignment_artifacts": {
            "source_sha256": _sha(source),
            "source_duration_ms": alignment_rows[-1]["cue_end_ms"] + 10_000,
        },
    }
    _write_json(alignment, alignment_payload)

    model_dir = tmp_path / "campplus-model"
    model_dir.mkdir()
    (model_dir / "configuration.json").write_bytes(b"pinned-campplus-configuration")
    (model_dir / "model.bin").write_bytes(b"pinned-campplus-weights")

    reference_dir = tmp_path / "references"
    reference_dir.mkdir()
    reference_specs = []
    for index in range(3):
        reference = reference_dir / f"enroll_ref_{index + 1}.wav"
        reference.write_bytes(f"channel-reference-{index + 1}".encode())
        reference_specs.append(
            {"id": f"ref-{index + 1}", "filename": reference.name, "sha256": _sha(reference)}
        )

    profile = tmp_path / "voiceprint_profile.v1.json"
    profile_payload = {
        "schema_version": host_vocal.PROFILE_SCHEMA_VERSION,
        "profile_id": "channel-test-v1",
        "subject": host_vocal.CHANNEL_PROFILE.display_name,
        "model": {
            "model_id": "damo/speech_campplus_sv_zh-cn_16k-common",
            "tree_sha256": host_vocal._sha256_directory(model_dir),
        },
        "references": reference_specs,
        "policy": host_vocal._canonical_policy(),
    }
    _write_json(profile, profile_payload)

    proof_path = tmp_path / "song.host-vocal-proof.json"
    checkpoint_dir = tmp_path / "song.host-vocal-proof.checkpoints"
    checkpoint_dir.mkdir()
    session_sample = checkpoint_dir / "session-host-anchor.wav"
    session_sample.write_bytes(b"session-host-anchor-audio")
    session_start, session_end = host_vocal._session_host_anchor_position(alignment_payload)
    session_host_anchor = {
        "start_ms": session_start,
        "end_ms": session_end,
        "window_ms": session_end - session_start,
        "sample_path": str(session_sample.resolve()),
        "sample_sha256": _sha(session_sample),
        "reference_scores": [
            {"reference_id": reference["id"], "score": 0.80} for reference in reference_specs
        ],
        "enroll_median_score": 0.80,
        "passed": True,
    }
    checkpoints = []
    first_ms = alignment_payload["first_lyric_start_ms"]
    last_ms = alignment_payload["last_lyric_end_ms"]
    selected_lyrics = host_vocal._selected_lyric_rows(alignment_payload)
    for index, (fraction, bucket, passed, lyric_row) in enumerate(
        zip(host_vocal.CHECKPOINT_FRACTIONS, host_vocal.CHECKPOINT_BUCKETS, passes, selected_lyrics, strict=True)
    ):
        sample = checkpoint_dir / f"checkpoint-{index + 1:02d}.wav"
        sample.write_bytes(f"checkpoint-audio-{index + 1}".encode())
        score = 0.40 if passed else 0.10
        center_ms, start_ms, end_ms = host_vocal._checkpoint_position_for_row(lyric_row)
        checkpoints.append(
            {
                "index": index,
                "fraction": fraction,
                "bucket": bucket,
                "alignment_index": lyric_row["alignment_index"],
                "matched_cue_id": lyric_row["matched_cue_id"],
                "lrc_time_ms": lyric_row["lrc_time_ms"],
                "lrc_text": lyric_row["lrc_text"],
                "lyric_cue_start_ms": lyric_row["cue_start_ms"],
                "lyric_cue_end_ms": lyric_row["cue_end_ms"],
                **{
                    key: lyric_row[key]
                    for key in host_vocal.READY_SINGING_ASSERTIONS
                },
                "center_ms": center_ms,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "window_ms": end_ms - start_ms,
                "sample_path": str(sample.resolve()),
                "sample_sha256": _sha(sample),
                "scores": [
                    {"reference_id": reference["id"], "score": score} for reference in reference_specs
                ],
                "median_score": score,
                "session_anchor_score": 0.40 if passed else 0.10,
                "passed": passed,
            }
        )
    status, decision, distribution = host_vocal._decision_from_checkpoints(checkpoints)
    proof = {
        "schema_version": host_vocal.PROOF_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "status": status,
        "decision": decision,
        "source_media": {"path": str(source.resolve()), "sha256": _sha(source)},
        "lyrics_alignment_report": {
            "path": str(alignment.resolve()),
            "sha256": _sha(alignment),
            "first_lyric_start_ms": first_ms,
            "last_lyric_end_ms": last_ms,
        },
        "reference_profile": {
            "path": str(profile.resolve()),
            "sha256": _sha(profile),
            "schema_version": host_vocal.PROFILE_SCHEMA_VERSION,
            "profile_id": profile_payload["profile_id"],
        },
        "speaker_model": {
            "path": str(model_dir.resolve()),
            "model_id": profile_payload["model"]["model_id"],
            "tree_sha256": profile_payload["model"]["tree_sha256"],
        },
        "references": [
            {**reference, "path": str((reference_dir / reference["filename"]).resolve())}
            for reference in reference_specs
        ],
        "session_host_anchor": session_host_anchor,
        "policy": host_vocal._canonical_policy(),
        "checkpoints": checkpoints,
        "distribution": distribution,
    }
    _write_json(proof_path, proof)
    claim = {
        "status": status,
        "proof_path": str(proof_path.resolve()),
        "proof_sha256": _sha(proof_path),
        "decision": decision,
    }
    return {
        "candidate_id": candidate_id,
        "source": source,
        "alignment": alignment,
        "profile": profile,
        "proof_path": proof_path,
        "proof": proof,
        "claim": claim,
    }


def _verify(bundle, **overrides):
    return host_vocal.verify_host_vocal_proof_claim(
        overrides.get("claim", bundle["claim"]),
        overrides.get("candidate_id", bundle["candidate_id"]),
        overrides.get("source", bundle["source"]),
        overrides.get("alignment", bundle["alignment"]),
        overrides.get("profile", bundle["profile"]),
    )


def _rewrite_proof_and_rebind_claim(bundle) -> None:
    _write_json(bundle["proof_path"], bundle["proof"])
    bundle["claim"]["proof_sha256"] = _sha(bundle["proof_path"])


def test_valid_hash_bound_ready_claim(tmp_path):
    bundle = _fixture(tmp_path)

    assert bundle["claim"]["status"] == host_vocal.READY_STATUS
    assert bundle["claim"]["decision"] == host_vocal.READY_DECISION
    assert _verify(bundle) is None


def test_verified_session_anchor_bridges_spoken_enrollment_to_singing_checkpoint(tmp_path):
    bundle = _fixture(tmp_path)
    checkpoint = bundle["proof"]["checkpoints"][0]
    for score in checkpoint["scores"]:
        score["score"] = 0.20
    checkpoint["median_score"] = 0.20
    checkpoint["session_anchor_score"] = 0.40
    checkpoint["passed"] = True
    _rewrite_proof_and_rebind_claim(bundle)

    assert _verify(bundle) is None


def test_three_second_post_song_speech_anchor_is_accepted_without_backfill(tmp_path):
    bundle = _fixture(tmp_path)
    alignment = json.loads(bundle["alignment"].read_text(encoding="utf-8"))
    alignment["audio_alignment_artifacts"]["source_duration_ms"] = (
        alignment["post_song_talk_start_ms"] + 3_450
    )

    assert host_vocal._session_host_anchor_position(alignment) == (
        alignment["post_song_talk_start_ms"],
        alignment["post_song_talk_start_ms"] + 3_450,
    )


def test_session_host_anchor_search_advances_through_post_song_speech(tmp_path):
    bundle = _fixture(tmp_path)
    alignment = json.loads(bundle["alignment"].read_text(encoding="utf-8"))
    first_start = alignment["post_song_talk_start_ms"]
    alignment["audio_alignment_artifacts"]["source_duration_ms"] = first_start + 30_000

    positions = host_vocal._session_host_anchor_positions(alignment)

    assert positions[:3] == (
        (first_start, first_start + 8_000),
        (first_start + 4_000, first_start + 12_000),
        (first_start + 8_000, first_start + 16_000),
    )


def test_shifted_session_host_anchor_requires_ordered_search_evidence(tmp_path):
    bundle = _fixture(tmp_path)
    positions = host_vocal._session_host_anchor_positions(
        json.loads(bundle["alignment"].read_text(encoding="utf-8"))
    )
    first_candidate = json.loads(
        json.dumps(bundle["proof"]["session_host_anchor"])
    )
    first_candidate["enroll_median_score"] = 0.40
    first_candidate["passed"] = False
    selected_candidate = bundle["proof"]["session_host_anchor"]
    selected_candidate["start_ms"], selected_candidate["end_ms"] = positions[1]
    selected_candidate["window_ms"] = positions[1][1] - positions[1][0]
    _rewrite_proof_and_rebind_claim(bundle)

    assert (
        _verify(bundle)
        == "shifted session host anchor lacks its search record"
    )

    bundle["proof"]["session_host_anchor_search"] = {
        "strategy": "first_verified_enrollment_window_after_song",
        "candidates": [first_candidate, selected_candidate],
    }
    _rewrite_proof_and_rebind_claim(bundle)

    assert _verify(bundle) is None


def _stretch_source(bundle, *, trailing_ms: int) -> dict:
    """Give the bundle a long post-song tail and rebind every hash to it."""

    alignment = json.loads(bundle["alignment"].read_text(encoding="utf-8"))
    alignment["audio_alignment_artifacts"]["source_duration_ms"] = (
        alignment["post_song_talk_start_ms"] + trailing_ms
    )
    _write_json(bundle["alignment"], alignment)
    bundle["proof"]["lyrics_alignment_report"]["sha256"] = _sha(bundle["alignment"])
    return alignment


def _failed_search_record(positions, *, selected_anchor, medians) -> dict:
    """Build a candidate list whose scores all miss MIN_SESSION_ENROLL_MEDIAN."""

    candidates = []
    for index, (start_ms, end_ms) in enumerate(positions):
        if (start_ms, end_ms) == (selected_anchor["start_ms"], selected_anchor["end_ms"]):
            candidates.append(selected_anchor)
            continue
        candidates.append(
            {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "window_ms": end_ms - start_ms,
                "enroll_median_score": medians[index],
                "passed": medians[index] >= host_vocal.MIN_SESSION_ENROLL_MEDIAN,
            }
        )
    return {
        "strategy": "first_verified_enrollment_window_after_song",
        "candidates": candidates,
    }


def test_session_host_anchor_search_reaches_speech_beyond_a_fixed_post_song_horizon(tmp_path):
    """A medley setlist can put the real post-show talk minutes after the song.

    ``post_song_talk_start_ms`` then lands inside the *next* song, so a search
    capped a fixed distance after it only ever samples singing.
    """

    bundle = _fixture(tmp_path)
    alignment = _stretch_source(bundle, trailing_ms=200_000)
    first_start = alignment["post_song_talk_start_ms"]

    positions = host_vocal._session_host_anchor_positions(alignment)

    assert positions[:2] == (
        (first_start, first_start + 8_000),
        (first_start + 4_000, first_start + 12_000),
    )
    assert (first_start + 120_000, first_start + 128_000) in positions
    assert (first_start + 196_000, first_start + 200_000) in positions
    assert positions[-1][1] == first_start + 200_000


def test_anchor_search_truncated_near_the_song_no_longer_proves_exhaustion(tmp_path):
    """A failed anchor search must cover the whole tail before it blocks.

    The old fixed horizon made a 13-window search look complete.  It is not:
    the windows it skipped are exactly where post-show speech lives.
    """

    bundle = _fixture(tmp_path)
    alignment = _stretch_source(bundle, trailing_ms=200_000)
    positions = host_vocal._session_host_anchor_positions(alignment)
    anchor = bundle["proof"]["session_host_anchor"]
    anchor["reference_scores"] = [
        {**row, "score": 0.30} for row in anchor["reference_scores"]
    ]
    anchor["enroll_median_score"] = 0.30
    anchor["passed"] = False
    truncated = positions[:13]
    bundle["proof"]["session_host_anchor_search"] = _failed_search_record(
        truncated,
        selected_anchor=anchor,
        medians=[0.30] + [0.10] * (len(truncated) - 1),
    )
    _rewrite_proof_and_rebind_claim(bundle)

    assert (
        _verify(bundle)
        == "session host anchor search did not retain the best failed window"
    )


def test_anchor_may_never_be_drawn_from_the_song_it_is_proving(tmp_path):
    """The bridge would otherwise verify the performance with the performance.

    A window inside the lyric span that clears 0.50 would hand every remaining
    checkpoint a free pass at 0.22 - which is the playback/background-vocal
    hole this gate exists to close.
    """

    bundle = _fixture(tmp_path)
    alignment = _stretch_source(bundle, trailing_ms=200_000)
    lyric_start = alignment["first_lyric_start_ms"]
    lyric_end = alignment["last_lyric_end_ms"]

    positions = host_vocal._session_host_anchor_positions(alignment)
    assert positions
    assert all(
        start_ms >= lyric_end or end_ms <= lyric_start for start_ms, end_ms in positions
    )

    anchor = bundle["proof"]["session_host_anchor"]
    anchor["start_ms"] = lyric_end - host_vocal.SESSION_HOST_ANCHOR_WINDOW_MS
    anchor["end_ms"] = lyric_end
    anchor["window_ms"] = host_vocal.SESSION_HOST_ANCHOR_WINDOW_MS
    _rewrite_proof_and_rebind_claim(bundle)

    assert _verify(bundle) == "session host anchor timing mismatch"


def test_exhausted_search_without_a_qualifying_anchor_still_refuses(tmp_path):
    """Reverse gate: a wider search must not become a softer one.

    Every window is scanned and every window misses 0.50, so the bridge stays
    closed even though each checkpoint's session-anchor score clears 0.22.
    """

    bundle = _fixture(tmp_path, passes=(False,) * 7)
    alignment = _stretch_source(bundle, trailing_ms=200_000)
    positions = host_vocal._session_host_anchor_positions(alignment)
    anchor = bundle["proof"]["session_host_anchor"]
    anchor["reference_scores"] = [
        {**row, "score": 0.49} for row in anchor["reference_scores"]
    ]
    anchor["enroll_median_score"] = 0.49
    anchor["passed"] = False
    for checkpoint in bundle["proof"]["checkpoints"]:
        checkpoint["session_anchor_score"] = 0.40
        checkpoint["passed"] = False
    bundle["proof"]["session_host_anchor_search"] = _failed_search_record(
        positions,
        selected_anchor=anchor,
        medians=[0.49] + [0.10] * (len(positions) - 1),
    )
    status, decision, distribution = host_vocal._decision_from_checkpoints(
        [{"bucket": row["bucket"], "passed": False} for row in bundle["proof"]["checkpoints"]]
    )
    bundle["proof"]["status"] = status
    bundle["proof"]["decision"] = decision
    bundle["proof"]["distribution"] = distribution
    bundle["claim"]["status"] = status
    bundle["claim"]["decision"] = decision
    _rewrite_proof_and_rebind_claim(bundle)

    assert len(positions) > 13
    assert _verify(bundle) is None
    assert bundle["proof"]["status"] == host_vocal.BLOCKED_STATUS
    assert bundle["proof"]["decision"] == host_vocal.BLOCKED_DECISION
    assert bundle["proof"]["distribution"]["passed_count"] == 0


def test_singing_domain_session_bridge_keeps_five_of_seven_gate():
    assert host_vocal._checkpoint_passed(
        session_anchor_ready=True,
        median_score=0.0,
        session_anchor_score=0.22,
    )
    assert not host_vocal._checkpoint_passed(
        session_anchor_ready=True,
        median_score=0.0,
        session_anchor_score=0.219,
    )
    status, decision, distribution = host_vocal._decision_from_checkpoints(
        [
            {"bucket": bucket, "passed": passed}
            for bucket, passed in zip(
                host_vocal.CHECKPOINT_BUCKETS,
                (True, True, False, True, True, False, True),
                strict=True,
            )
        ]
    )
    assert status == host_vocal.READY_STATUS
    assert decision == host_vocal.READY_DECISION
    assert distribution["passed_count"] == 5


def test_spoken_canonical_rows_never_supply_campp_singing_checkpoints(tmp_path):
    bundle = _fixture(tmp_path)
    alignment = json.loads(bundle["alignment"].read_text(encoding="utf-8"))
    alignment["alignment"][3].update(host_vocal.READY_SPOKEN_ASSERTIONS)

    selected = host_vocal._selected_lyric_rows(alignment)

    assert len(selected) == len(host_vocal.CHECKPOINT_FRACTIONS)
    assert 3 not in {row["alignment_index"] for row in selected}
    assert {row["lidousha_role"] for row in selected} == {"SINGING_THIS_LYRIC"}


def test_checkpoint_singing_role_binding_is_reverified(tmp_path):
    bundle = _fixture(tmp_path)
    bundle["proof"]["checkpoints"][0]["lidousha_role"] = "PERFORMING_THIS_LYRIC_SPOKEN"
    _rewrite_proof_and_rebind_claim(bundle)

    error = _verify(bundle)

    assert error is not None and "not bound to its selected lyric row" in error


def test_duplicate_checkpoint_pcm_is_rejected(tmp_path):
    bundle = _fixture(tmp_path)
    first = bundle["proof"]["checkpoints"][0]
    duplicate = bundle["proof"]["checkpoints"][1]
    duplicate["sample_path"] = first["sample_path"]
    duplicate["sample_sha256"] = first["sample_sha256"]
    _rewrite_proof_and_rebind_claim(bundle)

    error = _verify(bundle)

    assert error is not None
    assert "reuse decoded PCM" in error


@pytest.mark.parametrize(
    ("status", "expected_rc"),
    [(host_vocal.READY_STATUS, 0), (host_vocal.BLOCKED_STATUS, 3)],
)
def test_cli_exit_code_distinguishes_ready_from_honest_block(tmp_path, monkeypatch, status, expected_rc):
    decision = host_vocal.READY_DECISION if status == host_vocal.READY_STATUS else host_vocal.BLOCKED_DECISION
    monkeypatch.setattr(
        host_vocal,
        "generate_host_vocal_proof",
        lambda **_kwargs: {"status": status, "decision": decision, "proof_path": "proof.json", "proof_sha256": "0" * 64},
    )
    argv = [
        "--source-media", str(tmp_path / "source.mp4"),
        "--candidate-id", "candidate",
        "--lyrics-alignment-report", str(tmp_path / "alignment.json"),
        "--reference-profile", str(tmp_path / "profile.json"),
        "--reference-dir", str(tmp_path / "references"),
        "--model-dir", str(tmp_path / "model"),
        "--output", str(tmp_path / "proof.json"),
    ]

    assert host_vocal.main(argv) == expected_rc


def test_cli_runtime_error_returns_rc2(tmp_path, monkeypatch):
    def fail(**_kwargs):
        raise host_vocal.HostVocalProofError("test failure")

    monkeypatch.setattr(host_vocal, "generate_host_vocal_proof", fail)
    argv = [
        "--source-media", str(tmp_path / "source.mp4"),
        "--candidate-id", "candidate",
        "--lyrics-alignment-report", str(tmp_path / "alignment.json"),
        "--reference-profile", str(tmp_path / "profile.json"),
        "--reference-dir", str(tmp_path / "references"),
        "--model-dir", str(tmp_path / "model"),
        "--output", str(tmp_path / "proof.json"),
    ]

    assert host_vocal.main(argv) == 2


def test_missing_proof_fails_closed(tmp_path):
    bundle = _fixture(tmp_path)
    bundle["proof_path"].unlink()

    error = _verify(bundle)

    assert error is not None
    assert "No such file" in error or "does not exist" in error


def test_tampered_proof_artifact_is_rejected_by_claim_hash(tmp_path):
    bundle = _fixture(tmp_path)
    bundle["proof_path"].write_text(bundle["proof_path"].read_text(encoding="utf-8") + " ", encoding="utf-8")

    assert _verify(bundle) == "host vocal proof sha256 mismatch"


@pytest.mark.parametrize("wrong_binding", ["source", "candidate"])
def test_wrong_source_or_candidate_is_rejected(tmp_path, wrong_binding):
    bundle = _fixture(tmp_path)
    overrides = {}
    if wrong_binding == "source":
        wrong_source = tmp_path / "other-source.mp4"
        wrong_source.write_bytes(b"different-source-media")
        overrides["source"] = wrong_source
    else:
        overrides["candidate_id"] = "some-other-candidate"

    error = _verify(bundle, **overrides)

    assert error is not None
    assert "mismatch" in error


def test_forged_threshold_result_is_recomputed_from_three_scores(tmp_path):
    bundle = _fixture(tmp_path)
    checkpoint = bundle["proof"]["checkpoints"][0]
    for score in checkpoint["scores"]:
        score["score"] = 0.10
    # Attacker leaves the declared median/pass at the old passing values and
    # recomputes the outer artifact hash.  The verifier must still reject it.
    _rewrite_proof_and_rebind_claim(bundle)

    error = _verify(bundle)

    assert error is not None
    assert "median score was not recomputed honestly" in error


def test_out_of_range_similarity_score_is_rejected(tmp_path):
    bundle = _fixture(tmp_path)
    checkpoint = bundle["proof"]["checkpoints"][0]
    checkpoint["scores"][0]["score"] = 999
    checkpoint["median_score"] = 0.40
    _rewrite_proof_and_rebind_claim(bundle)

    error = _verify(bundle)

    assert error is not None
    assert "within [-1, 1]" in error


def test_alignment_audio_source_must_equal_host_vocal_source(tmp_path):
    bundle = _fixture(tmp_path)
    alignment_payload = json.loads(bundle["alignment"].read_text(encoding="utf-8"))
    alignment_payload["audio_alignment_artifacts"]["source_sha256"] = "f" * 64
    _write_json(bundle["alignment"], alignment_payload)
    bundle["proof"]["lyrics_alignment_report"]["sha256"] = _sha(bundle["alignment"])
    _rewrite_proof_and_rebind_claim(bundle)

    error = _verify(bundle)

    assert error == "audio alignment and host-vocal proof source sha256 mismatch"


def test_forged_aggregate_ready_decision_is_recomputed(tmp_path):
    bundle = _fixture(tmp_path, passes=(True, False, True, False, False, False, True))
    assert bundle["claim"]["status"] == host_vocal.BLOCKED_STATUS
    bundle["proof"]["status"] = host_vocal.READY_STATUS
    bundle["proof"]["decision"] = host_vocal.READY_DECISION
    bundle["claim"]["status"] = host_vocal.READY_STATUS
    bundle["claim"]["decision"] = host_vocal.READY_DECISION
    _rewrite_proof_and_rebind_claim(bundle)

    error = _verify(bundle)

    assert error is not None
    assert "aggregate decision mismatch" in error


def test_four_passes_concentrated_without_tail_remain_blocked(tmp_path):
    bundle = _fixture(tmp_path, passes=(True, True, True, True, False, False, False))

    assert bundle["claim"]["status"] == host_vocal.BLOCKED_STATUS
    assert bundle["claim"]["decision"] == host_vocal.BLOCKED_DECISION
    assert bundle["proof"]["distribution"]["passed_count"] == 4
    assert bundle["proof"]["distribution"]["bucket_coverage"]["tail"] is False
    assert _verify(bundle) is None


def test_four_passes_with_all_buckets_still_fail_five_of_seven_minimum(tmp_path):
    bundle = _fixture(tmp_path, passes=(True, False, True, True, False, True, False))

    assert bundle["claim"]["status"] == host_vocal.BLOCKED_STATUS
    assert bundle["proof"]["distribution"]["passed_count"] == 4
    assert all(bundle["proof"]["distribution"]["bucket_coverage"].values())
    assert _verify(bundle) is None


@pytest.mark.parametrize("tampered_input", ["reference", "model", "profile"])
def test_reference_model_and_profile_hashes_are_recomputed(tmp_path, tampered_input):
    bundle = _fixture(tmp_path)
    if tampered_input == "reference":
        reference = Path(bundle["proof"]["references"][1]["path"])
        reference.write_bytes(b"tampered-reference")
    elif tampered_input == "model":
        model_file = Path(bundle["proof"]["speaker_model"]["path"]) / "model.bin"
        model_file.write_bytes(b"tampered-model")
    else:
        bundle["profile"].write_text(bundle["profile"].read_text(encoding="utf-8") + " ", encoding="utf-8")

    error = _verify(bundle)

    assert error is not None
    assert "sha256 mismatch" in error
