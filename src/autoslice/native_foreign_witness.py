"""Explicit MOSS/MAI exact-cue evidence for the existing foreign-script judge.

No candidate text crosses the ASR boundary. A native transcript is not an
independent language verdict or pinyin witness, and never authorizes mutation.
This capability is opt-in at the existing consumer, not a new production default.
"""

from __future__ import annotations

from .review_errors import ContractRejection

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
from contextlib import contextmanager, nullcontext
from concurrent.futures import Future
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import time

from src.autoslice.local_asr_target_evidence import exact_target_evidence, digest
from src.autoslice.audio_cache_io import publish_directory, staged_directory
from src.autoslice.native_audio_budget_receipt import (
    BudgetReceiptPersistenceError,
    exclusive_native_audio_budget,
    persist_native_audio_budget_receipt,
    resume_native_audio_budget,
)
from src.autoslice.subtitle_audio_evidence import CONTRACT, _cached_secondary, observe_secondary
from src.autoslice.supplement_audio_budget import ensure_budget, get_budget


# Share failures as well as successful acquisitions among current thread callers.
# The file lock below additionally serializes other processes' cache publishers.
_FLIGHTS_GUARD = threading.Lock()
_FLIGHTS: dict[tuple[int, str, str], Future] = {}


def _reset_flights_after_fork():
    global _FLIGHTS_GUARD, _FLIGHTS
    _FLIGHTS_GUARD, _FLIGHTS = threading.Lock(), {}


os.register_at_fork(after_in_child=_reset_flights_after_fork)


def _no_links(path: Path) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("native witness path contains a symlink")


