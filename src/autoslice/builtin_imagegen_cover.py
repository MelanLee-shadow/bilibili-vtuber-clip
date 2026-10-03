"""Import a reviewed Codex image_gen artifact without claiming CPA execution.

The saved tool invocation and result are evidence, not a signed provider
attestation. This lane preserves that limitation and the undisclosed model ID;
ordinary source/identity, glyph, story and package gates still apply.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import shutil
from pathlib import Path
from typing import Mapping

from PIL import Image, ImageChops, ImageOps

PROVIDER = "codex_builtin_image_gen"
METHOD = "image_gen.imagegen"
TREATMENT = "builtin_imagegen_redraw"
SCHEMA = "lidousha-builtin-imagegen-source.v1"
FINAL_COVER_SUCCESSOR_SCHEMA = "b2-e422-builtin-final-cover-renderer-successor.v1"
FINAL_COVER_SUCCESSOR_STATUS = "PASS_TITLE_RENDERER_ONLY_NO_IMAGE_GENERATION"
B2_E422_SUCCESSOR_CANDIDATE_ID = "auto_120029_1409_1578"
B2_E422_PARENT_MANIFEST_SHA256 = "ba777fe16592f3d21fbe47998eecb85e3e26ab6851f05ec8680d34223aa036cb"
B2_E422_PARENT_FINAL_COVER_SHA256 = "7c9a2bd2b4551032f3def92add739014d10b6e2108aec16efc09334a516b3d6a"
B2_E422_CURRENT_FINAL_COVER_SHA256 = "338bf8327b3270a2ad1f23839fac4329c18ee24ce39a4ddb1aa674b485f62230"
B2_E422_PRE_OVERLAY_SHA256 = "65b5af1e5e18cd31e53e136c47d4c27755fab25c523e44384d5160c625deac00"
B2_E422_OLD_TITLE_MASK_SHA256 = "a664cf0f34053a7b65de2e80666b4e7cbfce1f31bfcdfc82134102b90c087a8f"
B2_E422_NEW_TITLE_MASK_SHA256 = "1294de10d1a8696eb36f54f4e9b598909ab070b2f3f26738796cfed7de98c97b"
B2_E422_CHANGED_PIXEL_COUNT = 10_980
B2_E422_DIFFERENCE_BBOX = [515, 386, 856, 858]


def _digest(value: object) -> str:
    return str(value or "").removeprefix("sha256:")


def _asset(root: Path, row: object) -> Path:
    if not isinstance(row, Mapping):
        raise ValueError("BUILTIN_IMAGEGEN_ASSET_MISSING")
    name = row.get("path")
    if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("BUILTIN_IMAGEGEN_ASSET_PATH_INVALID")
    path = root / name
    if path.is_symlink() or not path.is_file() or root.resolve() not in path.resolve().parents:
        raise ValueError("BUILTIN_IMAGEGEN_ASSET_NOT_REGULAR")
    if hashlib.sha256(path.read_bytes()).hexdigest() != _digest(row.get("sha256")):
        raise ValueError("BUILTIN_IMAGEGEN_ASSET_HASH_DRIFT")
    return path


_BACKGROUND_SIZE = (1920, 1080)
_BACKGROUND_RESAMPLE = Image.Resampling.LANCZOS


def _background_transform(source: Mapping[str, object]) -> tuple[
    str, tuple[int, int], tuple[float, float] | None
]:
    """Resolve the hash-bound background replay declared by a source manifest.

    Older manifests did not record how the generated image became the
    canonical background, so they retain the historical direct resize.  A
    successor may declare the geometry explicitly; keeping this parser strict
    prevents a malformed manifest from silently changing the provenance
    replay.
    """

    if "background_transform" not in source:
        return "resize", _BACKGROUND_SIZE, None
    declared = source["background_transform"]
    if not isinstance(declared, Mapping):
        raise ValueError("BUILTIN_IMAGEGEN_BACKGROUND_TRANSFORM_INVALID")

    method = declared.get("method")
    size = declared.get("size")
    resample = declared.get("resample")
    if method not in {"resize", "fit"} or resample != "LANCZOS":
        raise ValueError("BUILTIN_IMAGEGEN_BACKGROUND_TRANSFORM_INVALID")
    if (
        not isinstance(size, list)
        or len(size) != 2
        or any(type(value) is not int for value in size)
        or tuple(size) != _BACKGROUND_SIZE
    ):
        raise ValueError("BUILTIN_IMAGEGEN_BACKGROUND_TRANSFORM_INVALID")

    if method == "resize":
        if "centering" in declared:
            raise ValueError("BUILTIN_IMAGEGEN_BACKGROUND_TRANSFORM_INVALID")
        return method, _BACKGROUND_SIZE, None

    centering = declared.get("centering")
    if (
        not isinstance(centering, list)
        or len(centering) != 2
        or any(
            type(value) not in {int, float}
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
            for value in centering
        )
    ):
        raise ValueError("BUILTIN_IMAGEGEN_BACKGROUND_TRANSFORM_INVALID")
    return method, _BACKGROUND_SIZE, (float(centering[0]), float(centering[1]))


def _replay_background_transform(
    source: Mapping[str, object], raw: Image.Image
) -> Image.Image:
    method, size, centering = _background_transform(source)
    rgba = raw.convert("RGBA")
    if method == "fit":
        return ImageOps.fit(
            rgba,
            size,
            method=_BACKGROUND_RESAMPLE,
            centering=centering or (0.5, 0.5),
        )
    return rgba.resize(size, _BACKGROUND_RESAMPLE)


def _validate_final_cover_successor(
    *,
    path: Path,
    source: Mapping[str, object],
    current_final_cover: Path,
) -> list[Path]:
    """Replay an optional title-renderer-only final-cover successor.

    The original image-generation calls, identity reference and generated
    background remain immutable.  Only the derived title-overlay cover may
    change, and every changed pixel must stay inside the union of the old and
    new title masks.
    """

    successor = source.get("final_cover_successor")
    if successor is None:
        return []
    if not isinstance(successor, Mapping):
        raise ValueError("BUILTIN_IMAGEGEN_FINAL_COVER_SUCCESSOR_INVALID")
    if (
        successor.get("schema_version") != FINAL_COVER_SUCCESSOR_SCHEMA
        or successor.get("status") != FINAL_COVER_SUCCESSOR_STATUS
        or source.get("candidate_id") != B2_E422_SUCCESSOR_CANDIDATE_ID
        or successor.get("candidate_id") != B2_E422_SUCCESSOR_CANDIDATE_ID
        or successor.get("candidate_id") != source.get("candidate_id")
        or successor.get("image_generation_calls") != 0
        or successor.get("provider_calls") != 0
        or successor.get("pre_overlay_bytes_preserved") is not True
        or successor.get("render_spec_preserved") is not True
        or successor.get("title_text_preserved") is not True
        or successor.get("pixels_changed_outside_union_title_masks") != 0
    ):
        raise ValueError("BUILTIN_IMAGEGEN_FINAL_COVER_SUCCESSOR_INVALID")

    parent_manifest = _asset(path.parent, successor.get("parent_manifest"))
    parent_source, parent_assets = _read_source(parent_manifest)
    if parent_source.get("final_cover_successor") is not None:
        raise ValueError("BUILTIN_IMAGEGEN_FINAL_COVER_SUCCESSOR_CHAIN_UNSUPPORTED")
    if parent_source.get("candidate_id") != source.get("candidate_id"):
        raise ValueError("BUILTIN_IMAGEGEN_FINAL_COVER_SUCCESSOR_CANDIDATE_DRIFT")

    expected = dict(parent_source)
    expected["final_cover"] = source.get("final_cover")
    expected["final_cover_successor"] = dict(successor)
    if dict(source) != expected:
        raise ValueError("BUILTIN_IMAGEGEN_FINAL_COVER_SUCCESSOR_SCOPE_DRIFT")

    parent_final_cover = _asset(parent_manifest.parent, parent_source.get("final_cover"))
    old_mask = _asset(path.parent, successor.get("old_title_mask"))
    new_mask = _asset(path.parent, successor.get("new_title_mask"))
    if (
        _digest(successor.get("parent_manifest_sha256"))
        != B2_E422_PARENT_MANIFEST_SHA256
        or hashlib.sha256(parent_manifest.read_bytes()).hexdigest()
        != B2_E422_PARENT_MANIFEST_SHA256
        or _digest(successor.get("parent_final_cover_sha256"))
        != B2_E422_PARENT_FINAL_COVER_SHA256
        or hashlib.sha256(parent_final_cover.read_bytes()).hexdigest()
        != B2_E422_PARENT_FINAL_COVER_SHA256
        or _digest(successor.get("current_final_cover_sha256"))
        != B2_E422_CURRENT_FINAL_COVER_SHA256
        or hashlib.sha256(current_final_cover.read_bytes()).hexdigest()
        != B2_E422_CURRENT_FINAL_COVER_SHA256
        or _digest(successor.get("pre_overlay_sha256"))
        != B2_E422_PRE_OVERLAY_SHA256
        or _digest(source.get("background", {}).get("sha256"))
        != B2_E422_PRE_OVERLAY_SHA256
        or _digest(successor.get("old_title_mask", {}).get("sha256"))
        != B2_E422_OLD_TITLE_MASK_SHA256
        or _digest(successor.get("new_title_mask", {}).get("sha256"))
        != B2_E422_NEW_TITLE_MASK_SHA256
    ):
        raise ValueError("BUILTIN_IMAGEGEN_FINAL_COVER_SUCCESSOR_HASH_DRIFT")

    with (
        Image.open(parent_final_cover) as old_image,
        Image.open(current_final_cover) as new_image,
        Image.open(old_mask) as old_mask_image,
        Image.open(new_mask) as new_mask_image,
    ):
        old_rgb = old_image.convert("RGB")
        new_rgb = new_image.convert("RGB")
        old_mask_l = old_mask_image.convert("L")
        new_mask_l = new_mask_image.convert("L")
        if not (
            old_rgb.size == new_rgb.size == old_mask_l.size == new_mask_l.size
            and old_rgb.size == (1920, 1080)
        ):
            raise ValueError("BUILTIN_IMAGEGEN_FINAL_COVER_SUCCESSOR_SIZE_DRIFT")
        difference = ImageChops.difference(old_rgb, new_rgb)
        difference_bbox = difference.getbbox()
        changed = [any(pixel) for pixel in difference.getdata()]
        changed_count = sum(changed)
        union_mask = ImageChops.lighter(old_mask_l, new_mask_l)
        outside_count = sum(
            1
            for is_changed, mask_value in zip(changed, union_mask.getdata())
            if is_changed and mask_value == 0
        )

    if (
        difference_bbox is None
        or list(difference_bbox) != B2_E422_DIFFERENCE_BBOX
        or successor.get("old_vs_new_difference_bbox")
        != B2_E422_DIFFERENCE_BBOX
        or changed_count != B2_E422_CHANGED_PIXEL_COUNT
        or successor.get("old_vs_new_changed_pixels")
        != B2_E422_CHANGED_PIXEL_COUNT
        or outside_count
        != successor.get("pixels_changed_outside_union_title_masks")
    ):
        raise ValueError("BUILTIN_IMAGEGEN_FINAL_COVER_SUCCESSOR_PIXEL_DRIFT")
    return [parent_manifest, *parent_assets, old_mask, new_mask]


def _read_source(path: Path) -> tuple[dict, list[Path]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("BUILTIN_IMAGEGEN_SOURCE_NOT_REGULAR")
    source = json.loads(path.read_text())
    if (
        source.get("schema_version") != SCHEMA
        or source.get("provider") != PROVIDER
        or source.get("method") != METHOD
        or source.get("model") is not None
        or not source.get("owner_task_id")
        or not source.get("candidate_id")
    ):
        raise ValueError("BUILTIN_IMAGEGEN_SOURCE_IDENTITY_INVALID")
    calls = source.get("calls")
    if not isinstance(calls, list) or not calls:
        raise ValueError("BUILTIN_IMAGEGEN_CALLS_MISSING")
    assets: list[Path] = []
    previous = None
    for call in calls:
        rows = [call[k] for k in ("invocation", "result", "prompt", "output")]
        paths = [_asset(path.parent, row) for row in rows]
        invocation, result, prompt, output = paths
        request = json.loads(invocation.read_text())["payload"]
        response = json.loads(result.read_text())["payload"]
        code = request.get("input", request.get("arguments", ""))
        if (
            request.get("call_id") != call.get("call_id")
            or "tools.image_gen__imagegen(" not in code
            or not prompt.read_text().strip()
            or source["owner_task_id"] not in call.get("generated_path", "")
        ):
            raise ValueError("BUILTIN_IMAGEGEN_TOOL_INVOCATION_INVALID")
        blocks = response.get("output")
        if not isinstance(blocks, list):
            raise ValueError("BUILTIN_IMAGEGEN_TOOL_RESULT_INVALID")
        result_text = "\n".join(x.get("text", "") for x in blocks if x.get("type") == "input_text")
        images = [x.get("image_url", "") for x in blocks if x.get("type") == "input_image"]
        output_sha = _digest(call["output"]["sha256"])
        if call["generated_path"] not in result_text or not any(
            url.startswith("data:image/png;base64,")
            and hashlib.sha256(base64.b64decode(url.split(",", 1)[1], validate=True)).hexdigest()
            == output_sha
            for url in images
        ):
            raise ValueError("BUILTIN_IMAGEGEN_TOOL_OUTPUT_MISMATCH")
        references = call.get("references")
        if not isinstance(references, list) or not references:
            raise ValueError("BUILTIN_IMAGEGEN_REFERENCES_MISSING")
        assets.extend(_asset(path.parent, ref) for ref in references)
        if previous is not None and previous not in {_digest(ref["sha256"]) for ref in references}:
            raise ValueError("BUILTIN_IMAGEGEN_EDIT_CHAIN_BROKEN")
        previous = output_sha
        assets.extend(paths)
    assets.extend(
        _asset(path.parent, source[k]) for k in ("identity_reference", "background", "final_cover")
    )
    with Image.open(assets[-2]) as background, Image.open(output) as raw:
        replay = _replay_background_transform(source, raw)
        if (
            background.size != replay.size
            or background.convert("RGBA").tobytes() != replay.tobytes()
        ):
            raise ValueError("BUILTIN_IMAGEGEN_BACKGROUND_TRANSFORM_DRIFT")
    assets.extend(
        _validate_final_cover_successor(
            path=path,
            source=source,
            current_final_cover=assets[-1],
        )
    )
    return source, list(dict.fromkeys(assets))


def validate_builtin_imagegen_provenance(
    generation: Mapping[str, object],
    *,
    manifest_path: Path | None = None,
) -> bool:
    """Recheck saved tool bytes, references, edit ancestry and actual pixels."""
    if (
        generation.get("provider") != PROVIDER
        or generation.get("method") != METHOD
        or generation.get("image_gen_model") != PROVIDER
        or generation.get("model") is not None
        or generation.get("attempted_models") != []
        or generation.get("model_disclosed") is not False
        or generation.get("fallback_used") is not False
    ):
        return False
    try:
        path = manifest_path or Path(str(generation["builtin_imagegen_provenance_path"]))
        if hashlib.sha256(path.read_bytes()).hexdigest() != _digest(
            generation["builtin_imagegen_provenance_sha256"]
        ):
            return False
        source, _ = _read_source(path)
        if source["candidate_id"] != generation.get("candidate_id"):
            return False
        for asset, path_key, hash_key in (
            ("identity_reference", "reference_image", "reference_sha256"),
            ("background", "ai_background", "ai_background_sha256"),
            ("final_cover", "final_cover", "final_cover_sha256"),
        ):
            expected = _digest(source[asset]["sha256"])
            current = Path(str(generation[path_key]))
            if (
                expected != _digest(generation[hash_key])
                or current.is_symlink()
                or hashlib.sha256(current.read_bytes()).hexdigest() != expected
            ):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def copy_builtin_imagegen_source(generation: Mapping[str, object], destination: Path) -> Path:
    """Make the same source bundle portable; retain its manifest bytes exactly."""
    if not validate_builtin_imagegen_provenance(generation):
        raise ValueError("BUILTIN_IMAGEGEN_PROVENANCE_INVALID")
    original = Path(str(generation["builtin_imagegen_provenance_path"]))
    _, assets = _read_source(original)
    for source in [original, *assets]:
        target = destination / source.relative_to(original.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError("BUILTIN_IMAGEGEN_PORTABLE_PREIMAGE_DRIFT")
        if not target.exists():
            shutil.copyfile(source, target)
    return destination / original.name
