#!/usr/bin/env python3
"""Offline census: transcribe -> five-minute windows -> labels -> report/clues.

Example (no runner or publication integration):
  python scripts/channel_census.py --profile example --recordings recordings.json \
      --out /path/to/census --host worker-name --census-id example-2026 \
      --labeler my_provider:label --label-model model-version

Recordings JSON: {"recordings": [{"path": "relative/to/list.mp4", "duration_ms": 900000,
"recording_date": "", "danmaku_starts_ms": [1200]}]}. Duration is explicit;
the final partial window and silent gaps are retained. Dates/danmaku are optional.
Profile assets supply channel_census_config and slice_selection_metric; --config
can select an explicit census config. No credentials are loaded by this tool.

Provider injection uses importable module:callable references. Transcriber:
fn(recording: RecordingRef, media_path: Path) -> Sequence[CensusTranscriptCue].
Default is the existing BCUT client only (no automatic provider fallback).
Custom transcribers require --transcript-algorithm. Labeler:
fn(window: CensusWindowInput, *, prompt: str) -> str (strict JSON from design §1.3).
The supplied window's text is capped at 2500 characters. A provider may use
census_window_text(window) to obtain that exact input. Tests use fake providers.

Output stages are create-only, atomically linked under --out, keyed by input SHA.
Resolved paths are deliberately excluded from identity: moving identical input
does not repeat work. --host is provenance only. A nonblocking flock prevents
concurrent writers. Touch <out>/STOP to stop between stages (exit 75), then remove
it and repeat the same command to resume. Interrupted providers can be called
again; only durably completed stages are reusable. Existing files are never
replaced, and a conflicting artifact is an error. Raw data stays outside git.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice import channel_census as cc
from src.autoslice.channel_profile import ChannelProfileError, load_channel_profile

Transcriber = Callable[[cc.RecordingRef, Path], Sequence[cc.CensusTranscriptCue]]


class CensusStopped(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordingInput:
    ref: cc.RecordingRef
    path: Path
    danmaku_starts_ms: tuple[int, ...]


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def create_only(path: Path, payload: bytes) -> None:
    """Publish a complete inode without ever overwriting an existing destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"CENSUS_ARTIFACT_CONFLICT: {path}")
        return
    with NamedTemporaryFile(dir=path.parent, prefix=".census-", delete=False) as tmp:
        temporary = Path(tmp.name)
        try:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise ValueError(f"CENSUS_ARTIFACT_CONFLICT: {path}") from None
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink()


def file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def stop_check(out: Path) -> None:
    if (out / "STOP").exists():
        raise CensusStopped("CENSUS_STOP_REQUESTED")


