"""Explicit delivery-SRT translation into one supplied physical media clock.

No speaker inference. v1 receipts remain direct-clock only; v2 records the
source interval and the actual padded cue extraction windows without changing
SRT text/times or relabelling an already-computed score.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
from pathlib import Path
import subprocess
import wave

from src.autoslice.jingting_chunker import parse_srt_cues

SCHEMA = "speaker-attribution-source.v2"
RATE = 16000


def geometry(srt_text: str, offset_ms: int, end_ms: int, audio_duration_ms: int) -> dict:
    if (
        any(type(x) is not int for x in (offset_ms, end_ms, audio_duration_ms))
        or not 0 <= offset_ms < end_ms <= audio_duration_ms
    ):
        raise ValueError("SPEAKER_CLOCK_UNSUPPORTED")
    cues = [c for c in parse_srt_cues(srt_text) if c.text.strip()]
    if not cues:
        raise ValueError("SPEAKER_EMPTY_SRT")
    windows = []
    for i, cue in enumerate(cues):
        a, b = cue.start_ms + offset_ms, cue.end_ms + offset_ms
        if not offset_ms <= a < b <= end_ms:
            raise ValueError("SPEAKER_CUE_OUTSIDE_SOURCE_INTERVAL")
        # Existing speaker extractor's 150ms cue margin, constrained by the
        # explicit source interval. Overlaps remain observations, not purity.
        lower = max(a - 150, cues[i - 1].end_ms + offset_ms if i else offset_ms)
        upper = min(b + 150, cues[i + 1].start_ms + offset_ms if i + 1 < len(cues) else end_ms)
        if not lower <= a < b <= upper:
            # Overlapping/duplicate cue intervals must retain their target
            # samples, not become disjoint slivers outside the utterance.
            lower, upper = a, b
        windows.append(
            {
                "cue_index": i + 1,
                "target_window_ms": [cue.start_ms, cue.end_ms],
                "source_window_ms": [a, b],
                "extracted_source_window_ms": [lower, upper],
            }
        )
    return {
        "kind": "srt_plus_offset_in_supplied_media",
        "source_interval_ms": [offset_ms, end_ms],
        "target_interval_ms": [0, end_ms - offset_ms],
        "audio_duration_ms": audio_duration_ms,
        "cue_windows": windows,
    }


def valid_geometry(binding: dict, srt_text: str) -> bool:
    try:
        observed = binding["geometry"]
        offset, end = observed["source_interval_ms"]
        return (
            binding["timeline_offset_ms"] == offset
            and type(binding["timeline_offset_ms"]) is int
            and observed == geometry(srt_text, offset, end, observed["audio_duration_ms"])
        )
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def extract_cue_wavs(
    media_path: Path, srt_text: str, work_dir: Path, offset_ms: int, end_ms: int
) -> tuple[list[Path], dict, list[dict]]:
    """Decode silently once; slice PCM samples on the declared physical clock.

    WAV I/O needs only the standard library. Actual CAM++ remains in the
    existing speaker venv, with no dependency installation or ML fallback.
    """
    wav_path = work_dir / "clip-16k-mono.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(RATE),
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=550,
    )
    with wave.open(str(wav_path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, RATE):
            raise ValueError("SPEAKER_DECODE_FORMAT_INVALID")
        pcm = wav.readframes(wav.getnframes())
    mapping = geometry(srt_text, offset_ms, end_ms, len(pcm) // 2 * 1000 // RATE)
    cue_dir = work_dir / "cue-wavs"
    cue_dir.mkdir(exist_ok=False)
    paths, audio = [], []
    for window in mapping["cue_windows"]:
        a, b = window["extracted_source_window_ms"]
        chunk = pcm[a * RATE // 1000 * 2 : b * RATE // 1000 * 2]
        path = cue_dir / f"cue-{window['cue_index']:04d}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(chunk)
        paths.append(path)
        audio.append(
            {
                "cue_index": window["cue_index"],
                "relative_path": str(path.relative_to(work_dir)),
                "sha256": _evidence_file_sha256(path),
                "frames": len(chunk) // 2,
                "sample_rate": RATE,
            }
        )
    return paths, mapping, audio


def independent_host_supports(binding: dict, cue_ids: list[int]) -> int:
    """Overlapping extraction windows must not count as independent anchors."""
    windows = [
        w["extracted_source_window_ms"]
        for w in binding["geometry"]["cue_windows"]
        if w["cue_index"] in cue_ids
    ]
    count, prior_end = 0, -1
    for a, b in sorted(windows, key=lambda w: w[1]):
        if a >= prior_end:
            count += 1
            prior_end = b
    return count
