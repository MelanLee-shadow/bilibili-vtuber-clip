import hashlib
import json

import pytest

from scripts import regenerate_channel_cover as regen
from src.autoslice import cover_host_identity_gate as identity
from src.autoslice.cover_repair import _enrich_repaired_cover_generation
from src.autoslice.story_contract import COVER_BINDING_REQUIRED_KEYS


def test_song_regeneration_passes_song_scene_to_final_witness(tmp_path, monkeypatch):
    observed = {}
    monkeypatch.setenv("CPA_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "fixture-not-a-credential")

    def image(**kwargs):
        observed["prompt"] = kwargs["prompt"]
        kwargs["output_path"].write_bytes(b"unit-fixture-background")
        return {"status": "AI_BACKGROUND_READY"}

    def overlay(background, output, **kwargs):
        output.write_bytes(b"unit-fixture-final")
        return {}

    def witness(**kwargs):
        observed["scene"] = kwargs["scene_kind"]
        return {"status": "UNIT_FIXTURE_NOT_RELEASE_AUTHORITY"}

    monkeypatch.setattr(regen, "_call_cpa_image_edit", image)
    monkeypatch.setattr(regen, "_overlay_cover_title", overlay)
    monkeypatch.setattr(identity, "verify_final_host_identity", witness)
    monkeypatch.setattr(identity, "validate_final_host_identity_verification", lambda meta: True)
    reference = tmp_path / "ref.png"
    reference.write_bytes(b"unit-fixture-reference")
    meta = regen.regenerate_cover(
        title="【李豆沙】豆沙歌，《world.execute(me);》", cover_text="《world.execute(me);》",
        candidate_id="song", out_path=tmp_path / "cover.png", reference_path=reference, use_llm=False,
    )
    assert meta["art_direction"]["is_song"] is True
    assert observed["scene"] == "song"
    assert "KEEP IT WORN" in observed["prompt"]
    assert "invent the hidden face" in observed["prompt"]
    assert "historical persona descriptions" in observed["prompt"]


def test_regeneration_revision_brief_reaches_images_edit_and_metadata(tmp_path, monkeypatch):
    observed = {}
    monkeypatch.setenv("CPA_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "fixture-not-a-credential")

    def image(**kwargs):
        observed["prompt"] = kwargs["prompt"]
        observed["style_reference_paths"] = kwargs.get("style_reference_paths")
        kwargs["request_path"].write_text("{}", encoding="utf-8")
        kwargs["response_path"].write_text("{}", encoding="utf-8")
        kwargs["output_path"].write_bytes(b"unit-fixture-background")
        return {"status": "AI_BACKGROUND_READY"}

    def overlay(background, output, **kwargs):
        output.write_bytes(b"unit-fixture-final")
        return {}

    def witness(**_kwargs):
        return {"status": "UNIT_FIXTURE_NOT_RELEASE_AUTHORITY"}

    monkeypatch.setattr(regen, "_call_cpa_image_edit", image)
    monkeypatch.setattr(regen, "_overlay_cover_title", overlay)
    monkeypatch.setattr(identity, "verify_final_host_identity", witness)
    monkeypatch.setattr(identity, "validate_final_host_identity_verification", lambda meta: True)
    reference = tmp_path / "ref.png"
    reference.write_bytes(b"unit-fixture-reference")
    style_reference = tmp_path / "style.png"
    style_reference.write_bytes(b"unit-fixture-distinct-style")
    brief = "Do not show a tipped bottle or scattered tablets; preserve the approved meeting story."

    meta = regen.regenerate_cover(
        title="【李豆沙】meeting cover",
        cover_text="meeting cover",
        candidate_id="meeting-v02",
        out_path=tmp_path / "cover.png",
        reference_path=reference,
        style_reference_path=style_reference,
        use_llm=False,
        revision_brief=brief,
    )

    assert brief in observed["prompt"]
    assert observed["style_reference_paths"] == (style_reference,)
    assert meta["art_direction_style"]["reference_image"] == {
        "asset_key": "cover_style_reference", "path": str(style_reference),
        "sha256": "sha256:" + hashlib.sha256(style_reference.read_bytes()).hexdigest(),
        "role": "STYLE_ONLY",
    }
    assert "takes precedence over any conflicting optional scene_props" in observed["prompt"]
    assert meta["revision_brief"] == brief
    assert meta["revision_brief_sha256"] == (
        "sha256:" + hashlib.sha256(brief.encode("utf-8")).hexdigest()
    )


@pytest.mark.parametrize("mode,host_only", [("HOST_ONLY_GENERIC", True), ("VERIFIED_DUAL_STREAM_FRAME", False)])
def test_regeneration_contract_reaches_image_and_final_witness(tmp_path, monkeypatch, mode, host_only):
    """Generation and final-pixel observation must agree before package binding."""
    observed = {}
    monkeypatch.setenv("CPA_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "fixture-not-a-credential")
    monkeypatch.setattr(regen, "_cover_prompt", lambda **kwargs: "scene")

    def image(**kwargs):
        observed["prompt"] = kwargs["prompt"]
        kwargs["output_path"].write_bytes(b"unit-fixture-background")
        return {"status": "AI_BACKGROUND_READY"}

    def overlay(background, output, **kwargs):
        output.write_bytes(b"unit-fixture-final")
        return {}

    def witness(**kwargs):
        observed["host_only"] = kwargs["host_only_required"]
        return {"status": "UNIT_FIXTURE_NOT_RELEASE_AUTHORITY"}

    monkeypatch.setattr(regen, "_call_cpa_image_edit", image)
    monkeypatch.setattr(regen, "_overlay_cover_title", overlay)
    monkeypatch.setattr(identity, "verify_final_host_identity", witness)
    monkeypatch.setattr(identity, "validate_final_host_identity_verification", lambda meta: True)
    reference = tmp_path / "ref.png"
    reference.write_bytes(b"unit-fixture-reference")
    contract = {"candidate_id": "clip", "cover_fallback_mode": mode}
    meta = regen.regenerate_cover(
        title="【李豆沙】面试沉默", cover_text="面试沉默", candidate_id="clip",
        out_path=tmp_path / "cover.png", reference_path=reference, use_llm=False,
        story_contract=contract,
    )
    assert observed["host_only"] is host_only
    assert ("human silhouettes" in observed["prompt"]) is host_only
    assert ("before/after depictions" in observed["prompt"]) is host_only
    assert meta["story_contract"] == contract
    assert meta["story_contract"] is not contract


def test_package_contract_is_resolved_before_any_generation(tmp_path, monkeypatch):
    contract = {key: "bound" for key in COVER_BINDING_REQUIRED_KEYS}
    contract.update(candidate_id="clip", cover_fallback_mode="HOST_ONLY_GENERIC")
    publish = {
        "schema_version": "shadow-publish-draft.v1", "candidate_id": "clip",
        "title": "【李豆沙】面试沉默", "upload_enabled": False,
        "cover_generation": {"story_contract": contract},
    }
    (tmp_path / "clip.publish.json").write_text(json.dumps(publish))
    (tmp_path / "clip.record.json").write_text(json.dumps({"story_contract": contract}))
    observed = {}

    def generate(**kwargs):
        observed.update(kwargs)
        raise RuntimeError("unit fixture stops before providers")

    monkeypatch.setattr(regen, "regenerate_cover", generate)
    with pytest.raises(RuntimeError, match="stops before providers"):
        regen.main([
            "--title", publish["title"], "--candidate-id", "clip",
            "--out", str(tmp_path / "cover.png"), "--bind-package", str(tmp_path),
        ])
    assert observed["story_contract"] == contract
    with pytest.raises(SystemExit, match="CANDIDATE_TITLE_MISMATCH"):
        regen.main([
            "--title", "【李豆沙】another", "--candidate-id", "clip",
            "--out", str(tmp_path / "cover.png"), "--bind-package", str(tmp_path),
        ])


def test_regeneration_cli_passes_revision_brief(tmp_path, monkeypatch):
    observed = {}

    def generate(**kwargs):
        observed.update(kwargs)
        raise RuntimeError("unit fixture stops before providers")

    monkeypatch.setattr(regen, "regenerate_cover", generate)
    brief = "Do not show a tipped bottle or scattered tablets."
    with pytest.raises(RuntimeError, match="stops before providers"):
        regen.main([
            "--title", "【李豆沙】meeting cover",
            "--candidate-id", "clip",
            "--out", str(tmp_path / "cover.png"),
            "--revision-brief", brief,
            "--style-ref", str(tmp_path / "style.png"),
        ])
    assert observed["revision_brief"] == brief
    assert observed["style_reference_path"] == tmp_path / "style.png"


def test_cli_consumes_complete_current_body_story_and_research_before_provider(tmp_path, monkeypatch):
    text = "1\n00:00:00,000 --> 00:00:01,000\n铺垫\n\n2\n00:00:01,000 --> 00:00:02,000\n最后的真实回应\n"
    srt = tmp_path / "final.srt"
    srt.write_text(text)
    story = {"candidate_id": "clip", "selection_hook": "当前完整故事", "scene_kind": "talk"}
    research = {"candidate_id": "clip", "applicable": False, "reason": "当前故事不适用"}
    story_path, research_path = tmp_path / "story.json", tmp_path / "research.json"
    story_path.write_text(json.dumps(story))
    research_path.write_text(json.dumps(research))
    observed = {}
    def generate(**kwargs):
        observed.update(kwargs)
        raise RuntimeError("stopped before providers")
    monkeypatch.setattr(regen, "regenerate_cover", generate)
    args = ["--title", "【李豆沙】当前题", "--candidate-id", "clip", "--out", str(tmp_path / "cover.png"),
            "--final-srt", str(srt), "--story-contract", str(story_path), "--editorial-research", str(research_path)]
    with pytest.raises(RuntimeError, match="before providers"):
        regen.main(args)
    assert observed["final_srt_text"] == text
    assert observed["story_contract"] == story
    assert observed["story_hook"] == story["selection_hook"]
    assert observed["research_story_context"]["editorial_research"] == research
    story["candidate_id"] = "different"
    story_path.write_text(json.dumps(story))
    observed.clear()
    with pytest.raises(SystemExit, match="CANDIDATE_MISMATCH"):
        regen.main(args)
    assert observed == {}


def test_cli_refuses_body_drift_before_generated_result_can_bind(tmp_path, monkeypatch):
    srt = tmp_path / "final.srt"
    srt.write_text("current body")
    def generate(**kwargs):
        srt.write_text("next body")
        return {}
    monkeypatch.setattr(regen, "regenerate_cover", generate)
    with pytest.raises(ValueError, match="CURRENT_INPUT_DRIFT"):
        regen.main(["--title", "当前题", "--candidate-id", "clip", "--out", str(tmp_path / "cover.png"), "--final-srt", str(srt)])
    assert not (tmp_path / "cover.cover_generation.json").exists()


def test_cli_explicit_body_cannot_override_bound_package_body(tmp_path, monkeypatch):
    publish = {"schema_version": "shadow-publish-draft.v1", "candidate_id": "clip", "title": "当前题", "upload_enabled": False}
    (tmp_path / "clip.publish.json").write_text(json.dumps(publish))
    (tmp_path / "clip.srt").write_text("package current body")
    explicit = tmp_path / "old.srt"
    explicit.write_text("old body")
    calls = []
    monkeypatch.setattr(regen, "regenerate_cover", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(SystemExit, match="SUBTITLE_PACKAGE_CONFLICT"):
        regen.main(["--title", "当前题", "--candidate-id", "clip", "--out", str(tmp_path / "cover.png"),
                    "--bind-package", str(tmp_path), "--final-srt", str(explicit)])
    assert calls == []


def test_explicit_missing_style_reference_stops_before_image_request(tmp_path, monkeypatch):
    monkeypatch.setenv("CPA_BASE_URL", "https://fixture.invalid/v1")
    monkeypatch.setenv("CPA_API_KEY", "fixture-not-a-credential")
    reference = tmp_path / "ref.png"
    reference.write_bytes(b"unit-fixture-reference")
    calls = []
    monkeypatch.setattr(regen, "_call_cpa_image_edit", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(SystemExit, match="COVER_STYLE_REFERENCE_UNAVAILABLE"):
        regen.regenerate_cover(
            title="【李豆沙】meeting cover", cover_text="meeting cover", candidate_id="clip",
            out_path=tmp_path / "cover.png", reference_path=reference,
            style_reference_path=tmp_path / "missing.png", use_llm=False,
        )
    assert calls == []
    assert not (tmp_path / "cover.png").exists()


def test_binding_rejects_changed_contract_before_rewriting_generation(tmp_path):
    old = {key: "old" for key in COVER_BINDING_REQUIRED_KEYS}
    new = {**old, "cover_fallback_mode": "HOST_ONLY_GENERIC"}
    path = tmp_path / "generation.json"
    path.write_text("immutable old evidence")
    with pytest.raises(ValueError, match="CHANGED_SINCE_GENERATION"):
        _enrich_repaired_cover_generation(
            generation={"method": "images.edit", "story_contract": old},
            generation_path=path, documents=[(tmp_path / "record.json", {"story_contract": new})],
            title="【李豆沙】面试沉默",
        )
    assert path.read_text() == "immutable old evidence"
