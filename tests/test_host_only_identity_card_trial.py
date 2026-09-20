from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from src.autoslice import host_only_identity_card_trial as trial
from src.autoslice.cover_generation import (
    LidoushaCoverArtDirection,
    _overlay_cover_title,
)
from src.autoslice.cover_screenshot_poster import (
    _compose_screenshot_poster_background,
)
from src.autoslice.cover_source_composition import (
    IDENTITY_CARD_CROP_STRATEGY,
    verify_source_composition,
)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _probe(verdict: dict[str, object]):
    def probe(image_path: Path, _question: str, **_kwargs):
        return {
            "status": "OBSERVED",
            "provider": "cpa",
            "image_path": str(Path(image_path)),
            "image_sha256": hashlib.sha256(Path(image_path).read_bytes()).hexdigest(),
            "answer": json.dumps(verdict, ensure_ascii=False),
            "routing": {
                "preferred_provider": "cpa",
                "selected_provider": "cpa",
                "fallback_used": False,
            },
        }

    return probe


def _write_package(tmp_path: Path, *, source_led: bool = False) -> dict[str, object]:
    candidate = "auto_010203_4_5"
    source = tmp_path / "source-package"
    source.mkdir()
    (source / "cover_refs").mkdir()
    (source / "evidence").mkdir()
    (source / "covers_ai_original").mkdir()
    (source / "covers").mkdir()

    reference = source / "cover_refs" / f"{candidate}.cover-ref.png"
    image = Image.new("RGB", (1920, 1080), (18, 42, 78))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 379, 1079), fill=(240, 0, 0))
    draw.rectangle((490, 0, 1545, 1079), fill=(20, 210, 90))
    image.save(reference)
    verification = verify_source_composition(
        reference_path=reference,
        reference_sha256=_sha(reference),
        story_hook="主播讲述自己被内定的故事",
        title="【主播】老板是我亲戚",
        image_probe=_probe(
            {
                "lidousha_bbox_frac": [0.255, 0.0, 0.805, 1.0],
                "source_face_complete": True,
                "faithful_crop_can_make_dominant": True,
                "source_carries_story_reaction": True,
                "cpa_redraw_recommended": False,
                "reason": "主播完整但左侧聊天栏必须排除",
            }
        ),
    )
    receipt = source / "evidence" / f"{candidate}.cover-source-composition-verification.json"
    receipt.write_bytes(_json_bytes(verification))
    inline_verification = copy.deepcopy(verification)
    relocated_reference = f"/relocated/package/cover_refs/{reference.name}"
    inline_verification["reference_path"] = relocated_reference
    inline_witness = inline_verification["witness"]
    assert isinstance(inline_witness, dict)
    inline_witness["image_path"] = relocated_reference

    old_base = source / "covers_ai_original" / f"{candidate}.screenshot-base.png"
    Image.open(reference).convert("RGB").save(old_base)
    direction = LidoushaCoverArtDirection(
        role="shy_cute_default",
        expression_en="shocked",
        background_style="source-led" if source_led else "warm-scrapbook-collage",
        layout="banner",
        hook_color="yellow",
        is_song=False,
        cover_punch=("老板是我亲戚！", "我被内定了"),
    )
    old_poster = source / "covers_ai_original" / f"{candidate}.screenshot-poster.png"
    poster_evidence = _compose_screenshot_poster_background(
        old_base,
        old_poster,
        art_direction=direction,
        source_ai_modified=False,
        face_safe_contain=True,
        **({"source_title_zone": (260, 0, 1660, 298)} if source_led else {}),
    )
    exclusion = {
        "schema_version": "lidousha-cover-identity-landmark-title-exclusion.v1",
        "landmark": "lidousha_panda_ears",
        "background_sha256": _sha(old_poster),
        "protected_bbox": [590, 135, 1410, 500],
        "title_zone": [260, 560, 1660, 1060],
    }
    old_cover = source / "covers" / f"{candidate}.screenshot-title.cover.png"
    overlay = _overlay_cover_title(
        old_poster,
        old_cover,
        cover_text="老板是我亲戚！\n我被内定了",
        art_direction=direction,
        identity_landmark_title_exclusion=None if source_led else exclusion,
    )
    generation = {
        "candidate_id": candidate,
        "status": "AI_COVER_READY",
        "title": "【主播】老板是我亲戚",
        "cover_text": "老板是我亲戚！\n我被内定了",
        "method": "screenshot_direct",
        "model": "none",
        "image_gen_model": "none",
        "cover_origin": "SOURCE_SCREENSHOT",
        "image_generation_planned": False,
        "image_generation_attempted": False,
        "image_generation_used": False,
        "art_direction": dataclasses.asdict(direction),
        "story_contract": {"cover_fallback_mode": "HOST_ONLY_GENERIC"},
        "route_decision": {
            "schema_version": "lidousha-cover-route-decision.v2",
            "image_generation_planned": False,
            "image_generation_attempted": False,
            "image_generation_used": False,
            "execution_status": "BLOCKED",
            "actual_treatment": None,
        },
        "source_composition_verification": inline_verification,
        "source_composition_receipt": {
            "path": str(receipt),
            "sha256": _sha(receipt),
        },
        "reference_image": str(reference),
        "reference_sha256": _sha(reference),
        "screenshot_frame": {
            "schema": "cover-frame-transfer.v2",
            "status": "HASH_BOUND_CPA_IDENTITY_CROP",
            "frame_ms": 12_345,
            "source_path": str(reference),
            "source_sha256": _sha(reference),
            "reference_sha256": _sha(reference),
            "crop_applied": True,
            "crop_box": [0, 0, 1920, 1080],
            "source_size": [1920, 1080],
            "output_size": [1920, 1080],
            "camera_window_crop": True,
            "authority_identity_crop": True,
            "authority_bbox_frac": [0.255, 0.0, 0.805, 1.0],
            "crop_output_sha256": _sha(old_base),
        },
        "screenshot_graphic_poster": poster_evidence,
        "ai_background": str(old_poster),
        "ai_background_sha256": _sha(old_poster),
        "final_cover": str(old_cover),
        "final_cover_sha256": _sha(old_cover),
        "final_host_identity_verification": {
            "schema_version": "lidousha-cover-final-host-identity-verification.v4",
            "status": "FAIL",
            "reason_code": "NON_HOST_PERSON_VISIBLE",
        },
        **overlay,
    }
    publish = {"cover_generation": generation, "title": generation["title"]}
    evidence = {"publish_staging": {"cover_generation": generation}}
    record = {"publish_staging": {"cover_generation": generation}}
    publish_path = source / f"{candidate}.publish.json"
    evidence_path = source / f"{candidate}.record.json"
    record_path = source / f"{candidate}.delivery.record.json"
    publish_path.write_bytes(_json_bytes(publish))
    evidence_path.write_bytes(_json_bytes(evidence))
    record_path.write_bytes(_json_bytes(record))
    manifest = {
        "items": [
            {
                "candidate_id": candidate,
                "cover": old_cover.relative_to(source).as_posix(),
                "evidence_json": evidence_path.name,
                "record": record_path.name,
                "publish_json": publish_path.name,
            }
        ]
    }
    (source / "review_manifest.json").write_bytes(_json_bytes(manifest))
    return {
        "candidate": candidate,
        "source": source,
        "old_cover": old_cover,
        "publish": publish_path,
        "evidence": evidence_path,
        "record": record_path,
    }


