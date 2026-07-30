from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from src.autoslice.cover_host_identity_gate import (
    validate_final_host_identity_verification,
    verify_lidousha_final_host_identity,
)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_witness(answer: dict[str, object]):
    def probe(image_path: Path, _question: str, **_kwargs):
        return {
            "status": "OBSERVED",
            "provider": "cpa",
            "image_sha256": hashlib.sha256(
                Path(image_path).read_bytes()
            ).hexdigest(),
            "answer": json.dumps(answer, ensure_ascii=False),
        }

    return probe


def test_wrong_source_participant_as_protagonist_fails_closed(
    tmp_path, monkeypatch
):
    from src.autoslice import cpa_frame_witness

    reference = tmp_path / "source.png"
    final = tmp_path / "final.png"
    Image.new("RGB", (1920, 1080), (20, 30, 40)).save(reference)
    Image.new("RGB", (1920, 1080), (50, 60, 70)).save(final)
    monkeypatch.setattr(
        cpa_frame_witness,
        "image_vision_probe",
        _fake_witness(
            {
                "source_lidousha_located": True,
                "primary_subject_is_lidousha": False,
                "primary_subject_matches_other_source_participant": True,
                "primary_subject_identity": "伊索尔Sol",
                "identity_conflicts": ["红金角饰与伊索尔一致"],
                "reason": "主角沿用了伊索尔而不是李豆沙",
            }
        ),
    )

    receipt = verify_lidousha_final_host_identity(
        final_cover_path=final,
        final_cover_sha256=_sha(final),
        reference_path=reference,
    )

    assert receipt["status"] == "FAIL"
    assert receipt["reason_code"] == "FINAL_HOST_IDENTITY_MISMATCH"
    assert not validate_final_host_identity_verification(
        {
            "final_cover_sha256": _sha(final),
            "final_host_identity_verification": receipt,
        }
    )


def test_source_final_lidousha_identity_match_passes_and_binds_hash(
    tmp_path, monkeypatch
):
    from src.autoslice import cpa_frame_witness

    reference = tmp_path / "source.png"
    final = tmp_path / "final.png"
    Image.new("RGB", (1920, 1080), (20, 30, 40)).save(reference)
    Image.new("RGB", (1920, 1080), (80, 90, 100)).save(final)
    monkeypatch.setattr(
        cpa_frame_witness,
        "image_vision_probe",
        _fake_witness(
            {
                "source_lidousha_located": True,
                "primary_subject_is_lidousha": True,
                "primary_subject_matches_other_source_participant": False,
                "primary_subject_identity": "李豆沙",
                "identity_conflicts": [],
                "reason": "主角与源图李豆沙一致",
            }
        ),
    )

    receipt = verify_lidousha_final_host_identity(
        final_cover_path=final,
        final_cover_sha256=_sha(final),
        reference_path=reference,
    )
    generation = {
        "final_cover_sha256": _sha(final),
        "final_host_identity_verification": receipt,
    }

    assert receipt["status"] == "PASS"
    assert validate_final_host_identity_verification(generation)
    generation["final_cover_sha256"] = "sha256:" + "0" * 64
    assert not validate_final_host_identity_verification(generation)


def test_agy_identity_receipt_requires_disclosed_cpa_failure():
    digest = "b" * 64
    witness = {
        "status": "OBSERVED",
        "provider": "agy",
        "image_sha256": digest,
        "routing": {
            "preferred_provider": "cpa",
            "selected_provider": "agy",
            "fallback_used": True,
            "primary_status": "UNAVAILABLE",
            "primary_reason_code": "VISION_CALL_FAILED",
            "primary_receipt": {
                "status": "UNAVAILABLE",
                "provider": "cpa",
                "reason_code": "VISION_CALL_FAILED",
            },
        },
    }
    generation = {
        "final_cover_sha256": "sha256:" + "a" * 64,
        "final_host_identity_verification": {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v2"
            ),
            "authority": (
                "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_COMPARISON"
            ),
            "status": "PASS",
            "final_cover_sha256": "sha256:" + "a" * 64,
            "comparison_sha256": "sha256:" + digest,
            "witness": witness,
        },
    }

    assert validate_final_host_identity_verification(generation)
    witness["routing"].pop("primary_receipt")
    assert not validate_final_host_identity_verification(generation)


def test_cpa_redraw_blocks_before_ready_when_host_identity_is_wrong(
    tmp_path, monkeypatch
):
    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import (
        _write_synthetic_performance_clip,
    )

    media = _write_synthetic_performance_clip(tmp_path)
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "cpa")

    def fake_image_edit(**kwargs):
        Image.new("RGB", (1920, 1080), (90, 30, 70)).save(
            kwargs["output_path"]
        )
        return {
            "status": "AI_BACKGROUND_READY",
            "selected_model": "gpt-image-2",
            "attempted_models": ["gpt-image-2"],
        }

    def wrong_identity(**_kwargs):
        return {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v2"
            ),
            "status": "FAIL",
            "reason_code": "FINAL_HOST_IDENTITY_MISMATCH",
        }

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="pink-multi-person-regression",
        title="【李豆沙】小姐姐布下迷魂阵，我是侄女啊",
        cover_text="小姐姐布下迷魂阵\n我是侄女啊",
        run_ffmpeg=True,
        image_edit=fake_image_edit,
        final_host_identity_verifier=wrong_identity,
        enforce_final_host_identity=True,
    )

    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert result["reason_codes"] == [
        "COVER_FINAL_HOST_IDENTITY_UNVERIFIED"
    ]
    generation = result["cover_generation"]
    assert generation["route_decision"]["host_identity_required"] is True
    assert generation["route_decision"]["actual_treatment"] is None
    assert generation["route_decision"]["execution_status"] == "BLOCKED"