def recordings_from_list(path: Path) -> tuple[RecordingInput, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("recordings") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("CENSUS_RECORDINGS_LIST_REQUIRED")
    result = []
    identities = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise ValueError("INVALID_CENSUS_RECORDING")
        media = (path.parent / row["path"]).resolve()
        duration = row.get("duration_ms")
        if type(duration) is not int or duration <= 0:
            raise ValueError("INVALID_CENSUS_DURATION")
        recorded = row.get("recording_date")
        if recorded is not None:
            if not isinstance(recorded, str) or date.fromisoformat(recorded).isoformat() != recorded:
                raise ValueError("INVALID_CENSUS_RECORDING_DATE")
        danmaku = row.get("danmaku_starts_ms", [])
        if not isinstance(danmaku, list) or any(
            type(t) is not int or not 0 <= t < duration for t in danmaku
        ):
            raise ValueError("CENSUS_DANMAKU_OUTSIDE_RECORDING")
        ref = cc.RecordingRef(media.name, file_sha256(media), duration, recorded)
        identity = (ref.basename, ref.sha256)
        if identity in identities:
            raise ValueError("DUPLICATE_CENSUS_RECORDING")
        identities.add(identity)
        result.append(RecordingInput(ref, media, tuple(sorted(danmaku))))
    return tuple(sorted(result, key=lambda r: (r.ref.basename, r.ref.sha256)))


def load_callable(reference: str) -> Callable:
    module, separator, name = reference.partition(":")
    if not separator or not module or not name:
        raise ValueError("CENSUS_PROVIDER_EXPECTS_MODULE_COLON_CALLABLE")
    provider = getattr(importlib.import_module(module), name)
    if not callable(provider):
        raise ValueError("CENSUS_PROVIDER_NOT_CALLABLE")
    return cast(Callable, provider)


def transcribe_bcut(recording: cc.RecordingRef, media_path: Path) -> Sequence[cc.CensusTranscriptCue]:
    from scripts.free_asr_client import extract_audio_mp3, transcribe_bcut as bcut

    raw = bcut(extract_audio_mp3(media_path))
    return tuple(cc.CensusTranscriptCue(row["start_time"], row["end_time"], row["transcript"])
                 for row in raw["utterances"])


def capped_window(window: cc.CensusWindowInput) -> cc.CensusWindowInput:
    remaining = cc.MAX_CENSUS_INPUT_CHARS
    cues = []
    for cue in window.cues:
        if remaining <= 0:
            break
        text = cue.text[:remaining]
        cues.append(replace(cue, text=text))
        remaining -= len(text) + 1  # newline between cues
    return replace(window, cues=tuple(cues))


def run_census(
    args: argparse.Namespace, *, transcriber: Transcriber, labeler: Callable,
) -> Path:
    profile = load_channel_profile(ROOT, profile_id=args.profile, manifest_path=args.manifest, environ={})
    metric_text = profile.asset_file("slice_selection_metric").read_text(encoding="utf-8")
    cc.refuse_if_handwritten_clue_section(metric_text, section_heading=cc.HANDWRITTEN_CLUE_SECTION_HEADING)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CensusStopped("CENSUS_LOCKED") from None
        stop_check(out)
        config_path = args.config or profile.asset_file(cc.CENSUS_CONFIG_ASSET_KEY)
        config = cc.parse_census_config(json.loads(config_path.read_text(encoding="utf-8")),
                                        profile_id=profile.profile_id)
        prompt = cc.build_census_label_prompt(
            host_display_name=profile.display_name, config=config,
            metric_text=metric_text,
        )
        inputs = recordings_from_list(args.recordings.resolve())
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        run_input = {
            "schema_version": "channel-census-run-input.v1",
            "census_id": args.census_id, "profile_id": profile.profile_id,
            "recordings": [{"recording": asdict(r.ref), "danmaku_starts_ms": r.danmaku_starts_ms}
                           for r in inputs],
            "transcript_algorithm": args.transcript_algorithm, "transcriber": args.transcriber,
            "label_prompt_sha256": prompt_sha, "label_model": args.label_model,
            "labeler": args.labeler, "window_seconds": args.window_seconds,
        }
        run_dir = out / "runs" / digest(run_input)
        create_only(run_dir / "input.json", canonical(run_input))
        create_only(run_dir / "label-prompt.txt", prompt.encode("utf-8"))
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            manifest = cc.CensusInputManifest(
                args.census_id, profile.profile_id, tuple(r.ref for r in inputs),
                args.transcript_algorithm, args.window_seconds, prompt_sha, args.label_model,
                args.host, datetime.now(timezone.utc).isoformat(),
            )
            create_only(manifest_path, canonical({"schema_version": "channel-census-input.v1",
                                                  **asdict(manifest)}))
        manifest_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_raw.pop("schema_version")
        manifest_raw["recordings"] = tuple(cc.RecordingRef(**r) for r in manifest_raw["recordings"])
        manifest_raw["legacy_label_file_sha256"] = ()
        manifest = cc.CensusInputManifest(**manifest_raw)
        labels = []
        for recording in inputs:
            stop_check(out)
            transcription_sha = digest({"recording": asdict(recording.ref),
                                        "algorithm": args.transcript_algorithm,
                                        "provider": args.transcriber})
            transcript_path = out / "transcripts" / f"{transcription_sha}.json"
            if not transcript_path.exists():
                cues = tuple(transcriber(recording.ref, recording.path))
                # Validate timings/types before publishing a reusable transcript.
                cc.build_census_windows(cues, recording_group="validate", window_seconds=300,
                                        duration_ms=recording.ref.duration_ms)
                if file_sha256(recording.path) != recording.ref.sha256:
                    raise ValueError("CENSUS_RECORDING_CHANGED_DURING_ASR")
                create_only(transcript_path, canonical([asdict(c) for c in cues]))
            stop_check(out)
            cues = tuple(cc.CensusTranscriptCue(**row) for row in
                         json.loads(transcript_path.read_text(encoding="utf-8")))
            group = f"{recording.ref.recording_date or recording.ref.basename}-{recording.ref.basename}-{recording.ref.sha256}"
            windows_sha = digest({"transcript_sha256": file_sha256(transcript_path),
                                  "recording": asdict(recording.ref),
                                  "window_seconds": args.window_seconds,
                                  "danmaku_starts_ms": recording.danmaku_starts_ms})
            windows_path = out / "windows" / f"{windows_sha}.json"
            if windows_path.exists():
                cached = json.loads(windows_path.read_text(encoding="utf-8"))
                windows = tuple(cc.CensusWindowInput(
                    **{**w, "cues": tuple(cc.CensusTranscriptCue(**c) for c in w["cues"])}
                ) for w in cached)
            else:
                windows = cc.build_census_windows(
                    cues, recording_group=group, window_seconds=args.window_seconds,
                    duration_ms=recording.ref.duration_ms, danmaku_starts_ms=recording.danmaku_starts_ms,
                )
                create_only(windows_path, canonical([asdict(w) for w in windows]))
            for original in windows:
                stop_check(out)
                window = capped_window(original)
                label_sha = digest({"window": asdict(window), "prompt_sha256": prompt_sha,
                                    "model": args.label_model, "provider": args.labeler})
                path = out / "labels" / f"{label_sha}.json"
                if path.exists():
                    raw_label = json.loads(path.read_text(encoding="utf-8"))
                else:
                    raw_label = json.loads(labeler(window, prompt=prompt))
                    cc.parse_census_window_label(raw_label, window=window, config=config)
                    create_only(path, canonical(raw_label))
                labels.append(cc.parse_census_window_label(raw_label, window=window, config=config))
        stop_check(out)
        clues = cc.generate_census_clues(labels, config=config, manifest=manifest, metric_text=metric_text)
        create_only(run_dir / "windows.json", canonical([asdict(w) for w in labels]))
        create_only(run_dir / "selection_census_clues.v1.json", canonical(asdict(clues)))
        create_only(run_dir / "report.md", cc.render_census_report(labels, clues).encode("utf-8"))
        return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--profile")
    identity.add_argument("--manifest", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--recordings", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--host", required=True, help="execution host provenance; does not dispatch")
    parser.add_argument("--census-id", required=True)
    parser.add_argument("--window-seconds", type=int, default=300)
    parser.add_argument("--transcriber", default="scripts.channel_census:transcribe_bcut")
    parser.add_argument("--transcript-algorithm")
    parser.add_argument("--labeler", required=True, help="module:callable; no default network provider")
    parser.add_argument("--label-model", required=True)
    args = parser.parse_args(argv)
    if args.window_seconds <= 0:
        parser.error("--window-seconds must be positive")
    if not args.transcript_algorithm:
        if args.transcriber != "scripts.channel_census:transcribe_bcut":
            parser.error("custom --transcriber requires --transcript-algorithm")
        args.transcript_algorithm = "bcut-free-asr.v1"
    try:
        result = run_census(args, transcriber=load_callable(args.transcriber),
                            labeler=load_callable(args.labeler))
    except CensusStopped as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except (ChannelProfileError, OSError, ValueError, ImportError, AttributeError, TypeError) as exc:
        print(f"CENSUS_ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    print(json.dumps({"status": "COMPLETE", "run_dir": str(result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