def _patch_current_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        trial,
        "host_only_identity_route_blocker_detail",
        lambda _generation: "final pixels contain non-host avatar",
    )
    monkeypatch.setattr(
        trial,
        "host_only_visual_safety_evidence",
        lambda _story, *, scene_kind: {
            "schema_version": "lidousha-cover-host-only-visual-safety.v1",
            "status": "REQUIRED",
            "scene_kind": scene_kind,
            "cover_fallback_mode": "HOST_ONLY_GENERIC",
            "forbidden_visual_classes": ["NON_HOST_PERSON"],
            "requirement_basis": ["STORY_CONTRACT_HOST_ONLY_FALLBACK"],
        },
    )


def _private_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    return parent


def test_trial_rebuilds_full_frame_degeneracy_without_sidebar_or_source_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _write_package(tmp_path)
    _patch_current_contract(monkeypatch)
    source = fixture["source"]
    assert isinstance(source, Path)
    source_before = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    destination = _private_parent(tmp_path) / "trial"

    result = trial.build_host_only_identity_card_trial(
        source_package=source,
        destination=destination,
        candidate_id=str(fixture["candidate"]),
    )

    assert result["status"] == "PASS_PIXELS_READY_WITNESS_REQUIRED"
    assert result["crop_evidence"]["crop_strategy"] == IDENTITY_CARD_CROP_STRATEGY
    assert result["crop_evidence"]["full_frame_degeneracy_avoided"] is True
    recovery = result["source_composition_recovery"]
    assert recovery["status"] == "BOUND_RECEIPT_CURRENT"
    assert recovery["inline_current_validator"] is False
    assert recovery["differing_json_pointers"] == [
        "/reference_path",
        "/witness/image_path",
    ]
    assert recovery["authority_source"] == ("HASH_BOUND_RECEIPT_WITH_RELOCATED_INLINE_LOCATORS")
    assert result["provider_calls"] == 0
    assert result["image_generation_calls"] == 0
    assert result["package_writes"] == 0
    assert result["production_state_writes"] == 0
    assert result["upload_calls"] == 0
    assert result["upload_allowed"] is False
    assert result["source_preimage_unchanged"] is True
    assert {"evidence_record", "delivery_record", "publish_draft"} <= set(result["source_inputs"])
    assert result["cover_pixels_changed"] is True
    assert source_before == {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }

    base = Image.open(destination / "identity-card.base.png").convert("RGB")
    colors = base.getcolors(maxcolors=base.width * base.height)
    assert colors is not None
    assert not any(red > 220 and green < 20 and blue < 20 for _count, (red, green, blue) in colors)
    assert any(green > 150 and red < 80 and blue < 140 for _count, (red, green, blue) in colors)
    generation = json.loads((destination / "identity-card.cover_generation.json").read_text())
    assert generation["status"] == "BLOCKED_HOST_ONLY_V4_WITNESS_REQUIRED"
    assert "final_host_identity_verification" not in generation
    binding = generation["identity_card_pixel_successor"]
    assert binding["provider_calls"] == 0
    assert binding["source_composition_recovery"] == recovery
    receipt = json.loads(Path(generation["source_composition_receipt"]["path"]).read_text())
    assert generation["source_composition_verification"] == receipt
    exclusion = generation["identity_landmark_title_exclusion"]
    assert exclusion["background_sha256"] == generation["ai_background_sha256"]
    protected = exclusion["protected_bbox"]
    text = exclusion["text_pixel_bbox"]
    assert (
        text[2] <= protected[0]
        or protected[2] <= text[0]
        or text[3] <= protected[1]
        or protected[3] <= text[1]
    )
    assert (destination / "TRIAL-RESULT.json").is_file()
    assert not (destination / "FAILURE.json").exists()


