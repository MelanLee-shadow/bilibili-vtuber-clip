"""Song lane: window timing, delivery gates, selector retry policy + produce_song.

Extracted from scripts/free_session_autoslice.py (2026-07-15 屎山治理第五刀).
produce_song plus its timing/status/gate/retry helpers move here. Runner globals
(BASE, log, the SONG_* config constants, delivery helpers) resolve at CALL TIME
through the lazy ``_runner`` proxy so monkeypatching the runner steers this code
AND the module survives being imported while the runner runs as ``__main__``
under the cron. The runner re-imports every public name.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from src.autoslice.batch_terminal_state import project_terminal_song_disposition
from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice.song_delivery import SongDeliveryError
from src.autoslice.song_name_authority import (
    authoritative_song_title,
    extract_audio_song_name_authority,
    ordered_song_lrc_queries,
    quoted_titles_in,
    record_song_naming,
)
from src.autoslice.verified_io import _matches_sha256


_runner = RunnerProxy()

# 全源趟失败时保留下来的取证键。音频命名权威也在其中：窄窗那趟没有 audio
# aligner，真名只可能在 ``_full`` 那趟证出来，不带过来就会被这层白名单埋掉
# （《心型病毒》被埋掉的正是同一层）。
FULL_SOURCE_RETRY_FORENSIC_KEYS = (
    "start_ms",
    "end_ms",
    "rc",
    "status",
    "reason_codes",
    "window_classified_song",
    "song_complete",
    "song_completion_evidence",
    "song_name_authority",
    "transient_failure_code",
    "next_retry_at_epoch",
    "next_retry_at",
)


def _project_terminal_song_result(result: dict) -> dict:
    project_terminal_song_disposition(
        result,
        terminal_performer_rejection_codes=(
            _runner.SONG_TERMINAL_PERFORMER_REJECTION_CODES
        ),
        infra_transient_reason_codes=_runner.SONG_INFRA_TRANSIENT_REASON_CODES,
        song_infra_retry_cap=_runner.SONG_INFRA_RETRY_CAP,
    )
    record_song_naming(result)
    return result


def _srt_cue_spans(srt_path: Path, lo_ms: int, hi_ms: int) -> list[tuple[int, int]]:
    """(start_ms, end_ms) cue spans overlapping [lo,hi] from a whole-segment SRT."""
    try:
        text = srt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    spans = []
    for m in _runner._SRT_TS_RX.finditer(text):
        s = (int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])) * 1000 + int(m[4])
        e = (int(m[5]) * 3600 + int(m[6]) * 60 + int(m[7])) * 1000 + int(m[8])
        if e >= lo_ms and s <= hi_ms:
            spans.append((s, e))
    return spans


def _song_core_span(srt_path: Path, lo_ms: int, hi_ms: int,
                    *, min_gap_ms: int = 8_000, edge_frac: float = 0.35, min_span_ms: int = 90_000,
                    tail_gap_ms: int = 16_000, tail_frac: float = 0.12) -> tuple[int, int]:
    """Trim [lo,hi] to the sung core using ASR speech gaps.  A performance is set
    off from the surrounding chatter by a ≥8s music-intro gap at the front and a
    ≥16s outro gap at the back (《屑屑》: the recall anchor bloated BOTH ends — into
    the pig-nose talk before AND the '谢谢大家' talk after, so the window kept
    classifying as talk).  LEAD trim: song starts after the last big gap in the
    leading ``edge_frac``.  TAIL trim is DELIBERATELY conservative — only a LARGE
    (≥16s, bigger than a mid-song instrumental interlude) gap in the LAST
    ``tail_frac`` of the span counts, so the song's own late breaks are never
    clipped (clipping → SONG_PARTIAL, worse than carrying a little outro).  Returns
    (lo,hi) unchanged / partially-trimmed when a trim would be degenerate."""
    cues = _runner._srt_cue_spans(srt_path, lo_ms, hi_ms)
    if len(cues) < 4 or hi_ms - lo_ms <= min_span_ms:
        return lo_ms, hi_ms
    span = hi_ms - lo_ms
    lead_cut = lo_ms + edge_frac * span
    lead = [(s1 - e0, s1) for (s0, e0), (s1, e1) in zip(cues, cues[1:]) if e0 <= lead_cut and (s1 - e0) >= min_gap_ms]
    new_lo = max(lead, key=lambda g: g[0])[1] if lead else lo_ms
    tail_cut = hi_ms - tail_frac * span
    tail = [e0 for (s0, e0), (s1, e1) in zip(cues, cues[1:]) if s1 >= tail_cut and (s1 - e0) >= tail_gap_ms]
    new_hi = min(tail) if tail else hi_ms
    if new_lo == lo_ms and new_hi == hi_ms:
        return lo_ms, hi_ms
    if new_hi - new_lo < min_span_ms:  # a trim would over-shorten → keep the generous span
        return lo_ms, hi_ms
    return int(new_lo), int(new_hi)


def song_status(rc: int, delivered: bool) -> str:
    """Honest song-lane status words (2026-07-09 audit): the selector exiting 0
    only means the PIPELINE ran.  blocked = not a song / performance incomplete
    / no materialized artifact — never 'ok'."""
    if rc != 0:
        return "failed"
    return "review_ready" if delivered else "blocked"


def song_proof_retry_window(anchor_start_ms: int, anchor_end_ms: int, segment_duration_ms: int) -> tuple[int, int]:
    """Expand a recall anchor against the original segment for LRC proof."""
    start_ms = max(0, anchor_start_ms - _runner.SONG_PROOF_RETRY_PRE_MS)
    end_ms = anchor_end_ms + _runner.SONG_PROOF_RETRY_POST_MS
    if segment_duration_ms:
        end_ms = min(segment_duration_ms, end_ms)
    return start_ms, end_ms


def song_delivery_ok(
    selector_rc: int,
    is_song: bool,
    reason_codes,
    completion_evidence: bool | dict | None,
) -> bool:
    """Ivan's FINAL song rule (2026-07-10): at most MAX_SONGS_PER_SESSION per live session,
    danmaku-desc; a song is delivered when the window IS a song and the
    performance is AFFIRMATIVELY PROVEN complete.  Absence of ``SONG_PARTIAL``
    is not evidence: the 2026-07-09 ``芽吹くとき`` run had no LRC proof
    and therefore never emitted that negative code, but was still incorrectly
    delivered.  Everything else the semantic judge flags (closure, viewer
    context, boundary style, AUTO_UPLOAD/BLOCK itself) is reviewer REFERENCE,
    not a delivery gate."""
    # A bare bool was the pre-host-vocal compatibility shortcut.  It can carry
    # no hash-bound performer identity and is therefore no longer acceptable.
    reasons = {str(code) for code in (reason_codes or [])}
    proof_path = completion_evidence.get("host_vocal_proof_path") if isinstance(completion_evidence, dict) else None
    proof_sha256 = completion_evidence.get("host_vocal_proof_sha256") if isinstance(completion_evidence, dict) else None
    proof_ready = (
        isinstance(completion_evidence, dict)
        and completion_evidence.get("ready") is True
        and completion_evidence.get("host_vocal_status") == "READY"
        and completion_evidence.get("host_vocal_decision") == _runner.HOST_VOCAL_PRESENT_DECISION
        and completion_evidence.get("live_performance_status") == "READY"
        and completion_evidence.get("live_performance_mode") == "LIVE_STREAMER_SINGING"
        and completion_evidence.get("joint_singing_decision") == _runner.VERIFIED_HOST_SINGING_DECISION
        and isinstance(proof_path, str)
        and isinstance(proof_sha256, str)
        and _matches_sha256(Path(proof_path), proof_sha256)
    )
    identity_failure = (
        _runner.HOST_NOT_SINGING_REASON in reasons
        or "SONG_BACKGROUND_PLAYBACK_ONLY" in reasons
        or "SONG_LIVE_PERFORMANCE_UNPROVEN" in reasons
        or any(
        code.startswith("SONG_HOST_VOCAL_") and code != "SONG_HOST_VOCAL_VERIFIED" for code in reasons
        )
    )
    return (
        selector_rc == 0
        and bool(is_song)
        and proof_ready
        and "SONG_PARTIAL" not in reasons
        and not identity_failure
    )


def fresh_song_selector_dir(out_dir: Path, tag: str) -> Path:
    """Create an empty, invocation-owned selector output directory.

    Selector attempts used to share ``song_selector{tag}``.  A failed process
    could therefore leave the runner reading a previous invocation's valid
    ``summary.json`` and artifacts.  Keep every attempt as non-destructive
    evidence under the stable tag directory, but give the current subprocess a
    new empty child so only files it writes can influence this attempt.
    """
    history_dir = out_dir / f"song_selector{tag}"
    history_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="attempt-", dir=history_dir))


def song_window_media_path(
    out_dir: Path,
    candidate_id: str,
    tag: str,
    start_ms: int,
    end_ms: int,
) -> Path:
    """Return an interval-bound path for a materialized song proof window.

    The old fixed ``<candidate><tag>_source.mp4`` name let a later retry reuse
    bytes cut for a different interval.  The SRT and declared duration then
    described the new interval while AGY/CAM++ read the old media.  Bind the
    exact source interval into the filename so stale windows remain available
    for forensics but can never satisfy a different attempt.
    """

    lane = tag.removeprefix("_") or "tight"
    return out_dir / f"{candidate_id}_{lane}_{start_ms}_{end_ms}_source.mp4"


def classify_song_selector_transient(log_text: str) -> str | None:
    """Turn selector/provider diagnostics into a stable retry reason code."""

    upper = log_text.upper()
    if "TOO MANY REQUESTS" in upper or re.search(
        r"(?:HTTP(?: ERROR)?|STATUS(?: CODE)?|RESPONSE)\D{0,12}429\b", upper
    ):
        return "CPA_RATE_LIMITED"
    if "CPA_LLM_JUDGE_MODEL_DOWN" in upper:
        return "CPA_MODEL_DOWN"
    if re.search(r"HTTP(?: ERROR)?\s*(?:5\d\d|ERROR 5\d\d)", upper):
        return "CPA_UPSTREAM_5XX"
    if "TIMEOUT" in upper or "TIMED OUT" in upper:
        return "CPA_UPSTREAM_TIMEOUT"
    return None


def song_infra_retry_delay_seconds(completed_retry_count: int) -> int:
    """Exponential cross-tick backoff, capped so a provider outage stays bounded."""

    exponent = max(0, int(completed_retry_count))
    return min(_runner.SONG_INFRA_RETRY_MAX_SECONDS, _runner.SONG_INFRA_RETRY_BASE_SECONDS * (2**exponent))


def song_review_retry_after_seconds(summary_record: dict, selector_dir: Path) -> int | None:
    """Read a retry-after value only from this selector attempt's sidecar."""

    candidate_dir_raw = summary_record.get("candidate_dir")
    if not isinstance(candidate_dir_raw, str) or not candidate_dir_raw:
        return None
    try:
        candidate_dir = Path(candidate_dir_raw).resolve(strict=True)
        candidate_dir.relative_to(selector_dir.resolve(strict=True))
    except (OSError, ValueError):
        return None
    values: list[int] = []
    for marker in candidate_dir.glob("source_context/*.jingting.review-required.json"):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        value = (data.get("metadata") or {}).get("retry_after_seconds")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            values.append(value)
    return max(values) if values else None


