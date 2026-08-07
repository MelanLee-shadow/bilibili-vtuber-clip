"""Session game-context resolution from recorder metadata, chat, and drafts.

The 2026-08-07 鹅鸭杀 collab proved every static context lane blind to "what
game is this stream playing": the room title was unrelated prose, the live
area stayed 虚拟Singer, and the full-session danmaku never named the game —
yet the session transcripts were saturated with game-specific role names
(法医/警长/通灵者/复仇者/跟踪者).  This module resolves the session's game
from a reviewed per-game glossary registry plus deterministic evidence
counting, and exposes the matched game's terms as an occurrence-neutral
candidate block for subtitle correction prompts.

The resolved context is candidates only: it never proves a cue contains a
term and carries no mechanical mutation authority.  Unresolved sessions bind
nothing and the pipeline behaves exactly as before (fail-open by design —
a missing game context must never block production).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from src.autoslice.runtime_candidate_asset import bind_runtime_candidate_asset

GLOSSARY_SCHEMA = "vtuber-slice.game-glossary.v1"
CONTEXT_SCHEMA = "session-game-context.v1"
OCCURRENCE_POLICY = "GAME_TERM_EXISTS_NOT_CUE_OCCURRENCE_OR_MUTATION_AUTHORITY"
ENV_SUFFIX = "SESSION_GAME_CONTEXT"

# Detection needs several *distinct* characteristic surfaces so one stray
# common word (警长 also exists in 狼人杀) can never resolve a game alone.
DETECTION_MIN_DISTINCT = 3
DETECTION_MIN_TOTAL = 6

_MAX_XML_BYTES = 32 * 1024 * 1024
_MAX_TEXT_BYTES = 8 * 1024 * 1024
_SAFE_SURFACE = re.compile(r"^[0-9A-Za-z㐀-鿿 ・·\-]{1,32}$")
_TERM_KINDS = frozenset({"role", "role_community", "mechanic", "map", "faction"})
_STATUSES = frozenset(
    {"RESOLVED", "NO_MATCH", "AMBIGUOUS", "NO_GLOSSARY", "GLOSSARY_INVALID"}
)


class GameContextError(ValueError):
    pass


def _safe_surface(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise GameContextError(f"{label} must be a string")
    text = value.strip()
    if not _SAFE_SURFACE.fullmatch(text) or any(
        token in text.lower() for token in ("ignore ", "system prompt")
    ):
        raise GameContextError(f"{label} is not a safe term surface")
    return text


def validate_game_glossary(payload: object) -> dict[str, Any]:
    """Strictly validate the reviewed per-game glossary registry."""

    if not isinstance(payload, Mapping) or payload.get("schema_version") != GLOSSARY_SCHEMA:
        raise GameContextError("unsupported game glossary schema")
    if payload.get("occurrence_policy") != OCCURRENCE_POLICY:
        raise GameContextError("game glossary occurrence-neutral policy missing")
    games = payload.get("games")
    if not isinstance(games, list) or not 1 <= len(games) <= 8:
        raise GameContextError("game glossary games list is invalid")
    seen_ids: set[str] = set()
    normalized_games: list[dict[str, Any]] = []
    for index, game in enumerate(games):
        if not isinstance(game, Mapping):
            raise GameContextError(f"game {index} is not a mapping")
        if set(game) != {
            "game_id", "canonical", "aliases", "sources", "detection_surfaces", "terms",
        }:
            raise GameContextError(f"game {index} has unknown or missing fields")
        game_id = str(game.get("game_id") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", game_id) or game_id in seen_ids:
            raise GameContextError(f"game {index} id invalid or duplicated")
        seen_ids.add(game_id)
        canonical = _safe_surface(game["canonical"], label=f"game {game_id} canonical")
        aliases = game["aliases"]
        detection = game["detection_surfaces"]
        terms = game["terms"]
        sources = game["sources"]
        if not isinstance(aliases, list) or len(aliases) > 16:
            raise GameContextError(f"game {game_id} aliases invalid")
        if not isinstance(detection, list) or not 3 <= len(detection) <= 64:
            raise GameContextError(f"game {game_id} detection surfaces invalid")
        if not isinstance(terms, list) or not 1 <= len(terms) <= 128:
            raise GameContextError(f"game {game_id} terms invalid")
        if not isinstance(sources, list) or not sources:
            raise GameContextError(f"game {game_id} sources missing")
        for source in sources:
            if not isinstance(source, Mapping) or not str(source.get("url") or "").startswith(
                "https://"
            ):
                raise GameContextError(f"game {game_id} source lacks an https url")
        normalized_terms: list[dict[str, str]] = []
        seen_terms: set[str] = set()
        for t_index, term in enumerate(terms):
            if not isinstance(term, Mapping) or set(term) != {"surface", "kind"}:
                raise GameContextError(f"game {game_id} term {t_index} invalid fields")
            surface = _safe_surface(term["surface"], label=f"game {game_id} term {t_index}")
            kind = str(term["kind"])
            if kind not in _TERM_KINDS:
                raise GameContextError(f"game {game_id} term {surface} kind invalid")
            if surface in seen_terms:
                raise GameContextError(f"game {game_id} duplicate term {surface}")
            seen_terms.add(surface)
            normalized_terms.append({"surface": surface, "kind": kind})
        detection_surfaces = [
            _safe_surface(value, label=f"game {game_id} detection surface")
            for value in detection
        ]
        if len(set(detection_surfaces)) != len(detection_surfaces):
            raise GameContextError(f"game {game_id} duplicate detection surface")
        normalized_games.append(
            {
                "game_id": game_id,
                "canonical": canonical,
                "aliases": [
                    _safe_surface(value, label=f"game {game_id} alias") for value in aliases
                ],
                "sources": [dict(source) for source in sources],
                "detection_surfaces": detection_surfaces,
                "terms": normalized_terms,
            }
        )
    return {
        "schema_version": GLOSSARY_SCHEMA,
        "occurrence_policy": OCCURRENCE_POLICY,
        "games": normalized_games,
    }


def _count_hits(surfaces: Sequence[str], texts: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for surface in surfaces:
        total = sum(text.count(surface) for text in texts)
        if total:
            counts[surface] = total
    return counts


def resolve_session_game_context(
    glossary: Mapping[str, Any],
    *,
    recording_date: str,
    metadata_texts: Sequence[str],
    chat_texts: Sequence[str],
    draft_texts: Sequence[str],
    input_inventory_sha256: str,
    glossary_sha256: str,
) -> dict[str, Any]:
    """Deterministically resolve which registered game this session is playing.

    A room-title/area alias hit is strong standalone evidence.  Otherwise the
    session's structured chat plus selection/ASR drafts must contain at least
    ``DETECTION_MIN_DISTINCT`` distinct characteristic surfaces with at least
    ``DETECTION_MIN_TOTAL`` combined occurrences.  Two qualifying games make
    the session AMBIGUOUS and nothing is injected.
    """

    resolved: list[dict[str, Any]] = []
    for game in glossary["games"]:
        alias_surfaces = [game["canonical"], *game["aliases"]]
        metadata_hits = _count_hits(alias_surfaces, metadata_texts)
        alias_hits = _count_hits(alias_surfaces, [*chat_texts, *draft_texts])
        detection_hits = _count_hits(game["detection_surfaces"], [*chat_texts, *draft_texts])
        distinct = len(detection_hits)
        total = sum(detection_hits.values())
        qualifies = bool(metadata_hits) or (
            distinct >= DETECTION_MIN_DISTINCT and total >= DETECTION_MIN_TOTAL
        )
        if not qualifies:
            continue
        resolved.append(
            {
                "game_id": game["game_id"],
                "canonical": game["canonical"],
                "aliases": list(game["aliases"]),
                "terms": [dict(term) for term in game["terms"]],
                "evidence": {
                    "metadata_alias_hits": metadata_hits,
                    "chat_or_draft_alias_hits": alias_hits,
                    "detection_surface_hits": detection_hits,
                    "distinct_detection_surfaces": distinct,
                    "total_detection_hits": total,
                },
            }
        )
    if not resolved:
        status, selected = "NO_MATCH", None
    elif len(resolved) > 1:
        status, selected = "AMBIGUOUS", None
    else:
        status, selected = "RESOLVED", resolved[0]
    context: dict[str, Any] = {
        "schema_version": CONTEXT_SCHEMA,
        "occurrence_policy": OCCURRENCE_POLICY,
        "recording_date": str(recording_date),
        "status": status,
        "glossary_sha256": glossary_sha256,
        "input_inventory_sha256": input_inventory_sha256,
        "qualifying_game_ids": [row["game_id"] for row in resolved],
    }
    if selected is not None:
        context["game"] = selected
    return context


def validate_session_game_context(payload: object) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or payload.get("schema_version") != CONTEXT_SCHEMA:
        raise GameContextError("unsupported session game context schema")
    if payload.get("occurrence_policy") != OCCURRENCE_POLICY:
        raise GameContextError("session game context occurrence policy missing")
    status = str(payload.get("status") or "")
    if status not in _STATUSES:
        raise GameContextError("session game context status invalid")
    if status == "RESOLVED":
        game = payload.get("game")
        if not isinstance(game, Mapping) or not isinstance(game.get("terms"), list):
            raise GameContextError("resolved session game context lacks a game payload")
        for term in game["terms"]:
            if not isinstance(term, Mapping):
                raise GameContextError("resolved game term invalid")
            _safe_surface(term.get("surface"), label="resolved game term")
    return dict(payload)


def render_game_glossary_context(context: Mapping[str, Any]) -> str:
    """Render the resolved game's terms as an occurrence-neutral prompt block."""

    if context.get("status") != "RESOLVED":
        return ""
    game = context["game"]
    by_kind: dict[str, list[str]] = {}
    for term in game["terms"]:
        by_kind.setdefault(str(term["kind"]), []).append(str(term["surface"]))
    kind_labels = {
        "role": "官方角色/职业",
        "role_community": "社区叫法角色",
        "mechanic": "机制/黑话",
        "map": "地图/场景",
        "faction": "阵营",
    }
    lines = [
        (
            "本场直播游戏语境（会话级证据检测；候选闭集只证明词面存在，绝不证明本句出现，"
            "也没有机械改字权限；逐处仍须本句音频、结构化弹幕/SC 与语境仲裁）:"
        ),
        f"- 游戏: {game['canonical']}"
        + (f"（{'/'.join(game['aliases'])}）" if game["aliases"] else ""),
    ]
    for kind in ("faction", "role", "role_community", "mechanic", "map"):
        surfaces = by_kind.get(kind)
        if surfaces:
            lines.append(f"- {kind_labels[kind]}: " + "、".join(surfaces))
    return "\n".join(lines) + "\n"