def test_missing_title_exclusion_stops_after_safe_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _write_package(tmp_path)
    _patch_current_contract(monkeypatch)
    source = fixture["source"]
    assert isinstance(source, Path)
    for key in ("publish", "evidence", "record"):
        path = fixture[key]
        assert isinstance(path, Path)
        document = json.loads(path.read_text())
        generation = (
            document["cover_generation"]
            if key == "publish"
            else document["publish_staging"]["cover_generation"]
        )
        generation.pop("identity_landmark_title_exclusion", None)
        path.write_bytes(_json_bytes(document))
    source_before = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    destination = _private_parent(tmp_path) / "trial"

    result = trial.build_host_only_identity_card_trial(
        source_package=source,
        destination=destination,
        candidate_id=str(fixture["candidate"]),
    )

    assert result["status"] == ("PASS_BACKGROUND_READY_TITLE_EXCLUSION_AUTHORITY_REQUIRED")
    assert result["crop_evidence"]["crop_strategy"] == (trial.IDENTITY_CARD_CROP_STRATEGY)
    assert result["source_preimage_unchanged"] is True
    assert {"evidence_record", "delivery_record", "publish_draft"} <= set(result["source_inputs"])
    assert result["background_pixels_changed"] is True
    assert result["final_cover_generated"] is False
    assert result["cover_pixels_changed"] is None
    assert result["title_exclusion_authority"] == "REQUIRED_NOT_PRESENT"
    assert result["next_required_gate"] == ("CANDIDATE_BOUND_TITLE_EXCLUSION_AUTHORITY")
    assert result["provider_calls"] == 0
    assert result["image_generation_calls"] == 0
    assert result["package_writes"] == 0
    assert result["production_state_writes"] == 0
    assert result["upload_calls"] == 0
    assert result["upload_allowed"] is False
    assert (destination / "identity-card.base.png").is_file()
    assert (destination / "identity-card.poster.png").is_file()
    assert (destination / "identity-card.background_trial.json").is_file()
    assert (destination / "TRIAL-RESULT.json").is_file()
    assert not (destination / "identity-card.cover.png").exists()
    assert not (destination / "identity-card.cover_generation.json").exists()
    assert not (destination / "FAILURE.json").exists()
    assert source_before == {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }


