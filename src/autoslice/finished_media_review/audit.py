"""Package audit for the finished-media review lane (schema dispatch target).

``audit_if_finished_media`` is called from ``audit_package``; any other
manifest schema returns ``None`` and takes the unchanged legacy paths.

This is an interface audit, not a review: it only proves that the receipt is
well-formed, says PASS, names a reviewer different from the maker, and that
every file it binds (finished media, member, record, the ``check`` result file,
``REVIEW.md``) is still the exact byte stream.  Whether the review was good is
the reviewer's business; SRT validity, description rendering, title policy and
the rest stay with the upload gate in ``authorized_upload``.  The one exception
is decodability (A5): the upload gate never decodes the media, so it stays a block.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .common import (
    AUTO_CHECKS_NAME,
    EVIDENCE_DIR,
    PACKAGE_SCHEMA,
    RECEIPT_SCHEMA,
    RECORD_KIND,
    REVIEW_NAME,
    FmrError,
    candidate_id_for,
    file_sha,
    is_sha,
    load_json,
)
from .member import (
    FILE_KEYS,
    member_copy_path,
    member_problems,
    package_files,
    receipt_path,
    record_path,
)


def _issue(code: str, detail: str = "") -> dict[str, str]:
    row = {"code": code, "severity": "BLOCK"}
    if detail:
        row["detail"] = detail[:500]
    return row


def _bound(entry: object, path: Path) -> bool:
    return (isinstance(entry, Mapping) and entry.get("path") == path.name and path.is_file()
            and not path.is_symlink() and entry.get("sha256") == file_sha(path))


def package_problems(root: Path) -> list[dict[str, str]]:
    """Every reason this package may not pass; an empty list means the lane accepts it."""
    from .receipt import A_IDS, auto_rows

    root = Path(root)
    issues: list[dict[str, str]] = []
    try:
        review = load_json(root / "review_manifest.json")
        items = review.get("items")
        if review.get("schema_version") != PACKAGE_SCHEMA or not isinstance(items, list) or len(items) != 1:
            return [_issue("FMR_PACKAGE_MANIFEST_INVALID")]
        item = items[0]
        cid = str(item.get("candidate_id") or "")
        member = load_json(member_copy_path(root, cid))
        receipt = load_json(receipt_path(root, cid))
        record = load_json(record_path(root, cid))
        auto = load_json(root / EVIDENCE_DIR / AUTO_CHECKS_NAME)
    except FmrError as exc:
        return [_issue("FMR_PACKAGE_INPUT_MISSING", str(exc))]

    for code in member_problems(member):
        issues.append(_issue(code))
    try:
        derived = candidate_id_for(str(member.get("recording_date")), str(member.get("member_slug")))
    except FmrError:
        return issues + [_issue("FMR_MEMBER_IDENTITY_INVALID")]
    if derived != cid or review.get("candidate_id") != cid or review.get("date") != member.get("recording_date"):
        issues.append(_issue("FMR_CANDIDATE_IDENTITY_DRIFT"))
    located = re.search(r"/out/(\d{4}-\d{2}-\d{2})/", str(root.resolve()) + "/")
    if located and located.group(1) != member.get("recording_date"):
        issues.append(_issue("FMR_RECORDING_DATE_PATH_MISMATCH"))

    # receipt: schema, verdict, identities
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        issues.append(_issue("FMR_RECEIPT_SCHEMA_INVALID"))
    if receipt.get("verdict") != "PASS":
        issues.append(_issue("FMR_RECEIPT_VERDICT_NOT_PASS"))
    if receipt.get("candidate_id") != cid:
        issues.append(_issue("FMR_RECEIPT_CANDIDATE_MISMATCH"))
    maker = receipt.get("maker")
    reviewer = receipt.get("reviewer")
    if maker != member.get("maker"):
        issues.append(_issue("FMR_RECEIPT_MAKER_MISMATCH"))
    if not (isinstance(reviewer, Mapping) and reviewer.get("kind") in {"AGENT", "维护者"}
            and isinstance(reviewer.get("id"), str) and reviewer["id"].strip()):
        issues.append(_issue("FMR_RECEIPT_REVIEWER_INVALID"))
    elif reviewer["id"] == (member.get("maker") or {}).get("id"):
        issues.append(_issue("FMR_REVIEWER_IS_MAKER"))

    # hash bindings: every bound file is the exact current byte stream
    bindings = receipt.get("bindings")
    if not isinstance(bindings, Mapping) or set(bindings) != set(FILE_KEYS) | {"member", "record"}:
        issues.append(_issue("FMR_RECEIPT_BINDINGS_INCOMPLETE"))
        bindings = {}
    paths = dict(package_files(root, cid))
    paths["member"] = member_copy_path(root, cid)
    paths["record"] = record_path(root, cid)
    for key, path in paths.items():
        if not _bound(bindings.get(key), path):
            issues.append(_issue("FMR_BINDING_HASH_MISMATCH", key))
    if receipt.get("title_sha256") != (file_sha(paths["title"]) if paths["title"].is_file() else None):
        issues.append(_issue("FMR_BINDING_HASH_MISMATCH", "title"))
    auto_path = root / EVIDENCE_DIR / AUTO_CHECKS_NAME
    bound_auto = receipt.get("auto_checks")
    if not (isinstance(bound_auto, Mapping) and bound_auto.get("sha256") == file_sha(auto_path)):
        issues.append(_issue("FMR_EVIDENCE_HASH_MISMATCH", "auto_checks"))
    review_file = root / REVIEW_NAME
    if not _bound(receipt.get("review"), review_file):
        issues.append(_issue("FMR_REVIEW_HASH_MISMATCH" if review_file.is_file() else "FMR_REVIEW_MISSING"))
    if issues:
        return issues

    # review_manifest.json item hashes equal the current files
    item_keys = (("video", "video_sha256", "video"), ("cover", "cover_sha256", "cover"),
                 ("subtitle_srt", "subtitle_srt_sha256", "srt"), ("speaker_srt", "speaker_srt_sha256", "speaker_srt"),
                 ("ass_path", "ass_sha256", "ass"))
    for name_key, sha_key, file_key in item_keys:
        if item.get(name_key) != paths[file_key].name or item.get(sha_key) != file_sha(paths[file_key]):
            issues.append(_issue("FMR_MANIFEST_ITEM_MISMATCH", name_key))
    if item.get("record_sha256") != file_sha(paths["record"]):
        issues.append(_issue("FMR_MANIFEST_ITEM_MISMATCH", "record"))
    if item.get("receipt") != receipt_path(root, cid).name or item.get("receipt_sha256") != file_sha(
            receipt_path(root, cid)):
        issues.append(_issue("FMR_MANIFEST_ITEM_MISMATCH", "receipt"))
    if item.get("review") != REVIEW_NAME or item.get("review_sha256") != file_sha(review_file):
        issues.append(_issue("FMR_MANIFEST_ITEM_MISMATCH", "review"))

    # the `check` result file is complete and was produced for exactly these bytes
    if auto.get("candidate_id") != cid or set(auto_rows(auto)) != set(A_IDS):
        issues.append(_issue("FMR_AUTO_CHECKS_INVALID"))
    else:
        a1_files = (auto_rows(auto)["A1"].get("detail") or {}).get("files") or {}
        for key, path in paths.items():
            if key != "record" and (a1_files.get(key) or {}).get("sha256") != file_sha(path):
                issues.append(_issue("FMR_AUTO_CHECKS_STALE", key))
        # The only A item kept as a block: the upload gate never decodes the media, so an
        # undecodable file would otherwise reach the platform (upload safety, not review).
        if {"FMR_A5_FULL_DECODE_FAILED", "FMR_A5_PROBE_FAILED"} & set(auto_rows(auto)["A5"].get("reason_codes") or []):
            issues.append(_issue("FMR_MEDIA_NOT_DECODABLE", "A5"))

    issues += _record_problems(record, cid, member, auto, paths, root)
    return issues


def _record_problems(record, cid, member, auto, paths, root) -> list[dict[str, str]]:
    """Record identity and hashes (the record feeds the publication registry and the dedupe identity)."""
    from .receipt import auto_rows

    out: list[dict[str, str]] = []
    if record.get("record_kind") != RECORD_KIND or record.get("delivery_candidate_id") != cid:
        out.append(_issue("FMR_RECORD_IDENTITY_INVALID"))
    if record.get("recording_date") != member.get("recording_date"):
        out.append(_issue("FMR_RECORD_DATE_MISMATCH"))
    hashes = record.get("artifact_hashes") or {}
    for key, path in (("video_sha256", paths["video"]), ("burned_video_sha256", paths["video"]),
                      ("cover_sha256", paths["cover"]), ("subtitle_sha256", paths["srt"]),
                      ("delivery_subtitle_sha256", paths["srt"]), ("speaker_srt_sha256", paths["speaker_srt"]),
                      ("ass_sha256", paths["ass"])):
        if hashes.get(key) != file_sha(path):
            out.append(_issue("FMR_RECORD_HASH_MISMATCH", key))
    a1_pieces = (auto_rows(auto).get("A1", {}).get("detail") or {}).get("pieces")
    declared = [(p["start_ms"], p["end_ms"]) for p in member["source"]["pieces"]]
    if record.get("pieces") != a1_pieces or not a1_pieces:
        out.append(_issue("FMR_RECORD_PIECES_MISMATCH"))
    else:
        if [(p["source_start_ms"], p["source_end_ms"]) for p in a1_pieces] != declared:
            out.append(_issue("FMR_RECORD_PIECES_INTERVAL_MISMATCH"))
        if not all(is_sha(p.get("source_media_sha256")) for p in a1_pieces):
            out.append(_issue("FMR_RECORD_SOURCE_HASH_INVALID"))
    if record.get("auto_checks_sha256") != file_sha(root / EVIDENCE_DIR / AUTO_CHECKS_NAME):
        out.append(_issue("FMR_RECORD_AUTO_CHECKS_DRIFT"))
    return out


def audit_if_finished_media(root: Path, manifest: Mapping[str, Any]) -> list[dict[str, str]] | None:
    """``None`` for every non-FMR manifest; otherwise the lane's blocking issues."""
    if manifest.get("schema_version") != PACKAGE_SCHEMA:
        return None
    return package_problems(Path(root))


