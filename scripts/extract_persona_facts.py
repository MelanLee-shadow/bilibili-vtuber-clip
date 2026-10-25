#!/usr/bin/env python3
"""Extract/screen a sealed bundle into a dedicated, offline review directory.

No provider is selected implicitly. --proposals-json replays a fake extractor;
--extractor module:function injects a callable (for example the CPA adapter).
The bundle contains only sealed subtitle and evidence paths, never title/hook/
persona text. Knowledge assets and the source out/state trees remain read-only.
"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.autoslice import persona_facts as pf
from src.autoslice.persona_fact_extraction import (
    Extractor, ScreeningSources, document, integer, run_extraction, text,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path,
                        help="persona-extraction-input.v1 JSON with sealed and sources fields")
    parser.add_argument("--ledger", required=True, type=Path, help="Read-only B1 ledger")
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path, help="Dedicated review output, outside input trees")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--proposals-json", type=Path, help="Saved fake extractor output: array of ProposedFact")
    group.add_argument("--extractor", help="Injected module:callable; no built-in provider call")
    parser.add_argument("--extractor-id", default="saved-proposals")
    parser.add_argument("--extractor-version", default="v1")
    args = parser.parse_args(argv)
    try:
        payload = document(args.bundle)
        if set(payload) != {"schema_version", "sealed", "sources"} or payload["schema_version"] != "persona-extraction-input.v1":
            raise ValueError("Invalid extraction bundle fields/schema")
        raw_sealed, raw_sources = payload["sealed"], payload["sources"]
        if not isinstance(raw_sealed, dict) or not isinstance(raw_sources, dict):
            raise ValueError("Invalid sealed/sources object")
        sealed_keys = {"candidate_id", "recording_basename", "recording_date", "final_srt_path", "final_srt_sha256",
            "final_review_audit_path", "final_review_audit_sha256", "speaker_attribution_path",
            "terminal_adjudication_path", "entity_projection_path"}
        source_keys = {"session_relation_ledger", "publication_registry", "read_aloud_candidates", "media_context",
                       "source_offset_ms", "current_source_bindings"}
        if set(raw_sealed) != sealed_keys or set(raw_sources) - source_keys:
            raise ValueError("Bundle contains non-whitelisted inputs")
        def path(value: pf.JSONValue) -> Path | None:
            if value is None:
                return None
            target = Path(text(value))
            return (args.bundle.parent / target).resolve()
        srt_path = path(raw_sealed["final_srt_path"])
        audit_path = path(raw_sealed["final_review_audit_path"])
        if srt_path is None or audit_path is None:
            raise ValueError("Missing sealed paths")
        sealed = pf.SealedFinalSubtitle(text(raw_sealed["candidate_id"]), text(raw_sealed["recording_basename"]),
            text(raw_sealed["recording_date"]), srt_path, text(raw_sealed["final_srt_sha256"]), audit_path,
            text(raw_sealed["final_review_audit_sha256"]), path(raw_sealed["speaker_attribution_path"]),
            path(raw_sealed["terminal_adjudication_path"]), path(raw_sealed["entity_projection_path"]))
        sources = ScreeningSources(path(raw_sources.get("session_relation_ledger")),
            path(raw_sources.get("publication_registry")), path(raw_sources.get("read_aloud_candidates")),
            path(raw_sources.get("media_context")), integer(raw_sources.get("source_offset_ms", 0)),
            path(raw_sources.get("current_source_bindings")))
        ledger = pf.load_persona_fact_ledger(args.ledger, profile_id=args.profile_id)
        for source in (args.ledger, args.bundle, args.proposals_json):
            if source is not None and (source.resolve() == args.output_dir.resolve()
                                      or args.output_dir.resolve() in source.resolve().parents):
                raise ValueError("Output directory overlaps read-only command inputs")
        if args.proposals_json:
            def fake(_sealed: pf.SealedFinalSubtitle) -> tuple[pf.ProposedFact, ...]:
                value: pf.JSONValue = json.loads(args.proposals_json.read_bytes())
                return cast(tuple[pf.ProposedFact, ...], pf._decode(value, tuple[pf.ProposedFact, ...]))
            extractor: Extractor = fake
        else:
            module, separator, name = args.extractor.partition(":")
            if not separator:
                raise ValueError("Extractor must be module:callable")
            injected = getattr(importlib.import_module(module), name)
            if not callable(injected):
                raise ValueError("Extractor is not callable")
            extractor = cast(Extractor, injected)
        batch = run_extraction(sealed=sealed, ledger=ledger, sources=sources, extractor=extractor,
            output_dir=args.output_dir, extractor_id=args.extractor_id, extractor_version=args.extractor_version)
        print(batch)
        return 0
    except (ValueError, OSError, TypeError) as exc:
        print(f"PERSONA_EXTRACTION_INPUT_INVALID: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
