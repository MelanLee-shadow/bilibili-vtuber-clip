"""Immutable authority for one existing-BV title-and-cover-only revision.

This authority is deliberately separate from an initial upload manifest and
from full media replacement authority.  It replays the already-public identity
and metadata receipts, freezes the reviewed replacement cover, and permits
exactly two mutable fields: archive/part/section title and cover.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "same-bv-title-cover-revision-authority.v2"
_ALLOWED_FIELDS = ["title", "cover"]


class TitleCoverAuthorityError(RuntimeError):
    """The proposed same-BV revision is not fully hash-bound."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _regular_file(path: Path, label: str) -> Path:
    path = path.expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise TitleCoverAuthorityError(f"{label} must be a regular non-symlink file")
    path = path.resolve(strict=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TitleCoverAuthorityError(f"{label} must be a regular non-symlink file")
    return path


def file_descriptor(path: Path, label: str = "evidence") -> dict[str, Any]:
    path = _regular_file(path, label)
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _load_json_path(path: Path, label: str) -> dict[str, Any]:
    path = _regular_file(path, label)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TitleCoverAuthorityError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TitleCoverAuthorityError(f"{label} must be a JSON object")
    return payload


def validate_descriptor(value: object, label: str) -> Path:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "bytes"}:
        raise TitleCoverAuthorityError(f"{label} descriptor shape is invalid")
    path = _regular_file(Path(str(value.get("path") or "")), label)
    if _sha256_file(path) != value.get("sha256"):
        raise TitleCoverAuthorityError(f"{label} hash drift")
    if path.stat().st_size != value.get("bytes"):
        raise TitleCoverAuthorityError(f"{label} byte-size drift")
    return path


def load_json_descriptor(value: object, label: str) -> tuple[Path, dict[str, Any]]:
    path = validate_descriptor(value, label)
    return path, _load_json_path(path, label)


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TitleCoverAuthorityError(f"{label} must be a positive integer")
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TitleCoverAuthorityError(f"{label} must be non-empty text")
    return value


def _negative_visual_finding(value: object) -> bool:
    if value == []:
        return True
    return (
        isinstance(value, Mapping)
        and value.get("found") is False
        and value.get("items") == []
    )


def _glyphs_certain(value: object) -> bool:
    if value is True:
        return True
    return (
        isinstance(value, Mapping)
        and value.get("certain") is True
        and value.get("uncertain_glyphs") == []
    )


def _parse_fullres_answer(receipt: Mapping[str, Any], cover_sha256: str) -> dict[str, Any]:
    model = receipt.get("model")
    if (
        receipt.get("schema_version") != "cpa-frame-witness.v1"
        or receipt.get("status") != "OBSERVED"
        or receipt.get("provider") != "cpa"
        or not isinstance(model, str)
        or not model.strip()
        or receipt.get("image_sha256") != cover_sha256
    ):
        raise TitleCoverAuthorityError("full-resolution CPA receipt binding is invalid")
    answer = receipt.get("answer")
    if not isinstance(answer, str):
        raise TitleCoverAuthorityError("full-resolution CPA receipt has no answer")
    try:
        verdict = json.loads(answer)
    except json.JSONDecodeError as exc:
        raise TitleCoverAuthorityError("full-resolution CPA answer is not JSON") from exc
    if not isinstance(verdict, dict):
        raise TitleCoverAuthorityError("full-resolution CPA answer must be an object")
    observed_lines = verdict.get("observed_text_lines")
    if (
        not isinstance(observed_lines, list)
        or not observed_lines
        or any(not isinstance(line, str) or not line.strip() for line in observed_lines)
    ):
        raise TitleCoverAuthorityError("full-resolution CPA observed text is invalid")
    if (
        verdict.get("pass") is not True
        or not _glyphs_certain(verdict.get("glyph_certainty"))
        or verdict.get("identity_clear") is not True
        or verdict.get("single_story_clear") is not True
        or not _negative_visual_finding(verdict.get("forbidden_elements"))
        or not _negative_visual_finding(verdict.get("ui_or_text_residuals"))
    ):
        raise TitleCoverAuthorityError("full-resolution CPA verdict did not pass")
    return {
        "provider": "cpa",
        "model": model,
        "input_image_sha256": cover_sha256,
        "observed_text_lines": list(observed_lines),
        "identity_clear": True,
        "single_story_clear": True,
        "pass": True,
    }


