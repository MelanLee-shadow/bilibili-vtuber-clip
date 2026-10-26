"""Meaningful checks for strict admission and shared title rendering."""

from __future__ import annotations

import json
import random
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src.autoslice.cover_generation import LidoushaCoverArtDirection
from src.autoslice.cover_title_reflow import (
    CoverTitleReflowError,
    classify_text_occlusion_admission,
    render_title_reflow,
    title_only_occlusion,
)


ROOT = Path(__file__).resolve().parents[1]
ACTUAL = ROOT / ".codex-tmp/coordinate-resume-validation/overflow-rollout-20261014/academic-cover"


def _verdict() -> dict[str, object]:
    features = {
        "clothing": ["blue collar"],
        "head_appearance": ["white hair"],
        "accessories": ["heart bow"],
    }
    comparisons = {
        key: [
            {
                "source_feature": value[0],
                "status": "MATCH",
                "final_observation": f"{value[0]} remains visible",
            }
        ]
        for key, value in features.items()
    }
    return {
        "source_lidousha_located": True,
        "primary_subject_is_lidousha": True,
        "primary_subject_matches_other_source_participant": False,
        "primary_subject_is_visually_dominant": True,
        "primary_subject_face_is_large_and_clear": False,
        "primary_subject_carries_story_reaction": True,
        "excessive_dead_space": False,
        "meaningless_dominant_decoration": False,
        "thumbnail_has_clear_click_hook": True,
        "identity_conflicts": [],
        "composition_conflicts": ["rendered title text covers the eyes"],
        "reason": "FAIL: rendered title text covers the eyes; all other axes are clear",
        "other_recognizable_people_or_avatars_visible": False,
        "other_recognizable_people_or_avatars": [],
        "source_visible_feature_observations": features,
        "final_feature_matches": {key: True for key in features},
        "final_feature_conflicts": [],
        "final_feature_comparisons": comparisons,
    }


def _verification(**changes: object) -> dict[str, object]:
    verdict = _verdict()
    verdict.update(changes.pop("verdict", {}) or {})
    image_sha = "a" * 64
    row: dict[str, object] = {
        "schema_version": "lidousha-cover-final-host-identity-verification.v6",
        "authority": "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_PROMINENCE_AND_HOST_ONLY_FEATURE_COMPARISON",
        "status": "FAIL",
        "reason_code": "FINAL_COVER_SUBJECT_PROMINENCE_FAILED",
        "host_only_required": True,
        "comparison_sha256": "sha256:" + image_sha,
        "verdict": verdict,
        "witness": {
            "schema_version": "cpa-frame-witness.v1",
            "status": "OBSERVED",
            "provider": "cpa",
            "image_sha256": image_sha,
            "answer": json.dumps(verdict, ensure_ascii=False),
            "routing": {
                "preferred_provider": "cpa",
                "selected_provider": "cpa",
                "fallback_used": False,
            },
        },
    }
    row.update(changes)
    return row


def _direction(
    *, punch: tuple[str, ...] = ("肚子不舒服", "老师却劝我读博"), **changes: object
) -> LidoushaCoverArtDirection:
    direction = LidoushaCoverArtDirection(
        role="shy_cute_default",
        expression_en="soft cute girl-next-door",
        background_style="source-led",
        layout="banner",
        hook_color="yellow",
        is_song=False,
        secondary_fill="pink",
        cover_punch=punch,
        title_style="outline",
    )
    return replace(direction, **changes)


def _write_synthetic(
    path: Path, *, quiet_left: bool = False, noisy_everywhere: bool = False
) -> None:
    if noisy_everywhere:
        image = Image.effect_noise((1920, 1080), 100).convert("RGB")
        image.save(path)
        return
    image = Image.new("RGB", (1920, 1080), (156, 177, 204))
    draw = ImageDraw.Draw(image)
    rng = random.Random(20261015)
    if noisy_everywhere:
        noisy_box = (0, 0, 1920, 1080)
    else:
        noisy_box = (1040, 0, 1920, 1080) if quiet_left else (0, 0, 960, 1080)
    x0, y0, x1, y1 = noisy_box
    for _ in range(10000):
        x = rng.randrange(x0, x1)
        y = rng.randrange(y0, y1)
        colour = (rng.randrange(20, 100), rng.randrange(25, 120), rng.randrange(40, 150))
        draw.rectangle((x, y, x + 5, y + 5), fill=colour)
    image.save(path)


def test_actual_failed_member_is_admitted_only_for_explicit_text_occlusion() -> None:
    path = ACTUAL / "original-generation.json"
    if not path.is_file():
        pytest.skip("retained academic-cover member is unavailable")
    generation = json.loads(path.read_text())
    result = classify_text_occlusion_admission(generation)
    assert result["admitted"] is True
    assert result["reason_code"] == "COVER_TITLE_REFLOW_TEXT_OCCLUSION_ONLY"
    assert title_only_occlusion(generation) is True


