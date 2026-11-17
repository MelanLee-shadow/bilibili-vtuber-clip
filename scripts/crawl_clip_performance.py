#!/usr/bin/env python3
"""Daily update of the private per-clip performance records (src/autoslice/clip_performance.py).

Reads the lane's clip directories (what was chosen and how it was packaged) and Bilibili's public pages (the numbers),
labels each clip's draws from its final subtitles through CPA, and writes `<write-dir>/<BV>.json`.  Reads only: nothing
is posted, edited or deleted on Bilibili.  Exit 0 = the records were brought up to date (refused reads are listed and
retried next run); exit 2 = the run could not start (no channel config, unreadable inputs).
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

from src.autoslice import clip_performance as cp
from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL, cpa_call_timeout_seconds
from src.autoslice.llm_client import LlmConfig, build_llm_call
from src.autoslice.timely_term_crawler import BILIBILI_USER_AGENT, BoundedHttpClient

DEFAULT_LLM_COMMAND = f"bash scripts/llm_via_cpa.sh {{prompt_file}} {{completion_file}} '{TOP_CPA_MODEL}' medium 1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, default=None, help="the lane's task directory (its clips/, state/, discover/)")
    parser.add_argument("--write-dir", type=Path, required=True, help="private state directory of the records")
    parser.add_argument("--seed", type=Path, default=None,
                        help="JSON list of older BVs to backfill ({bvid, ...}); a missing file is skipped")
    parser.add_argument("--registry", type=Path, default=None, help="state/streamer_registry.json (guests' uids)")
    parser.add_argument("--medal-budget", type=int, default=400, help="commenter medal walls read per run")
    parser.add_argument("--max-requests", type=int, default=2000, help="anonymous public reads per run")
    parser.add_argument("--no-labels", action="store_true", help="skip the CPA labelling of the draws")
    parser.add_argument("--llm-command", default=DEFAULT_LLM_COMMAND)
    parser.add_argument("--now", default=None, help="ISO time with zone (tests)")
    return parser


def _public(client: BoundedHttpClient, gap_s: float) -> cp.Fetch:
    headers = {"User-Agent": BILIBILI_USER_AGENT, "Referer": "https://www.bilibili.com/"}

    def fetch(url: str) -> dict:
        time.sleep(gap_s)
        return json.loads(client.fetch(url, headers=headers).body)
    return fetch


def _login() -> cp.Fetch:
    from src.autoslice.lane_publish import bili   # the lane's own read-back: the cookie goes into the header only
    return bili.FETCH


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = (dt.datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now
           else dt.datetime.now(dt.timezone.utc).replace(microsecond=0))
    try:
        config = cp.load_config()
        if config is None:
            print("clip-performance: this channel has no clip_performance.json", file=sys.stderr)
            return 2
        seeds = json.loads(args.seed.read_text(encoding="utf-8")) if args.seed and args.seed.is_file() else []
        registry = (json.loads(args.registry.read_text(encoding="utf-8")).get("members") or []
                    if args.registry and args.registry.is_file() else [])
    except (OSError, ValueError) as exc:
        print(f"clip-performance failed: {type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return 2
    readers = cp.Readers(public=_public(BoundedHttpClient(max_requests=args.max_requests, timeout_seconds=20.0), 0.3),
                         login=_login(), medal_budget=args.medal_budget)
    ask = None if args.no_labels else build_llm_call(
        LlmConfig(transport="command", command_template=args.llm_command, timeout_seconds=cpa_call_timeout_seconds()))
    summary = cp.run(task=args.task, out_dir=args.write_dir, readers=readers, ask=ask, now=now, config=config,
                     seeds=seeds, registry=registry)
    for line in summary.problems:
        print(f"clip-performance partial: {line}", file=sys.stderr)
    print(json.dumps({**summary.to_json(), "medal_reads": readers.medal_reads}, ensure_ascii=False, sort_keys=True),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
