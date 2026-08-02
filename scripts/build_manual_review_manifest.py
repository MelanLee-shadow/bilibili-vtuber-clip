#!/usr/bin/env python3
"""Build review_manifest.json for ONE manual produce_slice_package delivery dir.

审计/上传闭环此前只对 runner 车道开放：auditor 要包内 review_manifest.json，
而两个 manifest 生成器都强制要 runner 的 state——`produce_slice_package` 直产
的包拿不到 state，除非伪造。本工具是 daily builder 的手动车道对应物，原则
完全一致：**字段全部 hash-bound 自包内真实产物（record/publish），不发明任何
值**；state 见证由**显式操作者署名**替代（--operator/--note 必填并入
manifest，谁裁定的、为什么，写清楚）。上传授权不因此扩大半分：manifest 恒
`upload_allowed=false`，投稿仍必须走 authorized_upload make-manifest 的
--quote 层绑定维护者原话 + 出版登记门。

手动包的平铺命名（与 runner 的 <cid>.recut.* 不同）：
  <stem>.mp4（烧录成品）/ .publish.json / .record.json / .srt /
  .final-sapphire72.ass / .chat-authority.json / .clip-context.json /
  .cover.png + .cover.pre-overlay.png / .cover.ai-bg.png / .cover.title-mask.png
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_lidousha_daily_review_manifest import (  # noqa: E402
    DailyManifestError,
    _candidate_lane,
    _lane_manifest_contract_fields,
    _sha256,
)


def _need(package_root: Path, name: str) -> Path:
    path = package_root / name
    if not path.is_file():
        raise DailyManifestError(f"required package file missing: {name}")
    return path


def _verified_cover_artifact(
    package_root: Path,
    *,
    flat_name: str,
    declared_path: object,
    declared_sha: object,
    label: str,
) -> str:
    """Resolve one cover evidence artifact: flat copy first, declared source fallback."""

    expected = str(declared_sha or "").removeprefix("sha256:")
    if not expected:
        raise DailyManifestError(f"cover generation lacks {label} hash")
    flat = package_root / flat_name
    if flat.is_file() and _sha256(flat) == expected:
        return flat_name
    source = Path(str(declared_path or ""))
    if source.is_symlink() or not source.is_file():
        raise DailyManifestError(f"cover {label} source missing or invalid: {source}")
    if _sha256(source) != expected:
        raise DailyManifestError(f"cover {label} source sha drift")
    flat.write_bytes(source.read_bytes())
    return flat_name


def build_manual(
    package_root: Path, *, operator: str, note: str
) -> dict:
    package_root = package_root.resolve()
    if not package_root.is_dir():
        raise DailyManifestError(f"package root missing: {package_root}")
    if not operator.strip() or not note.strip():
        raise DailyManifestError("operator attestation requires --operator and --note")
    publish_paths = sorted(
        path
        for path in package_root.glob("*.publish.json")
        if not path.name.endswith(".recut.publish.json")
    )
    if len(publish_paths) != 1:
        raise DailyManifestError(
            f"manual package must contain exactly one publish draft, found {len(publish_paths)}"
        )
    publish = publish_paths[0]
    stem = publish.name[: -len(".publish.json")]
    publish_doc = json.loads(publish.read_text(encoding="utf-8"))
    if publish_doc.get("schema_version") != "shadow-publish-draft.v1":
        raise DailyManifestError("package publish draft schema mismatch")
    if publish_doc.get("upload_enabled") is not False:
        raise DailyManifestError("package publish draft must keep upload_enabled false")
    candidate_id = str(publish_doc.get("candidate_id") or "")
    title = str(publish_doc.get("title") or "")
    if not candidate_id or not title:
        raise DailyManifestError("package publish draft lacks candidate/title binding")
    if str(publish_doc.get("cover_status") or "") != "AI_COVER_READY":
        raise DailyManifestError(
            "package cover is not AI_COVER_READY — repair the cover first "
            "(regenerate_channel_cover --bind-package writes it back)"
        )
    cover_generation = (
        publish_doc.get("cover_generation")
        or (publish_doc.get("publish_staging") or {}).get("cover_generation")
        or {}
    )
    if not isinstance(cover_generation, dict):
        raise DailyManifestError("cover generation is not an object")

    burned = _need(package_root, f"{stem}.mp4")
    record = _need(package_root, f"{stem}.record.json")
    record_doc = json.loads(record.read_text(encoding="utf-8"))
    subtitle = _need(package_root, f"{stem}.srt")
    ass = _need(package_root, f"{stem}.final-sapphire72.ass")
    chat_authority = _need(package_root, f"{stem}.chat-authority.json")
    clip_context = _need(package_root, f"{stem}.clip-context.json")
    # manifest.date 是 auditor 对 clip-context 日期绑定的比对面：从包内
    # hash-bound 的 clip-context 原样取（不发明值），缺失即拒。
    clip_context_doc = json.loads(clip_context.read_text(encoding="utf-8"))
    recording_date = str(clip_context_doc.get("recording_date") or "")
    if not recording_date:
        raise DailyManifestError("package clip context lacks recording_date")
    cover = _need(package_root, f"{stem}.cover.png")
    declared_cover_sha = str(
        cover_generation.get("final_cover_sha256") or ""
    ).removeprefix("sha256:")
    if declared_cover_sha and _sha256(cover) != declared_cover_sha:
        raise DailyManifestError(
            "package cover bytes do not match generation final_cover_sha256"
        )

    lane = _candidate_lane(record_doc, publish_doc)
    lane_manifest_contract_fields = _lane_manifest_contract_fields(
        lane=lane,
        record_doc=record_doc,
        publish_doc=publish_doc,
        subtitle_path=subtitle,
    )
    cover_pre_overlay = _verified_cover_artifact(
        package_root,
        flat_name=f"{stem}.cover.pre-overlay.png",
        declared_path=cover_generation.get("pre_overlay_path"),
        declared_sha=cover_generation.get("pre_overlay_sha256"),
        label="pre-overlay",
    )
    cover_route_background = _verified_cover_artifact(
        package_root,
        flat_name=f"{stem}.cover.ai-bg.png",
        declared_path=cover_generation.get("ai_background"),
        declared_sha=cover_generation.get("ai_background_sha256"),
        label="route background",
    )
    rendered_text_pixels = cover_generation.get("rendered_text_pixels")
    if not isinstance(rendered_text_pixels, dict):
        raise DailyManifestError("cover generation lacks rendered_text_pixels")
    cover_title_mask = _verified_cover_artifact(
        package_root,
        flat_name=f"{stem}.cover.title-mask.png",
        declared_path=rendered_text_pixels.get("mask_path"),
        declared_sha=rendered_text_pixels.get("mask_sha256"),
        label="title mask",
    )

    item = {
        "id": candidate_id,
        "candidate_id": candidate_id,
        "stem": stem,
        "kind": lane,
        "classification": lane,
        "title": title,
        "subtitle_srt": subtitle.name,
        "publish_json": publish.name,
        "evidence_json": record.name,
        "video": burned.name,
        "cover": cover.name,
        "cover_pre_overlay": cover_pre_overlay,
        "cover_title_mask": cover_title_mask,
        "cover_route_background": cover_route_background,
        "record": record.name,
        "chat_authority": chat_authority.name,
        "clip_context": clip_context.name,
        "ass_path": ass.name,
        "ass_sha256": _sha256(ass),
        "speaker_srt": subtitle.name,
        "speaker_srt_sha256": _sha256(subtitle),
        "sha256": {
            "subtitle_srt": _sha256(subtitle),
            "publish_json": _sha256(publish),
            "evidence_json": _sha256(record),
            "video": _sha256(burned),
            "cover": _sha256(cover),
        },
    }
    attestation = {
        "candidate_id": candidate_id,
        "reference_sha256": cover_generation.get("reference_sha256"),
        "final_cover_sha256": cover_generation.get("final_cover_sha256"),
        "method": cover_generation.get("method"),
        "route_decision": cover_generation.get("route_decision"),
        "reference_authority": cover_generation.get("reference_authority"),
    }
    manifest = {
        "schema_version": "lidousha-manual-review-manifest.v1",
        "generated_by": "build_manual_review_manifest.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "review_ready",
        "candidate_id": candidate_id,
        "date": recording_date,
        "run_mode": "MANUAL_PRODUCE_REVIEW",
        # 手动车道没有 runner state 见证：由显式操作者署名替代，谁裁定、为何。
        "manual_attestation": {
            "operator": operator.strip(),
            "note": note.strip(),
            "spec_lane": "produce_slice_package",
        },
        # 审片包本身不授权上传；上传授权由 authorized_upload make-manifest
        # 的 --quote 层绑定维护者原话 + 出版登记门。
        "upload_allowed": False,
        "subtitle_visual_contract": {
            "max_visual_lines": 2,
            "max_chars_per_line": 28,
        },
        "cover_route_attestations": [attestation],
        "items": [item],
    }
    manifest.update(lane_manifest_contract_fields)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--operator", required=True, help="裁定人（写进 manifest）")
    parser.add_argument(
        "--note", required=True, help="一句话：为什么这个手动包可以进入评审"
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    try:
        manifest = build_manual(
            args.package_root, operator=args.operator, note=args.note
        )
    except DailyManifestError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    out = args.out or (args.package_root / "review_manifest.json")
    out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"manifest": str(out), "candidate_id": manifest["candidate_id"], "status": manifest["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