def _iter_recorder_xml_texts(path: Path) -> tuple[list[str], list[str]]:
    """Return (metadata texts, chat texts) from one recorder danmaku XML.

    Both the blrec layout (``metadata/room_title``, ``metadata/area``) and the
    录播姬/BililiveRecorder layout (``BililiveRecorderRecordInfo`` attributes)
    are read; either recorder may have produced a given session.
    """

    root = ElementTree.fromstring(path.read_text(encoding="utf-8", errors="replace"))
    metadata: list[str] = []
    for value in (
        root.findtext("./metadata/room_title"),
        root.findtext("./metadata/area"),
        root.findtext("./metadata/parent_area"),
    ):
        if value and str(value).strip():
            metadata.append(str(value).strip())
    info = root.find("./BililiveRecorderRecordInfo")
    if info is not None:
        for key in ("title", "areanameparent", "areanamechild"):
            value = str(info.get(key) or "").strip()
            if value:
                metadata.append(value)
    chats = [str(node.text or "").strip() for node in root.iter("d") if node.text]
    return metadata, chats


def _readable_files(paths: Iterable[Path], *, max_bytes: int) -> list[Path]:
    out: list[Path] = []
    for path in sorted(paths):
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= max_bytes:
                out.append(path)
        except OSError:
            continue
    return out