def _identity(path: Path) -> tuple[int, ...]:
    _no_links(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("native witness source is not a regular file")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _sha_file(path: Path) -> str:
    h = _evidence_file_sha256(path)
    return h


def native_acquisition_identity(*, source_sha256: str, provider: str,
                                start_ms: int, end_ms: int) -> dict:
    """One physical acquisition, independent of target/candidate/request IDs."""
    from src.autoslice.local_asr_target_evidence import MODELS
    if (provider not in MODELS or type(start_ms) is not int or type(end_ms) is not int
            or not 0 <= start_ms < end_ms):
        raise ContractRejection("EXACT_TARGET_GEOMETRY_INVALID")
    return {"contract": CONTRACT, "source_media_sha256": source_sha256,
            "provider": provider, "model": MODELS[provider],
            "start_ms": start_ms, "end_ms": end_ms, "candidate_exposure": "none"}


def native_acquisition_path(root: Path, identity: dict) -> Path:
    # Preserve the existing immutable on-disk namespace for cold recovery.
    return (root / "native-exact" / identity["provider"] /
            f"{identity['source_media_sha256'][:20]}-{identity['start_ms']}-{identity['end_ms']}")


@contextmanager
def _singleflight(root: Path, identity: dict, *, wait_allowed: bool = True):
    """Wait for this acquisition only; never hold a budget transaction here.

    Separate opens contend across threads and processes. Keep the empty inode
    after release so a waiter cannot accidentally lock an unlinked predecessor.
    """
    folder = root / "native-acquisition-locks"
    _no_links(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / (digest(identity) + ".lock")
    _no_links(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        def validate():
            _no_links(path)
            info, current = os.fstat(fd), path.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or info.st_size != 0
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                raise ContractRejection("NATIVE_ACQUISITION_LOCK_UNSAFE")
        validate()
        waited = False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not wait_allowed:
                error = ValueError("NATIVE_ACQUISITION_OWNER_RECOVERY_REQUIRED")
                error.reason_code = "NATIVE_ACQUISITION_OWNER_RECOVERY_REQUIRED"
                raise error
            waited = True
            fcntl.flock(fd, fcntl.LOCK_EX)
        validate()
        yield waited
    finally:
        os.close(fd)


def _exact_result(metadata, audio, identity):
    result = exact_target_evidence(
        metadata, audio=audio, source_sha256=identity["source_media_sha256"],
        start_ms=identity["start_ms"], end_ms=identity["end_ms"],
        prefer_provider_text=identity["provider"] == "moss")
    rows = result["native_segments"]
    if (any(a["end_ms"] > b["start_ms"] for a, b in zip(rows, rows[1:]))
            and result.get("transcript_basis") != "provider_full_crop_text"):
        raise ContractRejection("EXACT_TARGET_NATIVE_ROWS_INVALID: overlap or out-of-order")
    result.update(served_from_cache=metadata.get("served_from_cache", False),
        observation_scope="complete_exact_cue_no_padding",
        language_observation_available=False, speaker_identity_observed=False)
    result.pop("receipt_sha256", None)
    result["receipt_sha256"] = digest(result)
    return result


def read_native_acquisition(*, source_media: Path, root: Path, identity: dict) -> dict:
    """Cold read only: no extraction, credentials, provider or budget mutation."""
    before = _identity(source_media)
    if _sha_file(source_media) != identity["source_media_sha256"]:
        raise ContractRejection("NATIVE_COMPLETED_CACHE_SOURCE_DRIFT")
    job = native_acquisition_path(root, identity)
    sound = job / "input.mp3"
    path = sound.with_suffix(f".audio-evidence.{identity['provider']}.json")
    _no_links(sound)
    _no_links(path)
    audio = sound.read_bytes()
    binding = {key: identity[key] for key in ("contract", "provider", "model", "candidate_exposure")}
    binding.update(input_audio_sha256=_sha_file(sound),
                   duration_ms=identity["end_ms"] - identity["start_ms"])
    metadata = _cached_secondary(path=path, binding=binding, exact_cue=True)
    if metadata is None:
        raise ContractRejection("NATIVE_COMPLETED_CACHE_DRIFT")
    result = _exact_result({**metadata, "served_from_cache": True}, audio, identity)
    if _identity(source_media) != before:
        raise ValueError("native witness source changed during cache read")
    return result


def _extract_exact_mp3(source: Path, output: Path, start_ms: int, end_ms: int) -> None:
    """Exact geometry, noninteractive FFmpeg and full decoded-duration check.

    The regular provider path adds acoustic padding. This explicit native path
    cannot ask a model to trim padding it never received markers for, so the
    submitted waveform is exactly the cue instead. No proportional text slicing.
    """
    _no_links(output)
    subprocess_env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "CPA_API_KEY",
            "AZURE_API_KEY",
            "AUTOSLICE_MOSS_API_KEY",
            "MOSS_API_KEY",
            "AUTOSLICE_MAI_API_KEY",
            "GEMINI_API_KEY",
            "GEMINI_API_KEY_2",
            "GEMINI_API_KEY_3",
        }
    }
    fd, name = tempfile.mkstemp(prefix=".native-exact-", suffix=".mp3", dir=output.parent)
    os.close(fd)
    temp = Path(name)
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-ss",
                f"{start_ms / 1000:.3f}",
                "-i",
                str(source),
                "-t",
                f"{(end_ms - start_ms) / 1000:.3f}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                "-y",
                str(temp),
            ],
            stdin=subprocess.DEVNULL,
            env=subprocess_env,
            capture_output=True,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError("WITNESS_AUDIO_EXTRACTION_FAILED")
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(temp),
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-",
            ],
            stdin=subprocess.DEVNULL,
            env=subprocess_env,
            capture_output=True,
            timeout=30,
        )
        if (
            decoded.returncode
            or not decoded.stdout
            or abs(len(decoded.stdout) / 32 - (end_ms - start_ms)) > 50
        ):
            raise RuntimeError("WITNESS_AUDIO_EXTRACTION_FAILED: decoded target duration mismatch")
        _no_links(output)
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)


def _persist_budget_receipt(
    *,
    source_media: Path,
    source_sha256: str,
    receipt_path: Path | None = None,
    output_dir: Path | None = None,
    provider: str | None = None,
) -> None:
    """Compatibility seam around the monotonic canonical receipt writer."""

    del provider
    if receipt_path is None:
        if output_dir is None:
            raise BudgetReceiptPersistenceError(
                "native audio budget receipt path is missing"
            )
        receipt_path = output_dir / "native-audio-budget.json"
    persist_native_audio_budget_receipt(
        source_media=source_media,
        source_media_sha256=source_sha256,
        receipt_path=receipt_path,
    )


