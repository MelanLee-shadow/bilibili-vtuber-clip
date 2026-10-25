"""Opt-in local speaker-vendor CLI transaction at the ordinary exact-final step.

No labels are inputs. Completed requests are recovered from verified disk
artifacts. Failed/partial requests are retained, not silently dispatched again.
This optional evidence never issues a release decision.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Mapping
import uuid
import wave

from src.autoslice import speaker_attribution as sa
from src.autoslice import speaker_attribution_geometry as sg
from src.autoslice.host_vocal_proof import _sha256_directory, _validate_profile
from src.autoslice.speaker_common import CHANNEL_PROFILE

ROOT = Path(__file__).resolve().parents[2]
VERSION = "producer-speaker-attribution.v1"
SOURCE_FILES = (
    "speaker_attribution.py",
    "speaker_attribution_geometry.py",
    "speaker_attribution_context.py",
    "speaker_context.py",
    "llm_client.py",
    "producer_speaker_attribution.py",
    "speaker_finalizer.py",
    "campp_embed_once.py",
    "speaker_common.py",
    "host_vocal_proof.py",
)


def _sha(path: Path) -> str:
    h = _evidence_file_sha256(path)
    return h


def _json(path: Path, value: object) -> None:
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def _snapshot(media: Path, profile: Path, model: Path, refs: Path, python: Path) -> dict:
    profile_doc = json.loads(profile.read_text())
    expected_model, expected_refs = _validate_profile(profile_doc)
    model_hash = _sha256_directory(model)
    if model_hash != expected_model["tree_sha256"]:
        raise ValueError("SPEAKER_MODEL_PROFILE_MISMATCH")
    reference_bindings = []
    for ref in expected_refs:
        path = (refs / ref["filename"]).resolve(strict=True)
        path.relative_to(refs.resolve(strict=True))
        actual = _sha(path)
        if actual != ref["sha256"]:
            raise ValueError("SPEAKER_REFERENCE_PROFILE_MISMATCH")
        reference_bindings.append({"id": ref["id"], "filename": ref["filename"], "sha256": actual})
    return {
        "media_sha256": _sha(media),
        "profile_sha256": _sha(profile),
        "model_tree_sha256": model_hash,
        "references": reference_bindings,
        "runtime_python_sha256": _sha(python.resolve(strict=True)),
        "context_launcher_sha256": _sha(ROOT / "scripts/llm_via_cpa.sh"),
        "code": {name: _sha(ROOT / "src/autoslice" / name) for name in SOURCE_FILES},
    }


def _valid_output(doc: object, request: dict, srt: str, directory: Path) -> bool:
    if not isinstance(doc, dict):
        return False
    binding = doc.get("source_binding")
    if (
        not isinstance(binding, dict)
        or binding.get("schema_version") != sg.SCHEMA
        or not sg.valid_geometry(binding, srt)
        or binding.get("srt_sha256") != sa._sha(srt)
        or binding["geometry"]["source_interval_ms"] != request["source_interval_ms"]
    ):
        return False
    inputs = request["inputs"]
    for key in ("media_sha256", "profile_sha256", "model_tree_sha256", "references"):
        if binding.get(key) != inputs[key]:
            return False
    rows = doc.get("cues")
    if not isinstance(rows, list):
        return False
    try:
        # The recorded decoded duration is measured metadata, not a free
        # parameter. Cold replay binds it to the retained actual PCM source.
        decoded = directory / "work/clip-16k-mono.wav"
        if _sha(decoded) != binding.get("decoded_audio_sha256"):
            return False
        with wave.open(str(decoded), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (sg.RATE, 1, 2):
                return False
            if wav.getnframes() * 1000 // sg.RATE != binding["geometry"]["audio_duration_ms"]:
                return False
        # Recompile identity rules from saved numeric observations, never from
        # saved labels. This is cheap semantic replay, not repeated acoustic work.
        canonical = sa.replay_attribution(doc, srt)
        for key in (
            "schema_version",
            "srt_sha256",
            "cue_count",
            "cues",
            "policy",
            "status",
            "score_partition",
            "media_screen",
            "context_calls",
            "reason_codes",
            "other_label",
            "other_label_hint",
            "display_speaker_mode",
            "display_authorized",
            "release_authorized",
            "mutation_authorized",
            "evidence_scope",
        ):
            if doc.get(key) != canonical[key]:
                return False
        if not request["context"] and (
            doc["runtime"]["context_attempted"] is not False
            or doc["runtime"].get("media_screen_attempted") is not False
            or doc.get("context_calls")):
            return False
        audio = doc["cue_audio"]
        if len(audio) != len(rows):
            return False
        for i, item in enumerate(audio, 1):
            expected_path = f"cue-wavs/cue-{i:04d}.wav"
            a, b = binding["geometry"]["cue_windows"][i - 1]["extracted_source_window_ms"]
            frames = (b - a) * sg.RATE // 1000
            if (
                item["cue_index"] != i
                or item["relative_path"] != expected_path
                or item.get("frames") != frames
                or item.get("sample_rate") != sg.RATE
                or _sha(directory / "work" / expected_path) != item["sha256"]
            ):
                return False
            with wave.open(str(directory / "work" / expected_path), "rb") as wav:
                if (
                    wav.getnframes(),
                    wav.getframerate(),
                    wav.getnchannels(),
                    wav.getsampwidth(),
                ) != (frames, sg.RATE, 1, 2):
                    return False
        if any(r["speaker"] != sa.UNKNOWN for r in rows):
            return bool(
                sa.attribution_context_for_srt(
                    doc,
                    srt,
                    expected_source_media_sha256=inputs["media_sha256"],
                    expected_profile_sha256=inputs["profile_sha256"],
                    expected_timeline_offset_ms=request["source_interval_ms"][0],
                )
            )
        return True
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _execute(command: list[str], directory: Path) -> dict:
    started = time.monotonic()
    env = dict(
        os.environ, PYTHONDONTWRITEBYTECODE="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1"
    )
    # cwd, not an ambient PYTHONPATH, determines the checked-out module.
    env.pop("PYTHONPATH", None)
    with subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as child:
        timed_out = False
        try:
            stdout, stderr = child.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(child.pid, signal.SIGKILL)
            stdout, stderr = child.communicate()
        (directory / "stdout.log").write_text(stdout)
        (directory / "stderr.log").write_text(stderr)
        return {
            "command": command,
            "returncode": child.returncode,
            "timed_out": timed_out,
            "wall_s": time.monotonic() - started,
            "context_requested": "--no-context" not in command,
        }


def generate_for_producer(
    *,
    options: Mapping,
    media_path: Path,
    srt_text: str,
    profile_path: Path,
    timeline_offset_ms: int,
    source_end_ms: int,
    out_root: Path,
    expected_media_sha256: str | None = None,
) -> tuple[dict, dict]:
    """Called only after explicit opt-in; all rejection paths yield UNKNOWN."""
    unknown = sa.attribute_speakers(srt_text, scorer=None, runtime_location="producer")
    root = out_root / "speaker-attribution"
    root.mkdir(parents=True, exist_ok=True)
    directory = None
    request = None
    try:
        if options.get("enabled") is not True:
            raise ValueError("SPEAKER_GENERATION_NOT_ENABLED")
        # Context is an explicit opt-in. It uses the existing CPA route for
        # playback screening and then identity expansion, never manual labels.
        context_enabled = options.get("context", False)
        if type(context_enabled) is not bool:
            raise ValueError("SPEAKER_CONTEXT_OPTION_INVALID")
        if (
            type(timeline_offset_ms) is not int
            or type(source_end_ms) is not int
            or not 0 <= timeline_offset_ms < source_end_ms
        ):
            raise ValueError("SPEAKER_CLOCK_UNSUPPORTED")
        sg.geometry(srt_text, timeline_offset_ms, source_end_ms, source_end_ms)
        model = Path(options.get("model_dir", "/opt/bilive/autoslice/models/campp"))
        refs = Path(
            options.get(
                "reference_dir",
                str(
                    Path("/opt/bilive/autoslice/voiceprints")
                    / CHANNEL_PROFILE.voiceprint_reference_subdirectory
                ),
            )
        )
        python = Path(options.get("speaker_python", "/opt/bilive/autoslice/venv-diar/bin/python"))
        inputs = _snapshot(media_path, profile_path, model, refs, python)
        if (
            expected_media_sha256 is not None
            and sa._source_digest(expected_media_sha256) != inputs["media_sha256"]
        ):
            raise ValueError("SPEAKER_CURRENT_SOURCE_MISMATCH")
        request = {
            "schema_version": VERSION,
            "inputs": inputs,
            "srt_sha256": sa._sha(srt_text),
            "source_interval_ms": [timeline_offset_ms, source_end_ms],
            "paths": {
                "media": str(media_path.resolve()),
                "profile": str(profile_path.resolve()),
                "model": str(model.resolve()),
                "references": str(refs.resolve()),
                "python": str(python.absolute()),
            },
            "context": context_enabled,
        }
        key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        directory = root / key
        cached = directory.exists()
        if cached:
            saved = json.loads((directory / "request.json").read_text())
            if not (directory / "terminal.json").is_file():
                raise ValueError("SPEAKER_REQUEST_INCOMPLETE")
            terminal = json.loads((directory / "terminal.json").read_text())
            execution = json.loads((directory / "execution.json").read_text())
            started = json.loads((directory / "started.json").read_text())
            if (
                saved != request
                or terminal.get("status") != "COMPLETE"
                or started.get("request_sha256") != key
                or execution.get("returncode") != 0
                or execution.get("timed_out") is not False
                or _sha(directory / "execution.json") != terminal.get("execution_sha256")
                or _sha(directory / "stdout.log") != terminal.get("stdout_sha256")
                or _sha(directory / "stderr.log") != terminal.get("stderr_sha256")
                or _sha(directory / "attribution.raw.json") != terminal.get("raw_sha256")
                or (directory / "input.srt").read_text() != srt_text
            ):
                raise ValueError("SPEAKER_CACHE_INVALID_OR_INCOMPLETE")
        else:
            directory.mkdir(exist_ok=False)
            _json(directory / "request.json", request)
            (directory / "input.srt").write_text(srt_text, encoding="utf-8")
            _json(directory / "started.json", {"status": "STARTED", "request_sha256": key})
            command = [
                str(python),
                "-B",
                "-m",
                "src.autoslice.speaker_attribution",
                "--media",
                str(media_path),
                "--srt",
                str(directory / "input.srt"),
                "--profile",
                str(profile_path),
                "--reference-dir",
                str(refs),
                "--model-dir",
                str(model),
                "--work-dir",
                str(directory / "work"),
                "--output",
                str(directory / "attribution.raw.json"),
                "--runtime-location",
                "producer-local-speaker-venv",
                "--timeline-offset-ms",
                str(timeline_offset_ms),
                "--source-end-ms",
                str(source_end_ms),
            ]
            if not context_enabled:
                command.append("--no-context")
            execution = _execute(command, directory)
            _json(directory / "execution.json", execution)
            if execution["returncode"] != 0 or execution.get("timed_out") is not False:
                raise ValueError("SPEAKER_RUNTIME_FAILED")
        raw = directory / "attribution.raw.json"
        doc = json.loads(raw.read_text())
        if inputs != _snapshot(media_path, profile_path, model, refs, python) or not _valid_output(
            doc, request, srt_text, directory
        ):
            raise ValueError("SPEAKER_RESULT_INVALID_OR_INPUT_DRIFT")
        if not cached:
            _json(
                directory / "terminal.json",
                {
                    "status": "COMPLETE",
                    "raw_sha256": _sha(raw),
                    "execution_sha256": _sha(directory / "execution.json"),
                    "stdout_sha256": _sha(directory / "stdout.log"),
                    "stderr_sha256": _sha(directory / "stderr.log"),
                },
            )
        return doc, {
            "schema_version": VERSION,
            "status": "CACHE_HIT" if cached else "GENERATED",
            "request_path": str(directory / "request.json"),
            "request_sha256": _sha(directory / "request.json"),
            "attribution_path": str(raw),
            "attribution_sha256": _sha(raw),
            "source_media_sha256": inputs["media_sha256"],
            "context_requested": context_enabled,
        }
    except Exception as exc:
        # Do not overwrite failed/partial originals or retry the same request.
        reason = (
            str(exc)
            if isinstance(exc, ValueError) and str(exc).startswith("SPEAKER_")
            else type(exc).__name__
        )
        unknown["reason_codes"] = [reason]
        if directory is None:
            directory = root / ("unavailable-" + uuid.uuid4().hex)
            directory.mkdir()
        fallback = directory / ("unavailable-" + uuid.uuid4().hex + ".json")
        _json(fallback, unknown)
        meta = {
            "schema_version": VERSION,
            "status": "UNKNOWN",
            "reason_code": reason,
            "attribution_path": str(fallback),
            "attribution_sha256": _sha(fallback),
            "source_media_sha256": (request or {}).get("inputs", {}).get("media_sha256"),
            "context_requested": options.get("context", False) is True,
        }
        _json(directory / ("failure-" + uuid.uuid4().hex + ".json"), meta)
        return unknown, meta