def _input_inventory_sha256(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{path.name}\n{stat.st_size}\n{int(stat.st_mtime)}\n".encode())
    return digest.hexdigest()


def compute_session_game_context_state(
    *,
    recording_date: str,
    day_source_dir: Path,
    day_cache_dir: Path,
    glossary_path: Path,
    state_path: Path,
) -> Path | None:
    """Compute (or reuse) the per-session context state file, fail-open.

    The state is recomputed only when the (name, size, mtime) inventory of the
    day's danmaku XMLs and selection-draft SRTs changes, so live nights pay
    one XML sweep per new segment instead of one per runner tick.  Any IO or
    validation failure leaves the previous state untouched and returns it if
    still present; this lane must never take the runner down.
    """

    try:
        glossary_raw = glossary_path.read_bytes()
        glossary = validate_game_glossary(json.loads(glossary_raw))
    except (OSError, ValueError):
        return state_path if state_path.is_file() else None
    xml_paths = _readable_files(day_source_dir.glob("*.xml"), max_bytes=_MAX_XML_BYTES)
    draft_paths = _readable_files(day_cache_dir.glob("*.bcut.srt"), max_bytes=_MAX_TEXT_BYTES)
    inventory = _input_inventory_sha256([*xml_paths, *draft_paths])
    if state_path.is_file() and not state_path.is_symlink():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
            if (
                previous.get("input_inventory_sha256") == inventory
                and previous.get("glossary_sha256")
                == "sha256:" + hashlib.sha256(glossary_raw).hexdigest()
            ):
                return state_path
        except (OSError, ValueError):
            pass
    metadata_texts: list[str] = []
    chat_texts: list[str] = []
    for path in xml_paths:
        try:
            metadata, chats = _iter_recorder_xml_texts(path)
        except (OSError, ElementTree.ParseError):
            continue
        metadata_texts.extend(metadata)
        chat_texts.extend(chats)
    draft_texts: list[str] = []
    for path in draft_paths:
        try:
            draft_texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    context = resolve_session_game_context(
        glossary,
        recording_date=recording_date,
        metadata_texts=metadata_texts,
        chat_texts=chat_texts,
        draft_texts=draft_texts,
        input_inventory_sha256=inventory,
        glossary_sha256="sha256:" + hashlib.sha256(glossary_raw).hexdigest(),
    )
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = state_path.with_name(state_path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, state_path)
    except OSError:
        return state_path if state_path.is_file() else None
    return state_path


def bind_session_game_context(
    env: MutableMapping[str, str],
    *,
    recording_date: str,
    recordings_root: Path,
    cache_root: Path,
    state_root: Path,
    glossary_path: Path,
    truth_mode: str,
    selectors: Mapping[str, str],
) -> Path | None:
    """Ensure and env-bind the session game context for one recording date."""

    safe_date = re.sub(r"[^0-9-]", "", str(recording_date))[:10]
    if not safe_date:
        return None
    state_path = state_root / "session_game_context" / f"{safe_date}.json"
    if truth_mode != "withheld" and not selectors.get(f"AUTOSLICE_{ENV_SUFFIX}"):
        compute_session_game_context_state(
            recording_date=safe_date,
            day_source_dir=recordings_root / safe_date,
            day_cache_dir=cache_root / safe_date,
            glossary_path=glossary_path,
            state_path=state_path,
        )
    return bind_runtime_candidate_asset(
        env,
        selectors=selectors,
        truth_mode=truth_mode,
        env_suffix=ENV_SUFFIX,
        runtime_path=state_path,
        committed_path=state_path,
        digest_file=lambda path: hashlib.sha256(path.read_bytes()).hexdigest(),
    )