def _early_song_infra_failure(
    result: dict,
    *,
    reason_code: str,
    error: str,
) -> dict:
    """Type and back off failures that happen before selector evidence exists."""

    retry_count = int(result.get("transient_retry_count") or 0)
    retry_delay = song_infra_retry_delay_seconds(retry_count)
    next_retry_epoch = int(time.time()) + retry_delay
    result.update(
        {
            "error": error,
            "status": "failed",
            "reason_codes": [reason_code],
            "transient_failure_code": reason_code,
            "retry_after_seconds": retry_delay,
            "next_retry_at_epoch": next_retry_epoch,
            "next_retry_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(next_retry_epoch)
            ),
        }
    )
    return result


def scheduled_song_retry_epoch(state: dict) -> int | None:
    """Earliest future infrastructure retry; its presence makes a date nonterminal."""

    now = time.time()
    epochs: list[int] = []
    for record in state.get("songs", []):
        if not isinstance(record, dict) or record.get("status") not in {"blocked", "failed"}:
            continue
        reasons = {str(code) for code in record.get("reason_codes") or []}
        if not reasons & _runner.SONG_INFRA_TRANSIENT_REASON_CODES:
            continue
        value = record.get("next_retry_at_epoch")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value) > now
        ):
            epochs.append(int(value))
    return min(epochs) if epochs else None


