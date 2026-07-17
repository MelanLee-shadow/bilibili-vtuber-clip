#!/usr/bin/env python3
"""Refresh the occurrence-neutral PSPLive entity roster from official Bilibili."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.channel_profile import load_channel_profile  # noqa: E402
from src.autoslice.psplive_roster_crawler import (  # noqa: E402
    PspliveRosterError,
    build_snapshot,
    load_source_config,
    snapshot_json,
)
from src.autoslice.timely_term_crawler import BoundedHttpClient, HttpCache  # noqa: E402


PROFILE = load_channel_profile(ROOT)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path, default=PROFILE.asset_file("psplive_roster_sources"))
    result.add_argument("--cache-dir", type=Path, default=ROOT / ".cache/psplive-roster-crawler")
    result.add_argument("--write", type=Path)
    result.add_argument("--now")
    return result


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
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
    try:
        config = load_source_config(args.config)
        client = BoundedHttpClient(
            max_requests=max(1, len(config["sources"])),
            max_response_bytes=512 * 1024,
            timeout_seconds=15,
            cache=HttpCache(args.cache_dir, max_body_bytes=512 * 1024),
            cache_ttl=dt.timedelta(hours=18),
            now=now,
        )
        payloads = []
        for source in config["sources"]:
            response = client.fetch(
                str(source["api_url"]),
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": str(source["public_url"]),
                },
            )
            payloads.append(json.loads(response.body))
        snapshot = build_snapshot(api_payloads=payloads, config=config, generated_at=now)
        rendered = snapshot_json(snapshot)
        if args.write:
            _atomic_write(args.write, rendered)
        else:
            sys.stdout.write(rendered)
        print(
            json.dumps(
                {
                    "members": len(snapshot["members"]),
                    "destination": str(args.write) if args.write else None,
                    "network_requests": client.requests_made,
                    "cache_hits": client.cache_hits,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 0
    except (OSError, ValueError, PspliveRosterError, json.JSONDecodeError) as exc:
        print(f"PSPLive roster crawler failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
