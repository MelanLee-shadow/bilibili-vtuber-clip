"""Source session and recent delivered-cover history used by skill scoring."""

from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping

from .title_cover_candidates import SCHEMA, sha, text_sha


def source_session_key(record: Mapping) -> str:
    for key in ("source_session_id", "session_id", "recording_date"):
        if isinstance(record.get(key), str) and record[key].strip():
            return text_sha(key + ":" + record[key])
    binding = record.get("source_binding") or {}
    for key in ("source_session_id", "session_id", "recording_date"):
        if isinstance(binding, Mapping) and isinstance(binding.get(key), str):
            return text_sha(key + ":" + binding[key])
    for key in ("source_video_path", "source_path", "source_media"):
        raw = record.get(key)
        if isinstance(raw, str) and raw.strip():
            name = Path(raw).name
            date = re.search(r"20\d\d[-_]\d\d[-_]\d\d", raw)
            # Original capture dates survive package relocation; conservatively
            # count all captures on that date as the same source session.
            return (
                text_sha("recording_date:" + date[0].replace("_", "-"))
                if date
                else text_sha("source_name:" + name)
            )
    return "UNRESOLVED"


def recent_choices(history_root: Path, source_session: str, candidate_id: str) -> dict:
    rows: dict[str, tuple] = {}
    if history_root.exists():
        for path in sorted(history_root.glob("**/choose.json")):
            if path.is_symlink():
                raise ValueError("COVER_HISTORY_SYMLINK")
            value = json.loads(path.read_text())
            if value.get("schema_version") != SCHEMA or value.get("candidate_id") == candidate_id:
                continue
            if "selected" not in value:
                continue
            selected = next(row for row in value["candidates"] if row["id"] == value["selected"])
            row = (
                value["created_at"],
                path,
                value.get("source_session", "UNRESOLVED"),
                selected,
                "CHOOSE_CREATED_AT",
            )
            key = str(value["candidate_id"])
            if key not in rows or (row[0], str(path)) > (rows[key][0], str(rows[key][1])):
                rows[key] = row
        for path in sorted(history_root.glob("**/*.record.json")):
            if path.is_symlink():
                raise ValueError("COVER_HISTORY_SYMLINK")
            record = json.loads(path.read_text())
            if not isinstance(record, Mapping):
                raise ValueError("COVER_HISTORY_RECORD_INVALID")
            staging = record.get("publish_staging") or {}
            key = str(record.get("candidate_id") or staging.get("candidate_id") or path.stem)
            if key == candidate_id or key in rows:
                continue
            generation = staging.get("cover_generation") or {}
            if not generation or generation.get("method") == "skill.candidates":
                continue
            if staging.get("cover_status") not in {"AI_COVER_READY", "REUSED_COVER"}:
                continue
            direction = generation.get("art_direction") or {}
            emote = str(direction.get("emote_id") or "").strip()
            if emote and not emote.startswith(("emote:", "shabutton:")):
                emote = "emote:" + emote.zfill(2)
            route = (generation.get("route_decision") or {}).get("actual_treatment")
            route = {
                "screenshot_direct": "screenshot",
                "screenshot_crop": "screenshot",
                "cpa_redraw": "ai_scene",
                "cpa_polish": "ai_scene",
                "screenshot_polish": "ai_scene",
            }.get(route, route)
            if route not in {
                "screenshot",
                "screenshot_collage",
                "emote",
                "cutout",
                "ai_sticker",
                "ai_scene",
            }:
                route = (
                    "emote"
                    if emote
                    else "ai_scene"
                    if generation.get("method")
                    in {"cpa.images.edit", "images.edit", "image_gen.imagegen"}
                    else "screenshot"
                )
            created = record.get("created_at") or record.get("generated_at")
            basis = "RECORD_CREATED_AT"
            if not isinstance(created, str):
                created = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                basis = "LEGACY_FILE_MTIME"
            rows[key] = (
                created,
                path,
                source_session_key(record),
                {"route": route, "main_emote": emote or None},
                basis,
            )
    ordered = sorted(rows.values(), key=lambda row: (row[0], str(row[1])))
    return {
        "schema_version": "lidousha-title-cover-history.v1",
        "source_session": source_session,
        "previous_route": ordered[-1][3]["route"] if ordered else None,
        "last_five_main_emotes": [
            row[3]["main_emote"] for row in ordered[-5:] if row[3].get("main_emote")
        ],
        "source_session_main_emotes": [
            row[3]["main_emote"]
            for row in ordered
            if row[3].get("main_emote")
            and (
                source_session == "UNRESOLVED" or row[2] == "UNRESOLVED" or row[2] == source_session
            )
        ],
        "inputs": [
            {"path": str(row[1]), "sha256": sha(row[1]), "order_basis": row[4]} for row in ordered
        ],
    }
