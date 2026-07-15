#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_auto_review_shadow_pipeline import _parse_srt, run_shadow_pipeline
from src.autoslice.channel_profile import load_channel_profile
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
from src.autoslice.song_repair import (
    build_composite_lrc_provider,
    build_kugou_lrc_provider,
    build_lrclib_lrc_provider,
    build_netease_lrc_provider,
)
from src.autoslice.subtitle_timing_qa import build_ssh_silero_vad_provider
from src.autoslice.term_lexicon import load_discovered_term_lexicon, normalize_text
from src.autoslice.full_session_transcription import (
    _build_ssh_agy_transcribe_runner as _build_ssh_agy_transcribe_runner,
    _cpa_correct_draft_cues as _cpa_correct_draft_cues,
    _cpa_reconcile_draft_cues as _cpa_reconcile_draft_cues,
    _cpa_pronoun_ta_pass as _cpa_pronoun_ta_pass,
    _asr_ts as _asr_ts,
    _agy_screen_text_lines as _agy_screen_text_lines,
    _build_aggregate_asr_transcriber as _build_aggregate_asr_transcriber,
    _copy_draft_runner as _copy_draft_runner,
    _build_ssh_agy_runner as _build_ssh_agy_runner,
)

CHANNEL_PROFILE = load_channel_profile(ROOT)


def profile_asset_file(key: str) -> Path:
    return CHANNEL_PROFILE.asset_file(key, repo_root=ROOT)


def _topic_graph_disabled() -> bool:
    return (
        os.environ.get("AUTOSLICE_DISABLE_TOPIC_ENTITY_GRAPH") == "1"
        or os.environ.get("LIDOUSHA_DISABLE_TOPIC_ENTITY_GRAPH") == "1"
    )


def _topic_graph_path() -> Path:
    configured = (
        os.environ.get("AUTOSLICE_TOPIC_ENTITY_GRAPH")
        or os.environ.get("LIDOUSHA_TOPIC_ENTITY_GRAPH")
    )
    return Path(configured) if configured else profile_asset_file("topic_entity_graph")


def _topic_graph_expected_sha256() -> str:
    return (
        os.environ.get("AUTOSLICE_TOPIC_ENTITY_GRAPH_SHA256")
        or os.environ.get("LIDOUSHA_TOPIC_ENTITY_GRAPH_SHA256")
        or ""
    )

