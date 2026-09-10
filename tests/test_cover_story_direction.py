"""Regression checks for story direction surviving prompt and title rendering."""
from dataclasses import replace
from pathlib import Path

from PIL import Image
import pytest

from src.autoslice.cover_generation import (
    ROOT,
    LidoushaCoverArtDirection,
    _cover_prompt,
    _normalize_cover_art_direction,
    _overlay_cover_title,
    _punch_layout_override,
    _COVER_LAYOUT_RENDER,
)
from src.autoslice.cover_text_pixel_evidence import verify_rendered_text_pixel_artifacts
from src.autoslice.cover_story_direction import refresh_visual_brief


BASE = LidoushaCoverArtDirection(
    role="contrast", expression_en="gentle then noisy", background_style="mono-manga-panels",
    layout="right-split", hook_color="red", is_song=False,
    cover_punch=("中午温柔", "晚上吵闹"),
)


@pytest.mark.parametrize("layout", ["banner", "footer", "left-split", "right-split"])
def test_screenshot_choices_do_not_require_an_ai_brief(layout):
    direction = _normalize_cover_art_direction(
        {"layout": layout, "title_style": "clean", "hook_color": "pink",
         "background_style": "a quiet warm scene"}, BASE, "中午温柔晚上吵闹",
    )
    assert (direction.layout, direction.title_style, direction.hook_color) == (layout, "clean", "pink")
    assert direction.background_style == "a quiet warm scene"
    assert direction.visual_brief == ""
    song = _normalize_cover_art_direction(
        {"layout": layout, "title_style": "clean", "background_style": "noisy"},
        replace(BASE, is_song=True, layout="song-clean", background_style="soft-radial"), "《歌名》",
    )
    assert (song.layout, song.title_style, song.background_style) == ("song-clean", "outline", "soft-radial")


@pytest.mark.parametrize("brief", ["", "A quiet scene that frames the same official sticker, with a clear title on the side."])
def test_no_brief_and_emote_prompts_keep_identity_without_forced_poster_style(brief):
    from src.autoslice.cover_emote import EmoteEntry

    direction = replace(BASE, background_style="source-led", visual_brief=brief)
    emote = EmoteEntry("1", "test", "lidousha", "x.png", "hd.png", "0" * 64, "", "", "")
    for chosen_emote in (None, emote):
        prompt = _cover_prompt(
            title="中午温柔晚上吵闹", cover_text="中午温柔晚上吵闹",
            art_direction=replace(direction, emote_mode="replace" if chosen_emote else ""),
            emote=chosen_emote,
        )
        assert "x=260..960" in prompt
        assert "VERY LARGE" not in prompt
        assert "clean white sticker" not in prompt
        assert "cobalt-and-navy" not in prompt
        assert "never an unreferenced alternate skin" in prompt
        if chosen_emote:
            assert "EXACT pose, expression" in prompt
            assert "take precedence" in prompt
        else:
            assert "PRESERVE THE EXACT OUTFIT" in prompt


