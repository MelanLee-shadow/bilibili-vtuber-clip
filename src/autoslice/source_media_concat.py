"""The ffmpeg command that joins cut source pieces with video kept and audio decoded piece by piece.

Split out of producer_source_media so the manual lane builds the same command without loading the producer;
producer_source_media re-exports it."""
from __future__ import annotations

from pathlib import Path


def _concat_piece_command(
    piece_paths: list[Path], durations: list[int], concat_list: Path, output: Path,
) -> list[str]:
    # The concat demuxer loses each AAC packet's priming skip metadata. Decode
    # audio independently before joining it; keep video PTS rather than shifting
    # them by the demuxer's negative AAC start time.
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-copyts",
               "-f", "concat", "-safe", "0", "-i", str(concat_list)]
    filters = []
    for index, (path, duration) in enumerate(zip(piece_paths, durations, strict=True)):
        command.extend(["-i", str(path)])
        seconds = f"{duration / 1000:.3f}"
        filters.append(
            f"[{index + 1}:a:0]apad=whole_dur={seconds},atrim=duration={seconds},"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
    inputs = "".join(f"[a{index}]" for index in range(len(piece_paths)))
    filters.append(f"{inputs}concat=n={len(piece_paths)}:v=0:a=1[a]")
    return command + ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[a]",
                      "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(output)]
