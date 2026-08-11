"""Hash-bound, receipt-last core for holdout ASR segments and aggregates.

No real provider adapter lives here.  Callers must inject byte-producing
functions, and execution is refused unless the immutable plan explicitly binds
this runner and authorizes the external upload.  The overnight automation only
exercises this module with in-memory fakes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping
from pathlib import Path


PLAN_SCHEMA = "speaker-holdout-extraction-plan.v1"
LEGACY_PLAN_SCHEMA = "speaker-holdout-extraction-plan.v0"
PLAN_PURPOSE = "HOLDOUT_PRELABEL_EXTRACTION_PLAN_ONLY_NO_TRUTH_PREDICTION_OR_PRODUCTION_AUTHORITY"
EXECUTION_STATUS = "EXTRACTION_PLAN_FROZEN_EXTERNAL_UPLOAD_AUTHORIZED"
EXECUTION_RUNTIME_STATE = "BOUND_EXTERNAL_UPLOAD_AUTHORIZED"
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
AGGREGATE_RECEIPT_NAME = "aggregate-asr-cue-receipt.json"
RUN_LOCK_NAME = "prelabel-run.lock"
AGGREGATE_RECEIPT_SCHEMA = "speaker-holdout-aggregate-extraction-receipt.v0"
AGGREGATE_RECEIPT_PURPOSE = (
    "UNLABELED_HOLDOUT_ASR_CUE_GRID_ONLY_NO_PREDICTION_OR_PRODUCTION_AUTHORITY"
)
EXPECTED_HOLDOUT_ACCEPTANCE_ID = (
    "lidousha-speaker-holdout-source-20260810-20260811.v0"
)
EXPECTED_HOLDOUT_SOURCE_PAYLOAD_SHA256 = (
    "sha256:c4e27632a0c279747697e3168d2a91cab535967a5d186fcd3e0c5cb0ff284184"
)
EXPECTED_HOLDOUT_REPLAY_FILE_SHA256 = frozenset(
    {
        "e80baec84ac8de4bca7de5a1edb265da732de2c4cf674aba29b8886c4c37de57",
        "18c71fe21a3f4ab89c0233b053a6e030308b57c8f4eb56745c7d4576c4ba7370",
    }
)
EXPECTED_HOLDOUT_SESSIONS = {
    "22966160:2026-08-10": {
        "session_date": "2026-08-10",
        "split_role": "HOLDOUT_A_SOURCE_FROZEN_UNLABELED",
        "segment_count": 10,
    },
    "22966160:2026-08-11": {
        "session_date": "2026-08-11",
        "split_role": "HOLDOUT_B_SOURCE_FROZEN_UNLABELED",
        "segment_count": 5,
    },
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


def _read_regular_file_bytes(path: Path, *, label: str) -> tuple[Path, bytes]:
    """Read one regular file through one descriptor and reject namespace races."""

    resolved = _regular_file(path, label=label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
        try:
            before = os.fstat(descriptor)
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 4 * 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = resolved.lstat()
    except OSError as exc:
        raise SpeakerHoldoutPrelabelError(f"{label} changed while reading") from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or before_identity != after_identity
        or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise SpeakerHoldoutPrelabelError(f"{label} changed while reading")
    return resolved, b"".join(chunks)


def load_plan(path: Path, *, expected_file_sha256: str) -> dict[str, object]:
    _, plan_bytes = _read_regular_file_bytes(path, label="holdout extraction plan")
    if bytes_sha256(plan_bytes) != expected_file_sha256:
        raise SpeakerHoldoutPrelabelError("holdout extraction plan file hash drifted")
    try:
        value = json.loads(plan_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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
            or contract.get("plan_only") is not False
            or contract.get("real_provider_execution_authorized") is not True
            or contract.get("external_audio_upload_authorized") is not True
            or toolchain.get("runtime_binding_state") != EXECUTION_RUNTIME_STATE
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
    created = False
    if create_only:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError as exc:
            raise SpeakerHoldoutPrelabelError(f"attempt directory already exists: {name}") from exc
    else:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
    if created:
        os.fsync(parent_fd)
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


def _open_run_lock(run_fd: int) -> int:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    created = False
    try:
        descriptor = os.open(
            RUN_LOCK_NAME,
            flags | os.O_EXCL,
            0o600,
            dir_fd=run_fd,
        )
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(RUN_LOCK_NAME, flags, 0o600, dir_fd=run_fd)
        except OSError as exc:
            raise SpeakerHoldoutPrelabelError("run lock is missing or unsafe") from exc
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_nlink != 1
    ):
        os.close(descriptor)
        raise SpeakerHoldoutPrelabelError("run lock is not a private regular file")
    if created:
        os.fsync(run_fd)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise SpeakerHoldoutPrelabelError("holdout run is busy") from exc
    return descriptor


def _aggregate_receipt_exists(run_fd: int) -> bool:
    try:
        os.stat(AGGREGATE_RECEIPT_NAME, dir_fd=run_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SpeakerHoldoutPrelabelError("cannot inspect aggregate seal") from exc
    return True


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
        lock_fd = _open_run_lock(run_fd)
        descriptors.append(lock_fd)
        if _aggregate_receipt_exists(run_fd):
            raise SpeakerHoldoutPrelabelError("holdout run is sealed by aggregate receipt")
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


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _open_child_directory(parent_fd: int, name: str, *, label: str) -> int:
    _safe_component(name, label=label)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise SpeakerHoldoutPrelabelError(f"{label} is missing or unsafe: {name}") from exc
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        os.close(descriptor)
        raise SpeakerHoldoutPrelabelError(f"{label} is not a private directory: {name}")
    return descriptor


def _directory_names(descriptor: int, *, label: str) -> list[str]:
    try:
        names = os.listdir(descriptor)
    except OSError as exc:
        raise SpeakerHoldoutPrelabelError(f"cannot list {label}") from exc
    if not all(isinstance(name, str) and SAFE_COMPONENT.fullmatch(name) for name in names):
        raise SpeakerHoldoutPrelabelError(f"{label} contains an unsafe entry")
    return sorted(names)


def _read_child_file(directory_fd: int, name: str, *, label: str) -> bytes:
    _safe_component(name, label=label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            before = os.fstat(descriptor)
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 4 * 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise SpeakerHoldoutPrelabelError(f"{label} changed while reading") from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) & 0o077
        or before.st_nlink != 1
        or before_identity != after_identity
        or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise SpeakerHoldoutPrelabelError(f"{label} is not a stable private file")
    return b"".join(chunks)


def _load_canonical_object(data: bytes, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpeakerHoldoutPrelabelError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict) or _json_bytes(value) != data:
        raise SpeakerHoldoutPrelabelError(f"{label} is not one canonical JSON object")
    return value


def _validate_aggregate_plan(
    plan: Mapping[str, object],
    *,
    expected_replay_file_sha256: frozenset[str],
) -> list[tuple[dict, dict]]:
    validate_plan(plan, require_execution_authority=True)
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise SpeakerHoldoutPrelabelError("aggregate requires the v1 execution plan")
    source_freeze = plan.get("source_freeze")
    if (
        not isinstance(source_freeze, dict)
        or set(source_freeze)
        != {
            "acceptance_id",
            "deterministic_payload_sha256",
            "source_inventory_stat_sha256",
            "replays",
        }
        or source_freeze.get("acceptance_id") != EXPECTED_HOLDOUT_ACCEPTANCE_ID
        or source_freeze.get("deterministic_payload_sha256")
        != EXPECTED_HOLDOUT_SOURCE_PAYLOAD_SHA256
    ):
        raise SpeakerHoldoutPrelabelError("aggregate source-freeze acceptance drifted")
    replays = source_freeze.get("replays")
    if not isinstance(replays, list) or len(replays) != 2:
        raise SpeakerHoldoutPrelabelError("aggregate source-freeze replay set drifted")
    observed_replay_hashes: set[str] = set()
    replay_manifests: list[dict[str, object]] = []
    for replay in replays:
        if (
            not isinstance(replay, dict)
            or set(replay) != {"path", "file_sha256"}
            or not isinstance(replay.get("path"), str)
            or not Path(replay["path"]).is_absolute()
            or not isinstance(replay.get("file_sha256"), str)
        ):
            raise SpeakerHoldoutPrelabelError("aggregate source-freeze replay binding drifted")
        replay_path, replay_bytes = _read_regular_file_bytes(
            Path(replay["path"]), label="source-freeze replay"
        )
        replay_hash = bytes_sha256(replay_bytes).removeprefix("sha256:")
        if replay_hash != replay["file_sha256"] or str(replay_path) != replay["path"]:
            raise SpeakerHoldoutPrelabelError("aggregate source-freeze replay bytes drifted")
        observed_replay_hashes.add(replay_hash)
        try:
            manifest = json.loads(replay_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpeakerHoldoutPrelabelError(
                "aggregate source-freeze replay is invalid JSON"
            ) from exc
        if not isinstance(manifest, dict):
            raise SpeakerHoldoutPrelabelError(
                "aggregate source-freeze replay must be one object"
            )
        replay_manifests.append(manifest)
    if observed_replay_hashes != set(expected_replay_file_sha256):
        raise SpeakerHoldoutPrelabelError("aggregate source-freeze replays are not accepted")
    deterministic_replays = [
        {
            key: value
            for key, value in manifest.items()
            if key not in {"deterministic_payload_sha256", "observation"}
        }
        for manifest in replay_manifests
    ]
    if (
        any(
            manifest.get("deterministic_payload_sha256")
            != EXPECTED_HOLDOUT_SOURCE_PAYLOAD_SHA256
            for manifest in replay_manifests
        )
        or deterministic_replays[0] != deterministic_replays[1]
        or replay_manifests[0].get("source_inventory_stat_sha256")
        != source_freeze.get("source_inventory_stat_sha256")
    ):
        raise SpeakerHoldoutPrelabelError(
            "aggregate source-freeze deterministic population drifted"
        )
    frozen_segments = replay_manifests[0].get("segments")
    if not isinstance(frozen_segments, list) or len(frozen_segments) != 15:
        raise SpeakerHoldoutPrelabelError(
            "aggregate source-freeze population is not exact 10+5"
        )
    expected_plan_members: list[dict[str, object]] = []
    for frozen in frozen_segments:
        if not isinstance(frozen, dict) or set(frozen) != {
            "session_date",
            "split_role",
            "segment_id",
            "source_media",
            "source_flv_evidence",
            "sidecars",
            "meta_event_evidence",
            "next_state",
        }:
            raise SpeakerHoldoutPrelabelError(
                "aggregate source-freeze segment field set drifted"
            )
        source_media = frozen.get("source_media")
        if not isinstance(source_media, dict) or set(source_media) != {
            "path",
            "size_bytes",
            "mtime_ns",
            "sha256",
            "adapter_target_sha256",
            "ffprobe",
        }:
            raise SpeakerHoldoutPrelabelError(
                "aggregate source-freeze media field set drifted"
            )
        probe = source_media.get("ffprobe")
        if not isinstance(probe, dict):
            raise SpeakerHoldoutPrelabelError(
                "aggregate source-freeze ffprobe binding drifted"
            )
        expected_plan_members.append(
            {
                "session_date": frozen.get("session_date"),
                "split_role": frozen.get("split_role"),
                "segment_id": frozen.get("segment_id"),
                "source": {
                    "path": source_media.get("path"),
                    "size_bytes": source_media.get("size_bytes"),
                    "mtime_ns": source_media.get("mtime_ns"),
                    "sha256": source_media.get("sha256"),
                    "adapter_target_sha256": source_media.get(
                        "adapter_target_sha256"
                    ),
                    "container_duration_ms": probe.get("duration_ms"),
                },
            }
        )
    policy = plan.get("policy")
    if (
        not isinstance(policy, dict)
        or policy.get("session_grouping") != "ROOM_AND_DATE_ONE_SESSION"
        or policy.get("selection")
        != "COMPLETE_FROZEN_SOURCE_INVENTORY_NO_SCORE_OR_TRUTH_FILTER"
        or policy.get("canonical_pcm")
        != {
            "sample_rate_hz": 16_000,
            "channels": 1,
            "sample_width_bytes": 2,
            "endianness": "little",
            "container": "raw_s16le",
        }
        or policy.get("canonical_pcm_cross_session_dedup_required") is not True
    ):
        raise SpeakerHoldoutPrelabelError("aggregate plan policy drifted")
    sessions = plan.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != 2:
        raise SpeakerHoldoutPrelabelError("aggregate requires exactly two sessions")
    validated: list[tuple[dict, dict]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    seen_source_hashes: set[str] = set()
    observed_sessions: set[str] = set()
    for session in sessions:
        if not isinstance(session, dict) or set(session) != {
            "session_group_id",
            "session_date",
            "split_role",
            "segments",
        }:
            raise SpeakerHoldoutPrelabelError("aggregate session field set drifted")
        session_id = session.get("session_group_id")
        expected = EXPECTED_HOLDOUT_SESSIONS.get(str(session_id))
        segments = session.get("segments")
        if (
            expected is None
            or session.get("session_date") != expected["session_date"]
            or session.get("split_role") != expected["split_role"]
            or not isinstance(segments, list)
            or len(segments) != expected["segment_count"]
            or str(session_id) in observed_sessions
        ):
            raise SpeakerHoldoutPrelabelError("aggregate session population drifted")
        observed_sessions.add(str(session_id))
        for segment in segments:
            if not isinstance(segment, dict) or set(segment) != {
                "session_date",
                "split_role",
                "segment_id",
                "source",
                "outputs",
                "state",
            }:
                raise SpeakerHoldoutPrelabelError("aggregate segment field set drifted")
            segment_id = segment.get("segment_id")
            source = segment.get("source")
            if (
                not isinstance(segment_id, str)
                or not SAFE_COMPONENT.fullmatch(segment_id)
                or segment_id in seen_ids
                or segment.get("session_date") != session["session_date"]
                or segment.get("split_role") != session["split_role"]
                or segment.get("state") != "PLANNED_NOT_EXECUTED"
                or not isinstance(source, dict)
                or set(source)
                != {
                    "path",
                    "size_bytes",
                    "mtime_ns",
                    "sha256",
                    "adapter_target_sha256",
                    "container_duration_ms",
                }
                or not _is_sha256(source.get("sha256"))
                or source.get("adapter_target_sha256") != source.get("sha256")
                or not isinstance(source.get("path"), str)
                or not Path(source["path"]).is_absolute()
                or source["path"] in seen_paths
                or source.get("sha256") in seen_source_hashes
            ):
                raise SpeakerHoldoutPrelabelError("aggregate segment identity drifted")
            _validate_output_contract(segment, segment_id=segment_id)
            seen_ids.add(segment_id)
            seen_paths.add(source["path"])
            seen_source_hashes.add(str(source["sha256"]))
            validated.append((session, segment))
    if observed_sessions != set(EXPECTED_HOLDOUT_SESSIONS) or len(validated) != 15:
        raise SpeakerHoldoutPrelabelError("aggregate requires the exact 10+5 population")
    observed_plan_members = [
        {
            "session_date": segment["session_date"],
            "split_role": segment["split_role"],
            "segment_id": segment["segment_id"],
            "source": segment["source"],
        }
        for _, segment in validated
    ]
    def member_key(row: Mapping[str, object]) -> tuple[str, str]:
        return str(row["session_date"]), str(row["segment_id"])

    if sorted(observed_plan_members, key=member_key) != sorted(
        expected_plan_members, key=member_key
    ):
        raise SpeakerHoldoutPrelabelError(
            "aggregate plan membership differs from accepted source-freeze replay"
        )
    return validated


def _replay_normalized_asr(
    normalized: Mapping[str, object], *, pcm_sample_count: int
) -> dict[str, object]:
    utterances = normalized.get("utterances")
    if not isinstance(utterances, list):
        raise SpeakerHoldoutPrelabelError("normalized ASR utterances are invalid")
    try:
        raw = {
            "provider": "bcut",
            "utterances": [
                {
                    "start_time": cue["start_ms"],
                    "end_time": cue["end_ms"],
                    "transcript": cue["transcript"],
                    "words": cue["words"],
                }
                for cue in utterances
            ],
        }
    except (KeyError, TypeError) as exc:
        raise SpeakerHoldoutPrelabelError("normalized ASR cue structure drifted") from exc
    replayed = normalize_asr(raw, pcm_sample_count=pcm_sample_count)
    if replayed != normalized:
        raise SpeakerHoldoutPrelabelError("normalized ASR deterministic replay drifted")
    return replayed


def _validate_success_attempt(
    *,
    attempt_fd: int,
    attempt_id: str,
    session: Mapping[str, object],
    segment: Mapping[str, object],
    decode_mp3: Callable[[bytes], bytes],
) -> dict[str, object]:
    output_names = _validate_output_contract(segment, segment_id=str(segment["segment_id"]))
    expected_names = set(output_names.values())
    if set(_directory_names(attempt_fd, label="successful attempt")) != expected_names:
        raise SpeakerHoldoutPrelabelError("successful attempt artifact set drifted")
    artifact_bytes = {
        name: _read_child_file(attempt_fd, name, label=f"attempt artifact {name}")
        for name in sorted(expected_names)
    }
    receipt_name = output_names["extraction_receipt"]
    receipt_bytes = artifact_bytes.pop(receipt_name)
    receipt = _load_canonical_object(receipt_bytes, label="segment extraction receipt")
    receipt_fields = {
        "schema_version",
        "purpose",
        "status",
        "plan_sha256",
        "session_group_id",
        "split_role",
        "segment_id",
        "attempt_id",
        "source",
        "provider",
        "canonical_pcm",
        "cue_count",
        "artifacts",
        "authority",
        "deterministic_payload_sha256",
    }
    deterministic = {
        key: value
        for key, value in receipt.items()
        if key != "deterministic_payload_sha256"
    }
    if (
        set(receipt) != receipt_fields
        or receipt.get("schema_version") != RECEIPT_SCHEMA
        or receipt.get("purpose") != RECEIPT_PURPOSE
        or receipt.get("plan_sha256") != segment["_plan_sha256"]
        or receipt.get("session_group_id") != session["session_group_id"]
        or receipt.get("split_role") != session["split_role"]
        or receipt.get("segment_id") != segment["segment_id"]
        or receipt.get("attempt_id") != attempt_id
        or receipt.get("source") != segment["source"]
        or receipt.get("provider") != {"name": "bcut", "model_id": "7"}
        or receipt.get("deterministic_payload_sha256") != canonical_sha256(deterministic)
    ):
        raise SpeakerHoldoutPrelabelError("segment extraction receipt binding drifted")
    expected_authority = {
        "segment_asr_cue_frozen": True,
        "aggregate_asr_frozen": False,
        "predictions_frozen": False,
        "human_truth_opened": False,
        "production_authority": False,
        "deployment_authority": False,
    }
    if receipt.get("authority") != expected_authority:
        raise SpeakerHoldoutPrelabelError("segment extraction receipt authority drifted")
    expected_artifacts = set(output_names.values()) - {receipt_name}
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != expected_artifacts:
        raise SpeakerHoldoutPrelabelError("segment receipt artifact table drifted")
    for name in sorted(expected_artifacts):
        binding = artifacts.get(name)
        data = artifact_bytes[name]
        if binding != {"sha256": bytes_sha256(data), "size_bytes": len(data)}:
            raise SpeakerHoldoutPrelabelError(f"segment artifact binding drifted: {name}")
    pcm = artifact_bytes[output_names["canonical_pcm_s16le"]]
    if not pcm or len(pcm) % 2:
        raise SpeakerHoldoutPrelabelError("aggregate canonical PCM is invalid")
    pcm_sample_count = len(pcm) // 2
    canonical_pcm = {
        "sha256": bytes_sha256(pcm),
        "sample_count": pcm_sample_count,
        "sample_rate_hz": 16_000,
        "channels": 1,
        "sample_width_bytes": 2,
        "endianness": "little",
    }
    if receipt.get("canonical_pcm") != canonical_pcm:
        raise SpeakerHoldoutPrelabelError("segment canonical PCM receipt drifted")
    decoded_pcm = decode_mp3(artifact_bytes[output_names["asr_input_mp3"]])
    if not isinstance(decoded_pcm, bytes) or decoded_pcm != pcm:
        raise SpeakerHoldoutPrelabelError("ASR MP3 to canonical PCM replay drifted")
    normalized = _load_canonical_object(
        artifact_bytes[output_names["asr_normalized_json"]], label="normalized ASR"
    )
    normalized = _replay_normalized_asr(normalized, pcm_sample_count=pcm_sample_count)
    cue_table = _load_canonical_object(
        artifact_bytes[output_names["cue_table_json"]], label="ASR cue table"
    )
    expected_cue_table = {
        "schema_version": "speaker-holdout-asr-cue-table.v0",
        "session_group_id": session["session_group_id"],
        "split_role": session["split_role"],
        "segment_id": segment["segment_id"],
        "source_media_sha256": segment["source"]["sha256"],
        "truth_state": "UNLABELED_LOCKED",
        "prediction_state": "NOT_RUN",
        "cues": normalized["utterances"],
    }
    if cue_table != expected_cue_table:
        raise SpeakerHoldoutPrelabelError("ASR cue table semantic replay drifted")
    if artifact_bytes[output_names["asr_srt"]] != render_srt(normalized):
        raise SpeakerHoldoutPrelabelError("ASR SRT deterministic replay drifted")
    cue_count = len(normalized["utterances"])
    expected_status = (
        "ASR_EMPTY_SEGMENT_RETAINED" if cue_count == 0 else "ASR_CUE_GRID_SEGMENT_FROZEN"
    )
    if receipt.get("cue_count") != cue_count or receipt.get("status") != expected_status:
        raise SpeakerHoldoutPrelabelError("segment cue count or status drifted")
    return {
        "segment_id": segment["segment_id"],
        "attempt_id": attempt_id,
        "status": expected_status,
        "source": segment["source"],
        "source_media_sha256": segment["source"]["sha256"],
        "receipt_relative_path": (
            f"segments/{segment['segment_id']}/{attempt_id}/{receipt_name}"
        ),
        "receipt_file_sha256": bytes_sha256(receipt_bytes),
        "receipt_payload_sha256": receipt["deterministic_payload_sha256"],
        "canonical_pcm": canonical_pcm,
        "cue_count": cue_count,
        "artifacts": artifacts,
    }


def _materialize_aggregate(
    *,
    plan: Mapping[str, object],
    plan_file_sha256: str,
    verifier_path: Path,
    run_fd: int,
    decode_mp3: Callable[[bytes], bytes],
    expected_replay_file_sha256: frozenset[str],
) -> dict[str, object]:
    validated = _validate_aggregate_plan(
        plan, expected_replay_file_sha256=expected_replay_file_sha256
    )
    root_names = set(_directory_names(run_fd, label="planned run root"))
    if AGGREGATE_RECEIPT_NAME in root_names:
        raise SpeakerHoldoutPrelabelError("aggregate receipt already exists")
    if root_names != {RUN_LOCK_NAME, "segments"}:
        raise SpeakerHoldoutPrelabelError("planned run root entry set drifted")
    segments_fd = _open_child_directory(run_fd, "segments", label="segments directory")
    try:
        expected_segment_ids = {str(segment["segment_id"]) for _, segment in validated}
        if set(_directory_names(segments_fd, label="segments directory")) != expected_segment_ids:
            raise SpeakerHoldoutPrelabelError("aggregate segment directory population drifted")
        session_rows: dict[str, list[dict[str, object]]] = {
            session_id: [] for session_id in EXPECTED_HOLDOUT_SESSIONS
        }
        pcm_sessions: dict[str, set[str]] = {}
        for session, original_segment in sorted(
            validated,
            key=lambda item: (item[0]["session_group_id"], item[1]["segment_id"]),
        ):
            segment = dict(original_segment)
            segment["_plan_sha256"] = plan["deterministic_payload_sha256"]
            segment_id = str(segment["segment_id"])
            segment_fd = _open_child_directory(
                segments_fd, segment_id, label="segment directory"
            )
            try:
                attempt_names = _directory_names(segment_fd, label="segment attempts")
                if not attempt_names:
                    raise SpeakerHoldoutPrelabelError(
                        f"segment has no extraction attempts: {segment_id}"
                    )
                successes: list[dict[str, object]] = []
                non_success_attempt_ids: list[str] = []
                for attempt_id in attempt_names:
                    attempt_fd = _open_child_directory(
                        segment_fd, attempt_id, label="attempt directory"
                    )
                    try:
                        names = set(_directory_names(attempt_fd, label="attempt directory"))
                        allowed = set(segment["outputs"].values())
                        if not names <= allowed:
                            raise SpeakerHoldoutPrelabelError(
                                f"attempt contains unexpected artifacts: {segment_id}"
                            )
                        if segment["outputs"]["extraction_receipt"] in names:
                            successes.append(
                                _validate_success_attempt(
                                    attempt_fd=attempt_fd,
                                    attempt_id=attempt_id,
                                    session=session,
                                    segment=segment,
                                    decode_mp3=decode_mp3,
                                )
                            )
                        else:
                            non_success_attempt_ids.append(attempt_id)
                    finally:
                        os.close(attempt_fd)
                if len(successes) != 1:
                    raise SpeakerHoldoutPrelabelError(
                        f"segment must have exactly one success receipt: {segment_id}"
                    )
                summary = successes[0]
                summary["non_success_attempt_ids"] = non_success_attempt_ids
                session_id = str(session["session_group_id"])
                session_rows[session_id].append(summary)
                pcm_sessions.setdefault(
                    str(summary["canonical_pcm"]["sha256"]), set()
                ).add(session_id)
            finally:
                os.close(segment_fd)
    finally:
        os.close(segments_fd)
    duplicated = sorted(
        pcm_sha for pcm_sha, sessions in pcm_sessions.items() if len(sessions) > 1
    )
    if duplicated:
        raise SpeakerHoldoutPrelabelError(
            "canonical PCM is duplicated across holdout sessions"
        )
    sessions_payload = []
    for session_id in sorted(session_rows):
        rows = sorted(session_rows[session_id], key=lambda row: str(row["segment_id"]))
        expected = EXPECTED_HOLDOUT_SESSIONS[session_id]
        sessions_payload.append(
            {
                "session_group_id": session_id,
                "session_date": expected["session_date"],
                "split_role": expected["split_role"],
                "segment_count": len(rows),
                "cue_count": sum(int(row["cue_count"]) for row in rows),
                "pcm_sample_count": sum(
                    int(row["canonical_pcm"]["sample_count"]) for row in rows
                ),
                "empty_segment_count": sum(int(row["cue_count"] == 0) for row in rows),
                "canonical_session_pcm_sha256": canonical_sha256(
                    [
                        {
                            "segment_id": row["segment_id"],
                            "sha256": row["canonical_pcm"]["sha256"],
                            "sample_count": row["canonical_pcm"]["sample_count"],
                        }
                        for row in rows
                    ]
                ),
                "segments": rows,
            }
        )
    payload: dict[str, object] = {
        "schema_version": AGGREGATE_RECEIPT_SCHEMA,
        "purpose": AGGREGATE_RECEIPT_PURPOSE,
        "status": "ASR_CUE_PACKAGE_FROZEN_PREDICTIONS_NOT_RUN",
        "plan": {
            "path": plan["scratch"]["plan_path"],
            "file_sha256": plan_file_sha256,
            "deterministic_payload_sha256": plan["deterministic_payload_sha256"],
            "schema_version": plan["schema_version"],
            "source_freeze_payload_sha256": EXPECTED_HOLDOUT_SOURCE_PAYLOAD_SHA256,
        },
        "verifier": {
            "path": str(verifier_path),
            "sha256": file_sha256(verifier_path),
            "core_sha256": plan["toolchain"]["run_one_core"]["sha256"],
        },
        "toolchain": plan["toolchain"],
        "sessions": sessions_payload,
        "session_count": len(sessions_payload),
        "segment_count": sum(row["segment_count"] for row in sessions_payload),
        "cue_count": sum(row["cue_count"] for row in sessions_payload),
        "pcm_sample_count": sum(row["pcm_sample_count"] for row in sessions_payload),
        "empty_segment_count": sum(
            row["empty_segment_count"] for row in sessions_payload
        ),
        "cross_session_pcm_duplicate_count": 0,
        "authority": {
            "aggregate_asr_cue_package_frozen": True,
            "predictions_frozen": False,
            "human_truth_opened": False,
            "production_authority": False,
            "deployment_authority": False,
            "next_required_state": "FREEZE_PRELABEL_PREDICTIONS_BEFORE_HUMAN_TRUTH",
        },
    }
    payload["deterministic_payload_sha256"] = canonical_sha256(payload)
    return payload


def _publish_aggregate_receipt(run_fd: int, data: bytes) -> None:
    temporary_name = f"aggregate-tmp-{secrets.token_hex(12)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=run_fd)
    temporary_exists = True
    published = False
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short aggregate receipt write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(
                temporary_name,
                AGGREGATE_RECEIPT_NAME,
                src_dir_fd=run_fd,
                dst_dir_fd=run_fd,
                follow_symlinks=False,
            )
            published = True
        except FileExistsError as exc:
            raise SpeakerHoldoutPrelabelError("aggregate receipt already exists") from exc
        try:
            os.fsync(run_fd)
        except OSError as exc:
            raise SpeakerHoldoutPrelabelError(
                "COMMITTED_BUT_DURABILITY_UNCONFIRMED: aggregate receipt"
            ) from exc
        os.unlink(temporary_name, dir_fd=run_fd)
        temporary_exists = False
        os.fsync(run_fd)
    finally:
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=run_fd)
                if not published:
                    os.fsync(run_fd)
            except FileNotFoundError:
                pass


def finalize_aggregate(
    *,
    plan_path: Path,
    expected_plan_file_sha256: str,
    verifier_path: Path,
    decode_mp3: Callable[[bytes], bytes],
    expected_replay_file_sha256: frozenset[str] = EXPECTED_HOLDOUT_REPLAY_FILE_SHA256,
) -> dict[str, object]:
    if not _is_sha256(expected_plan_file_sha256):
        raise SpeakerHoldoutPrelabelError("expected plan file SHA-256 is invalid")
    plan_path = _regular_file(plan_path, label="holdout extraction plan")
    plan = load_plan(plan_path, expected_file_sha256=expected_plan_file_sha256)
    _validate_aggregate_plan(
        plan, expected_replay_file_sha256=expected_replay_file_sha256
    )
    if str(plan_path) != plan["scratch"]["plan_path"]:
        raise SpeakerHoldoutPrelabelError("active plan path is not the bound plan path")
    verifier_path = _regular_file(verifier_path, label="aggregate verifier")
    verifier_binding = plan["toolchain"].get("hash_bound_aggregate_verifier")
    core_binding = plan["toolchain"].get("run_one_core")
    current_core = _regular_file(Path(__file__), label="run-one core")
    if (
        verifier_binding
        != {"path": str(verifier_path), "sha256": file_sha256(verifier_path)}
        or core_binding != {"path": str(current_core), "sha256": file_sha256(current_core)}
    ):
        raise SpeakerHoldoutPrelabelError("aggregate verifier or core binding drifted")
    scratch = Path(str(plan["scratch"]["allowed_root"]))
    run_root = Path(str(plan["scratch"]["planned_run_root"]))
    if run_root.parent != scratch:
        raise SpeakerHoldoutPrelabelError("aggregate run root escaped scratch")
    root_fd = _open_private_directory(scratch, label="holdout scratch root")
    try:
        run_fd = _open_child_directory(root_fd, run_root.name, label="planned run root")
        try:
            lock_fd = _open_run_lock(run_fd)
            try:
                first = _materialize_aggregate(
                    plan=plan,
                    plan_file_sha256=expected_plan_file_sha256,
                    verifier_path=verifier_path,
                    run_fd=run_fd,
                    decode_mp3=decode_mp3,
                    expected_replay_file_sha256=expected_replay_file_sha256,
                )
                second = _materialize_aggregate(
                    plan=plan,
                    plan_file_sha256=expected_plan_file_sha256,
                    verifier_path=verifier_path,
                    run_fd=run_fd,
                    decode_mp3=decode_mp3,
                    expected_replay_file_sha256=expected_replay_file_sha256,
                )
                if first != second:
                    raise SpeakerHoldoutPrelabelError(
                        "NONDETERMINISTIC_EXTRACTION: aggregate replay changed"
                    )
                reloaded = load_plan(
                    plan_path, expected_file_sha256=expected_plan_file_sha256
                )
                if reloaded != plan:
                    raise SpeakerHoldoutPrelabelError(
                        "active plan changed before aggregate commit"
                    )
                if (
                    file_sha256(verifier_path) != verifier_binding["sha256"]
                    or file_sha256(current_core) != core_binding["sha256"]
                ):
                    raise SpeakerHoldoutPrelabelError(
                        "aggregate verifier or core changed before commit"
                    )
                _publish_aggregate_receipt(run_fd, _json_bytes(first))
                return first
            finally:
                os.close(lock_fd)
        finally:
            os.close(run_fd)
    finally:
        os.close(root_fd)
