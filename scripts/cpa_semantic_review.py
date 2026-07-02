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
    args = parser.parse_args(argv)

    source = CpaSemanticSourceRef(
        video_path=args.source_video or "unknown-source-video",
        srt_path=args.source_srt or "unknown-source-srt",
        start_ms=args.start_ms,
        end_ms=max(args.end_ms, args.start_ms + 1),
        source_cues_path=args.source_cues_path or None,
        review_evidence_path=args.review_evidence_path or None,
    )
    request = CpaSemanticQaRequest(
        candidate_id=args.candidate_id,
        room_id=args.room_id,
        source=source,
        candidate_text=args.candidate_text,
        normalized_text=args.normalized_text,
        response_path=str(args.response_json),
        terminology=CpaTerminologyContext(
            schema_version="lidousha-term-lexicon.v1",
            applied_terms=("kmx",),
            evidence_paths=tuple(args.terminology_evidence_path),
        ),
        metadata={"runner": "scripts/cpa_semantic_review.py"},
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


def _load_response_payload(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("CPA response root must be an object")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
