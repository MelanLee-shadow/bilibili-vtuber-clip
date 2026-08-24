import json
from pathlib import Path

from src.autoslice.cover_text_pixel_evidence import verify_rendered_text_pixel_artifacts
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.title_policy import publish_title_policy_violations


ROOT = Path(__file__).resolve().parents[1] / "private_fastlane/c10_auto_143025_868_1094"


def test_c10_private_projection_preserves_watched_screen_lyrics_live_text_and_arigatou() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    source = parse_srt_cues((ROOT / package["subtitle_scope"]["pipeline_diagnostic"]).read_text(encoding="utf-8"))
    release = parse_srt_cues((ROOT / package["subtitle_scope"]["release_srt"]).read_text(encoding="utf-8"))
    watched = set(package["subtitle_scope"]["watched_song_authority"]["cue_ordinals"])
    assert watched
    assert len(release) == len(source) + len(package["subtitle_scope"]["concurrent_live_overlays"])
    assert all(cue.text.strip() for cue in release)
    assert "今天进社社社社团交朋友" in [cue.text for cue in release]
    assert "旁边还站着一只狗" in [cue.text for cue in release]
    assert source[12].text == release[12].text  # cue 13 remains outer/live text
    assert "谢谢你 arigatou" in [cue.text for cue in release]
    assert all("林克多" not in cue.text for cue in release)


def test_c10_private_title_and_cover_are_auditable_but_not_public_authority() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert publish_title_policy_violations(package["title"], lane="talk") == []
    assert package["status"] == "PRIVATE_REVIEW_READY_NOT_FORMAL_REPLAY"
    assert package["hard_blockers"]
    evidence = json.loads((ROOT / "cover/rendered-text-pixels.v4.json").read_text(encoding="utf-8"))
    assert evidence["rendered_text"] == "前辈的改词应援歌"
    assert verify_rendered_text_pixel_artifacts(
        evidence,
        final_cover_path=ROOT / "cover/auto_143025_868_1094.private.cover-v3.png",
        pre_overlay_path=ROOT / "cover/auto_143025_868_1094.private.cover-v3.pre-overlay.png",
        mask_path=ROOT / "cover/auto_143025_868_1094.private.cover-v3.title-mask.png",
        font_path=ROOT.parents[1] / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf",
        expected_pre_overlay_sha256=evidence["pre_overlay_sha256"],
    )


def test_c10_private_hash_and_player_clock_authority_fail_closed() -> None:
    evidence = json.loads((ROOT / "evidence/watched-screen-authority.json").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert evidence["recut"]["sha256"] == package["source_media"]["sha256"]
    watched = package["subtitle_scope"]["watched_song_authority"]
    assert evidence["watched_video"]["bv"] == watched["bv"]
    assert evidence["watched_video"]["sha256"] == watched["sha256"]
    clock = evidence["player_clock_reconciliation"]
    assert clock["ivan_label"] == "03:02"
    assert clock["release_grid_cue"] == 73
    assert clock["pretrim_clock_offset_seconds"] == 7
    assert clock["bound_text"] == package["subtitle_scope"]["exact_replacement"]["text"]
