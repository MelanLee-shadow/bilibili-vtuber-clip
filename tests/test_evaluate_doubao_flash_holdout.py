import json
from pathlib import Path

import pytest

from scripts import evaluate_doubao_flash_holdout as holdout
from src.autoslice.doubao_transcription import DoubaoTranscriptionError


CANDIDATE = "auto_test_100_102"


def _write_srt(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n", encoding="utf-8")


def _fixture(tmp_path: Path) -> dict:
    audio = tmp_path / "audio" / f"{CANDIDATE}.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"not-a-real-mp3-but-content-bound")

    cache_root = tmp_path / "cache"
    bcut = cache_root / "results" / CANDIDATE / "bcut" / "transcript.srt"
    _write_srt(bcut, "你好世间")

    baseline_root = tmp_path / "baselines"
    reference = baseline_root / f"{CANDIDATE}.reviewed.srt"
    _write_srt(reference, "你好世界")
    metadata = baseline_root / f"{CANDIDATE}.subtitle-baseline.v1.json"
    metadata.write_text(
        json.dumps(
            {
                "path": reference.name,
                "sha256": holdout._sha256_path(reference),
            }
        ),
        encoding="utf-8",
    )

    intent = tmp_path / "history" / "intent.json"
    intent.parent.mkdir(parents=True)
    intent.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "candidate_id": CANDIDATE,
                        "duration_ms": 2_000,
                        "encoded_audio_path": str(audio),
                        "cache_root": str(cache_root),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    generation_path = tmp_path / "frozen" / "generation.json"
    truth_path = tmp_path / "withheld" / "truth.json"
    generation, truth = holdout.freeze_manifests(
        historical_intent=intent,
        baseline_root=baseline_root,
        generation_manifest_path=generation_path,
        truth_manifest_path=truth_path,
    )
    return {
        "audio": audio,
        "bcut": bcut,
        "reference": reference,
        "generation_path": generation_path,
        "truth_path": truth_path,
        "generation": generation,
        "truth": truth,
        "result_dir": tmp_path / "results",
    }


def _ready() -> dict:
    return {
        "configured": True,
        "source": "environment",
        "reason_code": "DOUBAO_API_KEY_READY",
    }


def _missing() -> dict:
    return {
        "configured": False,
        "source": "none",
        "reason_code": "DOUBAO_API_KEY_MISSING",
    }


def _perfect_evidence(audio: Path, request_id: str) -> dict:
    return {
        "provider": "doubao_flash",
        "model": holdout.DOUBAO_FLASH_MODEL,
        "resource_id": holdout.DOUBAO_FLASH_RESOURCE,
        "input_audio_sha256": holdout._sha256_path(audio),
        "request_config_sha256": "a" * 64,
        "response_sha256": "b" * 64,
        "request_id": request_id,
        "status": "OK",
        "text": "你好世界",
        "native_segments": [
            {
                "start_ms": 0,
                "end_ms": 2_000,
                "text": "你好世界",
                "speaker": None,
                "words": None,
                "words_available": False,
            }
        ],
        "segment_count": 1,
        "speaker_labels": [],
        "diagnostics": [],
        "native_timeline": {
            "has_overlap": False,
            "has_out_of_order": False,
            "one_track_srt_eligible": True,
        },
        "one_track_srt_eligible": True,
        "candidate_exposure": "none",
        "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False,
    }


def test_freeze_splits_generation_from_withheld_truth(tmp_path: Path):
    fixture = _fixture(tmp_path)
    generation_text = fixture["generation_path"].read_text(encoding="utf-8")
    truth_text = fixture["truth_path"].read_text(encoding="utf-8")

    assert fixture["generation"]["item_count"] == 1
    assert fixture["generation"]["scoring_truth_supplied_to_provider_run"] is False
    assert fixture["generation"]["generation_process_accepts_truth_manifest"] is False
    assert fixture["generation"]["provider_contract"]["request_policy"]["hotwords"] is False
    assert "你好世界" not in generation_text
    assert str(fixture["reference"]) not in generation_text
    assert str(fixture["reference"]) in truth_text
    assert fixture["truth"]["generation_manifest_sha256"] == fixture["generation"][
        "manifest_sha256"
    ]
    assert fixture["truth"]["standard_acoustic_cer_truth"] is False


def test_preflight_without_credentials_opens_no_truth_and_calls_no_provider(
    tmp_path: Path, monkeypatch
):
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(holdout, "doubao_api_key_status", _missing)
    monkeypatch.setattr(
        holdout,
        "transcribe_doubao_flash_evidence",
        lambda *args, **kwargs: pytest.fail("preflight must not call the provider"),
    )

    report = holdout.preflight(
        generation_manifest_path=fixture["generation_path"],
        output_path=tmp_path / "preflight.json",
    )

    assert report["status"] == "BLOCKED_NO_CREDENTIALS"
    assert report["provider_calls"] == 0
    assert report["truth_manifest_opened"] is False
    assert report["items"][0]["status"] == "INPUTS_VERIFIED"


def test_run_requires_explicit_flag_and_never_loads_truth(tmp_path: Path, monkeypatch):
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(holdout, "doubao_api_key_status", _ready)
    monkeypatch.setattr(
        holdout,
        "_load_truth_manifest",
        lambda *_: pytest.fail("provider run must not open withheld truth"),
    )
    monkeypatch.setattr(
        holdout,
        "transcribe_doubao_flash_evidence",
        lambda *args, **kwargs: pytest.fail("provider flag is required"),
    )

    report = holdout.run_provider(
        generation_manifest_path=fixture["generation_path"],
        result_dir=fixture["result_dir"],
        execute_provider_calls=False,
    )

    assert report["status"] == "REFUSED_EXPLICIT_PROVIDER_FLAG_REQUIRED"
    assert report["provider_calls"] == 0
    assert report["truth_manifest_opened"] is False


def test_success_is_cached_without_credentials_and_scores_against_bcut(
    tmp_path: Path, monkeypatch
):
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(holdout, "doubao_api_key_status", _ready)
    calls = []

    def fake_provider(audio, *, duration_ms, before_request, request_id, expected_audio_sha256):
        assert expected_audio_sha256 == fixture["generation"]["items"][0]["audio"]["sha256"]
        calls.append((audio, duration_ms, request_id))
        before_request()
        return _perfect_evidence(audio, request_id)

    monkeypatch.setattr(holdout, "transcribe_doubao_flash_evidence", fake_provider)
    first = holdout.run_provider(
        generation_manifest_path=fixture["generation_path"],
        result_dir=fixture["result_dir"],
        execute_provider_calls=True,
    )

    assert first["status"] == "COMPLETE"
    assert first["provider_calls"] == 1
    assert first["items"][0]["status"] == "COMPLETE"
    assert len(calls) == 1

    monkeypatch.setattr(holdout, "doubao_api_key_status", _missing)
    monkeypatch.setattr(
        holdout,
        "transcribe_doubao_flash_evidence",
        lambda *args, **kwargs: pytest.fail("completed result must be cache-only"),
    )
    cached = holdout.run_provider(
        generation_manifest_path=fixture["generation_path"],
        result_dir=fixture["result_dir"],
        execute_provider_calls=True,
    )
    assert cached["status"] == "COMPLETE"
    assert cached["provider_calls"] == 0
    assert cached["items"][0]["status"] == "CACHED_COMPLETE"

    score = holdout.score_results(
        generation_manifest_path=fixture["generation_path"],
        truth_manifest_path=fixture["truth_path"],
        result_dir=fixture["result_dir"],
        output_path=tmp_path / "score.json",
    )
    aggregate = score["aggregate"]
    assert aggregate["scored_items"] == 1
    assert aggregate["bcut_d_keep_distance"] == 1
    assert aggregate["doubao_d_keep_distance"] == 0
    assert aggregate["paired_wins"] == 1
    assert aggregate["raw_asr_verdict"].startswith("DOUBAO_FLASH_RAW_ASR_IMPROVED")
    assert aggregate["production_decision"] == "NOT_AUTHORIZED_EXPLORATORY_RAW_ASR_ONLY"
    assert aggregate["pilot_advance_decision"] == "ADVANCE_TO_LARGER_UNTOUCHED_HOLDOUT_ONLY"
    assert aggregate["relative_d_keep_reduction"] == 1.0
    assert score["standard_acoustic_cer"] is False


def test_ambiguous_flash_dispatch_is_never_automatically_resubmitted(
    tmp_path: Path, monkeypatch
):
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(holdout, "doubao_api_key_status", _ready)
    calls = []

    def ambiguous(audio, *, duration_ms, before_request, request_id, expected_audio_sha256):
        assert expected_audio_sha256 == fixture["generation"]["items"][0]["audio"]["sha256"]
        calls.append(request_id)
        before_request()
        raise DoubaoTranscriptionError(
            "DOUBAO_TRANSPORT_ERROR",
            "simulated ambiguous one-shot dispatch",
        )

    monkeypatch.setattr(holdout, "transcribe_doubao_flash_evidence", ambiguous)
    first = holdout.run_provider(
        generation_manifest_path=fixture["generation_path"],
        result_dir=fixture["result_dir"],
        execute_provider_calls=True,
    )
    assert first["status"] == "BLOCKED_OR_FAILED"
    assert first["provider_calls"] == 1
    assert first["items"][0]["ledger_state"] == "SUBMIT_AMBIGUOUS"
    assert len(calls) == 1

    monkeypatch.setattr(
        holdout,
        "transcribe_doubao_flash_evidence",
        lambda *args, **kwargs: pytest.fail("ambiguous request must not be resubmitted"),
    )
    second = holdout.run_provider(
        generation_manifest_path=fixture["generation_path"],
        result_dir=fixture["result_dir"],
        execute_provider_calls=True,
    )
    assert second["provider_calls"] == 0
    assert second["items"][0]["status"] == "BLOCKED_NO_AUTOMATIC_RESUBMIT"
    assert second["items"][0]["ledger_state"] == "SUBMIT_AMBIGUOUS"


def test_new_item_without_credentials_remains_prepared_and_sends_nothing(
    tmp_path: Path, monkeypatch
):
    fixture = _fixture(tmp_path)
    monkeypatch.setattr(holdout, "doubao_api_key_status", _missing)
    monkeypatch.setattr(
        holdout,
        "transcribe_doubao_flash_evidence",
        lambda *args, **kwargs: pytest.fail("missing credentials must block before provider"),
    )

    report = holdout.run_provider(
        generation_manifest_path=fixture["generation_path"],
        result_dir=fixture["result_dir"],
        execute_provider_calls=True,
    )

    assert report["status"] == "BLOCKED_OR_FAILED"
    assert report["provider_calls"] == 0
    assert report["items"][0]["status"] == "BLOCKED_NO_CREDENTIALS"
    ledger_file = next((fixture["result_dir"] / "dispatch-ledgers").glob("*.json"))
    assert json.loads(ledger_file.read_text(encoding="utf-8"))["state"] == "PREPARED"


def test_manifest_tamper_fails_before_any_provider_call(tmp_path: Path, monkeypatch):
    fixture = _fixture(tmp_path)
    value = json.loads(fixture["generation_path"].read_text(encoding="utf-8"))
    value["items"][0]["duration_ms"] = 9_999
    fixture["generation_path"].write_text(json.dumps(value), encoding="utf-8")
    fixture["generation_path"].chmod(0o600)
    monkeypatch.setattr(
        holdout,
        "transcribe_doubao_flash_evidence",
        lambda *args, **kwargs: pytest.fail("tampered manifest must not reach provider"),
    )

    with pytest.raises(holdout.HoldoutError) as exc_info:
        holdout.run_provider(
            generation_manifest_path=fixture["generation_path"],
            result_dir=fixture["result_dir"],
            execute_provider_calls=True,
        )
    assert exc_info.value.reason_code == "HOLDOUT_HASH_MISMATCH"


def test_score_strips_declared_non_acoustic_speaker_labels() -> None:
    reference = [
        {
            "n": 1,
            "start_ms": 0,
            "end_ms": 2_000,
            "text": "[李豆沙] 你好世界 [李豆沙]",
        }
    ]
    hypothesis = [
        {
            "n": 1,
            "start_ms": 0,
            "end_ms": 2_000,
            "text": "你好世间",
        }
    ]

    score = holdout._score_cues(reference, hypothesis)

    assert score["reference_chars"] == 4
    assert score["d_keep_distance"] == 1
    assert score["whole_text_distance"] == 1
    assert score["reference_annotations_removed"] == {"[李豆沙]": 2}
    assert score["reference_annotation_policy"] == (
        "lidousha-reviewed-reference-speaker-labels.v1"
    )
    assert score["temporal_groups"][0]["reference"] == "[李豆沙] 你好世界 [李豆沙]"
    assert score["temporal_groups"][0]["scored_reference"].strip() == "你好世界"


def test_score_preserves_undeclared_bracketed_spoken_text() -> None:
    reference = [
        {
            "n": 1,
            "start_ms": 0,
            "end_ms": 2_000,
            "text": "念出来方括号测试",
        }
    ]
    hypothesis = [
        {
            "n": 1,
            "start_ms": 0,
            "end_ms": 2_000,
            "text": "测试",
        }
    ]

    score = holdout._score_cues(reference, hypothesis)

    assert score["reference_annotations_removed"] == {}
    assert score["reference_chars"] == len(holdout._normalize(reference[0]["text"]))


def test_resealed_pilot_policy_change_is_rejected_before_provider(
    tmp_path: Path, monkeypatch
):
    fixture = _fixture(tmp_path)
    value = json.loads(fixture["generation_path"].read_text(encoding="utf-8"))
    value["pilot_decision_policy"]["minimum_relative_d_keep_reduction"] = 0.0
    unsigned = dict(value)
    unsigned.pop("manifest_sha256")
    value["manifest_sha256"] = holdout._digest(unsigned)
    fixture["generation_path"].write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8"
    )
    fixture["generation_path"].chmod(0o600)
    monkeypatch.setattr(
        holdout,
        "transcribe_doubao_flash_evidence",
        lambda *args, **kwargs: pytest.fail("changed decision policy must block first"),
    )

    with pytest.raises(holdout.HoldoutError) as exc_info:
        holdout.run_provider(
            generation_manifest_path=fixture["generation_path"],
            result_dir=fixture["result_dir"],
            execute_provider_calls=True,
        )
    assert exc_info.value.reason_code == "HOLDOUT_DECISION_POLICY_MISMATCH"



