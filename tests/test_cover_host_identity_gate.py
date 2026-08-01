from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from src.autoslice.cover_host_identity_gate import (
    validate_final_host_identity_verification,
    verify_lidousha_final_host_identity,
)
from src.autoslice.cover_source_composition import (
    verify_lidousha_source_composition,
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


def _source_composition_redraw(**kwargs):
    reference_path = Path(kwargs["reference_path"])

    def probe(image_path: Path, _question: str, **_probe_kwargs):
        return {
            "status": "OBSERVED",
            "provider": "cpa",
            "image_sha256": hashlib.sha256(
                Path(image_path).read_bytes()
            ).hexdigest(),
            "answer": json.dumps(
                {
                    "lidousha_bbox_frac": [0.79, 0.70, 0.96, 0.98],
                    "source_face_complete": True,
                    "faithful_crop_can_make_dominant": False,
                    "source_carries_story_reaction": False,
                    "cpa_redraw_recommended": True,
                    "reason": "李豆沙只在角落，源图不承载标题反应",
                },
                ensure_ascii=False,
            ),
            "routing": {
                "preferred_provider": "cpa",
                "selected_provider": "cpa",
                "fallback_used": False,
            },
        }

    return verify_lidousha_source_composition(
        reference_path=reference_path,
        reference_sha256=str(kwargs["reference_sha256"]),
        story_hook=str(kwargs.get("story_hook") or ""),
        title=str(kwargs.get("title") or ""),
        image_probe=probe,
    )


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
                "primary_subject_is_visually_dominant": True,
                "primary_subject_face_is_large_and_clear": True,
                "primary_subject_carries_story_reaction": True,
                "excessive_dead_space": False,
                "meaningless_dominant_decoration": False,
                "thumbnail_has_clear_click_hook": True,
                "primary_subject_identity": "伊索尔Sol",
                "identity_conflicts": ["红金角饰与伊索尔一致"],
                "composition_conflicts": [],
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
                "primary_subject_is_visually_dominant": True,
                "primary_subject_face_is_large_and_clear": True,
                "primary_subject_carries_story_reaction": True,
                "excessive_dead_space": False,
                "meaningless_dominant_decoration": False,
                "thumbnail_has_clear_click_hook": True,
                "primary_subject_identity": "李豆沙",
                "identity_conflicts": [],
                "composition_conflicts": [],
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


def test_1411_small_lower_corner_lidousha_and_dead_space_fails_closed(
    tmp_path, monkeypatch
):
    """Identity alone cannot release the 7/26 1411-style bad thumbnail."""

    from src.autoslice import cpa_frame_witness

    reference = tmp_path / "source.png"
    final = tmp_path / "final.png"
    Image.new("RGB", (1920, 1080), (20, 30, 40)).save(reference)
    Image.new("RGB", (1920, 1080), (120, 18, 28)).save(final)
    monkeypatch.setattr(
        cpa_frame_witness,
        "image_vision_probe",
        _fake_witness(
            {
                "source_lidousha_located": True,
                "primary_subject_is_lidousha": True,
                "primary_subject_matches_other_source_participant": False,
                "primary_subject_is_visually_dominant": False,
                "primary_subject_face_is_large_and_clear": False,
                "primary_subject_carries_story_reaction": False,
                "excessive_dead_space": True,
                "meaningless_dominant_decoration": True,
                "thumbnail_has_clear_click_hook": False,
                "primary_subject_identity": "李豆沙",
                "identity_conflicts": [],
                "composition_conflicts": [
                    "李豆沙缩在右下角且主体过小",
                    "大片空白与无意义红条压过人物",
                ],
                "reason": "身份是李豆沙，但主体不显眼且构图不值得点击",
            }
        ),
    )

    receipt = verify_lidousha_final_host_identity(
        final_cover_path=final,
        final_cover_sha256=_sha(final),
        reference_path=reference,
    )

    assert receipt["status"] == "FAIL"
    assert receipt["reason_code"] == "FINAL_COVER_SUBJECT_PROMINENCE_FAILED"
    assert receipt["final_cover_sha256"] == _sha(final)
    assert not validate_final_host_identity_verification(
        {
            "final_cover_sha256": _sha(final),
            "final_host_identity_verification": receipt,
        }
    )


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
                "lidousha-cover-final-host-identity-verification.v3"
            ),
            "authority": (
                "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_AND_PROMINENCE_COMPARISON"
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
                "lidousha-cover-final-host-identity-verification.v3"
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
        source_composition_verifier=_source_composition_redraw,
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


def test_cpa_redraw_does_not_ship_source_pixels_when_identity_witness_is_down(
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
                "lidousha-cover-final-host-identity-verification.v3"
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
        source_composition_verifier=_source_composition_redraw,
        enforce_final_host_identity=True,
    )

    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert image_calls == 1
    assert identity_calls == 1
    generation = result["cover_generation"]
    route = generation["route_decision"]
    assert generation["method"] == "images.edit"
    assert generation["cover_origin"] == "AI_REDRAW"
    assert "cpa_redraw" not in generation
    assert generation["source_composition_verification"]["verdict"][
        "cpa_redraw_recommended"
    ] is True
    assert route["selected_treatment"] == "cpa_redraw"
    assert route["actual_treatment"] is None
    assert route["execution_status"] == "BLOCKED"
    assert generation["status"] == "BLOCKED"
    assert result["reason_codes"] == ["COVER_FINAL_HOST_IDENTITY_UNVERIFIED"]
    assert not validate_cover_route_decision(generation)


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
                    "lidousha-cover-final-host-identity-verification.v3"
                ),
                "status": "FAIL",
                "reason_code": "FINAL_HOST_IDENTITY_MISMATCH",
            }
        comparison_hash = "b" * 64
        return {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v3"
            ),
            "authority": (
                "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_AND_PROMINENCE_COMPARISON"
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
        source_composition_verifier=_source_composition_redraw,
        enforce_final_host_identity=True,
    )

    assert result["status"] == "AI_COVER_READY"
    assert image_calls == identity_calls == 2
    generation = result["cover_generation"]
    assert generation["host_identity_retry"]["status"] == "PASS"
    assert generation["final_host_identity_verification"]["status"] == "PASS"
    assert generation["route_decision"]["execution_status"] == "READY"


