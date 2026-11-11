#!/usr/bin/env python3
"""Daily crawl of circle news (events, projects, alliances, collaborations) into a candidate entity graph.

Reads the roster and the owner's dynamics snapshots, searches through the Search Router, lets CPA judge the results,
and writes `<write-root>/knowledge/{nodes,edges}.jsonl` (status=candidate only; format: src/autoslice/knowledge_graph)
plus `<write-root>/crawl_report.json`.  Exit 0 = delivered (partial failures are counted in the report and logged);
exit 2 = the run could not deliver (no search succeeded, CPA answered nothing, the graph did not validate).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL, cpa_call_timeout_seconds
from src.autoslice.llm_client import LlmConfig, build_llm_call
from src.autoslice.manual_lane.work_lookup import WebLookup
from src.autoslice.news_event_crawler import (
    MAX_SEEDS,
    NewsEventError,
    RouterSearch,
    crawl,
)
from src.autoslice.streamer_dynamics import validate_dynamics_snapshot
from src.autoslice.streamer_registry_crawler import validate_snapshot as validate_registry

PROFILE = load_channel_profile(ROOT)
DEFAULT_LLM_COMMAND = (
    "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} "
    f"'{TOP_CPA_MODEL}' medium 1"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True, help="state/streamer_registry.json")
    parser.add_argument("--dynamics", type=Path, required=True, help="state/streamer_dynamics.json")
    parser.add_argument("--write-root", type=Path, required=True, help="runtime graph root (knowledge/ below it)")
    parser.add_argument("--max-seeds", type=int, default=MAX_SEEDS)
    parser.add_argument("--max-runtime-seconds", type=int, default=3600)
    parser.add_argument("--now", help="pin an ISO timestamp")
    parser.add_argument("--llm-command", default=DEFAULT_LLM_COMMAND)
    return parser


def _load_snapshot(path: Path, validate, label: str, notes: list[str]):  # noqa: ANN001, ANN202
    """A validated snapshot, or None with a note: one input missing never stops the other seeds from being searched."""
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        notes.append(f"{label} unavailable: {type(exc).__name__}: {str(exc)[:200]}")
        return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = (
        dt.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    )
    if now.tzinfo is None:
        print("news-event crawler failed: --now must include a timezone", file=sys.stderr)
        return 2
    notes: list[str] = []
    registry = _load_snapshot(args.registry, lambda raw: validate_registry(raw, as_of=None), "registry", notes)
    dynamics = _load_snapshot(args.dynamics, validate_dynamics_snapshot, "dynamics", notes)
    try:
        llm_call = build_llm_call(
            LlmConfig(
                transport="command",
                command_template=args.llm_command,
                timeout_seconds=cpa_call_timeout_seconds(),
            )
        )
        report = crawl(
            owner=PROFILE.display_name,
            repo_root=PROFILE.asset_root,
            runtime_root=args.write_root,
            registry=registry,
            dynamics=dynamics,
            search=RouterSearch(WebLookup(), deadline=time.monotonic() + args.max_runtime_seconds),
            llm_call=llm_call,
            now=now,
            max_seeds=args.max_seeds,
            notes=notes,
        )
    except NewsEventError as exc:
        print(f"news-event crawler failed: {exc}", file=sys.stderr)
        if exc.report is not None:
            print(json.dumps(exc.report.to_json(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - any other failure is reported, never swallowed, with a non-zero exit
        print(f"news-event crawler failed: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return 2
    for line in report.errors:  # partial failures: delivered, but the log says what was missing
        print(f"news-event crawler partial: {line}", file=sys.stderr)
    print(json.dumps({
        "seeds": len(report.seeds), "search": dict(report.search), "cpa": dict(report.cpa),
        "candidates": report.candidates, "new_nodes": len(report.new_nodes), "new_edges": len(report.new_edges),
        "errors": len(report.errors)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
