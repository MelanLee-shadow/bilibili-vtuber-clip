"""Maker-supplied member manifest and package initialisation.

The maker states facts (files, source interval, doubts table, cover and
speaker claims).  Conclusions never come from the member manifest.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .common import (
    MEMBER_SCHEMA,
    FmrError,
    candidate_id_for,
    file_sha,
    load_json,
    valid_date,
    valid_slug,
)

FILE_KEYS = (
    "video", "srt", "speaker_srt", "ass", "cover", "title", "description", "intro_receipt",
)
_SUFFIX = {
    "video": ".mp4",
    "srt": ".srt",
    "speaker_srt": ".speaker.srt",
    "ass": ".ass",
    "cover": ".cover.png",
    "title": ".title.txt",
    "description": ".description.txt",
    "intro_receipt": ".intro-receipt.json",
}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def package_name(cid: str, key: str) -> str:
    return f"{cid}{_SUFFIX[key]}"


def package_files(root: Path, cid: str) -> dict[str, Path]:
    root = Path(root)
    return {key: root / package_name(cid, key) for key in FILE_KEYS}


def member_copy_path(root: Path, cid: str) -> Path:
    return Path(root) / f"{cid}.member.json"


def record_path(root: Path, cid: str) -> Path:
    return Path(root) / f"{cid}.record.json"


def receipt_path(root: Path, cid: str) -> Path:
    return Path(root) / f"{cid}.finished-media-review.json"


def _party(value: object, label: str, problems: list[str]) -> None:
    if not (
        isinstance(value, dict)
        and value.get("kind") in {"AGENT", "维护者"}
        and isinstance(value.get("id"), str)
        and value["id"].strip()
    ):
        problems.append(f"FMR_MEMBER_{label}_INVALID")


def member_problems(member: dict[str, Any]) -> list[str]:
    """Structural problems only; files are checked by ``resolve_member_files``."""
    problems: list[str] = []
    if member.get("schema_version") != MEMBER_SCHEMA:
        problems.append("FMR_MEMBER_SCHEMA_INVALID")
    if not valid_slug(member.get("member_slug")):
        problems.append("FMR_MEMBER_SLUG_INVALID")
    if not valid_date(member.get("recording_date")):
        problems.append("FMR_MEMBER_DATE_INVALID")
    _party(member.get("maker"), "MAKER", problems)
    files = member.get("files")
    if not isinstance(files, dict) or any(
        not isinstance(files.get(key), str) or not files[key] for key in FILE_KEYS
    ):
        problems.append("FMR_MEMBER_FILES_INCOMPLETE")
    source = member.get("source")
    pieces = source.get("pieces") if isinstance(source, dict) else None
    if not isinstance(pieces, list) or not pieces:
        problems.append("FMR_MEMBER_SOURCE_PIECES_MISSING")
    else:
        for piece in pieces:
            ok = (
                isinstance(piece, dict)
                and isinstance(piece.get("path"), str) and piece["path"]
                and type(piece.get("start_ms")) is int and type(piece.get("end_ms")) is int
                and 0 <= piece["start_ms"] < piece["end_ms"]
            )
            if not ok:
                problems.append("FMR_MEMBER_SOURCE_PIECE_INVALID")
    context = source.get("context_srt") if isinstance(source, dict) else None
    if not (
        isinstance(context, dict)
        and isinstance(context.get("path"), str) and context["path"]
        and type(context.get("source_zero_srt_ms")) is int
    ):
        problems.append("FMR_MEMBER_CONTEXT_SRT_MISSING")
    if any(
        isinstance(p, dict) and isinstance(p.get("path"), str) and not Path(p["path"]).is_absolute()
        for p in (pieces or [])
    ) or (isinstance(context, dict) and isinstance(context.get("path"), str)
          and not Path(context["path"]).is_absolute()):
        problems.append("FMR_MEMBER_SOURCE_PATH_NOT_ABSOLUTE")
    if "description_extra" in member:
        from src.autoslice.channel_envelope import description_extra_problems

        problems.extend(description_extra_problems(member["description_extra"]))
    if not isinstance(member.get("selection_hook"), str) or not member["selection_hook"].strip():
        problems.append("FMR_MEMBER_SELECTION_HOOK_MISSING")
    if not isinstance(member.get("doubts"), list):
        problems.append("FMR_MEMBER_DOUBTS_MISSING")
    else:
        for row in member["doubts"]:
            if not (
                isinstance(row, dict)
                and isinstance(row.get("id"), str) and row["id"]
                and row.get("grade") in {"A", "B", "C", "D"}
                and isinstance(row.get("basis"), str) and row["basis"].strip()
            ):
                problems.append("FMR_MEMBER_DOUBT_ROW_INVALID")
                break
    claims = member.get("cover_claims")
    if not (
        isinstance(claims, dict)
        and isinstance(claims.get("lines"), list) and claims["lines"]
        and all(isinstance(line, str) and line.strip() for line in claims["lines"])
    ):
        problems.append("FMR_MEMBER_COVER_CLAIMS_INVALID")
    return problems


def load_member(path: str | Path) -> dict[str, Any]:
    member = load_json(path)
    problems = member_problems(member)
    if problems:
        raise FmrError(",".join(sorted(set(problems))))
    return member


def member_candidate_id(member: dict[str, Any]) -> str:
    return candidate_id_for(member["recording_date"], member["member_slug"])


def resolve_member_files(member: dict[str, Any], base: Path) -> dict[str, Path]:
    """Absolute, regular, non-symlink member files; cover must be a real PNG."""
    out: dict[str, Path] = {}
    for key in FILE_KEYS:
        raw = Path(member["files"][key])
        path = raw if raw.is_absolute() else (Path(base) / raw)
        if path.is_symlink() or not path.is_file():
            raise FmrError(f"FMR_MEMBER_FILE_MISSING: {key}: {path}")
        out[key] = path
    with open(out["cover"], "rb") as handle:
        if handle.read(8) != PNG_MAGIC:
            raise FmrError("FMR_MEMBER_COVER_NOT_PNG")
    return out


def _link_or_copy(src: Path, dst: Path) -> None:
    if dst.exists():
        if file_sha(dst) != file_sha(src):
            raise FmrError(f"FMR_PACKAGE_FILE_EXISTS_DIFFERENT: {dst}")
        return
    # Copy, never hardlink: makers rewrite their working files in place, and a
    # shared inode would silently change an already sealed package.
    shutil.copy2(src, dst)


def init_package(member_path: str | Path, out_root: str | Path) -> Path:
    """Create ``<out_root>/out/<date>/<cid>/`` and bring the member files in."""
    member_path = Path(member_path).resolve()
    member = load_member(member_path)
    cid = member_candidate_id(member)
    files = resolve_member_files(member, member_path.parent)
    root = Path(out_root) / "out" / member["recording_date"] / cid
    root.mkdir(parents=True, exist_ok=True)
    for key, src in files.items():
        _link_or_copy(src, root / package_name(cid, key))
    target = member_copy_path(root, cid)
    if target.exists() and file_sha(target) != file_sha(member_path):
        raise FmrError("FMR_PACKAGE_MEMBER_DIFFERS: re-init needs a fresh output directory")
    if not target.exists():
        shutil.copy2(member_path, target)
    return root.resolve()