def test_relocated_inline_semantic_drift_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _write_package(tmp_path)
    _patch_current_contract(monkeypatch)
    for key in ("publish", "evidence", "record"):
        path = fixture[key]
        assert isinstance(path, Path)
        document = json.loads(path.read_text())
        generation = (
            document["cover_generation"]
            if key == "publish"
            else document["publish_staging"]["cover_generation"]
        )
        generation["source_composition_verification"]["verdict"]["reason"] = (
            "relocated inline receipt was semantically changed"
        )
        path.write_bytes(_json_bytes(document))
    destination = _private_parent(tmp_path) / "trial"

    with pytest.raises(
        trial.HostOnlyIdentityCardTrialError,
        match="authority drifts.*verdict/reason",
    ):
        trial.build_host_only_identity_card_trial(
            source_package=fixture["source"],
            destination=destination,
            candidate_id=str(fixture["candidate"]),
        )

    failure = json.loads((destination / "FAILURE.json").read_text())
    assert failure["status"] == "FAILED"
    assert "/verdict/reason" in failure["error"]
    assert failure["provider_calls"] == 0
    assert failure["package_writes"] == 0
    assert failure["upload_calls"] == 0
    assert not (destination / "TRIAL-RESULT.json").exists()


def test_non_full_frame_predecessor_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _write_package(tmp_path)
    _patch_current_contract(monkeypatch)
    for key in ("publish", "evidence", "record"):
        path = fixture[key]
        assert isinstance(path, Path)
        document = json.loads(path.read_text())
        generation = (
            document["cover_generation"]
            if key == "publish"
            else document["publish_staging"]["cover_generation"]
        )
        generation["screenshot_frame"]["crop_box"] = [200, 0, 1720, 1080]
        path.write_bytes(_json_bytes(document))
    destination = _private_parent(tmp_path) / "trial"

    with pytest.raises(
        trial.HostOnlyIdentityCardTrialError,
        match="not the full-frame crop degeneracy",
    ):
        trial.build_host_only_identity_card_trial(
            source_package=fixture["source"],
            destination=destination,
            candidate_id=str(fixture["candidate"]),
        )

    failure = json.loads((destination / "FAILURE.json").read_text())
    assert failure["status"] == "FAILED"
    assert failure["provider_calls"] == 0
    assert failure["package_writes"] == 0
    assert failure["upload_calls"] == 0
    assert not (destination / "TRIAL-RESULT.json").exists()


