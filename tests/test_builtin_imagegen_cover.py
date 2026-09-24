"""Builtin import preserves real tool lineage without claiming a CPA model."""

import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice.builtin_imagegen_cover import (
    METHOD,
    PROVIDER,
    SCHEMA,
    copy_builtin_imagegen_source,
    validate_builtin_imagegen_provenance,
)


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
    raw = asset("raw.png", Image.new("RGBA", (32, 18), "red"))
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
    "defect", [None, "source_hash", "incomplete_face", "identity", "tool_bytes"]
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
        "source_face_complete": defect != "incomplete_face",
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
        routes, "validate_final_host_identity_verification", lambda _g: defect != "identity"
    )
    if defect == "source_hash":
        generation["reference_sha256"] = "sha256:" + "0" * 64
    if defect == "tool_bytes":
        Path(generation["builtin_imagegen_provenance_path"]).write_text("{}")
    assert routes.validate_cover_route_decision(generation) is (defect is None)
