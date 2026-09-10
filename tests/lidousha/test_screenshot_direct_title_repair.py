"""Title-only direct screenshot repairs retain their real source and no-AI route."""

import copy
import json

import pytest

import scripts.session_autoslice as runner
from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
)
from tests.cover_binding_test_support import _screenshot_polish_binding_fixture


def _direct_fixture(tmp_path, monkeypatch):
    # Reuse the renderer/transaction fixture, but model a direct production
    # generation from the start. This is synthetic test data, never a migration.
    fx = _screenshot_polish_binding_fixture(tmp_path, monkeypatch)
    generation = json.loads(fx["generation_path"].read_text())
    generation.update(
        method="screenshot_direct", model="none", image_gen_model="none",
        attempted_models=[], model_fallback_used=False,
        cover_origin="SOURCE_SCREENSHOT", image_generation_planned=False,
        image_generation_attempted=False, image_generation_used=False,
    )
    generation["screenshot_graphic_poster"]["source_frame_transform"]["ai_modified"] = False
    generation["route_decision"] = build_cover_route_decision(
        selected_treatment="screenshot_direct",
        selected_rationale="verified original screenshot; title-only layout repair",
        story_contract=None, reference_authority=None, title=fx["title"],
        cover_text=generation["cover_text"],
        decision_inputs={"cover_mode": "screenshot", "subject_confident": True,
                         "verified_stream_frame": True},
    )
    generation["route_decision"]["host_identity_required"] = True
    record_cover_route_execution(
        generation, actual_treatment="screenshot_direct", execution_status="READY",
        image_generation_attempted=False, image_generation_used=False,
    )
    fx["generation_path"].write_text(json.dumps(generation))
    for path in (fx["delivery_record"], fx["source_record"], fx["publish_path"]):
        document = json.loads(path.read_text())
        surface = document if path == fx["publish_path"] else document["publish_staging"]
        surface["cover_generation"] = copy.deepcopy(generation)
        path.write_text(json.dumps(document))
    fx["generation"] = generation
    return fx


def test_direct_title_only_binding_and_crash_recovery(tmp_path, monkeypatch):
    fx = _direct_fixture(tmp_path, monkeypatch)
    old_state = copy.deepcopy(fx["rec"])
    background = fx["generation"]["ai_background_sha256"]
    runner._bind_repaired_cover(
        fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
    )
    assert runner._cover_binding_valid(fx["date"], fx["rec"], fx["mp4"], fx["cover"])
    generation = fx["rec"]["cover_generation"]
    assert generation["method"] == "screenshot_direct"
    assert generation["image_generation_used"] is False
    assert generation["image_gen_model"] == "none"
    assert generation["ai_background_sha256"] == background
    assert runner._recover_committed_cover_binding(
        fx["date"], old_state, fx["mp4"], fx["cover"]
    )
    assert runner._cover_binding_valid(fx["date"], old_state, fx["mp4"], fx["cover"])


@pytest.mark.parametrize("tamper", [
    "background", "source_transform", "model", "route", "reference",
    "frame_time", "face", "host", "mask", "title", "art_direction",
])
def test_direct_repair_rejects_drift_before_binding(tmp_path, monkeypatch, tamper):
    fx = _direct_fixture(tmp_path, monkeypatch)
    generation = copy.deepcopy(fx["generation"])
    if tamper == "background":
        # Even a valid new image/hash pair cannot replace a frozen direct backdrop.
        replacement = tmp_path / "unapproved-background.png"
        replacement.write_bytes(b"other background")
        generation.update(ai_background=str(replacement),
                          ai_background_sha256=fx["digest"](replacement))
    elif tamper == "source_transform":
        generation["screenshot_graphic_poster"]["source_frame_transform"]["ai_modified"] = True
    elif tamper == "model":
        generation["model"] = "gpt-image-2"
    elif tamper == "route":
        generation["route_decision"]["actual_treatment"] = "screenshot_polish"
    elif tamper == "reference":
        generation["reference_sha256"] = "sha256:" + "0" * 64
    elif tamper == "frame_time":
        generation["screenshot_frame"]["frame_ms"] += 1
    elif tamper == "face":
        generation["polish_face_verification"]["status"] = "FAIL"
    elif tamper == "host":
        generation["final_host_identity_verification"]["status"] = "FAIL"
    elif tamper == "mask":
        generation["rendered_text_pixels"]["mask_sha256"] = "sha256:" + "0" * 64
    elif tamper == "title":
        generation["title"] = "not the approved title"
    else:
        generation["art_direction"]["cover_punch"] = ["unreviewed copy"]
    fx["generation_path"].write_text(json.dumps(generation))
    before = {p: p.read_bytes() for p in
              (fx["delivery_record"], fx["source_record"], fx["publish_path"])}
    with pytest.raises(ValueError):
        runner._bind_repaired_cover(
            fx["date"], fx["rec"], fx["mp4"], fx["cover"], fx["generated_cover"]
        )
    assert all(p.read_bytes() == data for p, data in before.items())
    assert not fx["generated_cover"].with_suffix(".cover-binding.json").exists()


