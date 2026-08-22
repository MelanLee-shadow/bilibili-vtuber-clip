"""Exact v2 full-window baseline replay and delivery-local audit projection."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path

from src.autoslice.recut_materialization import (
    BOUNDARY_CLIPPED_CUE_MAX_VISIBLE_MS,
    BOUNDARY_CUE_START_TOLERANCE_MS,
    _fresh_srt_to_source_cues,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.redelivery_source_binding import V2RedeliverySourceBinding
from src.autoslice.redelivery_subtitle_baseline import (
    apply_redelivery_subtitle_baseline,
)
from src.autoslice.review_evidence import SourceCue
from src.autoslice.source_subtitle_truth import source_truth_owner_windows


class FullWindowReplayError(RuntimeError):
    """A full-window replay or its private temporary is unsafe."""


def exact_full_window_replay_enabled(
    config: Mapping[str, object], binding: V2RedeliverySourceBinding | None
) -> bool:
    return bool(
        binding is not None
        and config.get("exact_interval_replay") is True
        and config.get("absolute_source_start_ms")
        == binding.content_absolute_start_ms
        and config.get("absolute_source_end_ms")
        == binding.content_absolute_end_ms
    )


def translate_final_local_protected_windows(
    windows: Sequence[tuple[int, int]], *, final_start_ms: int, padded_content_start_ms: int
) -> list[tuple[int, int]]:
    """Translate final-local source-truth drops to the padded replay grid."""

    offset = final_start_ms - padded_content_start_ms
    return [(start_ms + offset, end_ms + offset) for start_ms, end_ms in windows]


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clip_interval(
    start_ms: int, end_ms: int, *, final_start_ms: int, final_end_ms: int
) -> tuple[int, int] | None:
    clipped_start = max(start_ms, final_start_ms)
    clipped_end = min(end_ms, final_end_ms)
    if clipped_end <= clipped_start:
        return None
    if (
        clipped_start - final_start_ms <= BOUNDARY_CUE_START_TOLERANCE_MS
        and clipped_end - clipped_start <= BOUNDARY_CLIPPED_CUE_MAX_VISIBLE_MS
    ):
        return None
    return clipped_start, clipped_end


def project_full_window_audit_to_final_delivery(
    audit: dict, *, final_start_ms: int, final_end_ms: int
) -> None:
    """Preserve the source-grid audit while exposing final-local owner rows."""

    mappings = audit.get("mappings")
    owned = audit.get("owned_intervals")
    protected = audit.get("protected_intervals")
    if not all(isinstance(value, list) for value in (mappings, owned, protected)):
        raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_AUDIT_INVALID")
    full_source = {
        "schema_version": "redelivery-baseline-full-source-replay.v1",
        "mappings": deepcopy(mappings),
        "owned_intervals": deepcopy(owned),
        "protected_intervals": deepcopy(protected),
    }
    active_mappings: list[dict] = []
    omitted: list[dict] = []
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_MAPPING_INVALID")
        start_ms, end_ms = mapping.get("start_ms"), mapping.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or isinstance(end_ms, bool)
            or not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or end_ms <= start_ms
        ):
            raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_MAPPING_INVALID")
        clipped = _clip_interval(
            start_ms, end_ms, final_start_ms=final_start_ms, final_end_ms=final_end_ms
        )
        if clipped is None:
            reason = (
                "OUTSIDE_FINAL_DELIVERY"
                if end_ms <= final_start_ms or start_ms >= final_end_ms
                else "BOUNDARY_FLASH_FRAGMENT_OMITTED"
            )
            omitted.append(
                {"baseline_cue_index": mapping.get("baseline_cue_index"),
                 "source_start_ms": start_ms, "source_end_ms": end_ms, "reason": reason}
            )
            continue
        projected = deepcopy(dict(mapping))
        projected.update({"start_ms": clipped[0] - final_start_ms,
                          "end_ms": clipped[1] - final_start_ms,
                          "full_source_start_ms": start_ms,
                          "full_source_end_ms": end_ms})
        active_mappings.append(projected)

    def project_intervals(rows: list, error: str) -> list[dict[str, int]]:
        active: list[dict[str, int]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise FullWindowReplayError(error)
            start_ms, end_ms = row.get("start_ms"), row.get("end_ms")
            if (
                isinstance(start_ms, bool)
                or isinstance(end_ms, bool)
                or not isinstance(start_ms, int)
                or not isinstance(end_ms, int)
                or end_ms <= start_ms
            ):
                raise FullWindowReplayError(error)
            clipped = _clip_interval(
                start_ms, end_ms, final_start_ms=final_start_ms, final_end_ms=final_end_ms
            )
            if clipped is not None:
                active.append({"start_ms": clipped[0] - final_start_ms,
                               "end_ms": clipped[1] - final_start_ms})
        return active

    audit.update(
        {
            "full_source_replay": full_source,
            "full_source_replay_sha256": _canonical_sha256(full_source),
            "mappings": active_mappings,
            "owned_intervals": project_intervals(
                owned, "REDELIVERY_BASELINE_FULL_REPLAY_OWNED_INTERVAL_INVALID"
            ),
            # A protected window is a deliberate omission, not a subtitle
            # cue, so preserve a visible clipped portion even if it is short.
            "protected_intervals": _project_protected_intervals(
                protected, final_start_ms=final_start_ms, final_end_ms=final_end_ms
            ),
            "full_source_mapped_cue_count": len(mappings),
            "mapped_cue_count": len(active_mappings),
            "omitted_context_mapping_count": len(omitted),
            "omitted_context_mappings": omitted,
        }
    )


def _project_protected_intervals(
    rows: list, *, final_start_ms: int, final_end_ms: int
) -> list[dict[str, int]]:
    projected: list[dict[str, int]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_PROTECTED_INTERVAL_INVALID")
        start_ms, end_ms = row.get("start_ms"), row.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or isinstance(end_ms, bool)
            or not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or end_ms <= start_ms
        ):
            raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_PROTECTED_INTERVAL_INVALID")
        clipped_start, clipped_end = max(start_ms, final_start_ms), min(end_ms, final_end_ms)
        if clipped_start < clipped_end:
            projected.append({"start_ms": clipped_start - final_start_ms,
                              "end_ms": clipped_end - final_start_ms})
    return projected


def replay_full_window_then_crop(
    *,
    recut_dir: Path,
    cid: str,
    sanitized: Sequence[SourceCue],
    binding: V2RedeliverySourceBinding,
    config: Mapping[str, object],
    spec_parent: Path,
    protected_windows: Sequence[tuple[int, int]],
    final_start_ms: int,
    final_end_ms: int,
    subtitle_path: Path,
    write_source_range_srt: Callable[[Sequence[SourceCue], int, int, Path], None],
) -> tuple[str, dict]:
    """Replay the sealed full interval and atomically clean its private temp."""

    full_path = recut_dir / f".{cid}.baseline-full-window.srt"
    try:
        os.lstat(full_path)
    except FileNotFoundError:
        pass
    else:
        raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_TEMP_PATH_EXISTS")
    owns_full_path = True
    try:
        write_source_range_srt(
            sanitized,
            binding.padded_content_start_ms,
            binding.padded_content_end_ms,
            full_path,
        )
        current_text = full_path.read_text(encoding="utf-8")
        output_text, audit = apply_redelivery_subtitle_baseline(
            current_text,
            config=config,
            spec_parent=spec_parent,
            protected_windows=translate_final_local_protected_windows(
                protected_windows,
                final_start_ms=final_start_ms,
                padded_content_start_ms=binding.padded_content_start_ms,
            ),
            current_source_start_ms=binding.content_absolute_start_ms,
            current_source_end_ms=binding.content_absolute_end_ms,
            current_source_recording_basename=binding.source_recording_basename,
            current_source_sha256=binding.source_sha256,
        )
        if audit.get("status") != "FAILED":
            full_cues = [
                SourceCue(
                    f"redelivery_full_{index:04d}",
                    binding.padded_content_start_ms + cue.start_ms,
                    binding.padded_content_start_ms + cue.end_ms,
                    cue.text.strip(), "zh", "speech", 1.0,
                )
                for index, cue in enumerate(parse_srt_cues(output_text), start=1)
                if cue.text.strip()
            ]
            write_source_range_srt(full_cues, final_start_ms, final_end_ms, subtitle_path)
            output_text = subtitle_path.read_text(encoding="utf-8")
            audit["final_delivery_projection"] = {
                "absolute_source_start_ms": binding.absolute_source_start_ms,
                "absolute_source_end_ms": binding.absolute_source_end_ms,
            }
            audit["exact_replay_then_final_crop"] = True
            project_full_window_audit_to_final_delivery(
                audit, final_start_ms=final_start_ms, final_end_ms=final_end_ms
            )
        return output_text, audit
    finally:
        if owns_full_path:
            try:
                info = os.lstat(full_path)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(info.st_mode):
                    raise FullWindowReplayError("REDELIVERY_BASELINE_FULL_REPLAY_TEMP_PATH_UNSAFE")
                full_path.unlink()


def replay_baseline_for_final_recut(
    *,
    truth_audit: Mapping[str, object],
    recut_dir: Path,
    cid: str,
    sanitized: Sequence[SourceCue],
    binding: V2RedeliverySourceBinding | None,
    config: Mapping[str, object],
    spec_parent: Path,
    final_start_ms: int,
    final_end_ms: int,
    subtitle_path: Path,
    write_source_range_srt: Callable[[Sequence[SourceCue], int, int, Path], None],
) -> tuple[str, dict]:
    """Apply either final-local or exact full-window baseline authority."""

    protected: list[tuple[int, int]] = []
    for key in ("applied", "satisfied"):
        rows = truth_audit.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping) or row.get("action") != "drop_cue":
                continue
            for owner_start, owner_end in source_truth_owner_windows(row):
                start_ms = max(0, owner_start - final_start_ms)
                end_ms = min(final_end_ms - final_start_ms, owner_end - final_start_ms)
                if start_ms < end_ms:
                    protected.append((start_ms, end_ms))
    if exact_full_window_replay_enabled(config, binding):
        assert binding is not None
        return replay_full_window_then_crop(
            recut_dir=recut_dir,
            cid=cid,
            sanitized=sanitized,
            binding=binding,
            config=config,
            spec_parent=spec_parent,
            protected_windows=protected,
            final_start_ms=final_start_ms,
            final_end_ms=final_end_ms,
            subtitle_path=subtitle_path,
            write_source_range_srt=write_source_range_srt,
        )
    current_start = binding.absolute_source_start_ms if binding else None
    current_end = binding.absolute_source_end_ms if binding else None
    return apply_redelivery_subtitle_baseline(
        subtitle_path.read_text(encoding="utf-8"),
        config=config,
        spec_parent=spec_parent,
        protected_windows=protected,
        current_source_start_ms=current_start,
        current_source_end_ms=current_end,
        current_source_recording_basename=(binding.source_recording_basename if binding else None),
        current_source_sha256=(binding.source_sha256 if binding else None),
    )


def replay_full_window_text_and_crop(
    *,
    text: str,
    config: Mapping[str, object],
    spec_parent: Path,
    padded_start_ms: int,
    padded_end_ms: int,
    final_start_ms: int,
    final_end_ms: int,
    write_source_range_srt: Callable[[Sequence[SourceCue], int, int, Path], None],
    crop_path: Path,
    read_crop: Callable[[Path], bytes],
) -> tuple[bytes, dict]:
    """Apply an exact padded baseline and return a deterministic final crop."""

    reviewed, audit = apply_redelivery_subtitle_baseline(
        text,
        config=config,
        spec_parent=spec_parent,
        current_source_start_ms=padded_start_ms,
        current_source_end_ms=padded_end_ms,
        current_source_recording_basename=str(config["source_recording_basename"]),
        current_source_sha256=str(config["source_sha256"]),
    )
    if audit.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}:
        raise FullWindowReplayError("REPLAY_BASELINE_APPLICATION_FAILED")
    try:
        cues = _fresh_srt_to_source_cues(
            reviewed, window_start_ms=0, duration_ms=padded_end_ms - padded_start_ms
        )
    except ValueError as exc:
        raise FullWindowReplayError("REPLAY_REVIEWED_BASELINE_GEOMETRY_INVALID") from exc
    write_source_range_srt(cues, final_start_ms, final_end_ms, crop_path)
    try:
        return read_crop(crop_path), audit
    finally:
        crop_path.unlink(missing_ok=True)
