"""The on-host manual-lane cover lock and a compact cross-task selection history."""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from . import channel
from .layout import layout


def shared_root(clip_dir: Path, remote: object = None) -> Path | None:
    """On the media host all tasks use its lane root; remote controllers outside it retain their own batch boundary."""
    from .adapters import LocalRemote

    task = Path(clip_dir).parent.parent
    root = Path(layout().work_root) / "lane"
    if task.parent.resolve() == root.resolve() or isinstance(remote, LocalRemote):
        if not root.is_dir():
            raise FileNotFoundError(f"the shared cover-history root does not exist: {root}")
        return root
    return None


def for_context(ctx: object) -> Path | None:
    adapters = getattr(ctx, "adapters", None)
    return shared_root(getattr(ctx, "clip_dir"), getattr(adapters, "remote", None))


def _history(root: Path) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", channel.PROFILE_ID):
        raise ValueError("invalid channel profile for cover history")
    return root / f".cover-history-{channel.PROFILE_ID}.jsonl"


def recent(ctx: object) -> list[dict[str, Any]]:
    """The latest recorded selection for each clip, excluding the clip currently being rebuilt."""
    root = for_context(ctx)
    if root is None or not (path := _history(root)).is_file():
        return []
    clip = Path(getattr(ctx, "clip_dir"))
    own = (str(clip.parent.parent.resolve()), clip.name)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if (not isinstance(row, dict) or not isinstance(row.get("task"), str) or not isinstance(row.get("cid"), str)
                or not isinstance(row.get("route"), dict) or not isinstance(row.get("written_ns"), int)):
            raise ValueError(f"invalid shared cover history: {path}")
        latest[(row["task"], row["cid"])] = row
    return [row for key, row in latest.items() if key != own]


def note(ctx: object, route: Mapping[str, Any]) -> None:
    """Append a selected cover while the caller holds the shared lock, before publishing the local candidate record."""
    root = for_context(ctx)
    if root is None:
        return
    clip = Path(getattr(ctx, "clip_dir"))
    chosen = route.get("chosen") or {}
    if not isinstance(chosen, Mapping) or not chosen.get("template_id") or not route.get("at"):
        raise ValueError("selected cover has no template or timestamp")
    guests = route.get("guests") or {}
    on_cover = guests.get("on_cover") or [] if isinstance(guests, Mapping) else []
    compact = {"method": "skill.candidates", "at": route["at"], "recording_date": route.get("recording_date"),
               "chosen": {k: chosen.get(k) for k in ("route", "main_emote", "style_id", "host_side", "layout",
                                                        "composition_key", "background_id", "template_id")},
               "peers": {"names": [channel.HOST]}, "base_frame": route.get("base_frame"),
               "guests": {"on_cover": [{"name": g["name"], "asset_id": g["asset_id"]}
                                       for g in on_cover if isinstance(g, Mapping) and g.get("name") and g.get("asset_id")]}}
    row = {"task": str(clip.parent.parent.resolve()), "cid": clip.name, "written_ns": time.time_ns(), "route": compact}
    with _history(root).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