def test_actual_background_member_uses_shared_renderer_when_retained(tmp_path: Path) -> None:
    generation_path = ACTUAL / "original-generation.json"
    background = ACTUAL / "background.png"
    if not generation_path.is_file() or not background.is_file():
        pytest.skip("retained academic-cover member is unavailable")
    generation = json.loads(generation_path.read_text())["generation"]
    direction = LidoushaCoverArtDirection(**generation["art_direction"])
    result = render_title_reflow(
        background,
        tmp_path / "actual-successor.png",
        cover_text=generation["cover_text"],
        art_direction=direction,
    )
    assert result["font_size"] >= 120
    assert result["rendered_lines"] == generation["cover_punch"]
    assert result["rendered_text_pixels"]["status"] == "PASS"
    assert (
        result["title_reflow_proposal"]["background_sha256"] == generation["ai_background_sha256"]
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("primary_subject_is_lidousha", False, "COVER_TITLE_REFLOW_SEMANTIC_AXIS_FAILED"),
        ("primary_subject_is_visually_dominant", False, "COVER_TITLE_REFLOW_SEMANTIC_AXIS_FAILED"),
        (
            "primary_subject_face_is_large_and_clear",
            True,
            "COVER_TITLE_REFLOW_FACE_FAILURE_REQUIRED",
        ),
    ],
)
def test_identity_small_subject_and_conflict_controls_are_rejected(
    field: str, value: bool, reason: str
) -> None:
    result = classify_text_occlusion_admission(_verification(verdict={field: value}))
    assert result["admitted"] is False
    assert result["reason_code"] == reason


def test_non_text_unknown_provider_and_raw_answer_controls_are_rejected() -> None:
    non_text = _verification(
        verdict={"composition_conflicts": ["主体太小"], "reason": "the subject is too small"}
    )
    assert (
        classify_text_occlusion_admission(non_text)["reason_code"]
        == "COVER_TITLE_REFLOW_NON_TEXT_COMPOSITION_FAILURE"
    )
    mixed = _verification(
        verdict={
            "composition_conflicts": ["rendered title text covers the eyes and the face is cropped"]
        }
    )
    assert not title_only_occlusion(mixed)
    noun_only = _verification(
        verdict={"composition_conflicts": ["this cover has title text but the face is too small"]}
    )
    assert not title_only_occlusion(noun_only)
    unknown = _verification(
        witness={
            "schema_version": "cpa-frame-witness.v1",
            "status": "OBSERVED",
            "provider": "unknown",
            "image_sha256": "a" * 64,
            "answer": json.dumps(_verdict()),
        }
    )
    assert (
        classify_text_occlusion_admission(unknown)["reason_code"]
        == "COVER_TITLE_REFLOW_PROVIDER_UNAVAILABLE"
    )
    changed = _verification()
    changed["witness"] = dict(changed["witness"])
    changed["witness"]["answer"] = json.dumps({**_verdict(), "reason": "changed"})
    assert (
        classify_text_occlusion_admission(changed)["reason_code"]
        == "COVER_TITLE_REFLOW_VERDICT_BINDING_MISMATCH"
    )
    empty_features = _verification(
        verdict={
            "source_visible_feature_observations": {
                "clothing": [],
                "head_appearance": [],
                "accessories": [],
            },
            "final_feature_comparisons": {
                "clothing": [],
                "head_appearance": [],
                "accessories": [],
            },
        }
    )
    assert (
        classify_text_occlusion_admission(empty_features)["reason_code"]
        == "COVER_TITLE_REFLOW_FEATURE_COMPARISON_FAILED"
    )


def test_reverse_subject_position_chooses_pixel_quiet_left_region(tmp_path: Path) -> None:
    background = tmp_path / "reverse.png"
    _write_synthetic(background, quiet_left=True)
    output = tmp_path / "reverse-final.png"
    result = render_title_reflow(
        background,
        output,
        cover_text="肚子不舒服却被老师劝读博",
        art_direction=_direction(),
    )
    zone = result["title_reflow_proposal"]["selected_zone"]
    assert zone[2] <= 1040
    assert result["font_size"] >= 120
    assert result["rendered_lines"] == ["肚子不舒服", "老师却劝我读博"]
    assert result["rendered_text_pixels"]["status"] == "PASS"


