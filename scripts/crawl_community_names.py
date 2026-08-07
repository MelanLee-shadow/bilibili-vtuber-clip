#!/usr/bin/env python3
"""Daily incremental Bilibili community-name and meme relation crawler."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.channel_profile import load_channel_profile  # noqa: E402
from src.autoslice.community_name_crawler import (  # noqa: E402
    CommunityNameError,
    canonical_json,
    crawl,
    load_config,
    load_state,
    validate_snapshot,
)
from src.autoslice.llm_client import LlmConfig, build_llm_call  # noqa: E402
from src.autoslice.streamer_registry_crawler import validate_snapshot as validate_registry  # noqa: E402
from src.autoslice.timely_term_crawler import (  # noqa: E402
    BoundedHttpClient,
    FetchLimitError,
    HttpCache,
)


PROFILE = load_channel_profile(ROOT)
DEFAULT_LLM_COMMAND = (
    "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} "
    "'gpt-5.6-luna gpt-5.5 gpt-5.4' medium 1"
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--registry", type=Path, required=False)
    result.add_argument(
        "--config", type=Path, default=PROFILE.asset_file("community_name_sources")
    )
    result.add_argument(
        "--cache-dir", type=Path, default=ROOT / ".cache/community-name-crawler"
    )
    result.add_argument("--state", type=Path, required=True)
    result.add_argument("--write", type=Path, required=True)
    result.add_argument("--entity", action="append", default=[])
    result.add_argument("--now")
    result.add_argument("--llm-command", default=DEFAULT_LLM_COMMAND)
    result.add_argument("--no-llm", action="store_true")
    result.add_argument("--request-interval-seconds", type=float)
    result.add_argument("--max-runtime-seconds", type=int)
    return result


class _PacedClient:
    def __init__(self, client, *, interval_seconds: float, max_runtime_seconds: int):  # noqa: ANN001
        self._client = client
        self._interval = interval_seconds
        self._started = time.monotonic()
        self._deadline = self._started + max_runtime_seconds
        self._last_fetch: float | None = None

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        return getattr(self._client, name)

    def fetch(self, url, **kwargs):  # noqa: ANN001, ANN201
        current = time.monotonic()
        if self._last_fetch is not None:
            remaining = self._interval - (current - self._last_fetch)
            if remaining > 0:
                if current + remaining > self._deadline:
                    raise FetchLimitError("community-name runtime budget exhausted")
                time.sleep(remaining)
        if time.monotonic() > self._deadline:
            raise FetchLimitError("community-name runtime budget exhausted")
        try:
            return self._client.fetch(url, **kwargs)
        finally:
            self._last_fetch = time.monotonic()


def _atomic_write(path: Path, text: str, *, mode: int) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        temporary = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    now = (
        dt.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    )
    registry_path = args.registry or PROFILE.asset_file("streamer_registry")
    try:
        registry = validate_registry(
            json.loads(registry_path.read_text(encoding="utf-8")), as_of=now
        )
        config = load_config(args.config)
        state = load_state(args.state)
        llm_call = None
        if not args.no_llm:
            llm_call = build_llm_call(
                LlmConfig(
                    transport="command",
                    command_template=args.llm_command,
                    timeout_seconds=240.0,
                )
            )
        raw_client = BoundedHttpClient(
            allowed_hosts=frozenset(str(item).lower() for item in config["source_hosts"]),
            max_requests=int(config["max_requests"]),
            max_response_bytes=512 * 1024,
            timeout_seconds=15,
            cache=HttpCache(
                args.cache_dir,
                max_body_bytes=512 * 1024,
                max_entries=1024,
                max_total_bytes=256 * 1024 * 1024,
            ),
            cache_ttl=dt.timedelta(hours=18),
            stale_if_error=dt.timedelta(days=7),
            now=now,
        )
        interval = (
            args.request_interval_seconds
            if args.request_interval_seconds is not None
            else float(config["request_interval_seconds"])
        )
        runtime = (
            args.max_runtime_seconds
            if args.max_runtime_seconds is not None
            else int(config["max_runtime_seconds"])
        )
        if interval < 0 or not 60 <= runtime <= 3600:
            raise CommunityNameError("invalid request pacing override")
        client = _PacedClient(
            raw_client,
            interval_seconds=interval,
            max_runtime_seconds=runtime,
        )
        result = crawl(
            client=client,
            registry=registry,
            config=config,
            state=state,
            now=now,
            llm_call=llm_call,
            forced_entities=args.entity or None,
        )
        state_changed = _atomic_write(args.state, canonical_json(result["state"]), mode=0o600)
        snapshot_changed = False
        if result["snapshot"] is not None:
            validate_snapshot(result["snapshot"], as_of=now)
            snapshot_changed = _atomic_write(
                args.write, canonical_json(result["snapshot"]), mode=0o444
            )
        last_run = result["state"]["last_run"]
        print(
            json.dumps(
                {
                    "accepted": len(
                        [row for row in result["state"]["mappings"] if row["status"] == "accepted"]
                    ),
                    "candidates": len(
                        [row for row in result["state"]["mappings"] if row["status"] == "candidate"]
                    ),
                    "conflicts": len(
                        [row for row in result["state"]["mappings"] if row["status"] == "conflict"]
                    ),
                    "full_failure": result["full_failure"],
                    "judge_error": last_run["judge_error"],
                    "semantic_review_error": last_run.get("semantic_review_error"),
                    "semantic_review_batches": last_run.get("semantic_review_batches", 0),
                    "semantic_review_count": last_run.get("semantic_review_count", 0),
                    "relation_kind_changes": last_run.get("relation_kind_changes", []),
                    "network_requests": last_run["network_requests"],
                    "selected_entities": len(last_run["selected_entities"]),
                    "successful_entities": len(last_run["successful_entities"]),
                    "search_stats": last_run["search_stats"],
                    "comment_stats": last_run["comment_stats"],
                    "search_circuit": last_run["search_circuit"],
                    "comment_circuit": last_run["comment_circuit"],
                    "snapshot_changed": snapshot_changed,
                    "state_changed": state_changed,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2 if result["full_failure"] else 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, CommunityNameError) as exc:
        print(f"community-name crawler failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
