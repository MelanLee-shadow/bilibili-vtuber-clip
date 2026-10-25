from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from src.autoslice import publish_staging
from src.autoslice.cover_generation import LidoushaCoverArtDirection
from src.autoslice.cover_source_composition import verify_source_composition


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("receipt_drift", [False, True])
def test_host_only_redraw_uses_exact_host_pixels_not_adjacent_character(
    tmp_path, receipt_drift
):
    reference = tmp_path / "reference.png"
    image = Image.new("RGB", (1920, 1080), "yellow")
    ImageDraw.Draw(image).rectangle((1482, 612, 1855, 1079), fill="white")
    image.save(reference)

    def probe(path, _question, **_kwargs):
        return {
            "status": "OBSERVED", "provider": "cpa",
            "routing": {
                "preferred_provider": "cpa", "selected_provider": "cpa",
                "fallback_used": False,
            },
            "image_sha256": sha(path).removeprefix("sha256:"),
            "answer": json.dumps({
                "lidousha_bbox_frac": [0.772, 0.567, 0.966, 1.0],
                "source_face_complete": True,
                "faithful_crop_can_make_dominant": True,
                "source_carries_story_reaction": True,
                "cpa_redraw_recommended": False,
                "reason": "当前主播小窗，旁边游戏角色不属于她",
            }),
        }

    composition = verify_source_composition(
        reference_path=reference, reference_sha256=sha(reference),
        story_hook="评论游戏台词", title="游戏里的闺蜜", image_probe=probe,
    )
    receipt_path = tmp_path / "composition.json"
    receipt_path.write_text(json.dumps(composition), encoding="utf-8")
    generation = {
        "source_composition_verification": composition,
        "source_composition_receipt": {"path": str(receipt_path), "sha256": sha(receipt_path)},
        "reference_selection": {"best_ms": 60500},
        "route_decision": {"scene_kind": "talk"},
    }
    if receipt_drift:
        receipt_path.write_text("{}", encoding="utf-8")
    called = []

    def image_edit(**kwargs):
        called.append(kwargs)
        cropped = Image.open(kwargs["reference_path"])
        assert cropped.size == (373, 468)
        assert cropped.getextrema() == ((255, 255), (255, 255), (255, 255))
        raise RuntimeError("TEST_DISPATCH_STOP")

    for name in ("refs", "ai", "covers", "evidence"):
        (tmp_path / name).mkdir()
    result = publish_staging._stage_cpa_redraw_cover(
        candidate_id="current-host", title="游戏里的闺蜜", cover_text="帮你涂口红",
        story_contract={"cover_fallback_mode": "HOST_ONLY_GENERIC"},
        cover_refs_dir=tmp_path / "refs", ai_dir=tmp_path / "ai",
        covers_dir=tmp_path / "covers", evidence_dir=tmp_path / "evidence",
        reference_path=reference, emote_library=None,
        art_direction=LidoushaCoverArtDirection(
            role="shy_cute_default", expression_en="surprised",
            background_style="cobalt-comic-burst", layout="banner",
            hook_color="yellow", is_song=False,
        ),
        cover_generation=generation, image_edit=image_edit,
        final_participant_verifier=None, final_host_identity_verifier=None,
        base_url="https://example.invalid/v1", api_key="test",
    )
    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    if receipt_drift:
        assert not called
        assert result["reason_codes"] == ["CPA_REDRAW_IDENTITY_REFERENCE_INVALID"]
    else:
        assert len(called) == 1, result
        transfer = generation["redraw_identity_reference"]
        assert transfer["frame_ms"] == 60500
        assert transfer["source_sha256"] == sha(reference)
        assert transfer["crop_output_sha256"] == sha(called[0]["reference_path"])
        assert transfer["crop_box"] == [1482, 612, 1855, 1080]
        assert transfer["crop_strategy"] == "EXACT_IDENTITY_BBOX_REDRAW_REFERENCE"