def scheduled_talk_retry_epoch(state: dict) -> int | None:
    """Return only a genuinely future retry; due rows were handled before projection."""

    now = time.time()
    epochs: list[int] = []
    for record in state.get("picks", []):
        if not isinstance(record, dict) or record.get("failure_recoverable") is not True:
            continue
        value = record.get("next_retry_at_epoch")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value) > now
        ):
            epochs.append(int(value))
    return min(epochs) if epochs else None


def scheduled_retry_epoch(state: dict) -> int | None:
    values = [
        _runner.scheduled_song_retry_epoch(state),
        _runner.scheduled_talk_retry_epoch(state),
    ]
    return min(value for value in values if value is not None) if any(
        value is not None for value in values
    ) else None


def _build_song_selector_command(
    *,
    item: dict,
    window_mp4: Path,
    window_srt: Path,
    selector_dir: Path,
    start: int,
    end: int,
    anchor_start: int,
    anchor_end: int,
    tag: str,
) -> list[str]:
    """Build one interval-bound selector invocation for the song lane."""

    command = [
        sys.executable,
        str(_runner.REPO_ROOT / "scripts" / "run_full_session_selector_cpa_shadow.py"),
        "--source-video", str(window_mp4),
        "--source-srt", str(window_srt),
        "--output-dir", str(selector_dir),
        "--max-candidates", "1",
        "--source-duration-ms", str(max(0, end - start)),
        "--cpa-command", _runner.cpa_qa_cmd(),
        "--semantic-recall-llm-command", _runner.CPA_CMD_DEEP,
        "--song-hint-llm-command", _runner.CPA_CMD_STANDARD,
        "--title-llm-command", _runner.CPA_CMD_TITLE,
        "--cover-art-direction-llm-command", _runner.CPA_CMD_STRUCTURED,
        "--lrc-provider", "auto",
        "--burn-preview",
        "--publish-staging",
        "--branding-intro-manifest", str(_runner.profile_asset_file("branding_intro_manifest")),
        "--host-vocal-python", str(_runner.HOST_VOCAL_PYTHON),
        "--host-vocal-reference-profile", str(_runner.HOST_VOCAL_PROFILE),
        "--host-vocal-reference-dir", str(_runner.HOST_VOCAL_REFERENCE_DIR),
        "--host-vocal-model-dir", str(_runner.HOST_VOCAL_MODEL_DIR),
    ]
    semantic_hook = str(item.get("hook") or "").strip()
    known_song_query = str(item.get("preview") or "").strip()
    visual_title_hint = str(item.get("title_hint") or "").strip()
    quoted_titles = quoted_titles_in(semantic_hook, known_song_query)
    # Prefer the visual/quoted title before noisy singing ASR.  LRC/audio proof,
    # not this query order, still authorizes the performance.  一旦音频链已经
    # 证出真名（重试/续跑携带 song_name_authority），命名权就整体切过去：错名
    # 不再占 preferred_title_hints 的位置。排序与阈值一字未动。
    for query in ordered_song_lrc_queries(
        authority_title=authoritative_song_title(item),
        visual_title_hint=visual_title_hint,
        quoted_titles=quoted_titles,
        known_song_query=known_song_query,
    ):
        command.extend(["--song-lrc-query", query])
    command.extend(
        [
            "--seed-song-candidate-id",
            f"seededsong_{max(0, anchor_start - start)}_{min(end - start, anchor_end - start)}",
            "--seed-song-anchor-start-ms",
            str(max(0, anchor_start - start)),
            "--seed-song-anchor-end-ms",
            str(min(end - start, anchor_end - start)),
        ]
    )
    if tag == "_full":
        command.append("--agy-audio-lrc-align")
    return command


