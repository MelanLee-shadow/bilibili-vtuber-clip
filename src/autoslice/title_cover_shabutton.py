"""Private, source-bound shabutton catalog fetched by the approved shared script."""

from __future__ import annotations
import json
import os
import shutil
from pathlib import Path
from urllib.parse import quote

from .title_cover_candidates import sha, strict_file

DEFAULT_MANIFEST = Path("/opt/bilive/private/title-cover/shabutton/source-manifest.json")
FETCH_SHA256 = "sha256:b32ebc7051e4bc6d854c86b6123f242a3d5ef66b9334083c72824412a2606709"
PAGE = "https://lu-91015.github.io/shadowlee.github.io/"
IMAGE_BASE = PAGE + "assets/images/emotes/"


def manifest_path() -> Path:
    return Path(os.environ.get("LIDOUSHA_SHABUTTON_MANIFEST") or DEFAULT_MANIFEST)


def validate_manifest(value: object) -> list[dict]:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "lidousha-shabutton-assets.v1"
        or value.get("fetch_script_sha256") != FETCH_SHA256
        or value.get("source_page") != PAGE
    ):
        raise ValueError("SHABUTTON_SOURCE_MANIFEST_INVALID")
    entries = value.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 512:
        raise ValueError("SHABUTTON_CATALOG_INVALID")
    ids = set()
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError("SHABUTTON_CATALOG_SOURCE_INVALID")
        name = str(row.get("file") or "")
        if (
            Path(name).name != name
            or name in {"", ".", ".."}
            or not str(row.get("id") or "").startswith("shabutton:")
            or row["id"] in ids
            or row.get("source_url") != IMAGE_BASE + quote(name)
        ):
            raise ValueError("SHABUTTON_CATALOG_SOURCE_INVALID")
        ids.add(row["id"])
    return entries


def stage_assets(root: Path) -> dict:
    path = manifest_path()
    if not path.exists() and not os.environ.get("LIDOUSHA_SHABUTTON_MANIFEST"):
        return {}  # Optional source absent; official 25 and stream frames remain.
    if path.is_symlink() or not path.is_file():
        raise ValueError("SHABUTTON_MANIFEST_MISSING_OR_UNSAFE")
    value = json.loads(path.read_text())
    entries = validate_manifest(value)
    copied_manifest = root / "shabutton-source-manifest.json"
    shutil.copyfile(path, copied_manifest)
    assets = {}
    for index, row in enumerate(entries):
        source = strict_file(path.parent, row["file"])
        if sha(source) != row["sha256"]:
            raise ValueError("SHABUTTON_SOURCE_HASH_DRIFT")
        destination = root / "sources" / ("shabutton-" + str(index) + source.suffix.lower())
        shutil.copyfile(source, destination)
        assets[row["id"]] = {
            "path": destination,
            "sha256": row["sha256"],
            "kind": "shabutton_emote",
            "participant": "UNKNOWN",
            "label": source.stem,
            "source_url": row["source_url"],
            "source_manifest_sha256": sha(copied_manifest),
        }
    return assets
