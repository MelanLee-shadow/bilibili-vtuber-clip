"""Recording-session discovery and deterministic song-name enrichment.

The live runner owns paths, environment policy, and patchable media probes. They
are resolved lazily so discovery remains reusable without changing the runner's
existing test and manual-repair seam.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import sys as _sys


class _RunnerProxy:
    """Resolve the live runner module without importing it recursively."""

    def __getattr__(self, name):
        module = _sys.modules.get("scripts.free_session_autoslice") or _sys.modules.get("__main__")
        return getattr(module, name)


_runner = _RunnerProxy()


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
    """Curated recurring-song titles (assets/lidousha/known_songs.json).

    This is a code-owned static asset, not a per-candidate human-truth file —
    unlike ``candidate_text_override_path``/``candidate_subtitle_regression_path``/
    ``candidate_speaker_override_path`` it has no ``human_truth_mode() ==
    "withheld"`` gate anywhere in this codebase (it is already used
    unconditionally for song-lane fingerprint pinning in
    ``run_auto_review_shadow_pipeline._load_known_songs``), so it is safe to
    include in both delivery and withheld runs.
    """

    path = _runner.REPO_ROOT / "assets" / "lidousha" / "known_songs.json"
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
    (Ivan 2026-07-13 — 7/11 delivery bug: ``下一首歌是爱拉拉爱`` instead of
    《爱啦啦》 with zero song-name context available to the correction lanes).

    Sources, in priority order: the screen songlist panel, 点歌 danmaku, then
    the curated known-songs table.  Every source here is machine evidence (or
    a code-owned asset) — never Ivan human-truth review, so this is safe in
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


def discover_segments(date: str, state: dict) -> None:
    """Phase A: transcribe + recall new segments into pending queues."""
    done = set(state.setdefault("segments_done", []))
    dead = state.setdefault("segments_dead", {})
    attempts = state.setdefault("bcut_attempts", {})
    pending_talk = state.setdefault("pending_talk", [])
    pending_song = state.setdefault("pending_song", [])
    visual_inventory = state.setdefault("visual_song_inventory", {})
    visual_seen = set(state.setdefault("visual_song_seen_entries", []))

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
            continue
        srt = _runner.bcut_transcribe(segment, date)
        if srt is None:
            attempts[stem] = attempts.get(stem, 0) + 1
            if attempts[stem] >= _runner.BCUT_MAX_ATTEMPTS:
                dead[stem] = f"bcut_failed_x{attempts[stem]}"
                _runner.log(f"segment {segment.name}: BCUT failed {attempts[stem]}x → dead")
            continue
        xml = _runner.find_danmaku_xml(segment)
        chat_jsonl = _runner.find_chat_jsonl(segment)
        candidates, lane, extras = _runner.recall_candidates(srt, _runner.danmaku_hints(xml))
        seg_dur = _runner.ffprobe_ms(segment)
        visual_result = _runner.discover_visual_songs(
            segment,
            _runner.BASE / "cache" / date / "visual-song-inventory",
            duration_ms=seg_dur,
            config=_runner.visual_song_config_from_env(),
        )
        visual_inventory[stem] = visual_result.to_manifest()
        fresh_visual = []
        for visual_candidate in visual_result.candidates:
            # The numbered overlay is cumulative across recording segments.
            # Deduplicate a stable numbered row across the date while still
            # allowing an unnumbered/repeated performance at another interval.
            list_index = visual_candidate.list_index
            identity = (
                f"list:{list_index}:{_runner.normalize_visual_title(visual_candidate.song_title)}"
                if list_index is not None
                else f"media:{stem}:{visual_candidate.start_ms}:{_runner.normalize_visual_title(visual_candidate.song_title)}"
            )
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
        for cand in candidates:
            meta = extras.get(cand.anchor.candidate_id, {})
            base_item = {
                "segment_path": str(segment),
                "seg_dur_ms": seg_dur,
                "xml": str(xml) if xml else None,
                "chat_jsonl": str(chat_jsonl) if chat_jsonl else None,
                "hook": meta.get("hook", ""),
                "confidence": meta.get("confidence"),
                "lane": lane,
                "preview": cand.text_preview[:80],
                "bcut_srt_path": str(srt),
            }
            if getattr(cand, "content_type_hint", "talk") == "song":
                a0, a1 = int(cand.anchor.anchor_start_ms), int(cand.anchor.anchor_end_ms)
                song_item = {
                    **base_item,
                    "cid": f"song_{seg_tag}_{a0 // 1000}",
                    "anchor_start_ms": a0,
                    "anchor_end_ms": a1,
                    "danmaku": _runner.danmaku_count_in(str(xml) if xml else None, a0, a1),
                }
                recalled_song_items.append(song_item)
            else:
                b = cand.boundary
                s0 = max(0, int(b.resolved_start_ms))
                s1 = min(seg_dur, int(b.resolved_end_ms)) if seg_dur else int(b.resolved_end_ms)
                pending_talk.append({
                    **base_item,
                    "cid": f"auto_{seg_tag}_{s0 // 1000}_{s1 // 1000}",
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
            song_item.setdefault("chat_jsonl", str(chat_jsonl) if chat_jsonl else None)
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
