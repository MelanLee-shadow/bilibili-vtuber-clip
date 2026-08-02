#!/usr/bin/env python3
"""从官方回放造出管线认可的 session 源三件套（sidecar + provenance）。

配套 `.agent/skills/official-replay-rescue` 的「从零造 session 源」一节：
只有官方回放、没有任何录播姬幸存件时，为**已经切好的段 mp4** 生成
`<stem>.xml`（弹幕忠实切片，相对轴归零）、`<stem>.jsonl`（结构化弹幕
sidecar，只含 DANMU_MSG）、`<stem>.meta.json`（RecordStartTime=画面时钟
锚定的墙钟）、`<stem>.replay-provenance.json`（全部推导披露）。

铁律（与 skill 一致）：文本与相对时间逐字来自回放 xml；绝对时间＝墙钟锚点
＋真实偏移；**不伪造任何字段**（uid/uname 留空，消费端本就把弹幕发送者存
空串）；回放 xml 自带的 epoch 不可信（批量导入时刻），绝不使用。

用法示例：
  python3 scripts/build_session_from_replay.py \\
    --segment ~/Videos/23222837/23222837_20260802-17-00-20.mp4 \\
    --replay-mp4 vod/BV1xxx.mp4 --replay-xml vod/BV1xxx.xml --bvid BV1xxx \\
    --room 23222837 --window-start 3600 --window-end 5400 \\
    --t0-wall "2026-08-02T17:00:20+08:00" \\
    --anchor-note "overlay TIME 两帧交叉验证，绝对误差<=10s"

段 mp4 的 stem 必须已按 `<room>_YYYYMMDD-HH-MM-SS` 命名（时间=画面时钟锚定
结果）。建议**至少造两个连续段**：边界复核需要看到终点之后的下一个话题，
段尾最后约 1 分钟内收束的候选在单段源上会因
BOUNDARY_SOURCE_WITNESS_RESERVE_INCOMPLETE 交付不了。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout.strip())


def build(args: argparse.Namespace) -> dict:
    segment = args.segment.resolve(strict=True)
    stem = segment.stem
    outdir = segment.parent
    t0_wall = dt.datetime.fromisoformat(args.t0_wall)
    if t0_wall.tzinfo is None:
        raise SystemExit("--t0-wall 必须带时区（如 +08:00）")
    window_start, window_end = float(args.window_start), float(args.window_end)
    if not window_end > window_start:
        raise SystemExit("--window-end 必须大于 --window-start")

    root = ET.parse(args.replay_xml).getroot()
    total_events = 0
    kept: list[tuple[float, list[str], str]] = []
    for node in root.iter("d"):
        total_events += 1
        text = (node.text or "").strip()
        fields = (node.get("p") or "").split(",")
        if not text:
            continue
        try:
            offset = float(fields[0])
        except (ValueError, IndexError):
            continue
        if not (window_start <= offset < window_end):
            continue
        kept.append((offset - window_start, fields, text))
    kept.sort(key=lambda row: row[0])

    xml_root = ET.Element("i")
    ET.SubElement(xml_root, "chatserver").text = "chat.bilibili.com"
    ET.SubElement(xml_root, "chatid").text = "0"
    ET.SubElement(xml_root, "mission").text = "0"
    ET.SubElement(xml_root, "maxlimit").text = str(max(len(kept), 1))
    ET.SubElement(xml_root, "state").text = "0"
    ET.SubElement(xml_root, "real_name").text = "0"
    ET.SubElement(xml_root, "source").text = f"official-replay:{args.bvid}"
    for rel, fields, text in kept:
        rest = fields[1:] if len(fields) > 1 else []
        node = ET.SubElement(xml_root, "d")
        node.set("p", ",".join([f"{rel:.3f}"] + rest))
        node.text = text
    xml_path = outdir / f"{stem}.xml"
    ET.ElementTree(xml_root).write(xml_path, encoding="utf-8", xml_declaration=True)

    # 消费端契约（src/autoslice/chat_event_timing.py）：DANMU_MSG →
    # info[0][4]=epoch ms、info[1]=文本；发送者由消费端存空串，不伪造。
    t0_ms = int(t0_wall.timestamp() * 1000)
    jsonl_path = outdir / f"{stem}.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for rel, _fields, text in kept:
            payload = {
                "cmd": "DANMU_MSG",
                "info": [[0, 0, 0, 0, t0_ms + int(round(rel * 1000)), 0, 0, "", 0], text, []],
            }
            handle.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            )

    (outdir / f"{stem}.meta.json").write_text(
        json.dumps(
            {"description": {"RecordStartTime": t0_wall.isoformat()}},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    provenance = {
        "schema_version": "official-replay-session-provenance.v1",
        "built_by": "scripts/build_session_from_replay.py（official-replay-rescue「从零造 session 源」）",
        "source": {
            "kind": "bilibili_official_live_replay",
            "bvid": args.bvid,
            "room_id": args.room,
            "replay_mp4_sha256": _sha256(args.replay_mp4),
            "replay_xml_sha256": _sha256(args.replay_xml),
            "replay_duration_s": _ffprobe_duration(args.replay_mp4),
        },
        "cut": {
            "window_in_replay_s": [window_start, window_end],
            "segment_path": str(segment),
            "segment_sha256": _sha256(segment),
            "segment_duration_s": _ffprobe_duration(segment),
            "note": args.note,
        },
        "wall_clock_anchor": {
            "method": "画面内时钟 > 官方标题场次时间（skill 锚点优先级）",
            "segment_start_wall": t0_wall.isoformat(),
            "uncertainty_s": args.uncertainty_s,
            "uncertainty_note": (
                "overlay 时钟相对回放轴可能有 ~8s/小时漂移；绝对误差按锚点披露。"
                "段内时间轴与弹幕轴同源于回放轴，内部一致性精确。"
            ),
            "anchor_note": args.anchor_note,
        },
        "chat": {
            "authority": "replay danmaku XML（相对轴）；回放自带 epoch 不可信（批量导入时刻）",
            "events_in_window": len(kept),
            "events_in_full_replay": total_events,
            "kinds": ["DANMU_MSG"],
            "not_present": ["SUPER_CHAT_MESSAGE", "SEND_GIFT", "GUARD_BUY"],
            "fabricated_fields": "none",
            "jsonl_sha256": _sha256(jsonl_path),
            "xml_sha256": _sha256(xml_path),
        },
        "known_limits": [
            "回放为转码件非原画；无录播姬封口事件链，不冒充 adapter 产物",
            "无 SC/礼物/上船证据，依赖这些证据的修复路径会 fail-closed",
            "单段源的段尾候选会因边界见证不足交付不了——建议至少造两个连续段",
        ],
    }
    (outdir / f"{stem}.replay-provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "stem": stem,
        "danmaku_in_window": len(kept),
        "segment_duration_s": provenance["cut"]["segment_duration_s"],
        "files": [
            xml_path.name,
            jsonl_path.name,
            f"{stem}.meta.json",
            f"{stem}.replay-provenance.json",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--segment", type=Path, required=True, help="已切好的段 mp4（stem=<room>_YYYYMMDD-HH-MM-SS）")
    parser.add_argument("--replay-mp4", type=Path, required=True)
    parser.add_argument("--replay-xml", type=Path, required=True)
    parser.add_argument("--bvid", required=True)
    parser.add_argument("--room", required=True)
    parser.add_argument("--window-start", type=float, required=True, help="段起点在回放时间轴上的秒数")
    parser.add_argument("--window-end", type=float, required=True)
    parser.add_argument("--t0-wall", required=True, help="段起点墙钟（ISO8601 带时区；画面时钟锚定结果）")
    parser.add_argument("--uncertainty-s", type=int, default=10)
    parser.add_argument("--anchor-note", default="", help="锚点证据说明（抽了哪些帧、读到什么钟）")
    parser.add_argument("--note", default="", help="选窗理由等备注")
    args = parser.parse_args()
    print(json.dumps(build(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