def test_changed_audio_is_rejected_before_any_flash_dispatch(tmp_path, monkeypatch):
    """The actual client, not a client stub, must enforce the frozen bytes."""
    import base64
    from src.autoslice import doubao_transcription as client

    fixture = _fixture(tmp_path)
    monkeypatch.setenv("AUTOSLICE_DOUBAO_API_KEY", "synthetic-intake-no-network")
    monkeypatch.delenv("AUTOSLICE_DOUBAO_API_KEY_FILE", raising=False)
    monkeypatch.delenv("VOLC_ASR_API_KEY", raising=False)
    read_audio = client._read_audio_path
    sent = []

    def changed_after_preflight(path):
        path.write_bytes(b"x" * path.stat().st_size)
        return read_audio(path)

    def offline_transport(endpoint, payload, headers, api_key):
        sent.append(base64.b64decode(payload["audio"]["data"]))
        body = json.dumps({"result": {"text": "test", "utterances": [
            {"text": "test", "start_time": 0, "end_time": 2000}
        ]}}).encode()
        return 200, {"provider_status_code": "20000000", "log_id": "synthetic"}, body

    monkeypatch.setattr(client, "_read_audio_path", changed_after_preflight)
    monkeypatch.setattr(client, "_request_once", offline_transport)
    report = None
    try:
        report = holdout.run_provider(
            generation_manifest_path=fixture["generation_path"],
            result_dir=fixture["result_dir"], execute_provider_calls=True,
        )
    except holdout.HoldoutError:
        # The old implementation refuses only after our fake transport got bytes.
        pass
    assert sent == [], "changed bytes reached transport before binding validation"
    assert report is not None and report["provider_calls"] == 0
    assert report["items"][0]["reason_code"] == "DOUBAO_AUDIO_CHANGED"
    ledger_file = next((fixture["result_dir"] / "dispatch-ledgers").glob("*.json"))
    assert json.loads(ledger_file.read_text())["state"] == "PREPARED"
