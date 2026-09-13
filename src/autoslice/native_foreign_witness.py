"""Explicit MOSS/MAI exact-cue evidence for the existing foreign-script judge.

No candidate text crosses the ASR boundary. A native transcript is not an
independent language verdict or pinyin witness, and never authorizes mutation.
This capability is opt-in at the existing consumer, not a new production default.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

from src.autoslice.local_asr_target_evidence import exact_target_evidence, digest
from src.autoslice.native_audio_budget_receipt import (
    BudgetReceiptPersistenceError,
    exclusive_native_audio_budget,
    persist_native_audio_budget_receipt,
    require_native_audio_budget_continuity,
)
from src.autoslice.subtitle_audio_evidence import observe_secondary
from src.autoslice.supplement_audio_budget import ensure_budget, get_budget


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
    h = hashlib.sha256()
    with path.open("rb") as f:
        for data in iter(lambda: f.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


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
    # Inspect occupied output before ensure_budget can create a fresh ledger.
    require_native_audio_budget_continuity(
        source_media=source_media,
        source_media_sha256=source_sha,
        receipt_path=budget_receipt_path,
    )
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    budget = ensure_budget(
        source_media,
        max_windows=max_windows,
        max_audio_ms=max_audio_ms,
    )
    with budget.lock, exclusive_native_audio_budget(budget_receipt_path):
        require_native_audio_budget_continuity(
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

    def observe(*, start_ms: int, end_ms: int) -> dict:
        # A stale callable must not spend from a replacement process ledger.
        with budget.lock, exclusive_native_audio_budget(budget_receipt_path):
            if get_budget(source_media) is not budget:
                raise BudgetReceiptPersistenceError(
                    "native audio budget object changed before observation"
                )
            # Deliberately outside try/finally: rejection must not rewrite evidence.
            require_native_audio_budget_continuity(
                source_media=source_media,
                source_media_sha256=source_sha,
                receipt_path=budget_receipt_path,
                require_existing=True,
            )
            started = time.monotonic()
            try:
                if (
                    type(start_ms) is not int
                    or type(end_ms) is not int
                    or not 0 <= start_ms < end_ms
                    or end_ms - start_ms > 20_000
                ):
                    raise ValueError("EXACT_TARGET_GEOMETRY_INVALID")
                if _identity(source_media) != before:
                    raise ValueError("native witness source changed")
                job = output_dir / "native-exact" / provider / f"{source_sha[:20]}-{start_ms}-{end_ms}"
                _no_links(job)
                job.mkdir(parents=True, exist_ok=True, mode=0o700)
                for item in job.iterdir():
                    _no_links(item)
                sound = job / "input.mp3"
                _extract_exact_mp3(source_media, sound, start_ms, end_ms)
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
                )
                if _identity(source_media) != before:
                    raise ValueError("native witness source changed during observation")
                result = exact_target_evidence(
                    metadata, audio=audio, source_sha256=source_sha, start_ms=start_ms, end_ms=end_ms,
                    prefer_provider_text=provider == "moss",
                )
                # Never sort or flatten overlapping speakers into a claimed exact string.
                rows = result["native_segments"]
                if (any(a["end_ms"] > b["start_ms"] for a, b in zip(rows, rows[1:]))
                        and result.get("transcript_basis") != "provider_full_crop_text"):
                    raise ValueError("EXACT_TARGET_NATIVE_ROWS_INVALID: overlap or out-of-order")
                result.update(
                    served_from_cache=metadata.get("served_from_cache", False),
                    observation_scope="complete_exact_cue_no_padding",
                    language_observation_available=False,
                    speaker_identity_observed=False,
                    elapsed_seconds=time.monotonic() - started,
                )
                result.pop("receipt_sha256", None)
                result["receipt_sha256"] = digest(result)
                return result
            finally:
                _persist_budget_receipt(
                    source_media=source_media,
                    source_sha256=source_sha,
                    receipt_path=budget_receipt_path,
                )

    return observe
