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
  单人车道 .final-sapphire72.ass，或说话人车道 .speaker.srt +
  .speaker.ass / .chat-authority.json / .clip-context.json /
  .cover.png + .cover.pre-overlay.png / .cover.ai-bg.png / .cover.title-mask.png
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_lidousha_daily_review_manifest import (  # noqa: E402
    DailyManifestError,
    _candidate_lane,
    _lane_manifest_contract_fields,
    _package_regular_file,
    _rebuild_package_speaker_evidence,
    _sha256,
    _sync_record_bound_candidate_artifacts,
)
from src.autoslice.review_package_ass_audit import (  # noqa: E402
    uniform_host_fallback_declared,
)
from src.autoslice.recovery_title_authority import (  # noqa: E402
    RecoveryTitleAuthorityError,
    validate_recovery_publication_authority,
)
from src.autoslice.qixi_corrected_package_finalization import (  # noqa: E402
    QixiCorrectedPackageError,
    validate_applied_receipt,
)
from src.autoslice.qixi_cover_successor_finalization import (  # noqa: E402
    QixiCoverSuccessorError,
    RECEIPT as QIXI_SUCCESSOR_RECEIPT,
    validate_applied_receipt as validate_qixi_successor_receipt,
)


def _need(package_root: Path, name: str) -> Path:
    path = _package_regular_file(
        package_root,
        name,
        label="required package artifact",
    )
    if path is None:
        raise DailyManifestError(f"required package file missing: {name}")
    return path


def _require_frozen_typed_receipt(
    *,
    package_root: Path,
    receipt_name: str,
    expected_bytes: bytes | None,
    expected_sha256: str | None,
) -> None:
    """Reject a Qixi receipt swapped after its sealed pre-write replay."""

    if expected_bytes is None or expected_sha256 is None:
        return
    try:
        current = _need(package_root, receipt_name).read_bytes()
    except (OSError, DailyManifestError) as exc:
        raise DailyManifestError("manual corrected same-BV receipt drifted during packaging") from exc
    current_sha256 = "sha256:" + hashlib.sha256(current).hexdigest()
    if current != expected_bytes or current_sha256 != expected_sha256:
        raise DailyManifestError("manual corrected same-BV receipt drifted during packaging")


@dataclass(frozen=True)
class _PreparedQixiGate:
    receipt_name: str
    receipt_present: bool
    receipt: dict | None
    receipt_bytes: bytes | None
    receipt_sha256: str | None
    publication_authority: object
    chat_name: str
    clip_name: str
    successor_receipt: bool = False


