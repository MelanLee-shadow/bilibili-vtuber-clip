"""New approved text is permitted; unrelated source changes remain rejected."""
import copy
import json
from pathlib import Path

import pytest

from src.autoslice.cover_generation import LidoushaCoverArtDirection, _overlay_cover_title
from src.autoslice.cover_punch_semantics import (
    REQUIRED_REVIEW_BOOLEANS, SCHEMA_VERSION, review_cover_punch_semantics,
)
from src.autoslice.cover_repair_route_lineage import validate_screenshot_route_authority
from src.autoslice.screenshot_punch_successor import prepare_successor, validate_successor
from src.autoslice.story_contract import COVER_BINDING_REQUIRED_KEYS
from tests.lidousha.test_screenshot_direct_title_repair import _direct_fixture


@pytest.fixture
def successor(tmp_path, monkeypatch):
    fx = _direct_fixture(tmp_path, monkeypatch)
    parent = fx["generation"]
    hook = "主播先说修复前的旧文案，随后改成修复后的封面文案。"
    parent["story_contract"] = {key: None for key in COVER_BINDING_REQUIRED_KEYS}
    parent["story_contract"].update(schema_version="lidousha-story-contract.v1", selection_hook=hook)
    record = json.loads(fx["source_record"].read_text())
    record["publish_staging"]["cover_generation"] = copy.deepcopy(parent)
    record["story_contract"] = copy.deepcopy(parent["story_contract"])
    fx["source_record"].write_text(json.dumps(record))
    lines = ("修复前的旧文案", "换成新文案了")
    # Synthetic model response is confined to this test fixture.
    response = {"schema_version": SCHEMA_VERSION, "status": "PASS", "final_punch": {"main": lines[0], "sub": lines[1]},
                "story_summary": "主播把旧文案换成了新文案", "click_motivation": "观众想知道新文案有哪些变化",
                **{key: True for key in REQUIRED_REVIEW_BOOLEANS}}
    _, proof = review_cover_punch_semantics(
        title=fx["title"], cover_text=parent["cover_text"], story_hook=hook,
        punch=lines, llm_call=lambda _: json.dumps(response))
    assert proof["status"] == "PASS"
    candidate = tmp_path / "candidate.png"
    overlay = _overlay_cover_title(
        Path(parent["ai_background"]), candidate, cover_text=parent["cover_text"],
        art_direction=LidoushaCoverArtDirection(**{**parent["art_direction"], "cover_punch": lines}))
    semantic = tmp_path / "semantic.json"
    semantic.write_text(json.dumps({"proof": proof, "candidate_id": fx["cid"]}))
    render = tmp_path / "render.json"
    render.write_text(json.dumps(overlay["rendered_text_pixels"]))
    face = copy.deepcopy(parent["polish_face_verification"])
    face["witness"]["image_sha256"] = fx["digest"](candidate).removeprefix("sha256:")
    host = copy.deepcopy(parent["final_host_identity_verification"])
    host["final_cover_sha256"] = fx["digest"](candidate)
    face_path, host_path = tmp_path / "face.json", tmp_path / "host.json"
    face_path.write_text(json.dumps(face))
    host_path.write_text(json.dumps(host))
    target = tmp_path / "successor" / "final.cover.png"
    target.parent.mkdir()
    generation = prepare_successor(
        candidate_id=fx["cid"], source_record=fx["source_record"], cover=candidate,
        reference=Path(parent["reference_image"]), semantic_result=semantic,
        render_receipt=render, face_receipt=face_path, host_receipt=host_path, out=target)
    return generation, parent, record, fx


def test_new_words_bind_to_the_active_parent(successor):
    generation, parent, record, fx = successor
    validate_screenshot_route_authority(generation, title=fx["title"],
                                        documents=[(fx["source_record"], record)])
    assert generation["cover_punch"] != parent["cover_punch"]
    assert generation["ai_background_sha256"] == parent["ai_background_sha256"]
    assert generation["image_generation_used"] is False


@pytest.mark.parametrize("tamper", ["title", "background", "frame", "design", "semantic", "pixels", "missing_preimage", "wrong_active_parent"])
def test_successor_rejects_drift(successor, tamper):
    generation, parent, _, _ = successor
    if tamper == "title":
        generation["title"] = "another title"
    elif tamper == "background":
        generation["ai_background_sha256"] = "sha256:" + "0" * 64
    elif tamper == "frame":
        generation["screenshot_frame"]["frame_ms"] += 1
    elif tamper == "design":
        generation["art_direction"]["layout"] = "footer"
    elif tamper == "semantic":
        generation["art_direction"]["cover_punch_semantic_review"]["status"] = "FAILED"
    elif tamper == "pixels":
        Path(generation["final_cover"]).write_bytes(b"changed")
    elif tamper == "missing_preimage":
        Path(generation["approved_punch_successor"]["parent_record_path"]).unlink()
    else:
        parent = copy.deepcopy(parent)
        parent["screenshot_frame"]["frame_ms"] += 1
    with pytest.raises((ValueError, OSError)):
        validate_successor(generation, active_parent=parent)
