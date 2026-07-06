#!/usr/bin/env python3
"""No-upload Li Dousha auto-review shadow daemon.

This is the production-safe bridge between the current automatic slice pipeline
and the P7 auto-review evidence gate:

1. Watch the latest `/app/Videos/<room>/<date>` directory.
2. Wait until every prepared slice has `.jingting.done`.
3. Build a symlink-only review package from the real production artifacts.
4. Run `run_auto_review_shadow_pipeline` in fail-closed shadow mode.
5. Persist a small state file so unchanged inputs are not replayed forever.

It deliberately has no upload path. Public publishing must remain gated by a
separate explicit AUTO_UPLOAD manifest + artifact-hash gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_auto_review_shadow_pipeline import _parse_srt, run_shadow_pipeline  # noqa: E402
from src.autoslice.full_session_candidate_selector import (  # noqa: E402
    select_fallback_session_candidates,
    select_full_session_candidates,
)
from src.autoslice.term_lexicon import load_discovered_term_lexicon  # noqa: E402
from src.autoslice.source_integrity import MediaSegmentObservation, build_source_range_ledger, plan_bilibili_replay_compensation  # noqa: E402

DEFAULT_ROOM = "22966160"
DEFAULT_VIDEOS_ROOT = Path("/app/Videos")
DEFAULT_REPORT_ROOT = Path("/app/reports/auto_review_shadow")


@dataclass(frozen=True)
class SliceArtifact:
    stem: str
    publish_json: Path
    video: Path
    cover: Path
    subtitle: Path
    evidence: Path
    jingting_srt: Path
    jingting_done: Path
    jingting_manifest: Path
    jingting_review_required: Path
    title: str
    prepared_at: str | None
    upload_enabled: bool | None
    duration_sec: float | None


@dataclass(frozen=True)
class LiveSourceRoute:
    stem: str
    source_video: Path
    source_srt: Path
    refined_srt: Path | None
    source_context_job: dict[str, Any]
    metadata_sources: tuple[str, ...]


RECORDING_COMPLETION_STATE_FILENAME = "recording_completion_state.json"
DEFAULT_RECORDING_QUIET_SECONDS = 300.0


def evaluate_recording_completion(
    previous: Mapping[str, Any] | None,
    *,
    size_bytes: int,
    now_epoch: float,
    min_quiet_seconds: float,
) -> tuple[bool, dict[str, Any]]:
    """A recording is complete once its file stops growing for a quiet window.

    Size-based on purpose: the Videos mount is cloud-backed and mtime is not
    updated live, so growth between two sweeps is the only trustworthy signal.
    """

    if not previous:
        return False, {"size_bytes": size_bytes, "observed_epoch": now_epoch, "reason": "first_observation"}
    prev_size = previous.get("size_bytes")
    prev_epoch = float(previous.get("observed_epoch") or 0.0)
    if prev_size != size_bytes:
        return False, {
            "size_bytes": size_bytes,
            "observed_epoch": now_epoch,
            "reason": "still_growing",
            "previous_size_bytes": prev_size,
        }
    quiet_seconds = now_epoch - prev_epoch
    if quiet_seconds < min_quiet_seconds:
        return False, {
            "size_bytes": size_bytes,
            "observed_epoch": prev_epoch,
            "reason": "quiet_window_short",
            "quiet_seconds": round(quiet_seconds, 1),
        }
    return True, {
        "size_bytes": size_bytes,
        "observed_epoch": prev_epoch,
        "reason": "stable",
        "quiet_seconds": round(quiet_seconds, 1),
    }


def detect_recording_completion(
    source_video: Path,
    *,
    state_path: Path,
    min_quiet_seconds: float = DEFAULT_RECORDING_QUIET_SECONDS,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now_epoch is None else now_epoch
    state = _read_json(state_path) if state_path.exists() else {}
    observations = state.get("observations") if isinstance(state.get("observations"), Mapping) else {}
    observations = dict(observations)
    key = str(source_video)
    size_bytes = source_video.stat().st_size if source_video.is_file() else -1
    previous = observations.get(key) if isinstance(observations.get(key), Mapping) else None
    complete, observation = evaluate_recording_completion(
        previous, size_bytes=size_bytes, now_epoch=now, min_quiet_seconds=min_quiet_seconds
    )
    observations[key] = {
        "size_bytes": observation["size_bytes"],
        "observed_epoch": observation["observed_epoch"],
    }
    state["observations"] = observations
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "complete": complete,
        "source_video": key,
        "min_quiet_seconds": min_quiet_seconds,
        **observation,
    }


def find_latest_date_dir(videos_root: Path, room_id: str) -> Path | None:
    room_root = videos_root / room_id
    if not room_root.is_dir():
        return None
    candidates = [path for path in room_root.iterdir() if path.is_dir() and path.name[:4].isdigit()]
    return max(candidates, key=lambda path: path.name) if candidates else None


def discover_slices(date_dir: Path) -> list[SliceArtifact]:
    slices: list[SliceArtifact] = []
    for publish_json in sorted(date_dir.glob("*s_*-.publish.json")):
        data = _read_json(publish_json)
        stem = publish_json.name[: -len(".publish.json")]
        video = _path_from_publish(data, "video_path", date_dir / f"{stem}.flv")
        cover = _path_from_publish(data, "cover_path", date_dir / f"{stem}.cover.png")
        subtitle = _path_from_publish(data, "subtitle_path", date_dir / "subtitles" / f"{stem}.srt")
        evidence = _path_from_publish(data, "evidence_path", date_dir / "evidence" / f"{stem}.evidence.json")
        slices.append(
            SliceArtifact(
                stem=stem,
                publish_json=publish_json,
                video=video,
                cover=cover,
                subtitle=subtitle,
                evidence=evidence,
                jingting_srt=date_dir / f"{stem}.jingting.srt",
                jingting_done=date_dir / f"{stem}.jingting.done",
                jingting_manifest=date_dir / f"{stem}.jingting.manifest.json",
                jingting_review_required=date_dir / f"{stem}.jingting.review-required.json",
                title=str(data.get("title") or stem),
                prepared_at=data.get("prepared_at") if isinstance(data.get("prepared_at"), str) else None,
                upload_enabled=data.get("upload_enabled") if isinstance(data.get("upload_enabled"), bool) else None,
                duration_sec=_probe_duration(video),
            )
        )
    return slices


def build_review_package(
    date_dir: Path,
    package_dir: Path,
    *,
    room_id: str,
    slices: Sequence[SliceArtifact] | None = None,
) -> dict[str, Any]:
    if slices is None:
        slices = discover_slices(date_dir)
    if package_dir.exists():
        shutil.rmtree(package_dir)
    (package_dir / "subtitles").mkdir(parents=True)
    (package_dir / "evidence").mkdir()
    items: list[dict[str, Any]] = []
    for item in slices:
        mp4 = _link_if_exists(item.video, package_dir / f"{item.stem}.mp4", package_dir)
        flv = _link_if_exists(item.video, package_dir / f"{item.stem}.flv", package_dir)
        cover = _link_if_exists(item.cover, package_dir / f"{item.stem}.cover.png", package_dir)
        srt = _link_if_exists(item.subtitle, package_dir / "subtitles" / f"{item.stem}.srt", package_dir)
        evidence = _link_if_exists(item.evidence, package_dir / "evidence" / f"{item.stem}.evidence.json", package_dir)
        publish_json = _link_if_exists(item.publish_json, package_dir / f"{item.stem}.publish.json", package_dir)
        jingting_srt = _link_if_exists(item.jingting_srt, package_dir / f"{item.stem}.jingting.srt", package_dir)
        jingting_done = _link_if_exists(item.jingting_done, package_dir / f"{item.stem}.jingting.done", package_dir)
        jingting_manifest = _link_if_exists(item.jingting_manifest, package_dir / f"{item.stem}.jingting.manifest.json", package_dir)
        jingting_review_required = _link_if_exists(
            item.jingting_review_required,
            package_dir / f"{item.stem}.jingting.review-required.json",
            package_dir,
        )
        draft_cues = _count_srt_cues(item.subtitle)
        jingting_cues = _count_srt_cues(item.jingting_srt)
        items.append(
            {
                "stem": item.stem,
                "title": item.title,
                "mp4": mp4,
                "flv": flv,
                "cover": cover,
                "srt": srt,
                "evidence": evidence,
                "duration_sec": item.duration_sec,
                "prepared_at": item.prepared_at,
                "upload_enabled": item.upload_enabled,
                "publish_json": publish_json,
                "jingting_srt": jingting_srt,
                "jingting_done": jingting_done,
                "jingting_manifest": jingting_manifest,
                "jingting_review_required": jingting_review_required,
                "jingting_qc": "same_timing" if jingting_cues is not None and jingting_cues == draft_cues else "unknown",
                "jingting_cue_count": jingting_cues,
                "draft_cue_count": draft_cues,
            }
        )
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "lidousha_auto_review_shadow_daemon.v1",
        "source": str(date_dir),
        "room_id": room_id,
        "date": date_dir.name,
        "counts": {
            "items": len(items),
            "publish_json": sum(1 for row in items if row.get("publish_json")),
            "jingting_done": sum(1 for row in items if row.get("jingting_done")),
            "jingting_manifest": sum(1 for row in items if row.get("jingting_manifest")),
            "jingting_review_required": sum(1 for row in items if row.get("jingting_review_required")),
        },
        "items": items,
    }
    _write_json(package_dir / "review_manifest.json", manifest)
    return manifest


def run_once(
    *,
    videos_root: Path,
    report_root: Path,
    room_id: str,
    date: str | None,
    require_jingting_complete: bool,
    force: bool,
) -> dict[str, Any]:
    date_dir = videos_root / room_id / date if date else find_latest_date_dir(videos_root, room_id)
    if date_dir is None or not date_dir.is_dir():
        return {"status": "skipped", "reason": "date_dir_missing", "room_id": room_id, "date": date}

    slices = discover_slices(date_dir)
    source_integrity = build_date_source_integrity(date_dir, room_id=room_id, slices=slices)
    review_package_slices = list(slices)
    live_source_routes: list[LiveSourceRoute] = []
    live_source_metadata_gaps: list[dict[str, Any]] = []
    full_session_selector = {"status": "NOT_RUN"}
    if not slices:
        source_pair = _find_full_session_source_pair(date_dir)
        recording_completion: dict[str, Any] | None = None
        if source_pair is not None:
            recording_completion = detect_recording_completion(
                source_pair[0],
                state_path=report_root / RECORDING_COMPLETION_STATE_FILENAME,
            )
            if not recording_completion["complete"] and not force:
                return {
                    "status": "skipped",
                    "reason": "recording_in_progress",
                    "date_dir": str(date_dir),
                    "recording_completion": recording_completion,
                    "source_integrity": source_integrity,
                }
        live_source_routes, full_session_selector = _full_session_live_source_routes(date_dir, room_id=room_id)
        if recording_completion is not None:
            full_session_selector = {**full_session_selector, "recording_completion": recording_completion}
        if not live_source_routes:
            return {
                "status": "skipped",
                "reason": "no_prepared_slices",
                "date_dir": str(date_dir),
                "full_session_selector": full_session_selector,
                "source_integrity": source_integrity,
            }

    missing_done = [item.stem for item in slices if not item.jingting_done.exists()]
    if require_jingting_complete and missing_done:
        review_package_slices = [item for item in slices if item.jingting_done.exists()]
        for item in slices:
            if item.jingting_done.exists():
                continue
            route = _live_source_route_for_slice(item)
            if route is not None:
                live_source_routes.append(route)
            else:
                live_source_metadata_gaps.append(_live_source_metadata_gap(item))
        if not review_package_slices and not live_source_routes:
            return {
                "status": "skipped",
                "reason": "jingting_incomplete",
                "date_dir": str(date_dir),
                "items": len(slices),
                "missing_jingting_done": missing_done,
                "live_source_metadata_gaps": live_source_metadata_gaps,
                "source_integrity": source_integrity,
            }

    report_root.mkdir(parents=True, exist_ok=True)
    state_path = report_root / "lidousha_auto_review_shadow_state.json"
    state = _read_json(state_path) if state_path.exists() else {}
    fingerprint = _fingerprint_slices(slices) if slices else _fingerprint_live_source_routes(live_source_routes)
    state_key = f"{room_id}/{date_dir.name}"
    if not force and state.get("fingerprints", {}).get(state_key) == fingerprint:
        return {
            "status": "skipped",
            "reason": "unchanged",
            "date_dir": str(date_dir),
            "fingerprint": fingerprint,
            "last_output_dir": state.get("outputs", {}).get(state_key),
            "source_integrity": source_integrity,
        }

    output_dir = _shadow_output_dir(report_root, room_id=room_id, date_name=date_dir.name)
    package_dir = report_root / "packages" / f"{room_id}-{date_dir.name}-latest"
    manifest: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    live_source_runs: list[dict[str, Any]] = []
    if review_package_slices:
        manifest = build_review_package(date_dir, package_dir, room_id=room_id, slices=review_package_slices)
        review_package_output_dir = output_dir if not live_source_routes else output_dir / "review_package"
        summary = run_shadow_pipeline(review_package=package_dir, output_dir=review_package_output_dir, no_upload=True)
        _persist_source_integrity_summary(review_package_output_dir, source_integrity)
    for route in live_source_routes:
        candidate_output_dir = output_dir / "live_source" / route.stem
        live_summary = run_shadow_pipeline(
            source_video=route.source_video,
            source_srt=route.source_srt,
            refined_srt=route.refined_srt,
            source_context_job=route.source_context_job,
            room_id=room_id,
            title=route.source_context_job.get("title") if isinstance(route.source_context_job.get("title"), str) else route.stem,
            output_dir=candidate_output_dir,
            no_upload=True,
        )
        if source_integrity is not None:
            live_summary.setdefault("source_integrity", dict(source_integrity))
        live_source_runs.append(
            {
                "stem": route.stem,
                "output_dir": str(candidate_output_dir),
                "metadata_sources": list(route.metadata_sources),
                "summary": live_summary,
            }
        )
    result = {
        "status": "ran",
        "date_dir": str(date_dir),
        "package_dir": str(package_dir) if manifest is not None else None,
        "output_dir": str(output_dir),
        "fingerprint": fingerprint,
        "manifest_counts": manifest.get("counts") if manifest is not None else None,
        "summary_counts": summary.get("counts") if summary is not None else None,
        "gap_summary": summary.get("gap_summary") if summary is not None else None,
        "validations": summary.get("validations") if summary is not None else _live_source_validations(live_source_runs),
        "route_counts": {
            "review_package_candidates": len(review_package_slices),
            "live_source_candidates": len(live_source_routes),
            "missing_jingting_done_unroutable": len(live_source_metadata_gaps),
        },
        "live_source_runs": live_source_runs,
        "live_source_metadata_gaps": live_source_metadata_gaps,
        "full_session_selector": full_session_selector,
        "source_integrity": source_integrity,
    }
    fingerprints = dict(state.get("fingerprints") or {})
    outputs = dict(state.get("outputs") or {})
    fingerprints[state_key] = fingerprint
    outputs[state_key] = str(output_dir)
    state.update(
        {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "room_id": room_id,
            "fingerprints": fingerprints,
            "outputs": outputs,
            "last_result": result,
        }
    )
    _write_json(state_path, state)
    return result


def _live_source_validations(live_source_runs: Sequence[Mapping[str, Any]]) -> dict[str, bool] | None:
    validations = []
    for run in live_source_runs:
        summary = run.get("summary")
        if not isinstance(summary, Mapping):
            continue
        value = summary.get("validations")
        if isinstance(value, Mapping):
            validations.append(value)
    if not validations:
        return None
    return {
        "no_upload": all(item.get("no_upload") is True for item in validations),
        "no_upload_or_free_deploy_performed": all(item.get("no_upload_or_free_deploy_performed") is True for item in validations),
    }


def _full_session_live_source_routes(date_dir: Path, *, room_id: str, max_candidates: int = 10) -> tuple[list[LiveSourceRoute], dict[str, Any]]:
    source_pair = _find_full_session_source_pair(date_dir)
    if source_pair is None:
        return [], {"status": "SOURCE_MISSING", "reason_codes": ["FULL_SESSION_SOURCE_OR_SRT_MISSING"]}

    source_video, source_srt = source_pair
    cues = _parse_srt(source_srt)
    if not cues:
        return [], {
            "status": "SOURCE_SRT_EMPTY",
            "reason_codes": ["FULL_SESSION_SOURCE_CUES_MISSING"],
            "source_video": str(source_video),
            "source_srt": str(source_srt),
        }

    source_duration_ms = _source_duration_ms(source_video, cues)
    selected = select_full_session_candidates(cues, max_candidates=max_candidates)
    selector_stage = "primary"
    if not selected:
        # Repair-first: zero candidates is a pipeline failure on arbitrary real
        # streams; fall back to performance-run/talk recall before giving up.
        selected = select_fallback_session_candidates(cues, max_candidates=min(max_candidates, 3))
        selector_stage = "fallback_recall"
    if not selected:
        return [], {
            "status": "NO_CANDIDATES",
            "reason_codes": ["FULL_SESSION_SELECTOR_NO_TALK_CANDIDATES"],
            "selector_stage": selector_stage,
            "source_video": str(source_video),
            "source_srt": str(source_srt),
            "source_duration_ms": source_duration_ms,
        }

    glossary = load_discovered_term_lexicon(source_srt)
    routes: list[LiveSourceRoute] = []
    for candidate in selected:
        job = candidate.to_source_context_job(source_duration_ms=source_duration_ms)
        job["selection"] = candidate.to_manifest()
        job["selector_stage"] = selector_stage
        if glossary is not None:
            job["glossary_path"] = str(glossary.path)
            job["glossary_sha256"] = hashlib.sha256(glossary.path.read_bytes()).hexdigest()
        routes.append(
            LiveSourceRoute(
                stem=candidate.anchor.candidate_id,
                source_video=source_video,
                source_srt=source_srt,
                refined_srt=None,
                source_context_job=job,
                metadata_sources=("full_session_selector", "source_video", "source_srt"),
            )
        )
    return routes, {
        "status": "SELECTED",
        "reason_codes": [],
        "selector_stage": selector_stage,
        "source_video": str(source_video),
        "source_srt": str(source_srt),
        "source_duration_ms": source_duration_ms,
        "candidate_count": len(routes),
        "candidates": [candidate.to_manifest() for candidate in selected],
    }


def _find_full_session_source_pair(date_dir: Path) -> tuple[Path, Path] | None:
    media_suffixes = {".mp4", ".m4s", ".flv"}
    for root in (date_dir / "sources", date_dir):
        if not root.is_dir():
            continue
        for source_video in sorted(path for path in root.iterdir() if path.is_file() and path.suffix.lower() in media_suffixes):
            source_srt = source_video.with_suffix(".srt")
            if source_srt.is_file():
                return source_video, source_srt
    return None


def _source_duration_ms(source_video: Path, cues: Sequence[Any]) -> int:
    duration_sec = _probe_duration(source_video)
    probed_ms = int(round(duration_sec * 1000)) if duration_sec is not None else 0
    cue_ms = max((getattr(cue, "source_end_ms", 0) for cue in cues), default=0)
    return max(probed_ms, cue_ms)


def _live_source_route_for_slice(item: SliceArtifact) -> LiveSourceRoute | None:
    publish = _read_json_if_exists(item.publish_json)
    evidence = _read_json_if_exists(item.evidence)
    source_video_value, source_video_field = _first_string_field(
        (publish, "source_video"),
        (evidence, "source_video"),
        (publish, "source_video_path"),
        (evidence, "source_mp4"),
    )
    source_srt_value, source_srt_field = _first_string_field(
        (publish, "source_srt"),
        (evidence, "source_srt"),
        (publish, "full_source_srt"),
        (evidence, "full_source_srt"),
        (publish, "full_session_srt"),
        (evidence, "full_session_srt"),
        (publish, "transcript_path"),
        (evidence, "transcript_path"),
    )
    anchor_start_ms, anchor_start_field = _first_int_field(
        (publish, "anchor_start_ms", 1),
        (evidence, "anchor_start_ms", 1),
        (publish, "source_start_ms", 1),
        (evidence, "source_start_ms", 1),
        (publish, "source_start", 1000),
        (evidence, "source_start", 1000),
    )
    anchor_end_ms, anchor_end_field = _first_int_field(
        (publish, "anchor_end_ms", 1),
        (evidence, "anchor_end_ms", 1),
        (publish, "source_end_ms", 1),
        (evidence, "source_end_ms", 1),
        (publish, "source_end", 1000),
        (evidence, "source_end", 1000),
    )
    if not source_video_value or not source_srt_value or anchor_start_ms is None or anchor_end_ms is None:
        return None
    if anchor_end_ms < anchor_start_ms:
        return None
    metadata_sources = tuple(
        dict.fromkeys(
            source
            for source in (source_video_field, source_srt_field, anchor_start_field, anchor_end_field)
            if source is not None
        )
    )
    source_context_job: dict[str, Any] = {
        "candidate_id": item.stem,
        "title": item.title,
        "anchor_start_ms": anchor_start_ms,
        "anchor_end_ms": anchor_end_ms,
    }
    source_video_sha256, _source_video_sha256_field = _first_string_field((evidence, "source_video_sha256"), (publish, "source_video_sha256"))
    provenance: dict[str, Any] = {}
    if source_video_sha256:
        provenance["source_sha256"] = source_video_sha256 if source_video_sha256.startswith("sha256:") else f"sha256:{source_video_sha256}"
    if provenance:
        source_context_job["provenance"] = provenance
    return LiveSourceRoute(
        stem=item.stem,
        source_video=Path(source_video_value),
        source_srt=Path(source_srt_value),
        refined_srt=None,
        source_context_job=source_context_job,
        metadata_sources=metadata_sources,
    )


def _live_source_metadata_gap(item: SliceArtifact) -> dict[str, Any]:
    publish = _read_json_if_exists(item.publish_json)
    evidence = _read_json_if_exists(item.evidence)
    found_fields: list[str] = []
    missing_fields: list[str] = []
    if _has_any_nonempty_string(publish, evidence, keys=("source_video", "source_video_path", "source_mp4")):
        found_fields.append("source_video")
    else:
        missing_fields.append("source_video")
    if _has_any_nonempty_string(publish, evidence, keys=("source_srt", "full_source_srt", "full_session_srt", "transcript_path")):
        found_fields.append("source_srt")
    else:
        missing_fields.append("source_srt")
    if _has_any_numeric(publish, evidence, keys=("anchor_start_ms", "source_start_ms", "source_start")):
        found_fields.append("anchor_start_ms")
    else:
        missing_fields.append("anchor_start_ms")
    if _has_any_numeric(publish, evidence, keys=("anchor_end_ms", "source_end_ms", "source_end")):
        found_fields.append("anchor_end_ms")
    else:
        missing_fields.append("anchor_end_ms")
    return {
        "stem": item.stem,
        "required_upstream_fields": [
            "source_video (or source_video_path/source_mp4 equivalent)",
            "source_srt (or full_source_srt/full_session_srt/transcript_path equivalent)",
            "anchor_start_ms (or source_start/source_start_ms equivalent)",
            "anchor_end_ms (or source_end/source_end_ms equivalent)",
        ],
        "missing_fields": missing_fields,
        "found_fields": found_fields,
    }


def _shadow_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _shadow_output_dir(report_root: Path, *, room_id: str, date_name: str) -> Path:
    base = report_root / "shadow" / f"{room_id}-{date_name}-{_shadow_timestamp()}"
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = base.with_name(f"{base.name}-{index:02d}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"unable to allocate unique shadow output dir for {base}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run no-upload Li Dousha auto-review shadow sweeps.")
    parser.add_argument("--videos-root", type=Path, default=DEFAULT_VIDEOS_ROOT)
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--room", default=DEFAULT_ROOM)
    parser.add_argument("--date", help="YYYY-MM-DD date directory. Defaults to latest room directory.")
    parser.add_argument("--once", action="store_true", help="Run one sweep and exit.")
    parser.add_argument("--daemon", action="store_true", help="Run forever, polling for completed jingting slices.")
    parser.add_argument("--sleep", type=float, default=300.0)
    parser.add_argument("--allow-partial-jingting", action="store_true", help="Run even when some slices lack .jingting.done.")
    parser.add_argument("--force", action="store_true", help="Rerun even if the input fingerprint is unchanged.")
    args = parser.parse_args(argv)

    if not args.once and not args.daemon:
        args.once = True

    def sweep() -> dict[str, Any]:
        return run_once(
            videos_root=args.videos_root,
            report_root=args.report_root,
            room_id=args.room,
            date=args.date,
            require_jingting_complete=not args.allow_partial_jingting,
            force=args.force,
        )

    if args.once:
        result = sweep()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0

    daemon_lock = args.report_root / f".lidousha_auto_review_shadow_{args.room}.lock"
    if not acquire_directory_lock(daemon_lock):
        print(
            json.dumps(
                {"status": "daemon_already_running", "room_id": args.room, "lock_path": str(daemon_lock)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 0

    print(
        json.dumps(
            {
                "status": "daemon_started",
                "room_id": args.room,
                "videos_root": str(args.videos_root),
                "report_root": str(args.report_root),
                "sleep": args.sleep,
                "allow_partial_jingting": args.allow_partial_jingting,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        while True:
            try:
                print(json.dumps(sweep(), ensure_ascii=False, sort_keys=True), flush=True)
            except Exception as exc:  # pragma: no cover - daemon resilience path.
                print(
                    json.dumps(
                        {"status": "error", "error_type": type(exc).__name__, "error": str(exc)},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )
            time.sleep(args.sleep)
    finally:
        release_directory_lock(daemon_lock)


def _path_from_publish(data: Mapping[str, Any], key: str, fallback: Path) -> Path:
    value = data.get(key)
    if isinstance(value, str) and value:
        return Path(value)
    return fallback


def _link_if_exists(src: Path, dst: Path, package_dir: Path) -> str:
    if not src.exists():
        return ""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    os.symlink(str(src), str(dst))
    return str(dst.relative_to(package_dir))


def _count_srt_cues(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return sum(1 for line in text.splitlines() if "-->" in line)


def build_date_source_integrity(date_dir: Path, *, room_id: str, slices: Sequence[SliceArtifact]) -> dict[str, Any] | None:
    slice_video_paths = {_safe_resolve(item.video) for item in slices}
    source_paths = [
        path
        for path in sorted(date_dir.iterdir())
        if _is_source_media_path(path, slice_video_paths)
    ]
    segments: list[MediaSegmentObservation] = []
    cursor_ms = 0
    start_hints = {path: _source_media_start_ms(path, date_dir) for path in source_paths}
    use_wall_clock = sum(1 for value in start_hints.values() if value is not None) >= 2
    if use_wall_clock:
        source_paths = sorted(source_paths, key=lambda path: (start_hints[path] if start_hints[path] is not None else 10**18, path.name))
    for path in source_paths:
        duration_sec = _probe_duration(path)
        duration_ms = int(round(duration_sec * 1000)) if duration_sec is not None else 0
        start_ms = start_hints[path] if use_wall_clock and start_hints[path] is not None else cursor_ms
        stat = path.stat()
        segments.append(
            MediaSegmentObservation(
                path=str(path),
                start_ms=start_ms,
                end_ms=start_ms + duration_ms,
                duration_ms=duration_ms,
                size_bytes=stat.st_size,
                probed_ok=duration_sec is not None,
            )
        )
        cursor_ms = max(cursor_ms, start_ms + duration_ms)

    danmaku_latest_ms = None if use_wall_clock else _latest_danmaku_ms(date_dir)
    expected_start_ms = min((segment.start_ms for segment in segments), default=0) if use_wall_clock else 0
    expected_end_ms = max((segment.end_ms for segment in segments), default=0)
    expected_end_ms = max(expected_end_ms, danmaku_latest_ms or 0)
    if expected_end_ms <= expected_start_ms:
        return None

    ledger = build_source_range_ledger(
        room_id=room_id,
        session_date=date_dir.name,
        segments=segments,
        expected_start_ms=expected_start_ms,
        expected_end_ms=expected_end_ms,
        danmaku_latest_ms=danmaku_latest_ms,
        active_media_size_growth_bytes=0,
    )
    replacement_source = _replacement_source_coverage(date_dir, expected_start_ms=expected_start_ms, expected_end_ms=expected_end_ms)
    if ledger.compensation_required and replacement_source and replacement_source["status"] == "COVERS_EXPECTED_RANGE":
        ledger = build_source_range_ledger(
            room_id=room_id,
            session_date=date_dir.name,
            segments=[
                MediaSegmentObservation(
                    path=str(replacement_source["root"]),
                    start_ms=expected_start_ms,
                    end_ms=expected_end_ms,
                    duration_ms=expected_end_ms - expected_start_ms,
                    size_bytes=int(replacement_source["size_bytes"]),
                    probed_ok=True,
                )
            ],
            expected_start_ms=expected_start_ms,
            expected_end_ms=expected_end_ms,
            danmaku_latest_ms=None,
            active_media_size_growth_bytes=0,
        )
    plan = plan_bilibili_replay_compensation(
        ledger,
        auth_available=False,
        replay_available=None,
    )
    result = {"ledger": ledger.to_manifest(), "compensation_plan": plan.to_manifest()}
    if replacement_source is not None:
        result["replacement_source"] = _replacement_source_manifest(replacement_source)
    return result


def _replacement_source_coverage(date_dir: Path, *, expected_start_ms: int, expected_end_ms: int) -> dict[str, Any] | None:
    root = date_dir / "replacement_source"
    if not root.is_dir():
        return None
    parts: list[dict[str, Any]] = []
    total_duration_ms = 0
    total_size_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".mp4", ".m4s", ".flv"}:
            continue
        duration_sec = _probe_duration(path)
        duration_ms = int(round(duration_sec * 1000)) if duration_sec is not None else 0
        size_bytes = path.stat().st_size
        parts.append(
            {
                "path": str(path),
                "duration_ms": duration_ms,
                "size_bytes": size_bytes,
                "probed_ok": duration_sec is not None,
            }
        )
        total_duration_ms += duration_ms
        total_size_bytes += size_bytes
    if not parts:
        return None
    expected_duration_ms = max(0, expected_end_ms - expected_start_ms)
    status = "COVERS_EXPECTED_RANGE" if total_duration_ms + 2_000 >= expected_duration_ms else "INSUFFICIENT_DURATION"
    return {
        "status": status,
        "root": root,
        "duration_ms": total_duration_ms,
        "size_bytes": total_size_bytes,
        "expected_duration_ms": expected_duration_ms,
        "parts": parts,
    }


def _replacement_source_manifest(replacement_source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "replacement-source-coverage.v1",
        "status": replacement_source["status"],
        "root": str(replacement_source["root"]),
        "duration_ms": replacement_source["duration_ms"],
        "size_bytes": replacement_source["size_bytes"],
        "expected_duration_ms": replacement_source["expected_duration_ms"],
        "parts": replacement_source["parts"],
    }


def _is_source_media_path(path: Path, slice_video_paths: set[Path]) -> bool:
    if not path.is_file() or path.suffix.lower() not in {".m4s", ".flv", ".mp4"}:
        return False
    resolved = _safe_resolve(path)
    if resolved in slice_video_paths:
        return False
    return not path.name.endswith((".cover.png", ".jingting.srt"))


def _source_media_start_ms(path: Path, date_dir: Path) -> int | None:
    patterns = (
        (r"(?P<stamp>\d{8}-\d{2}-\d{2}-\d{2})", "%Y%m%d-%H-%M-%S"),
        (r"(?P<stamp>\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})", "%Y-%m-%d-%H-%M-%S"),
    )
    for pattern, fmt in patterns:
        match = re.search(pattern, path.name)
        if match is None:
            continue
        try:
            value = datetime.strptime(match.group("stamp"), fmt)
        except ValueError:
            continue
        if value.strftime("%Y-%m-%d") != date_dir.name:
            continue
        return ((value.hour * 60 + value.minute) * 60 + value.second) * 1000
    return None


def _safe_resolve(path: Path) -> Path:
    return path.resolve(strict=False)


def _latest_danmaku_ms(date_dir: Path) -> int | None:
    latest: int | None = None
    for path in sorted(date_dir.glob("*danmaku*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            value = _danmaku_line_ms(line)
            if value is not None:
                latest = value if latest is None else max(latest, value)
    return latest


def _danmaku_line_ms(line: str) -> int | None:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, Mapping):
        return None
    for key in ("timeline_ms", "offset_ms", "time_ms", "ts_ms"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(round(value)))
    for key in ("timeline_sec", "offset_sec", "time_sec"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(round(value * 1000)))
    return None


def _probe_duration(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return round(float(proc.stdout.strip()), 3)
    except ValueError:
        return None


def _fingerprint_slices(slices: Sequence[SliceArtifact]) -> str:
    rows: list[dict[str, Any]] = []
    for item in slices:
        paths = [
            item.publish_json,
            item.video,
            item.cover,
            item.subtitle,
            item.evidence,
            item.jingting_srt,
            item.jingting_done,
            item.jingting_manifest,
            item.jingting_review_required,
        ]
        rows.append(
            {
                "stem": item.stem,
                "upload_enabled": item.upload_enabled,
                "paths": [_stat_fingerprint(path) for path in paths],
            }
        )
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _fingerprint_live_source_routes(routes: Sequence[LiveSourceRoute]) -> str:
    rows = [
        {
            "stem": route.stem,
            "source_video": _stat_fingerprint(route.source_video),
            "source_srt": _stat_fingerprint(route.source_srt),
            "source_context_job": route.source_context_job,
        }
        for route in routes
    ]
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _stat_fingerprint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    return {"path": str(path), "exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {}


def _first_string_field(*candidates: tuple[Mapping[str, Any], str]) -> tuple[str | None, str | None]:
    for mapping, key in candidates:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value, key
    return None, None


def _first_int_field(*candidates: tuple[Mapping[str, Any], str, int]) -> tuple[int | None, str | None]:
    for mapping, key, multiplier in candidates:
        value = mapping.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value * multiplier, key
        if isinstance(value, float):
            return int(round(value * multiplier)), key
    return None, None


def _has_any_nonempty_string(*mappings: Mapping[str, Any], keys: Sequence[str]) -> bool:
    return any(isinstance(mapping.get(key), str) and mapping.get(key, "").strip() for mapping in mappings for key in keys)


def _has_any_numeric(*mappings: Mapping[str, Any], keys: Sequence[str]) -> bool:
    return any(isinstance(mapping.get(key), (int, float)) and not isinstance(mapping.get(key), bool) for mapping in mappings for key in keys)


def acquire_directory_lock(lock_path: Path) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_path.mkdir()
    except FileExistsError:
        if _lock_pid_is_dead(lock_path):
            release_directory_lock(lock_path)
            try:
                lock_path.mkdir()
            except FileExistsError:
                return False
        else:
            return False
    lock_path.joinpath("pid").write_text(str(os.getpid()) + "\n", encoding="utf-8")
    lock_path.joinpath("created_at").write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")
    return True


def release_directory_lock(lock_path: Path) -> None:
    try:
        for child in lock_path.iterdir():
            child.unlink()
        lock_path.rmdir()
    except OSError:
        pass


def _lock_pid_is_dead(lock_path: Path) -> bool:
    try:
        pid = int(lock_path.joinpath("pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    if _proc_state(pid) == "Z":
        return True
    return False


def _proc_state(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parts = stat.split()
    return parts[2] if len(parts) >= 3 else None


def _persist_source_integrity_summary(output_dir: Path, source_integrity: Mapping[str, Any] | None) -> None:
    if not isinstance(source_integrity, Mapping):
        return
    summary_path = output_dir / "summary.json"
    if not summary_path.exists():
        return
    summary = _read_json(summary_path)
    summary["source_integrity"] = dict(source_integrity)
    _write_json(summary_path, summary)
    _write_json(output_dir / "source_integrity.json", source_integrity)


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
