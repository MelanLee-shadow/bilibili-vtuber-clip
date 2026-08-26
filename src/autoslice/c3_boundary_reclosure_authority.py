"""C3-specific, provider-free boundary reclosure authority.

This module is intentionally narrower than the ordinary boundary lanes.  It
consumes one committed Ivan response to rebind the already-reviewed delivery
endpoint from the stale 34-cue projection to the exact 11-cue release SRT.
It never changes media, text, endpoint milliseconds, or generic validators.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.autoslice.boundary_semantic_review import cue_grid_sha256, semantic_review_sha256
from src.autoslice.repository_asset_authority import require_repository_asset_authority

CANDIDATE_ID = "auto_220021_561_670"
RECORDING_DATE = "2026-08-13"
SCHEMA_VERSION = "c3-boundary-reclosure-authority.v1"
RECEIPT_SCHEMA_VERSION = "c3-derived-boundary-reclosure-receipt.v1"
AUTHORITY_RELATIVE_PATH = Path("assets/lidousha/fastlane_c3_boundary_reclosure_authority") / f"{CANDIDATE_ID}.v1.json"
AUTHORITY_FILE_SHA256 = "sha256:b9d448eff093f21b2ad83dd33877d206d4e138c7d1ef7de16a81518b8fc9ca48"
OLD_GRID_SHA256 = "sha256:53788f5a442de13a61d610c5ed81a74e61e4519e2ba21d317c6a01b05cee116b"
NEW_GRID_SHA256 = "sha256:3b7b51328d413e8a3228b6d845c90b64b59251e588b01a20570863091634bf6e"
ENDPOINT_MS = 108_940
DELIVERY_END_MS = 109_040
SUPERSEDED_REASON_CODES = (
    "BOUNDARY_SEMANTIC_CUE_GRID_MISMATCH",
    "BOUNDARY_SEMANTIC_ENDPOINT_CUE_MISMATCH",
)
_RESPONSE_JSON = '{"answers":[{"id":"c3_boundary_authority","selected":["授权 11-cue → 108.940s 重绑定（Recommended）"]}]}'
RESPONSE_SHA256 = "sha256:f68d837ed3c1efaae2e69f4322ecde2f3e5e0309cb055a2238e6d774b2d98546"

class C3BoundaryReclosureError(ValueError):
    """The candidate-specific receipt cannot be proven."""


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))).encode("utf-8")


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha_json(value: object) -> str:
    return _sha(_canonical(value))


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _strict_regular(path: Path) -> bytes:
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise C3BoundaryReclosureError("C3_BOUNDARY_AUTHORITY_NOT_REGULAR")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(fd)
            chunks: list[bytes] = []
            while chunk := os.read(fd, 1 << 20):
                chunks.append(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_nlink) != (after.st_dev, after.st_ino, after.st_size, after.st_nlink):
            raise C3BoundaryReclosureError("C3_BOUNDARY_AUTHORITY_DRIFT")
        payload = b"".join(chunks)
        if len(payload) != before.st_size:
            raise C3BoundaryReclosureError("C3_BOUNDARY_AUTHORITY_DRIFT")
        return payload
    except C3BoundaryReclosureError:
        raise
    except OSError as exc:
        raise C3BoundaryReclosureError("C3_BOUNDARY_AUTHORITY_UNAVAILABLE") from exc


def _authority_self_seal(document: Mapping[str, object]) -> str:
    body = {key: value for key, value in document.items() if key != "authority_sha256"}
    return _sha_json(body)


def _expected_authority() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "authority_scope": "c3_candidate_specific_boundary_rebind_only",
        "authorization": {
            "session_id": "session-896490c8-ae47-4531-a7d1-a0ab2d81999e",
            "tool_call_id": "call_ugwFxQfoUrNvVeDVSR3GbJXG|fc_0b3cd3bcdc4f5bfb016a8e78df60ec87d1b5d53712dacded42",
            "user_message_id": "f08d8ae-28c9-44f0-8648-21006686a8db",
            "seq": 526487,
            "step": 322,
            "turn": 14,
            "observed_at": "2026-08-26T12:01:43.899000Z",
            "question_id": "c3_boundary_authority",
            "selected": "授权 11-cue → 108.940s 重绑定（Recommended）",
            "response_json": _RESPONSE_JSON,
            "response_sha256": RESPONSE_SHA256,
        },
        "asked_scope": {
            "question": "c3_boundary_authority",
            "meaning": "authorize a C3-specific derived boundary receipt binding final SRT d70a96 exact 11-cue grid to unchanged endpoint 108.940s",
            "unchanged": ["video", "subtitle_text", "cover", "endpoint"],
            "provider": False,
        },
        "line947_c3_authority": {
            "authority": "Ivan/Claude line947 exhaustive C3 ruling",
            "uuid": "555195ed-ec18-418d-a311-558f7e54291f",
            "raw_sha256": "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa",
            "content_sha256": "sha256:0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b",
            "baseline_sha256": "sha256:d70a96c402df8313264ef6ca145d69d5dbb74e7a2c48eb512e78f3f417e8eecc",
            "boundary_change": False,
        },
        "v3_manifest": {
            "schema_version": "fastlane-c3-successor-authority-manifest.v3",
            "manifest_raw_sha256": "sha256:ecc19afdfa30bc3c978d1c29ef5a407f387251c04912b02ebcdbf37bc0de9f75",
            "manifest_self_seal": "sha256:0a5465acbb7d6457894638f9d62cbda59320474209ef8374c9b91213f9c15fa2",
            "root_tree_sha256": "sha256:7afbc9e4489f523890538402d497d2f9075a77387bbcf2c5ee3654547b7577f2",
        },
        "boundary": {
            "old_cue_count": 34,
            "old_grid_sha256": OLD_GRID_SHA256,
            "new_cue_count": 11,
            "new_grid_sha256": NEW_GRID_SHA256,
            "endpoint_ms": ENDPOINT_MS,
            "delivery_end_ms": DELIVERY_END_MS,
            "superseded_reason_codes": list(SUPERSEDED_REASON_CODES),
        },
        "final_assets": {
            "burned_video_sha256": "sha256:c5d49bdff6842298f9a9cc1907044faa20dc8cae3d7717e99e80bd51aa2f9faf",
            "delivery_video_sha256": "sha256:2e94ba7ae18e64903baca4cadb647b9545915778b19082e3cd0582d32e20c84e",
            "plain_srt_sha256": "sha256:d70a96c402df8313264ef6ca145d69d5dbb74e7a2c48eb512e78f3f417e8eecc",
            "speaker_srt_sha256": "sha256:51eef37bf38a2e1a58e4412115c5f2a9699904df80a70f6d8a406d6dde206153",
            "speaker_ass_sha256": "sha256:d3160f325648915db0ebb6f4f05e48cc6e6cf25caccf726759c654d02829838a",
            "cover_sha256": "sha256:7e77ab5d0141879caa7d24ae264f9d3ab94ea9fbdc80d7a4633e9a0a07758df3",
            "chat_sha256": "sha256:88728f603043603c59c77b0f57364309f8215640c9baa5a47d201df627e3981c",
            "record_sha256": "sha256:76966e096593d1b132a1f9155d30093866b5b5f28df2141adb3794ce640c8a21",
            "publish_sha256": "sha256:f711b5db6522a7064570a548db48b413b5bf1a5c94614b36da82e544c493b09d",
            "clip_context_sha256": "sha256:fb2d67110858fe3fcc348278856005e45f1ab790785a7d3dd0615103eb423987",
        },
        "permissions": {
            "provider": False,
            "ssh": False,
            "state": False,
            "registry": False,
            "ledger": False,
            "upload": False,
            "deploy": False,
            "video_change": False,
            "subtitle_text_change": False,
            "cover_change": False,
            "endpoint_change": False,
        },
    }


def validate_authority_document(document: object) -> dict[str, object]:
    if not isinstance(document, Mapping):
        raise C3BoundaryReclosureError("C3_BOUNDARY_AUTHORITY_INVALID")
    value = dict(document)
    declared = value.get("authority_sha256")
    if not isinstance(declared, str) or declared != _authority_self_seal(value):
        raise C3BoundaryReclosureError("C3_BOUNDARY_AUTHORITY_SELF_SEAL_DRIFT")
    expected = _expected_authority()
    if {key: item for key, item in value.items() if key != "authority_sha256"} != expected:
        raise C3BoundaryReclosureError("C3_BOUNDARY_AUTHORITY_PROVENANCE_DRIFT")
    return value


def load_c3_boundary_authority(*, repo_root: Path) -> dict[str, object]:
    """Load the sole committed C3 authority and require Git/deploy provenance."""
    root = Path(repo_root).resolve(strict=True)
    path = root / AUTHORITY_RELATIVE_PATH
    payload = _strict_regular(path)
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C3BoundaryReclosureError("C3_BOUNDARY_AUTHORITY_JSON_INVALID") from exc
    value = validate_authority_document(document)
    try:
        require_repository_asset_authority(
            repo_root=root, relative_path=AUTHORITY_RELATIVE_PATH, observed_bytes=payload
        )
    except Exception as exc:
        raise C3BoundaryReclosureError("C3_BOUNDARY_AUTHORITY_REPOSITORY_UNPROVEN") from exc
    return value


def _cue_text(cue: object) -> str:
    return str(getattr(cue, "text", "") or "")


def _current_cues(cues: Sequence[object]) -> list[object]:
    return [cue for cue in cues if _cue_text(cue).strip()]


def _require_pass(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or value.get("status") != "PASS":
        raise C3BoundaryReclosureError(f"C3_BOUNDARY_{label}_NOT_PASS")
    if label == "OWNER" and value.get("failures") != []:
        raise C3BoundaryReclosureError("C3_BOUNDARY_OWNER_NOT_PASS")
    if label == "COVERAGE" and value.get("failure") is not None:
        raise C3BoundaryReclosureError("C3_BOUNDARY_COVERAGE_NOT_PASS")
    return value


def _receipt_without_seal(receipt: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in receipt.items() if key != "receipt_sha256"}


def derive_current_boundary_review(
    *,
    stale_review: Mapping[str, object],
    cues: Sequence[object],
    owner_verification: Mapping[str, object],
    coverage_verification: Mapping[str, object],
    authority: Mapping[str, object],
) -> dict[str, object]:
    """Derive a current 11-cue review without making a new content judgment."""
    auth = validate_authority_document(authority)
    if auth.get("candidate_id") != CANDIDATE_ID:
        raise C3BoundaryReclosureError("C3_BOUNDARY_CANDIDATE_DRIFT")
    if not isinstance(stale_review, Mapping) or stale_review.get("status") != "PASS" or stale_review.get("review_scope") != "final_delivery":
        raise C3BoundaryReclosureError("C3_BOUNDARY_STALE_REVIEW_INVALID")
    if stale_review.get("cue_grid_sha256") != OLD_GRID_SHA256 or stale_review.get("recommended_end_cue_index") != 34 or stale_review.get("recommended_end_ms") != ENDPOINT_MS:
        raise C3BoundaryReclosureError("C3_BOUNDARY_STALE_REVIEW_BINDING_DRIFT")
    old_endpoint = stale_review.get("final_endpoint_binding")
    if not isinstance(old_endpoint, Mapping) or old_endpoint.get("final_cue_grid_sha256") != OLD_GRID_SHA256 or old_endpoint.get("final_closure_cue_index") != 34 or old_endpoint.get("final_snapped_end_ms") != ENDPOINT_MS or old_endpoint.get("final_end_ms") != DELIVERY_END_MS:
        raise C3BoundaryReclosureError("C3_BOUNDARY_STALE_ENDPOINT_DRIFT")
    owner = _require_pass(owner_verification, "OWNER")
    coverage = _require_pass(coverage_verification, "COVERAGE")
    current = _current_cues(cues)
    if len(current) != 11 or cue_grid_sha256(current) != NEW_GRID_SHA256 or not _int(getattr(current[-1], "end_ms", None)) or current[-1].end_ms != ENDPOINT_MS:
        raise C3BoundaryReclosureError("C3_BOUNDARY_CURRENT_GRID_DRIFT")
    closure_sha = _sha(_cue_text(current[-1]).encode("utf-8"))
    receipt: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "PASS",
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "authority_asset_sha256": AUTHORITY_FILE_SHA256,
        "authority_self_seal": auth["authority_sha256"],
        "authorization_response_sha256": RESPONSE_SHA256,
        "superseded_reason_codes": list(SUPERSEDED_REASON_CODES),
        "old_review": {"cue_count": 34, "cue_grid_sha256": OLD_GRID_SHA256, "endpoint_ms": ENDPOINT_MS, "endpoint_cue_index": 34},
        "current_review": {"cue_count": 11, "cue_grid_sha256": NEW_GRID_SHA256, "endpoint_ms": ENDPOINT_MS, "endpoint_cue_index": 11, "delivery_end_ms": DELIVERY_END_MS, "closure_text_sha256": closure_sha},
        "owner_verification": dict(owner),
        "coverage_verification": dict(coverage),
        "unchanged": {"video": True, "subtitle_text": True, "cover": True, "endpoint": True},
        "permissions": {"provider": False, "ssh": False, "state": False, "registry": False, "ledger": False, "upload": False, "deploy": False},
        "derived_from_review_sha256": semantic_review_sha256(stale_review),
    }
    receipt["receipt_sha256"] = _sha_json(receipt)
    result = dict(stale_review)
    result.update({
        "cue_grid_sha256": NEW_GRID_SHA256,
        "recommended_end_cue_index": 11,
        "evidence_cue_indexes": [10, 11],
        "target_cue_index": 11,
        "final_endpoint_binding": {
            **dict(old_endpoint),
            "semantic_cue_grid_sha256": NEW_GRID_SHA256,
            "final_cue_grid_sha256": NEW_GRID_SHA256,
            "recommended_end_cue_index": 11,
            "final_closure_cue_index": 11,
            "closure_text_sha256": closure_sha,
            "reason_codes": [],
        },
        "c3_derived_boundary_reclosure_receipt": receipt,
        "c3_derived_from_review_sha256": receipt["derived_from_review_sha256"],
        "c3_boundary_reclosure_authority": {"schema_version": SCHEMA_VERSION, "authority_sha256": auth["authority_sha256"]},
        "reason_codes": ["C3_IVAN_AUTHORIZED_BOUNDARY_REBIND"],
    })
    return result


def validate_derived_boundary_receipt(
    receipt: object,
    *,
    review: Mapping[str, object],
    cues: Sequence[object],
    authority: Mapping[str, object],
    owner_verification: Mapping[str, object],
    coverage_verification: Mapping[str, object],
) -> bool:
    """Independently verify receipt, current grid, endpoint, and authority."""
    try:
        auth = validate_authority_document(authority)
        if not isinstance(receipt, Mapping): return False
        body = _receipt_without_seal(receipt)
        if receipt.get("receipt_sha256") != _sha_json(body): return False
        current = _current_cues(cues)
        if len(current) != 11 or cue_grid_sha256(current) != NEW_GRID_SHA256 or current[-1].end_ms != ENDPOINT_MS: return False
        if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION or receipt.get("status") != "PASS" or receipt.get("candidate_id") != CANDIDATE_ID or receipt.get("recording_date") != RECORDING_DATE: return False
        if receipt.get("authority_self_seal") != auth.get("authority_sha256") or receipt.get("authorization_response_sha256") != RESPONSE_SHA256: return False
        if receipt.get("authority_asset_sha256") != AUTHORITY_FILE_SHA256: return False
        if receipt.get("superseded_reason_codes") != list(SUPERSEDED_REASON_CODES): return False
        if receipt.get("derived_from_review_sha256") != review.get("c3_derived_from_review_sha256"): return False
        old = receipt.get("old_review"); cur = receipt.get("current_review")
        if old != {"cue_count": 34, "cue_grid_sha256": OLD_GRID_SHA256, "endpoint_ms": ENDPOINT_MS, "endpoint_cue_index": 34}: return False
        if not isinstance(cur, Mapping) or cur.get("cue_count") != 11 or cur.get("cue_grid_sha256") != NEW_GRID_SHA256 or cur.get("endpoint_ms") != ENDPOINT_MS or cur.get("endpoint_cue_index") != 11 or cur.get("delivery_end_ms") != DELIVERY_END_MS or cur.get("closure_text_sha256") != _sha(_cue_text(current[-1]).encode()): return False
        if receipt.get("owner_verification") != dict(owner_verification) or receipt.get("coverage_verification") != dict(coverage_verification): return False
        if receipt.get("unchanged") != {"video": True, "subtitle_text": True, "cover": True, "endpoint": True}: return False
        if receipt.get("permissions") != {"provider": False, "ssh": False, "state": False, "registry": False, "ledger": False, "upload": False, "deploy": False}: return False
        endpoint = review.get("final_endpoint_binding")
        return bool(
            review.get("status") == "PASS" and review.get("review_scope") == "final_delivery" and review.get("cue_grid_sha256") == NEW_GRID_SHA256 and review.get("recommended_end_cue_index") == 11 and review.get("recommended_end_ms") == ENDPOINT_MS and isinstance(endpoint, Mapping) and endpoint.get("semantic_cue_grid_sha256") == NEW_GRID_SHA256 and endpoint.get("final_cue_grid_sha256") == NEW_GRID_SHA256 and endpoint.get("recommended_end_cue_index") == 11 and endpoint.get("final_closure_cue_index") == 11 and endpoint.get("final_snapped_end_ms") == ENDPOINT_MS and endpoint.get("final_end_ms") == DELIVERY_END_MS and review.get("c3_derived_boundary_reclosure_receipt") == dict(receipt)
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def apply_c3_boundary_reclosure(
    *, stale_review: Mapping[str, object], cues: Sequence[object], owner_verification: Mapping[str, object], coverage_verification: Mapping[str, object], authority: Mapping[str, object] | None = None, repo_root: Path | None = None,
) -> dict[str, object] | None:
    """Apply only the exact C3 rebind; all other candidates return ``None``."""
    if stale_review.get("candidate_id") != CANDIDATE_ID or stale_review.get("review_scope") != "final_delivery":
        return None
    auth = authority if authority is not None else load_c3_boundary_authority(repo_root=repo_root or Path.cwd())
    return derive_current_boundary_review(stale_review=stale_review, cues=cues, owner_verification=owner_verification, coverage_verification=coverage_verification, authority=auth)

# Compatibility aliases make the typed contract discoverable to callers without
# introducing a second implementation.
load_authority = load_c3_boundary_authority
build_derived_boundary_receipt = derive_current_boundary_review
validate_boundary_receipt = validate_derived_boundary_receipt