def _read_song_selector_summary(
    *,
    selector_dir: Path,
    result: dict,
) -> tuple[object, list, bool, dict, Path]:
    """Read only the current attempt's summary and bind its authority into result."""

    decision = None
    reasons: list = []
    is_song = False
    summary_record: dict = {}
    summary_path = selector_dir / "summary.json"
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            for entry in summary.get("records", []) if isinstance(summary, dict) else []:
                if not isinstance(entry, dict):
                    continue
                summary_record = entry
                decision = entry.get("decision_action") or decision
                reasons = list(
                    dict.fromkeys(
                        [
                            *reasons,
                            *_nested_reason_codes(entry),
                        ]
                    )
                )
                is_song = is_song or _runner.record_is_song(entry)
        except ValueError:
            pass
    # 命名权威与交付授权解耦：host-vocal 判否只说明「可能不是李豆沙在唱」，
    # 音频对齐仍然证明了这一段是哪首歌。所以无论这趟能不能交付都记权威名。
    audio_authority = extract_audio_song_name_authority(summary_record)
    if audio_authority is not None:
        result["song_name_authority"] = audio_authority
    if summary_record:
        try:
            result["selector_summary_path"] = str(summary_path.resolve(strict=True))
            result["selector_summary_sha256"] = "sha256:" + _runner._sha256_regular_file(summary_path)
            result["selector_record_candidate_id"] = str(
                summary_record.get("candidate_id") or ""
            )
        except (OSError, SongDeliveryError):
            # Missing authority disables zero-compute recovery, not evaluation
            # of the current in-memory, independently hash-bound proof chain.
            pass
    return decision, reasons, is_song, summary_record, summary_path


def _nested_reason_codes(record: object, *, _depth: int = 0) -> list[str]:
    """Collect machine reasons from the selector's nested proof record.

    Song failures are often emitted by ``song_repair_gate`` or the source
    context fallback below the top-level record.  Dropping those codes turns a
    provider outage into a permanent content rejection, so the unattended
    runner must preserve them when classifying retries.
    """

    if _depth > 6:
        return []
    if isinstance(record, dict):
        reasons: list[str] = []
        values = record.get("reason_codes")
        if isinstance(values, list):
            reasons.extend(str(value) for value in values if isinstance(value, str) and value)
        for value in record.values():
            if isinstance(value, (dict, list)):
                reasons.extend(_nested_reason_codes(value, _depth=_depth + 1))
        return list(dict.fromkeys(reasons))
    if isinstance(record, list):
        reasons: list[str] = []
        for value in record:
            if isinstance(value, (dict, list)):
                reasons.extend(_nested_reason_codes(value, _depth=_depth + 1))
        return list(dict.fromkeys(reasons))
    return []


