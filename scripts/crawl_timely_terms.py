#!/usr/bin/env python3
"""Build a bounded source-backed timely_terms snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.timely_term_crawler import (  # noqa: E402
    BoundedHttpClient,
    CrawlError,
    CrawlWindow,
    HttpCache,
    SeedSnapshotAdapter,
    crawl,
    load_source_config,
    snapshot_json,
    write_snapshot_atomically,
)


def _parse_now(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--now must be an ISO timestamp with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--now must include a timezone")
    return parsed.replace(microsecond=0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", help="pin an ISO timestamp for replay/idempotent tests")
    parser.add_argument("--lookback-months", type=int, default=9)
    parser.add_argument("--lookahead-months", type=int, default=6)
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "assets/lidousha/timely_term_sources.json",
    )
    parser.add_argument(
        "--seed",
        type=Path,
        default=ROOT / "assets/lidousha/timely_term_seeds.json",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / ".cache/timely-term-crawler",
    )
    parser.add_argument("--cache-ttl-hours", type=float, default=18.0)
    parser.add_argument("--max-requests", type=int, default=8)
    parser.add_argument("--max-response-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--max-terms", type=int, default=240)
    parser.add_argument("--offline", action="store_true", help="read cache only")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print snapshot; do not publish it")
    mode.add_argument("--write", type=Path, help="atomically replace a validated snapshot")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        now = _parse_now(args.now)
        window = CrawlWindow.around(
            now.date(),
            lookback_months=args.lookback_months,
            lookahead_months=args.lookahead_months,
        )
        anilist, manga, bangumi, rss = load_source_config(args.config)
        cache = HttpCache(args.cache_dir, max_body_bytes=args.max_response_bytes)
        client = BoundedHttpClient(
            max_requests=args.max_requests,
            max_response_bytes=args.max_response_bytes,
            timeout_seconds=args.timeout_seconds,
            cache=cache,
            cache_ttl=dt.timedelta(hours=args.cache_ttl_hours),
            offline=args.offline,
            now=now,
        )
        adapters = [SeedSnapshotAdapter(args.seed), anilist, manga, bangumi, *rss]
        result = crawl(
            client=client,
            window=window,
            generated_at=now,
            adapters=adapters,
            max_terms=args.max_terms,
        )
        summary = {
            "window": {"start": window.start.isoformat(), "end": window.end.isoformat()},
            "terms": len(result.snapshot["terms"]),
            "adapter_counts": result.adapter_counts,
            "adapter_errors": result.errors,
            "network_requests": result.network_requests,
            "cache_hits": result.cache_hits,
            "stale_cache_hits": result.stale_cache_hits,
        }
        if args.write:
            digest, changed = write_snapshot_atomically(result.snapshot, args.write)
            summary.update({"destination": str(args.write), "sha256": digest, "changed": changed})
        else:
            sys.stdout.write(snapshot_json(result.snapshot))
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 0 if not result.errors else 1
    except (CrawlError, OSError, ValueError) as exc:
        print(f"timely-term crawler failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
