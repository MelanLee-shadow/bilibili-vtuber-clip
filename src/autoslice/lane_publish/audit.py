"""The package audit step 10 runs first: is this still the package step 9 sealed, whole, and something Bilibili will take?

What step 9 judged (subtitles, title, cover, the finished-media review) is not judged again here; this checks that what is about
to be sent is those bytes: every file the seal (review_manifest.json) binds is there with its sha256, the title file says the
sealed title, the finished-media review receipt says PASS, there is a description and there are tags, the video has a picture
and a sound stream, and the cover is a PNG Bilibili accepts (at least 960x600)."""

from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path
from typing import Any

from src.autoslice.authorized_upload_metadata import validate_tags

from .common import now, sha256

SCHEMA = "lane-publish-package-audit.v1"
SEAL_SCHEMA = "finished-media-review-package.v1"
BOUND = ("video", "cover", "ass_path", "subtitle_srt", "speaker_srt", "record", "review", "receipt")
COVER_MIN = (960, 600)


def _png_size(path: Path) -> tuple[int, int] | None:
    head = path.read_bytes()[:24]
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def _streams(video: Path) -> tuple[set[str], float] | str:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json",
                              str(video)], capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"ffprobe 跑不了：{type(exc).__name__}: {exc}"
    if out.returncode:
        return f"ffprobe 返回 {out.returncode}：{out.stderr[-300:]}"
    doc = json.loads(out.stdout or "{}")
    kinds = {str(s.get("codec_type")) for s in doc.get("streams") or []}
    return kinds, float((doc.get("format") or {}).get("duration") or 0)


def audit(package: Path) -> dict[str, Any]:
    package = package.absolute()
    issues: list[str] = []
    inputs: dict[str, str] = {}
    seal = package / "review_manifest.json"
    try:
        manifest = json.loads(seal.read_text(encoding="utf-8"))
        items = [i for i in manifest.get("items") or [] if isinstance(i, dict) and i.get("stem") == package.name]
        if manifest.get("schema_version") != SEAL_SCHEMA:
            issues.append(f"封存清单 schema 是 {manifest.get('schema_version')!r}，应为 {SEAL_SCHEMA}")
        if len(items) != 1:
            issues.append(f"封存清单里名为 {package.name} 的条目有 {len(items)} 条，应为 1 条")
        item = items[0] if items else {}
    except (OSError, ValueError, AttributeError) as exc:
        item = {}
        issues.append(f"读不到封存清单 {seal}：{type(exc).__name__}: {exc}")
    for key in BOUND:
        name = item.get(key)
        if not isinstance(name, str) or not name:
            issues.append(f"封存清单没有 {key}")
            continue
        file = package / name
        if not file.is_file():
            issues.append(f"{key} 不在包里：{name}")
            continue
        inputs[key] = sha256(file)
        if inputs[key] != str(item.get(f"{key}_sha256") or "").removeprefix("sha256:"):
            issues.append(f"{key} 与封存时的 sha256 不同：{name}")
    title = item.get("title")
    try:
        written = (package / str(item.get("title_txt") or f"{package.name}.title.txt")).read_text(encoding="utf-8").rstrip("\n")
        if not title or written != title:
            issues.append(f"标题文件「{written}」与封存的标题「{title}」不一致")
        description = (package / str(item.get("description_txt") or f"{package.name}.description.txt")).read_text(encoding="utf-8")
        if not description.strip():
            issues.append("简介是空的")
    except OSError as exc:
        issues.append(f"读不到标题或简介：{exc}")
    if "receipt" in inputs:
        verdict = json.loads((package / item["receipt"]).read_text(encoding="utf-8")).get("verdict")
        if verdict != "PASS":
            issues.append(f"成品审查回执的结论是 {verdict!r}，不是 PASS")
    if "record" in inputs:
        upload_tags = json.loads((package / item["record"]).read_text(encoding="utf-8")).get("upload_tags") or {}
        tags = upload_tags.get("final_tags") if isinstance(upload_tags, dict) else None
        if not (isinstance(tags, list) and tags and all(isinstance(t, str) and t for t in tags)):
            issues.append("record.json 没有定下的标签（upload_tags.final_tags）")
        elif upload_tags.get("source") != "published" and (tag_problems := validate_tags(tags)):
            issues.append(f"record.json 标签不符合策略：{'；'.join(tag_problems)}")
    if "video" in inputs:
        probed = _streams(package / item["video"])
        if isinstance(probed, str):
            issues.append(probed)
        elif not {"video", "audio"} <= probed[0] or probed[1] <= 0:
            issues.append(f"成片要有画面和声音、时长大于 0：流 {sorted(probed[0])}，时长 {probed[1]}")
    if "cover" in inputs:
        size = _png_size(package / item["cover"])
        if size is None or size[0] < COVER_MIN[0] or size[1] < COVER_MIN[1]:
            issues.append(f"封面要是至少 {COVER_MIN[0]}x{COVER_MIN[1]} 的 PNG：{size}")
    return {"schema_version": SCHEMA, "root": str(package), "audited_at": now(), "audited_inputs": inputs,
            "issues": issues, "issue_count": len(issues), "blocking_issue_count": len(issues), "passed": not issues}
