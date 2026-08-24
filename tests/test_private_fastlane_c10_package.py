import json
from pathlib import Path

from src.autoslice.cover_text_pixel_evidence import verify_rendered_text_pixel_artifacts
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.title_policy import publish_title_policy_violations


ROOT = Path(__file__).resolve().parents[1] / "private_fastlane/c10_auto_143025_868_1094"


def test_c10_private_projection_keeps_only_live_speech_and_exact_arigatou() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    source = parse_srt_cues((ROOT / package["subtitle_scope"]["pipeline_diagnostic"]).read_text(encoding="utf-8"))
    release = parse_srt_cues((ROOT / package["subtitle_scope"]["release_srt"]).read_text(encoding="utf-8"))
    dropped = set(package["subtitle_scope"]["dropped_source_cues"])
    kept_source = [cue for ordinal, cue in enumerate(source, start=1) if ordinal not in dropped]
    assert [(cue.start_ms, cue.end_ms) for cue in release] == [(cue.start_ms, cue.end_ms) for cue in kept_source]
    assert "谢谢你 arigatou" in [cue.text for cue in release]
    assert all("林克多" not in cue.text for cue in release)


def test_c10_private_title_and_cover_are_auditable_but_not_public_authority() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert publish_title_policy_violations(package["title"], lane="talk") == []
    assert package["status"] == "PRIVATE_READY_NOT_FORMAL_REPLAY"
    assert package["hard_blockers"]
    evidence = json.loads((ROOT / "cover/rendered-text-pixels.v3.json").read_text(encoding="utf-8"))
    assert evidence["rendered_text"] == "前辈写的现在写不出"
    assert verify_rendered_text_pixel_artifacts(
        evidence,
        final_cover_path=ROOT / "cover/auto_143025_868_1094.private.cover-v2.png",
        pre_overlay_path=ROOT / "cover/auto_143025_868_1094.private.cover-v2.pre-overlay.png",
        mask_path=ROOT / "cover/auto_143025_868_1094.private.cover-v2.title-mask.png",
        font_path=ROOT.parents[1] / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf",
        expected_pre_overlay_sha256=evidence["pre_overlay_sha256"],
    )