@pytest.mark.parametrize("layout", ["banner", "footer", "left-split", "right-split"])
@pytest.mark.parametrize("title_style", ["clean", "outline"])
def test_source_frame_survives_title_and_center_crop_with_no_overlap(tmp_path, layout, title_style):
    from PIL import ImageOps
    from src.autoslice.cover_generation import _sha256
    from src.autoslice.cover_route_evidence import build_no_crop_participant_verification
    from src.autoslice.cover_polish_gate import _compose_screenshot_cover_with_face_gate

    source, output, final = (tmp_path / name for name in ("source.png", "poster.png", "cover.png"))
    original = Image.new("RGB", (1920, 1080), (180, 150, 120))
    original.paste((240, 70, 30), (110, 60, 340, 820))
    original.paste((40, 100, 210), (1600, 40, 1900, 1020))
    original.save(source)
    direction = replace(BASE, background_style="source-led", layout=layout, title_style=title_style)
    poster, overlay, _ = _compose_screenshot_cover_with_face_gate(
        overlay_source=source, crop_evidence={"full_frame_preserved": True},
        candidate_id="sample", ai_dir=tmp_path / "backgrounds", covers_dir=tmp_path,
        cover_text="中午温柔\n晚上吵闹", art_direction=direction,
        relationship_visual_required=True, method="screenshot_direct", base_url="", api_key="",
    )
    final = tmp_path / "sample.screenshot-title.cover.png"
    transform = poster["source_frame_transform"]
    box = transform["rendered_content_box"]
    expected = original.resize((box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS)
    assert Image.open(final).crop(box).tobytes() == expected.tobytes()
    assert transform["center_4_3_safe"] is True
    assert poster["palette"] == {}
    assert (box[2] - box[0]) * (box[3] - box[1]) >= 1920 * 1080 * 0.5
    selected = "footer" if "split" in layout else layout
    assert overlay["layout"] == overlay["art_direction"]["layout"] == selected
    assert poster["layout_resolution"]["selected_layout"] == selected
    assert overlay["font_size"] >= 120
    proof = build_no_crop_participant_verification({
        "route_decision": {"source_visible_participant_ids": ["host", "guest"]},
        "reference_sha256": "sha256:" + _sha256(source),
        "final_cover_sha256": "sha256:" + _sha256(final),
        "screenshot_graphic_poster": poster,
        "rendered_text_pixels": overlay["rendered_text_pixels"],
    })
    assert proof["status"] == "PASS"
    assert proof["title_occludes_source"] is False
    evidence = overlay["rendered_text_pixels"]
    assert verify_rendered_text_pixel_artifacts(
        evidence, final_cover_path=final, pre_overlay_path=Path(evidence["pre_overlay_path"]),
        mask_path=Path(evidence["mask_path"]), font_path=ROOT / "assets/lidousha/fonts" / overlay["font"],
        expected_pre_overlay_sha256=evidence["pre_overlay_sha256"],
    )


def test_source_geometry_retains_side_only_when_it_keeps_more_source_pixels(tmp_path):
    from src.autoslice.cover_screenshot_poster import _source_frame_layout

    # This is a geometry-unit input; the independent production route still
    # rejects vertical reference frames before the screenshot compositor.
    source = tmp_path / "tall-crop.png"
    Image.new("RGB", (700, 1200), (100, 130, 140)).save(source)
    direction = replace(BASE, background_style="source-led", title_style="clean")
    resolved, zone, decision = _source_frame_layout(source, direction)
    assert resolved.layout == "right-split"
    assert zone == _COVER_LAYOUT_RENDER["right-split"]["zone"]
    assert decision["selected_layout"] == "right-split"
    legacy = replace(direction, background_style="cobalt-comic-burst")
    assert _source_frame_layout(source, legacy) == (legacy, None, None)


def test_short_punch_preserves_story_layout_but_long_punch_gets_wide_zone():
    assert _punch_layout_override(BASE).layout == "right-split"
    long = replace(BASE, cover_punch=("一个主播竟能看到俩",))
    assert _punch_layout_override(long).layout == "banner"


def test_normalized_direction_reaches_image_prompt_with_matching_title_zone():
    brief = "Two temporal panels of the same reference character: a gentle pose then a noisy pose."
    direction = _normalize_cover_art_direction(
        {"layout": "footer", "visual_brief": brief, "title_style": "clean"},
        BASE, "中午温柔晚上吵闹",
    )
    direction = _punch_layout_override(direction)
    prompt = _cover_prompt(title="中午温柔晚上吵闹", cover_text="中午温柔晚上吵闹", art_direction=direction)
    assert direction.layout == "footer"
    assert brief in prompt
    assert "y=730..1060" in prompt
    assert "VERY LARGE head-and-shoulders" not in prompt
    assert "PRESERVE THE EXACT OUTFIT" in prompt
    assert "do not imply a second real participant" in prompt
    assert '["中午温柔", "晚上吵闹"]' in prompt
    assert "bold 16:9" not in prompt
    fallback = _normalize_cover_art_direction(
        {"layout": "unsupported", "title_style": "arbitrary", "visual_brief": "x"}, BASE, "中午温柔晚上吵闹"
    )
    assert fallback.layout == BASE.layout
    assert fallback.title_style == "outline"
    assert fallback.visual_brief == ""


def test_semantic_copy_change_replaces_stale_brief_before_image_prompt():
    import json
    from src.autoslice.cover_generation import _cover_art_direction
    from src.autoslice.cover_punch_semantics import SCHEMA_VERSION

    text = "推一个V能看到俩，中午温柔晚上吵闹"
    original = "The same person has two styles; a calm portrait fills the right of the picture."
    revised = "Two temporal panels of the same reference person, with gentle noon and noisy nighttime expressions."
    responses = iter([
        {"layout": "footer", "visual_brief": original, "title_style": "clean",
         "cover_punch": {"main": "推一个V", "sub": "能看到俩"},
         "words": ["推一个V", "能看到俩，", "中午", "温柔", "晚上", "吵闹"]},
        {"schema_version": SCHEMA_VERSION, "status": "REVISE",
         "final_punch": {"main": "中午温柔", "sub": "晚上吵闹"},
         "stranger_can_infer_event": True, "contains_concrete_subject": True,
         "contains_action_or_conflict": True, "no_fabricated_fact": True,
         "story_summary": "同一个主播中午温柔晚上却变得吵闹。", "click_motivation": "同一个人的两种状态形成反差。"},
        {"visual_brief": revised},
    ])
    prompts = []

    def llm(prompt):
        prompts.append(prompt)
        return json.dumps(next(responses), ensure_ascii=False)

    direction = _cover_art_direction(
        candidate_id="copy-change", title=text, cover_text=text, story_hook=text,
        art_direction_llm_call=llm, allow_punch=True,
    )
    assert len(prompts) == 3
    assert direction.cover_punch == ("中午温柔", "晚上吵闹")
    assert direction.visual_brief == revised
    prompt = _cover_prompt(title=text, cover_text=text, art_direction=direction)
    assert revised in prompt and original not in prompt
    assert '["中午温柔", "晚上吵闹"]' in prompt


def test_cosmetic_copy_change_reuses_art_but_failed_refresh_drops_old_brief():
    direction = replace(BASE, visual_brief="A complete, already reviewed cinematic composition for this selected story.")
    def unexpected(_prompt):
        pytest.fail("Cosmetic copy changes must not invoke another art call")
    same = refresh_visual_brief(
        direction, original_punch=("中午温柔！", "晚上吵闹。"), title="", story_hook="", llm_call=unexpected,
    )
    assert same is direction
    failed = refresh_visual_brief(
        direction, original_punch=("推一个V", "能看到俩"), title="", story_hook="", llm_call=lambda _: "{}",
    )
    assert failed.visual_brief == ""
    assert failed.cover_punch == direction.cover_punch


@pytest.mark.parametrize("layout,style", [("right-split", "clean"), ("footer", "outline"), ("banner", "clean")])
def test_real_fonts_replay_both_styles_and_reject_changed_pixels(tmp_path: Path, layout: str, style: str):
    bg = tmp_path / "background.png"
    final = tmp_path / "cover.png"
    Image.new("RGB", (1920, 1080), (25, 35, 30)).save(bg)
    receipt = _overlay_cover_title(
        ai_background_path=bg, final_cover_path=final, cover_text="中午温柔\n晚上吵闹",
        art_direction=replace(BASE, layout=layout, title_style=style),
    )
    assert receipt["layout"] == layout
    assert receipt["font_size"] >= 120
    assert receipt["rendered_lines"] == ["中午温柔", "晚上吵闹"]
    assert receipt["font_selection"]["glyph_risk"] == []
    if style == "clean":
        assert receipt["font"] == "SmileySans-Oblique.ttf"
    evidence = receipt["rendered_text_pixels"]
    box = evidence["text_pixel_bbox"]
    assert 260 <= box[0] < box[2] <= 1660
    assert 0 <= box[1] < box[3] <= 1080
    zone = _COVER_LAYOUT_RENDER[layout]["zone"]
    assert zone[0] <= box[0] < box[2] <= zone[2]
    assert zone[1] <= box[1] < box[3] <= zone[3]
    args = dict(
        final_cover_path=final, pre_overlay_path=Path(evidence["pre_overlay_path"]),
        mask_path=Path(evidence["mask_path"]), font_path=ROOT / "assets/lidousha/fonts" / receipt["font"],
        expected_pre_overlay_sha256=evidence["pre_overlay_sha256"],
    )
    assert verify_rendered_text_pixel_artifacts(evidence, **args)
    altered = Image.open(final).convert("RGB")
    altered.putpixel((960, 540), (255, 0, 255))
    altered.save(final)
    assert not verify_rendered_text_pixel_artifacts(evidence, **args)


def test_rotated_two_line_title_fits_reserved_vertical_zone(tmp_path, monkeypatch):
    # The Qixi fixed-title repair previously produced y=632..1072 outside its
    # y=640..1070 reservation after the second line was made more readable.
    zone = (260, 640, 1660, 1070)
    monkeypatch.setitem(_COVER_LAYOUT_RENDER, "banner", {**_COVER_LAYOUT_RENDER["banner"], "zone": zone})
    bg, final = tmp_path / "background.png", tmp_path / "cover.png"
    Image.new("RGB", (1920, 1080), (210, 120, 120)).save(bg)
    receipt = _overlay_cover_title(
        bg, final, cover_text="小李有女友感吗？宿敌是否有点亲密了",
        art_direction=replace(BASE, layout="banner", hook_color="purple",
                              cover_punch=("女友感各论各的", "宿敌却有点亲密")),
    )
    left, top, right, bottom = receipt["rendered_text_pixels"]["text_pixel_bbox"]
    assert zone[0] <= left < right <= zone[2]
    assert zone[1] <= top < bottom <= zone[3]
    assert receipt["font_size"] >= 120


def test_usable_calm_source_prefers_direct_even_without_all_story_actions():
    from src.autoslice.publish_staging import _decide_cover_treatment
    verdict = {"verdict": {"source_face_complete": True,
        "faithful_crop_can_make_dominant": True, "lidousha_bbox_frac": [0.28, 0, 0.85, 1],
        "source_carries_story_reaction": False, "cpa_redraw_recommended": True}}
    frame = {"candidates": [{"score": 3.9421}], "subject_confident": False,
             "motion_dispersion_frac": 0.5489}
    args = dict(cover_mode="auto", is_song=False, punch_allowed=True,
                frame_selection=frame, source_composition_verification=verdict)
    assert _decide_cover_treatment(**args)[0] == "screenshot_direct"
    frame.update(status="OPERATOR_OVERRIDE", candidates=[{"score": None}])
    assert _decide_cover_treatment(**args)[0] == "screenshot_direct"
    verdict["verdict"]["source_face_complete"] = False
    assert _decide_cover_treatment(**args)[0] == "cpa_redraw"


def test_clean_screenshot_preserves_source_face_pixels_and_relationship_branch(tmp_path):
    from src.autoslice.cover_screenshot_poster import _compose_screenshot_poster_background
    source, output = tmp_path / "source.png", tmp_path / "poster.png"
    original = Image.new("RGB", (1920, 1080), (180, 150, 120))
    original.putpixel((960, 440), (220, 30, 40)); original.save(source)
    direction = replace(BASE, layout="footer", title_style="clean",
        visual_brief="Keep the actual smiling source frame and place clear editorial copy below the face.")
    evidence = _compose_screenshot_poster_background(source, output, art_direction=direction)
    with Image.open(output) as rendered:
        assert rendered.crop((0, 0, 1920, 640)).tobytes() == original.crop((0, 0, 1920, 640)).tobytes()
        assert rendered.getpixel((960, 1000)) != original.getpixel((960, 1000))
    assert evidence["source_frame_transform"]["ai_modified"] is False
    relationship = _compose_screenshot_poster_background(source, output,
        art_direction=direction, preserve_full_frame=True)
    assert relationship.get("composition") != "source_frame_with_footer"
    assert relationship["source_frame_transform"]["center_4_3_safe"] is True
