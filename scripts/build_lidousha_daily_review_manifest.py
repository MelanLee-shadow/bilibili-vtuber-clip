"""Build a review_manifest.json for ONE daily-lane candidate package.

7/24 权宜上传缺口（2026-07-25）：v3 上传合同要求 package audit，audit 要求
包内 review_manifest.json，而 manifest 生成器只有 recovery 版（绑定 exact
contract/v7 rerun plan/upload_allowed=false 全套 recovery state 结构）——
daily 新 BV 上传车道自 7/14 后没有合法的 manifest 生成器。

本工具是 recovery builder 的 daily 对应物：字段全部 hash-bound 自真实产物
（record/publish/state），不发明任何值：

- 候选自身必须 state 里 rc=0 且 review_ready（批级 with_failures 可接受——
  Ivan 2026-07-25 行军令：能传的先传，不因批内其他候选失败扣押好片）；
- items 路径指向包内真实文件并带 sha256；
- 不声明 story_contract_required/upload_allowed（daily 新上传不是 recovery
  same-BV 修复，也不承诺 upload_allowed=false——上传授权由 authorized_upload
  的 make-manifest --quote 层绑定 Ivan 原话）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path


class DailyManifestError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(package_root: Path, state_path: Path, deployed_commit_file: Path,
          candidate_id: str) -> dict:
    package_root = package_root.resolve()
    if not package_root.is_dir():
        raise DailyManifestError(f"package root missing: {package_root}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    batch_status = str(state.get("status") or "")
    if batch_status not in {"review_ready", "review_ready_with_failures"}:
        raise DailyManifestError(
            f"batch status not reviewable: {batch_status}"
        )
    pick = next(
        (row for row in state.get("picks") or []
         if row.get("candidate_id") == candidate_id),
        None,
    )
    if pick is None:
        raise DailyManifestError(f"candidate not in state: {candidate_id}")
    if pick.get("status") != "review_ready" or pick.get("rc") != 0:
        raise DailyManifestError(
            f"candidate not individually green: status={pick.get('status')} rc={pick.get('rc')}"
        )
    deployed_commit = deployed_commit_file.read_text(encoding="utf-8").split()[0]

    stem = f"{candidate_id}.recut"
    def need(name: str) -> Path:
        path = package_root / name
        if not path.is_file():
            raise DailyManifestError(f"required package file missing: {name}")
        return path

    publish = need(f"{stem}.publish.json")
    publish_doc = json.loads(publish.read_text(encoding="utf-8"))
    record = need(f"{candidate_id}.record.json")
    subtitle = need(f"{stem}.srt")
    burned = need(f"{stem}.burned-final-sapphire72.mp4")
    cover_rel = None
    for candidate in (
        f"covers/{candidate_id}.ai-title.cover.png",
        f"covers/{candidate_id}.screenshot-title.cover.png",
    ):
        if (package_root / candidate).is_file():
            cover_rel = candidate
            break
    if cover_rel is None:
        raise DailyManifestError("no final cover found under covers/")

    ass = need(f"{stem}.final-sapphire72.ass")
    chat_name = f"{candidate_id}.chat-authority.json"
    chat_in_pkg = package_root / chat_name
    if not chat_in_pkg.is_file():
        # 装配步骤：chat authority 是 candidate 根目录的冻结产物，包自足性
        # （portable audit）要求它在包内。只在 record 声明的 sha 匹配时才
        # 复制——不匹配说明产物漂移，fail-closed。
        source = package_root.parent / chat_name
        if not source.is_file():
            raise DailyManifestError(f"chat authority missing: {source}")
        record_doc = json.loads(record.read_text(encoding="utf-8"))
        declared = str(
            (record_doc.get("artifact_hashes") or {}).get(
                "chat_authority_audit_sha256"
            )
            or ""
        ).removeprefix("sha256:")
        actual = _sha256(source)
        if actual != declared:
            raise DailyManifestError(
                f"chat authority sha drift: record={declared} actual={actual}"
            )
        chat_in_pkg.write_bytes(source.read_bytes())
    cover_generation = (
        publish_doc.get("cover_generation")
        or (publish_doc.get("publish_staging") or {}).get("cover_generation")
        or {}
    )
    def cover_artifact(generation_key: str, sha_key: str) -> str:
        """Locate a package-internal cover artifact declared by generation.

        The publish document records host staging paths; the package carries
        the same bytes under covers*/. Resolution is by basename and the
        bytes must match the generation-declared sha — drift is refused.
        """
        declared = cover_generation.get(generation_key)
        expected = str(cover_generation.get(sha_key) or "").removeprefix(
            "sha256:"
        )
        if not isinstance(declared, str) or not declared or not expected:
            raise DailyManifestError(
                f"cover generation lacks {generation_key}/{sha_key}"
            )
        basename = Path(declared).name
        for parent in ("covers", "covers_ai_original", "cover_refs"):
            candidate_path = package_root / parent / basename
            if candidate_path.is_file():
                if _sha256(candidate_path) != expected:
                    raise DailyManifestError(
                        f"cover artifact sha drift: {parent}/{basename}"
                    )
                return f"{parent}/{basename}"
        raise DailyManifestError(
            f"cover artifact missing from package: {basename}"
        )

    cover_pre_overlay = cover_artifact("pre_overlay_path", "pre_overlay_sha256")
    cover_route_background = cover_artifact(
        "ai_background", "ai_background_sha256"
    )
    mask_rel = str(Path(cover_pre_overlay).parent / (
        Path(cover_pre_overlay).name.replace(
            ".pre-overlay.png", ".title-mask.png"
        )
    ))
    if not (package_root / mask_rel).is_file():
        raise DailyManifestError(f"title mask missing: {mask_rel}")

    item = {
        "id": candidate_id,
        "candidate_id": candidate_id,
        "stem": stem,
        "kind": "talk",
        "title": str(publish_doc.get("title") or ""),
        "subtitle_srt": subtitle.name,
        "publish_json": publish.name,
        "evidence_json": record.name,
        "video": burned.name,
        "cover": cover_rel,
        "cover_pre_overlay": cover_pre_overlay,
        "cover_title_mask": mask_rel,
        "cover_route_background": cover_route_background,
        "record": record.name,
        "chat_authority": chat_name,
        "ass_path": ass.name,
        "ass_sha256": _sha256(ass),
        # uniform_host 政策（ee29e08）：单说话人包的 speaker 面与正文同体；
        # finalization 的 final_speaker_srt_sha256 即正文 srt 的 sha（已核）。
        "speaker_srt": subtitle.name,
        "speaker_srt_sha256": _sha256(subtitle),
        "sha256": {
            "subtitle_srt": _sha256(subtitle),
            "publish_json": _sha256(publish),
            "evidence_json": _sha256(record),
            "video": _sha256(burned),
            "cover": _sha256(package_root / cover_rel),
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
        "schema_version": "lidousha-daily-review-manifest.v1",
        "generated_by": "build_lidousha_daily_review_manifest.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "date": str(state.get("date") or state_path.stem),
        "status": "review_ready",
        "batch_status": batch_status,
        "candidate_id": candidate_id,
        "deployed_commit": deployed_commit,
        "state_path": str(state_path),
        "run_mode": "PRODUCTION_REVIEW",
        # 审片包本身不授权上传；上传授权由 authorized_upload make-manifest
        # 的 --quote 层绑定 Ivan 原话。
        "upload_allowed": False,
        # Sapphire72 车道显式声明 28 字合同（audit docstring 指定该车道必须
        # 显式声明，否则被旧手工包 18 字默认误拒）。
        "subtitle_visual_contract": {
            "max_visual_lines": 2,
            "max_chars_per_line": 28,
        },
        "cover_route_attestations": [attestation],
        "items": [item],
    }
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--deployed-commit-file", required=True, type=Path)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    try:
        manifest = build(args.package_root, args.state,
                         args.deployed_commit_file, args.candidate)
    except DailyManifestError as exc:
        print(f"REFUSE: {exc}")
        return 2
    out = args.out or args.package_root / "review_manifest.json"
    out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"WROTE {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
