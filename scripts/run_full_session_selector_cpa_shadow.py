#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_auto_review_shadow_pipeline import AgyExecutionResult, _parse_srt, run_shadow_pipeline
from src.autoslice.full_session_candidate_selector import select_full_session_candidates
from src.autoslice.term_lexicon import load_discovered_term_lexicon, normalize_text


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
    parser.add_argument("--no-ffmpeg", action="store_true", help="Testing only: skip ffmpeg materialization.")
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    cues = _parse_srt(args.source_srt)
    if not cues:
        raise SystemExit("NO_SOURCE_CUES")
    source_duration_ms = args.source_duration_ms or max(cue.source_end_ms for cue in cues)
    candidates = select_full_session_candidates(cues, max_candidates=max(1, args.max_candidates))
    if not candidates:
        raise SystemExit("NO_FULL_SESSION_CANDIDATES")

    lexicon = load_discovered_term_lexicon(args.source_srt)
    duplicate_corpus = [candidate.anchor.candidate_id for candidate in candidates]
    records: list[dict[str, object]] = []
    selected_summary: dict[str, object] | None = None
    for candidate in candidates[: args.max_candidates]:
        candidate_dir = args.output_dir / candidate.anchor.candidate_id
        cpa_dir = candidate_dir / "cpa"
        cpa_dir.mkdir(parents=True, exist_ok=True)
        request_json = cpa_dir / f"{candidate.anchor.candidate_id}.cpa.request.json"
        response_json = cpa_dir / f"{candidate.anchor.candidate_id}.cpa.response.json"
        normalized_text = normalize_text(candidate.text_preview, lexicon=lexicon)
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
        )
        job = candidate.to_source_context_job(source_duration_ms=source_duration_ms)
        job["room_id"] = args.room_id
        job["cpa_semantic_request_path"] = str(request_json)
        job["cpa_semantic_response_path"] = str(response_json)
        job["duplicate_corpus"] = [item for item in duplicate_corpus if item != candidate.anchor.candidate_id]
        summary = run_shadow_pipeline(
            source_video=args.source_video,
            source_srt=args.source_srt,
            refined_srt=None,
            source_context_job=job,
            source_context_agy_runner=_copy_draft_runner if args.copy_draft_context else None,
            room_id=args.room_id,
            title=normalized_text[:80],
            output_dir=candidate_dir,
            no_upload=True,
            source_context_run_ffmpeg=not args.no_ffmpeg,
        )
        record = summary.get("records", [{}])[0]
        records.append(
            {
                "candidate_id": candidate.anchor.candidate_id,
                "candidate_dir": str(candidate_dir),
                "decision_action": record.get("decision_action"),
                "reason_codes": record.get("reason_codes"),
                "evidence_path": record.get("evidence_path"),
                "source_context_job": record.get("source_context_job"),
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
) -> None:
    completed = subprocess.run(
        [
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
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr or f"CPA_SEMANTIC_REVIEW_FAILED rc={completed.returncode}")


def _copy_draft_runner(media_path: Path, draft_srt_path: Path, output_srt_path: Path) -> AgyExecutionResult:
    output_srt_path.write_text(draft_srt_path.read_text(encoding="utf-8"), encoding="utf-8")
    return AgyExecutionResult(provider="agy", model="copy-draft-test-runner", agy_rc=0, provider_fallback_used=False)


if __name__ == "__main__":
    raise SystemExit(main())
