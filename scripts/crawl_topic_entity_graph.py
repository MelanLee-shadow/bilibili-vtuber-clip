#!/usr/bin/env python3
"""Build a bounded topic -> anime work -> Chinese character-name graph."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gemini_slice_jingting import load_validated_timely_terms_snapshot  # noqa: E402
from src.autoslice.timely_term_crawler import BoundedHttpClient, CrawlError, HttpCache  # noqa: E402
from src.autoslice.topic_entity_crawler import (  # noqa: E402
    crawl_topic_entity_graph,
    graph_json,
    write_graph_atomically,
)


def _now(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone")
    return parsed.replace(microsecond=0)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--now", help="pin an ISO timestamp for replay")
    result.add_argument(
        "--timely-terms",
        type=Path,
        default=ROOT / "assets/lidousha/timely_terms.json",
    )
    result.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / ".cache/topic-entity-crawler",
    )
    result.add_argument("--cache-ttl-hours", type=float, default=24.0)
    result.add_argument("--max-requests", type=int, default=80)
    result.add_argument("--max-response-bytes", type=int, default=4 * 1024 * 1024)
    result.add_argument("--timeout-seconds", type=float, default=20.0)
    result.add_argument("--max-topics", type=int, default=16)
    result.add_argument("--max-queries", type=int, default=40)
    result.add_argument("--max-works-per-topic", type=int, default=3)
    result.add_argument("--max-entities-per-work", type=int, default=16)
    result.add_argument("--offline", action="store_true")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--write", type=Path)
    return result


def write_complete_graph(result, destination: Path) -> tuple[str, bool]:
    """Keep the last good runtime graph when any enrichment request failed."""

    if result.diagnostics:
        raise CrawlError(
            "refusing to replace the last good graph with a partial crawl: "
            + "; ".join(result.diagnostics[:4])
        )
    return write_graph_atomically(result.graph, destination)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        now = _now(args.now)
        timely_terms_sha256 = hashlib.sha256(args.timely_terms.read_bytes()).hexdigest()
        snapshot = load_validated_timely_terms_snapshot(args.timely_terms)
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
        result = crawl_topic_entity_graph(
            client=client,
            timely_snapshot=snapshot,
            input_timely_terms_sha256=timely_terms_sha256,
            generated_at=now,
            max_topics=args.max_topics,
            max_queries=args.max_queries,
            max_works_per_topic=args.max_works_per_topic,
            max_entities_per_work=args.max_entities_per_work,
        )
        summary = {
            "topics": len(result.graph["topics"]),
            "works": result.work_count,
            "entities": result.entity_count,
            "queries": result.query_count,
            "network_requests": client.requests_made,
            "cache_hits": client.cache_hits,
            "stale_cache_hits": client.stale_hits,
            "diagnostics": list(result.diagnostics),
        }
        if args.write:
            digest, changed = write_complete_graph(result, args.write)
            summary.update({"destination": str(args.write), "sha256": digest, "changed": changed})
        else:
            sys.stdout.write(graph_json(result.graph))
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 0
    except (CrawlError, OSError, ValueError) as exc:
        print(f"topic-entity crawler failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
