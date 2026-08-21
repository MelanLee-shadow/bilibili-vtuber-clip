#!/usr/bin/env python3
"""Refresh only the sealed 女友感 terminal-review evidence.

There are no candidate, path, text, title, or upload options.  The provider
reviews run before the fixed transaction is allowed to replace any target.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.produce_slice_package import (  # noqa: E402
    _load_term_boundary_surfaces,
    _review_glossary,
    _topic_graph_disabled,
    _topic_graph_expected_sha256,
    _topic_graph_path,
    profile_asset_file,
)
from src.autoslice.producer_text_pipeline import TextPipelineAdapters  # noqa: E402
from src.autoslice.qixi_terminal_evidence_refresh import (  # noqa: E402
    QixiTerminalEvidenceRefreshError,
    apply_projection,
    build_staged_refresh,
    load_authority,
    validate_runtime,
)


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--full-dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def _adapters() -> TextPipelineAdapters:
    # Transcribers are never reached in this terminal-only review lane.
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("terminal evidence refresh must not transcribe")

    return TextPipelineAdapters(
        build_aggregate_transcriber=forbidden,
        build_agy_transcriber=forbidden,
        load_term_boundary_surfaces=_load_term_boundary_surfaces,
        profile_asset_file=profile_asset_file,
        review_glossary=_review_glossary,
        topic_graph_disabled=_topic_graph_disabled,
        topic_graph_path=_topic_graph_path,
        topic_graph_expected_sha256=_topic_graph_expected_sha256,
    )


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    try:
        authority = load_authority(ROOT)
        runtime = validate_runtime(authority)
        if args.plan or not (args.full_dry_run or args.apply):
            print(json.dumps({"status": "PLAN_PASS", "upload_enabled": False}, sort_keys=True))
            return 0
        staged, _result = build_staged_refresh(
            repo_root=ROOT, authority=authority, runtime=runtime, adapters=_adapters()
        )
        outcome = apply_projection(
            authority=authority, before=runtime, after=staged, apply=args.apply
        )
        print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
        return 0
    except QixiTerminalEvidenceRefreshError as exc:
        print(f"QIXI_TERMINAL_EVIDENCE_REFRESH_BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
