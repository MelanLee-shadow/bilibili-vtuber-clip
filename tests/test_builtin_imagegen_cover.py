"""Builtin import preserves real tool lineage without claiming a CPA model."""

import base64
import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageOps

from src.autoslice.builtin_imagegen_cover import (
    GENERIC_FINAL_COVER_SUCCESSOR_SCHEMA,
    GENERIC_FINAL_COVER_SUCCESSOR_STATUS,
    METHOD,
    PROVIDER,
    SCHEMA,
    copy_builtin_imagegen_source,
    validate_builtin_imagegen_provenance,
)
from src.autoslice.cover_text_pixel_evidence import (
    materialize_rendered_text_pixel_evidence,
)
from src.autoslice.cover_title_rendering import (
    LAYOUT_ENGINE_BASIC,
    SCHEMA_VERSION as TITLE_RENDER_SCHEMA,
    render_title_layer,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[1]
FONT = ROOT / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf"


def test_builtin_repair_binding_replays_tool_bytes_and_current_identity(generation, monkeypatch):
    from src.autoslice import cover_repair_route_lineage as lineage

    generation.update(status="AI_COVER_READY", title="current title")
    cover = Path(generation["final_cover"])
    sidecar = cover.with_suffix(".cover_generation.json")
    sidecar.write_text(json.dumps(generation))
    monkeypatch.setattr(lineage, "validate_cover_route_decision", lambda *_a, **_k: True)
    document, path = lineage.validate_cover_generation_for_binding(
        cover=cover, title="current title", candidate_id="candidate",
    )
    assert document["model"] is None and path == sidecar
    from src.autoslice.cover_repair import _enrich_repaired_cover_generation
    from src.autoslice.story_contract import COVER_BINDING_REQUIRED_KEYS

    story = {key: None for key in COVER_BINDING_REQUIRED_KEYS}
    story["selection_hook"] = "current hook"
    document["story_contract"] = story
    monkeypatch.setattr(lineage, "active_story_contract", lambda *_a: story)
    before = sidecar.read_bytes()
    enriched, _ = _enrich_repaired_cover_generation(
        generation=document, generation_path=sidecar, documents=[], title="current title",
    )
    assert enriched["method"] == METHOD and sidecar.read_bytes() == before
    monkeypatch.setattr(lineage, "active_story_contract", lambda *_a: {**story, "selection_hook": "other hook"})
    with pytest.raises(ValueError, match="STORY_CONTRACT_CHANGED"):
        _enrich_repaired_cover_generation(generation=document, generation_path=sidecar, documents=[], title="current title")
    with pytest.raises(ValueError, match="BUILTIN_COVER_BINDING_INVALID"):
        lineage.validate_cover_generation_for_binding(cover=cover, title="stale title", candidate_id="candidate")
    source = json.loads(Path(generation["builtin_imagegen_provenance_path"]).read_text())
    raw = Path(generation["builtin_imagegen_provenance_path"]).parent / source["calls"][0]["output"]["path"]
    raw.write_bytes(b"tampered tool output")
    with pytest.raises(ValueError, match="BUILTIN_COVER_BINDING_INVALID"):
        lineage.validate_cover_generation_for_binding(cover=cover, title="current title", candidate_id="candidate")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def generation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def asset(name, payload):
        path = source / name
        if isinstance(payload, Image.Image):
            payload.save(path)
        else:
            path.write_text(payload)
        return {"path": name, "sha256": _sha(path)}

    reference = asset("reference.png", Image.new("RGB", (32, 18), "blue"))
    raw_image = Image.new("RGBA", (40, 18))
    raw_pixels = raw_image.load()
    for y in range(18):
        for x in range(40):
            raw_pixels[x, y] = (
                (13 * x + 7 * y) % 256,
                (5 * x + 17 * y) % 256,
                (23 * (x + y)) % 256,
                255,
            )
    raw = asset("raw.png", raw_image)
    background = asset(
        "background.png",
        Image.open(source / raw["path"]).resize((1920, 1080), Image.Resampling.LANCZOS),
    )
    final = asset("final.png", Image.new("RGBA", (1920, 1080), "green"))
    call_id = "call_actual_fixture"
    generated_path = "/generated/owner-task/raw.png"
    invocation = asset(
        "call.json",
        json.dumps(
            {
                "payload": {
                    "call_id": call_id,
                    "input": 'await tools.image_gen__imagegen({prompt:"draw"})',
                }
            }
        ),
    )
    result = asset(
        "result.json",
        json.dumps(
            {
                "payload": {
                    "output": [
                        {"type": "input_text", "text": generated_path},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,"
                            + base64.b64encode((source / raw["path"]).read_bytes()).decode(),
                        },
                    ]
                }
            }
        ),
    )
    prompt = asset("prompt.txt", "draw")
    document = {
        "schema_version": SCHEMA,
        "provider": PROVIDER,
        "method": METHOD,
        "model": None,
        "owner_task_id": "owner-task",
        "candidate_id": "candidate",
        "calls": [
            {
                "call_id": call_id,
                "generated_path": generated_path,
                "invocation": invocation,
                "result": result,
                "prompt": prompt,
                "output": raw,
                "references": [reference],
            }
        ],
        "identity_reference": reference,
        "background": background,
        "final_cover": final,
    }
    path = source / "source.json"
    path.write_text(json.dumps(document))
    return {
        "provider": PROVIDER,
        "method": METHOD,
        "image_gen_model": PROVIDER,
        "model": None,
        "model_disclosed": False,
        "attempted_models": [],
        "fallback_used": False,
        "candidate_id": "candidate",
        "builtin_imagegen_provenance_path": str(path),
        "builtin_imagegen_provenance_sha256": _sha(path),
        **{
            key: str(source / row["path"])
            for key, row in (
                ("reference_image", reference),
                ("ai_background", background),
                ("final_cover", final),
            )
        },
        "reference_sha256": reference["sha256"],
        "ai_background_sha256": background["sha256"],
        "final_cover_sha256": final["sha256"],
    }


