"""Fixed Qixi cover lanes must not inherit the generic redraw fallback."""

from pathlib import Path

from src.autoslice import publish_staging
from src.autoslice.cover_generation import LidoushaCoverArtDirection
from src.autoslice.fixed_cover_stage import FixedCoverStageOptions


def _direction() -> LidoushaCoverArtDirection:
    return LidoushaCoverArtDirection(
        role="shy_cute_default",
        expression_en="shocked",
        background_style="cobalt-comic-burst",
        layout="banner",
        hook_color="yellow",
        is_song=False,
        cover_punch=("有女友感吗？",),
    )


def test_fixed_screenshot_direct_lane_never_demotes_to_images_edit(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed direct composition is blocked, even when redraw is available."""

    reference = tmp_path / "reference.png"
    reference.write_bytes(b"reference")
    monkeypatch.setattr(
        publish_staging,
        "_prepare_lidousha_cover_reference",
        lambda *_args, **_kwargs: (reference, {"best_ms": 0}, None, None),
    )
    monkeypatch.setattr(
        publish_staging,
        "_cover_art_direction",
        lambda **_kwargs: _direction(),
    )
    monkeypatch.setattr(
        publish_staging,
        "_build_lidousha_cover_route",
        lambda **_kwargs: (
            "screenshot_direct",
            {"host_identity_required": False, "required_participant_ids": []},
        ),
    )
    blocked = {
        "status": "COVER_BLOCKED",
        "reason_codes": ["SCREENSHOT_ROUTE_MATERIALIZATION_FAILED"],
    }
    monkeypatch.setattr(
        publish_staging, "_stage_screenshot_direct_cover", lambda **_kwargs: blocked
    )
    monkeypatch.setattr(
        publish_staging, "_enforce_final_talk_cover_thumbnail_gate", lambda value: value
    )

    def redraw_must_not_run(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("fixed screenshot lane attempted images.edit")

    monkeypatch.setattr(publish_staging, "_stage_cpa_redraw_cover", redraw_must_not_run)
    result = publish_staging._stage_ai_cover(
        {"story_contract": {"selection_hook": "关系梗"}},
        media_path=tmp_path / "clip.mp4",
        candidate_id="auto_123655_771_844",
        title="【李豆沙】小李有女友感吗？宿敌是否有点亲密了",
        cover_text="小李有女友感吗？宿敌是否有点亲密了",
        run_ffmpeg=True,
        fixed_options=FixedCoverStageOptions(
            cover_mode_override="screenshot", require_screenshot_direct=True,
        ),
        image_edit=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("image edit must not run")
        ),
    )

    assert result is blocked


def test_default_stage_behavior_keeps_environment_route_compatibility(tmp_path: Path) -> None:
    """The new options are opt-in; this assertion pins their default values."""

    assert publish_staging._stage_ai_cover.__kwdefaults__["fixed_options"] == FixedCoverStageOptions()