def attestation_problems(manifest: Mapping[str, Any]) -> list[str]:
    """Upload-time binding between the manifest and the package's receipt/member."""
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, Mapping):
        return ["manifest v3 has no package_attestation object"]
    entry = attestation.get("finished_media_review")
    problems: list[str] = []
    if not isinstance(entry, Mapping):
        return ["package_attestation.finished_media_review is missing"]
    receipt_file = Path(str(entry.get("path") or ""))
    if not receipt_file.is_file():
        return [f"finished-media review receipt missing: {receipt_file}"]
    if entry.get("sha256") != file_sha(receipt_file).removeprefix("sha256:"):
        problems.append("finished-media review receipt HASH DRIFT")
    try:
        receipt = load_json(receipt_file)
        root = Path(str(attestation.get("package_root") or "")).resolve()
        member = load_json(member_copy_path(root, str(receipt.get("candidate_id") or "")))
    except FmrError as exc:
        return problems + [str(exc)]
    if receipt.get("schema_version") != RECEIPT_SCHEMA or receipt.get("verdict") != "PASS":
        problems.append("finished-media review receipt verdict is not PASS")
    if receipt_file.resolve().parent != root:
        problems.append("finished-media review receipt is not inside the package root")
    try:
        if load_json(root / "review_manifest.json").get("schema_version") != PACKAGE_SCHEMA:
            problems.append("finished-media review attestation needs a finished-media review package manifest")
    except FmrError as exc:
        problems.append(str(exc))
    bound_member = (receipt.get("bindings") or {}).get("member") or {}
    if bound_member.get("sha256") != file_sha(member_copy_path(root, receipt["candidate_id"])):
        problems.append("finished-media review member binding drifted")
    if manifest.get("description_extra") != member.get("description_extra"):
        problems.append("manifest description_extra does not equal the reviewed member description_extra")
    if str(manifest.get("title") or "") != (root / f"{receipt['candidate_id']}.title.txt").read_text(
            encoding="utf-8").strip():
        problems.append("manifest title differs from the reviewed title.txt")
    return problems


def same_bv_replay_problems(
    package_root: Path, review_manifest: Path, receipt_file: Path, receipt: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> list[str]:
    """Why this receipt cannot be the final review of a same-BV replacement (empty: accepted).

    The plan stores and replays the attestation without the rest of the manifest, so only the
    package-level closure is checked here (``package_problems``); title/description bindings stay
    with ``attestation_problems`` in verify.
    """
    cid = receipt.get("candidate_id")
    if not isinstance(manifest.get("recovery_publication_authority"), Mapping):
        return ["same-BV review requires publication authority"]
    if (not isinstance(cid, str) or review_manifest != Path(package_root) / "review_manifest.json"
            or receipt_file != receipt_path(package_root, cid)):
        return ["receipt is not the package's bound review receipt"]
    return [f"{row['code']}:{row.get('detail', '')}" for row in package_problems(Path(package_root))]
