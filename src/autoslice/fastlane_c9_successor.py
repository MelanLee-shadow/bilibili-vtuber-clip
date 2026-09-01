"""C9-only private successor package with a provider-free PASS0 gate."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.fastlane_c9_joint_qc import (
    BASE,
    CID,
    COVER_SHA,
    DATE,
    HOOK_SHA,
    JOINT_QC_NAME,
    TITLE_SHA,
    validate_c9_joint_qc,
)

OLD_SUBTITLE = "sha256:4b0402dafbffbc9cf3358f21ff09a3d4aa07c052978848b70cc6b860844c2ac1"
OBSERVED_SUBTITLE = "sha256:3327cb186ba44785df255c544e5e7ac1ce989a3872ccb91958a7b43ba77823c7"
SOURCE_SRT = "sha256:65ae7dfd66348c4af7a8dbc78b1f196bd4cf354c181416a03fa346b9b4cab069"
REVIEWED_SRT = "sha256:f436e1913b8eb2bd948c37b18bce9c2a9970dd9be07cf114f309a9c162c2b51b"
VIDEO_SHA = "sha256:40025aa2a2727493207a0deea69013d2a8f5f9c49b29003627f398349a847e7e"
CLIP_CONTEXT_SHA = "sha256:ca38b6601e5401e35519cb76fc8e30764121e1300f7f404f92c1f645f1b0b00c"
BOUNDARY_AUDIT_SHA = "sha256:57dccc07218635f612a26b6f03a77b7a9c2688160085b36af6f9253238c4a05e"
CHAT_AUDIT_SHA = "sha256:61149fe0ef1e875a24ce0542c5f90ebdce2f04d5f2216a566969899f7b6c86b6"
SOURCE_FACT_RECEIPT_SHA = "sha256:da34883b4952d46356cc44fb6cb695105ff51f34943158f29d218f51b14934bd"
CHAT_CLOSURE_SCHEMA = "fastlane-c9-sealed-chat-closure.v1"


class FastlaneC9SuccessorError(ValueError):
    pass


def _sha(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def _canon(v: object) -> bytes:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _reseal(doc: dict[str, Any]) -> None:
    doc["self_sha256"] = "sha256:" + hashlib.sha256(_canon({k: v for k, v in doc.items() if k != "self_sha256"})).hexdigest()


def _regular(path: Path, *, label: str) -> Path:
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise FastlaneC9SuccessorError(f"C9_SUCCESSOR_{label}_UNSAFE")
    return path


def _json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_regular(path, label=label).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FastlaneC9SuccessorError(f"C9_SUCCESSOR_{label}_INVALID") from exc
    if not isinstance(value, dict):
        raise FastlaneC9SuccessorError(f"C9_SUCCESSOR_{label}_INVALID")
    return value


def _sealed_chat_closure(publish: Mapping[str, Any], clip_context: Mapping[str, Any]) -> dict[str, Any]:
    """Consume only the already sealed source-fact/structured-chat evidence."""
    review = publish.get("source_fact_review")
    artifact_hashes = publish.get("artifact_hashes")
    if not isinstance(review, Mapping) or not isinstance(artifact_hashes, Mapping) or artifact_hashes.get("chat_authority_audit_sha256") != CHAT_AUDIT_SHA or review.get("status") != "PASS" or review.get("receipt_sha256") != SOURCE_FACT_RECEIPT_SHA:
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_CHAT_AUTHORITY_UNAVAILABLE")
    passes = review.get("passes")
    if not isinstance(passes, list) or not passes or not isinstance(passes[-1], Mapping):
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_CHAT_AUTHORITY_UNAVAILABLE")
    final = passes[-1]
    if final.get("status") != "KEEP" or final.get("final_selection_hook") != (review.get("final_selection_hook")) or final.get("final_title") != review.get("final_title") or final.get("selection_scorecard_review", {}).get("status") != "COMPATIBLE" or "structured_chat" not in final.get("supported_by", []):
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_CHAT_AUTHORITY_UNAVAILABLE")
    if clip_context.get("candidate_id") != CID or clip_context.get("recording_date") != DATE or not isinstance(clip_context.get("structured_chat"), list) or (clip_context.get("retrieval_budget") or {}).get("structured_chat_truncated") is not False:
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_CHAT_AUTHORITY_UNAVAILABLE")
    closure: dict[str, Any] = {
        "schema_version": CHAT_CLOSURE_SCHEMA,
        "candidate_id": CID,
        "recording_date": DATE,
        "status": "PASS",
        "provider_attempted": False,
        "chat_authority_audit_sha256": CHAT_AUDIT_SHA,
        "source_fact_receipt_sha256": SOURCE_FACT_RECEIPT_SHA,
        "clip_context_sha256": CLIP_CONTEXT_SHA,
        "structured_chat_rows": len(clip_context["structured_chat"]),
        "basis": "sealed source-fact KEEP receipt with structured_chat support plus complete, hash-bound clip context; no provider call",
    }
    closure["self_sha256"] = _sha_bytes(_canon(closure))
    return closure


def _joint_receipt(repo_root: Path) -> tuple[dict[str, Any], str]:
    path = Path(repo_root) / BASE / JOINT_QC_NAME
    receipt = _json(path, label="JOINT_QC")
    if not validate_c9_joint_qc(receipt, Path(repo_root)):
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_JOINT_QC_INVALID")
    return receipt, _sha(path)


def materialize_c9_successor(*, repo_root: Path, snapshot: Path, out: Path) -> Path:
    """Create only a local C9 PASS0 package from exact snapshot bytes."""
    snapshot, out = Path(snapshot), Path(out)
    if out.exists() or out.is_symlink():
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_OUTPUT_EXISTS")
    manifest = _json(snapshot / "artifact-manifest.json", label="SNAPSHOT_MANIFEST")
    listed = {row["path"]: row["sha256"] for row in manifest.get("local_files", []) if isinstance(row, Mapping) and isinstance(row.get("path"), str)}
    required = ("current.record.json", "current.publish.json", "current.cover.png", "current.clip-context.json", "preview.recut.mp4")
    if any(_sha(snapshot / name) != listed.get(name) for name in required):
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_SNAPSHOT_HASH_DRIFT")
    if _sha(snapshot / "current.srt") != OBSERVED_SUBTITLE:
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_OBSERVED_DRIFT_CHANGED")
    if _sha(snapshot / "preview.recut.mp4") != VIDEO_SHA or _sha(snapshot / "current.clip-context.json") != CLIP_CONTEXT_SHA:
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_MEDIA_INPUT_DRIFT")
    record = _json(snapshot / "current.record.json", label="PREDECESSOR_RECORD")
    if record.get("artifact_hashes", {}).get("subtitle_sha256") != OLD_SUBTITLE:
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_PREDECESSOR_RECORD_INVALID")
    publish = _json(snapshot / "current.publish.json", label="PUBLISH")
    clip_context = _json(snapshot / "current.clip-context.json", label="CLIP_CONTEXT")
    chat_closure = _sealed_chat_closure(publish, clip_context)
    title = publish.get("title") or publish.get("final_title")
    hook = (
        (publish.get("cover_generation") or {}).get("story_hook")
        or publish.get("selection_hook")
        or (publish.get("source_fact_review") or {}).get("final_selection_hook")
    )
    if not isinstance(title, str) or hashlib.sha256(title.encode()).hexdigest() != TITLE_SHA.removeprefix("sha256:"):
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_TITLE_DRIFT")
    if not isinstance(hook, str) or hashlib.sha256(hook.encode()).hexdigest() != HOOK_SHA.removeprefix("sha256:"):
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_HOOK_DRIFT")
    base = Path(repo_root) / BASE
    if _sha(base / f"{CID}.source.speaker-final.srt") != SOURCE_SRT or _sha(base / f"{CID}.reviewed.srt") != REVIEWED_SRT:
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_ACCEPTED_TEXT_DRIFT")
    joint, joint_sha = _joint_receipt(Path(repo_root))
    out.mkdir(parents=True)
    copies = (
        (snapshot / "preview.recut.mp4", "video.mp4"),
        (snapshot / "current.cover.png", "cover.png"),
        (snapshot / "current.publish.json", "publish.json"),
        (snapshot / "current.clip-context.json", "clip-context.json"),
        (base / f"{CID}.reviewed.srt", "reviewed.srt"),
        (base / f"{CID}.source.speaker-final.srt", "source.speaker-final.srt"),
        (base / f"{CID}.foreign-video-source-action.v1.json", "source-action.json"),
        (base / f"{CID}.root-acceptance-envelope.v1.json", "acceptance-envelope.json"),
        (base / JOINT_QC_NAME, "joint-title-cover-qc.json"),
    )
    for source, target in copies:
        shutil.copy2(source, out / target)
    (out / "chat-closure.json").write_text(json.dumps(chat_closure, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if _sha(out / "cover.png") != COVER_SHA:
        raise FastlaneC9SuccessorError("C9_SUCCESSOR_COVER_BYTES_DRIFT")
    successor: dict[str, Any] = {
        "schema_version": "fastlane-c9-successor-chain.v1",
        "candidate_id": CID,
        "recording_date": DATE,
        "upload_allowed": False,
        "provider_attempted": False,
        "title": title,
        "title_sha256": TITLE_SHA,
        "selection_hook_sha256": HOOK_SHA,
        "predecessor": {
            "record_sha256": _sha(snapshot / "current.record.json"),
            "declared_subtitle_sha256": OLD_SUBTITLE,
            "observed_current_srt_sha256": OBSERVED_SUBTITLE,
            "status": "SUBTITLE_BYTES_MISSING_DRIFT_EXPLICIT",
        },
        "target": {
            "source_srt_sha256": SOURCE_SRT,
            "reviewed_srt_sha256": REVIEWED_SRT,
            "video_sha256": _sha(out / "video.mp4"),
            "publish_sha256": _sha(out / "publish.json"),
            "cover_sha256": _sha(out / "cover.png"),
            "clip_context_sha256": _sha(out / "clip-context.json"),
            "joint_qc_sha256": joint_sha,
            "chat_closure_sha256": _sha(out / "chat-closure.json"),
            "chat_authority_audit_sha256": CHAT_AUDIT_SHA,
            "frozen_boundary_audit_sha256": BOUNDARY_AUDIT_SHA,
            "frozen_boundary_audit": record["boundary_audit"],
        },
        "typed_blocker": None,
    }
    _reseal(successor)
    (out / "successor-record.json").write_text(json.dumps(successor, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    review = {
        "schema_version": "fastlane-c9-private-successor-review.v1",
        "candidate_id": CID,
        "recording_date": DATE,
        "run_mode": "MANUAL_PRODUCE_REVIEW",
        "status": "PASS0",
        "provider_attempted": False,
        "canonical_delivery_allowed": False,
        "state_write_allowed": False,
        "upload_allowed": False,
        "successor_record": "successor-record.json",
        "joint_title_cover_qc": "joint-title-cover-qc.json",
        "joint_title_cover_qc_sha256": _sha(out / "joint-title-cover-qc.json"),
        "chat_closure": "chat-closure.json",
        "chat_closure_sha256": _sha(out / "chat-closure.json"),
        "typed_blocker": None,
    }
    (out / "review_manifest.json").write_text(json.dumps(review, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return out


def audit_c9_successor(root: Path, manifest: dict[str, Any]) -> list[dict[str, str]] | None:
    """Audit C9 privately; valid exact packages return no issues, never a waiver."""
    if manifest.get("schema_version") != "fastlane-c9-private-successor-review.v1":
        return None
    issues: list[dict[str, str]] = []
    root = Path(root)
    expected_manifest = {
        "schema_version": "fastlane-c9-private-successor-review.v1",
        "candidate_id": CID,
        "recording_date": DATE,
        "run_mode": "MANUAL_PRODUCE_REVIEW",
        "status": "PASS0",
        "provider_attempted": False,
        "canonical_delivery_allowed": False,
        "state_write_allowed": False,
        "upload_allowed": False,
        "successor_record": "successor-record.json",
        "joint_title_cover_qc": "joint-title-cover-qc.json",
        "chat_closure": "chat-closure.json",
        "typed_blocker": None,
    }
    if set(manifest) != set(expected_manifest) | {"joint_title_cover_qc_sha256", "chat_closure_sha256"} or any(manifest.get(key) != value for key, value in expected_manifest.items()):
        issues.append({"code": "C9_SUCCESSOR_MANIFEST_INVALID", "severity": "BLOCK"})
        return issues
    try:
        record = _json(root / "successor-record.json", label="RECORD")
        unsigned = dict(record)
        declared = unsigned.pop("self_sha256", None)
        if declared != _sha_bytes(_canon(unsigned)):
            issues.append({"code": "C9_SUCCESSOR_RECORD_HASH_DRIFT", "severity": "BLOCK"})
        if record.get("candidate_id") != CID or record.get("recording_date") != DATE or record.get("provider_attempted") is not False or record.get("typed_blocker") is not None:
            issues.append({"code": "C9_SUCCESSOR_SCOPE_INVALID", "severity": "BLOCK"})
        if record.get("title_sha256") != TITLE_SHA or record.get("selection_hook_sha256") != HOOK_SHA:
            issues.append({"code": "C9_SUCCESSOR_TITLE_HOOK_INVALID", "severity": "BLOCK"})
        target = record.get("target")
        if not isinstance(target, Mapping) or target.get("source_srt_sha256") != SOURCE_SRT or target.get("reviewed_srt_sha256") != REVIEWED_SRT or target.get("cover_sha256") != COVER_SHA or target.get("video_sha256") != VIDEO_SHA or target.get("clip_context_sha256") != CLIP_CONTEXT_SHA or target.get("frozen_boundary_audit_sha256") != BOUNDARY_AUDIT_SHA:
            issues.append({"code": "C9_SUCCESSOR_TARGET_BINDING_INVALID", "severity": "BLOCK"})
        if _sha(root / "cover.png") != COVER_SHA or _sha(root / "video.mp4") != VIDEO_SHA or _sha(root / "clip-context.json") != CLIP_CONTEXT_SHA or _sha(root / "reviewed.srt") != REVIEWED_SRT or _sha(root / "source.speaker-final.srt") != SOURCE_SRT:
            issues.append({"code": "C9_SUCCESSOR_FROZEN_BYTES_DRIFT", "severity": "BLOCK"})
        source_action = root / "source-action.json"
        source_action_sha = _sha(source_action)
        if source_action_sha != "sha256:32fb21e0703a5aec07920cfc544bb951201eaff0f59353fdc8152971f95dd2a4":
            issues.append({"code": "C9_SUCCESSOR_SOURCE_ACTION_DRIFT", "severity": "BLOCK"})
        try:
            publish = _json(root / "publish.json", label="PUBLISH")
            clip_context = _json(root / "clip-context.json", label="CLIP_CONTEXT")
            expected_chat = _sealed_chat_closure(publish, clip_context)
            observed_chat = _json(root / "chat-closure.json", label="CHAT_CLOSURE")
            if observed_chat != expected_chat:
                issues.append({"code": "C9_SUCCESSOR_CHAT_CLOSURE_DRIFT", "severity": "BLOCK"})
            if target.get("chat_closure_sha256") != _sha(root / "chat-closure.json") or target.get("chat_authority_audit_sha256") != CHAT_AUDIT_SHA or manifest.get("chat_closure_sha256") != _sha(root / "chat-closure.json"):
                issues.append({"code": "C9_SUCCESSOR_CHAT_BINDING_INVALID", "severity": "BLOCK"})
        except FastlaneC9SuccessorError as exc:
            issues.append({"code": str(exc), "severity": "BLOCK"})
        boundary = target.get("frozen_boundary_audit") if isinstance(target, Mapping) else None
        if not isinstance(boundary, Mapping) or _sha_bytes(_canon(boundary)) != BOUNDARY_AUDIT_SHA:
            issues.append({"code": "C9_SUCCESSOR_BOUNDARY_DRIFT", "severity": "BLOCK"})
        joint_path = root / "joint-title-cover-qc.json"
        joint = _json(joint_path, label="JOINT_QC")
        if joint.get("self_sha256") != (target or {}).get("joint_qc_sha256") and _sha(joint_path) != (target or {}).get("joint_qc_sha256"):
            issues.append({"code": "C9_SUCCESSOR_JOINT_QC_HASH_INVALID", "severity": "BLOCK"})
        if joint.get("status") != "PASS" or joint.get("provider_attempted") is not False or joint.get("candidate_id") != CID or joint.get("recording_date") != DATE or joint.get("cover_sha256") != COVER_SHA:
            issues.append({"code": "C9_SUCCESSOR_JOINT_QC_INVALID", "severity": "BLOCK"})
        if _sha(root / "joint-title-cover-qc.json") != manifest.get("joint_title_cover_qc_sha256"):
            issues.append({"code": "C9_SUCCESSOR_MANIFEST_JOINT_QC_HASH_INVALID", "severity": "BLOCK"})
    except FastlaneC9SuccessorError as exc:
        issues.append({"code": str(exc), "severity": "BLOCK"})
    return issues


def _sha_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()
