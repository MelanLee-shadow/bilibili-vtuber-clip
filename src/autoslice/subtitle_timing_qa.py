"""Subtitle timing QA for talk recuts — ASR-orchestra lessons, adapted.

Source ASR cue timing is coarse (integer-second boundaries) and
hallucination-prone over BGM: the 7/2 lidousha clip shipped a 24s trailing
"好漂亮" (a whisper stuck-segment), 1s flash cues, and cues hanging over
silence.  The orchestra rules (scan_srt_timing_quality/detect_suspect_segments)
gate cues by duration + text density and repair against real speech evidence.

Ground-truthing on the actual clip set one hard constraint: silero VAD here is
high-precision but LOW-RECALL — loud BGM masks speech, and screams/laughter
(real slice content!) score ~0.  So VAD spans are POSITIVE evidence only:

- a cue is NEVER dropped merely because VAD saw no speech;
- the one deletable pattern is the whisper stuck-segment: text duplicating a
  recent cue + duration at/over the hard max + no VAD support;
- only cues already proven suspect by independent duration + text-density
  evidence may be retimed toward observed speech islands or duration-clamped;
- ordinary ASR cues are never shortened from VAD non-detection at either edge.

Text is never invented, cue order is preserved, and every action is recorded
for the evidence trail.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

from src.autoslice.review_evidence import SourceCue

SpeechSpansProvider = Callable[[Path, int, int], "list[SpeechSpan]"]

SUBTITLE_TIMING_QA_SCHEMA_VERSION = "subtitle-timing-qa.v3"

_PUNCT_RX = re.compile(r"[\s？?！!。，,．.…~～　]+")


@dataclass(frozen=True)
class SpeechSpan:
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class TimingQaPolicy:
    boundary_fragment_max_visible_ms: int = 300
    min_readable_ms: int = 1_000
    soft_max_ms: int = 6_000
    hard_max_ms: int = 12_000
    min_density_cps: float = 0.8
    stuck_dup_lookback_ms: int = 60_000
    drop_max_vad_ratio: float = 0.10
    lead_pad_ms: int = 150
    tail_pad_ms: int = 250
    snap_slack_ms: int = 800


def sanitize_cue_timing(
    cues: Sequence[SourceCue],
    speech_spans: Sequence[SpeechSpan],
    *,
    window_start_ms: int,
    window_end_ms: int,
    policy: TimingQaPolicy | None = None,
) -> tuple[list[SourceCue], dict[str, object]]:
    """Sanitize cue timing inside [window_start_ms, window_end_ms).

    Returns (sanitized cues, report).  Cues outside the window pass through
    untouched; all times are source-timeline milliseconds.
    """

    policy = policy or TimingQaPolicy()
    ordered = sorted(cues, key=lambda cue: (cue.source_start_ms, cue.source_end_ms, cue.cue_id))
    spans = sorted((span for span in speech_spans if span.end_ms > span.start_ms), key=lambda span: span.start_ms)

    actions: list[dict[str, object]] = []
    result: list[SourceCue] = []
    recent_texts: list[tuple[int, str]] = []  # (cue_end_ms, normalized_text)

    for cue in ordered:
        if cue.source_end_ms <= window_start_ms or cue.source_start_ms >= window_end_ms:
            result.append(cue)
            continue

        visible_start_ms = max(cue.source_start_ms, window_start_ms)
        visible_end_ms = min(cue.source_end_ms, window_end_ms)
        visible_duration_ms = visible_end_ms - visible_start_ms
        # Clip padding may expose only the final 0-300 ms of the previous
        # topic.  Extending that sliver to the minimum readable duration turns
        # it into a prominent false opening subtitle.  Drop the unreadable
        # boundary text *before* flash extension while preserving its audio.
        # This also catches a pre-clipped source cue starting exactly at the
        # selected boundary, where provenance no longer says it straddled.
        if (
            visible_start_ms == window_start_ms
            and 0 < visible_duration_ms <= policy.boundary_fragment_max_visible_ms
        ):
            duration_ms = max(1, cue.source_end_ms - cue.source_start_ms)
            density_cps = _char_count(cue.text) / (duration_ms / 1000.0)
            actions.append(
                _action(
                    cue,
                    "drop_boundary_fragment",
                    cue.source_start_ms,
                    cue.source_end_ms,
                    None,
                    None,
                    ["unreadable_leading_boundary_fragment"],
                    0.0,
                    density_cps,
                )
            )
            continue

        start_ms = cue.source_start_ms
        end_ms = cue.source_end_ms
        duration_ms = max(1, end_ms - start_ms)
        text_norm = _normalize_text(cue.text)
        density_cps = _char_count(cue.text) / (duration_ms / 1000.0)
        islands = [span for span in spans if span.end_ms > start_ms and span.start_ms < end_ms]
        vad_overlap_ms = sum(min(end_ms, span.end_ms) - max(start_ms, span.start_ms) for span in islands)
        vad_ratio = vad_overlap_ms / duration_ms
        reasons: list[str] = []

        is_recent_duplicate = any(
            text_norm == earlier_text and start_ms - earlier_end <= policy.stuck_dup_lookback_ms
            for earlier_end, earlier_text in recent_texts
        )
        recent_texts.append((end_ms, text_norm))

        # 1. Whisper stuck-segment: the only deletion this module performs.
        if (
            is_recent_duplicate
            and duration_ms >= policy.hard_max_ms
            and vad_ratio < policy.drop_max_vad_ratio
        ):
            actions.append(
                _action(cue, "drop_stuck_segment", start_ms, end_ms, None, None, ["duplicate_of_recent_cue", "hard_max_duration", "no_vad_support"], vad_ratio, density_cps)
            )
            continue

        new_start = start_ms
        new_end = end_ms

        # 2. Long low-density cue: retime to its speech islands, else clamp.
        if duration_ms > policy.soft_max_ms and density_cps < policy.min_density_cps:
            if islands:
                new_start = max(start_ms, islands[0].start_ms - policy.lead_pad_ms)
                new_end = min(end_ms, islands[-1].end_ms + policy.tail_pad_ms)
                reasons.append("long_low_density_retimed_to_speech")
            else:
                new_end = start_ms + policy.soft_max_ms
                reasons.append("long_low_density_clamped")

        if new_end - new_start < policy.min_readable_ms:
            new_end = new_start + policy.min_readable_ms
            if "flash_extended" not in reasons:
                reasons.append("flash_extended")

        if reasons:
            actions.append(_action(cue, "retime", start_ms, end_ms, new_start, new_end, reasons, vad_ratio, density_cps))
        result.append(replace(cue, source_start_ms=new_start, source_end_ms=new_end))

    # Final monotonic pass: extensions must not overlap the next cue, and
    # nothing may leak past the window end.
    result.sort(key=lambda cue: (cue.source_start_ms, cue.source_end_ms, cue.cue_id))
    for index, cue in enumerate(result):
        if cue.source_end_ms <= window_start_ms or cue.source_start_ms >= window_end_ms:
            continue
        bounded_end = min(cue.source_end_ms, window_end_ms)
        if index + 1 < len(result):
            bounded_end = min(bounded_end, result[index + 1].source_start_ms)
        bounded_end = max(bounded_end, cue.source_start_ms + 1)
        if bounded_end != cue.source_end_ms:
            result[index] = replace(cue, source_end_ms=bounded_end)

    report = {
        "schema_version": SUBTITLE_TIMING_QA_SCHEMA_VERSION,
        "window": {"start_ms": window_start_ms, "end_ms": window_end_ms},
        "policy": {
            "boundary_fragment_max_visible_ms": policy.boundary_fragment_max_visible_ms,
            "min_readable_ms": policy.min_readable_ms,
            "soft_max_ms": policy.soft_max_ms,
            "hard_max_ms": policy.hard_max_ms,
            "min_density_cps": policy.min_density_cps,
        },
        "speech_span_count": len(spans),
        "speech_total_ms": sum(span.end_ms - span.start_ms for span in spans),
        "vad_evidence_contract": "positive_only_never_shrink_normal_asr_cues",
        "actions": actions,
        "counts": {
            "dropped": sum(
                1
                for action in actions
                if action["action"] in {"drop_stuck_segment", "drop_boundary_fragment"}
            ),
            "retimed": sum(1 for action in actions if action["action"] == "retime"),
        },
    }
    return result, report


def _action(
    cue: SourceCue,
    action: str,
    before_start: int,
    before_end: int,
    after_start: int | None,
    after_end: int | None,
    reasons: list[str],
    vad_ratio: float,
    density_cps: float,
) -> dict[str, object]:
    return {
        "cue_id": cue.cue_id,
        "text": cue.text[:40],
        "action": action,
        "before": {"start_ms": before_start, "end_ms": before_end},
        "after": None if after_start is None else {"start_ms": after_start, "end_ms": after_end},
        "reasons": reasons,
        "vad_overlap_ratio": round(vad_ratio, 3),
        "density_cps": round(density_cps, 3),
    }


def _normalize_text(text: str) -> str:
    return _PUNCT_RX.sub("", text)


def _char_count(text: str) -> int:
    return len(_normalize_text(text))


def build_ssh_silero_vad_provider(
    host: str,
    *,
    remote_script: str | None = None,
) -> SpeechSpansProvider:
    """Speech spans via silero VAD (onnxruntime) on the remote host.

    Extracts the [start_ms, end_ms) window of the source video as 16k mono WAV
    locally, ships it over, and maps the returned clip-relative spans back to
    source-timeline milliseconds.  Provisioning: scripts/silero_vad_spans.py +
    assets/vad/silero_vad.onnx（随仓分发；参考部署放 <host>:/opt/bilive/vad/）。
    脚本路径可用 ``AUTOSLICE_VAD_SCRIPT`` 覆盖，默认为参考部署路径。

    Raises RuntimeError on any failure — the caller decides whether timing QA
    is best-effort (record and skip) or mandatory.
    """

    if remote_script is None:
        remote_script = os.environ.get("AUTOSLICE_VAD_SCRIPT") or str(
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "silero_vad_spans.py"
        )

    def provider(source_video: Path, start_ms: int, end_ms: int) -> list[SpeechSpan]:
        duration_ms = max(1, end_ms - start_ms)
        local_host = host in {"localhost", "127.0.0.1"}
        remote_wav = f"/tmp/vad_{start_ms}_{end_ms}_{Path(source_video).stem[:24]}.wav"
        with tempfile.TemporaryDirectory(prefix="vad_wav_") as tmp:
            wav_path = Path(tmp) / "window.wav"
            extract = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{start_ms / 1000:.3f}",
                    "-i",
                    str(source_video),
                    "-t",
                    f"{duration_ms / 1000:.3f}",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(wav_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if extract.returncode != 0:
                raise RuntimeError(f"vad wav extraction failed: {extract.stderr[-300:]}")
            if local_host:
                # localhost 快路径：免 scp/免 self-ssh，直接对本地 wav 跑
                # spans 脚本。生产宿主金丝雀证明直执行与 ssh localhost 的
                # 输出逐字节相同（python3 从 PATH 解析，与 ssh 登录壳一致）。
                run = subprocess.run(
                    ["python3", remote_script, str(wav_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
            else:
                copy = subprocess.run(
                    ["scp", "-q", str(wav_path), f"{host}:{remote_wav}"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if copy.returncode != 0:
                    raise RuntimeError(f"vad wav upload failed: {copy.stderr[-300:]}")
                run = subprocess.run(
                    [
                        "ssh",
                        host,
                        f"python3 {shlex.quote(remote_script)} {shlex.quote(remote_wav)}; rc=$?; rm -f {shlex.quote(remote_wav)}; exit $rc",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
        if run.returncode != 0:
            raise RuntimeError(f"silero vad failed rc={run.returncode}: {run.stderr[-300:]}")
        payload = json.loads(run.stdout)
        return [
            SpeechSpan(start_ms=start_ms + int(span["start_ms"]), end_ms=start_ms + int(span["end_ms"]))
            for span in payload.get("spans", [])
        ]

    return provider