VIEWER_CONTEXT_MAX_EXPANSION_MS = 300_000


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_host_vocal_prover(
    *,
    python_path: Path,
    reference_profile: Path,
    reference_dir: Path,
    model_dir: Path,
):
    """Run the pinned CAM++ verifier out-of-process in its dedicated venv."""

    def prove(source_media, candidate_id, _boundary, alignment, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        report_value = alignment.get("alignment_report_path")
        if not isinstance(report_value, str) or not report_value:
            return {
                "status": "ERROR",
                "decision": "UNKNOWN",
                "reason_code": "SONG_HOST_VOCAL_PROOF_INVALID",
                "error": "lyrics alignment report path is missing",
            }
        proof_path = output_dir / f"{candidate_id}.host-vocal-proof.json"
        completed = subprocess.run(
            [
                str(python_path),
                "-m",
                "src.autoslice.host_vocal_proof",
                "--source-media",
                str(source_media),
                "--candidate-id",
                candidate_id,
                "--lyrics-alignment-report",
                report_value,
                "--reference-profile",
                str(reference_profile),
                "--reference-dir",
                str(reference_dir),
                "--model-dir",
                str(model_dir),
                "--output",
                str(proof_path),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if completed.returncode not in {0, 3} or not proof_path.is_file():
            return {
                "status": "ERROR",
                "decision": "UNKNOWN",
                "reason_code": "SONG_HOST_VOCAL_VERIFIER_UNAVAILABLE",
                "error": (completed.stderr or completed.stdout)[-1000:],
                "verifier_rc": completed.returncode,
            }
        try:
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {
                "status": "ERROR",
                "decision": "UNKNOWN",
                "reason_code": "SONG_HOST_VOCAL_PROOF_INVALID",
                "error": f"invalid verifier output: {exc}",
            }
        status = str(proof.get("status") or "BLOCKED")
        decision = str(proof.get("decision") or "UNKNOWN")
        return {
            "status": status,
            "decision": decision,
            "reason_code": (
                None
                if status == "READY"
                and decision == CHANNEL_PROFILE.decision("host_vocal_present")
                else CHANNEL_PROFILE.decision("host_not_singing_reason")
                if decision == CHANNEL_PROFILE.decision("host_vocal_absent")
                else "SONG_HOST_VOCAL_UNPROVEN"
            ),
            "proof_path": str(proof_path),
            "proof_sha256": "sha256:" + _sha256_file(proof_path),
            "source_media_path": str(source_media),
            "alignment_report_path": report_value,
            "profile_path": str(reference_profile),
        }

    return prove


def _seeded_song_candidate(
    cues,
    *,
    candidate_id: str,
    anchor_start_ms: int,
    anchor_end_ms: int,
    source_duration_ms: int,
) -> FullSessionCandidate:
    """Materialize the song anchor supplied by the unattended runner.

    A full-source proof retry must keep reviewing the song that triggered the
    retry.  Re-running top-1 semantic recall across the expanded window can
    otherwise select surrounding talk instead (the 2026-07-09 ``芽吹くとき``
    retry selected the post-song good-night chat).  The seed is only a recall
    anchor; LRC/audio proof still decides whether a full song may ship.
    """

    if not (0 <= anchor_start_ms < anchor_end_ms <= source_duration_ms):
        raise ValueError(
            "seeded song anchor must satisfy "
            f"0 <= start < end <= source duration ({anchor_start_ms}, {anchor_end_ms}, {source_duration_ms})"
        )
    window = tuple(
        cue
        for cue in sorted(cues, key=lambda item: (item.source_start_ms, item.source_end_ms, item.cue_id))
        if cue.source_end_ms > anchor_start_ms and cue.source_start_ms < anchor_end_ms
    )
    if not window:
        raise ValueError("seeded song anchor overlaps no source cues")
    anchor = AnchorCandidate(
        candidate_id=candidate_id,
        anchor_start_ms=anchor_start_ms,
        anchor_end_ms=anchor_end_ms,
    )
    boundary = BoundaryResolution(
        candidate_id=candidate_id,
        action=DecisionAction.AUTO_RECUT,
        resolved_start_ms=anchor_start_ms,
        resolved_end_ms=anchor_end_ms,
        start_boundary_score=0.0,
        end_boundary_score=0.0,
        reason_codes=("SEEDED_SONG_ANCHOR", "FULL_SOURCE_SONG_PROOF_REQUIRED"),
        next_start_ms=anchor_start_ms,
        next_end_ms=anchor_end_ms,
    )
    return FullSessionCandidate(
        anchor=anchor,
        boundary=boundary,
        cues=window,
        text_preview=" ".join(cue.text.strip() for cue in window if cue.text.strip())[:160],
        content_type_hint="song",
    )


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select full-session candidates, run CPA semantic QA, then no-upload shadow review."
    )
    parser.add_argument("--source-video", type=Path, required=True)
    parser.add_argument("--source-srt", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--room-id", default=CHANNEL_PROFILE.room_id)
    parser.add_argument("--source-duration-ms", type=int)
    parser.add_argument("--seed-song-candidate-id")
    parser.add_argument("--seed-song-anchor-start-ms", type=int)
    parser.add_argument("--seed-song-anchor-end-ms", type=int)
    parser.add_argument("--max-candidates", type=int, default=1)
    parser.add_argument("--cpa-command", required=True)
    parser.add_argument(
        "--copy-draft-context",
        action="store_true",
        help="Testing only: copy context draft SRT instead of calling agy.",
    )
    parser.add_argument(
        "--agy-ssh-host",
        help="Run the jingting second-listen (agy refine) on this ssh host (e.g. 'free') instead of a local agy binary.",
    )
    parser.add_argument(
        "--no-ffmpeg", action="store_true", help="Testing only: skip ffmpeg materialization."
    )
    parser.add_argument(
        "--lrc-provider",
        choices=("none", "netease", "lrclib", "kugou", "auto"),
        default="none",
        help="External LRC discovery for repair-first song completeness.",
    )
    parser.add_argument(
        "--agy-audio-lrc-align",
        action="store_true",
        help="Seeded full-song retry only: align current audio against a uniquely identified LRC when ASR is sparse.",
    )
    parser.add_argument(
        "--host-vocal-python", type=Path, help="Python executable for the pinned CAM++ host-vocal verifier."
    )
    parser.add_argument(
        "--host-vocal-reference-profile", type=Path, help="Versioned voiceprint threshold/hash profile."
    )
    parser.add_argument(
        "--host-vocal-reference-dir",
        type=Path,
        help=f"Private runtime directory containing {CHANNEL_PROFILE.display_name} enrollment WAVs.",
    )
    parser.add_argument("--host-vocal-model-dir", type=Path, help="Pinned local CAM++ model directory.")
    parser.add_argument(
        "--burn-preview", action="store_true", help="Burn recut subtitles into a shadow preview render."
    )
    parser.add_argument(
        "--branding-intro-manifest",
        type=Path,
        help="Committed branding intro manifest; accepted for compatibility but ignored by the song lane.",
    )
    parser.add_argument(
        "--song-hint-llm-command",
        help="LLM command template ({prompt_file} {completion_file}) for song-name guessing.",
    )
    parser.add_argument(
        "--song-lrc-query",
        action="append",
        default=[],
        help="Known title/artist/lyric query to try before ASR-derived LRC searches (repeatable).",
    )
    parser.add_argument(
        "--publish-staging", action="store_true", help="Stage AI title + cover + publish.json draft (upload_enabled always false)."
    )
    parser.add_argument("--title-llm-command", help="LLM command template for title generation.")
    parser.add_argument(
        "--cover-art-direction-llm-command",
        help="LLM command template ({prompt_file} {completion_file}) picking cover art direction.",
    )
    parser.add_argument(
        "--semantic-recall-llm-command",
        help="LLM command template ({prompt_file} {completion_file}) for viewer-perspective semantic recall.",
    )
    parser.add_argument(
        "--danmaku-xml",
        type=Path,
        help="blrec raw danmaku XML for this recording segment; enables recall and review evidence.",
    )
    return parser


def _review_selected_candidates(
    *,
    args: argparse.Namespace,
    candidates: list[FullSessionCandidate],
    cues,
    source_duration_ms: int,
    selector_stage: str | None,
    danmaku_items,
    host_vocal_prover,
    branding_intro,
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
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
        if args.agy_audio_lrc_align:
            from src.autoslice.agy_lrc_alignment import run_agy_audio_lrc_alignment

            audio_lrc_aligner = run_agy_audio_lrc_alignment
        else:
            audio_lrc_aligner = None
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
                else build_kugou_lrc_provider()
                if args.lrc_provider == "kugou"
                else build_composite_lrc_provider(
                    build_netease_lrc_provider(),
                    build_lrclib_lrc_provider(),
                    build_kugou_lrc_provider(),
                )
                if args.lrc_provider == "auto"
                else None
            ),
            song_hint_llm_call=build_llm_call(
                LlmConfig(transport="command", command_template=args.song_hint_llm_command, timeout_seconds=180.0)
            )
            if args.song_hint_llm_command
            else None,
            song_lrc_queries=tuple(args.song_lrc_query),
            audio_lrc_aligner=audio_lrc_aligner,
            host_vocal_prover=host_vocal_prover,
            burn_preview=args.burn_preview,
            branding_intro=branding_intro,
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
    return records, selected_summary

def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Ivan 2026-07-14: 歌切一律不加片头，直接进歌 —— the branding intro is a
    # talk-lane mandate only. The manifest argument stays accepted for CLI
    # compatibility but is intentionally ignored in this song lane.
    branding_intro = None
    if args.branding_intro_manifest is not None:
        print("branding intro manifest ignored: songs ship without the intro (Ivan 2026-07-14)")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    cues = _parse_srt(args.source_srt)
    if not cues:
        raise SystemExit("NO_SOURCE_CUES")
    source_duration_ms = args.source_duration_ms or max(cue.source_end_ms for cue in cues)

    seed_values = (
        args.seed_song_candidate_id,
        args.seed_song_anchor_start_ms,
        args.seed_song_anchor_end_ms,
    )
    if any(value is not None for value in seed_values) and not all(value is not None for value in seed_values):
        raise SystemExit("seeded song arguments must be supplied together")
    if args.agy_audio_lrc_align and not all(value is not None for value in seed_values):
        raise SystemExit("--agy-audio-lrc-align requires a seeded full-song anchor")
    host_vocal_values = (
        args.host_vocal_python,
        args.host_vocal_reference_profile,
        args.host_vocal_reference_dir,
        args.host_vocal_model_dir,
    )
    if any(value is not None for value in host_vocal_values) and not all(value is not None for value in host_vocal_values):
        raise SystemExit("host-vocal verifier arguments must be supplied together")
    host_vocal_prover = (
        _build_host_vocal_prover(
            python_path=args.host_vocal_python,
            reference_profile=args.host_vocal_reference_profile,
            reference_dir=args.host_vocal_reference_dir,
            model_dir=args.host_vocal_model_dir,
        )
        if all(value is not None for value in host_vocal_values)
        else None
    )

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
    if all(value is not None for value in seed_values):
        try:
            candidates = [
                _seeded_song_candidate(
                    cues,
                    candidate_id=str(args.seed_song_candidate_id),
                    anchor_start_ms=int(args.seed_song_anchor_start_ms),
                    anchor_end_ms=int(args.seed_song_anchor_end_ms),
                    source_duration_ms=source_duration_ms,
                )
            ]
        except ValueError as exc:
            raise SystemExit(f"INVALID_SEEDED_SONG_ANCHOR: {exc}") from exc
        selector_stage = "seeded_song_anchor"
    elif args.semantic_recall_llm_command:
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

    records, selected_summary = _review_selected_candidates(
        args=args,
        candidates=candidates,
        cues=cues,
        source_duration_ms=source_duration_ms,
        selector_stage=selector_stage,
        danmaku_items=danmaku_items,
        host_vocal_prover=host_vocal_prover,
        branding_intro=branding_intro,
    )

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


if __name__ == "__main__":
    raise SystemExit(main())
