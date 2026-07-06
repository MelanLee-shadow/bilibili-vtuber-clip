#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.cpa_semantic_qa import (
    CpaSemanticQaRequest,
    CpaSemanticSourceRef,
    CpaTerminologyContext,
    load_request_artifact,
    validate_cpa_semantic_response_payload,
    write_cpa_semantic_request_artifact,
)
from src.autoslice.jingting_chunker import parse_srt_cues

try:  # normal import path (pytest / `python -m`, ROOT on sys.path)
    from scripts.lidousha_glossary_terms import load_glossary_terms
except ImportError:  # run as a top-level script: scripts/ is sys.path[0]
    from lidousha_glossary_terms import load_glossary_terms

SURROUNDING_CONTEXT_WINDOW_MS = 90_000
SURROUNDING_CONTEXT_MAX_CHARS = 1_200


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run scriptable CPA semantic QA and persist strict JSON artifacts.")
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--candidate-text", required=True)
    parser.add_argument("--normalized-text", required=True)
    parser.add_argument("--request-json", type=Path, required=True)
    parser.add_argument("--response-json", type=Path, required=True)
    parser.add_argument("--cpa-command", required=True, help="External command template. Use {request_json} and {response_json} placeholders.")
    parser.add_argument("--room-id", default="22966160")
    parser.add_argument("--source-video", default="")
    parser.add_argument("--source-srt", default="")
    parser.add_argument("--start-ms", type=int, default=0)
    parser.add_argument("--end-ms", type=int, default=1)
    parser.add_argument("--source-cues-path", default="")
    parser.add_argument("--review-evidence-path", default="")
    parser.add_argument("--terminology-evidence-path", action="append", default=[])
    parser.add_argument(
        "--glossary",
        type=Path,
        default=None,
        help="Path to the Li Dousha glossary. Default: auto-discover the repo-vendored glossary. "
        "Its canon proper nouns become applied_terms and its mishearing variants the terminology blacklist.",
    )
    parser.add_argument("--content-type-hint", choices=("", "talk", "song"), default="")
    parser.add_argument("--song-complete", action="store_true")
    parser.add_argument("--lyrics-alignment-ready", action="store_true")
    parser.add_argument(
        "--danmaku-context-json",
        type=Path,
        help="Optional danmaku-context.v1 JSON: real viewer danmaku inside the candidate window.",
    )
    args = parser.parse_args(argv)

    source = CpaSemanticSourceRef(
        video_path=args.source_video or "unknown-source-video",
        srt_path=args.source_srt or "unknown-source-srt",
        start_ms=args.start_ms,
        end_ms=max(args.end_ms, args.start_ms + 1),
        source_cues_path=args.source_cues_path or None,
        review_evidence_path=args.review_evidence_path or None,
    )
    metadata: dict[str, object] = {"runner": "scripts/cpa_semantic_review.py"}
    surrounding = _surrounding_context(args.source_srt, start_ms=args.start_ms, end_ms=args.end_ms)
    if surrounding is not None:
        metadata["surrounding_context"] = surrounding
    if args.danmaku_context_json and args.danmaku_context_json.is_file():
        try:
            metadata["danmaku_context"] = json.loads(args.danmaku_context_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    if args.content_type_hint:
        metadata["content_type_hint"] = args.content_type_hint
        metadata["song_candidate"] = args.content_type_hint == "song"
    if args.song_complete:
        metadata["song_complete"] = True
    if args.lyrics_alignment_ready:
        metadata["lyrics_alignment_ready"] = True
    if args.song_complete and args.lyrics_alignment_ready:
        metadata["full_song_ready"] = True

    # Load the full authoritative terminology from the glossary instead of the
    # historic single hard-coded ("kmx",).  ``load_glossary_terms`` is fail-safe:
    # a missing/broken glossary degrades to ("kmx",) rather than raising, so the
    # terminology gate never crashes the QA run.  canon -> applied_terms (the
    # approved spellings), mishearing variants -> metadata blacklist so the judge
    # can check the normalized text no longer contains 停放熊/沙特琳/一四二/苏丹 …
    glossary_terms = load_glossary_terms(args.glossary)
    applied_terms = glossary_terms.canon or ("kmx",)
    if glossary_terms.mishear_blacklist:
        metadata["terminology_blacklist"] = list(glossary_terms.mishear_blacklist)

    request = CpaSemanticQaRequest(
        candidate_id=args.candidate_id,
        room_id=args.room_id,
        source=source,
        candidate_text=args.candidate_text,
        normalized_text=args.normalized_text,
        response_path=str(args.response_json),
        terminology=CpaTerminologyContext(
            schema_version="lidousha-term-lexicon.v1",
            applied_terms=tuple(applied_terms),
            evidence_paths=tuple(args.terminology_evidence_path),
        ),
        metadata=metadata,
    )
    request = write_cpa_semantic_request_artifact(request, args.request_json)

    command = args.cpa_command.format(request_json=str(args.request_json), response_json=str(args.response_json))
    completed = subprocess.run(shlex.split(command), check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        sys.stderr.write("CPA_COMMAND_FAILED\n")
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        return completed.returncode or 1

    try:
        response_payload = _load_response_payload(args.response_json)
        validation_errors = validate_cpa_semantic_response_payload(
            response_payload,
            request=load_request_artifact(args.request_json),
            response_path=args.response_json,
        )
    except Exception as exc:
        sys.stderr.write(f"CPA_SEMANTIC_QA_INVALID_JSON: {exc}\n")
        return 2
    if validation_errors:
        sys.stderr.write("CPA_SEMANTIC_QA_RESPONSE_INVALID: " + ",".join(validation_errors) + "\n")
        return 2
    print(json.dumps({"status": "ok", "request_json": str(args.request_json), "response_json": str(args.response_json)}, ensure_ascii=False))
    return 0


def _surrounding_context(source_srt: str, *, start_ms: int, end_ms: int) -> dict[str, object] | None:
    """Text right before/after the candidate window, for the viewer-perspective
    context check.  Best-effort: a missing SRT just omits the field (the judge
    then reviews on the candidate text alone, as before)."""

    if not source_srt:
        return None
    path = Path(source_srt)
    if not path.is_file():
        return None
    try:
        cues = parse_srt_cues(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return None
    before_texts = [
        cue.text
        for cue in cues
        if cue.end_ms <= start_ms and cue.end_ms > start_ms - SURROUNDING_CONTEXT_WINDOW_MS
    ]
    after_texts = [
        cue.text
        for cue in cues
        if cue.start_ms >= end_ms and cue.start_ms < end_ms + SURROUNDING_CONTEXT_WINDOW_MS
    ]
    before_text = " ".join(" ".join(text.split()) for text in before_texts)
    after_text = " ".join(" ".join(text.split()) for text in after_texts)
    # Keep the side nearest the window: the tail of "before", the head of "after".
    before_text = before_text[-SURROUNDING_CONTEXT_MAX_CHARS:]
    after_text = after_text[:SURROUNDING_CONTEXT_MAX_CHARS]
    if not before_text and not after_text:
        return None
    return {
        "window_ms": SURROUNDING_CONTEXT_WINDOW_MS,
        "before_text": before_text,
        "after_text": after_text,
    }


def _load_response_payload(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("CPA response root must be an object")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
