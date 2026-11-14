#!/usr/bin/env python3
"""audio_silences.py VIDEO NOISE_DB MIN_SILENCE_S

Prints one JSON line {"duration": <seconds>, "silences": [[start_s, end_s], ...]}: every stretch of at least MIN_SILENCE_S
of the first audio stream of VIDEO in which every sample of every channel is at or below NOISE_DB (ffmpeg silencedetect,
one pass).  A stretch still silent at the end of the audio ends at its duration.  Which stretch matters (under a subtitle)
is the lane's decision, not this tool's.  Any failure (no file, no audio stream, ffmpeg failing) ends non-zero with the
reason on stderr.  Runs on the media host; nothing is written.
"""
import json
import re
import subprocess
import sys


def main(argv):
    video, noise_db, min_silence = argv[1], float(argv[2]), float(argv[3])
    run = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", video, "-map", "0:a:0", "-vn",
                          "-af", f"silencedetect=noise={noise_db:g}dB:d={min_silence:g}", "-f", "null", "-"],
                         capture_output=True, text=True, check=False)
    if run.returncode != 0:
        raise SystemExit(f"ffmpeg rc={run.returncode}: {run.stderr[-400:]}")
    # the last "time=HH:MM:SS.xx" of the progress line is the audio's decoded length
    times = re.findall(r"time=(\d+):(\d+):(\d+\.\d+)", run.stderr)
    if not times:
        raise SystemExit(f"no decoded audio: {run.stderr[-400:]}")
    hours, minutes, seconds = times[-1]
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    silences, start = [], None
    for line in run.stderr.splitlines():
        found_start = re.search(r"silence_start: (-?\d+(?:\.\d+)?)", line)
        found_end = re.search(r"silence_end: (-?\d+(?:\.\d+)?)", line)
        if found_start:
            start = max(0.0, float(found_start.group(1)))
        elif found_end and start is not None:
            silences.append([start, float(found_end.group(1))])
            start = None
    if start is not None:
        silences.append([start, duration])
    print(json.dumps({"duration": duration, "silences": silences}))


if __name__ == "__main__":
    main(sys.argv)