def test_full_frame_predecessor_pixels_must_equal_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _write_package(tmp_path)
    _patch_current_contract(monkeypatch)
    source = fixture["source"]
    assert isinstance(source, Path)
    candidate = str(fixture["candidate"])
    old_base = source / "covers_ai_original" / f"{candidate}.screenshot-base.png"
    with Image.open(old_base) as raw:
        image = raw.convert("RGB")
    image.putpixel((0, 0), (1, 2, 3))
    image.save(old_base)
    new_base_sha = _sha(old_base)
    for key in ("publish", "evidence", "record"):
        path = fixture[key]
        assert isinstance(path, Path)
        document = json.loads(path.read_text())
        generation = (
            document["cover_generation"]
            if key == "publish"
            else document["publish_staging"]["cover_generation"]
        )
        generation["screenshot_frame"]["crop_output_sha256"] = new_base_sha
        path.write_bytes(_json_bytes(document))
    destination = _private_parent(tmp_path) / "trial"

    with pytest.raises(
        trial.HostOnlyIdentityCardTrialError,
        match="full-frame screenshot base pixels drift from reference",
    ):
        trial.build_host_only_identity_card_trial(
            source_package=source,
            destination=destination,
            candidate_id=candidate,
        )

    failure = json.loads((destination / "FAILURE.json").read_text())
    assert failure["status"] == "FAILED"
    assert failure["provider_calls"] == 0
    assert failure["package_writes"] == 0
    assert failure["upload_calls"] == 0
    assert not (destination / "TRIAL-RESULT.json").exists()


def test_review_manifest_symlink_is_rejected_before_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _write_package(tmp_path)
    _patch_current_contract(monkeypatch)
    source = fixture["source"]
    assert isinstance(source, Path)
    manifest = source / "review_manifest.json"
    real_manifest = source / "review_manifest.real.json"
    manifest.rename(real_manifest)
    manifest.symlink_to(real_manifest.name)
    destination = _private_parent(tmp_path) / "trial"

    with pytest.raises(
        trial.HostOnlyIdentityCardTrialError,
        match="review manifest is not a regular file",
    ):
        trial.build_host_only_identity_card_trial(
            source_package=source,
            destination=destination,
            candidate_id=str(fixture["candidate"]),
        )

    failure = json.loads((destination / "FAILURE.json").read_text())
    assert failure["status"] == "FAILED"
    assert failure["provider_calls"] == 0
    assert failure["package_writes"] == 0
    assert failure["upload_calls"] == 0
    assert not (destination / "TRIAL-RESULT.json").exists()


def test_destination_parent_must_be_private_before_any_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _write_package(tmp_path)
    _patch_current_contract(monkeypatch)
    parent = tmp_path / "public"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)
    destination = parent / "trial"

    with pytest.raises(
        trial.HostOnlyIdentityCardTrialError,
        match="owner-private mode 0700",
    ):
        trial.build_host_only_identity_card_trial(
            source_package=fixture["source"],
            destination=destination,
            candidate_id=str(fixture["candidate"]),
        )

    assert not destination.exists()