def _early_published_song_block(item: dict) -> dict | None:
    """Skip a visually identified prior upload before any provider/media work."""

    title_hint = str(item.get("title_hint") or "").strip()
    if not title_hint:
        return None
    common = {
        "candidate_id": item.get("cid"),
        "session_id": item.get("session_id"),
        "status": "blocked",
        "decision": "BLOCK",
        "rc": 0,
        "title_hint": title_hint,
        "pipeline_fingerprint": _runner.pipeline_fingerprint(),
        "song_pipeline_fingerprint": _runner.song_pipeline_fingerprint(),
    }
    try:
        prior_upload = _runner.published_song_match(title_hint)
    except _runner.PublishedSongHistoryError as exc:
        return {
            **common,
            "reason_codes": ["SONG_PUBLISHED_HISTORY_UNAVAILABLE"],
            "error": str(exc),
        }
    if prior_upload is None:
        return None
    return {
        **common,
        "reason_codes": ["SONG_PREVIOUSLY_PUBLISHED"],
        "published_song_match": prior_upload,
    }


def _published_song_delivery_allowed(result: dict, summary_record: dict) -> bool:
    """Apply the final exact-title duplicate gate before atomic delivery."""

    boundary = (summary_record.get("source_context_job") or {}).get("song_boundary") or {}
    query = str(boundary.get("song_title") or result.get("title") or "").strip()
    try:
        prior_upload = _runner.published_song_match(query)
    except _runner.PublishedSongHistoryError as exc:
        result["decision"] = "BLOCK"
        result["published_song_history_error"] = str(exc)
        result["reason_codes"] = list(
            dict.fromkeys(
                [*(result.get("reason_codes") or []), "SONG_PUBLISHED_HISTORY_UNAVAILABLE"]
            )
        )
        return False
    if prior_upload is None:
        return True
    result["decision"] = "BLOCK"
    result["published_song_match"] = prior_upload
    result["reason_codes"] = list(
        dict.fromkeys([*(result.get("reason_codes") or []), "SONG_PREVIOUSLY_PUBLISHED"])
    )
    return False


def _apply_canonical_song_title(result: dict, *, summary_record: dict, cid: str) -> None:
    """Ivan 2026-07-19 歌切标题铁律：边界/LRC 验证过的《歌名》是唯一标题权威，
    标题固定为「【李豆沙】豆沙歌，《歌名》」——staged/LLM 标题带任何 hook 尾巴
    或不同拼写时在这里最终定形，覆盖记录留审计。

    Ivan 2026-08-10：这个「验证过」明确收窄为**听音频那条链**证过的身份。纯
    文本 LRC 对齐同样会写出 FULL_SONG_READY 边界，但它归根到底是 BCUT 中文
    ASR 的匹配结果，分不开《心型病毒》/《新型病毒》这种同音对。音频没证成
    时正确行为是**没有权威名**（维持既有保守处置），不是回落到垃圾名。"""

    authority = extract_audio_song_name_authority(summary_record)
    canonical_title = _runner.verified_song_fallback_title(
        authority.get("song_title") if authority else None
    )
    if canonical_title and result.get("title") != canonical_title:
        if result.get("title"):
            _runner.log(
                f"song lane {cid}: staged title {result['title']!r} "
                f"canonicalized to {canonical_title!r}"
            )
            result["title_before_canonical_override"] = result["title"]
        result["title"] = canonical_title


