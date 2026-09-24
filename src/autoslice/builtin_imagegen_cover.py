"""Import a reviewed Codex image_gen artifact without claiming CPA execution.

The saved tool invocation and result are evidence, not a signed provider
attestation. This lane preserves that limitation and the undisclosed model ID;
ordinary source/identity, glyph, story and package gates still apply.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path
from typing import Mapping

from PIL import Image

PROVIDER = "codex_builtin_image_gen"
METHOD = "image_gen.imagegen"
TREATMENT = "builtin_imagegen_redraw"
SCHEMA = "lidousha-builtin-imagegen-source.v1"


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
        replay = raw.convert("RGBA").resize((1920, 1080), Image.Resampling.LANCZOS)
        if (
            background.size != replay.size
            or background.convert("RGBA").tobytes() != replay.tobytes()
        ):
            raise ValueError("BUILTIN_IMAGEGEN_BACKGROUND_TRANSFORM_DRIFT")
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