def test_cpa_redraw_degrades_to_source_pixels_when_identity_witness_is_down(
    tmp_path, monkeypatch
):
    from src.autoslice import publish_staging
    from src.autoslice.cover_route_evidence import (
        validate_cover_route_decision,
    )
    from tests.test_cover_frame_selection import (
        _write_synthetic_performance_clip,
    )

    media = _write_synthetic_performance_clip(tmp_path)
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "cpa")
    image_calls = 0
    identity_calls = 0

    def fake_image_edit(**kwargs):
        nonlocal image_calls
        image_calls += 1
        Image.new("RGB", (1920, 1080), (90, 30, 70)).save(
            kwargs["output_path"]
        )
        return {
            "status": "AI_BACKGROUND_READY",
            "selected_model": "gpt-image-2",
            "attempted_models": ["gpt-image-2"],
        }

    def unavailable_identity(**_kwargs):
        nonlocal identity_calls
        identity_calls += 1
        return {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v2"
            ),
            "status": "FAIL",
            "reason_code": "HOST_IDENTITY_WITNESS_UNAVAILABLE",
        }

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="identity-witness-down",
        title="【李豆沙】突然开起日语人称翻译大会",
        cover_text="日语人称翻译大会",
        run_ffmpeg=True,
        image_edit=fake_image_edit,
        final_host_identity_verifier=unavailable_identity,
        enforce_final_host_identity=True,
    )

    assert result["status"] == "AI_COVER_READY"
    assert image_calls == identity_calls == 1
    generation = result["cover_generation"]
    route = generation["route_decision"]
    assert generation["method"] == "screenshot_direct"
    assert generation["cover_origin"] == "SOURCE_SCREENSHOT"
    assert generation["cpa_redraw"]["status"] == (
        "DEGRADED_TO_DIRECT_IDENTITY_WITNESS_UNAVAILABLE"
    )
    assert route["selected_treatment"] == "cpa_redraw"
    assert route["actual_treatment"] == "screenshot_direct"
    assert route["execution_status"] == "READY_DEGRADED"
    assert generation["status"] == "READY_DEGRADED"
    assert validate_cover_route_decision(generation)


def test_cpa_redraw_retries_once_and_recovers_host_identity(
    tmp_path, monkeypatch
):
    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import (
        _write_synthetic_performance_clip,
    )

    media = _write_synthetic_performance_clip(tmp_path)
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    monkeypatch.setenv("AUTOSLICE_COVER_MODE", "cpa")
    image_calls = 0
    identity_calls = 0

    def fake_image_edit(**kwargs):
        nonlocal image_calls
        image_calls += 1
        Image.new("RGB", (1920, 1080), (30 * image_calls, 40, 80)).save(
            kwargs["output_path"]
        )
        return {
            "status": "AI_BACKGROUND_READY",
            "selected_model": "gpt-image-2",
            "attempted_models": ["gpt-image-2"],
        }

    def identity_verifier(*, final_cover_sha256: str, **_kwargs):
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls == 1:
            return {
                "schema_version": (
                    "lidousha-cover-final-host-identity-verification.v2"
                ),
                "status": "FAIL",
                "reason_code": "FINAL_HOST_IDENTITY_MISMATCH",
            }
        comparison_hash = "b" * 64
        return {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v2"
            ),
            "authority": (
                "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_COMPARISON"
            ),
            "status": "PASS",
            "final_cover_sha256": final_cover_sha256,
            "comparison_sha256": "sha256:" + comparison_hash,
            "witness": {
                "status": "OBSERVED",
                "provider": "cpa",
                "image_sha256": comparison_hash,
            },
        }

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="pink-multi-person-auto-recovery",
        title="【李豆沙】小姐姐布下迷魂阵，我是侄女啊",
        cover_text="小姐姐布下迷魂阵\n我是侄女啊",
        run_ffmpeg=True,
        image_edit=fake_image_edit,
        final_host_identity_verifier=identity_verifier,
        enforce_final_host_identity=True,
    )

    assert result["status"] == "AI_COVER_READY"
    assert image_calls == identity_calls == 2
    generation = result["cover_generation"]
    assert generation["host_identity_retry"]["status"] == "PASS"
    assert generation["final_host_identity_verification"]["status"] == "PASS"
    assert generation["route_decision"]["execution_status"] == "READY"
