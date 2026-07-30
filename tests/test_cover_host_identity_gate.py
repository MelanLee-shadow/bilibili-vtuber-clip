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
            "image_sha256": hashlib.sha256(
                Path(image_path).read_bytes()
            ).hexdigest(),
            "answer": json.dumps(answer, ensure_ascii=False),
        }

    return probe


def test_wrong_source_participant_as_protagonist_fails_closed(
    tmp_path, monkeypatch
):
    from src.autoslice import agy_frame_witness

    reference = tmp_path / "source.png"
    final = tmp_path / "final.png"
    Image.new("RGB", (1920, 1080), (20, 30, 40)).save(reference)
    Image.new("RGB", (1920, 1080), (50, 60, 70)).save(final)
    monkeypatch.setattr(
        agy_frame_witness,
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
    from src.autoslice import agy_frame_witness

    reference = tmp_path / "source.png"
    final = tmp_path / "final.png"
    Image.new("RGB", (1920, 1080), (20, 30, 40)).save(reference)
    Image.new("RGB", (1920, 1080), (80, 90, 100)).save(final)
    monkeypatch.setattr(
        agy_frame_witness,
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
                "lidousha-cover-final-host-identity-verification.v1"
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
