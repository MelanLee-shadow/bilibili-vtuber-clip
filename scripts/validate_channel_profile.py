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

from src.autoslice.channel_profile import (
    ChannelProfileError,
    load_channel_profile,
)
from src.autoslice.channel_census import (
    CENSUS_CLUES_ASSET_KEY,
    CENSUS_CONFIG_ASSET_KEY,
    lint_census_config,
    load_census_recall_input,
    parse_census_config,
)
from src.autoslice.profile_manifest_history import validate_manifest_history


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="profile id under profiles/<id>/profile.json")
    parser.add_argument("--manifest", type=Path, help="explicit profile manifest path")
    parser.add_argument("--check-history", action="store_true",
                        help="also require archived history for an external manifest (always on for profiles/)")
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
        # Managed profiles are distributed with their raw history. Explicit
        # external config probes keep their existing schema-only semantics;
        # executing an older binding still requires its authenticated archive.
        if args.check_history or profile.manifest_path.is_relative_to(ROOT / "profiles"):
            validate_manifest_history(ROOT, profile.manifest_path)
    except (ChannelProfileError, OSError, ValueError) as exc:
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
    census_config_lint = "ABSENT"
    if CENSUS_CONFIG_ASSET_KEY in profile.asset_files:
        config_path = profile.asset_file(CENSUS_CONFIG_ASSET_KEY)
        if config_path.exists():
            try:
                config = parse_census_config(
                    json.loads(config_path.read_text(encoding="utf-8")), profile_id=profile.profile_id,
                )
                lint_census_config(config, metric_text=profile.asset_file(
                    "slice_selection_metric").read_text(encoding="utf-8"))
                census_config_lint = "VALID"
            except (ChannelProfileError, OSError, ValueError) as exc:
                census_config_lint = f"INVALID: {exc}"
    payload["census_config_lint"] = census_config_lint
    _, census_status, _ = load_census_recall_input(
        profile.asset_files.get(CENSUS_CLUES_ASSET_KEY), profile_id=profile.profile_id,
    )
    payload["census_clues_status"] = census_status
    notes = []
    if status == "READY" and voiceprint_status != "READY":
        notes.append("voiceprint 未 enroll（声纹栈/歌切人声证明用；不影响 talk 产包）")
    if any(path.endswith("scripts/regenerate_channel_cover.py") for path in missing):
        notes.append("scripts/regenerate_channel_cover.py（tools.cover_regenerator）属于早先的流水线：缺它是预期的，切片流水线不用它")
    # the clip pipeline's own two files beside the manifest: a value still carrying the template's placeholder
    unfilled = []
    for name in ("manual_lane.json", "manual_lane_channel.json"):
        lane_file = profile.manifest_path.parent / name
        if lane_file.is_file():
            try:
                doc = json.loads(lane_file.read_text(encoding="utf-8"))
            except ValueError as exc:
                unfilled.append(f"{name}: unreadable ({exc})")
                continue
            unfilled += [f"{name}: {key}" for key, value in doc.items()
                         if not key.startswith("_") and isinstance(value, str) and "replace_me" in value.lower()]
    if unfilled:
        payload["lane_config_unfilled"] = unfilled
        notes.append("lane_config_unfilled 列出的键还是模板占位值：切片流水线第一次连主机时会失败，按 profiles/SETUP.md 填")
    if notes:
        payload["notes"] = notes
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if status == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
