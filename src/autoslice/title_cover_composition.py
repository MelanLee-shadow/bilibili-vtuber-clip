"""Source-bound cutout selection and portable deterministic composition replay."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from .title_cover_candidates import sha


def _checked_image(path: Path, digest: object) -> Image.Image:
    if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("COVER_COMPOSITION_SOURCE_UNSAFE")
    if sha(path) != digest:
        raise ValueError("COVER_COMPOSITION_SOURCE_HASH_DRIFT")
    with Image.open(path) as image:
        return image.convert("RGBA")


def sticker_cutout(source: dict, requested: object = None) -> str:
    """Select only existing renderer modes, using verified source bytes.

    White is opt-in for the verified shabutton catalog. The renderer floods
    from the border with its unchanged >720 RGB threshold; enclosed white
    subject details are retained. Final clean_layers/identity gates still apply.
    """
    if requested not in (None, "auto", "none", "alpha", "white"):
        raise ValueError("COVER_COMPOSITION_CUTOUT_INVALID")
    kind = source.get("kind")
    if kind not in ("stream_frame", "official_emote", "shabutton_emote"):
        raise ValueError("COVER_COMPOSITION_UNKNOWN_SOURCE")
    image = _checked_image(Path(source["path"]), source.get("sha256"))
    if kind == "official_emote":
        if not source.get("official_id") or not source.get("mask"):
            raise ValueError("COVER_OFFICIAL_EMOTE_MASK_MISSING")
        mask = _checked_image(Path(source["mask"]), source.get("mask_sha256"))
        low, high = mask.getchannel("A").getextrema()
        if low == 255 or high == 0:
            raise ValueError("COVER_OFFICIAL_EMOTE_MASK_INVALID")
        return "mask:" + str(source["mask"])
    if source.get("mask"):
        raise ValueError("COVER_COMPOSITION_UNTRUSTED_MASK")
    if kind == "stream_frame":
        if requested not in (None, "auto", "none"):
            raise ValueError("COVER_STREAM_FRAME_CUTOUT_FORBIDDEN")
        return "none"
    from .title_cover_shabutton import IMAGE_BASE

    if (
        not str(source.get("source_url") or "").startswith(IMAGE_BASE)
        or not str(source.get("source_manifest_sha256") or "").startswith("sha256:")
    ):
        raise ValueError("SHABUTTON_SOURCE_PROVENANCE_DRIFT")
    low, high = image.getchannel("A").getextrema()
    if high == 0:
        raise ValueError("COVER_COMPOSITION_EMPTY_SOURCE")
    if low < 255:
        return "alpha"  # Real pixels, including RGBA data under a .jpg filename.
    if requested == "alpha":
        raise ValueError("COVER_SOURCE_ALPHA_MISSING")
    if requested == "white":
        from .cover_style import cutout

        alpha = cutout(image, "white").getchannel("A")
        if alpha.getextrema() != (0, 255):
            raise ValueError("COVER_WHITE_CUTOUT_HAS_NO_SAFE_FOREGROUND")
        return "white"
    return "none"


def verify_candidate_composition(*, row, sources, lines, binding, bundle, bound) -> None:
    """Rebuild source → requested geometry → spec → background after relocation."""
    from .cover_style import background
    from .title_cover_candidates import parse_answer
    from .title_cover_staging import _render_spec

    assets, locators = {}, {}
    original = Path(binding.get("bundle_root") or binding["choose_path"]).absolute()
    if not binding.get("bundle_root"):
        original = original.parent
    for key, source in sources.items():
        item = {**source, "path": bound(source["file"])}
        locators[str(original / source["file"])] = str(item["path"])
        locators[str(bundle / source["file"])] = str(item["path"])
        if source.get("mask_file"):
            item["mask"] = bound(source["mask_file"])
            locators[str(original / source["mask_file"])] = str(item["mask"])
            locators[str(bundle / source["mask_file"])] = str(item["mask"])
        assets[key] = item
    prefix = "candidates/" + row["id"] + "/"
    geometry = parse_answer(json.loads(bound(prefix + "geometry.response.json").read_text())["answer"])
    generated = None
    if row["route"] in ("cutout", "ai_sticker", "ai_scene"):
        generated = bound(prefix + "generated-background.png")
        locators[str(original / (prefix + "generated-background.png"))] = str(generated)
        locators[str(bundle / (prefix + "generated-background.png"))] = str(generated)
    expected = _render_spec(row, geometry, assets, lines, generated)

    def relocate(value):
        if isinstance(value, dict):
            return {key: relocate(item) for key, item in value.items()}
        if isinstance(value, list):
            return [relocate(item) for item in value]
        if isinstance(value, str):
            if value.startswith("mask:") and value[5:] in locators:
                return "mask:" + locators[value[5:]]
            return locators.get(value, value)
        return value

    actual = relocate(json.loads(bound(prefix + "render-spec.json").read_text()))
    if actual != expected:
        raise ValueError("COVER_COMPOSITION_SPEC_DRIFT")
    replayed = background(expected).convert("RGB")
    for relative in (prefix + "composition.png", row["background_path"]):
        with Image.open(bound(relative)) as saved:
            if saved.size != replayed.size or saved.convert("RGB").tobytes() != replayed.tobytes():
                raise ValueError("COVER_COMPOSITION_PIXEL_REPLAY_FAILED")
