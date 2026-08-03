#!/usr/bin/env python3
"""Validate one channel profile and its referenced runtime assets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.channel_profile import (  # noqa: E402
    ChannelProfileError,
    load_channel_profile,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="profile id under profiles/<id>/profile.json")
    parser.add_argument("--manifest", type=Path, help="explicit profile manifest path")
    parser.add_argument(
        "--config-only",
        action="store_true",
        help="validate schema and paths without requiring referenced assets to exist",
    )
    args = parser.parse_args(argv)
    try:
        profile = load_channel_profile(
            ROOT,
            profile_id=args.profile,
            manifest_path=args.manifest,
            environ={},
        )
    except ChannelProfileError as exc:
        print(json.dumps({"status": "INVALID", "error": str(exc)}, ensure_ascii=False))
        return 2

    missing = [str(path) for path in profile.missing_runtime_paths()]
    status = "READY" if args.config_only or not missing else "BLOCKED"
    # 声纹占位可从模板整份复制而来：文件存在≠已 enroll。READY 不撒谎，但要
    # 把「层 4 声纹还没配」明说，别让新用户把 READY 读成全层齐活。
    voiceprint_status = "ABSENT"
    try:
        voiceprint_doc = json.loads(
            profile.asset_file("voiceprint_profile").read_text(encoding="utf-8")
        )
        voiceprint_status = str(
            voiceprint_doc.get("configuration_status") or "UNKNOWN"
        )
    except (ChannelProfileError, OSError, ValueError):
        pass
    payload = {
        "status": status,
        "profile_id": profile.profile_id,
        "manifest": str(profile.manifest_path),
        "manifest_sha256": profile.manifest_sha256,
        "room_id": profile.room_id,
        "output_directory": profile.output_directory,
        "missing_runtime_paths": missing,
        "voiceprint_status": voiceprint_status,
    }
    if status == "READY" and voiceprint_status != "READY":
        payload["notes"] = [
            "voiceprint 未 enroll（声纹栈/歌切人声证明用；不影响 talk 产包）"
        ]
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if status == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
