from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image, ImageStat

from src.autoslice.cover_source_composition import (
    validate_source_composition_verification,
    verify_lidousha_source_composition,
)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _probe(verdict: dict[str, object], *, provider: str = "cpa"):
    def probe(image_path: Path, _question: str, **_kwargs):
        return {
            "status": "OBSERVED",
            "provider": provider,
            "image_sha256": hashlib.sha256(
                Path(image_path).read_bytes()
            ).hexdigest(),
            "answer": json.dumps(verdict, ensure_ascii=False),
            "routing": {
                "preferred_provider": "cpa",
                "selected_provider": provider,
                "fallback_used": False,
            },
        }

    return probe


def _safe_verification(reference: Path) -> dict[str, object]:
    return verify_lidousha_source_composition(
        reference_path=reference,
        reference_sha256=_sha(reference),
        story_hook="主播刚解释完为什么被电，话音刚落就暴毙",
        title="【主播】话音刚落小主暴毙",
        image_probe=_probe(
            {
                "lidousha_bbox_frac": [0.42, 0.12, 0.78, 0.89],
                "source_face_complete": True,
                "faithful_crop_can_make_dominant": True,
                "source_carries_story_reaction": True,
                "cpa_redraw_recommended": False,
                "reason": "完整脸与暴毙反应清楚，可忠实裁成大主体",
            }
        ),
    )


def test_1411_source_composition_selects_initial_v2_redraw():
    from src.autoslice import publish_staging
    from src.autoslice.cover_generation import LidoushaCoverArtDirection

    verification = {
        "schema_version": "lidousha-cover-source-composition-verification.v1",
        "status": "PASS",
        "witness_receipt_sha256": "sha256:" + "a" * 64,
        "verdict": {
            "lidousha_bbox_frac": [0.79, 0.70, 0.96, 0.98],
            "source_face_complete": True,
            "faithful_crop_can_make_dominant": False,
            "source_carries_story_reaction": False,
            "cpa_redraw_recommended": True,
            "reason": "主播只在右下角且源图没有承担暴毙反应",
        },
    }
    generation: dict[str, object] = {}
    treatment, route = publish_staging._build_lidousha_cover_route(
        cover_generation=generation,
        story_contract=None,
        title="【主播】刚解释完为什么被电，话音刚落小主就暴毙",
        cover_text="话音刚落\n小主暴毙",
        cover_mode="auto",
        art_direction=LidoushaCoverArtDirection(
            role="shy_cute_default",
            expression_en="shocked",
            background_style="warm-scrapbook-collage",
            layout="banner",
            hook_color="yellow",
            is_song=False,
            cover_punch=("话音刚落", "小主暴毙"),
        ),
        punch_allowed=True,
        frame_selection={
            "status": "SELECTED",
            "best_ms": 30_000,
            "candidates": [{"score": 8.4, "emotion": 1.0}],
            "subject_confident": True,
            "motion_dispersion_frac": 0.2,
        },
        reference_authority=None,
        source_composition_verification=verification,
        enforce_final_host_identity=True,
    )

    assert treatment == "cpa_redraw"
    assert route["schema_version"] == "lidousha-cover-route-decision.v2"
    assert route["selected_treatment"] == "cpa_redraw"
    assert route["source_composition_redraw_recommended"] is True
    assert "source-composition" in route["selected_rationale"]