@pytest.mark.parametrize("surface", ["evidence", "record", "publish"])
@pytest.mark.parametrize("phase", ["after_document_read", "after_background_render"])
@pytest.mark.parametrize("background_only", [False, True])
def test_consumed_source_record_drift_cannot_claim_unchanged_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    phase: str,
    background_only: bool,
) -> None:
    fixture = _write_package(tmp_path)
    _patch_current_contract(monkeypatch)
    if background_only:
        for key in ("evidence", "record", "publish"):
            path = fixture[key]
            document = json.loads(path.read_text())
            generation = (
                document["cover_generation"]
                if key == "publish"
                else document["publish_staging"]["cover_generation"]
            )
            generation.pop("identity_landmark_title_exclusion", None)
            path.write_bytes(_json_bytes(document))
    target = fixture[surface]
    original_bytes = target.read_bytes()
    changed = False

    def change_consumed_record() -> None:
        nonlocal changed
        document = json.loads(target.read_text())
        generation = (
            document["cover_generation"]
            if surface == "publish"
            else document["publish_staging"]["cover_generation"]
        )
        generation["cover_text"] = "source text changed by another writer"
        target.write_bytes(_json_bytes(document))
        changed = True

    if phase == "after_document_read":
        original_loader = trial._load_object

        def load_then_change(path, **kwargs):
            document = original_loader(path, **kwargs)
            if Path(path) == target and not changed:
                change_consumed_record()
            return document

        monkeypatch.setattr(trial, "_load_object", load_then_change)
    else:
        original_renderer = trial._render_trial_background

        def render_then_change(**kwargs):
            rendered = original_renderer(**kwargs)
            change_consumed_record()
            return rendered

        monkeypatch.setattr(trial, "_render_trial_background", render_then_change)

    destination = _private_parent(tmp_path) / "trial"
    with pytest.raises(trial.HostOnlyIdentityCardTrialError, match="changed|drift"):
        trial.build_host_only_identity_card_trial(
            source_package=fixture["source"],
            destination=destination,
            candidate_id=str(fixture["candidate"]),
        )

    assert changed
    assert target.read_bytes() != original_bytes
    assert not (destination / "TRIAL-RESULT.json").exists()
    failure = json.loads((destination / "FAILURE.json").read_text())
    assert failure["status"] == "FAILED"
    assert failure["provider_calls"] == 0
    assert failure["package_writes"] == 0
    assert failure["upload_calls"] == 0


def _current_title_review_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fixture = _write_package(tmp_path)
    _patch_current_contract(monkeypatch)
    for key in ("publish", "record", "evidence"):
        path = fixture[key]
        document = json.loads(path.read_text())
        generation = (
            document["cover_generation"]
            if key == "publish"
            else document["publish_staging"]["cover_generation"]
        )
        generation.pop("identity_landmark_title_exclusion", None)
        path.write_bytes(_json_bytes(document))
    parent = _private_parent(tmp_path)
    background = trial.build_host_only_identity_card_trial(
        source_package=fixture["source"],
        destination=parent / "background",
        candidate_id=fixture["candidate"],
    )
    binding = json.loads(Path(background["outputs"]["background_trial"]["path"]).read_text())
    poster_sha = background["outputs"]["poster"]["sha256"]
    # This fixture is synthetic, not a statement about any real reviewed image.
    authority = {
        "schema_version": trial.TITLE_EXCLUSION_AUTHORITY_SCHEMA,
        "scope": "TITLE_PLACEMENT_ONLY",
        "authority": "ROOT_AGENT_VISUAL_REVIEW",
        "candidate_id": fixture["candidate"],
        "predecessor_generation_sha256": binding["predecessor_generation_sha256"],
        "background_sha256": poster_sha,
        "exclusion": {
            "schema_version": "lidousha-cover-identity-landmark-title-exclusion.v1",
            "landmark": "host_head_and_headwear",
            "background_sha256": poster_sha,
            "protected_bbox": [500, 110, 1440, 540],
            "title_zone": [260, 590, 1660, 1060],
        },
        "reviewed_by": "synthetic_test_fixture",
        "reviewed_at": "2026-09-20T00:00:00-04:00",
        "observations": ["Synthetic rectangle exclusion; not a real visual witness."],
        "inspection": {
            "source_sha256": poster_sha,
            "source_size": [1920, 1080],
            "transform": "FULL_FRAME_RESIZE",
            "image_sha256": "sha256:" + "a" * 64,
        },
    }
    path = parent / "title-authority.json"
    path.write_bytes(_json_bytes(authority))
    return fixture, parent, path, authority


