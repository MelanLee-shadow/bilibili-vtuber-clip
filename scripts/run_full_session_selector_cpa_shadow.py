#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_auto_review_shadow_pipeline import AgyExecutionResult, _parse_srt, run_shadow_pipeline
from src.autoslice.auto_review import DecisionAction
from src.autoslice.boundary_resolver import AnchorCandidate, BoundaryResolution
from src.autoslice.full_session_candidate_selector import (
    FullSessionCandidate,
    select_fallback_session_candidates,
    select_full_session_candidates,
)
from src.autoslice.llm_client import LlmCallError, LlmConfig, build_llm_call
from src.autoslice.danmaku_evidence import danmaku_in_window, find_danmaku_bursts, load_danmaku_xml
from src.autoslice.semantic_candidate_selector import select_semantic_session_candidates
from src.autoslice.song_repair import build_composite_lrc_provider, build_lrclib_lrc_provider, build_netease_lrc_provider
from src.autoslice.subtitle_timing_qa import build_ssh_silero_vad_provider
from src.autoslice.term_lexicon import load_discovered_term_lexicon, normalize_text

VIEWER_CONTEXT_MAX_EXPANSION_MS = 300_000


def _mmss_hint(ms: int) -> str:
    seconds = max(0, ms) // 1000
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _rebase_mmss(mmss: str, offset_ms: int) -> str:
    """Shift a probe-relative MM:SS to clip-relative; times before clip start
    come out negative ('-00:05') so the pairing rule still applies to titles
    the streamer starts reading right as the clip opens."""

    parts = mmss.strip().split(":")
    try:
        seconds = int(parts[-2]) * 60 + int(float(parts[-1])) if len(parts) >= 2 else int(float(parts[0]))
    except (ValueError, IndexError):
        return mmss
    rebased = seconds - offset_ms // 1000
    sign = "-" if rebased < 0 else ""
    rebased = abs(rebased)
    return f"{sign}{rebased // 60:02d}:{rebased % 60:02d}"


def _timeline_value(job: dict, key: str):
    timeline = job.get("timeline")
    return timeline.get(key) if isinstance(timeline, dict) else None


