"""Portable media generators for the dual-modal capability sentinel.

The generators deliberately encode random decimal tokens only inside media:

* audio uses ordinary DTMF keypad tones in a mono PCM WAV;
* video shows four consecutive two-digit panels in a real H.264 MP4.

No plaintext answer file is produced.  The caller commits only the answer hash
before dispatch.  ffmpeg/ffprobe are explicit, hash-bound runtime inputs of the
sentinel runner; this module never discovers or downloads binaries.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import stat
import struct
import subprocess
import wave


AUDIO_SAMPLE_RATE = 16_000
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_WIDTH = 2
AUDIO_TOKEN_DIGITS = 8
VIDEO_TOKEN_DIGITS = 8
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 360
VIDEO_PANEL_COUNT = 4
VIDEO_PANEL_SECONDS = 0.4
VIDEO_INPUT_FPS = 1.0 / VIDEO_PANEL_SECONDS
VIDEO_OUTPUT_FPS = 10

_DTMF = {
    "1": (697.0, 1209.0),
    "2": (697.0, 1336.0),
    "3": (697.0, 1477.0),
    "4": (770.0, 1209.0),
    "5": (770.0, 1336.0),
    "6": (770.0, 1477.0),
    "7": (852.0, 1209.0),
    "8": (852.0, 1336.0),
    "9": (852.0, 1477.0),
    "0": (941.0, 1336.0),
}
_DIGIT_FONT = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
}


class FinalMediaReviewSentinelMediaError(ValueError):
    """A deterministic media-generation or media-validation failure."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_binding(path: Path) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if (
        path.is_symlink()
        or not resolved.is_file()
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
    ):
        raise FinalMediaReviewSentinelMediaError(
            f"sentinel media is not a stable regular file: {path}"
        )
    return {
        "path": str(resolved),
        "sha256": _sha256(resolved),
        "bytes": info.st_size,
    }


def _validate_decimal_token(token: str, *, digits: int, label: str) -> None:
    if len(token) != digits or any(character not in "0123456789" for character in token):
        raise FinalMediaReviewSentinelMediaError(
            f"{label} token must contain exactly {digits} decimal digits"
        )