def test_current_poster_review_renders_without_identity_or_upload_authority(tmp_path, monkeypatch):
    fixture, parent, path, authority = _current_title_review_fixture(tmp_path, monkeypatch)
    result = trial.build_host_only_identity_card_trial(
        source_package=fixture["source"],
        destination=parent / "final",
        candidate_id=fixture["candidate"],
        title_exclusion_authority=path,
    )
    assert result["status"] == "PASS_PIXELS_READY_WITNESS_REQUIRED"
    assert result["source_preimage_unchanged"] is True
    assert "title_exclusion_authority" in result["source_inputs"]
    assert result["provider_calls"] == result["upload_calls"] == result["package_writes"] == 0
    assert result["upload_allowed"] is False
    evidence = result["landmark_rebind"]
    assert evidence["mapping_basis"] == "REVIEWED_CURRENT_POSTER_NO_AFFINE_REMAP"
    assert evidence["authority"] == authority
    assert evidence["final_host_identity_verified"] is False
    generation = json.loads(Path(result["outputs"]["generation"]["path"]).read_text())
    assert generation["status"] == "BLOCKED_HOST_ONLY_V4_WITNESS_REQUIRED"
    assert "final_host_identity_verification" not in generation
    assert generation["font_size"] >= 120
    assert generation["rendered_lines"] == ["老板是我亲戚！", "我被内定了"]
    assert generation["ai_background_sha256"] == authority["background_sha256"]


@pytest.mark.parametrize(
    "drift",
    [
        "candidate",
        "predecessor",
        "poster",
        "scope",
        "authority",
        "schema",
        "review_time",
        "reviewer",
        "observations",
        "inspection",
        "missing_exclusion",
        "overlap",
        "bool_coordinate",
        "invalid_landmark",
    ],
)
def test_current_title_review_rejects_binding_or_review_drift(tmp_path, monkeypatch, drift):
    fixture, parent, path, authority = _current_title_review_fixture(tmp_path, monkeypatch)
    if drift == "candidate":
        authority["candidate_id"] = "another-candidate"
    elif drift == "predecessor":
        authority["predecessor_generation_sha256"] = "sha256:" + "0" * 64
    elif drift == "poster":
        # A self-consistent review of another poster still cannot bind here.
        authority["background_sha256"] = "sha256:" + "0" * 64
        authority["exclusion"]["background_sha256"] = authority["background_sha256"]
        authority["inspection"]["source_sha256"] = authority["background_sha256"]
    elif drift in {"scope", "authority", "schema"}:
        authority[{"schema": "schema_version"}.get(drift, drift)] = "incorrect"
    elif drift == "review_time":
        authority["reviewed_at"] = "2026-09-20"
    elif drift == "reviewer":
        authority["reviewed_by"] = ""
    elif drift == "observations":
        authority["observations"] = []
    elif drift == "inspection":
        authority["inspection"]["source_sha256"] = "sha256:" + "0" * 64
    elif drift == "missing_exclusion":
        authority["exclusion"] = None
    elif drift == "overlap":
        authority["exclusion"]["title_zone"] = [300, 300, 1500, 900]
    elif drift == "bool_coordinate":
        authority["exclusion"]["protected_bbox"][0] = True
    else:
        authority["exclusion"]["landmark"] = ["not a valid landmark"]
    path.write_bytes(_json_bytes(authority))
    destination = parent / "refused"
    with pytest.raises(trial.HostOnlyIdentityCardTrialError):
        trial.build_host_only_identity_card_trial(
            source_package=fixture["source"],
            destination=destination,
            candidate_id=fixture["candidate"],
            title_exclusion_authority=path,
        )
    assert not (destination / "TRIAL-RESULT.json").exists()
    assert not (destination / "identity-card.cover.png").exists()


def test_current_title_review_drift_during_render_fails_closed(tmp_path, monkeypatch):
    fixture, parent, path, _ = _current_title_review_fixture(tmp_path, monkeypatch)
    original = trial._render_trial_background

    def change_review(**kwargs):
        background = original(**kwargs)
        path.write_text("{}\n")
        return background

    monkeypatch.setattr(trial, "_render_trial_background", change_review)
    destination = parent / "refused"
    with pytest.raises(trial.HostOnlyIdentityCardTrialError, match="changed"):
        trial.build_host_only_identity_card_trial(
            source_package=fixture["source"],
            destination=destination,
            candidate_id=fixture["candidate"],
            title_exclusion_authority=path,
        )
    assert not (destination / "TRIAL-RESULT.json").exists()


