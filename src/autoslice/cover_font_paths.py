"""Filesystem-only font path resolution for cover rendering."""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import os
from pathlib import Path
from typing import Any


def resolve_cover_fonts_dir(
    media_path: Path | None,
    *,
    channel_profile: Any,
    root: Path,
) -> Path | None:
    """Resolve selected-profile fonts while retaining runtime fallbacks."""

    candidates: list[Path] = []
    env_value = os.environ.get("AUTOSLICE_FONTS_DIR") or os.environ.get(
        "LIDOUSHA_FONTS_DIR"
    )
    if env_value:
        candidates.append(Path(env_value))
    if media_path is not None:
        for parent in [media_path.parent, *media_path.parents]:
            candidates.append(parent / "fonts")
    candidates.extend(
        [
            channel_profile.asset_directory("fonts", repo_root=root),
            root / "assets" / "fonts",
            root / "assets",
            Path("/app/assets/fonts"),
            Path("/opt/bilive/app/assets/fonts"),
            Path("/app/assets"),
            Path("/opt/bilive/app/assets"),
        ]
    )
    return next(
        (candidate for candidate in candidates if candidate.is_dir()),
        None,
    )


def cover_fallback_font_candidates(
    *,
    channel_profile: Any,
    root: Path,
) -> list[Path]:
    return [
        candidate
        for candidate in (
            channel_profile.asset_directory("fonts", repo_root=root)
            / "SmileySans-Oblique.ttf",
        )
        if candidate.is_file()
    ]


def resolve_primary_cover_font(
    *,
    channel_profile: Any,
    root: Path,
    fontsdir: Path | None,
) -> Path:
    """Resolve ZCOOL and fail closed instead of silently changing fonts."""

    candidates = [
        channel_profile.asset_directory("fonts", repo_root=root)
        / "ZCOOLKuaiLe-Regular.ttf",
        Path("/opt/bilive/app/assets/fonts/ZCOOLKuaiLe-Regular.ttf"),
        Path("/app/assets/fonts/ZCOOLKuaiLe-Regular.ttf"),
    ]
    if fontsdir is not None:
        candidates.insert(0, fontsdir / "ZCOOLKuaiLe-Regular.ttf")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "COVER_FONT_MISSING: ZCOOLKuaiLe-Regular.ttf not found in the selected profile fonts"
    )


def resolve_trusted_cover_font(
    *,
    file_name: str,
    expected_sha256: str,
    channel_profile: Any,
    root: Path,
) -> Path:
    """Resolve the private current font or a historical committed profile font.

    Review packages may carry a filename and hash, but they never get to choose
    an arbitrary host path.  This keeps independent glyph replay rooted in the
    deployment-owned binding or committed assets, independent of package locators.
    """

    if (
        not file_name
        or Path(file_name).name != file_name
        or not expected_sha256.startswith("sha256:")
        or len(expected_sha256) != len("sha256:") + 64
    ):
        raise RuntimeError("COVER_FONT_AUTHORITY_INVALID")
    from .cover_private_font import FILE_NAME, resolve_private_cover_font
    if file_name == FILE_NAME:
        try:
            candidate, _binding = resolve_private_cover_font()
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        if expected_sha256 != "sha256:" + _evidence_file_sha256(candidate):
            raise RuntimeError("COVER_FONT_AUTHORITY_HASH_MISMATCH")
        return candidate
    # Historical covers and the separate song lane still replay their original fonts.
    fonts_dir = channel_profile.asset_directory("fonts", repo_root=root)
    candidate = fonts_dir / file_name
    if not candidate.is_file() or candidate.is_symlink():
        raise RuntimeError("COVER_FONT_AUTHORITY_MISSING")
    digest = _evidence_file_sha256(candidate)
    if expected_sha256 != "sha256:" + digest:
        raise RuntimeError("COVER_FONT_AUTHORITY_HASH_MISMATCH")
    return candidate
