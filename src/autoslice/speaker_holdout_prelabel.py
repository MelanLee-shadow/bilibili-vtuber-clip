"""Hash-bound, receipt-last core for one holdout ASR segment.

No real provider adapter lives here.  Callers must inject byte-producing
functions, and execution is refused unless the immutable plan explicitly binds
this runner and authorizes the external upload.  The overnight automation only
exercises this module with in-memory fakes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path


PLAN_SCHEMA = "speaker-holdout-extraction-plan.v1"
LEGACY_PLAN_SCHEMA = "speaker-holdout-extraction-plan.v0"
PLAN_PURPOSE = "HOLDOUT_PRELABEL_EXTRACTION_PLAN_ONLY_NO_TRUTH_PREDICTION_OR_PRODUCTION_AUTHORITY"
EXECUTION_STATUS = "EXTRACTION_PLAN_FROZEN_EXTERNAL_UPLOAD_AUTHORIZED"
RECEIPT_SCHEMA = "speaker-holdout-segment-extraction-receipt.v0"
RECEIPT_PURPOSE = "UNLABELED_HOLDOUT_ASR_CUE_SEGMENT_ONLY_NO_PREDICTION_OR_PRODUCTION_AUTHORITY"
SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
PLAN_FIELDS = {
    "schema_version",
    "purpose",
    "status",
    "source_freeze",
    "scratch",
    "policy",
    "toolchain",
    "sessions",
    "session_count",
    "segment_count",
    "execution_contract",
    "authority",
    "deterministic_payload_sha256",
}
ARTIFACT_OUTPUTS = {
    "canonical_pcm_s16le": "canonical-pcm.s16le",
    "asr_input_mp3": "asr-input-16k-mono-64k.mp3",
    "asr_normalized_json": "asr.normalized.json",
    "cue_table_json": "cue-table.json",
    "asr_srt": "asr.srt",
    "extraction_receipt": "extraction-receipt.json",
}


class SpeakerHoldoutPrelabelError(RuntimeError):
    pass


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def bytes_sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _regular_file(path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise SpeakerHoldoutPrelabelError(f"{label} is missing: {absolute}") from exc
    if not stat.S_ISREG(metadata.st_mode) or absolute.is_symlink() or absolute != resolved:
        raise SpeakerHoldoutPrelabelError(
            f"{label} must have a regular non-symlink path chain: {absolute}"
        )
    return resolved


def load_plan(path: Path, *, expected_file_sha256: str) -> dict[str, object]:
    path = _regular_file(path, label="holdout extraction plan")
    if file_sha256(path) != expected_file_sha256:
        raise SpeakerHoldoutPrelabelError("holdout extraction plan file hash drifted")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpeakerHoldoutPrelabelError("holdout extraction plan is invalid JSON") from exc
    if not isinstance(value, dict):
        raise SpeakerHoldoutPrelabelError("holdout extraction plan must be one object")
    validate_plan(value, require_execution_authority=False)
    return value


def validate_plan(plan: Mapping[str, object], *, require_execution_authority: bool) -> None:
    if set(plan) != PLAN_FIELDS:
        raise SpeakerHoldoutPrelabelError("holdout extraction plan field set drifted")
    if plan.get("schema_version") not in {PLAN_SCHEMA, LEGACY_PLAN_SCHEMA} or plan.get(
        "purpose"
    ) != PLAN_PURPOSE:
        raise SpeakerHoldoutPrelabelError("holdout extraction plan schema or purpose drifted")
    deterministic = {
        key: value for key, value in plan.items() if key != "deterministic_payload_sha256"
    }
    if plan.get("deterministic_payload_sha256") != canonical_sha256(deterministic):
        raise SpeakerHoldoutPrelabelError("holdout extraction plan payload hash drifted")
    policy = plan.get("policy")
    contract = plan.get("execution_contract")
    authority = plan.get("authority")
    toolchain = plan.get("toolchain")
    sessions = plan.get("sessions")
    if not all(isinstance(value, dict) for value in (policy, contract, authority, toolchain)):
        raise SpeakerHoldoutPrelabelError("holdout extraction plan policy fields are invalid")
    if not isinstance(sessions, list) or len(sessions) != plan.get("session_count"):
        raise SpeakerHoldoutPrelabelError("holdout extraction plan session count drifted")
    segment_count = sum(
        len(session.get("segments", []))
        for session in sessions
        if isinstance(session, dict) and isinstance(session.get("segments"), list)
    )
    if segment_count != plan.get("segment_count"):
        raise SpeakerHoldoutPrelabelError("holdout extraction plan segment count drifted")
    if (
        policy.get("asr_provider") != "bcut"
        or policy.get("asr_model_id") != "7"
        or policy.get("threshold_state") is not None
    ):
        raise SpeakerHoldoutPrelabelError("holdout extraction plan provider policy drifted")
    if any(
        contract.get(key) is not False
        for key in ("production_runner_allowed", "human_truth_allowed", "speaker_prediction_allowed")
    ):
        raise SpeakerHoldoutPrelabelError("holdout extraction plan crosses its authority boundary")
    if any(
        authority.get(key) is not False
        for key in (
            "predictions_frozen",
            "human_truth_opened",
            "production_authority",
            "deployment_authority",
        )
    ):
        raise SpeakerHoldoutPrelabelError("holdout extraction plan claims forbidden authority")
    if require_execution_authority:
        if plan.get("schema_version") != PLAN_SCHEMA:
            raise SpeakerHoldoutPrelabelError(
                "legacy extraction plan is validate-only and cannot execute"
            )
        wrapper = toolchain.get("hash_bound_run_one_wrapper")
        if (
            plan.get("status") != EXECUTION_STATUS
            or contract.get("real_provider_execution_authorized") is not True
            or contract.get("external_audio_upload_authorized") is not True
            or not isinstance(wrapper, dict)
            or set(wrapper) != {"path", "sha256"}
        ):
            raise SpeakerHoldoutPrelabelError("external provider execution is not authorized")


def _find_segment(plan: Mapping[str, object], segment_id: str) -> tuple[dict, dict]:
    matches: list[tuple[dict, dict]] = []
    for session in plan["sessions"]:
        for segment in session["segments"]:
            if segment.get("segment_id") == segment_id:
                matches.append((session, segment))
    if len(matches) != 1:
        raise SpeakerHoldoutPrelabelError("segment ID is not exactly one frozen plan member")
    return matches[0]


def _source_snapshot(segment: Mapping[str, object]) -> tuple[Path, tuple[int, int]]:
    source = segment.get("source")
    if not isinstance(source, dict):
        raise SpeakerHoldoutPrelabelError("segment source binding is missing")
    path = _regular_file(Path(str(source.get("path"))), label="holdout source media")
    before = path.stat()
    if (before.st_size, before.st_mtime_ns) != (
        source.get("size_bytes"),
        source.get("mtime_ns"),
    ):
        raise SpeakerHoldoutPrelabelError("holdout source stat drifted")
    if file_sha256(path) != source.get("sha256"):
        raise SpeakerHoldoutPrelabelError("holdout source bytes drifted")
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SpeakerHoldoutPrelabelError("holdout source changed while hashing")
    return path, (after.st_size, after.st_mtime_ns)


def _validate_output_contract(segment: Mapping[str, object], *, segment_id: str) -> dict[str, str]:
    outputs = segment.get("outputs")
    expected_fields = {"attempt_root_template", *ARTIFACT_OUTPUTS}
    if not isinstance(outputs, dict) or set(outputs) != expected_fields:
        raise SpeakerHoldoutPrelabelError("segment output contract field set drifted")
    expected_root = f"segments/{segment_id}/{{attempt_id}}"
    if outputs.get("attempt_root_template") != expected_root:
        raise SpeakerHoldoutPrelabelError("segment attempt root contract drifted")
    for key, expected_name in ARTIFACT_OUTPUTS.items():
        if outputs.get(key) != expected_name:
            raise SpeakerHoldoutPrelabelError(f"segment artifact contract drifted: {key}")
    return {key: str(outputs[key]) for key in ARTIFACT_OUTPUTS}


def normalize_asr(result: Mapping[str, object], *, pcm_sample_count: int) -> dict[str, object]:
    if result.get("provider") != "bcut":
        raise SpeakerHoldoutPrelabelError("ASR result provider is not bcut")
    utterances = result.get("utterances")
    if not isinstance(utterances, list):
        raise SpeakerHoldoutPrelabelError("ASR utterances must be a list")
    if isinstance(pcm_sample_count, bool) or not isinstance(pcm_sample_count, int) or pcm_sample_count <= 0:
        raise SpeakerHoldoutPrelabelError("canonical PCM sample count is invalid")
    normalized: list[dict[str, object]] = []
    previous_end = 0
    for index, row in enumerate(utterances, 1):
        if not isinstance(row, dict):
            raise SpeakerHoldoutPrelabelError("ASR utterance must be an object")
        start = row.get("start_time")
        end = row.get("end_time")
        transcript = row.get("transcript")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < previous_end
            or start < 0
            or end <= start
            or end * 16 > pcm_sample_count
            or not isinstance(transcript, str)
            or not transcript.strip()
        ):
            raise SpeakerHoldoutPrelabelError(f"ASR cue {index} bounds or text are invalid")
        words = row.get("words", [])
        if not isinstance(words, list):
            raise SpeakerHoldoutPrelabelError(f"ASR cue {index} words are invalid")
        normalized_words = []
        word_previous_end = start
        for word_index, word in enumerate(words, 1):
            if not isinstance(word, dict):
                raise SpeakerHoldoutPrelabelError(f"ASR cue {index} word is invalid")
            word_start = word.get("start_time")
            word_end = word.get("end_time")
            label = word.get("label")
            if (
                isinstance(word_start, bool)
                or isinstance(word_end, bool)
                or not isinstance(word_start, int)
                or not isinstance(word_end, int)
                or word_start < word_previous_end
                or word_start < start
                or word_end <= word_start
                or word_end > end
                or not isinstance(label, str)
                or not label.strip()
            ):
                raise SpeakerHoldoutPrelabelError(
                    f"ASR cue {index} word {word_index} bounds or label are invalid"
                )
            normalized_words.append(
                {"label": label, "start_time": word_start, "end_time": word_end}
            )
            word_previous_end = word_end
        normalized.append(
            {
                "cue_id": index,
                "start_ms": start,
                "end_ms": end,
                "start_sample": start * 16,
                "end_sample": end * 16,
                "transcript": transcript,
                "words": normalized_words,
            }
        )
        previous_end = end
    return {
        "provider": "bcut",
        "model_id": "7",
        "pcm_sample_count": pcm_sample_count,
        "pcm_duration_ms_floor": pcm_sample_count // 16,
        "utterances": normalized,
        "observation_fields_removed": ["elapsed_s"],
    }


def _srt_timestamp(milliseconds: int) -> str:
    return (
        f"{milliseconds // 3_600_000:02d}:"
        f"{milliseconds // 60_000 % 60:02d}:"
        f"{milliseconds // 1_000 % 60:02d},{milliseconds % 1_000:03d}"
    )


def render_srt(normalized: Mapping[str, object]) -> bytes:
    rows = []
    for cue in normalized["utterances"]:
        rows.append(
            f"{cue['cue_id']}\n{_srt_timestamp(cue['start_ms'])} --> "
            f"{_srt_timestamp(cue['end_ms'])}\n{cue['transcript']}\n"
        )
    return ("\n".join(rows) + ("\n" if rows else "")).encode("utf-8")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _safe_component(value: str, *, label: str) -> str:
    if not SAFE_COMPONENT.fullmatch(value) or value in {".", ".."}:
        raise SpeakerHoldoutPrelabelError(f"{label} is not one safe path component")
    return value


def _open_private_directory(path: Path, *, label: str) -> int:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise SpeakerHoldoutPrelabelError(f"{label} is missing") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or absolute.is_symlink()
        or absolute != resolved
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise SpeakerHoldoutPrelabelError(f"{label} must be current-owner mode 0700 non-symlink")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(absolute, flags)


def _mkdir_open(parent_fd: int, name: str, *, create_only: bool) -> int:
    _safe_component(name, label="directory component")
    if create_only:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise SpeakerHoldoutPrelabelError(f"attempt directory already exists: {name}") from exc
    else:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise SpeakerHoldoutPrelabelError(f"unsafe directory component: {name}") from exc
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        os.close(descriptor)
        raise SpeakerHoldoutPrelabelError(f"directory component is not private: {name}")
    return descriptor


def _write_exclusive(directory_fd: int, name: str, data: bytes) -> None:
    _safe_component(name, label="artifact name")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    except FileExistsError as exc:
        raise SpeakerHoldoutPrelabelError(f"artifact already exists: {name}") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short artifact write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run_one(
    *,
    plan: Mapping[str, object],
    segment_id: str,
    attempt_id: str,
    runner_path: Path,
    source_to_mp3: Callable[[Path], bytes],
    mp3_to_pcm_s16le: Callable[[bytes], bytes],
    transcribe_bcut: Callable[[bytes], Mapping[str, object]],
) -> dict[str, object]:
    validate_plan(plan, require_execution_authority=True)
    segment_id = _safe_component(segment_id, label="segment ID")
    attempt_id = _safe_component(attempt_id, label="attempt ID")
    session, segment = _find_segment(plan, segment_id)
    output_names = _validate_output_contract(segment, segment_id=segment_id)
    runner_path = _regular_file(runner_path, label="hash-bound run-one wrapper")
    wrapper = plan["toolchain"]["hash_bound_run_one_wrapper"]
    if str(runner_path) != wrapper["path"] or file_sha256(runner_path) != wrapper["sha256"]:
        raise SpeakerHoldoutPrelabelError("hash-bound run-one wrapper drifted")
    source_path, source_stat = _source_snapshot(segment)

    scratch = Path(str(plan["scratch"]["allowed_root"]))
    run_root = Path(str(plan["scratch"]["planned_run_root"]))
    if run_root.parent != scratch:
        raise SpeakerHoldoutPrelabelError("planned run root escaped scratch")
    root_fd = _open_private_directory(scratch, label="holdout scratch root")
    descriptors: list[int] = [root_fd]
    try:
        run_fd = _mkdir_open(root_fd, run_root.name, create_only=False)
        descriptors.append(run_fd)
        segments_fd = _mkdir_open(run_fd, "segments", create_only=False)
        descriptors.append(segments_fd)
        segment_fd = _mkdir_open(segments_fd, segment_id, create_only=False)
        descriptors.append(segment_fd)
        attempt_fd = _mkdir_open(segment_fd, attempt_id, create_only=True)
        descriptors.append(attempt_fd)

        mp3 = source_to_mp3(source_path)
        if not isinstance(mp3, bytes) or not mp3:
            raise SpeakerHoldoutPrelabelError("ASR input MP3 is empty or invalid")
        pcm = mp3_to_pcm_s16le(mp3)
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            raise SpeakerHoldoutPrelabelError(
                "canonical PCM is empty or not signed 16-bit samples"
            )
        source_after_extraction = source_path.stat()
        if (
            (source_after_extraction.st_size, source_after_extraction.st_mtime_ns) != source_stat
            or file_sha256(source_path) != segment["source"]["sha256"]
        ):
            raise SpeakerHoldoutPrelabelError("holdout source changed during input extraction")
        result = transcribe_bcut(mp3)
        normalized = normalize_asr(result, pcm_sample_count=len(pcm) // 2)
        normalized_bytes = _json_bytes(normalized)
        cue_table = {
            "schema_version": "speaker-holdout-asr-cue-table.v0",
            "session_group_id": session["session_group_id"],
            "split_role": session["split_role"],
            "segment_id": segment_id,
            "source_media_sha256": segment["source"]["sha256"],
            "truth_state": "UNLABELED_LOCKED",
            "prediction_state": "NOT_RUN",
            "cues": normalized["utterances"],
        }
        cue_bytes = _json_bytes(cue_table)
        srt_bytes = render_srt(normalized)
        artifacts = {
            output_names["asr_input_mp3"]: mp3,
            output_names["canonical_pcm_s16le"]: pcm,
            output_names["asr_normalized_json"]: normalized_bytes,
            output_names["cue_table_json"]: cue_bytes,
            output_names["asr_srt"]: srt_bytes,
        }
        receipt_payload: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA,
        "purpose": RECEIPT_PURPOSE,
        "status": (
            "ASR_EMPTY_SEGMENT_RETAINED"
            if not normalized["utterances"]
            else "ASR_CUE_GRID_SEGMENT_FROZEN"
        ),
        "plan_sha256": plan["deterministic_payload_sha256"],
        "session_group_id": session["session_group_id"],
        "split_role": session["split_role"],
        "segment_id": segment_id,
        "attempt_id": attempt_id,
        "source": segment["source"],
        "provider": {"name": "bcut", "model_id": "7"},
        "canonical_pcm": {
            "sha256": bytes_sha256(pcm),
            "sample_count": len(pcm) // 2,
            "sample_rate_hz": 16_000,
            "channels": 1,
            "sample_width_bytes": 2,
            "endianness": "little",
        },
        "cue_count": len(normalized["utterances"]),
        "artifacts": {
            name: {"sha256": bytes_sha256(data), "size_bytes": len(data)}
            for name, data in sorted(artifacts.items())
        },
        "authority": {
            "segment_asr_cue_frozen": True,
            "aggregate_asr_frozen": False,
            "predictions_frozen": False,
            "human_truth_opened": False,
            "production_authority": False,
            "deployment_authority": False,
        },
        }
        receipt_payload["deterministic_payload_sha256"] = canonical_sha256(receipt_payload)
        receipt_bytes = _json_bytes(receipt_payload)
        for name, data in artifacts.items():
            _write_exclusive(attempt_fd, name, data)
        _write_exclusive(attempt_fd, output_names["extraction_receipt"], receipt_bytes)
        os.fsync(attempt_fd)
        return receipt_payload
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
