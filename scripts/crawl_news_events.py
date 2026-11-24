#!/usr/bin/env python3
"""Daily crawl of circle news (events, projects, alliances, collaborations) into a candidate entity graph.

Reads the roster and the owner's dynamics snapshots and the registry accounts' recent dynamics (the accounts step 6
reads when it asks who is in a call), searches through the Search Router, lets CPA judge the results and the dynamics,
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

from src.autoslice import community_name_graph
from src.autoslice.community_comment_discovery import fetch_wbi_mixin_key
from src.autoslice.community_name_crawler import load_config as load_name_sources
from src.autoslice.timely_term_crawler import BoundedHttpClient, HttpCache
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL, cpa_call_timeout_seconds
from src.autoslice.llm_client import LlmConfig, build_llm_call
from src.autoslice.manual_lane.work_lookup import WebLookup
from src.autoslice.news_event_seeds import clip_seed_names
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
    parser.add_argument("--clip-references", type=Path, default=None,
                        help="what the clips looked up (default: the lane's reference seeds file, references.seeds_path)")
    parser.add_argument("--community-names", type=Path, default=None,
                        help="state/community_names.json: fold its names into the graph (deduped against it)")
    parser.add_argument("--community-state", type=Path, default=None,
                        help="state/community_name_state.json: the earliest video of each name")
    parser.add_argument("--community-cache", type=Path, default=None,
                        help="HTTP cache for the Bilibili search that dates the names (default: <base>/cache/community-name-origins)")
    parser.add_argument("--origin-limit", type=int, default=community_name_graph.ORIGIN_LIMIT,
                        help="community names whose meaning/origin/earliest date are looked up this run (a backfill sets it high)")
    parser.add_argument("--max-seeds", type=int, default=MAX_SEEDS)
    parser.add_argument("--max-runtime-seconds", type=int, default=3600)
    parser.add_argument("--now", help="pin an ISO timestamp")
    parser.add_argument("--llm-command", default=DEFAULT_LLM_COMMAND)
    return parser


def _clip_references(given: Path | None, notes: list[str]) -> Path | None:
    """The lane's reference seeds file: the one named, else where the lane's own layout puts it.  The lane is asked only
    here, when the crawler runs: a host without the lane's layout crawls with its other seeds and says so."""
    if given is not None:
        return given
    try:
        from src.autoslice.manual_lane.references import seeds_path

        return seeds_path()
    except Exception as exc:  # noqa: BLE001 - one seed source missing never stops the crawl
        notes.append(f"clip references unavailable: {type(exc).__name__}: {str(exc)[:200]}")
        return None


def _load_snapshot(path: Path, validate, label: str, notes: list[str]):  # noqa: ANN001, ANN202
    """A validated snapshot, or None with a note: one input missing never stops the other seeds from being searched."""
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        notes.append(f"{label} unavailable: {type(exc).__name__}: {str(exc)[:200]}")
        return None


def read_peer_posts(notes: list[str]) -> dict[str, list[dict[str, str]]] | None:
    """The recent dynamics of the reviewed registry's accounts, read the way step 6 reads them (collab_guests.accounts,
    dynamics_feed.recent); None with a note when the registry cannot be read, a note per account that cannot."""
    try:
        from src.autoslice.manual_lane import collab_guests, dynamics_feed

        uids = collab_guests.accounts()
    except Exception as exc:  # noqa: BLE001 - the web search still runs; the log says the dynamics were not read
        notes.append(f"peer dynamics unavailable: {type(exc).__name__}: {str(exc)[:200]}")
        return None
    out: dict[str, list[dict[str, str]]] = {}
    for name, uid in list(uids.items())[:collab_guests.PEERS_READ]:
        try:
            out[name] = dynamics_feed.recent(uid)
        except (dynamics_feed.FeedError, OSError, ValueError) as exc:
            notes.append(f"peer dynamics {name}: {type(exc).__name__}: {str(exc)[:200]}")
    return out


class NameVideos:
    """Bilibili video search for one community name, paced like the community-name crawler (its sources config:
    hosts, request cap, interval); the public WBI signing key is fetched once, on first use."""

    def __init__(self, cache_dir: Path, now: dt.datetime, limit: int) -> None:
        config = load_name_sources(PROFILE.asset_file("community_name_sources"))
        self.endpoint, self.nav = str(config["search_endpoint"]), str(config["wbi_nav_endpoint"])
        self.max_results, self.interval = int(config["max_results"]), float(config["request_interval_seconds"])
        self.now = now
        self.client = BoundedHttpClient(
            allowed_hosts=frozenset(str(item).lower() for item in config["source_hosts"]),
            max_requests=limit + 1, max_response_bytes=512 * 1024, timeout_seconds=15,
            cache=HttpCache(cache_dir, max_body_bytes=512 * 1024, max_entries=256, max_total_bytes=64 * 1024 * 1024),
            cache_ttl=dt.timedelta(hours=18), stale_if_error=dt.timedelta(days=7), now=now)
        self.key: str | None = None
        self.last = 0.0

    def __call__(self, name: str) -> list:  # noqa: ANN204
        if self.key is None:
            self.key, fresh = fetch_wbi_mixin_key(self.client, endpoint=self.nav)
            if not fresh:
                self.key = None
                raise RuntimeError("Bilibili WBI bootstrap used a stale fallback")
        wait = self.interval - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        try:
            return community_name_graph.probe_videos(self.client, endpoint=self.endpoint,
                                                     max_results=self.max_results, query=name,
                                                     mixin_key=self.key, now=self.now)
        finally:
            self.last = time.monotonic()


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
    peer_posts = read_peer_posts(notes)
    community = None
    if args.community_names is not None:
        try:
            community = community_name_graph.load(args.community_names, args.community_state,
                                                  args.write_root.parent / community_name_graph.ORIGINS_FILE, notes)
        except (OSError, ValueError) as exc:  # the news part still runs; the log says the names were not folded
            notes.append(f"community names unavailable: {type(exc).__name__}: {str(exc)[:200]}")
    name_videos = None
    if community is not None:
        try:
            name_videos = NameVideos(args.community_cache or args.write_root.parent.parent / "cache/community-name-origins",
                                     now, args.origin_limit)
        except Exception as exc:  # noqa: BLE001 - the names are still folded and looked up on the web, without dates
            notes.append(f"bilibili name search unavailable: {type(exc).__name__}: {str(exc)[:200]}")
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
            peer_posts=peer_posts,
            search=RouterSearch(WebLookup(), deadline=time.monotonic() + args.max_runtime_seconds),
            llm_call=llm_call,
            now=now,
            max_seeds=args.max_seeds,
            notes=notes,
            clip_names=clip_seed_names(_clip_references(args.clip_references, notes)),
            community=community,
            name_videos=name_videos,
            origin_limit=args.origin_limit,
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
