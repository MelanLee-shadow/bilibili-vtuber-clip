"""Small lane projections for candidate-private producer preparation."""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path


def talk_delivery_basename(hook: object, candidate_id: object) -> str:
    """Return the injective Talk delivery basename outside the lane loop."""

    candidate = str(candidate_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", candidate):
        raise ValueError(f"unsafe talk delivery candidate id: {candidate!r}")
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "", str(hook or "").strip())
    readable = cleaned[:18].rstrip("“”「」『』《》〈〉（）(),，、。：:;；—-·…‘’'\"")
    return f"{readable or cleaned[:18] or candidate}__{candidate}"


def talk_producer_command(command: list[str], *, reuse_cover: bool, prepare_only: bool) -> list[str]:
    if reuse_cover:
        command.append("--reuse-cover")
    if prepare_only:
        command.append("--prepare-only")
    return command


def remove_candidate_private_recuts(out_root: Path, candidate_id: str) -> None:
    """Delete only this candidate's private recut attempt after title rejection."""

    candidate_root = out_root / candidate_id
    recuts = candidate_root / "replacement_recuts"
    try:
        candidate_info = os.lstat(candidate_root)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError("candidate-private cleanup cannot inspect its root") from exc
    if stat.S_ISLNK(candidate_info.st_mode) or not stat.S_ISDIR(candidate_info.st_mode):
        raise RuntimeError("candidate-private cleanup root is unsafe")
    try:
        recuts_info = os.lstat(recuts)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError("candidate-private cleanup cannot inspect recuts") from exc
    if stat.S_ISLNK(recuts_info.st_mode) or not stat.S_ISDIR(recuts_info.st_mode):
        raise RuntimeError("candidate-private recuts root is unsafe")
    shutil.rmtree(recuts)


def prepared_talk_result(result: dict) -> dict:
    summary = result.pop("summary", None)
    if isinstance(summary, dict):
        # ``summary.delivery`` is a materialized-delivery authority consumed
        # by cover/publication maintenance.  Keep prepare facts separate until
        # the owned target rename and state commit project them canonically.
        result["prepared_summary"] = summary
    result["status"] = "delivery_prepared_no_target"
    return result


def prepared_song_result(result: dict, delivery_update: dict, *, prepare_only: bool) -> dict | None:
    result.update(delivery_update)
    if not prepare_only:
        return None
    result["status"] = "delivery_prepared_no_target"
    return result