def produce_song(date: str, item: dict) -> dict:
    """Run the fail-closed song pipeline; retry anchor bleed on the sung core."""
    early_block = _early_published_song_block(item)
    if early_block is not None:
        return early_block
    item = normalize_song_work_item(date, item)

    seg_dur_ms = item["seg_dur_ms"]
    segment = Path(item["segment_path"])
    cid = item["cid"]
    out_dir = _runner.BASE / "out" / date / cid
    out_dir.mkdir(parents=True, exist_ok=True)

    def window_for(a0: int, a1: int) -> tuple[int, int]:
        s = max(0, a0 - _runner.SONG_WINDOW_PRE_MS)
        e = min(seg_dur_ms, a1 + _runner.SONG_WINDOW_POST_MS) if seg_dur_ms else a1 + _runner.SONG_WINDOW_POST_MS
        return s, e

    def attempt(start: int, end: int, tag: str) -> dict:
        _runner.log(f"song lane {cid}{tag}: window {start // 1000}-{end // 1000}s (danmaku x{item.get('danmaku', 0)}) from {segment.name}")
        result = {"candidate_id": cid, "segment": segment.name, "start_ms": start, "end_ms": end,
                  "danmaku": item.get("danmaku", 0), "hook": item.get("hook", ""), "preview": item.get("preview", "")[:60], "rc": -1,
                  "discovery_lane": item.get("lane"), "title_hint": item.get("title_hint"),
                  "visual_song_evidence": item.get("visual_song_evidence"), "song_name_authority": item.get("song_name_authority"),
                  "pipeline_fingerprint": _runner.pipeline_fingerprint(),
                  "song_pipeline_fingerprint": _runner.song_pipeline_fingerprint(),
                  "transient_retry_count": int(item.get("transient_retry_count") or 0),
                  "anchor_start_ms": item.get("anchor_start_ms"),
                  "anchor_end_ms": item.get("anchor_end_ms")}
        window_mp4 = _runner.song_window_media_path(out_dir, cid, tag, start, end)
        if not window_mp4.is_file():
            cut = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", f"{start / 1000:.3f}", "-to", f"{end / 1000:.3f}", "-i", str(segment),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
                 str(window_mp4)],
                check=False, capture_output=True, text=True, timeout=3600,
            )
            if cut.returncode != 0 or not window_mp4.is_file():
                return _early_song_infra_failure(
                    result,
                    reason_code="SONG_WINDOW_CUT_FAILED",
                    error=f"window cut failed: {cut.stderr[-200:]}",
                )

        src_srt = _runner.BASE / "cache" / date / f"{segment.stem}.bcut.srt"
        window_srt = out_dir / f"{cid}{tag}_source.srt"
        if _runner.slice_srt(src_srt, start, end, window_srt) == 0:
            return _early_song_infra_failure(
                result,
                reason_code="SONG_SOURCE_TRANSCRIPT_EMPTY",
                error="empty window srt",
            )

        # NOTE: segment danmaku XML is segment-relative — do not pass it to the
        # selector (it would misalign against the window-relative video); LRC is
        # the subtitle authority for songs anyway.
        selector_dir = _runner.fresh_song_selector_dir(out_dir, tag)
        log_path = _runner.BASE / "logs" / f"{date}_{cid}.log"
        try:
            log_offset = log_path.stat().st_size
        except OSError:
            log_offset = 0
        with open(log_path, "a", encoding="utf-8") as sink:
            selector_env = _runner.song_selector_env(date)
            selector_command = _build_song_selector_command(
                item=item,
                window_mp4=window_mp4,
                window_srt=window_srt,
                selector_dir=selector_dir,
                start=start,
                end=end,
                anchor_start=anchor_start,
                anchor_end=anchor_end,
                tag=tag,
            )
            completed = subprocess.run(
                selector_command,
                check=False, stdout=sink, stderr=subprocess.STDOUT, timeout=5400,
                cwd=str(_runner.REPO_ROOT), env=selector_env,
            )
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as source:
                source.seek(log_offset)
                invocation_log = source.read()
        except OSError:
            invocation_log = ""
        transient_code = _runner.classify_song_selector_transient(invocation_log)
        result["rc"] = completed.returncode
        result["log"] = str(log_path)
        decision, reasons, is_song, summary_record, summary_path = (
            _read_song_selector_summary(selector_dir=selector_dir, result=result)
        )
        result["decision"] = decision
        completion = _runner.song_completion_evidence(summary_record)
        reason_set = {str(code) for code in reasons}
        specific_agy_transient = next(
            (
                code
                for code in (
                    "AGY_QUOTA_EXHAUSTED",
                    "AGY_TIMEOUT",
                    "AGY_EMPTY_OUTPUT",
                    "AGY_FAILED_RC",
                    "AGY_AND_GEMINI_API_FAILED",
                )
                if code in reason_set
            ),
            None,
        )
        if specific_agy_transient is not None:
            transient_code = specific_agy_transient
        elif transient_code is None and "AGY_SOURCE_CONTEXT_RUNNER_FAILED" in reason_set:
            transient_code = "AGY_SOURCE_CONTEXT_RUNNER_FAILED"
        elif transient_code is None:
            # Ivan 2026-08-08 歌lane provider门修复: JINGTING_PROVIDER_NOT_AGY
            # and siblings were never recognized here, so a Jingting-chain
            # outage (with its cascading SONG_*_MISSING/INVALID artifacts)
            # left transient_failure_code unset and got terminally rejected
            # by project_terminal_song_disposition instead of waiting
            # (2026-08-07 song_230754_1118 recurrence).  Any remaining
            # SONG_INFRA_TRANSIENT_REASON_CODES member from THIS attempt
            # wins per 50-song-lane.md's "同一 attempt 有明确 typed transient
            # 则 transient 优先".
            remaining_infra = reason_set & _runner.SONG_INFRA_TRANSIENT_REASON_CODES
            if remaining_infra:
                transient_code = min(remaining_infra)
        result["reason_codes"] = list(
            dict.fromkeys(
                [*reasons, *completion["reason_codes"], *([transient_code] if transient_code else [])]
            )
        )
        if transient_code:
            retry_count = int(result.get("transient_retry_count") or 0)
            provider_retry_after = _runner.song_review_retry_after_seconds(
                summary_record, selector_dir
            )
            retry_delay = max(
                _runner.song_infra_retry_delay_seconds(retry_count),
                (provider_retry_after + 60) if provider_retry_after is not None else 0,
            )
            next_retry_epoch = int(time.time()) + retry_delay
            result["transient_failure_code"] = transient_code
            result["retry_after_seconds"] = retry_delay
            result["next_retry_at_epoch"] = next_retry_epoch
            result["next_retry_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(next_retry_epoch)
            )
        result["window_classified_song"] = is_song
        result["song_completion_evidence"] = completion
        artifacts = _runner.song_delivery_artifacts(summary_record)
        if artifacts.get("title"):
            result["title"] = artifacts["title"]
        if not result.get("title"):  # pre-gate-era summaries carry no staging title
            for publish in sorted(selector_dir.glob("**/replacement_recuts/*.publish.json")):
                try:
                    result["title"] = json.loads(publish.read_text(encoding="utf-8")).get("title")
                except (OSError, ValueError):
                    pass
        # Burned video: only the summary-recorded, hash-bound artifact is
        # eligible.  Old selector debris must never inherit a newer proof.
        burned = Path(artifacts["video_path"]) if artifacts.get("video_path") else None
        if burned is not None and (
            not isinstance(artifacts.get("video_sha256"), str)
            or not _matches_sha256(burned, artifacts["video_sha256"])
        ):
            _runner.log(f"song lane {cid}: burned video hash missing/drifted since materialization — refusing stale artifact")
            burned = None
        result["song_complete"] = completion["ready"] is True
        result["lyrics_alignment_ready"] = completion["lyrics_alignment_status"] == "READY"
        if result["song_complete"]:
            _apply_canonical_song_title(result, summary_record=summary_record, cid=cid)
        if "cover_release_gate_satisfied" in artifacts:
            result["cover_release_gate_satisfied"] = artifacts["cover_release_gate_satisfied"]
        delivery_authorized = burned is not None and burned.is_file() and _runner.song_delivery_ok(
            completed.returncode, is_song, reasons, completion
        )
        if delivery_authorized:
            delivery_authorized = _published_song_delivery_allowed(result, summary_record)
        if delivery_authorized:
            try:
                delivery_update = _runner._commit_verified_song_package(
                    date=date,
                    delivery_candidate_id=cid,
                    summary_record=summary_record,
                    title=str(result.get("title") or ""),
                    selector_rc=completed.returncode,
                    summary_authority_root=summary_path.parent,
                )
            except (OSError, SongDeliveryError, ValueError) as exc:
                _runner.log(
                    f"song lane {cid}: atomic verified delivery refused: "
                    f"{type(exc).__name__}: {exc}"
                )
                result["delivery_error"] = f"{type(exc).__name__}: {exc}"
                result["reason_codes"] = list(
                    dict.fromkeys([*(result.get("reason_codes") or []), "SONG_DELIVERY_ATOMIC_COPY_FAILED"])
                )
                # Positive song/host proof has already passed.  Reserve this
                # delivery slot so later songs cannot fill the quota before
                # the exact hash-bound package is deterministically recovered.
                recovery_authority = _runner._song_delivery_recovery_authority(
                    date=date,
                    outer_candidate_id=cid,
                    summary_path=result.get("selector_summary_path"),
                    summary_sha256=result.get("selector_summary_sha256"),
                    source_candidate_id=result.get("selector_record_candidate_id"),
                    title=result.get("title"),
                )
                if recovery_authority is not None:
                    result["verified_delivery_pending_commit"] = True
                    result["song_delivery_recovery_authority"] = recovery_authority
                else:
                    # A reservation without its complete state envelope can
                    # neither recover nor requeue, permanently consuming a
                    # delivery slot.  Keep capacity open and allow one bounded
                    # fresh attempt to recapture the missing authority.
                    result["reason_codes"] = list(
                        dict.fromkeys(
                            [
                                *(result.get("reason_codes") or []),
                                "SONG_DELIVERY_RECOVERY_AUTHORITY_MISSING",
                            ]
                        )
                    )
            else:
                result.update(delivery_update)
        result["status"] = _runner.song_status(
            completed.returncode, bool(result.get("delivered"))
        )
        return result

    anchor_start, anchor_end = item["anchor_start_ms"], item["anchor_end_ms"]
    src_srt = _runner.BASE / "cache" / date / f"{segment.stem}.bcut.srt"
    # A previous authoritative full-source pass that failed only because its
    # AGY runner timed out already proved that the tight window is a song but
    # lacks complete boundary evidence.  Cross-tick infrastructure recovery
    # should resume that expensive stage directly instead of spending another
    # model call rediscovering the same incomplete tight result.
    if item.get("resume_full_source") is True:
        full_start, full_end = _runner.song_proof_retry_window(
            anchor_start, anchor_end, seg_dur_ms
        )
        resumed = attempt(full_start, full_end, "_full")
        resumed["retried_full_source"] = True
        resumed["resumed_full_source_after_transient"] = True
        return _project_terminal_song_result(resumed)

    result = attempt(*window_for(anchor_start, anchor_end), "")
    if result.get("window_classified_song") and not result.get("song_complete"):
        full_start, full_end = _runner.song_proof_retry_window(
            anchor_start, anchor_end, seg_dur_ms
        )
        if full_start < result.get("start_ms", full_start) or full_end > result.get("end_ms", full_end):
            _runner.log(
                f"song lane {cid}: song identified but positive LRC boundary proof is missing — "
                f"retrying with original-source context {full_start // 1000}-{full_end // 1000}s"
            )
            proof_retry = attempt(full_start, full_end, "_full")
            if proof_retry.get("song_complete"):
                result = {**proof_retry, "retried_full_source": True}
            else:
                result["full_source_retry"] = {
                    key: proof_retry.get(key)
                    for key in FULL_SOURCE_RETRY_FORENSIC_KEYS
                }
                for key in (
                    "transient_failure_code",
                    "retry_after_seconds",
                    "next_retry_at_epoch",
                    "next_retry_at",
                ):
                    result.pop(key, None)
                for key in (
                    "transient_failure_code",
                    "retry_after_seconds",
                    "next_retry_at_epoch",
                    "next_retry_at",
                ):
                    if proof_retry.get(key) is not None:
                        result[key] = proof_retry[key]
                # The full-source AGY/CAM++ pass is authoritative.  A timeout,
                # nonzero exit, malformed/missing artifact, unknown reason, or
                # semantic rejection are all the same at this boundary: not a
                # complete positive.  Keep the tight attempt only for timeline
                # forensics; every nonpositive authoritative result revokes all
                # top-level authorization rather than promoting a hand-picked
                # subset of known performer reason codes.
                proof_reasons = [str(code) for code in (proof_retry.get("reason_codes") or [])]
                if not proof_reasons:
                    proof_reasons = ["SONG_AUTHORITATIVE_RETRY_INCOMPLETE"]
                result["initial_attempt_reason_codes"] = list(
                    result.get("reason_codes") or []
                )
                result["decision"] = "BLOCK"
                result["reason_codes"] = list(dict.fromkeys(proof_reasons))
                result["song_completion_evidence"] = proof_retry.get("song_completion_evidence")
                result["song_complete"] = False
                result["lyrics_alignment_ready"] = bool(proof_retry.get("lyrics_alignment_ready"))
                result["full_source_authoritative_block"] = True
                result.pop("delivered", None)
                result.pop("delivered_sidecars", None)
                if proof_retry.get("rc") == 0 and _runner.SONG_TERMINAL_PERFORMER_REJECTION_CODES.intersection(proof_reasons):
                    result["full_source_performer_rejection"] = True
    if not result.get("window_classified_song") and not result.get("delivered"):
        d0, d1 = _runner._song_core_span(src_srt, anchor_start, anchor_end)
        if d0 >= anchor_start + _runner.SONG_ANCHOR_TRIM_MIN_MS or d1 <= anchor_end - _runner.SONG_ANCHOR_TRIM_MIN_MS:
            _runner.log(f"song lane {cid}: window classified as talk — retrying on sung core {d0 // 1000}-{d1 // 1000}s")
            retry = attempt(*window_for(d0, d1), "_core")
            if retry.get("window_classified_song") or retry.get("delivered"):
                result = {**retry, "retried_core": True}
    return _project_terminal_song_result(result)


