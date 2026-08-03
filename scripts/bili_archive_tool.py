#!/usr/bin/env python3
"""B 站已发稿件维护 CLI —— 统一入口，替代 free 上的一次性脚本群。

用法（在 free 上、或任何持有 cookie 文件的机器上）：

  view        python3 scripts/bili_archive_tool.py view BV1xx
  换封面/标题  python3 scripts/bili_archive_tool.py edit BV1xx [--title 新标题] [--part-title 分P标题] [--cover new.png] [--tags "a,b,c"]
  零配额换源   python3 scripts/bili_archive_tool.py replace BV1xx --media new.mp4 [--cover new.png] [--title 新标题]
  入合集       python3 scripts/bili_archive_tool.py season-add BV1xx --section-id 9110001

新投稿不在此工具范围 —— 走 scripts/authorized_upload.py（授权引语冻结 +
配额 + 台账）。编辑不占投稿配额；删稿有验证码墙，别在这里找。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.autoslice.bilibili_member_api import (  # noqa: E402
    DEFAULT_BILIUP_COOKIES,
    BiliSession,
    replace_archive_source,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookies", type=Path, default=DEFAULT_BILIUP_COOKIES)
    sub = parser.add_subparsers(dest="command", required=True)

    p_view = sub.add_parser("view", help="打印稿件状态/标题/分P")
    p_view.add_argument("bvid")

    p_edit = sub.add_parser("edit", help="改标题/封面/tags（不动视频）")
    p_edit.add_argument("bvid")
    p_edit.add_argument("--title")
    p_edit.add_argument("--part-title", help="完整替换所有现有分P标题；单P修复时通常与稿件标题相同")
    p_edit.add_argument("--cover", type=Path)
    p_edit.add_argument("--tags", help="逗号分隔完整替换")

    p_replace = sub.add_parser("replace", help="append+edit 零配额换源")
    p_replace.add_argument("bvid")
    p_replace.add_argument("--media", type=Path, required=True)
    p_replace.add_argument("--cover", type=Path)
    p_replace.add_argument("--title")

    p_season = sub.add_parser("season-add", help="加入合集小节（幂等）")
    p_season.add_argument("bvid")
    p_season.add_argument("--section-id", type=int, required=True)

    args = parser.parse_args()
    session = BiliSession(cookie_path=args.cookies)

    if args.command == "view":
        data = session.archive_view(args.bvid)
        archive = data["archive"]
        print(json.dumps(
            {
                "bvid": args.bvid,
                "state": archive.get("state_desc"),
                "title": archive.get("title"),
                "tag": archive.get("tag"),
                "cover": archive.get("cover"),
                "videos": [
                    {"title": v.get("title"), "cid": v.get("cid")}
                    for v in data.get("videos") or []
                ],
            },
            ensure_ascii=False, indent=1,
        ))
        return 0

    if args.command == "edit":
        if args.title is None and args.part_title is None and args.cover is None and args.tags is None:
            parser.error("edit needs at least one of --title/--part-title/--cover/--tags")
        data = session.archive_view(args.bvid)
        cover_url = session.cover_up(args.cover.read_bytes()) if args.cover else None
        payload = session.build_edit_payload(
            data,
            title=args.title,
            cover_url=cover_url,
            tag=args.tags,
            video_title=args.part_title,
        )
        print(json.dumps(session.edit_archive(payload), ensure_ascii=False))
        return 0

    if args.command == "replace":
        result = replace_archive_source(
            session,
            args.bvid,
            new_media=args.media,
            new_cover_png=args.cover,
            new_title=args.title,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.command == "season-add":
        data = session.archive_view(args.bvid)
        archive = data["archive"]
        videos = data.get("videos") or []
        if not videos:
            raise SystemExit(f"{args.bvid}: no videos yet (transcoding?) — retry later")
        result = session.season_episode_add(
            args.section_id,
            aid=archive["aid"],
            cid=videos[0]["cid"],
            title=archive.get("title") or args.bvid,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
