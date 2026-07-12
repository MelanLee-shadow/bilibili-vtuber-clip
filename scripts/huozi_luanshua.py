#!/usr/bin/env python3
"""Build, suggest, verify, and render Li Dousha "活字乱刷" speech.

The workflow is deliberately two-phase.  ``plan`` may search a discovery
corpus, but ``render`` accepts only a plan whose selected pieces were bound to
independent text evidence and immutable source-media hashes by ``verify``.
Nothing in this tool uploads or publishes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.huozi_luanshua import (  # noqa: E402
    CORPUS_SCHEMA,
    PLAN_SCHEMA,
    SOURCE_MANIFEST_SCHEMA,
    VERIFIED_PLAN_SCHEMA,
    apply_verification,
    build_corpus,
    build_verification,
    canonical_json_bytes,
    load_corpus,
    normalize_text,
    plan_text,
    rank_suggestions,
    sha256_file,
    split_clauses,
    validate_renderable_plan,
)
from scripts.run_auto_review_shadow_pipeline import (  # noqa: E402
    _write_lidousha_sapphire_ass_from_srt,
)


RENDER_MANIFEST_SCHEMA = "huozi-render.v1"
COMPOSITE_MANIFEST_SCHEMA = "huozi-composite.v1"
SUGGESTION_REPORT_SCHEMA = "huozi-suggestion-report.v1"
HISTORY_DISCOVERY_SCHEMA = "huozi-history-discovery.v1"
INTRO_LEAD_PAUSE_MS = 120
CLAUSE_PAUSE_MS = 260
PIECE_LOUDNESS_FILTER = "loudnorm=I=-18:LRA=7:TP=-2"

_SRT_TIMELINE_RX = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
)


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run(command: Sequence[str], *, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(part) for part in command],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{command[0]} failed rc={completed.returncode}: {completed.stderr[-1200:]}"
        )
    return completed


def _ffprobe_duration_ms(path: Path) -> int:
    completed = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=duration",
            "-of",
            "json",
            str(path),
        ],
        timeout=120,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON for {path}") from exc
    candidates = [
        payload.get("format", {}).get("duration")
        if isinstance(payload.get("format"), Mapping)
        else None
    ]
    streams = payload.get("streams")
    if isinstance(streams, list):
        candidates.extend(
            stream.get("duration")
            for stream in streams
            if isinstance(stream, Mapping)
        )
    for raw_duration in candidates:
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return round(duration * 1000)
    raise RuntimeError(f"ffprobe found no positive duration for {path}")


def _srt_time(ms: int) -> str:
    hours, remainder = divmod(max(0, ms), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _safe_concat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def _srt_timestamp_ms(parts: Sequence[str]) -> int:
    hours, minutes, seconds, millis = (int(part) for part in parts)
    return ((hours * 60 + minutes) * 60 + seconds) * 1_000 + millis


def _parse_srt_for_discovery(path: Path) -> list[dict[str, object]]:
    raw = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    cues: list[dict[str, object]] = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [line.strip() for line in block.splitlines()]
        timeline_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timeline_index is None:
            continue
        match = _SRT_TIMELINE_RX.search(lines[timeline_index])
        if match is None:
            continue
        text = "".join(lines[timeline_index + 1 :]).strip()
        normalized = normalize_text(text)
        if not normalized:
            continue
        cues.append(
            {
                "start_ms": _srt_timestamp_ms(match.groups()[:4]),
                "end_ms": _srt_timestamp_ms(match.groups()[4:]),
                "text": text,
                "normalized": normalized,
            }
        )
    return cues


def _source_date_from_path(path: Path) -> str:
    match = re.search(r"(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)", str(path))
    return "-".join(match.groups()) if match else ""


def scan_history_transcripts(
    target: str,
    transcript_paths: Sequence[Path],
    *,
    max_gap_ms: int = 1_500,
    min_match_chars: int = 2,
) -> dict[str, object]:
    """Find long exact target fragments in coarse historical SRTs.

    This is deliberately a discovery surface.  A hit must still be promoted
    through the word-timed, dual-ASR and Li-Dousha speaker gates before the
    planner can cut it.
    """

    if max_gap_ms < 0 or min_match_chars < 1:
        raise ValueError("history scan limits must be non-negative")
    clauses = split_clauses(target)
    inventory: list[dict[str, object]] = []
    matches: list[dict[str, object]] = []
    for path in sorted({item.resolve() for item in transcript_paths}, key=str):
        if not path.is_file():
            raise FileNotFoundError(path)
        cues = _parse_srt_for_discovery(path)
        transcript_sha = sha256_file(path)
        source_date = _source_date_from_path(path)
        inventory.append(
            {
                "path": str(path),
                "sha256": transcript_sha,
                "source_date": source_date,
                "cue_count": len(cues),
            }
        )
        groups: list[list[dict[str, object]]] = []
        current: list[dict[str, object]] = []
        for cue in cues:
            if current and int(cue["start_ms"]) - int(current[-1]["end_ms"]) > max_gap_ms:
                groups.append(current)
                current = []
            current.append(cue)
        if current:
            groups.append(current)

        for group in groups:
            joined = "".join(str(cue["normalized"]) for cue in group)
            cue_for_character: list[int] = []
            for cue_index, cue in enumerate(group):
                cue_for_character.extend([cue_index] * len(str(cue["normalized"])))
            for clause_index, clause in enumerate(clauses):
                normalized_clause = clause["normalized"]
                for length in range(len(normalized_clause), min_match_chars - 1, -1):
                    for target_start in range(len(normalized_clause) - length + 1):
                        fragment = normalized_clause[target_start : target_start + length]
                        search_from = 0
                        while True:
                            found = joined.find(fragment, search_from)
                            if found < 0:
                                break
                            search_from = found + 1
                            first_cue = cue_for_character[found]
                            last_cue = cue_for_character[found + length - 1]
                            context_start = max(0, first_cue - 1)
                            context_end = min(len(group), last_cue + 2)
                            matches.append(
                                {
                                    "fragment": fragment,
                                    "character_count": length,
                                    "clause_index": clause_index,
                                    "target_span": [target_start, target_start + length],
                                    "source_date": source_date,
                                    "transcript_path": str(path),
                                    "transcript_sha256": transcript_sha,
                                    "source_interval_ms": [
                                        int(group[first_cue]["start_ms"]),
                                        int(group[last_cue]["end_ms"]),
                                    ],
                                    "context": " / ".join(
                                        str(cue["text"]) for cue in group[context_start:context_end]
                                    ),
                                    "promotion_status": "DISCOVERY_ONLY",
                                    "_group_interval_ms": [
                                        int(group[0]["start_ms"]),
                                        int(group[-1]["end_ms"]),
                                    ],
                                    "_source_character_span": [found, found + length],
                                }
                            )

    unique_matches: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    maximal_by_group: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for row in sorted(
        matches,
        key=lambda item: (
            -int(item["character_count"]),
            int(item["clause_index"]),
            str(item["source_date"]),
            str(item["transcript_path"]),
            item["source_interval_ms"],
        ),
    ):
        interval = row["source_interval_ms"]
        assert isinstance(interval, list)
        key = (
            row["fragment"],
            row["transcript_path"],
            int(interval[0]),
            int(interval[1]),
        )
        if key in seen:
            continue
        source_span = row["_source_character_span"]
        group_interval = row["_group_interval_ms"]
        target_span = row["target_span"]
        assert isinstance(source_span, list)
        assert isinstance(group_interval, list)
        assert isinstance(target_span, list)
        group_key = (
            row["transcript_path"],
            row["clause_index"],
            int(group_interval[0]),
            int(group_interval[1]),
        )
        group_rows = maximal_by_group.setdefault(group_key, [])
        if any(
            int(kept["_source_character_span"][0]) <= int(source_span[0])
            and int(kept["_source_character_span"][1]) >= int(source_span[1])
            and int(kept["target_span"][0]) <= int(target_span[0])
            and int(kept["target_span"][1]) >= int(target_span[1])
            for kept in group_rows
        ):
            continue
        seen.add(key)
        unique_matches.append(row)
        group_rows.append(row)
    for row in unique_matches:
        row.pop("_group_interval_ms", None)
        row.pop("_source_character_span", None)
    payload: dict[str, object] = {
        "schema_version": HISTORY_DISCOVERY_SCHEMA,
        "status": "DISCOVERY_ONLY_NOT_RENDERABLE",
        "target": target,
        "transcript_file_count": len(inventory),
        "transcripts": inventory,
        "matches": unique_matches,
    }
    payload["discovery_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


def _render_piece(
    piece: Mapping[str, object],
    destination: Path,
    *,
    actual_media_sha256: str | None = None,
    lead_pause_ms: int = 0,
    tail_pause_ms: int = 0,
) -> None:
    media = Path(str(piece["media_path"]))
    expected_sha = str(piece["media_sha256"])
    actual_sha = actual_media_sha256 or sha256_file(media)
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"source media hash drift for {piece['piece_id']}: expected {expected_sha}, got {actual_sha}"
        )
    start_ms = int(piece["cut_start_ms"])
    end_ms = int(piece["cut_end_ms"])
    if end_ms <= start_ms:
        raise RuntimeError(f"invalid source interval for {piece['piece_id']}")
    duration_ms = end_ms - start_ms
    fade_ms = min(12, max(3, duration_ms // 20))
    fade_seconds = fade_ms / 1000
    fade_out_start = max(0.0, duration_ms / 1000 - fade_seconds)
    lead_seconds = max(0, lead_pause_ms) / 1000
    pause_seconds = max(0, tail_pause_ms) / 1000
    video_filter = (
        f"trim=duration={duration_ms / 1000:.3f},"
        "scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,fps=30,format=yuv420p,setpts=PTS-STARTPTS"
    )
    audio_filter = (
        f"atrim=duration={duration_ms / 1000:.3f},aresample=48000,asetpts=PTS-STARTPTS,"
        f"{PIECE_LOUDNESS_FILTER},"
        f"afade=t=in:st=0:d={fade_seconds:.3f},"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade_seconds:.3f}"
    )
    if lead_seconds:
        video_filter += f",tpad=start_mode=clone:start_duration={lead_seconds:.3f}"
        audio_filter += f",adelay={lead_pause_ms}|{lead_pause_ms}"
    if pause_seconds:
        video_filter += f",tpad=stop_mode=clone:stop_duration={pause_seconds:.3f}"
        audio_filter += f",apad=pad_dur={pause_seconds:.3f}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-i",
            str(media),
            "-t",
            f"{(duration_ms + lead_pause_ms + tail_pause_ms) / 1000:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-vf",
            video_filter,
            "-af",
            audio_filter,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(destination),
        ],
        timeout=1800,
    )
    if not destination.is_file() or destination.stat().st_size < 2_048:
        raise RuntimeError(f"ffmpeg produced an empty piece for {piece['piece_id']}: {destination}")


def _write_plan_srt(plan: Mapping[str, object], piece_durations: Sequence[int], path: Path) -> None:
    clauses = plan.get("clauses")
    if not isinstance(clauses, list):
        raise RuntimeError("plan has no clauses")
    pieces = plan.get("pieces")
    if not isinstance(pieces, list) or len(pieces) != len(piece_durations):
        raise RuntimeError("plan piece/duration mismatch")
    cursor = 0
    blocks: list[str] = []
    active_clause: int | None = None
    clause_start = 0
    for piece, duration in zip(pieces, piece_durations):
        assert isinstance(piece, Mapping)
        clause_index = int(piece["clause_index"])
        if active_clause is None:
            active_clause = clause_index
            clause_start = cursor
        elif clause_index != active_clause:
            clause = clauses[active_clause]
            if not isinstance(clause, Mapping):
                raise RuntimeError("invalid plan clause")
            text = str(clause.get("display") or "") + str(clause.get("punctuation") or "")
            blocks.append(
                f"{len(blocks) + 1}\n{_srt_time(clause_start)} --> {_srt_time(cursor)}\n{text}"
            )
            active_clause = clause_index
            clause_start = cursor
        cursor += duration
    if active_clause is not None:
        clause = clauses[active_clause]
        if not isinstance(clause, Mapping):
            raise RuntimeError("invalid plan clause")
        text = str(clause.get("display") or "") + str(clause.get("punctuation") or "")
        blocks.append(
            f"{len(blocks) + 1}\n{_srt_time(clause_start)} --> {_srt_time(cursor)}\n{text}"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _final_encode_command(clean_video: Path, ass_path: Path, output: Path) -> list[str]:
    escaped_ass = str(ass_path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(clean_video),
        "-vf",
        f"subtitles='{escaped_ass}',fps=30",
        "-af",
        "aresample=48000:async=1:first_pts=0",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-fps_mode",
        "cfr",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(output),
    ]


def render_plan(plan: Mapping[str, object], output: Path, *, work_dir: Path) -> dict[str, object]:
    validate_renderable_plan(plan)
    pieces = plan["pieces"]
    assert isinstance(pieces, list)
    work_dir.mkdir(parents=True, exist_ok=True)
    piece_paths: list[Path] = []
    piece_durations: list[int] = []
    piece_records: list[dict[str, object]] = []
    media_hashes: dict[str, str] = {}
    last_piece_for_clause: dict[int, int] = {}
    for index, piece in enumerate(pieces):
        assert isinstance(piece, Mapping)
        last_piece_for_clause[int(piece["clause_index"])] = index
    for index, piece in enumerate(pieces, start=1):
        assert isinstance(piece, Mapping)
        destination = work_dir / f"piece-{index:03d}.mp4"
        media_path = str(piece["media_path"])
        if media_path not in media_hashes:
            media_hashes[media_path] = sha256_file(Path(media_path))
        _render_piece(
            piece,
            destination,
            actual_media_sha256=media_hashes[media_path],
            lead_pause_ms=INTRO_LEAD_PAUSE_MS if index == 1 else 0,
            tail_pause_ms=(
                CLAUSE_PAUSE_MS
                if last_piece_for_clause.get(int(piece["clause_index"])) == index - 1
                and index < len(pieces)
                else 0
            ),
        )
        duration_ms = _ffprobe_duration_ms(destination)
        piece_paths.append(destination)
        piece_durations.append(duration_ms)
        piece_records.append(
            {
                "piece_id": piece["piece_id"],
                "text": piece["text"],
                "source_id": piece["source_id"],
                "source_interval_ms": [piece["cut_start_ms"], piece["cut_end_ms"]],
                "rendered_path": str(destination),
                "rendered_sha256": sha256_file(destination),
                "rendered_duration_ms": duration_ms,
            }
        )

    concat_list = work_dir / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{_safe_concat_path(path)}'\n" for path in piece_paths), encoding="utf-8"
    )
    clean_video = work_dir / "assembled.clean.mp4"
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(clean_video),
        ]
    )
    srt_path = output.with_suffix(".srt")
    ass_path = output.with_suffix(".ass")
    _write_plan_srt(plan, piece_durations, srt_path)
    _write_lidousha_sapphire_ass_from_srt(srt_path, ass_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        _final_encode_command(clean_video, ass_path, output),
        timeout=1800,
    )
    manifest: dict[str, object] = {
        "schema_version": RENDER_MANIFEST_SCHEMA,
        "status": "REVIEW_READY_NO_UPLOAD",
        "upload_enabled": False,
        "target": plan["target"],
        "normalized_target": plan["normalized_target"],
        "verified_plan_sha256": plan["verified_plan_sha256"],
        "quality": plan["quality"],
        "pieces": piece_records,
        "artifacts": {
            "video": {
                "path": str(output.resolve()),
                "sha256": sha256_file(output),
                "duration_ms": _ffprobe_duration_ms(output),
            },
            "subtitle_srt": {"path": str(srt_path.resolve()), "sha256": sha256_file(srt_path)},
            "subtitle_ass": {"path": str(ass_path.resolve()), "sha256": sha256_file(ass_path)},
        },
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    return manifest


def _load_bound_render_manifest(path: Path) -> Mapping[str, object]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"render manifest is not an object: {path}")
    if (
        payload.get("schema_version") != RENDER_MANIFEST_SCHEMA
        or payload.get("status") != "REVIEW_READY_NO_UPLOAD"
        or payload.get("upload_enabled") is not False
    ):
        raise RuntimeError(f"render manifest is not a no-upload review artifact: {path}")
    expected_manifest_sha = str(payload.get("manifest_sha256") or "")
    unsigned = dict(payload)
    unsigned.pop("manifest_sha256", None)
    if (
        re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha) is None
        or hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest() != expected_manifest_sha
    ):
        raise RuntimeError(f"render manifest hash drift: {path}")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping) or not artifacts:
        raise RuntimeError(f"render manifest has no artifacts: {path}")
    for name, raw_artifact in artifacts.items():
        if not isinstance(raw_artifact, Mapping):
            raise RuntimeError(f"malformed {name} artifact in {path}")
        artifact_path = Path(str(raw_artifact.get("path") or ""))
        expected_sha = str(raw_artifact.get("sha256") or "").removeprefix("sha256:")
        if (
            not artifact_path.is_file()
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
            or sha256_file(artifact_path) != expected_sha
        ):
            raise RuntimeError(f"{name} artifact hash drift in {path}")
    return payload


def build_comparison_manifest(
    original_manifest_path: Path,
    suggested_manifest_path: Path,
    suggestion_report_path: Path,
    *,
    selection: str | None = None,
) -> dict[str, object]:
    if selection not in {None, "original", "suggested"}:
        raise RuntimeError(f"unsupported user selection: {selection!r}")
    original = _load_bound_render_manifest(original_manifest_path)
    suggested = _load_bound_render_manifest(suggested_manifest_path)
    suggestion_report = _read_json(suggestion_report_path)
    if (
        not isinstance(suggestion_report, Mapping)
        or suggestion_report.get("schema_version") != SUGGESTION_REPORT_SCHEMA
    ):
        raise RuntimeError("invalid suggestion report")
    recommendation = suggestion_report.get("recommendation")
    if not isinstance(recommendation, Mapping):
        raise RuntimeError("suggestion report has no recommendation")
    original_target = str(suggestion_report.get("target") or "")
    suggested_target = str(recommendation.get("text") or "")
    if original.get("target") != original_target or suggested.get("target") != suggested_target:
        raise RuntimeError("rendered targets do not match the suggestion report")

    choices = []
    for choice_id, manifest_path, manifest in (
        ("original", original_manifest_path, original),
        ("suggested", suggested_manifest_path, suggested),
    ):
        choices.append(
            {
                "choice_id": choice_id,
                "target": manifest["target"],
                "quality": manifest["quality"],
                "render_manifest": {
                    "path": str(manifest_path.resolve()),
                    "sha256": sha256_file(manifest_path),
                    "manifest_sha256": manifest["manifest_sha256"],
                },
                "artifacts": manifest["artifacts"],
            }
        )
    comparison: dict[str, object] = {
        "schema_version": COMPOSITE_MANIFEST_SCHEMA,
        "status": (
            "USER_SELECTED_NO_UPLOAD"
            if selection is not None
            else "USER_CHOICE_REQUIRED_NO_UPLOAD"
        ),
        "upload_enabled": False,
        "selection": selection,
        "suggestion_report": {
            "path": str(suggestion_report_path.resolve()),
            "sha256": sha256_file(suggestion_report_path),
        },
        "suggestion": {
            "text": suggested_target,
            "edit_distance": recommendation.get("edit_distance"),
            "rationale": recommendation.get("rationale"),
            "meaning_preservation": recommendation.get("meaning_preservation"),
        },
        "choices": choices,
    }
    comparison["comparison_sha256"] = hashlib.sha256(canonical_json_bytes(comparison)).hexdigest()
    return comparison


def _extract_json_object(raw: str) -> object:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    first_array = text.find("[")
    first_object = text.find("{")
    starts = [value for value in (first_array, first_object) if value >= 0]
    if not starts:
        raise ValueError("suggestion model returned no JSON")
    return json.loads(text[min(starts) :])


def _phrase_inventory(corpus_payload: Mapping[str, object], target: str, *, limit: int = 320) -> list[str]:
    target_chars = set(normalize_text(target))
    rows = []
    for utterance in load_corpus(corpus_payload):
        text = utterance.normalized_text
        overlap = len(set(text) & target_chars)
        if overlap >= 2 and 2 <= len(text) <= 36:
            rows.append((overlap, min(len(text), 18), utterance.transcript_confidence, text))
    rows.sort(key=lambda row: (-row[0], -row[1], -row[2], row[3]))
    return list(dict.fromkeys(row[3] for row in rows))[:limit]


def _generate_suggestions(
    target: str,
    corpus_payload: Mapping[str, object],
    *,
    command_template: str,
    work_dir: Path,
) -> list[Mapping[str, object]]:
    original = plan_text(target, corpus_payload)
    inventory = _phrase_inventory(corpus_payload, target)
    prompt = f"""你在给李豆沙的“活字乱刷”语音拼接器做保守改句建议。

