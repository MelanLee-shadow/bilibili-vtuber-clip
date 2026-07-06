#!/usr/bin/env python3
"""Silero VAD (onnxruntime) -> speech spans JSON.  Runs on the `free` host.

Deploy: scp scripts/free_silero_vad_spans.py free:/opt/bilive/vad/silero_vad_spans.py
Model:  /opt/bilive/vad/silero_vad.onnx (silero-vad v5 from snakers4/silero-vad)
Usage:  python3 silero_vad_spans.py input.wav   # 16kHz mono s16le WAV

Silero v5 contract: each 512-sample chunk must be fed as 576 samples — the
last 64 samples of the previous chunk prepended as context — plus the
recurrent state carried across calls.  Without the context samples the model
outputs ~0 for everything (silently broken).

Output: {"frame_ms": 32, "spans": [{"start_ms": int, "end_ms": int}, ...]}

Calibration note (Li Dousha live audio, 2026-07-03): precision is high but
recall is LOW under loud BGM — masked speech, screams, and laughter score ~0.
Consumers must treat spans as positive evidence only; absence of a span is
never sufficient to delete a subtitle cue.
"""

import json
import sys
import wave

import numpy as np
import onnxruntime

MODEL = "/opt/bilive/vad/silero_vad.onnx"
SR = 16000
CHUNK = 512  # 32ms
CONTEXT = 64
THRESHOLD = 0.5
RELEASE = 0.35
MIN_SPEECH_MS = 200
MIN_SILENCE_MS = 400
PAD_MS = 120


def main() -> int:
    wav_path = sys.argv[1]
    with wave.open(wav_path, "rb") as handle:
        assert handle.getframerate() == SR and handle.getnchannels() == 1, "need 16k mono wav"
        pcm = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768.0

    session = onnxruntime.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros(CONTEXT, dtype=np.float32)
    sr = np.array(SR, dtype=np.int64)
    probs = []
    for offset in range(0, len(audio) - CHUNK + 1, CHUNK):
        chunk = audio[offset : offset + CHUNK]
        model_input = np.concatenate([context, chunk])[None, :]
        out, state = session.run(None, {"input": model_input, "state": state, "sr": sr})
        probs.append(float(out[0][0]))
        context = chunk[-CONTEXT:]

    frame_ms = CHUNK * 1000 // SR
    spans = []
    speaking = False
    start_frame = 0
    silence_run = 0
    for index, prob in enumerate(probs):
        if not speaking:
            if prob >= THRESHOLD:
                speaking = True
                start_frame = index
                silence_run = 0
        else:
            if prob < RELEASE:
                silence_run += 1
                if silence_run * frame_ms >= MIN_SILENCE_MS:
                    spans.append((start_frame, index - silence_run + 1))
                    speaking = False
                    silence_run = 0
            else:
                silence_run = 0
    if speaking:
        spans.append((start_frame, len(probs)))

    merged = []
    for start_frame, end_frame in spans:
        start_ms = max(0, start_frame * frame_ms - PAD_MS)
        end_ms = end_frame * frame_ms + PAD_MS
        if end_ms - start_ms < MIN_SPEECH_MS:
            continue
        if merged and start_ms <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end_ms)
        else:
            merged.append([start_ms, end_ms])
    print(json.dumps({"frame_ms": frame_ms, "spans": [{"start_ms": s, "end_ms": e} for s, e in merged]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
