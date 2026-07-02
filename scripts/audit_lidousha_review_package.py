from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

MAX_VISUAL_LINES = 2
MAX_VISUAL_LINE_CHARS = 18
LONG_STATIC_CUE_SECONDS = 10.0


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def _resolve(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute():
        if path.exists():
            return path
        # Remote/container paths in manifests are not locally reachable. Fall through to basename lookup.
        candidates = list(root.rglob(path.name))
        return candidates[0] if candidates else path
    return root / path


def _text_len(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _parse_srt_time(value: str) -> float:
    hms, ms = value.split(",", 1)
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _parse_srt(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_raw, end_raw = [part.strip() for part in lines[1].split("-->", 1)]
        try:
            start = _parse_srt_time(start_raw)
            end = _parse_srt_time(end_raw)
        except Exception:
            continue
        cues.append({"index": lines[0], "start": start, "end": end, "timing": lines[1], "lines": lines[2:]})
    return cues


def _ass_dialogue_texts(path: Path) -> list[str]:
    if not path.exists():
        return []
    texts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) == 10:
            texts.append(parts[9])
    return texts


def _visual_lines(ass_text: str) -> list[str]:
    # ASS manual line breaks are \N. Some test fixtures may contain an escaped double backslash.
    return [part for part in re.split(r"\\+N", ass_text) if part != ""]


def _read_title_txt(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("cover_text:"):
            return stripped
    return ""


def _read_publish_title(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    data = _load_json(path)
    title = data.get("title")
    return title.strip() if isinstance(title, str) else ""


def _contains_japanese(text: str) -> bool:
    return bool(re.search(r"[ぁ-んァ-ヶ]", text))


def _looks_song_like(item: dict[str, Any], evidence: dict[str, Any]) -> bool:
    joined = " ".join(
        str(value)
        for value in [
            item.get("title", ""),
            evidence.get("title", ""),
            evidence.get("summary", ""),
            evidence.get("transcript_excerpt", ""),
            evidence.get("hook", ""),
        ]
    )
    song_markers = [
        "合唱",
        "唱歌",
        "唱着",
        "歌词",
        "词曲",
        "一首歌",
        "最后一首",
        "伴奏",
        "KTV",
        "song",
        "lyrics",
        "fly me",
    ]
    return any(marker.lower() in joined.lower() for marker in song_markers) or _contains_japanese(joined)


def _has_alignment_evidence(root: Path, item: dict[str, Any]) -> bool:
    keys = [
        "lyrics_alignment_report",
        "lyrics_alignment",
        "alignment_report",
        "lyrics_source",
        "clip_first_lyric_time",
        "clip_last_lyric_time",
        "tail_delta",
    ]
    if any(key in item for key in keys):
        return True
    stem = str(item.get("stem") or "")
    if not stem:
        return bool(list((root / "lyrics_alignment").glob("*.json"))) if (root / "lyrics_alignment").exists() else False
    candidates = list(root.glob(f"lyrics_alignment/**/{stem}*.json")) + list(root.glob(f"**/{stem}*alignment*.json"))
    return bool(candidates)


def _has_ai_cover_evidence(root: Path, item: dict[str, Any]) -> bool:
    if item.get("ai_cover_generated") is True:
        return True
    for key in ["source_ai_background", "ai_background", "ai_cover", "ai_background_sha256"]:
        if item.get(key):
            return True
    cover_generation = item.get("cover_generation")
    if isinstance(cover_generation, dict):
        if cover_generation.get("ai_background") or cover_generation.get("model") or cover_generation.get("method"):
            return True
    stem = str(item.get("stem") or "")
    if (root / "covers_ai_original").exists():
        if stem and list((root / "covers_ai_original").glob(f"{stem}*")):
            return True
        if not stem and list((root / "covers_ai_original").glob("*")):
            return True
    return False


def _add_issue(issues: list[dict[str, Any]], code: str, *, stem: str = "", path: Path | None = None, detail: str = "", severity: str = "BLOCK") -> None:
    issue: dict[str, Any] = {"code": code, "severity": severity}
    if stem:
        issue["stem"] = stem
    if path is not None:
        issue["path"] = str(path)
    if detail:
        issue["detail"] = detail
    issues.append(issue)


def audit_package(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    manifest_path = root / "review_manifest.json"
    manifest = _load_json(manifest_path)
    issues: list[dict[str, Any]] = []

    if not manifest:
        _add_issue(issues, "MANIFEST_MISSING_OR_INVALID", path=manifest_path)
        return {"passed": False, "root": str(root), "issues": issues, "issue_count": len(issues)}

    status = str(manifest.get("status") or "")
    if status.startswith("invalid_review_draft"):
        _add_issue(issues, "PACKAGE_MARKED_INVALID_REVIEW_DRAFT", detail=f"manifest.status is {status}")

    invalid_marker = root / "INVALID_REDO_REQUIRED.json"
    if invalid_marker.exists():
        marker = _load_json(invalid_marker)
        _add_issue(
            issues,
            "PACKAGE_MARKED_INVALID_REDO_REQUIRED",
            path=invalid_marker,
            detail=str(marker.get("reason") or marker.get("status") or "package explicitly invalidated"),
        )

    items = manifest.get("items")
    if not isinstance(items, list):
        _add_issue(issues, "MANIFEST_ITEMS_MISSING", path=manifest_path)
        items = []

    for item in items:
        if not isinstance(item, dict):
            continue
        stem = str(item.get("stem") or item.get("id") or item.get("title") or "")
        subtitle_path = _resolve(root, item.get("subtitle_srt") or item.get("subtitle"))
        ass_path = _resolve(root, item.get("ass_path") or item.get("ass"))
        evidence_path = _resolve(root, item.get("evidence_json") or item.get("evidence"))
        publish_path = _resolve(root, item.get("publish_json") or item.get("publish"))
        title_txt_path = _resolve(root, item.get("title_txt") or item.get("title_path"))
        evidence = _load_json(evidence_path) if evidence_path else {}

        is_song = _looks_song_like(item, evidence)
        if is_song:
            source_srt = str(item.get("source_srt") or "")
            if source_srt.endswith(".jingting.srt"):
                _add_issue(issues, "SONG_USES_JINGTING_SRT", stem=stem, detail=source_srt)
            if not _has_alignment_evidence(root, item):
                _add_issue(issues, "SONG_LYRIC_SOURCE_MISSING", stem=stem, detail="No external timed lyric source evidence found")
                _add_issue(issues, "SONG_ALIGNMENT_REPORT_MISSING", stem=stem, detail="No first/last lyric anchor offset/tail report found")
            title = str(item.get("title") or "")
            if not (title.startswith("【李豆沙】豆沙歌，") and "《" in title and "》" in title):
                _add_issue(issues, "SONG_TITLE_FORMAT_INVALID", stem=stem, detail=title)

        if subtitle_path and subtitle_path.exists():
            for cue in _parse_srt(subtitle_path):
                duration = cue["end"] - cue["start"]
                lines = [line.strip() for line in cue["lines"] if line.strip()]
                if len(lines) > MAX_VISUAL_LINES:
                    _add_issue(issues, "SUBTITLE_CUE_TOO_MANY_LINES", stem=stem, path=subtitle_path, detail=f"cue {cue['index']} has {len(lines)} lines")
                for line in lines:
                    n = _text_len(line)
                    if n > MAX_VISUAL_LINE_CHARS:
                        _add_issue(issues, "SUBTITLE_LINE_TOO_LONG", stem=stem, path=subtitle_path, detail=f"cue {cue['index']} line length {n}: {line}")
                if duration >= LONG_STATIC_CUE_SECONDS and lines:
                    _add_issue(issues, "SUBTITLE_LONG_STATIC_CUE", stem=stem, path=subtitle_path, detail=f"cue {cue['index']} lasts {duration:.2f}s")

        if ass_path and ass_path.exists():
            for idx, text in enumerate(_ass_dialogue_texts(ass_path), start=1):
                visual_lines = _visual_lines(text)
                if len(visual_lines) > MAX_VISUAL_LINES:
                    _add_issue(issues, "SUBTITLE_ASS_TOO_MANY_VISUAL_LINES", stem=stem, path=ass_path, detail=f"dialogue {idx} has {len(visual_lines)} visual lines")
                for line in visual_lines:
                    n = _text_len(line)
                    if n > MAX_VISUAL_LINE_CHARS:
                        _add_issue(issues, "SUBTITLE_ASS_LINE_TOO_LONG", stem=stem, path=ass_path, detail=f"dialogue {idx} line length {n}: {line}")

        title_txt = _read_title_txt(title_txt_path)
        publish_title = _read_publish_title(publish_path)
        if title_txt and publish_title and title_txt != publish_title:
            _add_issue(issues, "PUBLISH_TITLE_TXT_MISMATCH", stem=stem, detail=f"title_txt={title_txt!r}; publish.title={publish_title!r}")

        cover_generation = item.get("cover_generation")
        cover_generation_text = json.dumps(cover_generation, ensure_ascii=False) if isinstance(cover_generation, dict) else str(cover_generation or "")
        if isinstance(cover_generation, dict):
            fallback_cover = bool(item.get("cover_regenerated_from_burn_frame")) or cover_generation.get("fallback_used") is True
        else:
            fallback_cover = bool(item.get("cover_regenerated_from_burn_frame")) or any(
                marker in cover_generation_text.lower()
                for marker in ["deterministic", "burn-frame", "burned-frame", "fallback", "frame cover"]
            )
        if fallback_cover:
            _add_issue(issues, "COVER_FALLBACK_NOT_FINISHED", stem=stem, detail=cover_generation_text)
        if not _has_ai_cover_evidence(root, item):
            _add_issue(issues, "AI_COVER_EVIDENCE_MISSING", stem=stem, detail="No AI background/reference/model evidence found")

    blocking = [issue for issue in issues if issue.get("severity") != "INFO"]
    return {"passed": not blocking, "root": str(root), "issues": issues, "issue_count": len(issues), "blocking_issue_count": len(blocking)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a Li Dousha finished/review package for subtitle, title, and cover gates.")
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    result = audit_package(args.package_root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"passed={result['passed']} blocking={result['blocking_issue_count']} issues={result['issue_count']}")
        for issue in result["issues"]:
            print(f"{issue.get('severity','BLOCK')} {issue['code']} {issue.get('stem','')} {issue.get('detail','')}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