def test_direct_cli_moves_title_without_recomposing_source(tmp_path, monkeypatch):
    import sys

    from scripts import repair_screenshot_cover as cli
    from src.autoslice.cover_repair_route_lineage import validate_cover_generation_for_binding

    fx = _direct_fixture(tmp_path, monkeypatch)
    generation = fx["generation"]
    record = json.loads(fx["delivery_record"].read_text())
    record["publish_staging"]["publish_json_path"] = ""
    record_path = tmp_path / "direct.record.json"
    record_path.write_text(json.dumps(record))
    out = tmp_path / "direct-repair.png"
    exclusion = {
        "schema_version": "lidousha-cover-identity-landmark-title-exclusion.v1",
        "landmark": "lidousha_panda_ears",
        "background_sha256": generation["ai_background_sha256"],
        "protected_bbox": [260, 0, 1660, 700],
        "title_zone": [300, 760, 1620, 1070],
    }
    exclusion_path = tmp_path / "title-zone.json"
    exclusion_path.write_text(json.dumps(exclusion))
    calls = []

    def face(path, **_kwargs):
        calls.append("face")
        result = copy.deepcopy(generation["polish_face_verification"])
        result["witness"]["image_sha256"] = fx["digest"](path).removeprefix("sha256:")
        return result

    def host(**kwargs):
        calls.append("host")
        result = copy.deepcopy(generation["final_host_identity_verification"])
        result["final_cover_sha256"] = kwargs["final_cover_sha256"]
        return result

    monkeypatch.setattr(cli, "_verify_polish_face_integrity", face)
    monkeypatch.setattr(cli, "verify_final_host_identity", host)
    monkeypatch.setattr(
        cli, "_compose_screenshot_poster_background",
        lambda *_a, **_k: pytest.fail("direct title repair must not recompose its frozen source"),
    )
    monkeypatch.setattr(sys, "argv", [
        "repair_screenshot_cover.py", "--record", str(record_path),
        "--candidate-id", fx["cid"], "--out", str(out),
        "--polished", str(fx["generated_cover"].with_name("screenshot-polished.png")),
        "--identity-landmark-title-exclusion", str(exclusion_path),
    ])
    assert cli.main() == 0
    repaired, _ = validate_cover_generation_for_binding(
        cover=out, title=fx["title"], candidate_id=fx["cid"],
        documents=[(record_path, record)],
    )
    assert calls == ["face", "host"]
    assert repaired["image_generation_used"] is False
    assert repaired["image_generation_attempted"] is False
    assert repaired["screenshot_graphic_poster"] == generation["screenshot_graphic_poster"]
    assert repaired["ai_background_sha256"] == generation["ai_background_sha256"]
    assert repaired["rendered_text_pixels"]["text_pixel_bbox"][1] >= 760


def test_active_binding_refuses_wrong_generation_root_before_mutation(tmp_path, monkeypatch):
    fx = _direct_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "diagnostic-only" / "final.cover.png"
    outside.parent.mkdir()
    outside.write_bytes(fx["generated_cover"].read_bytes())
    generation = copy.deepcopy(fx["generation"])
    generation["final_cover"] = str(outside)
    outside.with_suffix(".cover_generation.json").write_text(json.dumps(generation))
    before = {p: p.read_bytes() for p in
              (fx["delivery_record"], fx["source_record"], fx["publish_path"])}
    with pytest.raises(ValueError, match="generation root"):
        runner._bind_repaired_cover(fx["date"], fx["rec"], fx["mp4"], fx["cover"], outside)
    assert all(p.read_bytes() == data for p, data in before.items())
    assert not outside.with_suffix(".cover-binding.json").exists()
