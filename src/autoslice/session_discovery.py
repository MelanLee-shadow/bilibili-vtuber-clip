"""Recording-session discovery and deterministic song-name enrichment.

The live runner owns paths, environment policy, and patchable media probes. They
are resolved lazily so discovery remains reusable without changing the runner's
existing test and manual-repair seam.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from pathlib import Path

from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice.segment_scene_context import resolve_segment_scene_context
from src.autoslice.story_contract import canonicalize_relation_summary


_runner = RunnerProxy()
_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}")

# Visual song discovery is optional enrichment.  A failed inventory gets one
# later-tick retry after the segment itself is durable; a second failure is
# terminal for this lane so a date cannot be held open forever.
VISUAL_RETRY_MAX_ATTEMPTS = 2
VISUAL_RETRY_METADATA_KEY = "visual_retry"
VISUAL_RETRY_DUE = "DUE"
VISUAL_RETRY_SUCCEEDED = "SUCCEEDED"
VISUAL_RETRY_EXHAUSTED = "EXHAUSTED"
_SONG_VISUAL_COLLECTIONS = (
    "pending_song",
    "song_backlog",
    "song_selection_backlog",
    "songs",
)


def _verified_state_source_sha256(state: dict, segment: Path) -> str | None:
    """Return a persisted whole-source hash only under its recovery authority.

    Ordinary recorder state has no whole-file hash and must keep the relation
    binding basename-only.  Recovery state may name the official complete
    replay, but only after the source-inventory gate has passed and the same
    recording basename remains in that audited inventory.
    """

    value = str(state.get("source_sha256") or "")
    if _SHA256_RX.fullmatch(value) is None:
        return None
    if state.get("source_authority") != "OFFICIAL_COMPLETE_REPLAY":
        return None
    inventory = state.get("source_integrity")
    if (
        not isinstance(inventory, dict)
        or inventory.get("status") != "PASS"
        or inventory.get("can_select") is not True
        or inventory.get("issues") not in ([], None)
    ):
        return None
    consumer_segments = inventory.get("consumer_segments")
    if not isinstance(consumer_segments, list) or segment.name not in {
        Path(str(path)).name for path in consumer_segments
    }:
        return None
    return value


def _session_relation_for_state(
    date: str, segment: Path, state: dict
) -> dict[str, object] | None:
    source_sha256 = _verified_state_source_sha256(state, segment)
    if source_sha256 is None:
        return _runner.session_relation_for_segment(date, segment)
    return _runner.session_relation_for_segment(
        date,
        segment,
        source_sha256=source_sha256,
    )


def _normalized_live_start_id(raw: object) -> str | None:
    """Turn recorder live-start metadata into a stable quota identity."""

    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"live-{parsed.strftime('%Y%m%dT%H%M%S%z')}"


def recording_session_id(segment: Path, date: str) -> str:
    """Read the recorder's authoritative live start for one media segment.

    Every reconnect/rotated file from one broadcast carries the same
    ``LiveStartTime``.  The MP4 ``date`` tag is a fallback for older sidecars.
    Missing metadata deliberately collapses into one unknown session so the
    runner under-delivers instead of inventing extra quotas.
    """

    meta_path = segment.with_suffix(".meta.json")
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if isinstance(payload, dict):
        description = payload.get("description")
        candidates = [
            description.get("LiveStartTime") if isinstance(description, dict) else None,
            payload.get("LiveStartTime"),
            payload.get("Date"),
        ]
        for candidate in candidates:
            session_id = _normalized_live_start_id(candidate)
            if session_id:
                return session_id

    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format_tags=date",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(segment),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if completed is not None and completed.returncode == 0:
        session_id = _normalized_live_start_id(completed.stdout.strip())
        if session_id:
            return session_id
    return f"live-{date.replace('-', '')}Tunknown"


def annotate_state_sessions(
    date: str,
    state: dict,
    *,
    include_song_rows: bool = True,
    talk_candidate_ids: Collection[str] | None = None,
) -> bool:
    """Migrate rows onto live sessions without crossing a frozen Talk scope."""

    changed = False
    mapping = state.setdefault("segment_sessions", {})
    if not isinstance(mapping, dict):
        mapping = {}
        state["segment_sessions"] = mapping
        changed = True
    relation_mapping = state.setdefault("segment_relation_authorities", {})
    if not isinstance(relation_mapping, dict):
        relation_mapping = {}
        state["segment_relation_authorities"] = relation_mapping
        changed = True
    scene_mapping = state.setdefault("segment_scene_contexts", {})
    if not isinstance(scene_mapping, dict):
        scene_mapping = {}
        state["segment_scene_contexts"] = scene_mapping
        changed = True
    for segment in _runner.list_segments(date):
        if not mapping.get(segment.stem):
            session_id = recording_session_id(segment, date)
            mapping[segment.stem] = session_id
            changed = True
        scene = resolve_segment_scene_context(
            segment,
            xml_path=_runner.find_danmaku_xml(segment),
            cached=scene_mapping.get(segment.stem),
        )
        if scene_mapping.get(segment.stem) != scene:
            scene_mapping[segment.stem] = scene
            changed = True
        relation = _session_relation_for_state(date, segment, state)
        if relation is not None and relation_mapping.get(segment.stem) != relation:
            relation_mapping[segment.stem] = relation
            changed = True
        if relation is None and segment.stem in relation_mapping:
            del relation_mapping[segment.stem]
            changed = True

    unique_relations = {
        str(relation.get("relation_id") or ""): relation
        for relation in relation_mapping.values()
        if isinstance(relation, dict) and relation.get("relation_id")
    }
    relation_summary: dict[str, object] | None
    if len(unique_relations) == 1:
        relation_summary = next(iter(unique_relations.values()))
    elif len(unique_relations) > 1:
        relation_summary = {
            "state": "MULTIPLE",
            "relation_ids": sorted(unique_relations),
        }
    else:
        relation_summary = None
    if relation_summary is None:
        if "session_relation_authority" in state:
            del state["session_relation_authority"]
            changed = True
    elif state.get("session_relation_authority") != relation_summary:
        state["session_relation_authority"] = relation_summary
        changed = True

    talk_collections = (
        "picks",
        "pending_talk",
        "talk_backlog",
        "talk_superseded_attempts",
    )
    collections = talk_collections
    if include_song_rows:
        collections += (
            "songs",
            "pending_song",
            "song_backlog",
            "song_superseded_attempts",
        )
    for collection in collections:
        for row in state.get(collection, []):
            if not isinstance(row, dict):
                continue
            if talk_candidate_ids is not None and collection in talk_collections:
                candidate_id = str(
                    row.get("cid") or row.get("candidate_id") or ""
                ).strip()
                if candidate_id not in talk_candidate_ids:
                    continue
            segment_value = row.get("segment_path") or row.get("segment")
            if not segment_value:
                continue
            segment_path = Path(str(segment_value))
            scene = scene_mapping.get(segment_path.stem)
            if scene is None and segment_path.is_file():
                scene = resolve_segment_scene_context(
                    segment_path,
                    xml_path=_runner.find_danmaku_xml(segment_path),
                )
                scene_mapping[segment_path.stem] = scene
                changed = True
            if isinstance(scene, dict) and row.get("segment_scene_context") != scene:
                row["segment_scene_context"] = scene
                changed = True
            elif scene is None and "segment_scene_context" in row:
                del row["segment_scene_context"]
                changed = True
            relation = relation_mapping.get(segment_path.stem)
            if isinstance(relation, dict) and row.get(
                "session_relation_authority"
            ) != relation:
                row["session_relation_authority"] = relation
                changed = True
            elif relation is None and "session_relation_authority" in row:
                del row["session_relation_authority"]
                changed = True
            if row.get("session_id"):
                continue
            session_id = mapping.get(segment_path.stem)
            if not session_id and segment_path.is_file():
                session_id = recording_session_id(segment_path, date)
                mapping[segment_path.stem] = session_id
                changed = True
            if session_id:
                row["session_id"] = session_id
                changed = True

    recording_sessions = sorted(set(mapping.values()))
    if recording_sessions and state.get("recording_sessions") != recording_sessions:
        state["recording_sessions"] = recording_sessions
        changed = True
    return changed


def date_chat_jsonl_files(date: str) -> list[Path]:
    """All structured live-event sidecars for a date's recordings, deduped.

    Reuses the same segment→sidecar lookup as segment discovery (``find_chat_jsonl``)
    instead of re-globbing the recordings root, so it stays consistent with
    whatever a test's ``list_segments``/``REC_ROOT`` monkeypatch already covers.
    """

    seen: dict[str, Path] = {}
    for segment in _runner.list_segments(date):
        jsonl = _runner.find_chat_jsonl(segment)
        if jsonl is not None:
            seen.setdefault(str(jsonl.resolve()), jsonl)
    return list(seen.values())


def _clean_dian_ge_title(raw: str) -> str | None:
    title = _runner._TRAILING_PUNCT_RX.sub("", str(raw or "").strip())
    if not title or not (1 <= len(title) <= 20):
        return None
    return title


def dian_ge_song_titles(date: str, *, cap: int = 40) -> list[str]:
    """点歌 danmaku pool: titles a viewer explicitly requested by name.

    REUSES ``src.autoslice.chat_authority.load_chat_jsonl`` (the same
    authoritative structured danmaku/SC parser the finalization lane uses,
    including its recording-epoch handling) instead of a second bilibili
    DANMU_MSG decoder.
    """

    from src.autoslice.chat_authority import load_chat_jsonl

    titles: list[str] = []
    seen_norm: set[str] = set()
    for jsonl_path in _runner.date_chat_jsonl_files(date):
        try:
            evidence = load_chat_jsonl(jsonl_path)
        except (OSError, ValueError):
            continue
        for item in evidence:
            if item.kind != "danmaku":
                continue
            match = _runner._DIAN_GE_RX.match(item.text.strip())
            if not match:
                continue
            title = _runner._clean_dian_ge_title(match.group(1))
            if title is None:
                continue
            norm = title.lower()
            if norm in seen_norm:
                continue
            seen_norm.add(norm)
            titles.append(title)
            if len(titles) >= cap:
                return titles
    return titles


def visual_song_titles(state: dict) -> list[str]:
    """Screen-songlist titles seen anywhere in the date so far.

    The numbered songlist overlay (right-top panel) persists on screen across
    recording segments, so every segment's inventory entry — plus the
    cross-segment dedup identities already tracked in
    ``visual_song_seen_entries`` (``list:<n>:<title>`` / ``media:<stem>:<start_ms>:<title>``) —
    is in scope, not just the current segment's.
    """

    titles: list[str] = []
    seen_norm: set[str] = set()

    def _add(raw_title) -> None:
        title = str(raw_title or "").strip()
        if not title:
            return
        norm = _runner.normalize_visual_title(title)
        if not norm or norm in seen_norm:
            return
        seen_norm.add(norm)
        titles.append(title)

    inventory = state.get("visual_song_inventory")
    if isinstance(inventory, dict):
        for entry in inventory.values():
            if not isinstance(entry, dict):
                continue
            for candidate in entry.get("candidates") or []:
                if isinstance(candidate, dict):
                    _add(candidate.get("song_title"))
    for entry in state.get("visual_song_seen_entries") or []:
        if not isinstance(entry, str):
            continue
        if entry.startswith("list:"):
            parts = entry.split(":", 2)
            if len(parts) == 3:
                _add(parts[2])
        elif entry.startswith("media:"):
            parts = entry.split(":", 3)
            if len(parts) == 4:
                _add(parts[3])
    return titles


def known_song_titles() -> list[str]:
    """Curated recurring-song titles from the selected profile asset.

    This is a code-owned static asset, not a per-candidate human-truth file —
    unlike ``candidate_text_override_path``/``candidate_subtitle_regression_path``/
    ``candidate_speaker_override_path`` it has no ``human_truth_mode() ==
    "withheld"`` gate anywhere in this codebase (it is already used
    unconditionally for song-lane fingerprint pinning in
    ``run_auto_review_shadow_pipeline._load_known_songs``), so it is safe to
    include in both delivery and withheld runs.
    """

    path = _runner.profile_asset_file("known_songs")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    songs = payload.get("songs") if isinstance(payload, dict) else None
    if not isinstance(songs, list):
        return []
    titles: list[str] = []
    for song in songs:
        if isinstance(song, dict):
            title = str(song.get("title") or "").strip()
            if title:
                titles.append(title)
    return titles


def collect_song_name_candidates(date: str, state: dict, *, cap: int = 60) -> list[str]:
    """Machine-evidence song-name pool for the talk lane's deterministic pin
    (维护者 — 7/11 delivery bug: ``下一首歌是爱拉拉爱`` instead of
    《爱啦啦》 with zero song-name context available to the correction lanes).

    Sources, in priority order: the screen songlist panel, 点歌 danmaku, then
    the curated known-songs table.  Every source here is machine evidence (or
    a code-owned asset) — never 维护者 human-truth review, so this is safe in
    both delivery and withheld/blind runs.
    """

    titles: list[str] = []
    seen_norm: set[str] = set()

    def _extend(source: list[str]) -> None:
        for title in source:
            norm = _runner.normalize_visual_title(title)
            if not norm or norm in seen_norm:
                continue
            seen_norm.add(norm)
            titles.append(title)

    _extend(_runner.visual_song_titles(state))
    _extend(_runner.dian_ge_song_titles(date))
    _extend(_runner.known_song_titles())
    return titles[:cap]


def visual_song_config_from_env() -> _runner.VisualSongConfig:
    """Malformed optional tuning cannot disable the independent ASR lane."""

    try:
        sample_seconds = int(os.environ.get("AUTOSLICE_VISUAL_SONG_SAMPLE_SECONDS", "10"))
        timeout_seconds = int(os.environ.get("AUTOSLICE_VISUAL_SONG_TIMEOUT_SECONDS", "900"))
    except ValueError:
        sample_seconds, timeout_seconds = 10, 900
    return _runner.VisualSongConfig(
        sample_every_seconds=max(5, sample_seconds),
        timeout_seconds=max(60, timeout_seconds),
    )


def _discovery_state_collections(state: dict) -> tuple:
    """Return fresh collection references after a persistence callback.

    The normal state writer replaces a tracked state mapping with a deep copy
    after a successful write.  Local aliases therefore must be rebound before
    the next segment, or its mutations would be lost from the durable mapping.
    """

    return (
        set(state.setdefault("segments_done", [])),
        state.setdefault("segments_dead", {}),
        state.setdefault("bcut_attempts", {}),
        state.setdefault("pending_talk", []),
        state.setdefault("pending_song", []),
        state.setdefault("visual_song_inventory", {}),
        set(state.setdefault("visual_song_seen_entries", [])),
        state.setdefault("segment_durations_ms", {}),
    )


def _checkpoint_state(persist_state: Callable[[], None] | None) -> None:
    """Persist one internally consistent discovery transition, if requested.

    The callback deliberately runs outside a catch block: a failed write must
    stop discovery before the next segment can mutate the in-memory state.
    """

    if persist_state is not None:
        persist_state()


def _positive_int(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _visual_retry_state(manifest: Mapping[str, object]) -> tuple[int, bool]:
    """Return ``(attempts, exhausted)`` for one failed inventory.

    A manifest from before retry metadata existed is conservatively one prior
    attempt.  Unknown or malformed explicit metadata fails closed at the
    two-attempt ceiling rather than creating an unbounded provider loop.
    """

    if str(manifest.get("status") or "").upper() != "FAILED":
        return 0, False
    metadata = manifest.get(VISUAL_RETRY_METADATA_KEY)
    if isinstance(metadata, Mapping):
        raw_attempts = metadata.get("attempts", metadata.get("attempt_count"))
        if raw_attempts is None:
            attempts = 1
        elif isinstance(raw_attempts, bool) or not isinstance(raw_attempts, int):
            return VISUAL_RETRY_MAX_ATTEMPTS, True
        else:
            attempts = max(1, min(raw_attempts, VISUAL_RETRY_MAX_ATTEMPTS))
        metadata_status = str(metadata.get("status") or "").upper()
        exhausted = bool(metadata.get("exhausted") is True) or metadata_status in {
            VISUAL_RETRY_EXHAUSTED,
            VISUAL_RETRY_SUCCEEDED,
        }
        if metadata.get("due") is False:
            exhausted = True
    else:
        # A few short-lived pre-schema experiments used top-level retry hints;
        # read them for migration, while the canonical writer uses the nested
        # object above.
        raw_attempts = next(
            (
                manifest.get(key)
                for key in ("visual_retry_attempts", "retry_attempts", "retry_count")
                if key in manifest
            ),
            None,
        )
        if raw_attempts is None and not any(
            manifest.get(key) is True
            for key in ("visual_retry_exhausted", "retry_exhausted")
        ):
            return 1, False
        if raw_attempts is None:
            attempts = VISUAL_RETRY_MAX_ATTEMPTS
        elif isinstance(raw_attempts, bool) or not isinstance(raw_attempts, int):
            return VISUAL_RETRY_MAX_ATTEMPTS, True
        else:
            attempts = max(1, min(raw_attempts, VISUAL_RETRY_MAX_ATTEMPTS))
        exhausted = bool(
            attempts >= VISUAL_RETRY_MAX_ATTEMPTS
            or any(
                manifest.get(key) is True
                for key in ("visual_retry_exhausted", "retry_exhausted")
            )
        )
    return attempts, exhausted or attempts >= VISUAL_RETRY_MAX_ATTEMPTS


def visual_song_retry_due(
    state: Mapping[str, object], *, segment_stems: Collection[str] | None = None
) -> bool:
    """Whether a durable done segment has one visual-only retry remaining."""

    done = {str(stem) for stem in (state.get("segments_done") or [])}
    allowed = {str(stem) for stem in segment_stems} if segment_stems is not None else None
    inventory = state.get("visual_song_inventory")
    if not isinstance(inventory, Mapping):
        return False
    for raw_stem, raw_manifest in inventory.items():
        stem = str(raw_stem)
        if stem not in done or (allowed is not None and stem not in allowed):
            continue
        if not isinstance(raw_manifest, Mapping):
            continue
        attempts, exhausted = _visual_retry_state(raw_manifest)
        if (
            str(raw_manifest.get("status") or "").upper() == "FAILED"
            and not exhausted
            and attempts < VISUAL_RETRY_MAX_ATTEMPTS
        ):
            return True
    return False


def _with_visual_retry_metadata(
    manifest: Mapping[str, object], *, attempts: int, status: str
) -> dict[str, object]:
    updated = dict(manifest)
    bounded_attempts = max(1, min(int(attempts), VISUAL_RETRY_MAX_ATTEMPTS))
    updated[VISUAL_RETRY_METADATA_KEY] = {
        "attempts": bounded_attempts,
        "max_attempts": VISUAL_RETRY_MAX_ATTEMPTS,
        "status": status,
        "due": status == VISUAL_RETRY_DUE,
        "exhausted": status == VISUAL_RETRY_EXHAUSTED,
    }
    return updated


def _manifest_from_visual_result(result: object) -> dict[str, object]:
    try:
        raw = result.to_manifest()
    except Exception:  # noqa: BLE001 - optional result shape must fail open
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _safe_visual_retry_error(result: object, *, fallback: str) -> str:
    """Keep retry state errors structural; never persist provider text/secrets."""

    error = getattr(result, "error", None)
    if not isinstance(error, str):
        return fallback
    value = error.strip()
    if not value or len(value) > 512 or re.search(
        r"(?i)(api[_ -]?key|secret|token|password|credential)", value
    ):
        return fallback
    if value.startswith("GEMINI_VISUAL_SONG_DISCOVERY_FAILED"):
        return re.sub(r"[^A-Za-z0-9_.:;= -]", "_", value)[:512]
    return fallback


def _visual_candidate_identity(stem: str, candidate: object) -> str:
    title = str(getattr(candidate, "song_title", "") or "").strip()
    normalized = _runner.normalize_visual_title(title)
    list_index = getattr(candidate, "list_index", None)
    if list_index is None:
        evidence = getattr(candidate, "evidence", {})
        if isinstance(evidence, Mapping):
            list_index = evidence.get("list_index")
    if isinstance(list_index, int) and not isinstance(list_index, bool) and list_index > 0:
        return f"list:{list_index}:{normalized}"
    start_ms = _positive_int(getattr(candidate, "start_ms", 0))
    return f"media:{stem}:{start_ms}:{normalized}"


def _visual_row_identity(row: Mapping[str, object]) -> str | None:
    evidence = row.get("visual_song_evidence")
    if not isinstance(evidence, Mapping) and row.get("lane") not in {
        "visual_song_inventory",
        "visual_song_list",
    }:
        return None
    evidence = evidence if isinstance(evidence, Mapping) else {}
    title = str(evidence.get("song_title") or row.get("title_hint") or "").strip()
    normalized = _runner.normalize_visual_title(title)
    if not normalized:
        return None
    nested_evidence = evidence.get("evidence")
    list_index = (
        nested_evidence.get("list_index")
        if isinstance(nested_evidence, Mapping)
        else None
    )
    if isinstance(list_index, int) and not isinstance(list_index, bool) and list_index > 0:
        return f"list:{list_index}:{normalized}"
    segment_value = row.get("segment_path") or row.get("segment")
    stem = Path(str(segment_value)).stem if segment_value else ""
    start_ms = _positive_int(evidence.get("start_ms"), default=_positive_int(row.get("anchor_start_ms")))
    return f"media:{stem}:{start_ms}:{normalized}"


def _visual_candidate_cid(segment_tag: str, candidate: object) -> str:
    title = str(getattr(candidate, "song_title", "") or "")
    title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
    start_ms = _positive_int(getattr(candidate, "start_ms", 0))
    return f"songvis_{segment_tag}_{start_ms // 1000}_{title_hash}"


def _song_state_rows(state: Mapping[str, object]) -> list[Mapping[str, object]]:
    rows: list[Mapping[str, object]] = []
    for collection in _SONG_VISUAL_COLLECTIONS + ("song_superseded_attempts",):
        values = state.get(collection)
        if isinstance(values, list):
            rows.extend(row for row in values if isinstance(row, Mapping))
    return rows


def _song_visual_target_entries(
    state: Mapping[str, object], stem: str
) -> list[tuple[str, int, dict[str, object]]]:
    """Return active song rows for a stem with their durable collection slots."""

    entries: list[tuple[str, int, dict[str, object]]] = []
    for collection in _SONG_VISUAL_COLLECTIONS:
        values = state.get(collection)
        if not isinstance(values, list):
            continue
        for index, row in enumerate(values):
            if not isinstance(row, Mapping):
                continue
            segment_value = row.get("segment_path") or row.get("segment")
            if not segment_value or Path(str(segment_value)).stem != stem:
                continue
            entries.append((collection, index, dict(row)))
    return entries


def _retry_failed_visual_songs(
    date: str, state: dict, *, persist_state: Callable[[], None] | None
) -> None:
    """Replay only failed visual inventory for already-done segments."""

    done = {str(stem) for stem in (state.get("segments_done") or [])}
    for segment in _runner.list_segments(date):
        stem = segment.stem
        if stem not in done:
            continue
        inventory = state.get("visual_song_inventory")
        manifest = inventory.get(stem) if isinstance(inventory, Mapping) else None
        if not isinstance(manifest, Mapping):
            continue
        attempts, exhausted = _visual_retry_state(manifest)
        if (
            str(manifest.get("status") or "").upper() != "FAILED"
            or exhausted
            or attempts >= VISUAL_RETRY_MAX_ATTEMPTS
        ):
            continue

        try:
            durations = state.get("segment_durations_ms")
            duration = (
                durations.get(stem)
                if isinstance(durations, Mapping)
                else None
            )
            duration_ms = _positive_int(duration)
            if duration_ms <= 0:
                duration_ms = _positive_int(_runner.ffprobe_ms(segment))
            if duration_ms <= 0:
                raise ValueError("visual retry duration unavailable")
            result = _runner.discover_visual_songs(
                segment,
                _runner.BASE / "cache" / date / "visual-song-inventory",
                duration_ms=duration_ms,
                config=_runner.visual_song_config_from_env(),
            )
        except Exception as exc:  # noqa: BLE001 - visual lane is fail-open
            failed = dict(manifest)
            failed.update(
                status="FAILED",
                candidates=[],
                cache_hit=False,
                error=f"VISUAL_RETRY_EXCEPTION:{type(exc).__name__}",
            )
            failed = _with_visual_retry_metadata(
                failed,
                attempts=VISUAL_RETRY_MAX_ATTEMPTS,
                status=VISUAL_RETRY_EXHAUSTED,
            )
            state.setdefault("visual_song_inventory", {})[stem] = failed
            _checkpoint_state(persist_state)
            continue

        result_status = str(getattr(result, "status", "FAILED") or "FAILED").upper()
        if result_status != "READY":
            failed = _manifest_from_visual_result(result)
            failed.update(
                status="FAILED",
                candidates=[],
                cache_hit=False,
                error=_safe_visual_retry_error(
                    result, fallback="VISUAL_RETRY_FAILED"
                ),
            )
            failed = _with_visual_retry_metadata(
                failed,
                attempts=VISUAL_RETRY_MAX_ATTEMPTS,
                status=VISUAL_RETRY_EXHAUSTED,
            )
            state.setdefault("visual_song_inventory", {})[stem] = failed
            _checkpoint_state(persist_state)
            continue

        visual_seen = {
            str(value)
            for value in (state.get("visual_song_seen_entries") or [])
            if isinstance(value, str)
        }
        existing_rows = _song_state_rows(state)
        existing_visual_ids = {
            identity
            for row in existing_rows
            if (identity := _visual_row_identity(row)) is not None
        }
        existing_cids = {
            str(row.get("cid") or row.get("candidate_id") or "")
            for row in existing_rows
            if row.get("cid") or row.get("candidate_id")
        }
        seg_tag = re.sub(r"\D", "", stem)[-6:]
        fresh_visual = []
        for candidate in getattr(result, "candidates", ()) or ():
            identity = _visual_candidate_identity(stem, candidate)
            if identity in visual_seen:
                continue
            visual_seen.add(identity)
            if identity in existing_visual_ids:
                continue
            if _visual_candidate_cid(seg_tag, candidate) in existing_cids:
                continue
            fresh_visual.append(candidate)

        target_entries = _song_visual_target_entries(state, stem)
        target_rows = [row for _, _, row in target_entries]
        combined = _runner.union_visual_song_candidates(
            target_rows,
            fresh_visual,
            segment_tag=seg_tag,
        )
        segment_xml = _runner.find_danmaku_xml(segment)
        sessions = state.setdefault("segment_sessions", {})
        if not isinstance(sessions, dict):
            sessions = {}
            state["segment_sessions"] = sessions
        session_id = sessions.get(stem)
        if not session_id:
            session_id = recording_session_id(segment, date)
            sessions[stem] = session_id
        for song_item in combined:
            song_item.setdefault("segment_path", str(segment))
            song_item.setdefault("seg_dur_ms", duration_ms)
            song_item.setdefault("xml", str(segment_xml) if segment_xml else None)
            song_item.setdefault("session_id", session_id)
            song_item.setdefault(
                "danmaku",
                _runner.danmaku_count_in(
                    str(segment_xml) if segment_xml else None,
                    int(song_item["anchor_start_ms"]),
                    int(song_item["anchor_end_ms"]),
                ),
            )

        existing_target_cids = {
            str(row.get("cid") or row.get("candidate_id") or "")
            for row in target_rows
            if row.get("cid") or row.get("candidate_id")
        }
        for song_item in combined:
            cid = str(song_item.get("cid") or song_item.get("candidate_id") or "")
            if cid and cid not in existing_target_cids:
                _runner._remember_song_quarantine_interval(state, song_item)

        # A semantic song can already have moved beyond pending_song by the
        # time visual enrichment retries.  Union once across all active song
        # collections, then write each matched row back to its original slot.
        # Only the truly unmatched suffix is appended, exactly once, to the
        # pending queue.
        target_count = len(target_entries)
        for (collection, index, _), song_item in zip(
            target_entries, combined[:target_count]
        ):
            values = state.get(collection)
            if isinstance(values, list) and index < len(values):
                values[index] = song_item
        unmatched = combined[target_count:]
        if unmatched:
            pending = state.get("pending_song")
            if not isinstance(pending, list):
                pending = []
                state["pending_song"] = pending
            pending.extend(unmatched)
        state["visual_song_seen_entries"] = sorted(visual_seen)
        ready_manifest = _manifest_from_visual_result(result)
        ready_manifest = _with_visual_retry_metadata(
            ready_manifest,
            attempts=VISUAL_RETRY_MAX_ATTEMPTS,
            status=VISUAL_RETRY_SUCCEEDED,
        )
        state.setdefault("visual_song_inventory", {})[stem] = ready_manifest
        _checkpoint_state(persist_state)


def discover_segments(
    date: str,
    state: dict,
    *,
    persist_state: Callable[[], None] | None = None,
) -> None:
    """Phase A: transcribe + recall new segments into pending queues."""
    _retry_failed_visual_songs(date, state, persist_state=persist_state)
    (
        done,
        dead,
        attempts,
        pending_talk,
        pending_song,
        visual_inventory,
        visual_seen,
        segment_durations_ms,
    ) = _discovery_state_collections(state)

    for segment in _runner.list_segments(date):
        stem = segment.stem
        if stem in done or stem in dead:
            continue
        try:
            size = segment.stat().st_size
        except OSError:
            continue
        if size < _runner.MIN_SEGMENT_BYTES:
            dead[stem] = f"stub_too_small({size}B)"
            _runner.log(f"segment {segment.name}: dead stub ({size}B), skipping forever")
            _checkpoint_state(persist_state)
            (
                done,
                dead,
                attempts,
                pending_talk,
                pending_song,
                visual_inventory,
                visual_seen,
                segment_durations_ms,
            ) = _discovery_state_collections(state)
            continue
        srt = _runner.bcut_transcribe(segment, date)
        if srt is None:
            attempts[stem] = attempts.get(stem, 0) + 1
            if attempts[stem] >= _runner.BCUT_MAX_ATTEMPTS:
                dead[stem] = f"bcut_failed_x{attempts[stem]}"
                _runner.log(f"segment {segment.name}: BCUT failed {attempts[stem]}x → dead")
            _checkpoint_state(persist_state)
            (
                done,
                dead,
                attempts,
                pending_talk,
                pending_song,
                visual_inventory,
                visual_seen,
                segment_durations_ms,
            ) = _discovery_state_collections(state)
            continue
        xml = _runner.find_danmaku_xml(segment)
        scene_mapping = state.setdefault("segment_scene_contexts", {})
        if not isinstance(scene_mapping, dict):
            scene_mapping = {}
            state["segment_scene_contexts"] = scene_mapping
        segment_scene = resolve_segment_scene_context(
            segment, xml_path=xml, cached=scene_mapping.get(stem)
        )
        scene_mapping[stem] = segment_scene
        verified_source_sha256 = _verified_state_source_sha256(state, segment)
        try:
            chat_binding = _runner.resolve_structured_chat_binding(
                segment,
                source_sha256=verified_source_sha256,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failures = state.setdefault("structured_chat_binding_failures", {})
            if not isinstance(failures, dict):
                failures = {}
                state["structured_chat_binding_failures"] = failures
            failures[stem] = str(exc)
            _runner.log(
                f"{segment.name}: structured chat binding BLOCKED ({exc})"
            )
            _checkpoint_state(persist_state)
            (
                done,
                dead,
                attempts,
                pending_talk,
                pending_song,
                visual_inventory,
                visual_seen,
                segment_durations_ms,
            ) = _discovery_state_collections(state)
            continue
        failures = state.get("structured_chat_binding_failures")
        if isinstance(failures, dict):
            failures.pop(stem, None)
        candidates, lane, extras = _runner.recall_candidates(srt, _runner.danmaku_hints(xml), xml)
        seg_dur = _runner.ffprobe_ms(segment)
        segment_durations_ms[stem] = seg_dur
        visual_result = _runner.discover_visual_songs(
            segment,
            _runner.BASE / "cache" / date / "visual-song-inventory",
            duration_ms=seg_dur,
            config=_runner.visual_song_config_from_env(),
        )
        visual_manifest = _manifest_from_visual_result(visual_result)
        visual_status = str(getattr(visual_result, "status", "FAILED") or "FAILED").upper()
        if visual_status == "FAILED":
            visual_manifest = _with_visual_retry_metadata(
                visual_manifest,
                attempts=1,
                status=VISUAL_RETRY_DUE,
            )
        visual_inventory[stem] = visual_manifest
        fresh_visual = []
        if visual_status == "READY":
            for visual_candidate in visual_result.candidates:
                # The numbered overlay is cumulative across recording
                # segments. Deduplicate a stable numbered row across the date
                # while still allowing an unnumbered/repeated performance at
                # another interval.
                identity = _visual_candidate_identity(stem, visual_candidate)
                if identity in visual_seen:
                    continue
                visual_seen.add(identity)
                fresh_visual.append(visual_candidate)
        state["visual_song_seen_entries"] = sorted(visual_seen)
        if visual_result.status == "FAILED":
            _runner.log(f"{segment.name}: visual song inventory failed open ({visual_result.error})")
        else:
            _runner.log(
                f"{segment.name}: visual song inventory {len(fresh_visual)} new / "
                f"{len(visual_result.candidates)} visible ({'cache' if visual_result.cache_hit else 'AGY High'})"
            )
        _runner.log(f"{segment.name}: {len(candidates)} candidate(s) via {lane}")
        seg_tag = re.sub(r"\D", "", stem)[-6:]
        recalled_song_items: list[dict] = []
        session_id = state.setdefault("segment_sessions", {}).get(stem)
        if not session_id:
            session_id = recording_session_id(segment, date)
            state["segment_sessions"][stem] = session_id
        session_relation = _session_relation_for_state(date, segment, state)
        if session_relation is not None:
            state["session_relation_authority"] = session_relation
        for cand in candidates:
            meta = extras.get(cand.anchor.candidate_id, {})
            selection_hook = canonicalize_relation_summary(
                str(meta.get("hook") or ""),
                session_relation_authority=session_relation,
            )
            raw_selection_scorecard = (
                dict(meta["selection_scorecard"])
                if isinstance(meta.get("selection_scorecard"), dict)
                else None
            )
            is_song_candidate = getattr(cand, "content_type_hint", "talk") == "song"
            if is_song_candidate:
                a0 = int(cand.anchor.anchor_start_ms)
                a1 = int(cand.anchor.anchor_end_ms)
                final_candidate_id = f"song_{seg_tag}_{a0 // 1000}"
            else:
                boundary = cand.boundary
                s0 = max(0, int(boundary.resolved_start_ms))
                s1 = (
                    min(seg_dur, int(boundary.resolved_end_ms))
                    if seg_dur
                    else int(boundary.resolved_end_ms)
                )
                final_candidate_id = (
                    f"auto_{seg_tag}_{s0 // 1000}_{s1 // 1000}"
                )
            from src.autoslice.selection_scorecard import (
                apply_reviewed_selection_calibration,
            )

            calibrated_selection_scorecard = apply_reviewed_selection_calibration(
                final_candidate_id,
                raw_selection_scorecard,
            )
            base_item = {
                "segment_path": str(segment),
                "seg_dur_ms": seg_dur,
                "xml": str(xml) if xml else None,
                **chat_binding,
                "hook": selection_hook,
                "confidence": meta.get("confidence"),
                "selection_scorecard": calibrated_selection_scorecard,
                "lane": lane,
                "preview": cand.text_preview[:80],
                "bcut_srt_path": str(srt),
                "session_id": session_id,
                "segment_scene_context": segment_scene,
                "session_relation_authority": session_relation,
                "filler_proposals": list(meta.get("filler_proposals") or []),
                "filler_proposal_srt_sha256": meta.get(
                    "filler_proposal_srt_sha256"
                ),
                "merge_gap_removals": list(meta.get("merge_gap_removals") or []),
            }
            if is_song_candidate:
                song_item = {
                    **base_item,
                    "cid": final_candidate_id,
                    "anchor_start_ms": a0,
                    "anchor_end_ms": a1,
                    "danmaku": _runner.danmaku_count_in(str(xml) if xml else None, a0, a1),
                }
                recalled_song_items.append(song_item)
            else:
                pending_talk.append({
                    **base_item,
                    "cid": final_candidate_id,
                    "start_ms": s0,
                    "end_ms": s1,
                })
        combined_song_items = _runner.union_visual_song_candidates(
            recalled_song_items,
            fresh_visual,
            segment_tag=seg_tag,
        )
        for song_item in combined_song_items:
            song_item.setdefault("segment_path", str(segment))
            song_item.setdefault("seg_dur_ms", seg_dur)
            song_item.setdefault("xml", str(xml) if xml else None)
            for key, value in chat_binding.items():
                song_item.setdefault(key, value)
            song_item.setdefault("session_id", session_id)
            song_item.setdefault(
                "danmaku",
                _runner.danmaku_count_in(
                    str(xml) if xml else None,
                    int(song_item["anchor_start_ms"]),
                    int(song_item["anchor_end_ms"]),
                ),
            )
            pending_song.append(song_item)
            _runner._remember_song_quarantine_interval(state, song_item)
        done.add(stem)
        state["segments_done"] = sorted(done)
        _checkpoint_state(persist_state)
        (
            done,
            dead,
            attempts,
            pending_talk,
            pending_song,
            visual_inventory,
            visual_seen,
            segment_durations_ms,
        ) = _discovery_state_collections(state)
    state["segments_done"] = sorted(done)