def _baseline_from_receipts(receipts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    manifest = receipts["manifest"]
    uploaded = receipts["uploaded"]
    public = receipts["public_verify"]
    season = receipts["season_verify"]
    reconciliation = receipts["publication_reconciliation"]

    if uploaded.get("schema_version") != "authorized-upload-result.v3" or uploaded.get("status") != "VERIFIED_PUBLIC":
        raise TitleCoverAuthorityError("uploaded receipt is not VERIFIED_PUBLIC v3")
    if public.get("schema_version") != "authorized-upload-public-verify.v2" or public.get("status") != "VERIFIED_PUBLIC" or public.get("problems") != []:
        raise TitleCoverAuthorityError("public verification receipt is not clean")
    if season.get("schema_version") != "authorized-upload-season-verify.v1" or season.get("status") != "IN_SEASON_PUBLIC":
        raise TitleCoverAuthorityError("season verification receipt is not public")
    if reconciliation.get("schema_version") != "new-bv-publication-reconciliation-authority.v1" or reconciliation.get("status") != "VERIFIED_PUBLIC":
        raise TitleCoverAuthorityError("publication reconciliation is not verified")

    bvid = _require_text(uploaded.get("bvid"), "baseline BVID")
    aid = _require_positive_int(uploaded.get("aid"), "baseline AID")
    cid = _require_positive_int(uploaded.get("cid"), "baseline CID")
    old_title = _require_text(uploaded.get("title"), "baseline title")
    expected = public.get("expected") or {}
    public_view = public.get("public_view") or {}
    member = public.get("member_archive") or {}
    section_api = public.get("section_api") or {}
    titles = section_api.get("episode_titles") or []
    manifest_title = manifest.get("title")
    if any(value != old_title for value in (
        manifest_title,
        public.get("manifest_title"),
        expected.get("title"),
        public_view.get("title"),
        member.get("title"),
        season.get("title"),
        reconciliation.get("title"),
    )) or titles != [old_title]:
        raise TitleCoverAuthorityError("baseline title receipts disagree")
    if any(value != bvid for value in (
        public.get("bvid"), member.get("bvid"), season.get("bvid"), reconciliation.get("bvid")
    )):
        raise TitleCoverAuthorityError("baseline BVID receipts disagree")
    if any(value != aid for value in (
        public_view.get("aid"), member.get("aid"), season.get("aid"), reconciliation.get("aid")
    )):
        raise TitleCoverAuthorityError("baseline AID receipts disagree")
    if any(value != cid for value in (
        public_view.get("cid"), season.get("cid"), reconciliation.get("cid")
    )):
        raise TitleCoverAuthorityError("baseline CID receipts disagree")

    policy = manifest.get("publish_policy") or {}
    # The initial upload manifest freezes the lane/title policy, not the platform
    # IDs allocated/selected after upload.  Those IDs become authority only when
    # public verification, season verification and reconciliation all agree.
    metadata = {
        "description": manifest.get("description"),
        "tags": list(manifest.get("tags") or []),
        "tid": policy.get("tid"),
        "copyright": policy.get("copyright"),
        "source": policy.get("source"),
        "season_id": expected.get("season_id"),
        "section_id": expected.get("section_id"),
    }
    public_tags = list(public.get("public_tags") or [])
    if (
        metadata["description"] != expected.get("description")
        or set(metadata["tags"]) != set(expected.get("tags") or [])
        or set(metadata["tags"]) != set(public_tags)
        or metadata["tid"] != expected.get("tid")
        or metadata["copyright"] != expected.get("copyright")
        or metadata["source"] != expected.get("source")
        or metadata["season_id"] != season.get("season_id")
        or metadata["section_id"] != season.get("section_id")
    ):
        raise TitleCoverAuthorityError("baseline metadata receipts disagree")
    _require_positive_int(metadata["season_id"], "season_id")
    _require_positive_int(metadata["section_id"], "section_id")
    for label in ("description", "source"):
        _require_text(metadata[label], label)
    if not metadata["tags"] or any(not isinstance(item, str) or not item for item in metadata["tags"]):
        raise TitleCoverAuthorityError("baseline tags are invalid")

    manifest_evidence = (reconciliation.get("evidence") or {}).get("manifest") or {}
    uploaded_evidence = (reconciliation.get("evidence") or {}).get("uploaded") or {}
    public_evidence = (reconciliation.get("evidence") or {}).get("public_verify") or {}
    season_evidence = (reconciliation.get("evidence") or {}).get("season_verify") or {}
    if (
        manifest_evidence.get("sha256") != uploaded.get("manifest_sha256")
        or uploaded_evidence.get("payload") != uploaded
        or public_evidence.get("payload") != public
        or season_evidence.get("payload") != season
    ):
        raise TitleCoverAuthorityError("publication reconciliation payload binding drifted")

    return {
        "candidate_id": _require_text(reconciliation.get("candidate_id"), "candidate_id"),
        "recording_date": _require_text(reconciliation.get("recording_date"), "recording_date"),
        "identity": {"bvid": bvid, "aid": aid, "cid": cid},
        "old_title": old_title,
        "metadata": metadata,
        "old_cover_sha256": _require_text(uploaded.get("cover_sha256"), "old cover sha256"),
        "video_sha256": _require_text(uploaded.get("video_sha256"), "video sha256"),
        "subtitle_sha256": _require_text(uploaded.get("subtitle_sha256"), "subtitle sha256"),
    }


def _validate_title_review(
    review: Mapping[str, Any], baseline: Mapping[str, Any], target_title: str
) -> None:
    """Consume the existing CPA title decision, never infer it from cover PASS."""
    decision = review.get("decision") or {}
    review_schema = review.get("schema_version")
    if (
        not isinstance(review_schema, str)
        or not review_schema.endswith("-title-cpa-review.v1")
        or review.get("status") != "PASS"
        or review.get("provider") != "cpa"
        or review.get("fallback_used") is not False
        or review.get("candidate_id") != baseline["candidate_id"]
        or review.get("public_target") != {
            "bvid": baseline["identity"]["bvid"], "same_bv_only": True
        }
        or (review.get("source_bindings") or {}).get("final_srt_sha256")
        != baseline["subtitle_sha256"]
        or decision.get("selected_title") != target_title
        or any(decision.get(key) is not True for key in (
            "pass", "single_story", "unfamiliar_reader_clear",
            "avoids_unproven_causality", "aligned_with_cover",
        ))
    ):
        raise TitleCoverAuthorityError("CPA title review does not authorize the exact target")
    for kind in ("prompt", "completion"):
        path = _regular_file(Path(str(review.get(f"{kind}_path") or "")), kind)
        if _sha256_file(path) != review.get(f"{kind}_sha256"):
            raise TitleCoverAuthorityError(f"CPA title {kind} hash drift")
        if kind == "completion" and _load_json_path(path, kind) != decision:
            raise TitleCoverAuthorityError("CPA title decision differs from completion")


def build_authority(
    *,
    manifest_path: Path,
    uploaded_path: Path,
    public_verify_path: Path,
    season_verify_path: Path,
    reconciliation_path: Path,
    review_consumption_path: Path,
    fullres_cpa_path: Path,
    title_review_path: Path,
    target_title: str,
    target_cover_path: Path,
    authorized_by: str,
    authorization_quote: str,
) -> dict[str, Any]:
    descriptors = {
        "manifest": file_descriptor(manifest_path, "baseline manifest"),
        "uploaded": file_descriptor(uploaded_path, "uploaded receipt"),
        "public_verify": file_descriptor(public_verify_path, "public verification"),
        "season_verify": file_descriptor(season_verify_path, "season verification"),
        "publication_reconciliation": file_descriptor(reconciliation_path, "publication reconciliation"),
    }
    payloads = {name: _load_json_path(Path(desc["path"]), name) for name, desc in descriptors.items()}
    baseline = _baseline_from_receipts(payloads)
    target_title = _require_text(target_title, "target title")
    if target_title == baseline["old_title"]:
        raise TitleCoverAuthorityError("target title must differ from the baseline")
    target_cover = file_descriptor(target_cover_path, "target cover")

    review_desc = file_descriptor(review_consumption_path, "review consumption")
    review = _load_json_path(Path(review_desc["path"]), "review consumption")
    if (
        review.get("cover_sha256") != target_cover["sha256"]
        or review.get("identity") != "PASS"
        or (review.get("single_image_verdict") or {}).get("pass") is not True
        or (review.get("comparison_verdict") or {}).get("pass") is not False
        or review.get("originals_preserved") is not True
    ):
        raise TitleCoverAuthorityError("review consumption does not preserve the expected PASS/FAIL split")

    fullres_desc = file_descriptor(fullres_cpa_path, "full-resolution CPA receipt")
    fullres = _load_json_path(Path(fullres_desc["path"]), "full-resolution CPA receipt")
    fullres_claims = _parse_fullres_answer(fullres, target_cover["sha256"])
    title_desc = file_descriptor(title_review_path, "CPA title review")
    title_review = _load_json_path(Path(title_desc["path"]), "CPA title review")
    _validate_title_review(title_review, baseline, target_title)
    authority = {
        "schema_version": SCHEMA_VERSION,
        "scope": {
            "same_bv_only": True,
            "new_bv_forbidden": True,
            "unchanged_cid_required": True,
            "allowed_fields": list(_ALLOWED_FIELDS),
        },
        "receipts": descriptors,
        "baseline": baseline,
        "target": {"title": target_title, "cover": target_cover},
        "quality_evidence": {
            "review_consumption": review_desc,
            "old_comparison_preserved_fail": True,
            "fullres_cpa": fullres_desc,
            "fullres_claims": fullres_claims,
            "title_review": title_desc,
        },
        "authorization": {
            "by": _require_text(authorized_by, "authorizer"),
            "quote": _require_text(authorization_quote, "authorization quote"),
            "scope": "SAME_BV_TITLE_COVER_ONLY",
        },
    }
    authority["authority_sha256"] = _canonical_sha256(authority)
    validate_authority(authority)
    return authority


def validate_authority(authority: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version", "scope", "receipts", "baseline", "target",
        "quality_evidence", "authorization", "authority_sha256",
    }
    if set(authority) != required or authority.get("schema_version") != SCHEMA_VERSION:
        raise TitleCoverAuthorityError("title-cover authority schema/shape mismatch")
    unhashed = dict(authority)
    claimed = unhashed.pop("authority_sha256", None)
    if claimed != _canonical_sha256(unhashed):
        raise TitleCoverAuthorityError("title-cover authority digest mismatch")
    scope = authority.get("scope") or {}
    if scope != {
        "same_bv_only": True,
        "new_bv_forbidden": True,
        "unchanged_cid_required": True,
        "allowed_fields": list(_ALLOWED_FIELDS),
    }:
        raise TitleCoverAuthorityError("title-cover authority scope expanded")
    receipts_raw = authority.get("receipts") or {}
    if set(receipts_raw) != {"manifest", "uploaded", "public_verify", "season_verify", "publication_reconciliation"}:
        raise TitleCoverAuthorityError("baseline receipt set is incomplete")
    payloads: dict[str, dict[str, Any]] = {}
    for name, descriptor in receipts_raw.items():
        _path, payloads[name] = load_json_descriptor(descriptor, name)
    derived = _baseline_from_receipts(payloads)
    if authority.get("baseline") != derived:
        raise TitleCoverAuthorityError("derived baseline differs from frozen authority")
    target = authority.get("target") or {}
    if set(target) != {"title", "cover"}:
        raise TitleCoverAuthorityError("target shape is invalid")
    title = _require_text(target.get("title"), "target title")
    if title == derived["old_title"]:
        raise TitleCoverAuthorityError("target title no longer differs")
    cover_path = validate_descriptor(target.get("cover"), "target cover")
    if cover_path.suffix.lower() != ".png":
        raise TitleCoverAuthorityError("target cover must be PNG")
    quality = authority.get("quality_evidence") or {}
    if set(quality) != {"review_consumption", "old_comparison_preserved_fail", "fullres_cpa", "fullres_claims", "title_review"}:
        raise TitleCoverAuthorityError("quality evidence shape is invalid")
    _review_path, review = load_json_descriptor(quality.get("review_consumption"), "review consumption")
    if (
        quality.get("old_comparison_preserved_fail") is not True
        or review.get("cover_sha256") != target["cover"]["sha256"]
        or review.get("identity") != "PASS"
        or (review.get("single_image_verdict") or {}).get("pass") is not True
        or (review.get("comparison_verdict") or {}).get("pass") is not False
        or review.get("originals_preserved") is not True
    ):
        raise TitleCoverAuthorityError("review evidence no longer preserves the conflict")
    _fullres_path, fullres = load_json_descriptor(quality.get("fullres_cpa"), "full-resolution CPA receipt")
    claims = _parse_fullres_answer(fullres, target["cover"]["sha256"])
    if quality.get("fullres_claims") != claims:
        raise TitleCoverAuthorityError("full-resolution CPA claims drifted")
    _title_path, title_review = load_json_descriptor(quality.get("title_review"), "CPA title review")
    _validate_title_review(title_review, derived, title)
    authorization = authority.get("authorization") or {}
    if set(authorization) != {"by", "quote", "scope"} or authorization.get("scope") != "SAME_BV_TITLE_COVER_ONLY":
        raise TitleCoverAuthorityError("authorization scope is invalid")
    _require_text(authorization.get("by"), "authorizer")
    _require_text(authorization.get("quote"), "authorization quote")
    return dict(authority)


def write_authority(path: Path, authority: Mapping[str, Any]) -> None:
    validate_authority(authority)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(authority, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def load_authority(path: Path) -> dict[str, Any]:
    return validate_authority(_load_json_path(path, "title-cover authority"))
