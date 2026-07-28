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


def _sync_declared_artifact(
    *,
    target: Path,
    source: Path,
    declared_sha256: str,
    label: str,
) -> None:
    """Copy the record-bound candidate artifact into the portable package.

    A previous package assembly may have left an older file at ``target``.
    Presence alone is therefore not proof that the package carries the bytes
    declared by the current record.  Validate the candidate-root source first,
    then replace a missing or stale package copy deterministically.
    """
    expected = str(declared_sha256 or "").removeprefix("sha256:")
    if not source.is_file():
        raise DailyManifestError(f"{label} missing: {source}")
    actual = _sha256(source)
    if not expected or actual != expected:
        raise DailyManifestError(
            f"{label} sha drift: record={expected} actual={actual}"
        )
    if not target.is_file() or _sha256(target) != actual:
        target.write_bytes(source.read_bytes())


def _resolve_final_cover(
    *,
    package_root: Path,
    pick: dict,
    cover_generation: dict,
) -> str:
    """Resolve only the exact final cover declared by state and record."""
    declared = cover_generation.get("final_cover")
    expected = str(
        cover_generation.get("final_cover_sha256") or ""
    ).removeprefix("sha256:")
    pick_path = pick.get("cover_path")
    pick_expected = str(pick.get("cover_sha256") or "").removeprefix(
        "sha256:"
    )
    if not isinstance(declared, str) or not declared or not expected:
        raise DailyManifestError(
            "cover generation lacks final_cover/final_cover_sha256"
        )
    if not isinstance(pick_path, str) or not pick_path or not pick_expected:
        raise DailyManifestError("state pick lacks cover_path/cover_sha256")

    basename = Path(declared).name
    if Path(pick_path).name != basename or pick_expected != expected:
        raise DailyManifestError(
            "state/record final cover binding drift: "
            f"state={Path(pick_path).name}:{pick_expected} "
            f"record={basename}:{expected}"
        )
    relative = f"covers/{basename}"
    final_cover = package_root / relative
    if not final_cover.is_file():
        raise DailyManifestError(f"final cover missing from package: {relative}")
    actual = _sha256(final_cover)
    if actual != expected:
        raise DailyManifestError(
            f"final cover sha drift: record={expected} actual={actual}"
        )
    return relative


def _sync_record_bound_candidate_artifacts(
    *,
    package_root: Path,
    candidate_id: str,
    record_doc: dict,
) -> dict[str, str]:
    """Make candidate-root evidence portable under its record hashes."""
    artifact_hashes = record_doc.get("artifact_hashes") or {}
    artifacts = {
        "chat_authority": (
            f"{candidate_id}.chat-authority.json",
            artifact_hashes.get("chat_authority_audit_sha256"),
        ),
        "clip_context": (
            f"{candidate_id}.clip-context.json",
            artifact_hashes.get("clip_context_file_sha256"),
        ),
    }
    resolved: dict[str, str] = {}
    for label, (name, declared_sha256) in artifacts.items():
        _sync_declared_artifact(
            target=package_root / name,
            source=package_root.parent / name,
            declared_sha256=str(declared_sha256 or ""),
            label=label.replace("_", " "),
        )
        resolved[label] = name
    return resolved


def build(package_root: Path, state_path: Path, deployed_commit_file: Path,
          candidate_id: str) -> dict:
    package_root = package_root.resolve()
    if not package_root.is_dir():
        raise DailyManifestError(f"package root missing: {package_root}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    batch_status = str(state.get("status") or "")
    # retry_wait 只表示批内其他 pick 还有重试预算；review_ready pick 的产物
    # 已冻结（主车道从不 re-supersede review_ready）。processing 仍然拒绝：
    # runner 正在写 picks。
    if batch_status not in {
        "review_ready",
        "review_ready_with_failures",
        "review_ready_retry_wait",
    }:
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
    cover_generation = (
        publish_doc.get("cover_generation")
        or (publish_doc.get("publish_staging") or {}).get("cover_generation")
        or {}
    )
    if not isinstance(cover_generation, dict):
        raise DailyManifestError("cover generation is not an object")
    record = need(f"{candidate_id}.record.json")
    record_doc = json.loads(record.read_text(encoding="utf-8"))
    subtitle = need(f"{stem}.srt")
    burned = need(f"{stem}.burned-final-sapphire72.mp4")
    cover_rel = _resolve_final_cover(
        package_root=package_root,
        pick=pick,
        cover_generation=cover_generation,
    )

    ass = need(f"{stem}.final-sapphire72.ass")
    # 装配步骤：candidate-root 的冻结证据即使在包内已有旧副本，也必须
    # 重新对照当前 record 声明同步，避免远端 auditor 偷读包外文件。
    portable_evidence = _sync_record_bound_candidate_artifacts(
        package_root=package_root,
        candidate_id=candidate_id,
        record_doc=record_doc,
    )
    chat_name = portable_evidence["chat_authority"]
    clip_context_name = portable_evidence["clip_context"]
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

    # authorized_upload v3 的 same-stem 合同：video stem X 要求包根直下
    # X.record.json / X.srt / X.cover.png。装配为审定字节的副本（字节级
    # 相同，sha 与 chat/record 冻结值天然一致），manifest item 指向副本。
    upload_stem = burned.name.removesuffix(".mp4")
    upload_family = {
        f"{upload_stem}.record.json": record,
        f"{upload_stem}.srt": subtitle,
        f"{upload_stem}.cover.png": package_root / cover_rel,
    }
    for name, source_path in upload_family.items():
        target = package_root / name
        payload = source_path.read_bytes()
        if not (target.is_file() and target.read_bytes() == payload):
            target.write_bytes(payload)

    item = {
        "id": candidate_id,
        "candidate_id": candidate_id,
        "stem": stem,
        "kind": "talk",
        "title": str(publish_doc.get("title") or ""),
        "subtitle_srt": f"{upload_stem}.srt",
        "publish_json": publish.name,
        "evidence_json": record.name,
        "video": burned.name,
        "cover": f"{upload_stem}.cover.png",
        "cover_pre_overlay": cover_pre_overlay,
        "cover_title_mask": mask_rel,
        "cover_route_background": cover_route_background,
        "record": f"{upload_stem}.record.json",
        "chat_authority": chat_name,
        "clip_context": clip_context_name,
        "ass_path": ass.name,
        "ass_sha256": _sha256(ass),
        # uniform_host 政策（ee29e08）：单说话人包的 speaker 面与正文同体；
        # finalization 的 final_speaker_srt_sha256 即正文 srt 的 sha（已核）。
        "speaker_srt": f"{upload_stem}.srt",
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
