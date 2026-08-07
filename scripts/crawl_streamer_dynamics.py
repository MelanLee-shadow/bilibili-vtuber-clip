#!/usr/bin/env python3
"""Bounded crawler for the channel owner's recent Bilibili dynamics (动态)."""

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
from src.autoslice.streamer_dynamics import (  # noqa: E402
    ALLOWED_HOSTS,
    StreamerDynamicsError,
    build_snapshot,
    snapshot_json,
)
from src.autoslice.timely_term_crawler import (  # noqa: E402
    BoundedHttpClient,
    CrawlError,
    HttpCache,
)


PROFILE = load_channel_profile(ROOT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room-id", default=PROFILE.room_id)
    parser.add_argument(
        "--cache-dir", type=Path, default=ROOT / ".cache/streamer-dynamics-crawler"
    )
    parser.add_argument("--now", help="pin an ISO timestamp for replay/idempotent tests")
    parser.add_argument("--write", type=Path, required=True)
    return parser


def _atomic_write(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary: Path | None = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        temporary = None
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        now = (
            dt.datetime.fromisoformat(args.now.replace("Z", "+00:00"))
            if args.now
            else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        )
        if now.tzinfo is None:
            raise StreamerDynamicsError("--now must include a timezone")
        cache = HttpCache(args.cache_dir, max_body_bytes=2 * 1024 * 1024)
        client = BoundedHttpClient(
            allowed_hosts=ALLOWED_HOSTS,
            max_requests=3,
            max_response_bytes=2 * 1024 * 1024,
            timeout_seconds=15,
            cache=cache,
            cache_ttl=dt.timedelta(hours=6),
            now=now,
        )
        snapshot, skipped = build_snapshot(client, room_id=args.room_id, now=now)
        changed = _atomic_write(args.write, snapshot_json(snapshot))
        print(
            json.dumps(
                {
                    "items": len(snapshot["items"]),
                    "skipped": skipped,
                    "network_requests": client.requests_made,
                    "changed": changed,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1 if skipped else 0
    except (StreamerDynamicsError, CrawlError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"streamer-dynamics crawler failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
