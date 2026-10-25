"""Deployment-owned private REEJI font binding; packages cannot choose paths."""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from PIL import ImageFont

SCHEMA = "lidousha-private-cover-font.v1"
FILE_NAME = "REEJI-ZhenyanGB.ttf"
DEFAULT_BINDING = Path("/opt/bilive/private/title-cover/fonts/cover-font.json")



APPROVED_LOCAL_SOURCE = Path("/path/to/private-font.ttf")
APPROVED_LOCAL_SHA256 = "sha256:" + "0" * 64
APPROVED_LOCAL_BYTES = 0
APPROVED_LOCAL_PAGE = "https://www.reeji.com/"
APPROVED_LOCAL_ACQUISITION = "USER_APPROVED_LOCAL_FILE"
APPROVED_LOCAL_APPROVAL = {}


def _digest(path: Path) -> str:
    return "sha256:" + _evidence_file_sha256(path)


def _regular(path: Path) -> bool:
    return path.is_file() and not any(p.is_symlink() for p in (path, *path.parents))


def _outside_repository(path: Path) -> bool:
    return not any((p / ".git").exists() for p in (path.parent, *path.parents))


def _approved_local_source_is_valid(binding: dict) -> bool:
    """Private operator authorizations are not distributed by the public snapshot."""
    return False


def resolve_private_cover_font(*, binding_path: Path | None = None) -> tuple[Path, dict]:
    """Read only an operator-installed manifest, never a package font locator.

    Both acquisition routes use the same private-file, hash and FreeType gates.
    Private local authorization is unavailable here; missing or different
    bytes cannot activate a historical or system-font fallback.
    """
    manifest = (binding_path or Path(
        os.environ.get("LIDOUSHA_COVER_FONT_BINDING") or DEFAULT_BINDING
    )).absolute()
    if not _regular(manifest):
        raise ValueError("COVER_PRIVATE_FONT_BINDING_MISSING")
    try:
        binding = json.loads(manifest.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError("COVER_PRIVATE_FONT_BINDING_INVALID") from exc
    if (
        not isinstance(binding, dict)
        or binding.get("schema_version") != SCHEMA
        or binding.get("font_file_name") != FILE_NAME
    ):
        raise ValueError("COVER_PRIVATE_FONT_BINDING_INVALID")
    acquisition = binding.get("acquisition")
    if acquisition == APPROVED_LOCAL_ACQUISITION:
        if not _approved_local_source_is_valid(binding):
            raise ValueError("COVER_PRIVATE_FONT_BINDING_INVALID")
    elif acquisition == "OFFICIAL_DOWNLOAD":
        if (
            urlparse(str(binding.get("source_page") or "")).scheme != "https"
            or urlparse(str(binding.get("source_page") or "")).hostname
            not in ("www.reeji.com", "reeji.com")
            or urlparse(str(binding.get("download_url") or "")).scheme != "https"
        ):
            raise ValueError("COVER_PRIVATE_FONT_BINDING_INVALID")
        host = urlparse(binding["download_url"]).hostname or ""
        if host != "reeji.com" and not host.endswith(".reeji.com"):
            raise ValueError("COVER_PRIVATE_FONT_SOURCE_INVALID")
    else:
        raise ValueError("COVER_PRIVATE_FONT_BINDING_INVALID")
    configured = os.environ.get("LIDOUSHA_COVER_FONT")
    path = (Path(configured) if configured else manifest.parent / FILE_NAME).absolute()
    if path.name != FILE_NAME or not _regular(path):
        raise ValueError("COVER_PRIVATE_FONT_MISSING_OR_UNSAFE")
    # Only the operator-selected private installation can supply font bytes.
    # A local binding cannot redirect to a package, symlink or unrelated path.
    if not _outside_repository(path) or not _outside_repository(manifest):
        raise ValueError("COVER_PRIVATE_FONT_MUST_STAY_OUTSIDE_REPOSITORY")
    if acquisition == APPROVED_LOCAL_ACQUISITION and (
        manifest.name != "cover-font.json"
        or path != manifest.parent / FILE_NAME
        or binding.get("font_path") != str(path)
    ):
        raise ValueError("COVER_PRIVATE_FONT_MISSING_OR_UNSAFE")
    actual = _digest(path)
    if binding.get("font_sha256") != actual:
        raise ValueError("COVER_FONT_AUTHORITY_HASH_MISMATCH")
    if "font_bytes" in binding and (
        type(binding["font_bytes"]) is not int or binding["font_bytes"] != path.stat().st_size
    ):
        raise ValueError("COVER_FONT_AUTHORITY_HASH_MISMATCH")
    pinned = os.environ.get("LIDOUSHA_COVER_FONT_SHA256")
    if pinned and pinned.removeprefix("sha256:") != actual.removeprefix("sha256:"):
        raise ValueError("COVER_FONT_AUTHORITY_HASH_MISMATCH")
    face = binding.get("font_face_index", 0)
    if type(face) is not int or face != 0:
        raise ValueError("COVER_PRIVATE_FONT_IDENTITY_MISMATCH")
    try:
        family, style = ImageFont.truetype(str(path), 64, index=face).getname()
    except OSError as exc:
        raise ValueError("COVER_PRIVATE_FONT_FORMAT_INVALID") from exc
    if "zhenyan" not in family.lower() and "真言" not in family:
        raise ValueError("COVER_PRIVATE_FONT_FAMILY_MISMATCH")
    if binding.get("font_family") != family or binding.get("font_style") != style:
        raise ValueError("COVER_PRIVATE_FONT_IDENTITY_MISMATCH")
    authority = {
        "schema_version": SCHEMA,
        "font_file_name": FILE_NAME,
        "font_sha256": actual,
        "font_family": family,
        "font_style": style,
        "source_page": binding["source_page"],
        "download_url": binding["download_url"],
        "acquisition": acquisition,
        "binding_sha256": _digest(manifest),
    }
    if acquisition == APPROVED_LOCAL_ACQUISITION:
        authority.update(
            font_bytes=path.stat().st_size,
            font_face_index=face,
            source_path=binding["source_path"],
            source_sha256=binding["source_sha256"],
            provenance_basis=binding["provenance_basis"],
        )
    return path, authority