def test_current_title_review_cannot_override_existing_exclusion(tmp_path, monkeypatch):
    fixture = _write_package(tmp_path)
    _patch_current_contract(monkeypatch)
    parent = _private_parent(tmp_path)
    with pytest.raises(trial.HostOnlyIdentityCardTrialError, match="cannot override"):
        trial.build_host_only_identity_card_trial(
            source_package=fixture["source"],
            destination=parent / "refused",
            candidate_id=fixture["candidate"],
            title_exclusion_authority=parent / "not-read.json",
        )
    assert not (parent / "refused/identity-card.base.png").exists()


def test_source_led_trial_preserves_frozen_caption_geometry(tmp_path, monkeypatch):
    fixture = _write_package(tmp_path, source_led=True)
    _patch_current_contract(monkeypatch)
    source = fixture["source"]
    before = {str(p): _sha(p) for p in source.rglob("*") if p.is_file()}
    result = trial.build_host_only_identity_card_trial(
        source_package=source,
        destination=_private_parent(tmp_path) / "source-led-trial",
        candidate_id=fixture["candidate"],
    )
    assert result["status"] == "PASS_BACKGROUND_READY_TITLE_EXCLUSION_AUTHORITY_REQUIRED"
    assert result["final_cover_generated"] is False
    assert result["provider_calls"] == result["package_writes"] == result["upload_calls"] == 0
    assert before == {str(p): _sha(p) for p in source.rglob("*") if p.is_file()}
    # Inspect the actual compositor return via a separate deterministic render.
    poster = Path(result["outputs"]["poster"]["path"])
    original = json.loads(fixture["publish"].read_text())["cover_generation"][
        "screenshot_graphic_poster"
    ]
    evidence = trial._compose_screenshot_poster_background(
        Path(result["outputs"]["base"]["path"]),
        tmp_path / "expected.png",
        art_direction=trial._art_direction(
            json.loads(fixture["publish"].read_text())["cover_generation"]["art_direction"]
        ),
        source_ai_modified=False,
        face_safe_contain=True,
        source_title_zone=tuple(original["title_zone"]),
    )
    assert _sha(poster) == _sha(tmp_path / "expected.png")
    assert (
        evidence["source_frame_transform"]["rendered_content_box"]
        == original["source_frame_transform"]["rendered_content_box"]
    )
    assert evidence["screenshot_card"] == original["screenshot_card"]
    assert evidence["title_zone"] == original["title_zone"]


@pytest.mark.parametrize(
    "drift", ["title_zone", "content_box", "crop", "frame_preservation", "style"]
)
def test_source_led_trial_still_rejects_geometry_or_transfer_drift(tmp_path, monkeypatch, drift):
    fixture = _write_package(tmp_path, source_led=True)
    _patch_current_contract(monkeypatch)
    original = trial._compose_screenshot_poster_background

    def changed(*args, **kwargs):
        evidence = original(*args, **kwargs)
        if drift == "title_zone":
            evidence["title_zone"][1] += 1
        elif drift == "content_box":
            evidence["source_frame_transform"]["rendered_content_box"][0] += 1
        elif drift == "crop":
            evidence["source_frame_transform"]["crop_applied"] = True
        elif drift == "frame_preservation":
            evidence["source_frame_transform"]["full_frame_preserved"] = False
        else:
            evidence["composition"] = "source_frame_with_footer"
        return evidence

    monkeypatch.setattr(trial, "_compose_screenshot_poster_background", changed)
    target = _private_parent(tmp_path) / "blocked"
    with pytest.raises(trial.HostOnlyIdentityCardTrialError, match="screen-space geometry"):
        trial.build_host_only_identity_card_trial(
            source_package=fixture["source"],
            destination=target,
            candidate_id=fixture["candidate"],
        )
    assert (target / "FAILURE.json").is_file()
    assert not (target / "TRIAL-RESULT.json").exists()