原句：{target}
当前精确拼接质量：{json.dumps(original['quality'], ensure_ascii=False)}

历史语料中较相关、可直接连续取用的真实文字片段：
{chr(10).join('- ' + item for item in inventory)}

任务：给出 12 个以内候选句。只允许非常小的改字/加字/删字（总编辑距离不超过 4 个汉字），
必须保留原意、反转梗和第一人称语气；不要把“零”改成“0”，不要解释成性知识。
优先让候选句能直接命中上面的更长连续片段。原句不重复输出。

只输出 JSON 数组，每项严格为：
{{"text":"候选完整句","rationale":"为何更好拼","meaning_preservation":"如何保留原意"}}
"""
    work_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = work_dir / "suggestion.prompt.md"
    completion_path = work_dir / "suggestion.completion.json"
    prompt_path.write_text(prompt, encoding="utf-8")
    command = command_template.format(
        prompt_file=shlex.quote(str(prompt_path)), completion_file=shlex.quote(str(completion_path))
    )
    completed = subprocess.run(
        command,
        shell=True,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"suggestion command failed rc={completed.returncode}: {completed.stderr[-1200:]}")
    raw = completion_path.read_text(encoding="utf-8") if completion_path.is_file() else completed.stdout
    payload = _extract_json_object(raw)
    if not isinstance(payload, list):
        raise RuntimeError("suggestion model output is not an array")
    return [row for row in payload if isinstance(row, Mapping)]


def _corpus_command(args: argparse.Namespace) -> int:
    manifest = _read_json(args.sources)
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != SOURCE_MANIFEST_SCHEMA:
        raise SystemExit("invalid source manifest")
    corpus = build_corpus(manifest, manifest_dir=args.sources.parent, include_kinds=args.include_kind)
    _write_json(args.output, corpus)
    print(json.dumps({"schema_version": CORPUS_SCHEMA, "utterance_count": corpus["utterance_count"], "output": str(args.output)}, ensure_ascii=False))
    return 0


def _history_scan_command(args: argparse.Namespace) -> int:
    paths = list(args.transcript or [])
    for root in args.transcript_root or []:
        paths.extend(root.rglob("*.srt"))
    if not paths:
        raise SystemExit("provide --transcript or --transcript-root with SRT files")
    report = scan_history_transcripts(
        args.text,
        paths,
        max_gap_ms=args.max_gap_ms,
        min_match_chars=args.min_match_chars,
    )
    _write_json(args.output, report)
    print(
        json.dumps(
            {
                "schema_version": HISTORY_DISCOVERY_SCHEMA,
                "status": report["status"],
                "transcript_file_count": report["transcript_file_count"],
                "match_count": len(report["matches"]),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _plan_command(args: argparse.Namespace) -> int:
    corpus = _read_json(args.corpus)
    if not isinstance(corpus, Mapping):
        raise SystemExit("invalid corpus")
    plan = plan_text(args.text, corpus)
    _write_json(args.output, plan)
    print(json.dumps({"schema_version": PLAN_SCHEMA, "quality": plan["quality"], "output": str(args.output)}, ensure_ascii=False))
    return 0


def _suggest_command(args: argparse.Namespace) -> int:
    corpus = _read_json(args.corpus)
    if not isinstance(corpus, Mapping):
        raise SystemExit("invalid corpus")
    if args.suggestions:
        raw = _read_json(args.suggestions)
        if not isinstance(raw, list):
            raise SystemExit("suggestions JSON must be an array")
        suggestions = [row for row in raw if isinstance(row, Mapping)]
    elif args.suggestion_command:
        suggestions = _generate_suggestions(
            args.text,
            corpus,
            command_template=args.suggestion_command,
            work_dir=args.output.parent / ".suggestion-work",
        )
    else:
        raise SystemExit("provide --suggestions or --suggestion-command")
    ranked = rank_suggestions(args.text, corpus, suggestions, max_edits=args.max_edits)
    report = {
        "schema_version": SUGGESTION_REPORT_SCHEMA,
        "target": args.text,
        "original_plan": plan_text(args.text, corpus),
        "suggestions": ranked,
        "recommendation": ranked[0] if ranked else None,
        "user_choice_required": bool(ranked),
    }
    _write_json(args.output, report)
    print(json.dumps({"ranked": len(ranked), "recommendation": ranked[0]["text"] if ranked else None, "output": str(args.output)}, ensure_ascii=False))
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    plan = _read_json(args.plan)
    verification = _read_json(args.verification)
    if not isinstance(plan, Mapping) or not isinstance(verification, Mapping):
        raise SystemExit("invalid plan or verification")
    verified = apply_verification(plan, verification)
    _write_json(args.output, verified)
    print(json.dumps({"schema_version": VERIFIED_PLAN_SCHEMA, "status": verified["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0


def _evidence_command(args: argparse.Namespace) -> int:
    plan = _read_json(args.plan)
    if not isinstance(plan, Mapping):
        raise SystemExit("invalid plan")
    verification = build_verification(
        plan,
        timing_tolerance_ms=args.timing_tolerance_ms,
    )
    _write_json(args.output, verification)
    print(
        json.dumps(
            {
                "schema_version": verification["schema_version"],
                "piece_count": len(verification["pieces"]),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _render_command(args: argparse.Namespace) -> int:
    plan = _read_json(args.plan)
    if not isinstance(plan, Mapping):
        raise SystemExit("invalid verified plan")
    manifest = render_plan(plan, args.output, work_dir=args.work_dir)
    manifest_path = args.manifest or args.output.with_suffix(".manifest.json")
    _write_json(manifest_path, manifest)
    print(json.dumps({"status": manifest["status"], "video": str(args.output), "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0


def _bundle_command(args: argparse.Namespace) -> int:
    manifest = build_comparison_manifest(
        args.original_manifest,
        args.suggested_manifest,
        args.suggestion_report,
        selection=args.select,
    )
    _write_json(args.output, manifest)
    print(
        json.dumps(
            {
                "schema_version": COMPOSITE_MANIFEST_SCHEMA,
                "status": manifest["status"],
                "choices": [choice["target"] for choice in manifest["choices"]],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    corpus = subparsers.add_parser("corpus", help="build exact word-timed corpus")
    corpus.add_argument("--sources", type=Path, required=True)
    corpus.add_argument("--output", type=Path, required=True)
    corpus.add_argument("--include-kind", action="append", default=["talk"])
    corpus.set_defaults(func=_corpus_command)

    history_scan = subparsers.add_parser(
        "history-scan", help="find long exact fragments in coarse historical SRTs"
    )
    history_scan.add_argument("--text", required=True)
    history_scan.add_argument("--transcript", type=Path, action="append")
    history_scan.add_argument("--transcript-root", type=Path, action="append")
    history_scan.add_argument("--max-gap-ms", type=int, default=1_500)
    history_scan.add_argument("--min-match-chars", type=int, default=2)
    history_scan.add_argument("--output", type=Path, required=True)
    history_scan.set_defaults(func=_history_scan_command)

    plan = subparsers.add_parser("plan", help="plan an exact phrase")
    plan.add_argument("--corpus", type=Path, required=True)
    plan.add_argument("--text", required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.set_defaults(func=_plan_command)

    suggest = subparsers.add_parser("suggest", help="rank minimally edited alternatives")
    suggest.add_argument("--corpus", type=Path, required=True)
    suggest.add_argument("--text", required=True)
    suggest.add_argument("--suggestions", type=Path)
    suggest.add_argument("--suggestion-command")
    suggest.add_argument("--max-edits", type=int, default=4)
    suggest.add_argument("--output", type=Path, required=True)
    suggest.set_defaults(func=_suggest_command)

    verify = subparsers.add_parser("verify", help="bind independent evidence to a plan")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--verification", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    verify.set_defaults(func=_verify_command)

    evidence = subparsers.add_parser(
        "evidence", help="derive timed dual-ASR evidence from a discovery plan"
    )
    evidence.add_argument("--plan", type=Path, required=True)
    evidence.add_argument("--timing-tolerance-ms", type=int, default=1_200)
    evidence.add_argument("--output", type=Path, required=True)
    evidence.set_defaults(func=_evidence_command)

    render = subparsers.add_parser("render", help="render a verified plan")
    render.add_argument("--plan", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--work-dir", type=Path, required=True)
    render.add_argument("--manifest", type=Path)
    render.set_defaults(func=_render_command)

    bundle = subparsers.add_parser(
        "bundle", help="bind original and suggested renders for user choice"
    )
    bundle.add_argument("--original-manifest", type=Path, required=True)
    bundle.add_argument("--suggested-manifest", type=Path, required=True)
    bundle.add_argument("--suggestion-report", type=Path, required=True)
    bundle.add_argument("--select", choices=("original", "suggested"))
    bundle.add_argument("--output", type=Path, required=True)
    bundle.set_defaults(func=_bundle_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
