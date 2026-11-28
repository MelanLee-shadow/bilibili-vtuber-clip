"""Step 3, sound events (manual_lane/sound_events.py): the loudness and the pitch of a clip's sound, frame by frame.

Usage: sound_levels.py <media>

The media's sound is decoded to one channel at RATE Hz (ffmpeg) and cut into frames of FRAME_S every HOP_S.  Per frame:
the loudness (RMS in dBFS, one decimal) and the pitch (Hz, by the frame's normalised autocorrelation between
F0_MIN and F0_MAX; 0 when its peak is under VOICED_MIN, i.e. no clear voice).  Printed as the last line of stdout,
{"hop_s", "frame_s", "rate", "db": [...], "f0": [...]}: frame i starts at i * hop_s on the media's clock.
Only numpy; no model, nothing leaves the host.
"""

from __future__ import annotations

import json
import subprocess
import sys

import numpy as np

RATE = 16_000
HOP_S = 0.05
FRAME_S = 0.04
F0_MIN, F0_MAX = 80.0, 1100.0      # a scream or a falsetto goes far above speech; below 80 Hz is hum, not a voice
VOICED_MIN = 0.45                  # the autocorrelation peak a frame needs to count as voiced
SILENT_DB = -90.0


def decode(media: str) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", media, "-vn", "-ac", "1",
                          "-ar", str(RATE), "-f", "f32le", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def frames(x: np.ndarray) -> tuple[list[float], list[float]]:
    hop, size = round(HOP_S * RATE), round(FRAME_S * RATE)
    lo, hi = int(RATE / F0_MAX), int(RATE / F0_MIN)
    db: list[float] = []
    f0: list[float] = []
    window = np.hanning(size)
    for start in range(0, max(len(x) - size, 0) + 1, hop):
        seg = x[start:start + size].astype(np.float64)
        rms = float(np.sqrt(np.mean(seg * seg)))
        db.append(round(20 * np.log10(rms), 1) if rms > 0 else SILENT_DB)
        seg = (seg - seg.mean()) * window
        spec = np.fft.rfft(seg, 2 * size)
        ac = np.fft.irfft(spec * np.conj(spec))[:size]
        if ac[0] <= 0 or hi >= size:
            f0.append(0.0)
            continue
        norm = ac[lo:hi] / ac[0]
        k = int(np.argmax(norm))
        f0.append(round(RATE / (lo + k), 1) if norm[k] >= VOICED_MIN else 0.0)
    return db, f0


def main(argv: list[str]) -> int:
    db, f0 = frames(decode(argv[1]))
    print(json.dumps({"hop_s": HOP_S, "frame_s": FRAME_S, "rate": RATE, "db": db, "f0": f0}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