def generate_dtmf_wav(path: Path, token: str) -> dict[str, object]:
    """Create one real PCM WAV whose only information is an eight-digit token."""

    _validate_decimal_token(token, digits=AUDIO_TOKEN_DIGITS, label="audio")
    if path.exists() or path.is_symlink():
        raise FinalMediaReviewSentinelMediaError(
            f"audio challenge output already exists: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lead_seconds = 0.20
    tone_seconds = 0.38
    gap_seconds = 0.16
    trail_seconds = 0.20
    amplitude = 0.28
    samples: list[int] = []

    def silence(seconds: float) -> None:
        samples.extend([0] * round(AUDIO_SAMPLE_RATE * seconds))

    silence(lead_seconds)
    for index, digit in enumerate(token):
        low, high = _DTMF[digit]
        frame_count = round(AUDIO_SAMPLE_RATE * tone_seconds)
        ramp = round(AUDIO_SAMPLE_RATE * 0.015)
        for frame in range(frame_count):
            envelope = 1.0
            if frame < ramp:
                envelope = frame / max(1, ramp)
            elif frame >= frame_count - ramp:
                envelope = (frame_count - frame - 1) / max(1, ramp)
            value = (
                math.sin(2.0 * math.pi * low * frame / AUDIO_SAMPLE_RATE)
                + math.sin(2.0 * math.pi * high * frame / AUDIO_SAMPLE_RATE)
            ) / 2.0
            samples.append(round(32767 * amplitude * envelope * value))
        if index != len(token) - 1:
            silence(gap_seconds)
    silence(trail_seconds)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(AUDIO_CHANNELS)
        handle.setsampwidth(AUDIO_SAMPLE_WIDTH)
        handle.setframerate(AUDIO_SAMPLE_RATE)
        handle.writeframes(b"".join(struct.pack("<h", value) for value in samples))
    path.chmod(0o600)
    return validate_dtmf_wav(path)


def validate_dtmf_wav(path: Path) -> dict[str, object]:
    binding = _file_binding(path)
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            sample_frames = handle.getnframes()
            compression = handle.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise FinalMediaReviewSentinelMediaError(
            "audio challenge is not a readable PCM WAV"
        ) from exc
    if (
        channels != AUDIO_CHANNELS
        or sample_width != AUDIO_SAMPLE_WIDTH
        or sample_rate != AUDIO_SAMPLE_RATE
        or sample_frames <= 0
        or compression != "NONE"
    ):
        raise FinalMediaReviewSentinelMediaError(
            "audio challenge WAV geometry differs from the sentinel contract"
        )
    duration_us = round(sample_frames * 1_000_000 / sample_rate)
    if not 3_500_000 <= duration_us <= 6_000_000:
        raise FinalMediaReviewSentinelMediaError(
            "audio challenge duration differs from the sentinel contract"
        )
    return {
        **binding,
        "format": "wav_pcm_s16le",
        "channels": channels,
        "sample_width_bytes": sample_width,
        "sample_rate": sample_rate,
        "sample_frames": sample_frames,
        "duration_us": duration_us,
        "encoding": "DTMF_8_DIGIT_SEQUENCE",
    }


def _paint_rectangle(
    pixels: bytearray,
    *,
    x0: int,
    y0: int,
    width: int,
    height: int,
    rgb: tuple[int, int, int],
) -> None:
    x1 = min(VIDEO_WIDTH, x0 + width)
    y1 = min(VIDEO_HEIGHT, y0 + height)
    for y in range(max(0, y0), y1):
        start = (y * VIDEO_WIDTH + max(0, x0)) * 3
        for x in range(max(0, x0), x1):
            offset = start + (x - max(0, x0)) * 3
            pixels[offset : offset + 3] = bytes(rgb)


def _render_digit_frame(path: Path, pair: str, *, panel_index: int) -> None:
    _validate_decimal_token(pair, digits=2, label="video panel")
    pixels = bytearray(b"\x08\x0c\x18" * (VIDEO_WIDTH * VIDEO_HEIGHT))
    accent = (
        40 + (panel_index * 47) % 160,
        90 + (panel_index * 31) % 130,
        150 + (panel_index * 17) % 90,
    )
    _paint_rectangle(
        pixels,
        x0=20,
        y0=20,
        width=VIDEO_WIDTH - 40,
        height=VIDEO_HEIGHT - 40,
        rgb=accent,
    )
    _paint_rectangle(
        pixels,
        x0=30,
        y0=30,
        width=VIDEO_WIDTH - 60,
        height=VIDEO_HEIGHT - 60,
        rgb=(8, 12, 24),
    )
    scale = 34
    gap = 34
    digit_width = 5 * scale
    total_width = digit_width * 2 + gap
    start_x = (VIDEO_WIDTH - total_width) // 2
    start_y = (VIDEO_HEIGHT - 7 * scale) // 2
    for digit_index, digit in enumerate(pair):
        pattern = _DIGIT_FONT[digit]
        base_x = start_x + digit_index * (digit_width + gap)
        for row, row_bits in enumerate(pattern):
            for column, bit in enumerate(row_bits):
                if bit == "1":
                    _paint_rectangle(
                        pixels,
                        x0=base_x + column * scale,
                        y0=start_y + row * scale,
                        width=scale - 3,
                        height=scale - 3,
                        rgb=(248, 250, 255),
                    )
    header = f"P6\n{VIDEO_WIDTH} {VIDEO_HEIGHT}\n255\n".encode("ascii")
    path.write_bytes(header + bytes(pixels))
    path.chmod(0o600)


def _run_checked(
    argv: list[str], *, timeout_seconds: int, label: str
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FinalMediaReviewSentinelMediaError(
            f"{label} could not run deterministically"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:1000]
        raise FinalMediaReviewSentinelMediaError(
            f"{label} failed with rc={completed.returncode}: {detail}"
        )
    return completed


def generate_temporal_video(
    path: Path,
    token: str,
    *,
    ffmpeg_path: Path,
    ffprobe_path: Path,
) -> dict[str, object]:
    """Create a four-panel temporal MP4 and verify it through ffprobe."""

    _validate_decimal_token(token, digits=VIDEO_TOKEN_DIGITS, label="video")
    if path.exists() or path.is_symlink():
        raise FinalMediaReviewSentinelMediaError(
            f"video challenge output already exists: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    frames = path.parent / "frames"
    if frames.exists() or frames.is_symlink():
        raise FinalMediaReviewSentinelMediaError(
            "video challenge frame directory already exists"
        )
    frames.mkdir(mode=0o700)
    for index in range(VIDEO_PANEL_COUNT):
        pair = token[index * 2 : index * 2 + 2]
        _render_digit_frame(frames / f"frame-{index:03d}.ppm", pair, panel_index=index)
    _run_checked(
        [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-framerate",
            f"{VIDEO_INPUT_FPS:g}",
            "-i",
            str(frames / "frame-%03d.ppm"),
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(VIDEO_OUTPUT_FPS),
            "-movflags",
            "+faststart",
            str(path),
        ],
        timeout_seconds=60,
        label="sentinel ffmpeg",
    )
    path.chmod(0o600)
    receipt = validate_temporal_video(path, ffprobe_path=ffprobe_path)
    receipt["panel_count"] = VIDEO_PANEL_COUNT
    receipt["panel_seconds"] = VIDEO_PANEL_SECONDS
    receipt["encoding"] = "FOUR_FAST_TEMPORAL_TWO_DIGIT_PANELS"
    receipt["frame_sources"] = [
        {
            "path": str(frame.resolve()),
            "sha256": _sha256(frame),
            "bytes": frame.stat().st_size,
        }
        for frame in sorted(frames.glob("frame-*.ppm"))
    ]
    return receipt


def validate_temporal_video(
    path: Path, *, ffprobe_path: Path
) -> dict[str, object]:
    binding = _file_binding(path)
    completed = _run_checked(
        [
            str(ffprobe_path),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,nb_frames,r_frame_rate,duration",
            "-show_entries",
            "format=format_name,duration",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=30,
        label="sentinel ffprobe",
    )
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams")
        stream = streams[0] if isinstance(streams, list) and len(streams) == 1 else None
        format_row = payload.get("format")
        duration = float((format_row or {}).get("duration"))
        width = int((stream or {}).get("width"))
        height = int((stream or {}).get("height"))
        codec = str((stream or {}).get("codec_name"))
        frame_count_raw = (stream or {}).get("nb_frames")
        frame_count = int(frame_count_raw) if str(frame_count_raw).isdigit() else None
    except (TypeError, ValueError, IndexError, json.JSONDecodeError) as exc:
        raise FinalMediaReviewSentinelMediaError(
            "video challenge ffprobe output is malformed"
        ) from exc
    if (
        codec != "h264"
        or width != VIDEO_WIDTH
        or height != VIDEO_HEIGHT
        or not 1.4 <= duration <= 1.8
        or (frame_count is not None and frame_count < 14)
    ):
        raise FinalMediaReviewSentinelMediaError(
            "video challenge geometry differs from the sentinel contract"
        )
    return {
        **binding,
        "format": "mp4_h264_yuv420p",
        "codec_name": codec,
        "width": width,
        "height": height,
        "duration_us": round(duration * 1_000_000),
        "video_frame_count": frame_count,
    }


__all__ = [
    "AUDIO_TOKEN_DIGITS",
    "FinalMediaReviewSentinelMediaError",
    "VIDEO_INPUT_FPS",
    "VIDEO_OUTPUT_FPS",
    "VIDEO_PANEL_SECONDS",
    "VIDEO_TOKEN_DIGITS",
    "generate_dtmf_wav",
    "generate_temporal_video",
    "validate_dtmf_wav",
    "validate_temporal_video",
]