def test_cpa_identity_bbox_is_the_screenshot_crop_authority(tmp_path):
    from src.autoslice import publish_staging
    from src.autoslice.cover_generation import LidoushaCoverArtDirection

    reference = tmp_path / "reference.png"
    Image.new("RGB", (1920, 1080), (15, 25, 35)).save(reference)
    verification = _safe_verification(reference)
    assert validate_source_composition_verification(
        verification,
        reference_sha256=_sha(reference),
    )
    receipt = tmp_path / "source-composition.json"
    receipt.write_text(
        json.dumps(verification, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _, output, evidence = publish_staging._screenshot_base_and_crop(
        media_path=tmp_path / "motion-must-not-be-read.mp4",
        candidate_id="cpa-bbox-screenshot",
        ai_dir=tmp_path,
        reference_path=reference,
        frame_selection={
            "best_ms": 12_345,
            "subject_confident": False,
            "camera_window_bbox_frac": [0.0, 0.0, 0.1, 0.1],
        },
        art_direction=LidoushaCoverArtDirection(
            role="shy_cute_default",
            expression_en="shocked",
            background_style="cobalt-comic-burst",
            layout="banner",
            hook_color="yellow",
            is_song=False,
            cover_punch=("小主暴毙",),
        ),
        relationship_visual_required=False,
        source_composition_verification=verification,
        source_composition_receipt={"path": str(receipt), "sha256": _sha(receipt)},
    )

    assert output.is_file()
    assert Image.open(output).size == (1920, 1080)
    assert evidence["status"] == "HASH_BOUND_CPA_IDENTITY_CROP"
    assert evidence["source_sha256"] == _sha(reference)
    assert evidence["authority_bbox_frac"] == [0.42, 0.12, 0.78, 0.89]
    assert evidence["motion_bbox_role"] == "CANDIDATE_ONLY_NOT_AUTHORITY"
    assert evidence["source_composition_receipt_sha256"] == _sha(receipt)
    assert evidence["crop_output_sha256"] == _sha(output)


def test_invalid_bbox_or_unavailable_witness_fails_before_image_generation(
    tmp_path, monkeypatch
):
    from src.autoslice import publish_staging
    from tests.test_cover_frame_selection import (
        _write_synthetic_performance_clip,
    )

    reference = tmp_path / "reference.png"
    Image.new("RGB", (1920, 1080), (10, 20, 30)).save(reference)
    invalid = verify_lidousha_source_composition(
        reference_path=reference,
        reference_sha256=_sha(reference),
        story_hook="故事",
        title="标题",
        image_probe=_probe(
            {
                "lidousha_bbox_frac": [0.8, 0.2, 0.3, 0.9],
                "source_face_complete": True,
                "faithful_crop_can_make_dominant": True,
                "source_carries_story_reaction": True,
                "cpa_redraw_recommended": False,
                "reason": "非法反向框",
            }
        ),
    )
    assert invalid["status"] == "FAIL"
    assert invalid["reason_code"] == "SOURCE_COMPOSITION_VERDICT_INVALID"

    media = _write_synthetic_performance_clip(tmp_path)
    monkeypatch.setenv("CPA_BASE_URL", "https://cpa.example.test/v1")
    monkeypatch.setenv("CPA_API_KEY", "test-key")
    calls = 0

    def unavailable_verifier(**_kwargs):
        return {
            "schema_version": (
                "lidousha-cover-source-composition-verification.v1"
            ),
            "status": "FAIL",
            "reason_code": "SOURCE_COMPOSITION_WITNESS_UNAVAILABLE",
        }

    def forbidden_image_edit(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("source witness failure must precede generation")

    result = publish_staging._stage_lidousha_ai_cover(
        {"status": "MATERIALIZED", "media_path": str(media)},
        media_path=media,
        candidate_id="source-composition-unavailable",
        title="【主播】话音刚落小主暴毙",
        cover_text="小主暴毙",
        run_ffmpeg=True,
        image_edit=forbidden_image_edit,
        final_host_identity_verifier=lambda **_kwargs: {},
        source_composition_verifier=unavailable_verifier,
        enforce_final_host_identity=True,
    )
    assert calls == 0
    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert result["reason_codes"] == [
        "COVER_SOURCE_COMPOSITION_UNVERIFIED",
        "SOURCE_COMPOSITION_WITNESS_UNAVAILABLE",
    ]
    assert "route_decision" not in result["cover_generation"]


def test_face_safe_poster_is_1440x810_zero_degree_without_full_width_bar(
    tmp_path,
):
    from src.autoslice.cover_generation import LidoushaCoverArtDirection
    from src.autoslice.cover_screenshot_poster import (
        _compose_screenshot_poster_background,
    )

    source = tmp_path / "crop.png"
    Image.new("RGB", (1920, 1080), (210, 180, 160)).save(source)
    output = tmp_path / "poster.png"
    evidence = _compose_screenshot_poster_background(
        source,
        output,
        art_direction=LidoushaCoverArtDirection(
            role="shy_cute_default",
            expression_en="shocked",
            background_style="cobalt-comic-burst",
            layout="banner",
            hook_color="yellow",
            is_song=False,
            cover_punch=("小主暴毙",),
        ),
        face_safe_contain=True,
    )
    transform = evidence["source_frame_transform"]
    assert evidence["screenshot_card"] == {
        "size": [1440, 810],
        "rotation_degrees": 0.0,
    }
    assert transform["center_4_3_safe"] is True
    assert transform["rendered_content_box"] == [240, 135, 1680, 945]

    # Regression: the old compositor painted x=120..1800 at y=1020..1062 as
    # one solid accent strip.  A multi-sample row must now retain background
    # variation and cannot be a single family-color band.
    with Image.open(output) as rendered:
        row = rendered.crop((120, 1040, 1800, 1041)).convert("RGB")
        colors = [row.getpixel((x, 0)) for x in range(row.width)]
        assert len(set(colors)) > 1
        assert colors.count((255, 204, 45)) < len(colors) * 0.10
        bottom_band = rendered.crop((0, 1000, 1920, 1080)).convert("RGB")
        means = ImageStat.Stat(bottom_band).mean
        assert max(means) - min(means) > 3