def _prepare_qixi_gate(
    *,
    package_root: Path,
    stem: str,
    candidate_id: str,
    title: str,
    record_doc: dict,
    publish_doc: dict,
    qixi_repo_root: Path | None,
) -> _PreparedQixiGate:
    """Preflight a typed Qixi receipt before candidate evidence can be copied."""

    successor_path = _package_regular_file(
        package_root, QIXI_SUCCESSOR_RECEIPT, label="Qixi cover successor receipt"
    )
    if successor_path is not None:
        try:
            successor_bytes = successor_path.read_bytes()
            successor = json.loads(successor_bytes.decode("utf-8"))
            validate_qixi_successor_receipt(
                successor,
                package_root=package_root,
                repo_root=qixi_repo_root,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, QixiCoverSuccessorError) as exc:
            raise DailyManifestError("Qixi cover successor receipt is unreadable") from exc
        publication_authority = record_doc.get("recovery_publication_authority")
        staging = record_doc.get("publish_staging")
        publish_authority = publish_doc.get("recovery_publication_authority")
        staging_authority = staging.get("recovery_publication_authority") if isinstance(staging, dict) else None
        if not (publication_authority is not None and publication_authority == staging_authority == publish_authority):
            raise DailyManifestError("Qixi successor publication authority drifts")
        try:
            publication_authority = validate_recovery_publication_authority(publication_authority, candidate_id=candidate_id, expected_final_title=title)
        except RecoveryTitleAuthorityError as exc:
            raise DailyManifestError(f"Qixi successor publication authority invalid: {exc}") from exc
        portable = _sync_record_bound_candidate_artifacts(package_root=package_root, candidate_id=candidate_id, record_doc=record_doc)
        return _PreparedQixiGate(receipt_name=QIXI_SUCCESSOR_RECEIPT, receipt_present=True, receipt=successor, receipt_bytes=successor_bytes, receipt_sha256="sha256:" + hashlib.sha256(successor_bytes).hexdigest(), publication_authority=publication_authority, chat_name=portable["chat_authority"], clip_name=portable["clip_context"], successor_receipt=True)

    receipt_name = "qixi-corrected-package-finalization.json"
    typed_receipt_path = _package_regular_file(
        package_root, receipt_name, label="Qixi corrected finalization receipt"
    )
    receipt: dict | None = None
    receipt_bytes: bytes | None = None
    receipt_sha256: str | None = None
    if typed_receipt_path is not None:
        try:
            receipt_bytes = typed_receipt_path.read_bytes()
            receipt = json.loads(receipt_bytes.decode("utf-8"))
            validate_applied_receipt(
                receipt, package_root=package_root, repo_root=qixi_repo_root
            )
            receipt_sha256 = "sha256:" + hashlib.sha256(receipt_bytes).hexdigest()
        except (OSError, UnicodeError, json.JSONDecodeError, QixiCorrectedPackageError) as exc:
            raise DailyManifestError("manual corrected same-BV receipt is unreadable") from exc

    publication_authority = record_doc.get("recovery_publication_authority")
    staging = record_doc.get("publish_staging")
    publish_authority = publish_doc.get("recovery_publication_authority")
    staging_authority = (
        staging.get("recovery_publication_authority") if isinstance(staging, dict) else None
    )
    if any(value is not None for value in (publication_authority, staging_authority, publish_authority)):
        if not (
            publication_authority is not None
            and publication_authority == staging_authority == publish_authority
        ):
            raise DailyManifestError("recovery publication authority drifts across package surfaces")
        try:
            publication_authority = validate_recovery_publication_authority(
                publication_authority,
                candidate_id=candidate_id,
                expected_final_title=title,
            )
        except RecoveryTitleAuthorityError as exc:
            raise DailyManifestError(f"recovery publication authority is invalid: {exc}") from exc

    artifact_hashes = record_doc.get("artifact_hashes")
    if typed_receipt_path is not None and isinstance(artifact_hashes, dict) and {
        "chat_authority_audit_sha256",
        "clip_context_file_sha256",
    }.issubset(artifact_hashes):
        portable_evidence = _sync_record_bound_candidate_artifacts(
            package_root=package_root,
            candidate_id=candidate_id,
            record_doc=record_doc,
        )
        chat_name = portable_evidence["chat_authority"]
        clip_name = portable_evidence["clip_context"]
        _require_frozen_typed_receipt(
            package_root=package_root,
            receipt_name=receipt_name,
            expected_bytes=receipt_bytes,
            expected_sha256=receipt_sha256,
        )
    else:
        chat_name = f"{stem}.chat-authority.json"
        clip_name = f"{stem}.clip-context.json"
    return _PreparedQixiGate(
        receipt_name=receipt_name,
        receipt_present=typed_receipt_path is not None,
        receipt=receipt,
        receipt_bytes=receipt_bytes,
        receipt_sha256=receipt_sha256,
        publication_authority=publication_authority,
        chat_name=chat_name,
        clip_name=clip_name,
    )


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
    package_root: Path,
    *,
    operator: str,
    note: str,
    qixi_repo_root: Path | None = None,
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
    qixi_gate = _prepare_qixi_gate(
        package_root=package_root,
        stem=stem,
        candidate_id=candidate_id,
        title=title,
        record_doc=record_doc,
        publish_doc=publish_doc,
        qixi_repo_root=qixi_repo_root,
    )
    chat_authority = _need(package_root, qixi_gate.chat_name)
    try:
        chat_authority_doc = json.loads(chat_authority.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DailyManifestError("package chat authority is unreadable or invalid") from exc
    uniform_fallback = uniform_host_fallback_declared(record_doc, chat_authority_doc)
    if uniform_fallback:
        ass = _need(package_root, f"{stem}.final-sapphire72.ass")
        speaker_srt = subtitle
    else:
        ass = _need(package_root, f"{stem}.speaker.ass")
        speaker_srt = _need(package_root, f"{stem}.speaker.srt")
    clip_context = _need(package_root, qixi_gate.clip_name)
    # manifest.date 是 auditor 对 clip-context 日期绑定的比对面：从包内
    # hash-bound 的 clip-context 原样取（不发明值），缺失即拒。
    clip_context_doc = json.loads(clip_context.read_text(encoding="utf-8"))
    recording_date = str(clip_context_doc.get("recording_date") or "")
    if not recording_date:
        raise DailyManifestError("package clip context lacks recording_date")
    cover = _need(package_root, f"{stem}.cover.png")
    declared_cover_sha = str(cover_generation.get("final_cover_sha256") or "").removeprefix(
        "sha256:"
    )
    if declared_cover_sha and _sha256(cover) != declared_cover_sha:
        raise DailyManifestError("package cover bytes do not match generation final_cover_sha256")

    # A manual package can be the reviewed replacement for an already-public
    # BV.  Do not silently discard that identity: when introduced, the
    # authority is a five-surface exact contract (record/staging/publish/item
    # and manifest map) and is replayed before this builder writes anything.
    publication_authority = qixi_gate.publication_authority
    corrected_receipt = None
    corrected_receipt_path: Path | None = None
    receipt_candidate = package_root / qixi_gate.receipt_name
    if (receipt_candidate.exists() or receipt_candidate.is_symlink()) and not qixi_gate.successor_receipt:
        corrected_receipt_path = _need(package_root, qixi_gate.receipt_name)
        try:
            raw_receipt = qixi_gate.receipt
            if raw_receipt is None:
                raise QixiCorrectedPackageError("receipt appeared after pre-write validation")
            corrected_receipt = raw_receipt["manual_corrected_same_bv"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, QixiCorrectedPackageError) as exc:
            raise DailyManifestError("manual corrected same-BV receipt is unreadable") from exc
        if (
            not isinstance(corrected_receipt, dict)
            or corrected_receipt.get("schema_version") != "manual-corrected-same-bv.v1"
            or corrected_receipt.get("candidate_id") != candidate_id
            or corrected_receipt.get("recovery_publication_authority") != publication_authority
            or str(corrected_receipt.get("approved_burned_video_sha256") or "").removeprefix("sha256:")
            != _sha256(burned)
            or str(corrected_receipt.get("approved_subtitle_sha256") or "").removeprefix("sha256:")
            != _sha256(subtitle)
            or str(corrected_receipt.get("approved_cover_sha256") or "").removeprefix("sha256:")
            != _sha256(cover)
        ):
            raise DailyManifestError("manual corrected same-BV receipt drifts from package")
    if publication_authority is not None and corrected_receipt is None and not qixi_gate.successor_receipt:
        raise DailyManifestError("manual same-BV package lacks corrected receipt")
    if corrected_receipt is not None and publication_authority is None:
        raise DailyManifestError("corrected receipt lacks recovery publication authority")

    _require_frozen_typed_receipt(
        package_root=package_root,
        receipt_name=qixi_gate.receipt_name,
        expected_bytes=qixi_gate.receipt_bytes,
        expected_sha256=qixi_gate.receipt_sha256,
    )
    lane = _candidate_lane(record_doc, publish_doc)
    packaged_speaker_manifest: Path | None = None
    if lane == "talk":
        speaker_evidence, packaged_speaker_manifest = _rebuild_package_speaker_evidence(
            package_root=package_root,
            record_doc=record_doc,
            subtitle_path=subtitle,
            speaker_srt_path=(None if uniform_fallback else speaker_srt),
        )
        lane_manifest_contract_fields = _lane_manifest_contract_fields(
            lane=lane,
            record_doc=record_doc,
            publish_doc=publish_doc,
            subtitle_path=subtitle,
            speaker_evidence=speaker_evidence,
            qixi_repo_root=qixi_repo_root,
        )
    else:
        # Song remains on its independent lyric/alignment evidence lane.
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
    _require_frozen_typed_receipt(
        package_root=package_root,
        receipt_name=qixi_gate.receipt_name,
        expected_bytes=qixi_gate.receipt_bytes,
        expected_sha256=qixi_gate.receipt_sha256,
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
    _require_frozen_typed_receipt(
        package_root=package_root,
        receipt_name=qixi_gate.receipt_name,
        expected_bytes=qixi_gate.receipt_bytes,
        expected_sha256=qixi_gate.receipt_sha256,
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
        "speaker_srt": speaker_srt.name,
        "speaker_srt_sha256": _sha256(speaker_srt),
        **(
            {"recovery_publication_authority": publication_authority}
            if publication_authority is not None
            else {}
        ),
        **(
            {
                "manual_corrected_same_bv": corrected_receipt,
                "manual_corrected_same_bv_receipt": corrected_receipt_path.name,
                "manual_corrected_same_bv_receipt_sha256": qixi_gate.receipt_sha256,
            }
            if corrected_receipt is not None and corrected_receipt_path is not None
            else {}
        ),
        **(
            {
                "qixi_cover_successor_finalization": qixi_gate.receipt_name,
                "qixi_cover_successor_finalization_sha256": qixi_gate.receipt_sha256,
            }
            if qixi_gate.successor_receipt
            else {}
        ),
        **(
            {
                "speaker_finalization_manifest": packaged_speaker_manifest.name,
                "speaker_finalization_manifest_sha256": (
                    "sha256:" + _sha256(packaged_speaker_manifest)
                ),
            }
            if packaged_speaker_manifest is not None
            else {}
        ),
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
    finalized_qixi_same_bv = (
        corrected_receipt is not None and corrected_receipt_path is not None
    )
    manifest = {
        "schema_version": "lidousha-manual-review-manifest.v1",
        "generated_by": "build_manual_review_manifest.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # A legacy/manual package remains an operator review artifact.  The
        # only exception is a Qixi package whose sealed finalizer receipt was
        # replayed above and is embedded in its sole item: that exact closure
        # may enter the still-no-upload final perceptual-review lane.
        "status": (
            "finished_review_package_no_upload_pending_human_review"
            if finalized_qixi_same_bv or qixi_gate.successor_receipt
            else "review_ready"
        ),
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
    if publication_authority is not None:
        manifest["recovery_publication_authorities_by_candidate"] = {
            candidate_id: publication_authority
        }
    if finalized_qixi_same_bv or qixi_gate.successor_receipt:
        manifest.update(
            {
                "exact_candidate_ids": [candidate_id],
                "selection_contract": {
                    "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
                    "candidate_ids": [candidate_id],
                },
            }
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--operator", required=True, help="裁定人（写进 manifest）")
    parser.add_argument("--note", required=True, help="一句话：为什么这个手动包可以进入评审")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    try:
        manifest = build_manual(args.package_root, operator=args.operator, note=args.note)
    except DailyManifestError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    out = args.out or (args.package_root / "review_manifest.json")
    out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "manifest": str(out),
                "candidate_id": manifest["candidate_id"],
                "status": manifest["status"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