def test_1411_prominence_failure_retries_with_stronger_prompt_then_stays_pending(
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
    prompts: list[str] = []
    verification_calls = 0

    def fake_image_edit(**kwargs):
        prompts.append(str(kwargs["prompt"]))
        Image.new("RGB", (1920, 1080), (115, 20, 30)).save(
            kwargs["output_path"]
        )
        return {
            "status": "AI_BACKGROUND_READY",
            "selected_model": "gpt-image-2",
            "attempted_models": ["gpt-image-2"],
        }

    def small_corner_identity_match(**_kwargs):
        nonlocal verification_calls
        verification_calls += 1
        return {
            "schema_version": (
                "lidousha-cover-final-host-identity-verification.v3"
            ),
            "status": "FAIL",
            "reason_code": "FINAL_COVER_SUBJECT_PROMINENCE_FAILED",
            "detail": "李豆沙缩在右下角，大片空白和无意义红条压过主体",
        }

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="auto_152944_1411_1463",
        title="【李豆沙】刚解释完为什么被电，话音刚落小李就暴毙",
        cover_text="话音刚落\n小李暴毙",
        run_ffmpeg=True,
        image_edit=fake_image_edit,
        final_host_identity_verifier=small_corner_identity_match,
        source_composition_verifier=_source_composition_redraw,
        enforce_final_host_identity=True,
    )

    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert result["reason_codes"] == [
        "COVER_FINAL_HOST_IDENTITY_UNVERIFIED"
    ]
    assert len(prompts) == verification_calls == 2
    assert "MANDATORY SUBJECT PROMINENCE" in prompts[0]
    assert "Never shrink her into a corner" in prompts[0]
    assert "Never place her as a small lower-corner figure" in prompts[1]
    assert "meaningless solid-color/red bars" in prompts[1]
    generation = result["cover_generation"]
    assert generation["host_identity_retry"]["status"] == (
        "FAILED_FINAL_IDENTITY"
    )
    assert generation["route_decision"]["actual_treatment"] is None
    assert generation["route_decision"]["execution_status"] == "BLOCKED"


def _published_carry_verification() -> dict[str, object]:
    return {
        "schema_version": (
            "lidousha-cover-final-host-identity-verification.v2"
        ),
        "authority": (
            "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_COMPARISON"
        ),
        "status": "PASS",
        "final_cover_sha256": "sha256:" + "a" * 64,
        "comparison_sha256": "sha256:" + "b" * 64,
        "witness": {"provider": "cpa", "image_sha256": "b" * 64},
    }


def _published_carry_generation() -> dict[str, object]:
    return {
        "carried_forward_from_published_record": True,
        "final_cover_sha256": "sha256:" + "a" * 64,
        "final_host_identity_verification": _published_carry_verification(),
    }


def test_published_carry_v2_witness_accepted_for_byte_identical_reuse():
    assert validate_final_host_identity_verification(
        _published_carry_generation()
    )


def test_published_carry_clause_requires_carry_flag():
    generation = _published_carry_generation()
    generation.pop("carried_forward_from_published_record")
    assert not validate_final_host_identity_verification(generation)


def test_published_carry_clause_pins_exact_v2_generation_pair():
    for key, value in (
        ("schema_version", "lidousha-cover-final-host-identity-verification.v1"),
        (
            "authority",
            "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_AND_PROMINENCE_COMPARISON",
        ),
    ):
        generation = _published_carry_generation()
        generation["final_host_identity_verification"][key] = value
        assert not validate_final_host_identity_verification(generation)


def test_published_carry_clause_keeps_hash_and_status_gates():
    mismatched = _published_carry_generation()
    mismatched["final_cover_sha256"] = "sha256:" + "c" * 64
    assert not validate_final_host_identity_verification(mismatched)

    failed = _published_carry_generation()
    failed["final_host_identity_verification"]["status"] = "FAIL"
    assert not validate_final_host_identity_verification(failed)
