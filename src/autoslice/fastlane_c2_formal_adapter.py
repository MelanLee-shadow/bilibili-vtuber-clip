"""C2-only formal package adapter; never a generic recovery/publish lane."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .cover_text_pixel_evidence import verify_rendered_text_pixel_artifacts

CID = "auto_203011_328_389"
TITLE = "【李豆沙】弹幕拔智齿后求“老公”亲亲，李豆沙：不是你老公，只会嘲笑你！"
SCHEMA = "fastlane-c2-formal-private-review-manifest.v1"
NAMES = {"video": f"{CID}.recut.burned-final-speaker.mp4", "source_video": f"{CID}.recut.mp4", "srt": f"{CID}.recut.srt", "ass": f"{CID}.recut.final-sapphire72.ass", "cover": f"{CID}.cover.png", "pre": f"{CID}.cover.pre-overlay.png", "mask": f"{CID}.cover.title-mask.png", "background": f"{CID}.cover.route-background.png", "title": f"{CID}.title.txt", "graph": "cue-graph.v1.json", "cover_meta": "cover-reprojection.v1.json", "visual": "visual-evidence/visual-evidence.v1.json"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def is_fastlane_c2_formal_manifest(manifest: Mapping[str, object]) -> bool:
    if manifest.get("candidate_id") != CID:
        return False
    if manifest.get("schema_version") == SCHEMA:
        return True
    from .fastlane_c2_release_bridge import is_fastlane_c2_release_manifest
    return is_fastlane_c2_release_manifest(manifest)


def visual_inventory(root: Path) -> list[dict[str, object]]:
    directory = root / "visual-evidence"
    return [
        {"path": path.relative_to(root).as_posix(), "sha256": sha256(path), "bytes": path.stat().st_size}
        for path in sorted(directory.rglob("*")) if path.is_file() and not path.is_symlink()
    ] if directory.is_dir() and not directory.is_symlink() else []


def visual_inventory_valid(root: Path, receipt: Mapping[str, object], inventory: object) -> bool:
    if not isinstance(inventory, list) or not inventory or inventory != visual_inventory(root):
        return False
    declared = receipt.get("artifacts")
    if not isinstance(declared, Mapping):
        return False
    for relative, digest in declared.items():
        path = root / "visual-evidence" / str(relative)
        if not path.is_file() or path.is_symlink() or sha256(path) != digest:
            return False
    return len(declared) + 1 == len(inventory)  # receipt itself is the one extra inventory member


def _issue(code: str, detail: str = "") -> dict[str, str]:
    return {"code": code, "severity": "BLOCK", "detail": detail}


def audit_fastlane_c2_formal_package(root: Path) -> list[dict[str, str]]:
    manifest = _json(root / "review_manifest.json")
    from .fastlane_c2_release_bridge import is_fastlane_c2_release_manifest
    if is_fastlane_c2_release_manifest(manifest):
        from .fastlane_c2_release_bridge import audit_fastlane_c2_release_package
        return audit_fastlane_c2_release_package(root)
    issues: list[dict[str, str]] = []
    required = {"schema_version", "candidate_id", "title", "upload_allowed", "artifacts", "record", "publish", "scope", "visual_evidence_inventory"}
    if set(manifest) != required or not is_fastlane_c2_formal_manifest(manifest) or manifest.get("title") != TITLE or manifest.get("upload_allowed") is not False or manifest.get("scope") != "C2_NAMED_FASTLANE_REPAIR_ONLY":
        return [_issue("C2_FORMAL_MANIFEST_INVALID")]
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(NAMES):
        return [_issue("C2_FORMAL_ARTIFACT_SET_INVALID")]
    paths: dict[str, Path] = {}
    for key, name in NAMES.items():
        value = artifacts.get(key)
        path = root / name
        if not isinstance(value, Mapping) or value.get("path") != name or value.get("sha256") != sha256(path) if path.is_file() else True:
            issues.append(_issue("C2_FORMAL_ARTIFACT_DRIFT", key))
        elif path.is_symlink():
            issues.append(_issue("C2_FORMAL_SYMLINK", key))
        else:
            paths[key] = path
    if issues:
        return issues
    if paths["title"].read_text(encoding="utf-8") != TITLE + "\n":
        issues.append(_issue("C2_TITLE_DRIFT"))
    srt = paths["srt"].read_text(encoding="utf-8")
    ass = paths["ass"].read_text(encoding="utf-8")
    if srt.count("\n\n") != 21 or "00:00:08,720 --> 00:00:11,240\n小豆老公；； 不是你老公" not in srt or "00:00:56,080 --> 00:00:58,860\n小豆哪有好吵" not in srt:
        issues.append(_issue("C2_SRT_NAMED_REPAIR_DRIFT"))
    if "0:00:08.72,0:00:11.24,Default,,0,0,0,,小豆老公；； 不是你老公" not in ass or "0:00:56.08,0:00:58.86,Default,,0,0,0,,小豆哪有好吵" not in ass:
        issues.append(_issue("C2_ASS_NAMED_REPAIR_DRIFT"))
    graph, cover, visual = _json(paths["graph"]), _json(paths["cover_meta"]), _json(paths["visual"])
    rows = graph.get("rows") if isinstance(graph, Mapping) else None
    if not isinstance(rows, list) or len(rows) != 22 or [r.get("cue") for r in rows if isinstance(r, Mapping) and r.get("disposition") == "OPERATOR_REPAIR"] != [5]:
        issues.append(_issue("C2_CUE_GRAPH_DRIFT"))
    pixels = cover.get("overlay", {}).get("rendered_text_pixels") if isinstance(cover.get("overlay"), Mapping) else None
    if not isinstance(pixels, Mapping) or cover.get("rendered_lines") != ["拔智齿求亲亲", "小豆：只会嘲笑"] or not verify_rendered_text_pixel_artifacts(pixels, final_cover_path=paths["cover"], pre_overlay_path=paths["pre"], mask_path=paths["mask"], font_path=Path(__file__).resolve().parents[2] / "assets/lidousha/fonts/ZCOOLKuaiLe-Regular.ttf", expected_pre_overlay_sha256=pixels.get("pre_overlay_sha256")):
        issues.append(_issue("C2_COVER_PIXEL_BINDING_DRIFT"))
    if visual.get("burned_video", {}).get("sha256") != sha256(paths["video"]) or visual.get("intro_offset_ms") != 5749 or len(visual.get("items") or []) != 2 or not visual_inventory_valid(root, visual, manifest.get("visual_evidence_inventory")):
        issues.append(_issue("C2_VISUAL_EVIDENCE_DRIFT"))
    decode = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-i", str(paths["video"]), "-f", "null", "-"], capture_output=True, text=True, check=False)
    if decode.returncode:
        issues.append(_issue("C2_BURNED_DECODE_FAILED", decode.stderr[-240:]))
    return issues
