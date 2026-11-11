"""Shared black-frame encoder; callers retain their own range policy."""
from pathlib import Path
import subprocess
from typing import Protocol


class MediaValidator(Protocol):
    def __call__(self, path: Path, /, *, expected_duration_ms: int) -> tuple[bool, str]: ...


def render_black_frame_audio(
    *, source_media: Path, audio_path: Path, start_ms: int, end_ms: int,
    validate_media: MediaValidator,
) -> tuple[bool, str]:
    """Encode a caller-validated range; narrow and full-block policies are separate."""
    duration_s = (end_ms - start_ms) / 1000.0
    if start_ms < 0 or not 0 < duration_s or audio_path.is_symlink():
        return False, "audio witness crop interval or output path is invalid"
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_ms / 1000.0:.3f}",
            "-t",
            f"{duration_s:.3f}",
            "-i",
            str(source_media),
            "-f",
            "lavfi",
            "-t",
            f"{duration_s:.3f}",
            "-i",
            "color=c=black:s=320x240:r=10",
            "-map",
            "1:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            str(audio_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )
    if completed.returncode != 0 or not audio_path.is_file():
        return False, completed.stderr
    return validate_media(
        audio_path, expected_duration_ms=end_ms - start_ms,
    )