def _int_or_zero(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _write_danmaku_context(path: Path, danmaku_items, start_ms: int, end_ms: int) -> Path | None:
    """Real viewer danmaku inside the candidate window, for the CPA judge —
    the ground truth for 'was this danmaku-triggered and is the trigger in
    the clip'."""

    from src.autoslice.danmaku_evidence import format_danmaku_lines

    in_window = danmaku_in_window(danmaku_items, start_ms, end_ms, max_items=40)
    if not in_window:
        return None
    path.write_text(
        json.dumps(
            {
                "schema_version": "danmaku-context.v1",
                "window": {"start_ms": start_ms, "end_ms": end_ms},
                "count": len(in_window),
                "lines": format_danmaku_lines(in_window, base_ms=start_ms),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Select full-session candidates, run CPA semantic QA, then no-upload shadow review.")
    parser.add_argument("--source-video", type=Path, required=True)
    parser.add_argument("--source-srt", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--room-id", default="22966160")
    parser.add_argument("--source-duration-ms", type=int)
    parser.add_argument("--max-candidates", type=int, default=1)
    parser.add_argument("--cpa-command", required=True)
    parser.add_argument("--copy-draft-context", action="store_true", help="Testing only: copy context draft SRT instead of calling agy.")
    parser.add_argument(
        "--agy-ssh-host",
        help="Run the jingting second-listen (agy refine) on this ssh host (e.g. 'free') instead of a local agy binary.",
    )
    parser.add_argument("--no-ffmpeg", action="store_true", help="Testing only: skip ffmpeg materialization.")
    parser.add_argument("--lrc-provider", choices=("none", "netease", "lrclib", "auto"), default="none", help="External LRC discovery for repair-first song completeness.")
    parser.add_argument("--burn-preview", action="store_true", help="Burn recut subtitles into a shadow preview render.")
    parser.add_argument("--song-hint-llm-command", help="LLM command template ({prompt_file} {completion_file}) for song-name guessing.")
    parser.add_argument("--publish-staging", action="store_true", help="Stage AI title + cover + publish.json draft (upload_enabled always false).")
    parser.add_argument("--title-llm-command", help="LLM command template for title generation.")
    parser.add_argument(
        "--cover-art-direction-llm-command",
        help="LLM command template ({prompt_file} {completion_file}) picking cover art direction; falls back to deterministic persona baseline.",
    )
    parser.add_argument(
        "--semantic-recall-llm-command",
        help="LLM command template ({prompt_file} {completion_file}) for viewer-perspective semantic recall; when set this lane runs before the keyword selectors.",
    )
    parser.add_argument(
        "--danmaku-xml",
        type=Path,
        help="blrec raw danmaku XML for this recording segment (sources/*.xml); enables burst hints for recall, danmaku context for CPA, and on-screen hint lines for jingting.",
    )
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    cues = _parse_srt(args.source_srt)
    if not cues:
        raise SystemExit("NO_SOURCE_CUES")
    source_duration_ms = args.source_duration_ms or max(cue.source_end_ms for cue in cues)

    # Danmaku evidence (blrec raw XML): burst windows steer recall, window
    # text feeds CPA viewer-context, and per-chunk lines feed jingting.
    danmaku_items = []
    danmaku_hints = None
    if args.danmaku_xml and args.danmaku_xml.is_file():
        danmaku_items = load_danmaku_xml(args.danmaku_xml)
        bursts = find_danmaku_bursts(danmaku_items)
        if bursts:
            danmaku_hints = "\n".join(
                f"{_mmss_hint(burst.start_ms)}-{_mmss_hint(burst.end_ms)} (弹幕x{burst.count}): "
                + " / ".join(burst.sample_texts)
                for burst in sorted(bursts, key=lambda b: b.start_ms)
            )
            (args.output_dir / "danmaku_bursts.json").write_text(
                json.dumps(
                    {
                        "schema_version": "danmaku-evidence.v1",
                        "danmaku_xml": str(args.danmaku_xml),
                        "total_danmaku": len(danmaku_items),
                        "bursts": [
                            {
                                "start_ms": burst.start_ms,
                                "end_ms": burst.end_ms,
                                "count": burst.count,
                                "sample_texts": list(burst.sample_texts),
                            }
                            for burst in bursts
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

    # Lane order: semantic recall (viewer-perspective LLM) is the primary way
    # interesting moments are found — keyword lanes stay only as fallbacks so
    # an LLM outage can never mean zero session output.
    candidates: list[FullSessionCandidate] = []
    selector_stage = None
    semantic_diagnostics: dict[str, object] | None = None
    if args.semantic_recall_llm_command:
        try:
            recall_llm = build_llm_call(
                LlmConfig(transport="command", command_template=args.semantic_recall_llm_command, timeout_seconds=600.0)
            )
            candidates, semantic_diagnostics = select_semantic_session_candidates(
                cues,
                llm_call=recall_llm,
                max_candidates=max(1, args.max_candidates),
                danmaku_hints=danmaku_hints,
            )
            selector_stage = "semantic_recall"
        except LlmCallError as exc:
            semantic_diagnostics = {"stage": "semantic_recall", "error": str(exc)}
            candidates = []
    if semantic_diagnostics is not None:
        (args.output_dir / "semantic_recall.json").write_text(
            json.dumps(semantic_diagnostics, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if not candidates:
        candidates = select_full_session_candidates(cues, max_candidates=max(1, args.max_candidates))
        selector_stage = "primary"
    if not candidates:
        # Repair-first: zero candidates is itself a failure of the unattended
        # goal; fall back to performance-run recall so review can decide.
        candidates = select_fallback_session_candidates(cues, max_candidates=max(1, args.max_candidates))
        selector_stage = "fallback_recall"
    if not candidates:
        raise SystemExit("NO_FULL_SESSION_CANDIDATES")

    lexicon = load_discovered_term_lexicon(args.source_srt)
    duplicate_corpus = [candidate.anchor.candidate_id for candidate in candidates]
    records: list[dict[str, object]] = []
    selected_summary: dict[str, object] | None = None
    for original_candidate in candidates[: args.max_candidates]:
        candidate = original_candidate
        candidate_dir = args.output_dir / original_candidate.anchor.candidate_id
        cpa_dir = candidate_dir / "cpa"
        cpa_dir.mkdir(parents=True, exist_ok=True)
        request_json = cpa_dir / f"{candidate.anchor.candidate_id}.cpa.request.json"
        response_json = cpa_dir / f"{candidate.anchor.candidate_id}.cpa.response.json"
        normalized_text = normalize_text(candidate.text_preview, lexicon=lexicon)
        danmaku_context_json = None
        if danmaku_items:
            danmaku_context_json = _write_danmaku_context(
                cpa_dir / f"{candidate.anchor.candidate_id}.danmaku.json",
                danmaku_items,
                candidate.boundary.resolved_start_ms,
                candidate.boundary.resolved_end_ms,
            )
        _run_cpa_script(
            candidate_id=candidate.anchor.candidate_id,
            candidate_text=candidate.text_preview,
            normalized_text=normalized_text,
            request_json=request_json,
            response_json=response_json,
            cpa_command=args.cpa_command,
            room_id=args.room_id,
            source_video=args.source_video,
            source_srt=args.source_srt,
            start_ms=candidate.boundary.resolved_start_ms,
            end_ms=candidate.boundary.resolved_end_ms,
            content_type_hint=candidate.content_type_hint,
            danmaku_context_json=danmaku_context_json,
        )
        # Viewer-perspective retry: when the CPA judge says a viewer could not
        # follow the clip and names how far the missing context sits, expand
        # the window once and re-review.  Still fail-closed: if the expanded
        # window is judged incomplete again, the reason codes stand.
        expansion_record: dict[str, object] | None = None
        expanded = _viewer_context_expanded_candidate(
            candidate, cues, response_json, source_duration_ms=source_duration_ms
        )
        if expanded is not None:
            expansion_record = {
                "original_candidate_id": original_candidate.anchor.candidate_id,
                "expanded_candidate_id": expanded.anchor.candidate_id,
                "original_start_ms": candidate.boundary.resolved_start_ms,
                "original_end_ms": candidate.boundary.resolved_end_ms,
                "expanded_start_ms": expanded.boundary.resolved_start_ms,
                "expanded_end_ms": expanded.boundary.resolved_end_ms,
            }
            candidate = expanded
            request_json = cpa_dir / f"{candidate.anchor.candidate_id}.cpa.request.json"
            response_json = cpa_dir / f"{candidate.anchor.candidate_id}.cpa.response.json"
            normalized_text = normalize_text(candidate.text_preview, lexicon=lexicon)
            danmaku_context_json = None
            if danmaku_items:
                danmaku_context_json = _write_danmaku_context(
                    cpa_dir / f"{candidate.anchor.candidate_id}.danmaku.json",
                    danmaku_items,
                    candidate.boundary.resolved_start_ms,
                    candidate.boundary.resolved_end_ms,
                )
            _run_cpa_script(
                candidate_id=candidate.anchor.candidate_id,
                candidate_text=candidate.text_preview,
                normalized_text=normalized_text,
                request_json=request_json,
                response_json=response_json,
                cpa_command=args.cpa_command,
                room_id=args.room_id,
                source_video=args.source_video,
                source_srt=args.source_srt,
                start_ms=candidate.boundary.resolved_start_ms,
                end_ms=candidate.boundary.resolved_end_ms,
                content_type_hint=candidate.content_type_hint,
                danmaku_context_json=danmaku_context_json,
            )
        job = candidate.to_source_context_job(source_duration_ms=source_duration_ms)
        job["room_id"] = args.room_id
        job["selector_stage"] = selector_stage
        if selector_stage == "semantic_recall" or expansion_record is not None:
            # Semantic windows (LLM recall or viewer-context expansion) carry
            # their own bounds; keyword boundary heuristics become advisory.
            job["boundary_authority"] = "semantic"
        if expansion_record is not None:
            job["viewer_context_expansion"] = expansion_record
        if args.agy_ssh_host and args.copy_draft_context:
            raise SystemExit("--agy-ssh-host and --copy-draft-context are mutually exclusive")
        agy_runner = None
        if args.copy_draft_context:
            agy_runner = _copy_draft_runner
        elif args.agy_ssh_host:
            agy_runner = _build_ssh_agy_runner(
                args.agy_ssh_host,
                danmaku_items=danmaku_items or None,
                context_start_ms=_int_or_zero(_timeline_value(job, "context_start_ms")),
            )
        if lexicon is not None:
            job["glossary_path"] = str(lexicon.path)
            job["glossary_sha256"] = hashlib.sha256(lexicon.path.read_bytes()).hexdigest()
        job["cpa_semantic_request_path"] = str(request_json)
        job["cpa_semantic_response_path"] = str(response_json)
        job["duplicate_corpus"] = [item for item in duplicate_corpus if item != candidate.anchor.candidate_id]
        fresh_transcriber = None
        if args.agy_ssh_host and candidate.content_type_hint == "talk" and not args.copy_draft_context:
            fresh_transcriber = _build_ssh_agy_transcribe_runner(
                args.agy_ssh_host,
                danmaku_items=danmaku_items or None,
                window_start_ms=candidate.boundary.next_start_ms
                if candidate.boundary.next_start_ms is not None
                else candidate.boundary.resolved_start_ms,
                source_video=args.source_video,
            )
        summary = run_shadow_pipeline(
            source_video=args.source_video,
            source_srt=args.source_srt,
            refined_srt=None,
            source_context_job=job,
            source_context_agy_runner=agy_runner,
            fresh_talk_transcriber=fresh_transcriber,
            speech_spans_provider=build_ssh_silero_vad_provider(args.agy_ssh_host) if args.agy_ssh_host else None,
            room_id=args.room_id,
            title=normalized_text[:80],
            output_dir=candidate_dir,
            no_upload=True,
            source_context_run_ffmpeg=not args.no_ffmpeg,
            lrc_provider=(
                build_netease_lrc_provider()
                if args.lrc_provider == "netease"
                else build_lrclib_lrc_provider()
                if args.lrc_provider == "lrclib"
                else build_composite_lrc_provider(build_netease_lrc_provider(), build_lrclib_lrc_provider())
                if args.lrc_provider == "auto"
                else None
            ),
            song_hint_llm_call=build_llm_call(
                LlmConfig(transport="command", command_template=args.song_hint_llm_command, timeout_seconds=180.0)
            )
            if args.song_hint_llm_command
            else None,
            burn_preview=args.burn_preview,
            publish_staging=args.publish_staging,
            title_llm_call=build_llm_call(
                LlmConfig(transport="command", command_template=args.title_llm_command, timeout_seconds=180.0)
            )
            if args.title_llm_command
            else None,
            art_direction_llm_call=build_llm_call(
                LlmConfig(transport="command", command_template=args.cover_art_direction_llm_command, timeout_seconds=180.0)
            )
            if args.cover_art_direction_llm_command
            else None,
        )
        record = summary.get("records", [{}])[0]
        records.append(
            {
                "candidate_id": candidate.anchor.candidate_id,
                "candidate_dir": str(candidate_dir),
                "selector_stage": selector_stage,
                "viewer_context_expansion": expansion_record,
                "decision_action": record.get("decision_action"),
                "reason_codes": record.get("reason_codes"),
                "evidence_path": record.get("evidence_path"),
                # The unattended runner consumes this OUTER summary.  Do not
                # project away the hash-bound recut/title fields from the inner
                # shadow record: doing so makes every proven song look
                # unmaterialized and permanently fail closed.
                "title": record.get("title"),
                "subtitle_source": record.get("subtitle_source"),
                "source_context_job": record.get("source_context_job"),
                "boundary_resolution": record.get("boundary_resolution"),
                "materialized_recut": record.get("materialized_recut"),
                "cpa_request_json": str(request_json),
                "cpa_response_json": str(response_json),
            }
        )
        selected_summary = summary
        if record.get("decision_action") == "AUTO_UPLOAD":
            break

    final_summary = {
        "schema_version": "full-session-selector-cpa-shadow-run.v1",
        "source_video": str(args.source_video),
        "source_srt": str(args.source_srt),
        "room_id": args.room_id,
        "no_upload": True,
        "selector_stage": selector_stage,
        "records": records,
        "last_shadow_summary": selected_summary,
    }
    (args.output_dir / "summary.json").write_text(json.dumps(final_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(final_summary, ensure_ascii=False, indent=2))
    return 0


def _run_cpa_script(
    *,
    candidate_id: str,
    candidate_text: str,
    normalized_text: str,
    request_json: Path,
    response_json: Path,
    cpa_command: str,
    room_id: str,
    source_video: Path,
    source_srt: Path,
    start_ms: int,
    end_ms: int,
    content_type_hint: str,
    danmaku_context_json: Path | None = None,
) -> None:
    argv = [
        sys.executable,
        str(ROOT / "scripts" / "cpa_semantic_review.py"),
        "--candidate-id",
        candidate_id,
        "--candidate-text",
        candidate_text,
        "--normalized-text",
        normalized_text,
        "--request-json",
        str(request_json),
        "--response-json",
        str(response_json),
        "--cpa-command",
        cpa_command,
        "--room-id",
        room_id,
        "--source-video",
        str(source_video),
        "--source-srt",
        str(source_srt),
        "--start-ms",
        str(start_ms),
        "--end-ms",
        str(end_ms),
        "--content-type-hint",
        content_type_hint,
    ]
    if danmaku_context_json is not None:
        argv.extend(["--danmaku-context-json", str(danmaku_context_json)])
    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise SystemExit(completed.stderr or f"CPA_SEMANTIC_REVIEW_FAILED rc={completed.returncode}")


def _viewer_context_expanded_candidate(
    candidate: FullSessionCandidate,
    cues,
    response_json: Path,
    *,
    source_duration_ms: int,
) -> FullSessionCandidate | None:
    """Build the expanded candidate the CPA viewer-context verdict asked for.

    Returns None when no expansion applies: song candidates (their boundary is
    redone from the full source anyway), missing/OK verdicts, zero expansion,
    or an expansion that changes nothing.
    """

    if candidate.content_type_hint == "song":
        return None
    try:
        payload = json.loads(response_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    viewer_context = metadata.get("viewer_context") if isinstance(metadata, dict) else None
    if not isinstance(viewer_context, dict) or viewer_context.get("viewer_context_ok", True):
        return None
    before_ms = _clamped_expand_ms(viewer_context.get("expand_before_ms"))
    after_ms = _clamped_expand_ms(viewer_context.get("expand_after_ms"))
    if before_ms == 0 and after_ms == 0:
        return None

    new_start_ms = max(0, candidate.boundary.resolved_start_ms - before_ms)
    new_end_ms = min(source_duration_ms, candidate.boundary.resolved_end_ms + after_ms)
    ordered = sorted(cues, key=lambda cue: (cue.source_start_ms, cue.source_end_ms, cue.cue_id))
    window = tuple(cue for cue in ordered if cue.source_end_ms > new_start_ms and cue.source_start_ms < new_end_ms)
    if not window:
        return None
    start_ms = window[0].source_start_ms
    end_ms = window[-1].source_end_ms
    if start_ms == candidate.boundary.resolved_start_ms and end_ms == candidate.boundary.resolved_end_ms:
        return None

    lane_prefix = candidate.anchor.candidate_id.split("_", 1)[0]
    anchor = AnchorCandidate(
        candidate_id=f"{lane_prefix}_{start_ms}_{end_ms}_ctxexp",
        anchor_start_ms=start_ms,
        anchor_end_ms=end_ms,
    )
    boundary = BoundaryResolution(
        candidate_id=anchor.candidate_id,
        action=DecisionAction.AUTO_RECUT,
        resolved_start_ms=start_ms,
        resolved_end_ms=end_ms,
        start_boundary_score=candidate.boundary.start_boundary_score,
        end_boundary_score=candidate.boundary.end_boundary_score,
        reason_codes=("VIEWER_CONTEXT_EXPANDED",) + tuple(candidate.boundary.reason_codes),
        next_start_ms=start_ms,
        next_end_ms=end_ms,
    )
    text_preview = " ".join(cue.text.strip() for cue in window if cue.text.strip())[:160]
    return FullSessionCandidate(
        anchor=anchor,
        boundary=boundary,
        cues=window,
        text_preview=text_preview,
        content_type_hint=candidate.content_type_hint,
    )


def _clamped_expand_ms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(min(VIEWER_CONTEXT_MAX_EXPANSION_MS, max(0, int(value))))


def _build_ssh_agy_transcribe_runner(
    host: str,
    *,
    danmaku_items=None,
    window_start_ms: int = 0,
    source_video: Path | None = None,
    screen_text_preroll_ms: int = 10_000,
):
    """Fresh whole-window transcription of a finished talk clip via agy.

    Same proven contract as scripts/transcribe_live_talk_via_agy.sh (Gemini
    3.5 Flash High, faithful colloquial Chinese, accurate per-utterance
    timing), plus the Li Dousha glossary and the clip's danmaku lines as
    on-screen evidence.  Returns the raw SRT text (clip-relative); the
    materializer validates and lifts it onto the source timeline.
    """

    import os
    import shlex
    import time as _time

    from scripts.gemini_slice_jingting import glossary, looks_like_srt, strip_markdown_fence
    from src.autoslice.danmaku_evidence import danmaku_in_window, format_danmaku_lines
    from src.autoslice.source_context_executor import AgyRunnerError

    model = os.environ.get("AGY_TRANSCRIBE_MODEL", "Gemini 3.5 Flash (High)")
    poll_deadline_seconds = 1500
    poll_interval_seconds = 20
    attempts = 2

    def run(cmd: list[str], *, timeout: int = 2400) -> subprocess.CompletedProcess:
        completed = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
        if completed.returncode != 0:
            raise RuntimeError(f"{cmd[0]} failed rc={completed.returncode}: {completed.stderr[-400:]}")
        return completed

    def build_screen_text_prompt(job_dir: str) -> str:
        return f"""Watch input.mp4 in this job directory ({job_dir}).

Task: list ALL readable on-screen text EXCEPT the rolling viewer danmaku.
The streamer is usually watching something (a video, images, a page) and
reading its text aloud — so the MOST important text is the content INSIDE
what she is watching: video title cards, captions/subtitles inside the
embedded video, meme text, image titles. Static UI labels (player controls,
watermarks) matter less but list them too.

Scan DENSELY: check every moment where the visible text changes (a new image,
a new scene inside the embedded video, a title card appearing). Do not just
sample a few frames — text that appears for only a couple of seconds inside
the embedded video must still be captured, ESPECIALLY near the start of the
clip. Record every distinct text once per appearance with the time it first
becomes readable.

Allowed tools: view_file on prompt.md and input.mp4; write_to_file to relative
screen_text.json; view_file on screen_text.json only after writing.
Forbidden: shell, terminal, browser, web, any file outside this directory,
absolute paths.

Write screen_text.json: a JSON array, each item
{{"time": "MM:SS", "text": "exact text as written", "kind": "video_text|image_title|caption|ui|other"}}
where time is when the text becomes readable (clip-relative).
Transcribe the text EXACTLY as written, even if absurd or nonsensical —
absurd parody titles are exactly what we need verbatim.
JSON only, no markdown fences. An empty array is valid if there is none."""

    def build_prompt(duration_hint_s: int, danmaku_block: str, screen_text_block: str) -> str:
        glossary_text = glossary().strip()
        glossary_block = f"\nGlossary and style rules:\n{glossary_text}\n" if glossary_text else ""
        return f"""You are transcribing a short Bilibili VTuber TALK clip (Li Dousha, ~{duration_hint_s}s).

Use only these local files in this job directory:
- input.mp4

Allowed tools:
- view_file on prompt.md and input.mp4
- write_to_file to relative output.srt
- view_file on output.srt only after writing

Forbidden actions:
- No shell, terminal, browser, web, search, or any file outside this job directory.
- Do not write to an absolute path.

Task:
1. Listen to the FULL audio from 00:00 to the end. Do not stop early.
2. Write output.srt: a complete simplified-Chinese transcription of everything the streamer says.
   - One utterance/sentence per cue; keep colloquial wording, particles, and tone words faithfully.
   - Cue timestamps must be as accurate as you can hear them — the cue must start when the words start
     and end when they end. Never stretch a cue over silence or music.
   - Meaningful screams/exclamations (啊——, 好可怕) are content: transcribe them with accurate timing.
   - Pure music/silence gets NO cue.
   - When the streamer says something absurd, punny, or nonsensical (word games,
     parody titles, deliberate mispronunciations), transcribe the absurd words
     VERBATIM as heard — never normalize them to what would make sense given
     the on-screen image or context.
   - READ the on-screen text (rolling danmaku, image captions, UI) and use it to get names, memes,
     and homophones right — only when it matches what you hear.
   - SRT only in that file: index, HH:MM:SS,mmm --> HH:MM:SS,mmm, text. No markdown fences.

TEMPORAL PAIRING RULE (critical): the on-screen text timeline and the danmaku
timeline below are TIME-PAIRED evidence.
- Text visible on screen at time T is a STRONG candidate for the words spoken
  NEAR T (within ~10s) — the streamer constantly reads titles/captions/danmaku
  aloud the moment they appear. If the audio near T sounds like the on-screen
  text at T, the on-screen text IS the correct wording (copy it exactly).
- Conversely, on-screen text or danmaku whose timestamp is FAR from T (more
  than ~20s away) is NOT a candidate for the words at T — do not borrow it.
- Times like -00:05 mean the text appeared shortly BEFORE the clip's first
  frame; it is still a strong candidate for words spoken at the very start
  (she starts reading a title the moment it appears).
{glossary_block}{screen_text_block}{danmaku_block}"""

    def run_agy_job(job_dir: str, media_path: Path, prompt: str, output_name: str, *, stage: str) -> str:
        run(["ssh", host, f"mkdir -p {shlex.quote(job_dir)}"])
        with tempfile.TemporaryDirectory(prefix="fresh_tx_") as tmp:
            prompt_file = Path(tmp) / "prompt.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            run(["scp", "-q", str(media_path), f"{host}:{job_dir}/input.mp4"])
            run(["scp", "-q", str(prompt_file), f"{host}:{job_dir}/prompt.md"])
        short_prompt = (
            f"Open {job_dir}/prompt.md with view_file and follow it exactly. "
            f"Use only {job_dir}/prompt.md, {job_dir}/input.mp4, and {job_dir}/{output_name}. "
            "Do not inspect any other file or directory. Do not use shell or terminal."
        )
        agy_inner = (
            f"/root/.local/bin/agy --sandbox --add-dir {shlex.quote(job_dir)} "
            f"--model {shlex.quote(model)} -p {shlex.quote(short_prompt)} --print-timeout 15m"
        )
        agy_cmd = (
            f"cd {shlex.quote(job_dir)} && script -qec {shlex.quote(agy_inner)} /dev/null "
            f"> {shlex.quote(job_dir)}/agy.stdout 2> {shlex.quote(job_dir)}/agy.stderr; "
            f"echo rc=$? > {shlex.quote(job_dir)}/agy.rc"
        )
        run(["ssh", host, f"nohup bash -c {shlex.quote(agy_cmd)} >/dev/null 2>&1 & echo started"])

        deadline = _time.time() + poll_deadline_seconds
        rc_line = ""
        while _time.time() < deadline:
            probe = subprocess.run(
                ["ssh", host, f"cat {shlex.quote(job_dir)}/agy.rc 2>/dev/null"],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            rc_line = probe.stdout.strip()
            if rc_line:
                break
            _time.sleep(poll_interval_seconds)
        if not rc_line:
            subprocess.run(["ssh", host, f"pkill -f {shlex.quote(job_dir)} || true"], check=False, capture_output=True, timeout=60)
            raise AgyRunnerError("AGY_TIMEOUT", f"{stage} did not finish; see {host}:{job_dir}")
        if rc_line != "rc=0":
            raise AgyRunnerError("AGY_FAILED_RC", f"{stage} failed {rc_line}; see {host}:{job_dir}")
        fetched = subprocess.run(
            ["ssh", host, f"cat {shlex.quote(job_dir)}/{output_name}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return strip_markdown_fence(fetched.stdout) if fetched.returncode == 0 else ""

    def extract_screen_text(media_path: Path, stamp: str) -> list[dict]:
        """On-screen text with timestamps — the time-paired evidence track.

        Extracted with a pre-roll from the SOURCE video when available: the
        streamer reads a title the moment it appears, so the text she speaks
        over at clip start was often visible only BEFORE the clip's first
        frame.  Times are rebased so 00:00 = clip start (pre-roll times are
        negative-ish, clamped to 00:00).  Best-effort: failure degrades to an
        empty track."""

        probe_path = media_path
        probe_offset_ms = 0
        if source_video is not None and Path(source_video).is_file():
            try:
                probe_start_ms = max(0, window_start_ms - screen_text_preroll_ms)
                probe_offset_ms = window_start_ms - probe_start_ms
                probe_path = media_path.with_suffix(".screen_probe.mp4")
                run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-ss",
                        f"{probe_start_ms / 1000:.3f}",
                        "-i",
                        str(source_video),
                        "-t",
                        f"{(probe_offset_ms + 130_000) / 1000:.3f}",
                        "-vf",
                        "scale=1280:-2",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "28",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "96k",
                        str(probe_path),
                    ],
                    timeout=1800,
                )
            except (RuntimeError, subprocess.TimeoutExpired):
                probe_path = media_path
                probe_offset_ms = 0

        job_dir = f"/opt/bilive/jingting_jobs/screentext-{Path(media_path).stem[:28]}-{stamp}"
        try:
            raw = run_agy_job(job_dir, probe_path, build_screen_text_prompt(job_dir), "screen_text.json", stage="screen text extraction")
            payload = json.loads(raw) if raw.strip() else []
            items = []
            for item in payload:
                if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                    continue
                items.append(
                    {
                        "time": _rebase_mmss(str(item.get("time") or ""), probe_offset_ms),
                        "text": str(item.get("text") or ""),
                        "kind": str(item.get("kind") or "other"),
                    }
                )
            media_path.with_suffix(".screen_text.json").write_text(
                json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            return items
        except (AgyRunnerError, RuntimeError, json.JSONDecodeError, OSError):
            return []

    def transcriber(media_path: Path, speech_spans_ms=None) -> str:
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        duration_hint_s = 90
        danmaku_block = ""
        if danmaku_items:
            in_window = danmaku_in_window(danmaku_items, window_start_ms, window_start_ms + 600_000, max_items=60)
            lines = format_danmaku_lines(in_window, base_ms=window_start_ms)
            if lines:
                danmaku_block = (
                    "\nViewer danmaku timeline (mm:ss relative to clip start, rolling on screen; "
                    "time-paired evidence per the rule above):\n" + "\n".join(lines) + "\n"
                )
        screen_text_items = extract_screen_text(media_path, stamp)
        screen_text_block = ""
        if screen_text_items:
            lines = [f"{item['time']} [{item['kind']}] {item['text']}" for item in screen_text_items[:60]]
            screen_text_block = (
                "\nOn-screen text timeline (mm:ss relative to clip start — image titles, captions, UI; "
                "time-paired evidence per the rule above):\n" + "\n".join(lines) + "\n"
            )
        if speech_spans_ms:
            span_lines = ", ".join(
                f"{int(s) // 60000:02d}:{(int(s) // 1000) % 60:02d}-{int(e) // 60000:02d}:{(int(e) // 1000) % 60:02d}"
                for s, e in speech_spans_ms[:40]
            )
            screen_text_block += (
                "\nAcoustic speech detection (VAD) found human speech at these times — every one of these"
                " spans MUST be covered by a cue if any words are audible there (do not skip short"
                " reactions); spans may be incomplete under loud music, so also transcribe speech you hear"
                f" outside them:\n{span_lines}\n"
            )
        prompt = build_prompt(duration_hint_s, danmaku_block, screen_text_block)

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            job_dir = f"/opt/bilive/jingting_jobs/fresh-{Path(media_path).stem[:32]}-{stamp}-a{attempt}"
            try:
                srt_text = run_agy_job(job_dir, media_path, prompt, "output.srt", stage="fresh transcription")
                if not looks_like_srt(srt_text):
                    raise AgyRunnerError("AGY_EMPTY_OUTPUT", f"fresh transcription produced no valid SRT; see {host}:{job_dir}")
                return srt_text
            except (AgyRunnerError, RuntimeError) as exc:
                last_error = exc
        raise last_error

    return transcriber


def _cpa_correct_draft_cues(draft_srt: str, *, danmaku_lines, cpa_llm_call, screen_text_lines=None):
    """Text-only proper-noun/meme correction via CPA (Ivan 2026-07-04).

    The correction is a TEXT task, so it belongs to CPA — the same judge the
    rest of the pipeline uses — not agy.  The LLM NEVER sees or returns
    timestamps: it gets the numbered cue texts, returns corrected texts by cue
    number, and we splice them back onto the ASR timeline.  Timeline
    preservation is therefore structural, not a validation afterthought.
    """

    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.llm_client import LlmCallError, extract_json_object
    from scripts.gemini_slice_jingting import glossary

    # glossary() = term canon + authoritative subtitle_correction_principles.md,
    # so the full rule set is injected from one source (no inline duplication).
    glossary_text = glossary().strip()
    cues = parse_srt_cues(draft_srt)
    if not cues:
        return draft_srt
    numbered = "\n".join(f"[{_asr_ts(cue.start_ms)[:8]}] {index}. {cue.text}" for index, cue in enumerate(cues, start=1))
    danmaku_block = ""
    if danmaku_lines:
        danmaku_block = (
            "\n同时段观众弹幕(mm:ss 主播常读弹幕/接梗,可佐证人名和梗词的正确写法):\n" + "\n".join(danmaku_lines[:60]) + "\n"
        )
    screen_block = ""
    if screen_text_lines:
        screen_block = (
            "\n画面上的文字(mm:ss;来自 superchat 卡片、图片标题、UI 等——主播常照着念,按时间就近配对补正她读出的内容):\n"
            + "\n".join(screen_text_lines[:60]) + "\n"
            "使用规则:①画面文字帮你补正**词表里没有的**词、句子结构、外文;"
            "②但**词表已有的专名/梗名以词表为准**——画面是花体字/艺术字时视觉识别本身会错(例如把'沙豆李'误读成'大小姐姐姐'),"
            "别被画面误读带偏,词表说'沙豆李'就写沙豆李;③忽略 SC 卡片的价格/元信息(如'本段话五毛'、'括号内容删除'),那不是她念的正文。\n"
        )
    prompt = (
        "你在校对李豆沙(B站虚拟主播)直播切片的字幕草稿。草稿文本来自准确的语音识别,时间轴已经对好——"
        "你只负责改字,不要改动条数、顺序、时间。每行草稿前的 [时间] 用于和弹幕/画面文字按时间就近配对。\n"
        "**严格逐条遵守下面《李豆沙字幕校正原则》和术语表**——里面写了最小编辑、语境推测同音字、不臆造地名专名、外来词保留原文、"
        "代词一致(动物→它/性别未知的人→TA/已知→他她)、SC=superchat('谢SC'非'修完')、幻听孤立碎片删除、口语保真不书面化等全部规则,"
        "不要只改专名而漏掉这些类。术语表里的专名写法是硬约束。\n"
        f"\n{glossary_text}\n"
        f"{screen_block}"
        f"{danmaku_block}"
        f"\n字幕草稿(每行:[时间] 编号. 文本):\n{numbered}\n"
        '\n只输出一个 JSON 对象,条数必须和草稿完全一致(要删的幻听条 text 给空串),只改必要的字:'
        '{"cues": [{"n": 1, "text": "修正后文本或空串"}, ...]}'
    )
    try:
        payload = extract_json_object(cpa_llm_call(prompt))
        corrected = {int(item["n"]): str(item["text"]) for item in payload.get("cues", []) if "n" in item and "text" in item}
    except (LlmCallError, ValueError, KeyError, TypeError):
        return draft_srt  # fail-open: accurate ASR draft ships uncorrected
    blocks = []
    out_index = 0
    for index, cue in enumerate(cues, start=1):
        raw = corrected.get(index)
        if raw is not None and raw.strip() == "":
            continue  # CPA flagged a hallucination cue → drop
        text = (raw or "").strip() or cue.text
        out_index += 1
        blocks.append(f"{out_index}\n{_asr_ts(cue.start_ms)} --> {_asr_ts(cue.end_ms)}\n{text}")
    return "\n\n".join(blocks) + "\n" if blocks else draft_srt


def _cpa_reconcile_draft_cues(bcut_srt: str, agy_srt: str, *, danmaku_lines, cpa_llm_call):
    """Reconcile BCUT (timeline authority) vs AGY (heard the audio) per cue —
    CPA is the judge (Ivan 2026-07-04 architecture).

    BCUT is a professional ASR: its text is ALREADY accurate — the ONLY weak
    spot is proper nouns / names / homophones.  So BCUT is the base and is kept
    by default; AGY (Gemini, multimodal, heard the audio + knows the glossary)
    is used ONLY to fix the specific proper-noun/homophone word BCUT misheard,
    NOT to reword BCUT's general phrasing.  CPA sees BOTH texts per cue and:
    keeps BCUT by default, swaps in AGY's spelling only for a proper-noun /
    homophone difference, applies the glossary/pronoun/SC rules, and DROPs
    context-incoherent hallucination cues (a lone song title amid a bedtime
    chat) by returning empty text.  It never adopts AGY's rewording of ordinary
    words / structure — AGY is a targeted name/homophone supplement.

    Timeline stays BCUT's: AGY refine keeps BCUT cue timing (validate_same_timing),
    so the two align by index; the output splices onto the BCUT timestamps.
    """

    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.llm_client import LlmCallError, extract_json_object
    from scripts.gemini_slice_jingting import glossary

    # glossary() = term canon + subtitle_correction_principles.md (single source).
    glossary_text = glossary().strip()
    bcut_cues = parse_srt_cues(bcut_srt)
    agy_cues = parse_srt_cues(agy_srt)
    if not bcut_cues:
        return bcut_srt
    agy_by_index = {i: c.text for i, c in enumerate(agy_cues, start=1)}
    numbered = "\n".join(
        f"[{_asr_ts(c.start_ms)[:8]}] {i}. BCUT: {c.text} | AGY: {agy_by_index.get(i, '(无)')}"
        for i, c in enumerate(bcut_cues, start=1)
    )
    danmaku_block = ""
    if danmaku_lines:
        danmaku_block = "\n同时段弹幕(可佐证人名/梗):\n" + "\n".join(danmaku_lines[:60]) + "\n"
    prompt = (
        "你在给李豆沙(B站虚拟主播)切片定稿字幕。每条 cue 有两个来源:BCUT(专业语音识别,**文本准确度很高**,时间轴准,"
        "唯一弱点是专有名词/人名/同音字)和 AGY(多模态大模型,听了音频、认得术语表,专门补 BCUT 的专名/同音字弱点)。\n"
        "核心原则:**BCUT 是准确基准,默认保留 BCUT 的文本。AGY 只用来补专名/同音字,不要用 AGY 去改 BCUT 的普通措辞。**\n"
        "逐条规则:\n"
        "① 两者一致就用 BCUT。\n"
        "② 不一致时**只看那个不一致的词是不是专有名词或同音字**:若差异恰好是一个人名/专名/同音字,而 BCUT 听错了、AGY 对了"
        "(例如 BCUT'停放熊'→AGY'kmx'、BCUT'再玩'→AGY'再睡'、BCUT'没有修完'→AGY'没有谢完'),就只把那个词换成 AGY 的写法,"
        "句子其余部分保留 BCUT。**AGY 对普通措辞、语气词、句子结构、断句的任何改写一律不采纳**,保留 BCUT——AGY 只补专名/同音字。\n"
        "③ 定稿后再逐条套下面《李豆沙字幕校正原则》和术语表(即使 BCUT/AGY 都没给对):专名归一、SC=superchat('谢SC'非'修完')、"
        "外来词保留原文、代词一致(动物→它/性别未知的人→TA/已知→他她)、同音字按语境、口语保真。\n"
        "④ **幻听丢弃**:若某条 cue 是和上下文完全不搭的孤立碎片(通常是对背景音乐/杂音的幻听,例如一段哄睡对话里突然冒出"
        "'贡丸'、'虫儿飞~'这种歌名/词碎片),把它的 text 设为空字符串 \"\" 表示删除这条。\n"
        f"\n{glossary_text}\n"
        f"{danmaku_block}"
        f"\n字幕(每行:[时间] 编号. BCUT: ... | AGY: ...):\n{numbered}\n"
        '\n只输出一个 JSON 对象,cues 数量和上面完全一致(要删的条 text 给空串):'
        '{"cues": [{"n": 1, "text": "最终文本或空串"}, ...]}'
    )
    try:
        payload = extract_json_object(cpa_llm_call(prompt))
        final = {int(item["n"]): str(item["text"]) for item in payload.get("cues", []) if "n" in item and "text" in item}
    except (LlmCallError, ValueError, KeyError, TypeError):
        # fail-open: prefer AGY refine (it heard the audio) over raw BCUT.
        return agy_srt if agy_cues else bcut_srt
    blocks = []
    out_index = 0
    for index, cue in enumerate(bcut_cues, start=1):
        text = final.get(index, cue.text).strip()
        if not text:
            continue  # CPA flagged a hallucination cue → drop
        out_index += 1
        blocks.append(f"{out_index}\n{_asr_ts(cue.start_ms)} --> {_asr_ts(cue.end_ms)}\n{text}")
    return "\n\n".join(blocks) + "\n" if blocks else bcut_srt


def _cpa_pronoun_ta_pass(srt: str, *, cpa_llm_call):
    """Dedicated whole-clip pronoun pass: 他/她 → TA for unknown-gender people.

    Referent-gender resolution is a DISCOURSE task the general per-cue
    correction/reconcile does poorly (the rule gets buried and 他 is the default,
    so the model leaves it).  This is a single-purpose pass over the WHOLE clip:
    find who each 他/她 refers to, and if the clip never established that person's
    gender (a classmate / friend / kmx mentioned without a gender cue), rewrite
    every 他/她 for them to TA.  Animals/objects are 它 and out of scope.  Text
    only — timeline untouched; fail-open to the input.
    """

    import re

    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.llm_client import extract_json_object

    # Personal-pronoun 他/她 (not 其他 / 他们 / 她们).  Used both to gate and to do
    # the mechanical rewrite once CPA has judged which cues qualify.
    pron = re.compile(r"(?<!其)[他她](?!们)")
    cues = parse_srt_cues(srt)
    if not cues:
        return srt
    candidate_idx = [i for i, c in enumerate(cues, start=1) if pron.search(c.text)]
    if not candidate_idx:
        return srt  # cheap gate: no personal pronoun to resolve

    numbered = "\n".join(f"{i}. {c.text}" for i, c in enumerate(cues, start=1))
    # The MODEL only judges (which cue numbers), the CODE does the 他/她→TA rewrite.
    # A tiny numbers-only output is safer than asking the model to re-emit full
    # cue texts (deterministic rewrite = no risk of the LLM garbling the rest).
    prompt = (
        "你在给李豆沙(B站虚拟主播)切片字幕判断代词。下面是整条切片的完整字幕(带编号),先通读,搞清每个“他/她”指代谁。\n"
        f"候选编号(这些 cue 里有指人的“他/她”):{candidate_idx}\n"
        "从候选里挑出**指代匿名、性别无从判断的人**(例如“我同学/一个朋友/那个人/kmx”这种通篇没名没姓、也没提性别的)的编号。\n"
        "**不要挑**:(a)片里已点明性别的;(b)有名有姓、性别是常识的具体人物(历史人物司马懿/曹操、明星、动漫角色等)。\n"
        f"\n字幕:\n{numbered}\n"
        '\n只输出 JSON(挑出的编号列表,可为空):{"ta_cues": [编号, ...]}'
    )
    # CPA intermittently returns an empty completion; retry before giving up.
    ta_cues = None
    for _attempt in range(3):
        try:
            payload = extract_json_object(cpa_llm_call(prompt))
            ta_cues = {int(n) for n in payload.get("ta_cues", [])}
            break
        except Exception:
            continue
    if not ta_cues:
        return srt  # fail-open: nothing to change, or CPA never returned usable JSON
    blocks = []
    for index, cue in enumerate(cues, start=1):
        text = pron.sub("TA", cue.text) if index in ta_cues else cue.text
        blocks.append(f"{index}\n{_asr_ts(cue.start_ms)} --> {_asr_ts(cue.end_ms)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def _asr_ts(ms: int) -> str:
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _agy_screen_text_lines(host: str, media_path: Path) -> list[str]:
    """Read on-screen text (superchat cards, image titles, UI) off the finished
    clip with agy vision, as time-paired reference for CPA correction.

    This is the ONE thing CPA-with-glossary cannot do: superchats are NOT in the
    blrec danmaku XML (only scrolling danmaku are), so when the streamer reads a
    SC aloud, the correct proper-noun spelling exists only on the SC card in the
    frame.  agy (vision) is the stable extractor; the extracted text feeds CPA
    as a text track.  Best-effort: any failure returns [] (correction falls
    back to glossary + danmaku).
    """

    import shlex
    import time as _time

    from scripts.gemini_slice_jingting import AGY_MODEL, strip_markdown_fence
    from src.autoslice.source_context_executor import AgyRunnerError

    stamp = _time.strftime("%Y%m%d-%H%M%S")
    job_dir = f"/opt/bilive/jingting_jobs/screentext-{Path(media_path).stem[:28]}-{stamp}"
    prompt = (
        f"Watch input.mp4 in this job directory ({job_dir}).\n"
        "List readable on-screen text EXCEPT scrolling viewer danmaku: superchat / 醒目留言 cards "
        "(the paid message boxes the streamer reads aloud), titles/captions inside images or videos she is "
        "viewing, big stylized text, UI labels. Superchat card text matters MOST — she reads it verbatim.\n"
        "Scan densely; capture text that appears only briefly. Transcribe EXACTLY as written, even if absurd.\n"
        "Allowed: view_file on prompt.md and input.mp4; write_to_file to relative screen_text.json.\n"
        "Forbidden: shell/terminal/web/any file outside this directory.\n"
        'Write screen_text.json: a JSON array, each item {"time":"MM:SS","text":"exact text","kind":"superchat|video_text|image_title|caption|ui|other"}. '
        "JSON only, no markdown. Empty array if none."
    )

    def run(cmd, timeout=2400):
        c = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
        if c.returncode != 0:
            raise RuntimeError(f"{cmd[0]} rc={c.returncode}: {c.stderr[-200:]}")
        return c

    try:
        run(["ssh", host, f"mkdir -p {shlex.quote(job_dir)}"])
        with tempfile.TemporaryDirectory(prefix="screentext_") as tmp:
            pf = Path(tmp) / "prompt.md"
            pf.write_text(prompt, encoding="utf-8")
            run(["scp", "-q", str(media_path), f"{host}:{job_dir}/input.mp4"])
            run(["scp", "-q", str(pf), f"{host}:{job_dir}/prompt.md"])
        short = (
            f"Open {job_dir}/prompt.md with view_file and follow it exactly. "
            f"Use only {job_dir}/prompt.md, {job_dir}/input.mp4, and {job_dir}/screen_text.json. "
            "Do not inspect any other file or directory. Do not use shell or terminal."
        )
        inner = (
            f"/root/.local/bin/agy --sandbox --add-dir {shlex.quote(job_dir)} "
            f"--model {shlex.quote(AGY_MODEL)} -p {shlex.quote(short)} --print-timeout 15m"
        )
        agy_cmd = (
            f"cd {shlex.quote(job_dir)} && script -qec {shlex.quote(inner)} /dev/null "
            f"> {shlex.quote(job_dir)}/agy.stdout 2> {shlex.quote(job_dir)}/agy.stderr; echo rc=$? > {shlex.quote(job_dir)}/agy.rc"
        )
        run(["ssh", host, f"nohup bash -c {shlex.quote(agy_cmd)} >/dev/null 2>&1 & echo started"])
        deadline = _time.time() + 1200
        rc_line = ""
        while _time.time() < deadline:
            probe = subprocess.run(["ssh", host, f"cat {shlex.quote(job_dir)}/agy.rc 2>/dev/null"], check=False, capture_output=True, text=True, timeout=120)
            rc_line = probe.stdout.strip()
            if rc_line:
                break
            _time.sleep(20)
        if rc_line != "rc=0":
            return []
        fetched = subprocess.run(["ssh", host, f"cat {shlex.quote(job_dir)}/screen_text.json"], check=False, capture_output=True, text=True, timeout=120)
        raw = strip_markdown_fence(fetched.stdout) if fetched.returncode == 0 else ""
        items = json.loads(raw) if raw.strip() else []
        lines = []
        for it in items:
            if isinstance(it, dict) and str(it.get("text") or "").strip():
                lines.append(f"{it.get('time', '')} [{it.get('kind', 'other')}] {it.get('text')}")
        media_path.with_suffix(".screen_text.json").write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return lines
    except (AgyRunnerError, RuntimeError, json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
        return []


def _build_aggregate_asr_transcriber(
    host: str,
    *,
    danmaku_items=None,
    window_start_ms: int = 0,
    source_video: Path | None = None,
    correct: str = "bcut_agy_cpa",
    screen_text: bool = False,
):
    """Finished-clip subtitle substrate = BCUT aggregate ASR + AGY refine + CPA
    reconcile (Ivan 2026-07-04 3-way architecture).

    The aggregate ASR (bcut primary, jianying backup — `scripts/free_asr_client`)
    owns the TIMELINE and a rough draft text.  BCUT is a general ASR, weak on
    proper nouns / homophones, so AGY (Gemini, multimodal) LISTENS to the clip
    with the glossary and produces high-quality text on the same timeline, and
    CPA reconciles BCUT vs AGY per cue (prefer AGY where it heard a name /
    homophone right; drop hallucinated cues).

    ``correct``:
      "bcut_agy_cpa" (default) — BCUT draft → AGY jingting refine → CPA reconcile.
      "cpa" — BCUT draft → CPA text-only correction (glossary+danmaku, no AGY;
              faster but blind to audio, weaker on far-off proper nouns).
      "agy" — BCUT draft → AGY refine only (no CPA reconcile).
      "none" — raw BCUT draft.
    Fail-open at each stage: a stage failure degrades to the best draft so far.
    """

    import tempfile as _tempfile

    from scripts.free_asr_client import extract_audio_mp3, to_srt, transcribe
    from scripts.gemini_slice_jingting import looks_like_srt
    from src.autoslice.danmaku_evidence import danmaku_in_window, format_danmaku_lines
    from src.autoslice.llm_client import LlmConfig, build_llm_call
    from src.autoslice.source_context_executor import AgyRunnerError

    danmaku_lines = []
    if danmaku_items:
        in_window = danmaku_in_window(danmaku_items, window_start_ms, window_start_ms + 600_000, max_items=60)
        danmaku_lines = format_danmaku_lines(in_window, base_ms=window_start_ms)
    cpa_llm_call = build_llm_call(
        # 600s: an 11-min clip's reconcile prompt (~250 cues × two sources) can
        # legitimately take gpt-5.5(medium) past 180s (2026-07-06 long-clip run).
        LlmConfig(transport="command", command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file}", timeout_seconds=600.0)
    )
    agy_refine_runner = (
        _build_ssh_agy_runner(host, danmaku_items=danmaku_items, context_start_ms=window_start_ms)
        if correct in ("agy", "bcut_agy_cpa")
        else None
    )

    def _agy_refine(media_path, draft_srt):
        """AGY jingting refine on the BCUT draft: same timeline, AGY's text."""
        if agy_refine_runner is None:
            return None
        with _tempfile.TemporaryDirectory(prefix="asr_refine_") as tmp:
            draft_path = Path(tmp) / "draft.srt"
            out_path = Path(tmp) / "out.srt"
            draft_path.write_text(draft_srt if draft_srt.endswith("\n") else draft_srt + "\n", encoding="utf-8")
            try:
                agy_refine_runner(media_path, draft_path, out_path)
                refined = out_path.read_text(encoding="utf-8")
                if looks_like_srt(refined):
                    media_path.with_suffix(".agy_refined.srt").write_text(refined, encoding="utf-8")
                    return refined
            except (AgyRunnerError, RuntimeError):
                pass
        return None

    def transcriber(media_path: Path, speech_spans_ms=None) -> str:
        sound = extract_audio_mp3(Path(media_path))
        result = transcribe(sound, provider="auto", log=lambda *_: None)
        draft_srt = to_srt(result)
        if not looks_like_srt(draft_srt):
            raise AgyRunnerError("ASR_EMPTY_OUTPUT", f"aggregate ASR produced no utterances for {media_path}")
        media_path.with_suffix(".asr_draft.srt").write_text(
            draft_srt if draft_srt.endswith("\n") else draft_srt + "\n", encoding="utf-8"
        )
        if correct == "none":
            return draft_srt
        if correct == "agy":
            corrected = _agy_refine(media_path, draft_srt) or draft_srt
        elif correct == "bcut_agy_cpa":
            # BCUT (timeline+rough) → AGY refine (heard audio, high-quality text)
            # → CPA reconcile (judge BCUT vs AGY, apply rules, drop hallucinations).
            agy_srt = _agy_refine(media_path, draft_srt)
            if agy_srt is None:
                # AGY down → fall back to CPA text-only on the BCUT draft.
                corrected = _cpa_correct_draft_cues(draft_srt, danmaku_lines=danmaku_lines, cpa_llm_call=cpa_llm_call)
            else:
                corrected = _cpa_reconcile_draft_cues(draft_srt, agy_srt, danmaku_lines=danmaku_lines, cpa_llm_call=cpa_llm_call)
        else:
            # correct == "cpa": text-only, enriched with agy screen text when asked.
            screen_text_lines = _agy_screen_text_lines(host, media_path) if screen_text else None
            corrected = _cpa_correct_draft_cues(
                draft_srt, danmaku_lines=danmaku_lines, cpa_llm_call=cpa_llm_call, screen_text_lines=screen_text_lines
            )
        # Dedicated whole-clip pronoun pass (他/她 → TA for unknown-gender people);
        # a discourse task the general correction can't reliably do inline.
        return _cpa_pronoun_ta_pass(corrected, cpa_llm_call=cpa_llm_call)

    return transcriber


def _copy_draft_runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> AgyExecutionResult:
    output_srt_path.write_text(draft_srt_path.read_text(encoding="utf-8"), encoding="utf-8")
    return AgyExecutionResult(provider="agy", model="copy-draft-test-runner", agy_rc=0, provider_fallback_used=False)


def _build_ssh_agy_runner(host: str, *, danmaku_items=None, context_start_ms: int = 0):
    """Chunked jingting second-listen over ssh: agy lives on the remote host.

    gemini-3.5-flash silently returns empty output (rc=0, no file, no stderr)
    on long-video contexts — the 7/2 whole-session job died exactly this way —
    and the documented sweet spot for transcription-accuracy work is ~5 minute
    clips.  So the context is split at cue gaps (jingting_chunker), each chunk
    re-encoded to a small 1280p clip (the proven-good ~50MB input profile),
    refined by one agy call per chunk with the production jingting prompt, and
    the refined texts are merged back onto the untouched draft timeline.

    Fail-closed with distinguishable reason codes: AGY_EMPTY_OUTPUT vs
    AGY_TIMEOUT vs AGY_FAILED_RC — a mislabeled failure sends the follow-up
    fix in the wrong direction.  Exactly one retry per chunk: empty output is
    often transient, but unbounded retries are how free's disk filled up.
    """

    import shlex
    import time as _time

    from scripts.gemini_slice_jingting import (
        AGY_MODEL,
        agy_prompt,
        looks_like_srt,
        strip_markdown_fence,
        validate_same_timing,
    )
    from src.autoslice.jingting_chunker import merge_refined_chunks, plan_jingting_chunks
    from src.autoslice.source_context_executor import AgyRunnerError

    chunk_print_timeout = "15m"
    chunk_poll_deadline_seconds = 1500
    chunk_poll_interval_seconds = 20
    attempts_per_chunk = 2

    def run(cmd: list[str], *, timeout: int = 2400) -> subprocess.CompletedProcess:
        completed = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
        if completed.returncode != 0:
            raise RuntimeError(f"{cmd[0]} failed rc={completed.returncode}: {completed.stderr[-400:]}")
        return completed

    def encode_chunk_clip(media_path: Path, chunk, out_path: Path) -> None:
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{chunk.media_start_ms / 1000:.3f}",
                "-i",
                str(media_path),
                "-t",
                f"{chunk.media_duration_ms / 1000:.3f}",
                "-vf",
                "scale=1280:-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                str(out_path),
            ],
            timeout=1800,
        )

    from src.autoslice.danmaku_evidence import danmaku_in_window, format_danmaku_lines

    def run_chunk_agy(job_dir: str, chunk_clip: Path, chunk_srt_text: str, chunk) -> str:
        # On-screen evidence: the rolling danmaku recorded during this chunk is
        # exactly what the streamer reads aloud/reacts to — first-class hints
        # for names/memes/homophones.  The visual-read instruction itself lives
        # in agy_prompt (Gemini reads the frames; free only relays files).
        chunk_danmaku_lines: list[str] | None = None
        if danmaku_items:
            window_start = context_start_ms + chunk.media_start_ms
            window_end = context_start_ms + chunk.media_end_ms
            in_window = danmaku_in_window(danmaku_items, window_start, window_end, max_items=60)
            if in_window:
                chunk_danmaku_lines = format_danmaku_lines(in_window, base_ms=window_start)
        prompt = agy_prompt(chunk_srt_text, danmaku_lines=chunk_danmaku_lines)
        run(["ssh", host, f"mkdir -p {shlex.quote(job_dir)}"])
        with tempfile.TemporaryDirectory(prefix="ssh_agy_chunk_") as tmp:
            prompt_file = Path(tmp) / "prompt.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            draft_file = Path(tmp) / "draft.srt"
            draft_file.write_text(chunk_srt_text if chunk_srt_text.endswith("\n") else chunk_srt_text + "\n", encoding="utf-8")
            run(["scp", "-q", str(chunk_clip), f"{host}:{job_dir}/input.mp4"])
            run(["scp", "-q", str(draft_file), f"{host}:{job_dir}/draft.srt"])
            run(["scp", "-q", str(prompt_file), f"{host}:{job_dir}/prompt.md"])

        short_prompt = (
            f"Open {job_dir}/prompt.md with view_file and follow it exactly. "
            f"Use only {job_dir}/prompt.md, {job_dir}/input.mp4, "
            f"{job_dir}/draft.srt, and {job_dir}/output.srt. "
            "Do not inspect any other file or directory. Do not use shell or terminal."
        )
        # `script -qec` gives agy a pseudo-TTY: without one, antigravity print
        # mode is documented to drop its stdout entirely (antigravity-cli#76).
        # output.srt stays the authority; stdout is only diagnostics.
        agy_inner = (
            f"/root/.local/bin/agy --sandbox --add-dir {shlex.quote(job_dir)} "
            f"--model {shlex.quote(AGY_MODEL)} -p {shlex.quote(short_prompt)} --print-timeout {chunk_print_timeout}"
        )
        agy_cmd = (
            f"cd {shlex.quote(job_dir)} && script -qec {shlex.quote(agy_inner)} /dev/null "
            f"> {shlex.quote(job_dir)}/agy.stdout 2> {shlex.quote(job_dir)}/agy.stderr; "
            f"echo rc=$? > {shlex.quote(job_dir)}/agy.rc"
        )
        run(["ssh", host, f"nohup bash -c {shlex.quote(agy_cmd)} >/dev/null 2>&1 & echo started"])

        deadline = _time.time() + chunk_poll_deadline_seconds
        rc_line = ""
        while _time.time() < deadline:
            probe = subprocess.run(
                ["ssh", host, f"cat {shlex.quote(job_dir)}/agy.rc 2>/dev/null"],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            rc_line = probe.stdout.strip()
            if rc_line:
                break
            _time.sleep(chunk_poll_interval_seconds)
        if not rc_line:
            subprocess.run(["ssh", host, f"pkill -f {shlex.quote(job_dir)} || true"], check=False, capture_output=True, timeout=60)
            raise AgyRunnerError(
                "AGY_TIMEOUT",
                f"remote agy chunk did not finish within {chunk_poll_deadline_seconds}s; see {host}:{job_dir}",
            )
        if rc_line != "rc=0":
            raise AgyRunnerError("AGY_FAILED_RC", f"remote agy failed {rc_line}; see {host}:{job_dir}/agy.stderr")

        fetched = subprocess.run(
            ["ssh", host, f"cat {shlex.quote(job_dir)}/output.srt"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        corrected = strip_markdown_fence(fetched.stdout) if fetched.returncode == 0 else ""
        if not looks_like_srt(corrected):
            raise AgyRunnerError(
                "AGY_EMPTY_OUTPUT",
                f"remote agy exited rc=0 but produced no valid output.srt; see {host}:{job_dir}",
            )
        validate_same_timing(chunk_srt_text, corrected)
        return corrected

    def runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> AgyExecutionResult:
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        srt_text = Path(draft_srt_path).read_text(encoding="utf-8")
        chunks = plan_jingting_chunks(srt_text)
        if not chunks:
            raise AgyRunnerError("AGY_NO_DRAFT_CUES", f"draft SRT has no parseable cues: {draft_srt_path}")

        refined_pairs: list[tuple[object, str]] = []
        with tempfile.TemporaryDirectory(prefix="ssh_agy_clips_") as clips_tmp:
            for chunk in chunks:
                chunk_clip = Path(clips_tmp) / f"chunk_{chunk.chunk_index:02d}.mp4"
                encode_chunk_clip(media_path, chunk, chunk_clip)
                chunk_srt_text = chunk.chunk_srt_text()
                last_error: Exception | None = None
                for attempt in range(1, attempts_per_chunk + 1):
                    job_dir = (
                        f"/opt/bilive/jingting_jobs/ssh-{Path(media_path).stem}-{stamp}"
                        f"-c{chunk.chunk_index:02d}a{attempt}"
                    )
                    try:
                        refined_pairs.append((chunk, run_chunk_agy(job_dir, chunk_clip, chunk_srt_text, chunk)))
                        last_error = None
                        break
                    except (AgyRunnerError, RuntimeError) as exc:
                        last_error = exc
                if last_error is not None:
                    raise last_error

        merged = merge_refined_chunks(srt_text, refined_pairs)
        validate_same_timing(srt_text, merged)
        Path(output_srt_path).write_text(merged if merged.endswith("\n") else merged + "\n", encoding="utf-8")
        return AgyExecutionResult(
            provider="agy",
            model=AGY_MODEL,
            agy_rc=0,
            provider_fallback_used=False,
            provider_request_id=f"{host}:jingting-chunked:{stamp}:{len(chunks)}chunks",
        )

    return runner


if __name__ == "__main__":
    raise SystemExit(main())