def build_native_foreign_witness(
    *,
    source_media: Path,
    output_dir: Path,
    provider: str,
    max_windows: object = None,
    max_audio_ms: object = None,
    budget_receipt_path: Path | None = None,
    enforce_limits: object = None,
):
    """Return observe(start_ms=..., end_ms=...) with no lexical input channel.

    Uses the existing provider-native client, cache and source-scoped budget.
    Requests outside the bounded contract fail; there is no hidden fallback.
    """
    if provider not in {"mai", "moss"}:
        raise ValueError("native provider must explicitly be mai or moss")
    source_media = source_media.absolute()
    output_dir = output_dir.absolute()
    budget_receipt_path = (
        output_dir / "native-audio-budget.json"
        if budget_receipt_path is None
        else Path(budget_receipt_path).absolute()
    )
    before = _identity(source_media)
    source_sha = _sha_file(source_media)
    if _identity(source_media) != before:
        raise ValueError("native witness source changed while hashing")
    _no_links(output_dir)
    _no_links(budget_receipt_path)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    budget = ensure_budget(
        source_media,
        max_windows=max_windows,
        max_audio_ms=max_audio_ms,
        enforce_limits=enforce_limits,
    )
    with budget.lock, exclusive_native_audio_budget(budget_receipt_path):
        resume_native_audio_budget(
            source_media=source_media,
            source_media_sha256=source_sha,
            receipt_path=budget_receipt_path,
        )
        # Establish an initial receipt before returning any callable observer.
        _persist_budget_receipt(
            source_media=source_media,
            source_sha256=source_sha,
            receipt_path=budget_receipt_path,
        )

    def persist_dispatch_reservation() -> None:
        # Short before-HTTP transaction only; no lock survives into transport.
        if get_budget(source_media) is not budget:
            raise BudgetReceiptPersistenceError(
                "native audio budget object changed before dispatch persistence"
            )
        _persist_budget_receipt(
            source_media=source_media, source_sha256=source_sha,
            receipt_path=budget_receipt_path,
        )

    @contextmanager
    def budget_transaction():
        # Read/modify/write of monotonic counters cannot be replaced by an
        # unconditional file rename: two processes would lose reservations.
        with budget.lock, exclusive_native_audio_budget(budget_receipt_path):
            if get_budget(source_media) is not budget:
                raise BudgetReceiptPersistenceError(
                    "native audio budget object changed before observation"
                )
            resume_native_audio_budget(
                source_media=source_media,
                source_media_sha256=source_sha,
                receipt_path=budget_receipt_path,
                require_existing=True,
            )
            try:
                yield
            finally:
                _persist_budget_receipt(
                    source_media=source_media, source_sha256=source_sha,
                    receipt_path=budget_receipt_path,
                )

    def acquisition_attempt_count(start_ms, end_ms):
        identity = native_acquisition_identity(source_sha256=source_sha, provider=provider,
            start_ms=start_ms, end_ms=end_ms)
        return sum(row["provider"] == provider and row["model"] == identity["model"]
            and (row["start_ms"], row["end_ms"]) == (start_ms, end_ms)
            for row in budget.snapshot()["attempts"])

    def acquire(*, start_ms: int, end_ms: int, last_native_admission: bool = False) -> dict:
        acquisition = native_acquisition_identity(source_sha256=source_sha, provider=provider,
            start_ms=start_ms, end_ms=end_ms)
        started = time.monotonic()
        try:
            with budget_transaction():
                initial_attempts = acquisition_attempt_count(start_ms, end_ms)
            if _identity(source_media) != before:
                raise ValueError("native witness source changed")
            job = native_acquisition_path(output_dir, acquisition)
            _no_links(job)
            workspace = nullcontext(job) if job.exists() else staged_directory(job)
            with workspace as work:
                return observe_in_workspace(work, job, start_ms, end_ms, started,
                                            last_native_admission, initial_attempts=initial_attempts)
        finally:
            if budget.enforce_limits and last_native_admission:
                from src.autoslice.exact_review_parallel import finish_native_admission
                finish_native_admission()
            with budget_transaction():
                pass

    def observe_in_workspace(work, job, start_ms, end_ms, started, last_native_admission, acquisition_locked=False, initial_attempts=0):
        for item in work.iterdir():
            _no_links(item)
        sound = work / "input.mp3"
        receipt = sound.with_suffix(f".audio-evidence.{provider}.json")
        if receipt.exists() or receipt.is_symlink():
            from src.autoslice.mai_transcription import MAI_MODEL
            from src.autoslice.moss_transcription import MOSS_MODEL
            binding = {"contract": CONTRACT, "provider": provider,
                       "model": {"mai": MAI_MODEL, "moss": MOSS_MODEL}[provider],
                       "input_audio_sha256": _sha_file(sound),
                       "duration_ms": end_ms - start_ms, "candidate_exposure": "none"}
            if _cached_secondary(path=receipt, binding=binding, exact_cue=True) is None:
                raise ContractRejection("NATIVE_COMPLETED_CACHE_DRIFT")
        else:
            _extract_exact_mp3(source_media, sound, start_ms, end_ms)
        if not acquisition_locked:
            identity = native_acquisition_identity(source_sha256=source_sha, provider=provider,
                start_ms=start_ms, end_ms=end_ms)
            # Extraction may proceed in another process. Only dispatch through
            # atomic publication is serialized, outside every budget transaction.
            with _singleflight(output_dir, identity,
                    wait_allowed=not budget._lock._is_owned()) as waited:
                if work != job and job.exists():
                    return observe_in_workspace(job, job, start_ms, end_ms, started,
                                                last_native_admission, acquisition_locked=True)
                if work == job:
                    return observe_ready(work, job, start_ms, end_ms, started, last_native_admission)
                with budget_transaction():
                    current_attempts = acquisition_attempt_count(start_ms, end_ms)
                if current_attempts > initial_attempts or (waited and current_attempts):
                    # A concurrent owner already spent this acquisition and
                    # did not publish a usable result. Stay with that owner.
                    error = ValueError("NATIVE_ACQUISITION_OWNER_RECOVERY_REQUIRED")
                    error.reason_code = "NATIVE_ACQUISITION_OWNER_RECOVERY_REQUIRED"
                    raise error
                return observe_ready(work, job, start_ms, end_ms, started, last_native_admission)
        return observe_ready(work, job, start_ms, end_ms, started, last_native_admission)

    def observe_ready(work, job, start_ms, end_ms, started, last_native_admission):
        sound = work / "input.mp3"
        if _identity(source_media) != before:
            raise ValueError("native witness source changed during extraction")
        audio = sound.read_bytes()
        metadata = observe_secondary(
            audio,
            media_path=sound,
            provider=provider,
            duration_ms=end_ms - start_ms,
            supplement_source=source_media,
            crop_start_ms=start_ms,
            crop_end_ms=end_ms,
            exact_cue=True,
            persist_before_dispatch=persist_dispatch_reservation,
            budget_transaction=budget_transaction,
            publish_evidence=(lambda: publish_directory(work, job)) if work != job else None,
            last_native_admission=last_native_admission,
        )
        if _identity(source_media) != before:
            raise ValueError("native witness source changed during observation")
        result = _exact_result(metadata, audio, native_acquisition_identity(
            source_sha256=source_sha, provider=provider, start_ms=start_ms, end_ms=end_ms))
        result["elapsed_seconds"] = time.monotonic() - started
        result.pop("receipt_sha256", None)
        result["receipt_sha256"] = digest(result)
        if work != job and work.exists():
            publish_directory(work, job)
        return result

    def observe(*, start_ms: int, end_ms: int, last_native_admission: bool = False) -> dict:
        identity = native_acquisition_identity(source_sha256=source_sha, provider=provider,
            start_ms=start_ms, end_ms=end_ms)
        if budget.enforce_limits:
            from src.autoslice.exact_review_parallel import wait_for_native_turn
            wait_for_native_turn()
        key = (os.getpid(), str(output_dir), digest(identity))
        with _FLIGHTS_GUARD:
            leader = key not in _FLIGHTS
            flight = _FLIGHTS.setdefault(key, Future())
        if not leader:
            try:
                if budget._lock._is_owned():
                    # Legacy pre-dispatch recovery may enter with its source
                    # transaction open. Never wait on another owner's HTTP
                    # while retaining that transaction.
                    error = ValueError("NATIVE_ACQUISITION_OWNER_RECOVERY_REQUIRED")
                    error.reason_code = "NATIVE_ACQUISITION_OWNER_RECOVERY_REQUIRED"
                    raise error
                result = deepcopy(flight.result())
                result["served_from_cache"] = True
                result.pop("receipt_sha256", None)
                result["receipt_sha256"] = digest(result)
                if _identity(source_media) != before:
                    raise ValueError("native witness source changed while waiting")
                return result
            finally:
                if budget.enforce_limits and last_native_admission:
                    from src.autoslice.exact_review_parallel import finish_native_admission
                    finish_native_admission()
        try:
            result = acquire(start_ms=start_ms, end_ms=end_ms,
                last_native_admission=last_native_admission)
        except BaseException as exc:
            flight.set_exception(exc)
            raise
        else:
            flight.set_result(result)
            return result
        finally:
            with _FLIGHTS_GUARD:
                del _FLIGHTS[key]

    return observe