def normalize_song_work_item(date: str, item: dict) -> dict:
    """Accept both discovery queue items and persisted completed-attempt rows."""

    normalized = dict(item)
    cid = str(normalized.get("cid") or normalized.get("candidate_id") or "").strip()
    if not cid:
        raise ValueError("song work item is missing cid/candidate_id")
    segment_value = str(
        normalized.get("segment_path") or normalized.get("segment") or ""
    ).strip()
    if not segment_value:
        raise ValueError(f"song work item {cid} is missing segment_path/segment")
    segment = Path(segment_value)
    if not segment.is_absolute():
        segment = _runner.REC_ROOT / date / segment.name
    seg_dur_ms = normalized.get("seg_dur_ms")
    if (
        not isinstance(seg_dur_ms, int)
        or isinstance(seg_dur_ms, bool)
        or seg_dur_ms <= 0
    ):
        seg_dur_ms = _runner.ffprobe_ms(segment)
    if seg_dur_ms <= 0:
        raise ValueError(f"song work item {cid} has no valid source duration")
    for key in ("anchor_start_ms", "anchor_end_ms"):
        value = normalized.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"song work item {cid} is missing {key}")
    normalized.update(
        {
            "cid": cid,
            "segment_path": str(segment),
            "seg_dur_ms": seg_dur_ms,
            "lane": normalized.get("lane") or normalized.get("discovery_lane"),
        }
    )
    return normalized