def test_real_renderer_metadata_and_background_bytes_are_preserved(tmp_path: Path) -> None:
    background = tmp_path / "background.png"
    _write_synthetic(background)
    before = background.read_bytes()
    output = tmp_path / "final.png"
    result = render_title_reflow(
        background,
        output,
        cover_text="肚子不舒服却被老师劝读博",
        art_direction=_direction(),
    )
    assert background.read_bytes() == before
    assert output.read_bytes() != before
    assert result["pre_overlay_sha256"].startswith("sha256:")
    with Image.open(result["pre_overlay_path"]) as pre, Image.open(background) as source:
        assert pre.convert("RGB").tobytes() == source.convert("RGB").tobytes()
    assert result["rendered_text_pixels"]["status"] == "PASS"
    assert result["title_reflow_proposal"]["background_bytes_preserved"] is True
    assert result["title_reflow_proposal"]["font_and_rotation_authority"].endswith(
        "_overlay_cover_title"
    )


def test_no_fitting_area_fails_before_final_render(tmp_path: Path) -> None:
    background = tmp_path / "busy.png"
    _write_synthetic(background, noisy_everywhere=True)
    output = tmp_path / "should-not-exist.png"
    with pytest.raises(CoverTitleReflowError) as error:
        render_title_reflow(
            background, output, cover_text="肚子不舒服却被老师劝读博", art_direction=_direction()
        )
    assert error.value.reason_code == "COVER_TITLE_REFLOW_NO_SAFE_FITTING_ZONE"
    assert not output.exists()


def test_existing_renderer_keeps_full_text_gate_and_short_text() -> None:
    # A short full-text title is allowed by the ordinary renderer; the reflow
    # lane cannot silently change it into a punch or bypass its line contract.
    direction = _direction(punch=())
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        background = Path(directory) / "background.png"
        _write_synthetic(background)
        result = render_title_reflow(
            background, Path(directory) / "full.png", cover_text="短标题", art_direction=direction
        )
    assert result["cover_text_mode"] == "full"
    assert "短标题" in "".join(result["rendered_lines"])


@pytest.mark.parametrize(
    "damage",
    [None, "final", "background", "pre_overlay", "mask", "reference", "comparison", "font_proof"],
)
def test_reflow_binding_replays_real_glyph_evidence_and_refuses_drift(tmp_path, damage):
    from src.autoslice.cover_generation import _overlay_cover_title
    from src.autoslice.cover_title_reflow_binding import reflow_title_occlusion
    from src.autoslice.cover_host_identity_gate import _build_comparison
    from src.autoslice.cover_title_rendering import sha256_file

    background, final = tmp_path / "background.png", tmp_path / "old-final.png"
    _write_synthetic(background)
    direction = _direction()
    text = "肚子不舒服却被老师劝读博"
    overlay = _overlay_cover_title(
        background, final, cover_text=text, art_direction=direction
    )
    reference, comparison = tmp_path / "reference.png", tmp_path / "comparison.png"
    Image.new("RGB", (1920, 1080), "blue").save(reference)
    _build_comparison(reference_path=reference, final_cover_path=final, output_path=comparison)
    # Semantic answer is an explicitly offline fixture; all glyph/image/file
    # consumers below are real, including the resolved font-choice wrapper.
    verification = _verification()
    verification.update(
        final_cover_path=str(final),
        final_cover_sha256=sha256_file(final),
        reference_path=str(reference),
        reference_sha256=sha256_file(reference),
        comparison_path=str(comparison),
        comparison_sha256=sha256_file(comparison),
    )
    verification["witness"]["image_sha256"] = sha256_file(comparison).removeprefix("sha256:")
    generation = {
        **overlay,
        "art_direction": asdict(direction),
        "cover_text": text,
        "ai_background": str(background),
        "ai_background_sha256": sha256_file(background),
        "final_cover": str(final),
        "final_cover_sha256": sha256_file(final),
        "final_host_identity_verification": verification,
    }
    output = tmp_path / "new-final.png"
    paths = {
        "final": final,
        "background": background,
        "reference": reference,
        "comparison": comparison,
        "pre_overlay": Path(overlay["pre_overlay_path"]),
        "mask": Path(overlay["rendered_text_pixels"]["mask_path"]),
    }
    if damage == "font_proof":
        generation["rendered_text_pixels"]["font_file_sha256"] = "sha256:" + "f" * 64
    elif damage:
        paths[damage].write_bytes(b"changed original bytes")
    if damage:
        with pytest.raises(ValueError, match="COVER_TITLE_REFLOW_"):
            reflow_title_occlusion(generation, output_path=output)
        assert not output.exists()
    else:
        assert reflow_title_occlusion(generation, output_path=output)
        assert generation["final_cover_sha256"] == sha256_file(output)
        assert generation["final_host_identity_verification"]["status"] == "PENDING"
        assert generation["title_occlusion_reflow"]["previous"][
            "final_cover_sha256"
        ] == sha256_file(final)
