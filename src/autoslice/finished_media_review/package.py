"""Record and review-manifest construction for a finished-media review package.

The record carries only fields that come from real files or real check runs;
it has no story_contract, boundary_audit or scorecard because this lane does
not run those stages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .checks import Context
from .common import (
    AUTO_CHECKS_NAME,
    EVIDENCE_DIR,
    PACKAGE_SCHEMA,
    RECORD_KIND,
    REVIEW_NAME,
    FmrError,
    file_sha,
    load_json,
    write_json,
)
from .member import member_copy_path, package_files, receipt_path, record_path

RECORD_SCHEMA = "finished-media-review-record.v1"
RUN_MODE = "FINISHED_MEDIA_REVIEW"


def load_repair_authority(
    path: str | Path, *, cid: str, title: str, replay_evidence: bool = True,
) -> dict[str, Any]:
    """Validate a same-BV repair authority file for this package; return its canonical form.

    Interface only: the authority names the already published BV this package may
    replace (built from the first publication's reconciliation by
    ``new_bv_repair_authority.build_new_bv_repair_authority``).  It adds no review gate.
    ``replay_evidence=False`` is for a maker host that cannot see the first publication's
    evidence files; make-manifest, verify and repair-plan replay the full closure regardless.
    """
    from src.autoslice.new_bv_repair_authority import (
        NewBvRepairAuthorityError,
        validate_new_bv_repair_authority,
    )

    value = load_json(path)
    try:
        return validate_new_bv_repair_authority(
            value, candidate_id=cid, expected_final_title=title, replay_evidence=replay_evidence)
    except NewBvRepairAuthorityError as exc:
        raise FmrError(f"FMR_REPAIR_AUTHORITY_INVALID: {exc}") from exc


def load_title_receipt_reference(
    path: str | Path, *, authority: Mapping[str, Any], cid: str, title: str, replay_evidence: bool = True,
) -> dict[str, str]:
    """Validate an FMR title receipt through the shared title-chain replayer.  ``replay_evidence=False`` (with
    ``--authority-shape-only``) skips only that replay, which reads root-only files: title_receipt_shape checks the rest."""
    raw_path = Path(path)
    if raw_path.is_symlink() or not raw_path.is_file():
        raise FmrError("FMR_TITLE_RECEIPT_INVALID: TITLE_REPAIR_PREDECESSOR_TITLE_RECEIPT_ATTESTATION_MISSING")
    receipt_path = raw_path.resolve()
    reference = {"path": str(receipt_path), "sha256": file_sha(receipt_path).removeprefix("sha256:")}
    from src.autoslice.authorized_upload_title_repair import TitleRepairError, validate_title_repair_successor
    from .title_receipt_shape import validate_title_receipt_shape
    try:
        if replay_evidence:
            validate_title_repair_successor(
                {"receipt": reference}, authority=authority, candidate_id=cid, title=title,
            )
        else:
            validate_title_receipt_shape(receipt_path, authority=authority, candidate_id=cid, title=title)
    except TitleRepairError as exc:
        raise FmrError(f"FMR_TITLE_RECEIPT_INVALID: {exc}") from exc
    return reference


def write_record(
    ctx: Context, *, repair_authority: Mapping[str, Any] | None = None,
    title_receipt: Mapping[str, str] | None = None,
) -> Path | None:
    """Write ``<cid>.record.json`` once every field-producing check has succeeded.

    ``repair_authority`` (from ``load_repair_authority``) is written under
    ``recovery_publication_authority`` only when given; without it the record is unchanged.
    """
    parts = ctx.record
    needed = ("pieces", "subtitle_audio_correspondence", "upload_tags", "_intro", "_burned_preview")
    if any(key not in parts for key in needed):
        return None
    files = package_files(ctx.root, ctx.cid)
    video_sha = file_sha(files["video"])
    hashes = dict(parts.get("artifact_hashes") or {})
    hashes.update({
        "video_sha256": video_sha,
        "burned_video_sha256": video_sha,
        "cover_sha256": file_sha(files["cover"]),
        "subtitle_sha256": file_sha(files["srt"]),
        "delivery_subtitle_sha256": file_sha(files["srt"]),
        "speaker_srt_sha256": file_sha(files["speaker_srt"]),
        "ass_sha256": file_sha(files["ass"]),
    })
    preview = dict(parts["_burned_preview"])
    preview["ass_path"] = str(files["ass"])
    preview["branding_intro"] = dict(parts["_intro"])
    record = {
        "schema_version": RECORD_SCHEMA,
        "record_kind": RECORD_KIND,
        "delivery_candidate_id": ctx.cid,
        "recording_date": ctx.member["recording_date"],
        "pieces": parts["pieces"],
        "artifact_hashes": hashes,
        "publish_staging": {"title": files["title"].read_text(encoding="utf-8").strip()},
        "burned_preview": preview,
        "subtitle_audio_correspondence": parts["subtitle_audio_correspondence"],
        "upload_tags": parts["upload_tags"],
        "unresolved_findings_disclosed": [],
        "auto_checks_sha256": file_sha(ctx.root / EVIDENCE_DIR / AUTO_CHECKS_NAME),
    }
    if repair_authority is not None:
        record["recovery_publication_authority"] = dict(repair_authority)
    if title_receipt is not None:
        record["verified_title_repair"] = {"receipt": dict(title_receipt)}
    path = record_path(ctx.root, ctx.cid)
    write_json(path, record)
    return path


def write_review_manifest(
    root: Path, cid: str, member: dict[str, Any], *, repair_authority: Mapping[str, Any] | None = None,
) -> Path:
    """Package manifest.  Item keys reuse the generic names so the audit's input closure sees every file.

    With ``repair_authority`` the item carries the same authority as the record: the two-surface
    shape that ``attach_package_recovery_publication_authority`` projects into the manifest.
    """
    root = Path(root)
    files = package_files(root, cid)
    receipt = receipt_path(root, cid)
    review = root / REVIEW_NAME
    if not receipt.is_file():
        raise FmrError("FMR_RECEIPT_MISSING")
    if not review.is_file():
        raise FmrError("FMR_REVIEW_MISSING")
    title = files["title"].read_text(encoding="utf-8").strip()
    item = {
        "id": cid, "stem": cid, "candidate_id": cid, "title": title,
        "video": files["video"].name, "video_sha256": file_sha(files["video"]),
        "cover": files["cover"].name, "cover_sha256": file_sha(files["cover"]),
        "subtitle_srt": files["srt"].name, "subtitle_srt_sha256": file_sha(files["srt"]),
        "speaker_srt": files["speaker_srt"].name, "speaker_srt_sha256": file_sha(files["speaker_srt"]),
        "ass_path": files["ass"].name, "ass_sha256": file_sha(files["ass"]),
        "record": record_path(root, cid).name, "record_sha256": file_sha(record_path(root, cid)),
        "title_txt": files["title"].name, "description_txt": files["description"].name,
        "intro_receipt": files["intro_receipt"].name,
        "member": member_copy_path(root, cid).name,
        "receipt": receipt.name, "receipt_sha256": file_sha(receipt),
        "review": review.name, "review_sha256": file_sha(review),
    }
    if repair_authority is not None:
        record = load_json(record_path(root, cid))
        if record.get("recovery_publication_authority") != dict(repair_authority):
            raise FmrError("FMR_REPAIR_AUTHORITY_RECORD_MISMATCH: re-run `check` with the same --repair-authority")
        item["recovery_publication_authority"] = dict(repair_authority)
    manifest = {
        "schema_version": PACKAGE_SCHEMA,
        "date": member["recording_date"],
        "run_mode": RUN_MODE,
        "upload_allowed": False,
        "candidate_id": cid,
        "items": [item],
    }
    path = root / "review_manifest.json"
    write_json(path, manifest)
    return path