def _rewrite_source(generation, mutate):
    path = Path(generation["builtin_imagegen_provenance_path"])
    source = json.loads(path.read_text())
    mutate(source)
    path.write_text(json.dumps(source))
    generation["builtin_imagegen_provenance_sha256"] = _sha(path)


def test_accepts_original_tool_bytes_and_portable_copy(generation, tmp_path):
    assert validate_builtin_imagegen_provenance(generation)
    original = Path(generation["builtin_imagegen_provenance_path"])
    copied = copy_builtin_imagegen_source(generation, tmp_path / "portable")
    assert copied.read_bytes() == original.read_bytes()
    assert validate_builtin_imagegen_provenance(generation, manifest_path=copied)
    assert copy_builtin_imagegen_source(generation, copied.parent) == copied


@pytest.mark.parametrize("defect", [None, "missing_declaration", "parent", "reference", "pixels", "geometry"])
def test_edit_chain_replays_canonical_background_reference(generation, tmp_path, defect):
    root = Path(generation["builtin_imagegen_provenance_path"]).parent
    source = json.loads((root / "source.json").read_text())
    prior = source["calls"][0]
    reference = copy.deepcopy(source["background"])
    if defect == "pixels":
        image = Image.open(root / reference["path"]).convert("RGBA")
        image.putpixel((0, 0), (255, 0, 0, 255))
        image.save(root / "edit-reference.png")
        reference = {"path": "edit-reference.png", "sha256": _sha(root / "edit-reference.png")}
    edit = {**copy.deepcopy(prior), "references": [reference]}
    if defect != "missing_declaration":
        edit["edit_reference_transform"] = {
            "parent_output_sha256": prior["output"]["sha256"] if defect != "parent" else "0" * 64,
            "reference_sha256": reference["sha256"] if defect != "reference" else "0" * 64,
            "method": "resize",
            "size": [1920, 1080] if defect != "geometry" else [1920, 1000],
            "resample": "LANCZOS",
        }
    _rewrite_source(generation, lambda document: document["calls"].append(edit))
    assert validate_builtin_imagegen_provenance(generation) is (defect is None)
    if defect is None:
        copied = copy_builtin_imagegen_source(generation, tmp_path / "canonical-edit-portable")
        assert validate_builtin_imagegen_provenance(generation, manifest_path=copied)


def test_accepts_declared_fit_background_transform_and_portable_copy(
    generation, tmp_path
):
    root = Path(generation["builtin_imagegen_provenance_path"]).parent
    with Image.open(root / "raw.png") as raw:
        fitted = ImageOps.fit(
            raw.convert("RGBA"),
            (1920, 1080),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        resized = raw.convert("RGBA").resize(
            (1920, 1080), Image.Resampling.LANCZOS
        )
        assert fitted.tobytes() != resized.tobytes()
        fitted.save(root / "background.png")

    source_path = root / "source.json"
    source = json.loads(source_path.read_text())
    source["background"]["sha256"] = _sha(root / "background.png")
    source_path.write_text(json.dumps(source))
    generation["builtin_imagegen_provenance_sha256"] = _sha(source_path)
    generation["ai_background_sha256"] = _sha(root / "background.png")

    # A fit-derived background must not silently pass through the legacy
    # direct-resize behavior when its transform declaration is absent.
    assert not validate_builtin_imagegen_provenance(generation)

    source["background_transform"] = {
        "method": "fit",
        "size": [1920, 1080],
        "resample": "LANCZOS",
        "centering": [0.5, 0.5],
    }
    source_path.write_text(json.dumps(source))
    generation["builtin_imagegen_provenance_sha256"] = _sha(source_path)
    assert validate_builtin_imagegen_provenance(generation)
    copied = copy_builtin_imagegen_source(generation, tmp_path / "fit-portable")
    copied_source = json.loads(copied.read_text())
    assert copied_source["background_transform"]["method"] == "fit"
    assert validate_builtin_imagegen_provenance(generation, manifest_path=copied)


@pytest.mark.parametrize(
    "transform",
    [
        None,
        {"method": "crop", "size": [1920, 1080], "resample": "LANCZOS"},
        {"method": "fit", "size": [1920, 1081], "resample": "LANCZOS", "centering": [0.5, 0.5]},
        {"method": "fit", "size": [1920, 1080], "resample": "BILINEAR", "centering": [0.5, 0.5]},
        {"method": "fit", "size": [1920, 1080], "resample": "LANCZOS", "centering": [-0.1, 0.5]},
        {"method": "fit", "size": [1920, 1080], "resample": "LANCZOS", "centering": [0.5]},
        {"method": "resize", "size": [1920, 1080], "resample": "LANCZOS", "centering": [0.5, 0.5]},
    ],
)
def test_rejects_invalid_declared_background_transform(generation, transform):
    _rewrite_source(generation, lambda source: source.update(background_transform=transform))
    assert not validate_builtin_imagegen_provenance(generation)


@pytest.mark.parametrize(
    "key,value",
    [
        ("provider", "cpa"),
        ("method", "images.edit"),
        ("model", "gpt-image-2"),
        ("model_disclosed", True),
        ("attempted_models", ["gpt-image-2"]),
    ],
)
def test_rejects_cpa_or_undisclosed_model_spoof(generation, key, value):
    generation[key] = value
    assert not validate_builtin_imagegen_provenance(generation)


@pytest.mark.parametrize(
    "name", ["prompt.txt", "reference.png", "raw.png", "result.json", "final.png"]
)
def test_rejects_asset_tamper(generation, name):
    root = Path(generation["builtin_imagegen_provenance_path"]).parent
    with (root / name).open("ab") as stream:
        stream.write(b"tampered")
    assert not validate_builtin_imagegen_provenance(generation)


def test_rejects_tool_output_even_when_manifest_hash_is_recomputed(generation):
    root = Path(generation["builtin_imagegen_provenance_path"]).parent
    Image.new("RGBA", (32, 18), "yellow").save(root / "raw.png")
    _rewrite_source(
        generation, lambda s: s["calls"][0]["output"].update(sha256=_sha(root / "raw.png"))
    )
    assert not validate_builtin_imagegen_provenance(generation)


def test_rejects_broken_edit_ancestry(generation):
    def mutate(source):
        source["calls"].append(copy.deepcopy(source["calls"][0]))

    _rewrite_source(generation, mutate)
    assert not validate_builtin_imagegen_provenance(generation)


def test_rejects_replaced_background_even_if_hashes_match(generation):
    path = Path(generation["ai_background"])
    Image.new("RGBA", (1920, 1080), "yellow").save(path)
    generation["ai_background_sha256"] = _sha(path)
    _rewrite_source(generation, lambda s: s["background"].update(sha256=_sha(path)))
    assert not validate_builtin_imagegen_provenance(generation)


def test_copy_refuses_to_overwrite_changed_portable_asset(generation, tmp_path):
    destination = tmp_path / "portable"
    copy_builtin_imagegen_source(generation, destination)
    (destination / "prompt.txt").write_text("changed")
    with pytest.raises(ValueError, match="PREIMAGE_DRIFT"):
        copy_builtin_imagegen_source(generation, destination)


@pytest.mark.parametrize(
    "defect",
    [None, "source_hash", "incomplete_face", "identity", "incomplete_face_identity", "tool_bytes"],
)
def test_redraw_accepts_bound_corner_host_without_requiring_screenshot_crop(
    generation,
    monkeypatch,
    defect,
):
    from src.autoslice import cover_route_evidence as routes
    from src.autoslice.cover_source_composition import verify_source_composition

    verdict = {
        "lidousha_bbox_frac": [0.75, 0.57, 0.96, 1.0],
        "source_face_complete": defect not in {"incomplete_face", "incomplete_face_identity"},
        "faithful_crop_can_make_dominant": False,
        "source_carries_story_reaction": True,
        "cpa_redraw_recommended": True,
        "reason": "Small corner host needs redraw.",
    }

    def probe(path, _question, **_kwargs):
        return {
            "status": "OBSERVED",
            "provider": "cpa",
            "image_sha256": _sha(path),
            "answer": json.dumps(verdict),
            "routing": {
                "preferred_provider": "cpa",
                "selected_provider": "cpa",
                "fallback_used": False,
            },
        }

    generation["reference_sha256"] = "sha256:" + _sha(Path(generation["reference_image"]))
    proof = verify_source_composition(
        reference_path=Path(generation["reference_image"]),
        reference_sha256=generation["reference_sha256"],
        story_hook="Thunder burp",
        title="Thunder burp",
        image_probe=probe,
    )
    generation.update(source_composition_verification=proof, cover_origin="BUILTIN_AI_REDRAW")
    generation["route_decision"] = routes.build_cover_route_decision(
        selected_treatment="builtin_imagegen_redraw",
        selected_rationale="Reviewed artwork",
        story_contract=None,
        reference_authority=None,
        decision_inputs={"cover_mode": "reviewed_builtin_import", "host_identity_required": True},
        source_composition_verification=proof,
    )
    routes.record_cover_route_execution(
        generation,
        actual_treatment="builtin_imagegen_redraw",
        execution_status="READY",
        image_generation_attempted=True,
        image_generation_used=True,
    )
    monkeypatch.setattr(
        routes,
        "validate_final_host_identity_verification",
        lambda _g: defect not in {"identity", "incomplete_face_identity"},
    )
    if defect == "source_hash":
        generation["reference_sha256"] = "sha256:" + "0" * 64
    if defect == "tool_bytes":
        Path(generation["builtin_imagegen_provenance_path"]).write_text("{}")
    assert routes.validate_cover_route_decision(generation) is (
        defect in {None, "incomplete_face"}
    )


def _add_renderer_only_successor(generation, monkeypatch):
    import shutil

    from PIL import ImageChops, ImageDraw
    from src.autoslice import builtin_imagegen_cover as builtin

    root = Path(generation["builtin_imagegen_provenance_path"]).parent
    source_path = root / "source.json"
    parent_root = root / "parent"
    parent_root.mkdir()
    for source_file in list(root.iterdir()):
        if source_file.is_file():
            shutil.copy2(source_file, parent_root / source_file.name)
    parent_manifest = parent_root / "source.json"
    parent_source = json.loads(parent_manifest.read_text())
    parent_final = parent_root / parent_source["final_cover"]["path"]

    current_final = Path(generation["final_cover"])
    with Image.open(current_final) as image:
        current = image.convert("RGBA")
    ImageDraw.Draw(current).rectangle((5, 5, 14, 14), fill="yellow")
    current.save(current_final)

    old_mask = root / "old-title-mask.png"
    new_mask = root / "new-title-mask.png"
    for mask_path in (old_mask, new_mask):
        mask = Image.new("L", (1920, 1080), 0)
        ImageDraw.Draw(mask).rectangle((5, 5, 14, 14), fill=255)
        mask.save(mask_path)

    with Image.open(parent_final) as old_image, Image.open(current_final) as new_image:
        difference = ImageChops.difference(
            old_image.convert("RGB"), new_image.convert("RGB")
        )
        bbox = list(difference.getbbox() or ())
        changed_count = sum(any(pixel) for pixel in difference.getdata())

    source = json.loads(source_path.read_text())
    source["final_cover"]["sha256"] = _sha(current_final)
    source["final_cover_successor"] = {
        "schema_version": builtin.FINAL_COVER_SUCCESSOR_SCHEMA,
        "status": builtin.FINAL_COVER_SUCCESSOR_STATUS,
        "candidate_id": generation["candidate_id"],
        "parent_manifest": {
            "path": "parent/source.json",
            "sha256": _sha(parent_manifest),
        },
        "parent_manifest_sha256": "sha256:" + _sha(parent_manifest),
        "parent_final_cover_sha256": "sha256:" + _sha(parent_final),
        "current_final_cover_sha256": "sha256:" + _sha(current_final),
        "pre_overlay_sha256": "sha256:" + source["background"]["sha256"],
        "old_title_mask": {
            "path": old_mask.name,
            "sha256": _sha(old_mask),
        },
        "new_title_mask": {
            "path": new_mask.name,
            "sha256": _sha(new_mask),
        },
        "old_vs_new_changed_pixels": changed_count,
        "old_vs_new_difference_bbox": bbox,
        "pixels_changed_outside_union_title_masks": 0,
        "pre_overlay_bytes_preserved": True,
        "render_spec_preserved": True,
        "title_text_preserved": True,
        "image_generation_calls": 0,
        "provider_calls": 0,
    }
    source_path.write_text(json.dumps(source))
    generation["final_cover_sha256"] = _sha(current_final)
    generation["builtin_imagegen_provenance_sha256"] = _sha(source_path)

    monkeypatch.setattr(
        builtin, "B2_E422_SUCCESSOR_CANDIDATE_ID", generation["candidate_id"]
    )
    monkeypatch.setattr(
        builtin, "B2_E422_PARENT_MANIFEST_SHA256", _sha(parent_manifest)
    )
    monkeypatch.setattr(
        builtin, "B2_E422_PARENT_FINAL_COVER_SHA256", _sha(parent_final)
    )
    monkeypatch.setattr(
        builtin, "B2_E422_CURRENT_FINAL_COVER_SHA256", _sha(current_final)
    )
    monkeypatch.setattr(
        builtin, "B2_E422_PRE_OVERLAY_SHA256", source["background"]["sha256"]
    )
    monkeypatch.setattr(
        builtin, "B2_E422_OLD_TITLE_MASK_SHA256", _sha(old_mask)
    )
    monkeypatch.setattr(
        builtin, "B2_E422_NEW_TITLE_MASK_SHA256", _sha(new_mask)
    )
    monkeypatch.setattr(builtin, "B2_E422_CHANGED_PIXEL_COUNT", changed_count)
    monkeypatch.setattr(builtin, "B2_E422_DIFFERENCE_BBOX", bbox)
    return source_path, parent_manifest, old_mask, new_mask


def test_accepts_renderer_only_final_cover_successor_and_portable_copy(
    generation, tmp_path, monkeypatch
):
    source_path, _parent, old_mask, new_mask = _add_renderer_only_successor(
        generation, monkeypatch
    )
    assert validate_builtin_imagegen_provenance(generation)
    copied = copy_builtin_imagegen_source(generation, tmp_path / "portable-successor")
    assert copied.read_bytes() == source_path.read_bytes()
    assert (copied.parent / "parent/source.json").is_file()
    assert (copied.parent / old_mask.name).is_file()
    assert (copied.parent / new_mask.name).is_file()
    assert validate_builtin_imagegen_provenance(generation, manifest_path=copied)


def test_renderer_successor_rejects_pixel_change_outside_rebound_masks(
    generation, monkeypatch
):
    from src.autoslice import builtin_imagegen_cover as builtin

    source_path, _parent, old_mask, new_mask = _add_renderer_only_successor(
        generation, monkeypatch
    )
    for mask_path in (old_mask, new_mask):
        Image.new("L", (1920, 1080), 0).save(mask_path)
    source = json.loads(source_path.read_text())
    source["final_cover_successor"]["old_title_mask"]["sha256"] = _sha(old_mask)
    source["final_cover_successor"]["new_title_mask"]["sha256"] = _sha(new_mask)
    source_path.write_text(json.dumps(source))
    generation["builtin_imagegen_provenance_sha256"] = _sha(source_path)
    monkeypatch.setattr(builtin, "B2_E422_OLD_TITLE_MASK_SHA256", _sha(old_mask))
    monkeypatch.setattr(builtin, "B2_E422_NEW_TITLE_MASK_SHA256", _sha(new_mask))
    assert not validate_builtin_imagegen_provenance(generation)


def _font_overlay_spec(text: str) -> dict[str, object]:
    return {
        "schema_version": TITLE_RENDER_SCHEMA,
        "layout_engine": LAYOUT_ENGINE_BASIC,
        "font_file_name": FONT.name,
        "font_file_sha256": sha256_file(FONT),
        "font_face_index": 0,
        "layer_size": [1000, 300],
        "rendered_layer_size": [1000, 300],
        "angle_degrees": 0,
        "lines": [
            {
                "x": 30,
                "y": 30,
                "font_size": 180,
                "segment_pad": 15,
                "segments": [{"text": text, "fill": [255, 198, 41]}],
                "outlines": [
                    {"width": 15, "color": [18, 36, 79]},
                    {"width": 8, "color": [255, 255, 255]},
                ],
            }
        ],
    }


def _add_generic_font_overlay_successor(generation):
    """Build a real two-render successor using the trusted glyph replayer."""

    root = Path(generation["builtin_imagegen_provenance_path"]).parent
    source_path = root / "source.json"
    parent_root = root / "parent"
    parent_root.mkdir()
    for source_file in root.iterdir():
        if source_file.is_file():
            shutil.copy2(source_file, parent_root / source_file.name)

    parent_manifest = parent_root / "source.json"
    parent_source = json.loads(parent_manifest.read_text())
    parent_final = parent_root / parent_source["final_cover"]["path"]
    current_final = root / "final.png"
    parent_pre = parent_root / "font-pre.png"
    current_pre = root / "font-pre.png"
    with Image.open(root / "background.png") as route_background:
        normalized = ImageOps.fit(
            route_background.convert("RGB"),
            (1920, 1080),
            method=Image.Resampling.LANCZOS,
        )
    normalized.save(current_pre)
    shutil.copy2(current_pre, parent_pre)
    parent_font = parent_root / FONT.name
    current_font = root / FONT.name
    shutil.copy2(FONT, current_font)
    shutil.copy2(FONT, parent_font)

    def render(final_path, pre_path, text):
        spec = _font_overlay_spec(text)
        layer = render_title_layer(spec, font_path=FONT)
        with Image.open(pre_path) as pre:
            image = pre.convert("RGBA")
        image.paste(layer, (300, 100), layer)
        image.save(final_path)
        evidence = materialize_rendered_text_pixel_evidence(
            final_cover_path=final_path,
            pre_overlay_path=pre_path,
            font_path=FONT,
            render_spec=spec,
            paste_xy=(300, 100),
            font_size=180,
            rendered_text=text,
        )
        return evidence

    old_evidence = render(parent_final, parent_pre, "旧封面文字")
    new_evidence = render(current_final, current_pre, "新封面文字")

    def relative_evidence(evidence, *, pre_path, mask_path):
        result = dict(evidence)
        result["pre_overlay_path"] = str(pre_path.relative_to(root))
        result["mask_path"] = str(mask_path.relative_to(root))
        return result

    old_mask = parent_root / "final.title-mask.png"
    new_mask = root / "final.title-mask.png"
    old_evidence = relative_evidence(
        old_evidence, pre_path=parent_pre, mask_path=old_mask
    )
    new_evidence = relative_evidence(
        new_evidence, pre_path=current_pre, mask_path=new_mask
    )

    parent_source["final_cover"]["sha256"] = _sha(parent_final)
    parent_manifest.write_text(json.dumps(parent_source))
    source = json.loads(source_path.read_text())
    source["final_cover"]["sha256"] = _sha(current_final)

    def bundle(evidence, *, font, pre, mask):
        return {
            "evidence": evidence,
            "font": {"path": str(font.relative_to(root)), "sha256": _sha(font)},
            "pre_overlay": {
                "path": str(pre.relative_to(root)),
                "sha256": _sha(pre),
            },
            "mask": {"path": str(mask.relative_to(root)), "sha256": _sha(mask)},
        }

    source["final_cover_successor"] = {
        "schema_version": GENERIC_FINAL_COVER_SUCCESSOR_SCHEMA,
        "status": GENERIC_FINAL_COVER_SUCCESSOR_STATUS,
        "candidate_id": generation["candidate_id"],
        "parent_manifest": {
            "path": str(parent_manifest.relative_to(root)),
            "sha256": "sha256:" + _sha(parent_manifest),
        },
        "parent_manifest_sha256": "sha256:" + _sha(parent_manifest),
        "parent_final_cover_sha256": "sha256:" + _sha(parent_final),
        "current_final_cover_sha256": "sha256:" + _sha(current_final),
        "pre_overlay_sha256": new_evidence["pre_overlay_sha256"],
        "old_title_mask": {
            "path": str(old_mask.relative_to(root)),
            "sha256": _sha(old_mask),
        },
        "new_title_mask": {
            "path": str(new_mask.relative_to(root)),
            "sha256": _sha(new_mask),
        },
        "old_rendered_text_pixels": bundle(
            old_evidence, font=parent_font, pre=parent_pre, mask=old_mask
        ),
        "new_rendered_text_pixels": bundle(
            new_evidence, font=current_font, pre=current_pre, mask=new_mask
        ),
        "old_vs_new_changed_pixels": 0,
        "old_vs_new_difference_bbox": [],
        "pixels_changed_outside_union_title_masks": 0,
        "pre_overlay_bytes_preserved": True,
        "render_spec_preserved": False,
        "title_text_preserved": False,
        "image_generation_calls": 0,
        "provider_calls": 0,
    }
    with Image.open(parent_final) as old_image, Image.open(current_final) as new_image:
        difference = ImageChops.difference(
            old_image.convert("RGB"), new_image.convert("RGB")
        )
        source["final_cover_successor"]["old_vs_new_difference_bbox"] = list(
            difference.getbbox() or ()
        )
        source["final_cover_successor"]["old_vs_new_changed_pixels"] = sum(
            any(pixel) for pixel in difference.getdata()
        )
    source_path.write_text(json.dumps(source))
    generation["final_cover_sha256"] = _sha(current_final)
    generation["builtin_imagegen_provenance_sha256"] = _sha(source_path)
    return {
        "source_path": source_path,
        "parent_manifest": parent_manifest,
        "old_mask": old_mask,
        "new_mask": new_mask,
        "current_final": current_final,
    }


def test_accepts_generic_font_overlay_successor_and_portable_copy(generation, tmp_path):
    artifacts = _add_generic_font_overlay_successor(generation)
    assert validate_builtin_imagegen_provenance(generation)
    copied = copy_builtin_imagegen_source(generation, tmp_path / "generic-portable")
    assert copied.read_bytes() == artifacts["source_path"].read_bytes()
    assert validate_builtin_imagegen_provenance(generation, manifest_path=copied)


@pytest.mark.parametrize("mutation", ["provider", "background", "calls"])
def test_generic_successor_rejects_parent_scope_drift(generation, mutation):
    artifacts = _add_generic_font_overlay_successor(generation)
    path = artifacts["source_path"]
    source = json.loads(path.read_text())
    if mutation == "provider":
        source["provider"] = "cpa"
    elif mutation == "background":
        source["background"]["sha256"] = "0" * 64
    else:
        source["calls"][0]["call_id"] = "changed-call"
    path.write_text(json.dumps(source))
    generation["builtin_imagegen_provenance_sha256"] = _sha(path)
    assert not validate_builtin_imagegen_provenance(generation)


def test_generic_successor_rejects_mask_tamper(generation):
    artifacts = _add_generic_font_overlay_successor(generation)
    mask = artifacts["new_mask"]
    Image.new("L", (1920, 1080), 0).save(mask)
    path = artifacts["source_path"]
    source = json.loads(path.read_text())
    successor = source["final_cover_successor"]
    successor["new_title_mask"]["sha256"] = _sha(mask)
    successor["new_rendered_text_pixels"]["mask"]["sha256"] = _sha(mask)
    successor["new_rendered_text_pixels"]["evidence"]["mask_sha256"] = (
        "sha256:" + _sha(mask)
    )
    path.write_text(json.dumps(source))
    generation["builtin_imagegen_provenance_sha256"] = _sha(path)
    assert not validate_builtin_imagegen_provenance(generation)


def test_generic_successor_rejects_non_text_pixel_tamper(generation):
    artifacts = _add_generic_font_overlay_successor(generation)
    current = artifacts["current_final"]
    with Image.open(current) as image:
        tampered = image.convert("RGB")
    tampered.putpixel((1900, 1000), (1, 2, 3))
    tampered.save(current)
    path = artifacts["source_path"]
    source = json.loads(path.read_text())
    source["final_cover"]["sha256"] = _sha(current)
    source["final_cover_successor"]["current_final_cover_sha256"] = (
        "sha256:" + _sha(current)
    )
    source["final_cover_successor"]["new_rendered_text_pixels"]["evidence"][
        "final_cover_sha256"
    ] = "sha256:" + _sha(current)
    path.write_text(json.dumps(source))
    generation["final_cover_sha256"] = _sha(current)
    generation["builtin_imagegen_provenance_sha256"] = _sha(path)
    assert not validate_builtin_imagegen_provenance(generation)


def test_generic_successor_rejects_rebound_non_route_pre_overlay(
    generation, monkeypatch
):
    from src.autoslice import builtin_imagegen_cover as builtin

    artifacts = _add_generic_font_overlay_successor(generation)
    root = artifacts["source_path"].parent
    wrong_pre = Image.new("RGB", (1920, 1080), (1, 2, 3))
    old_pre = root / "parent/font-pre.png"
    new_pre = root / "font-pre.png"
    wrong_pre.save(old_pre)
    wrong_pre.save(new_pre)
    wrong_sha = "sha256:" + _sha(new_pre)
    path = artifacts["source_path"]
    source = json.loads(path.read_text())
    successor = source["final_cover_successor"]
    successor["pre_overlay_sha256"] = wrong_sha
    for key in ("old_rendered_text_pixels", "new_rendered_text_pixels"):
        bundle = successor[key]
        bundle["pre_overlay"]["sha256"] = _sha(
            root / bundle["pre_overlay"]["path"]
        )
        bundle["evidence"]["pre_overlay_sha256"] = wrong_sha
    path.write_text(json.dumps(source))
    generation["builtin_imagegen_provenance_sha256"] = _sha(path)
    # Isolate the route-background check from the recomposition check: even
    # after both preimages and hashes are rebound, the source route must win.
    monkeypatch.setattr(
        builtin, "verify_rendered_text_pixel_artifacts", lambda *_a, **_k: True
    )
    assert not validate_builtin_imagegen_provenance(generation)


def test_generic_successor_rejects_false_title_preserved_claim(generation):
    artifacts = _add_generic_font_overlay_successor(generation)
    path = artifacts["source_path"]
    source = json.loads(path.read_text())
    source["final_cover_successor"]["title_text_preserved"] = True
    path.write_text(json.dumps(source))
    generation["builtin_imagegen_provenance_sha256"] = _sha(path)
    assert not validate_builtin_imagegen_provenance(generation)


def test_generic_successor_rejects_new_provider_call(generation):
    artifacts = _add_generic_font_overlay_successor(generation)
    path = artifacts["source_path"]
    source = json.loads(path.read_text())
    source["final_cover_successor"]["provider_calls"] = 1
    path.write_text(json.dumps(source))
    generation["builtin_imagegen_provenance_sha256"] = _sha(path)
    assert not validate_builtin_imagegen_provenance(generation)
