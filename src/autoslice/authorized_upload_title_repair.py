"""One-shot, source-fact-bound title repair for an already published BV.

This module deliberately has no upload or publication framework of its own.  It
uses the existing member adapter supplied by :mod:`scripts.authorized_upload`,
and persists only the small receipt needed to make the Creator edit
append-at-most-once.  Every invocation re-reads the original manifest,
record, subtitle, publication proof, and the live three-surface snapshot.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from src.autoslice.addressee_attribution import (
    SpeakerEvidenceRejected,
    build_addressee_evidence,
)
from src.autoslice.authorized_upload_recovery_cli import write_json_sidecar
from src.autoslice.channel_envelope import accepted_talk_title_prefix
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.part_name_contract import part_name_is_title, same_stable
from src.autoslice.publication_identity import publication_identity
from src.autoslice.review_evidence import SourceCue
from src.autoslice.same_bv_repair import normalise_creator_snapshot, snapshots_equivalent
from src.autoslice.source_fact_review import validate_source_fact_review
from src.autoslice.title_cover_quotes import check_title_quotes
from src.autoslice.title_policy import publish_title_lane, publish_title_policy_violations


SCHEMA_VERSION = "same-bv-title-repair.v1"
TITLE_INPUT_FIELDS = frozenset(
    {
        "manifest",
        "manifest_sha256",
        "record_path",
        "record_sha256",
        "srt_path",
        "srt_sha256",
        "bvid",
        "candidate_id",
        "original_title",
        "original_selection_hook",
        "source_fact_inputs",
    }
)
TITLE_RESULT_FIELDS = frozenset(
    {"status", "final_title", "final_selection_hook", "source_fact_review", "input_sha256"}
)
SOURCE_FACT_INPUT_FIELDS = frozenset(
    {"final_transcript", "clip_context_prompt", "selection_scorecard", "speaker_evidence", "story_contract"}
)

EDIT_STATES = {
    "PLANNED",
    "EDIT_INTENT",
    "EDIT_ACCEPTED",
    "EDIT_OUTCOME_UNKNOWN",
    "SECTION_TITLE_SYNC_INTENT",
    "SECTION_TITLE_SYNC_ACCEPTED",
    "SECTION_TITLE_SYNC_OUTCOME_UNKNOWN",
    "PUBLIC_PENDING",
    "VERIFIED_TITLE_ONLY",
    "BLOCKED_NON_TITLE_DRIFT",
}
TERMINAL_STATES = {"VERIFIED_TITLE_ONLY", "BLOCKED_NON_TITLE_DRIFT"}


class TitleRepairError(ValueError):
    """A title repair proposal, receipt, or live identity is invalid."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return "sha256:" + _sha256_bytes(_canonical_json(value))


def _sha256_file(path: Path) -> str:
    from .evidence_identity import file_sha256
    try:
        return file_sha256(path)
    except OSError as exc:
        raise TitleRepairError(f"TITLE_REPAIR_ARTIFACT_UNREADABLE:{path}") from exc


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TitleRepairError(f"TITLE_REPAIR_{label}_UNREADABLE") from exc
    if not isinstance(value, Mapping):
        raise TitleRepairError(f"TITLE_REPAIR_{label}_SCHEMA_INVALID")
    return dict(value)


def _normal_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.removeprefix("sha256:")
    if len(candidate) != 64:
        return None
    try:
        int(candidate, 16)
    except ValueError:
        return None
    return candidate


def _sha_matches(value: object, actual: str) -> bool:
    return _normal_sha(value) == actual


def _required_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TitleRepairError(code)
    return value


def _regular_path(value: object, *, label: str, base: Path | None = None) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute() and base is not None:
        path = base / path
    if path.is_symlink() or not path.is_file():
        raise TitleRepairError(f"TITLE_REPAIR_{label}_INVALID")
    return path.resolve()


def _entry_path(entry: object, *, label: str) -> tuple[Path, str]:
    if not isinstance(entry, Mapping):
        raise TitleRepairError(f"TITLE_REPAIR_{label}_ATTESTATION_MISSING")
    path = _regular_path(entry.get("path"), label=label)
    actual = _sha256_file(path)
    if not _sha_matches(entry.get("sha256"), actual):
        raise TitleRepairError(f"TITLE_REPAIR_{label}_HASH_DRIFT")
    return path, actual


def _load_original_artifacts(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    title_input: Mapping[str, Any],
) -> tuple[dict[str, Any], Path, str, Path, str, str, dict[str, Any]]:
    """Read the immutable manifest-bound record and subtitle once per run."""

    if set(title_input) != TITLE_INPUT_FIELDS:
        raise TitleRepairError("TITLE_REPAIR_TITLE_INPUT_SCHEMA_INVALID")
    manifest_sha = _sha256_file(manifest_path)
    if not _sha_matches(title_input.get("manifest_sha256"), manifest_sha):
        raise TitleRepairError("TITLE_REPAIR_MANIFEST_BINDING_INVALID")
    if Path(str(title_input.get("manifest"))).resolve() != manifest_path.resolve():
        raise TitleRepairError("TITLE_REPAIR_MANIFEST_PATH_BINDING_INVALID")
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, Mapping):
        raise TitleRepairError("TITLE_REPAIR_MANIFEST_ATTESTATION_MISSING")
    record_path, record_sha = _entry_path(attestation.get("record"), label="RECORD")
    subtitle_path, subtitle_sha = _entry_path(attestation.get("subtitle"), label="SRT")
    if Path(str(title_input.get("record_path"))).resolve() != record_path.resolve():
        raise TitleRepairError("TITLE_REPAIR_RECORD_PATH_BINDING_INVALID")
    if Path(str(title_input.get("srt_path"))).resolve() != subtitle_path.resolve():
        raise TitleRepairError("TITLE_REPAIR_SRT_PATH_BINDING_INVALID")
    if not _sha_matches(title_input.get("record_sha256"), record_sha):
        raise TitleRepairError("TITLE_REPAIR_RECORD_BINDING_INVALID")
    if not _sha_matches(title_input.get("srt_sha256"), subtitle_sha):
        raise TitleRepairError("TITLE_REPAIR_SRT_BINDING_INVALID")
    record = _read_json(record_path, "RECORD")
    return (
        record,
        subtitle_path,
        subtitle_sha,
        record_path,
        record_sha,
        manifest_sha,
        dict(attestation),
    )


def _record_candidate(record: Mapping[str, Any]) -> str:
    story = record.get("story_contract")
    candidate = record.get("candidate_id")
    if not isinstance(candidate, str) or not candidate.strip():
        candidate = story.get("candidate_id") if isinstance(story, Mapping) else None
    return _required_text(candidate, "TITLE_REPAIR_RECORD_CANDIDATE_MISSING")


def _current_source_fact_inputs(
    *,
    record: Mapping[str, Any],
    subtitle_path: Path,
) -> dict[str, Any]:
    story = record.get("story_contract")
    if not isinstance(story, Mapping):
        raise TitleRepairError("TITLE_REPAIR_STORY_CONTRACT_MISSING")
    _required_text(
        story.get("selection_hook"), "TITLE_REPAIR_SELECTION_HOOK_MISSING"
    )
    if "clip_context_prompt" not in story or not isinstance(
        story.get("clip_context_prompt"), str
    ):
        raise TitleRepairError("TITLE_REPAIR_CLIP_CONTEXT_MISSING")
    scorecard = copy.deepcopy(story.get("selection_scorecard"))
    try:
        cues = [
            SourceCue(int(cue.index), cue.start_ms, cue.end_ms, cue.text)
            for cue in parse_srt_cues(subtitle_path.read_text(encoding="utf-8"))
        ]
        final_transcript, speaker_result = build_addressee_evidence(record, cues)
    except (OSError, UnicodeError, ValueError, TypeError, SpeakerEvidenceRejected) as exc:
        raise TitleRepairError("TITLE_REPAIR_SPEAKER_EVIDENCE_INVALID") from exc
    speaker_evidence = speaker_result.speaker_evidence
    if not isinstance(speaker_evidence, Mapping):
        raise TitleRepairError("TITLE_REPAIR_SPEAKER_EVIDENCE_INVALID")
    return {
        "final_transcript": final_transcript,
        "clip_context_prompt": str(story.get("clip_context_prompt")),
        "selection_scorecard": scorecard,
        "speaker_evidence": copy.deepcopy(dict(speaker_evidence)),
        "story_contract": copy.deepcopy(dict(story)),
    }


def _proposal_source_inputs_match(
    proposal_inputs: Mapping[str, Any],
    current_inputs: Mapping[str, Any],
) -> None:
    if set(proposal_inputs) != SOURCE_FACT_INPUT_FIELDS:
        raise TitleRepairError("TITLE_REPAIR_SOURCE_FACT_INPUTS_SCHEMA_INVALID")
    for key in SOURCE_FACT_INPUT_FIELDS:
        if proposal_inputs.get(key) != current_inputs.get(key):
            raise TitleRepairError(f"TITLE_REPAIR_SOURCE_FACT_{key.upper()}_DRIFT")


def _load_and_validate_proposal(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    title_input_path: Path,
    title_result_path: Path,
    record: Mapping[str, Any],
    subtitle_path: Path,
    subtitle_sha: str,
    record_sha: str,
    manifest_sha: str,
    predecessor_title: str | None = None,
) -> dict[str, Any]:
    title_input = _read_json(title_input_path, "TITLE_INPUT")
    title_result = _read_json(title_result_path, "TITLE_RESULT")
    if set(title_input) != TITLE_INPUT_FIELDS:
        raise TitleRepairError("TITLE_REPAIR_TITLE_INPUT_SCHEMA_INVALID")
    if set(title_result) != TITLE_RESULT_FIELDS:
        raise TitleRepairError("TITLE_REPAIR_TITLE_RESULT_SCHEMA_INVALID")
    if title_result.get("status") != "OK":
        raise TitleRepairError("TITLE_REPAIR_RESULT_NOT_OK")
    original_title = _required_text(title_input.get("original_title"), "TITLE_REPAIR_ORIGINAL_TITLE_MISSING")
    if (predecessor_title if predecessor_title is not None else manifest.get("title")) != original_title:
        raise TitleRepairError("TITLE_REPAIR_MANIFEST_TITLE_DRIFT")
    final_title = _required_text(title_result.get("final_title"), "TITLE_REPAIR_FINAL_TITLE_MISSING")
    if final_title == original_title:
        raise TitleRepairError("TITLE_REPAIR_TITLE_UNCHANGED")
    lane = publish_title_lane(original_title)
    if publish_title_policy_violations(final_title, lane=lane):
        raise TitleRepairError("TITLE_REPAIR_FINAL_TITLE_POLICY_INVALID")
    bvid = _required_text(title_input.get("bvid"), "TITLE_REPAIR_BVID_MISSING")
    candidate_id = _record_candidate(record)
    if title_input.get("candidate_id") != candidate_id:
        raise TitleRepairError("TITLE_REPAIR_CANDIDATE_BINDING_INVALID")
    input_hook = _required_text(
        title_input.get("original_selection_hook"),
        "TITLE_REPAIR_ORIGINAL_SELECTION_HOOK_MISSING",
    )
    story = record.get("story_contract")
    if not isinstance(story, Mapping) or story.get("selection_hook") != input_hook:
        raise TitleRepairError("TITLE_REPAIR_SELECTION_HOOK_BINDING_INVALID")
    result_hook = title_result.get("final_selection_hook")
    if result_hook != input_hook:
        raise TitleRepairError("TITLE_REPAIR_TITLE_ONLY_HOOK_DRIFT")
    if not _sha_matches(title_input.get("manifest_sha256"), manifest_sha):
        raise TitleRepairError("TITLE_REPAIR_MANIFEST_BINDING_INVALID")
    if not _sha_matches(title_input.get("srt_sha256"), subtitle_sha):
        raise TitleRepairError("TITLE_REPAIR_SRT_BINDING_INVALID")
    if not _sha_matches(title_input.get("record_sha256"), record_sha):
        raise TitleRepairError("TITLE_REPAIR_RECORD_BINDING_INVALID")
    current_inputs = _current_source_fact_inputs(record=record, subtitle_path=subtitle_path)
    proposal_inputs = title_input.get("source_fact_inputs")
    if not isinstance(proposal_inputs, Mapping):
        raise TitleRepairError("TITLE_REPAIR_SOURCE_FACT_INPUTS_MISSING")
    _proposal_source_inputs_match(proposal_inputs, current_inputs)
    if not _sha_matches(title_result.get("input_sha256"), _sha256_file(title_input_path)):
        raise TitleRepairError("TITLE_REPAIR_SOURCE_FACT_INPUT_HASH_INVALID")
    review = title_result.get("source_fact_review")
    if not isinstance(review, Mapping):
        raise TitleRepairError("TITLE_REPAIR_SOURCE_FACT_REVIEW_MISSING")
    review = dict(review)
    if review.get("status") != "PASS":
        raise TitleRepairError("TITLE_REPAIR_SOURCE_FACT_REVIEW_NOT_PASS")
    if _normal_sha(review.get("receipt_sha256")) is None:
        raise TitleRepairError("TITLE_REPAIR_SOURCE_FACT_REVIEW_HASH_INVALID")
    # An editorial replacement can already be factually correct.  Its fresh
    # KEEP receipt starts at the proposed title, while the immutable manifest
    # and title input continue to bind the current published title.  Retain
    # the older original-title REPAIR chain; never accept an unrelated review
    # origin.  The full receipt replay below still checks all source bindings.
    proposed_title_keep = (
        review.get("original_title") == final_title
        and review.get("decision") == "KEEP"
    )
    if (
        (review.get("original_title") != original_title and not proposed_title_keep)
        or review.get("original_selection_hook") != input_hook
        or review.get("final_title") != final_title
        or review.get("final_selection_hook") != input_hook
    ):
        raise TitleRepairError("TITLE_REPAIR_SOURCE_FACT_REVIEW_SURFACE_INVALID")
    validate_kwargs: dict[str, Any] = {
        "selection_hook": input_hook,
        "title": final_title,
        "final_transcript": current_inputs["final_transcript"],
        "clip_context_prompt": current_inputs["clip_context_prompt"],
        "selection_scorecard": current_inputs["selection_scorecard"],
        "candidate_id": candidate_id,
        "final_reviewed_srt_path": subtitle_path,
        "story_contract": current_inputs["story_contract"],
    }
    validate_kwargs["speaker_evidence"] = current_inputs["speaker_evidence"]
    try:
        valid = validate_source_fact_review(review, **validate_kwargs)
    except Exception as exc:
        raise TitleRepairError("TITLE_REPAIR_SOURCE_FACT_REVIEW_INVALID") from exc
    if not valid:
        raise TitleRepairError("TITLE_REPAIR_SOURCE_FACT_REVIEW_INVALID")
    return {
        "bvid": bvid,
        "candidate_id": candidate_id,
        "original_title": original_title,
        "original_selection_hook": input_hook,
        "final_title": final_title,
        "final_selection_hook": input_hook,
        "manifest_sha256": manifest_sha,
        "record_sha256": record_sha,
        "srt_sha256": subtitle_sha,
        "title_input_sha256": _sha256_file(title_input_path),
        "title_result_sha256": _sha256_file(title_result_path),
        "source_fact_review_sha256": _normal_sha(str(review.get("receipt_sha256") or "")),
        "source_fact_review": review,
    }


def _stable_without_title(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(dict(snapshot))
    for surface_name in ("creator", "public"):
        surface = value.get(surface_name)
        if not isinstance(surface, dict):
            continue
        metadata = surface.get("metadata")
        if isinstance(metadata, dict):
            metadata.pop("title", None)
        # State is the only deliberately ignored pending field.  Identity,
        # metadata, videos, and exact-section topology stay byte-for-byte.
        surface.pop("state", None)
        surface.pop("state_desc", None)
    section = value.get("section")
    if isinstance(section, dict):
        matches = section.get("matches")
        if isinstance(matches, list):
            for row in matches:
                if isinstance(row, dict):
                    row.pop("title", None)
    return value


def _stable_without_title_and_desc(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Like ``_stable_without_title`` but also ignoring the description.

    Only the FMR predecessor comparison uses it: 维护者 may hand-edit a
    description in the Bilibili backend between two transactions, and the
    next title edit then starts from the live description.  Identity (CID,
    aid, section), tags, cover and every other field stay compared.
    """

    value = _stable_without_title(snapshot)
    for surface_name in ("creator", "public"):
        surface = value.get(surface_name)
        metadata = surface.get("metadata") if isinstance(surface, dict) else None
        if isinstance(metadata, dict):
            metadata.pop("desc", None)
    return value


def _snapshot_sha(snapshot: Mapping[str, Any]) -> str:
    return _sha256_json(snapshot)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _surface_identity(
    snapshot: Mapping[str, Any], *, bvid: str, aid: int, cid: int, title: str | None
) -> bool:
    creator = snapshot.get("creator")
    public = snapshot.get("public")
    section = snapshot.get("section")
    if not all(
        isinstance(snapshot.get(name), Mapping)
        and snapshot[name].get("available") is True
        for name in ("creator", "public", "section")
    ):
        return False
    assert isinstance(creator, Mapping) and isinstance(public, Mapping) and isinstance(section, Mapping)
    creator_meta = creator.get("metadata")
    public_meta = public.get("metadata")
    videos = creator.get("videos")
    matches = section.get("matches")
    if not isinstance(creator_meta, Mapping) or not isinstance(public_meta, Mapping):
        return False
    if not isinstance(videos, list) or len(videos) != 1 or not isinstance(matches, list) or len(matches) != 1:
        return False
    match = matches[0]
    video = videos[0]
    return bool(
        (creator.get("bvid") in (None, bvid))
        and creator.get("aid") == aid
        and public.get("bvid") in (None, bvid)
        and public.get("aid") == aid
        and public.get("cid") == cid
        and video.get("cid") == cid
        and match.get("bvid") in (None, bvid)
        and match.get("aid") == aid
        and match.get("cid") == cid
        and (title is None or creator_meta.get("title") == title)
        and (title is None or public_meta.get("title") == title)
        and (title is None or match.get("title") == title)
    )


def _published_identity(
    *, manifest_path: Path, manifest: Mapping[str, Any], bvid: str
) -> tuple[dict[str, Any], int, int, int]:
    # Match ``scripts.authorized_upload.uploaded_sidecar_path`` exactly,
    # including the historical non-``.upload_manifest.json`` spelling.
    uploaded_path = _uploaded_sidecar_path(manifest_path)
    uploaded = _read_json(uploaded_path, "UPLOADED")
    manifest_sha = _sha256_file(manifest_path)
    if (
        uploaded.get("status") != "VERIFIED_PUBLIC"
        or uploaded.get("bvid") != bvid
        or not _sha_matches(uploaded.get("manifest_sha256"), manifest_sha)
        or uploaded.get("title") != manifest.get("title")
    ):
        raise TitleRepairError("TITLE_REPAIR_PUBLISHED_IDENTITY_INVALID")
    aid, cid = uploaded.get("aid"), uploaded.get("cid")
    if not _positive_int(aid) or not _positive_int(cid):
        raise TitleRepairError("TITLE_REPAIR_PUBLISHED_MEDIA_ID_INVALID")
    public_path = _regular_path(uploaded.get("public_verify"), label="PUBLIC_PROOF")
    if not _sha_matches(uploaded.get("public_verify_sha256"), _sha256_file(public_path)):
        raise TitleRepairError("TITLE_REPAIR_PUBLIC_PROOF_DRIFT")
    proof = _read_json(public_path, "PUBLIC_PROOF")
    expected = proof.get("expected")
    section_api = proof.get("section_api")
    section_id = expected.get("section_id") if isinstance(expected, Mapping) else None
    if (
        proof.get("status") != "VERIFIED_PUBLIC"
        or proof.get("bvid") != bvid
        or not isinstance(expected, Mapping)
        or expected.get("title") != manifest.get("title")
        or not _positive_int(section_id)
        or not isinstance(section_api, Mapping)
        or section_api.get("episode_match_count") != 1
    ):
        raise TitleRepairError("TITLE_REPAIR_EXACT_SECTION_PROOF_INVALID")
    return uploaded, int(aid), int(cid), int(section_id)


def _uploaded_sidecar_path(manifest_path: Path) -> Path:
    """Use the uploader's exact sidecar spelling for every manifest variant."""

    name = manifest_path.name
    stem = (
        name[: -len(".upload_manifest.json")]
        if name.endswith(".upload_manifest.json")
        else name
    )
    return manifest_path.parent / (stem + ".uploaded.json")


def _title_only_payload(
    *, session: Any, raw_view: Mapping[str, Any], final_title: str, bvid: str
) -> dict[str, Any]:
    baseline = session.build_edit_payload(raw_view)
    payload = session.build_edit_payload(raw_view, title=final_title)
    if payload.get("title") != final_title or payload.get("bvid") != bvid:
        raise TitleRepairError("TITLE_REPAIR_EDIT_PAYLOAD_IDENTITY_INVALID")
    for key in set(baseline) | set(payload):
        if key in {"title", "csrf"}:
            continue
        if key == "videos" and len(payload["videos"]) == 1:
            # The sole part name may move to the final title, nothing else.
            if payload["videos"][0] != {**baseline["videos"][0], "title": final_title}:
                raise TitleRepairError("TITLE_REPAIR_NON_TITLE_PAYLOAD_DRIFT")
            continue
        if payload.get(key) != baseline.get(key):
            raise TitleRepairError("TITLE_REPAIR_NON_TITLE_PAYLOAD_DRIFT")
    return dict(payload)


def _binding_from_receipt(receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    binding = receipt.get("binding")
    if not isinstance(binding, Mapping):
        raise TitleRepairError("TITLE_REPAIR_RECEIPT_BINDING_MISSING")
    return binding


def _validate_receipt(
    receipt: Mapping[str, Any], *, binding: Mapping[str, Any], before: Mapping[str, Any]
) -> dict[str, Any]:
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise TitleRepairError("TITLE_REPAIR_RECEIPT_SCHEMA_INVALID")
    if dict(receipt.get("binding") or {}) != dict(binding):
        raise TitleRepairError("TITLE_REPAIR_RECEIPT_BINDING_DRIFT")
    status = receipt.get("status")
    if status not in EDIT_STATES:
        raise TitleRepairError("TITLE_REPAIR_RECEIPT_STATE_INVALID")
    if receipt.get("before_sha256") != _snapshot_sha(receipt.get("before") or {}):
        raise TitleRepairError("TITLE_REPAIR_RECEIPT_BEFORE_HASH_INVALID")
    # The title is expected to change after a successful Creator edit.  The
    # caller compares the live snapshot with this immutable receipt and
    # persists BLOCKED_NON_TITLE_DRIFT when another field changed.  Keeping
    # that decision in the caller also makes the block durable instead of
    # turning an ordinary live drift into an uncategorized exception.
    if receipt.get("edit_calls") not in (0, 1) or receipt.get("section_sync_calls") not in (0, 1):
        raise TitleRepairError("TITLE_REPAIR_RECEIPT_CALL_COUNT_INVALID")
    if status in {"PLANNED"} and receipt.get("edit_calls") != 0:
        raise TitleRepairError("TITLE_REPAIR_RECEIPT_CALL_COUNT_INVALID")
    if status in {
        "EDIT_INTENT",
        "EDIT_ACCEPTED",
        "EDIT_OUTCOME_UNKNOWN",
        "SECTION_TITLE_SYNC_INTENT",
        "SECTION_TITLE_SYNC_ACCEPTED",
        "SECTION_TITLE_SYNC_OUTCOME_UNKNOWN",
        "PUBLIC_PENDING",
        "VERIFIED_TITLE_ONLY",
    } and receipt.get("edit_calls") != 1:
        raise TitleRepairError("TITLE_REPAIR_RECEIPT_CALL_COUNT_INVALID")
    if status in {
        "SECTION_TITLE_SYNC_INTENT",
        "SECTION_TITLE_SYNC_ACCEPTED",
        "SECTION_TITLE_SYNC_OUTCOME_UNKNOWN",
    } and receipt.get("section_sync_calls") != 1:
        raise TitleRepairError("TITLE_REPAIR_RECEIPT_CALL_COUNT_INVALID")
    return dict(receipt)


def _blocked(receipt: dict[str, Any], *, reason: str, after: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    receipt.update(status="BLOCKED_NON_TITLE_DRIFT", reason=reason, after=dict(after))
    return 5, receipt


def _cover_predecessor_proof(
    *, completed_path: Path, manifest_path: Path, manifest: Mapping[str, Any],
    expected_bvid: str,
) -> dict[str, Any]:
    """Replay one already completed cover-only predecessor.

    A title repair after a cover successor cannot reuse the first-publication
    ``uploaded`` sidecar: that sidecar describes a different manifest title.
    The normal cover completion validator is the authority for the current
    title/cover/live surfaces, so this helper only adds the small identity
    projection needed by the title receipt binding.
    """

    from src.autoslice import publication_reconciliation as reconciliation

    completed_path = completed_path.resolve()
    try:
        completed, bvid, aid, cid = reconciliation._validate_same_bv_cover_completed(
            completed_path=completed_path,
            manifest=dict(manifest),
            manifest_path=manifest_path.resolve(),
        )
        candidate_id, recording_date = reconciliation._candidate_and_date(manifest)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_COVER_INVALID") from exc
    if bvid != expected_bvid or not _positive_int(aid) or not _positive_int(cid):
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_COVER_IDENTITY_INVALID")
    if completed.get("candidate_id") != candidate_id:
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_COVER_CANDIDATE_INVALID")
    live = completed.get("live_snapshot")
    section = live.get("section") if isinstance(live, Mapping) else None
    section_id = section.get("section_id") if isinstance(section, Mapping) else None
    if not _positive_int(section_id):
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_COVER_SECTION_INVALID")
    try:
        completion_binding = reconciliation._sha_entry(completed_path)
    except (OSError, ValueError) as exc:
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_COVER_HASH_INVALID") from exc
    return {
        "completed": completion_binding,
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "bvid": bvid,
        "aid": int(aid),
        "cid": int(cid),
        "section_id": int(section_id),
        "title": _required_text(manifest.get("title"), "TITLE_REPAIR_MANIFEST_TITLE_MISSING"),
        "live_snapshot": live,
    }


def _cover_predecessor_binding(proof: Mapping[str, Any]) -> dict[str, Any]:
    completion = proof.get("completed")
    if not isinstance(completion, Mapping):
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_COVER_BINDING_INVALID")
    # ``published_sha256`` and ``public_verify_sha256`` are historical field
    # names in the title receipt schema.  For this route both bind the real
    # cover-completion receipt; no new upload or public-proof file is created.
    binding = {
        "predecessor_cover_completed": dict(completion),
        "published_sha256": completion.get("sha256"),
        "public_verify_sha256": completion.get("sha256"),
        "predecessor_cover_candidate_id": proof.get("candidate_id"),
        "predecessor_cover_recording_date": proof.get("recording_date"),
        "predecessor_cover_bvid": proof.get("bvid"),
        "predecessor_cover_aid": proof.get("aid"),
        "predecessor_cover_cid": proof.get("cid"),
        "predecessor_cover_section_id": proof.get("section_id"),
        "predecessor_cover_title": proof.get("title"),
    }
    if not _normal_sha(binding["published_sha256"]):
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_COVER_HASH_INVALID")
    return binding


def _manifest_media_binding(
    manifest: Mapping[str, Any], *, label: str
) -> dict[str, object]:
    """Return a verified path/bytes/hash binding for video or subtitle data."""

    if label == "VIDEO":
        entry = manifest.get("video")
    else:
        attestation = manifest.get("package_attestation")
        entry = attestation.get("subtitle") if isinstance(attestation, Mapping) else None
    if not isinstance(entry, Mapping):
        raise TitleRepairError(f"TITLE_REPAIR_{label}_ATTESTATION_MISSING")
    path, actual_sha = _entry_path(entry, label=label)
    actual_bytes = path.stat().st_size
    if type(entry.get("bytes")) is not int or entry.get("bytes") != actual_bytes:
        raise TitleRepairError(f"TITLE_REPAIR_{label}_BYTES_INVALID")
    return {"path": str(path), "sha256": actual_sha, "bytes": actual_bytes}


def _validate_cover_successor_context(
    *, current_manifest: Mapping[str, Any], current_record: Mapping[str, Any],
    first_manifest: Mapping[str, Any], incoming_authority: Mapping[str, Any],
    candidate_id: str, cover_proof: Mapping[str, Any],
    reference: Mapping[str, Any], visited: set[tuple[str, str]],
) -> None:
    """Validate the second title edit's current-title predecessor chain."""

    # A normal successor may materialize the immutable video/SRT under a new
    # package root.  Each side is still read and hash/byte checked above; the
    # identity comparison therefore excludes path spelling while retaining
    # the content proof that protects the first publication.
    current_video = _manifest_media_binding(current_manifest, label="VIDEO")
    first_video = _manifest_media_binding(first_manifest, label="VIDEO")
    current_srt = _manifest_media_binding(current_manifest, label="SRT")
    first_srt = _manifest_media_binding(first_manifest, label="SRT")
    if any(
        current_video[key] != first_video[key] for key in ("sha256", "bytes")
    ) or any(current_srt[key] != first_srt[key] for key in ("sha256", "bytes")):
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_MEDIA_DRIFT")
    current_authority = current_record.get("recovery_publication_authority")
    manifest_authority = current_manifest.get("recovery_publication_authority")
    if not isinstance(current_authority, Mapping) or manifest_authority != current_authority:
        raise TitleRepairError("TITLE_REPAIR_CURRENT_AUTHORITY_INVALID")
    if current_authority.get("source_publication") != incoming_authority.get(
        "source_publication"
    ):
        raise TitleRepairError("TITLE_REPAIR_CURRENT_SOURCE_PUBLICATION_DRIFT")
    if any(
        current_authority.get(key) != cover_proof.get(key)
        for key in ("candidate_id", "recording_date", "bvid", "aid", "cid")
    ):
        raise TitleRepairError("TITLE_REPAIR_CURRENT_COVER_IDENTITY_DRIFT")
    if current_authority.get("title") != current_manifest.get("title"):
        raise TitleRepairError("TITLE_REPAIR_CURRENT_TITLE_AUTHORITY_DRIFT")
    if incoming_authority.get("observed_public_title") != current_authority.get(
        "observed_public_title"
    ):
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_ORIGINAL_TITLE_DRIFT")

    from src.autoslice.new_bv_repair_authority import (
        validate_new_bv_repair_authority,
    )

    try:
        validated_current = validate_new_bv_repair_authority(
            current_authority,
            candidate_id=candidate_id,
            expected_final_title=str(current_manifest.get("title") or ""),
        )
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise TitleRepairError("TITLE_REPAIR_CURRENT_AUTHORITY_INVALID") from exc
    if any(
        validated_current.get(key) != cover_proof.get(key)
        for key in ("candidate_id", "recording_date", "bvid", "aid", "cid")
    ):
        raise TitleRepairError("TITLE_REPAIR_CURRENT_COVER_IDENTITY_DRIFT")
    previous_reference = current_record.get("verified_title_repair")
    if not isinstance(previous_reference, Mapping) or previous_reference == reference:
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_REFERENCE_INVALID")
    # The recursive call is bounded by receipt path/hash.  A repeated receipt
    # is a self-reference or cycle, never a valid second title transaction.
    _validate_title_repair_successor(
        previous_reference,
        authority=validated_current,
        candidate_id=candidate_id,
        title=str(current_manifest.get("title") or ""),
        _visited=visited,
    )


def _validate_title_repair_successor(
    reference: object, *, authority: Mapping[str, Any], candidate_id: str,
    title: str, _visited: set[tuple[str, str]],
) -> None:
    """Internal replay with a receipt cycle guard."""

    from src.autoslice.new_bv_repair_authority import (
        replay_first_publication_manifest,
        validate_new_bv_repair_authority,
    )

    if isinstance(reference, Mapping) and set(reference) == {"receipt"}:
        _validate_fmr_title_repair_successor(
            reference["receipt"], authority=authority, candidate_id=candidate_id,
            title=title, visited=_visited,
        )
        return
    if not isinstance(reference, Mapping) or set(reference) != {
        "receipt", "title_input", "title_result"
    }:
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_REFERENCE_INVALID")
    path_pairs = {
        key: _entry_path(reference[key], label=key.upper()) for key in reference
    }
    paths = {key: pair[0] for key, pair in path_pairs.items()}
    receipt_binding = path_pairs["receipt"][1]
    visit_key = (str(paths["receipt"]), receipt_binding)
    if visit_key in _visited:
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_REFERENCE_CYCLE")
    _visited.add(visit_key)
    validated = validate_new_bv_repair_authority(
        authority, candidate_id=candidate_id, expected_final_title=title
    )
    title_input = _read_json(paths["title_input"], "TITLE_INPUT")
    manifest_path = _regular_path(title_input.get("manifest"), label="MANIFEST")
    manifest = _read_json(manifest_path, "MANIFEST")
    first_manifest = replay_first_publication_manifest(validated)
    receipt = _read_json(paths["receipt"], "RECEIPT")
    recorded_binding = _binding_from_receipt(receipt)
    previous_title = None
    previous_reference = recorded_binding.get("predecessor_title_repair")
    if previous_reference is not None:
        previous_title = _title_predecessor_proof(
            previous_reference, manifest_path=manifest_path, manifest=manifest,
            visited=_visited,
        )
    predecessor_entry = recorded_binding.get("predecessor_cover_completed")
    cover_mode = predecessor_entry is not None
    cover_proof = None
    if cover_mode:
        predecessor_path, predecessor_sha = _entry_path(
            predecessor_entry, label="PREDECESSOR_COVER_COMPLETED"
        )
        cover_proof = _cover_predecessor_proof(
            completed_path=predecessor_path,
            manifest_path=manifest_path,
            manifest=manifest,
            expected_bvid=str(validated["bvid"]),
        )
        if cover_proof["completed"].get("sha256") != predecessor_sha:
            raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_COVER_HASH_INVALID")
        expected_cover_binding = _cover_predecessor_binding(cover_proof)
        if any(
            recorded_binding.get(key) != value
            for key, value in expected_cover_binding.items()
        ):
            raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_COVER_BINDING_DRIFT")
    elif manifest != first_manifest:
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_ORIGINAL_MANIFEST_DRIFT")
    record, subtitle_path, subtitle_sha, _record_path, record_sha, manifest_sha, _ = (
        _load_original_artifacts(
            manifest_path=manifest_path, manifest=manifest, title_input=title_input
        )
    )
    proposal = _load_and_validate_proposal(
        manifest_path=manifest_path, manifest=manifest,
        title_input_path=paths["title_input"], title_result_path=paths["title_result"],
        record=record, subtitle_path=subtitle_path, subtitle_sha=subtitle_sha,
        record_sha=record_sha, manifest_sha=manifest_sha,
        predecessor_title=previous_title["final_title"] if previous_title else None,
    )
    bvid = str(validated["bvid"])
    if cover_mode:
        assert cover_proof is not None
        _validate_cover_successor_context(
            current_manifest=manifest, current_record=record,
            first_manifest=first_manifest, incoming_authority=validated,
            candidate_id=candidate_id, cover_proof=cover_proof,
            reference=reference, visited=_visited,
        )
        aid = int(cover_proof["aid"])
        cid = int(cover_proof["cid"])
        section_id = int(cover_proof["section_id"])
        if (
            proposal["bvid"] != bvid
            or proposal["candidate_id"] != candidate_id
            or proposal["final_title"] != title
            or proposal["original_title"] != manifest.get("title")
            or any(
                validated.get(key) != cover_proof.get(key)
                for key in ("candidate_id", "recording_date", "bvid", "aid", "cid")
            )
        ):
            raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_IDENTITY_DRIFT")
        published_sha = str(cover_proof["completed"]["sha256"])
        public_verify_sha = published_sha
    else:
        if previous_title is not None:
            aid, cid, section_id = (previous_title[key] for key in ("aid", "cid", "section_id"))
        else:
            uploaded, aid, cid, section_id = _published_identity(
                manifest_path=manifest_path, manifest=manifest, bvid=bvid
            )
        if (
            proposal["bvid"] != bvid or proposal["candidate_id"] != candidate_id
            or proposal["final_title"] != title
            or proposal["original_title"] != (
                previous_title["final_title"] if previous_title else validated["observed_public_title"]
            )
            or aid != validated["aid"] or cid != validated["cid"]
        ):
            raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_IDENTITY_DRIFT")
        if previous_title is not None:
            published_sha = previous_title["published_sha256"]
            public_verify_sha = previous_title["public_verify_sha256"]
        else:
            published_sha = _sha256_file(_uploaded_sidecar_path(manifest_path))
            public_verify_sha = _sha256_file(
                _regular_path(uploaded.get("public_verify"), label="PUBLIC_PROOF")
            )
    binding = {
        "manifest_sha256": manifest_sha,
        "record_sha256": record_sha, "srt_sha256": subtitle_sha,
        "published_sha256": published_sha,
        "public_verify_sha256": public_verify_sha,
        "title_input_sha256": proposal["title_input_sha256"],
        "title_result_sha256": proposal["title_result_sha256"],
        "source_fact_review_sha256": proposal["source_fact_review_sha256"],
        "bvid": bvid, "aid": aid, "cid": cid, "section_id": section_id,
        "original_title": proposal["original_title"], "final_title": title,
        "candidate_id": candidate_id,
        "authorized_by": _required_text(recorded_binding.get("authorized_by"),
                                        "TITLE_REPAIR_AUTHORIZATION_MISSING"),
        "quote": _required_text(recorded_binding.get("quote"),
                               "TITLE_REPAIR_AUTHORIZATION_MISSING"),
    }
    if cover_mode:
        assert cover_proof is not None
        binding.update(_cover_predecessor_binding(cover_proof))
    if previous_title is not None:
        binding["predecessor_title_repair"] = previous_reference
    before, after = receipt.get("before"), receipt.get("after")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_SNAPSHOTS_MISSING")
    _validate_receipt(receipt, binding=binding, before=before)
    if receipt.get("status") != "VERIFIED_TITLE_ONLY":
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_NOT_VERIFIED")
    if not all(
        _surface_identity(snapshot, bvid=bvid, aid=aid, cid=cid, title=expected)
        and snapshot["section"].get("section_id") == section_id
        for snapshot, expected in ((before, proposal["original_title"]), (after, title))
    ) or not same_stable(before, after, _stable_without_title):
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_NON_TITLE_DRIFT")
    if cover_mode:
        assert cover_proof is not None
        if not same_stable(cover_proof["live_snapshot"], before, _stable_without_title):
            raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_COVER_LIVE_DRIFT")
    if previous_title is not None and not same_stable(previous_title["after"], before, _stable_without_title):
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_NON_TITLE_DRIFT")


def _title_predecessor_proof(
    reference: object, *, manifest_path: Path, manifest: Mapping[str, Any],
    visited: set[tuple[str, str]],
) -> dict[str, Any]:
    """Replay an explicit completed title transaction on the same immutable package."""

    if not isinstance(reference, Mapping) or set(reference) != {"receipt", "title_input", "title_result"}:
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_REFERENCE_INVALID")
    paths = {key: _entry_path(value, label=key.upper())[0] for key, value in reference.items()}
    visit = (str(paths["receipt"]), _sha256_file(paths["receipt"]))
    if visit in visited:
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_REFERENCE_CYCLE")
    visited.add(visit)
    receipt = _read_json(paths["receipt"], "RECEIPT")
    binding = _binding_from_receipt(receipt)
    previous_reference = binding.get("predecessor_title_repair")
    previous = None
    if previous_reference is not None:
        previous = _title_predecessor_proof(
            previous_reference, manifest_path=manifest_path, manifest=manifest, visited=visited,
        )
    title_input = _read_json(paths["title_input"], "TITLE_INPUT")
    record, subtitle_path, subtitle_sha, _record_path, record_sha, manifest_sha, _ = _load_original_artifacts(
        manifest_path=manifest_path, manifest=manifest, title_input=title_input,
    )
    proposal = _load_and_validate_proposal(
        manifest_path=manifest_path, manifest=manifest,
        title_input_path=paths["title_input"], title_result_path=paths["title_result"],
        record=record, subtitle_path=subtitle_path, subtitle_sha=subtitle_sha,
        record_sha=record_sha, manifest_sha=manifest_sha,
        predecessor_title=previous["final_title"] if previous else None,
    )
    cover_reference = binding.get("predecessor_cover_completed")
    if cover_reference is not None:
        cover_path, _cover_sha = _entry_path(cover_reference, label="PREDECESSOR_COVER_COMPLETED")
        cover = _cover_predecessor_proof(
            completed_path=cover_path, manifest_path=manifest_path,
            manifest=manifest, expected_bvid=proposal["bvid"],
        )
        publication_binding = _cover_predecessor_binding(cover)
        aid, cid, section_id = (int(cover[key]) for key in ("aid", "cid", "section_id"))
    elif previous is not None:
        aid, cid, section_id = (previous[key] for key in ("aid", "cid", "section_id"))
        publication_binding = {key: previous[key] for key in ("published_sha256", "public_verify_sha256")}
    else:
        uploaded, aid, cid, section_id = _published_identity(
            manifest_path=manifest_path, manifest=manifest, bvid=proposal["bvid"],
        )
        publication_binding = {
            "published_sha256": _sha256_file(_uploaded_sidecar_path(manifest_path)),
            "public_verify_sha256": _sha256_file(_regular_path(uploaded.get("public_verify"), label="PUBLIC_PROOF")),
        }
    expected_binding = {
        key: proposal[key] for key in (
            "manifest_sha256", "record_sha256", "srt_sha256", "title_input_sha256",
            "title_result_sha256", "source_fact_review_sha256", "bvid", "candidate_id",
            "original_title", "final_title",
        )
    }
    expected_binding.update(
        publication_binding, aid=aid, cid=cid, section_id=section_id,
        authorized_by=_required_text(binding.get("authorized_by"), "TITLE_REPAIR_AUTHORIZATION_MISSING"),
        quote=_required_text(binding.get("quote"), "TITLE_REPAIR_AUTHORIZATION_MISSING"),
    )
    if previous is not None:
        expected_binding["predecessor_title_repair"] = previous_reference
    before, after = receipt.get("before"), receipt.get("after")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_SNAPSHOTS_MISSING")
    _validate_receipt(receipt, binding=expected_binding, before=before)
    if receipt.get("status") != "VERIFIED_TITLE_ONLY":
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_NOT_VERIFIED")
    if not all(
        _surface_identity(snapshot, bvid=proposal["bvid"], aid=aid, cid=cid, title=title)
        and snapshot["section"].get("section_id") == section_id
        for snapshot, title in ((before, proposal["original_title"]), (after, proposal["final_title"]))
    ) or not same_stable(before, after, _stable_without_title):
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_NON_TITLE_DRIFT")
    prior_snapshot = previous["after"] if previous else cover["live_snapshot"] if cover_reference is not None else None
    if prior_snapshot is not None and not same_stable(prior_snapshot, before, _stable_without_title):
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_NON_TITLE_DRIFT")
    return {
        **proposal, "after": after, "aid": aid, "cid": cid, "section_id": section_id,
        "published_sha256": expected_binding["published_sha256"],
        "public_verify_sha256": expected_binding["public_verify_sha256"],
    }


def validate_title_repair_successor(
    reference: object, *, authority: Mapping[str, Any], candidate_id: str, title: str
) -> None:
    """Replay a completed title-only transaction before a media/cover successor.

    Ordinary references bind receipt/input/result; FMR references bind only
    ``receipt`` and replay the existing recursive FMR proof.  The immutable
    publication manifest selects the lane.  Live successor gates stay separate.
    """

    _validate_title_repair_successor(
        reference,
        authority=authority,
        candidate_id=candidate_id,
        title=title,
        _visited=set(),
    )


# --- finished-media review lane (FMR) --------------------------------------
#
# An FMR-published BV has no story_contract, selection scorecard or joint QC;
# its publication basis is the hash-bound finished-media review receipt
# (docs/pipeline/85-finished-media-review.md).  The branch is chosen from the
# manifest alone, never from a flag, so a non-FMR manifest cannot opt into it.
FMR_AUTHORITY_SCHEMA = "fmr-title-authority.v1"
FMR_AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "bvid",
        "candidate_id",
        "original_title",
        "final_title",
        "authorized_by",
        "context",
        "authorizations",
    }
)
FMR_AUTHORIZATION_FIELDS = frozenset({"id", "quote", "session", "line", "at", "origin"})
FMR_RECEIPT_SCHEMA = "finished-media-review-receipt.v2"
FMR_RECORD_KIND = "FINISHED_MEDIA_REVIEW"
FMR_AUTHORIZED_BY = "维护者"
FMR_QUOTE_SEPARATOR = " / "


def _is_fmr_manifest(manifest: Mapping[str, Any]) -> bool:
    attestation = manifest.get("package_attestation")
    return isinstance(attestation, Mapping) and "finished_media_review" in attestation


def _fmr_reject_foreign_arguments(args: Any) -> None:
    """FMR repairs take the authority file and at most one explicit predecessor."""

    if getattr(args, "title_input", None) or getattr(args, "title_result", None):
        raise TitleRepairError("TITLE_REPAIR_FMR_SOURCE_FACT_ARGUMENTS_UNSUPPORTED")
    # The FMR lane has no TITLE-INPUT/RESULT artifacts, so its title predecessor
    # is the receipt alone; the cover predecessor stays a non-FMR route.
    if getattr(args, "predecessor_cover_completed", None) or any(
        getattr(args, "predecessor_title_" + key, None) for key in ("input", "result")
    ):
        raise TitleRepairError("TITLE_REPAIR_FMR_PREDECESSOR_UNSUPPORTED")
    if getattr(args, "predecessor_title_receipt", None) and getattr(
        args, "predecessor_repair_completed", None
    ):
        raise TitleRepairError("TITLE_REPAIR_FMR_PREDECESSOR_AMBIGUOUS")


def _load_fmr_artifacts(
    *, manifest_path: Path, manifest: Mapping[str, Any]
) -> tuple[Path, str, str, str, str, str]:
    """Bind the manifest to its record, subtitle and FMR receipt.

    Returns ``(subtitle_path, subtitle_sha, record_sha, manifest_sha,
    receipt_sha, candidate_id)``.  ``load_and_verify`` has already replayed the
    full FMR attestation; this re-reads the receipt so the title receipt binds
    to the same bytes and the lane evidence cannot be swapped between the two.
    """

    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, Mapping):
        raise TitleRepairError("TITLE_REPAIR_MANIFEST_ATTESTATION_MISSING")
    manifest_sha = _sha256_file(manifest_path)
    record_path, record_sha = _entry_path(attestation.get("record"), label="RECORD")
    subtitle_path, subtitle_sha = _entry_path(attestation.get("subtitle"), label="SRT")
    receipt_path, receipt_sha = _entry_path(
        attestation.get("finished_media_review"), label="FMR_RECEIPT"
    )
    if attestation.get("title_cover_qc") != attestation.get("finished_media_review"):
        raise TitleRepairError("TITLE_REPAIR_FMR_RECEIPT_BINDING_INVALID")
    record = _read_json(record_path, "RECORD")
    receipt = _read_json(receipt_path, "FMR_RECEIPT")
    if record.get("record_kind") != FMR_RECORD_KIND:
        raise TitleRepairError("TITLE_REPAIR_FMR_RECORD_KIND_INVALID")
    try:
        candidate_id = publication_identity(record).candidate_id
    except ValueError as exc:
        raise TitleRepairError("TITLE_REPAIR_FMR_CANDIDATE_INVALID") from exc
    _required_text(candidate_id, "TITLE_REPAIR_FMR_CANDIDATE_INVALID")
    bindings = receipt.get("bindings")
    if (
        receipt.get("schema_version") != FMR_RECEIPT_SCHEMA
        or receipt.get("verdict") != "PASS"
        or receipt.get("candidate_id") != candidate_id
        or not isinstance(bindings, Mapping)
        or not isinstance(bindings.get("record"), Mapping)
        or not isinstance(bindings.get("srt"), Mapping)
        or not _sha_matches(bindings["record"].get("sha256"), record_sha)
        or not _sha_matches(bindings["srt"].get("sha256"), subtitle_sha)
    ):
        raise TitleRepairError("TITLE_REPAIR_FMR_RECEIPT_BINDING_INVALID")
    return subtitle_path, subtitle_sha, record_sha, manifest_sha, receipt_sha, candidate_id


def _fmr_reference(path: Path, sha: str) -> dict[str, str]:
    return {"path": str(path), "sha256": sha}


def _fmr_repair_completed_proof(
    *, completed_path: Path, manifest_path: Path, manifest: Mapping[str, Any],
    context: Mapping[str, str], expected_bvid: str,
) -> dict[str, Any]:
    """Replay a same-BV replacement as the proof of the *current* publication.

    A BV whose video was replaced in place has no uploaded sidecar for its
    current manifest; ``same-bv-repair-completed.v1`` is that manifest's
    publication proof.  The normal completed-predecessor replay (plan hash,
    plan envelope, hash-chained journal, terminal VERIFIED row, snapshot
    equality) is reused; this adds the bindings a title edit relies on:
    same manifest bytes, same replacement media, same candidate, one live
    title that is the manifest title.
    """

    from src.autoslice import same_bv_repair as repair

    completed_path = _regular_path(completed_path, label="PREDECESSOR_REPAIR_COMPLETED")
    completed = _read_json(completed_path, "PREDECESSOR_REPAIR_COMPLETED")
    authority = manifest.get("recovery_publication_authority")
    snapshot = completed.get("live_snapshot")
    if not isinstance(authority, Mapping) or not isinstance(snapshot, Mapping):
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_REPAIR_COMPLETED_INVALID")
    try:
        repair._predecessor_completion_attestation(
            completed_path, authority=authority, bvid=expected_bvid, snapshot=snapshot,
        )
    except (repair.RepairError, OSError, TypeError, ValueError, KeyError, AttributeError) as exc:
        raise TitleRepairError(
            "TITLE_REPAIR_PREDECESSOR_REPAIR_COMPLETED_INVALID:" + str(exc)
        ) from exc
    completed_manifest = completed.get("manifest")
    replacement = completed.get("replacement")
    if (
        not isinstance(completed_manifest, Mapping)
        or not _sha_matches(completed_manifest.get("sha256"), context["manifest_sha256"])
        or not isinstance(replacement, Mapping)
        or any(
            not isinstance(replacement.get(kind), Mapping)
            or not isinstance(manifest.get(kind), Mapping)
            or replacement[kind].get("sha256") != manifest[kind].get("sha256")
            for kind in ("video", "cover")
        )
    ):
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_REPAIR_COMPLETED_MANIFEST_DRIFT")
    aid, cid = completed.get("aid"), completed.get("new_cid")
    section = snapshot.get("section")
    section_id = section.get("section_id") if isinstance(section, Mapping) else None
    if (
        completed.get("bvid") != expected_bvid
        or completed.get("candidate_id") != context["candidate_id"]
        or not _positive_int(aid) or not _positive_int(cid) or not _positive_int(section_id)
    ):
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_REPAIR_COMPLETED_IDENTITY_INVALID")
    title = _required_text(manifest.get("title"), "TITLE_REPAIR_MANIFEST_TITLE_MISSING")
    if not _surface_identity(snapshot, bvid=expected_bvid, aid=aid, cid=cid, title=title):
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_REPAIR_COMPLETED_IDENTITY_INVALID")
    sha = _sha256_file(completed_path)
    return {
        "bvid": expected_bvid, "candidate_id": context["candidate_id"], "final_title": title,
        "after": copy.deepcopy(dict(snapshot)), "aid": int(aid), "cid": int(cid),
        "section_id": int(section_id),
        # Same convention as the cover predecessor: both historical field names
        # bind the real completion receipt; no uploaded sidecar is invented.
        "published_sha256": sha, "public_verify_sha256": sha,
        "binding_ref": {"predecessor_repair_completed": _fmr_reference(completed_path, sha)},
    }


def _fmr_title_receipt_proof(
    reference: object, *, manifest_path: Path, manifest: Mapping[str, Any],
    context: Mapping[str, str], visited: set[tuple[str, str]],
) -> dict[str, Any]:
    """Replay a completed FMR ``VERIFIED_TITLE_ONLY`` receipt, recursively.

    The receipt binding is rebuilt from the current immutable package plus the
    receipt's own predecessor (a repair-completed, another FMR title receipt,
    or the original ``uploaded`` + ``public_verify`` pair) and must match
    byte-for-byte.  Authorization fields and the authority file hash are
    taken from the receipt, as the non-FMR replay does.
    """

    invalid = "TITLE_REPAIR_PREDECESSOR_TITLE_RECEIPT_BINDING_INVALID"
    if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_RECEIPT_REFERENCE_INVALID")
    receipt_path, receipt_sha = _entry_path(reference, label="PREDECESSOR_TITLE_RECEIPT")
    visit = (str(receipt_path), receipt_sha)
    if visit in visited:
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_REFERENCE_CYCLE")
    visited.add(visit)
    receipt = _read_json(receipt_path, "PREDECESSOR_TITLE_RECEIPT")
    recorded = _binding_from_receipt(receipt)
    bvid = _required_text(recorded.get("bvid"), invalid)
    prior_receipt_ref = recorded.get("predecessor_title_receipt")
    prior_completed_ref = recorded.get("predecessor_repair_completed")
    if prior_receipt_ref is not None and prior_completed_ref is not None:
        raise TitleRepairError("TITLE_REPAIR_FMR_PREDECESSOR_AMBIGUOUS")
    prior: dict[str, Any] | None = None
    if prior_receipt_ref is not None:
        prior = _fmr_title_receipt_proof(
            prior_receipt_ref, manifest_path=manifest_path, manifest=manifest,
            context=context, visited=visited,
        )
    elif prior_completed_ref is not None:
        prior_path, _prior_sha = _entry_path(
            prior_completed_ref, label="PREDECESSOR_REPAIR_COMPLETED"
        )
        prior = _fmr_repair_completed_proof(
            completed_path=prior_path, manifest_path=manifest_path, manifest=manifest,
            context=context, expected_bvid=bvid,
        )
    if prior is not None:
        aid, cid, section_id = (prior[key] for key in ("aid", "cid", "section_id"))
        published_sha, public_verify_sha = prior["published_sha256"], prior["public_verify_sha256"]
        expected_original = prior["final_title"]
    else:
        uploaded, aid, cid, section_id = _published_identity(
            manifest_path=manifest_path, manifest=manifest, bvid=bvid,
        )
        published_sha = _sha256_file(_uploaded_sidecar_path(manifest_path))
        public_verify_sha = _sha256_file(
            _regular_path(uploaded.get("public_verify"), label="PUBLIC_PROOF")
        )
        expected_original = _required_text(
            manifest.get("title"), "TITLE_REPAIR_MANIFEST_TITLE_MISSING"
        )
    original_title = _required_text(recorded.get("original_title"), invalid)
    final_title = _required_text(recorded.get("final_title"), invalid)
    if original_title != expected_original or final_title == original_title:
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_RECEIPT_TITLE_CHAIN_INVALID")
    expected_binding: dict[str, Any] = {
        "manifest_sha256": context["manifest_sha256"],
        "record_sha256": context["record_sha256"],
        "srt_sha256": context["srt_sha256"],
        "published_sha256": published_sha,
        "public_verify_sha256": public_verify_sha,
        "title_input_sha256": None,
        "title_result_sha256": None,
        "source_fact_review_sha256": None,
        "finished_media_review_sha256": context["finished_media_review_sha256"],
        "title_authority_sha256": _required_text(
            recorded.get("title_authority_sha256"), "TITLE_REPAIR_AUTHORIZATION_MISSING"
        ),
        "bvid": bvid, "aid": aid, "cid": cid, "section_id": section_id,
        "original_title": original_title, "final_title": final_title,
        "candidate_id": context["candidate_id"],
        "authorized_by": _required_text(
            recorded.get("authorized_by"), "TITLE_REPAIR_AUTHORIZATION_MISSING"
        ),
        "quote": _required_text(recorded.get("quote"), "TITLE_REPAIR_AUTHORIZATION_MISSING"),
    }
    if prior is not None:
        expected_binding.update(prior["binding_ref"])
    before, after = receipt.get("before"), receipt.get("after")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_SNAPSHOTS_MISSING")
    _validate_receipt(receipt, binding=expected_binding, before=before)
    if receipt.get("status") != "VERIFIED_TITLE_ONLY":
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_NOT_VERIFIED")
    if not all(
        _surface_identity(snapshot, bvid=bvid, aid=aid, cid=cid, title=title)
        and snapshot["section"].get("section_id") == section_id
        for snapshot, title in ((before, original_title), (after, final_title))
    ) or not same_stable(before, after, _stable_without_title):
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_NON_TITLE_DRIFT")
    if prior is not None and not same_stable(prior["after"], before, _stable_without_title_and_desc):
        raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_NON_TITLE_DRIFT")
    return {
        "bvid": bvid, "candidate_id": context["candidate_id"], "final_title": final_title,
        "after": copy.deepcopy(dict(after)), "aid": aid, "cid": cid, "section_id": section_id,
        "published_sha256": published_sha, "public_verify_sha256": public_verify_sha,
        "binding_ref": {"predecessor_title_receipt": _fmr_reference(receipt_path, receipt_sha)},
    }


def _validate_fmr_title_repair_successor(
    reference: object, *, authority: Mapping[str, Any], candidate_id: str,
    title: str, visited: set[tuple[str, str]],
) -> None:
    """Adapt a receipt-only reference to the existing FMR title-chain replay.

    The lane comes from the immutable publication manifest, not the reference
    shape, a CLI flag, or the successor record.  All consumers (package builder,
    auditor, media successor and reconciliation) share this boundary.
    """

    from src.autoslice.new_bv_repair_authority import (
        replay_first_publication_manifest, validate_new_bv_repair_authority,
    )

    validated = validate_new_bv_repair_authority(
        authority, candidate_id=candidate_id, expected_final_title=title,
    )
    manifest = replay_first_publication_manifest(validated)
    if not _is_fmr_manifest(manifest):
        raise TitleRepairError("TITLE_REPAIR_FMR_REFERENCE_REQUIRES_FMR_MANIFEST")
    # The authority replay above validated this exact source/evidence closure.
    # Use its real manifest locator, never the changed successor package.
    source_path, _ = _entry_path(validated.get("source_publication"), label="SOURCE_PUBLICATION")
    source = _read_json(source_path, "SOURCE_PUBLICATION")
    manifest_path, _ = _entry_path(
        (source.get("evidence") or {}).get("manifest"), label="MANIFEST",
    )
    if _read_json(manifest_path, "MANIFEST") != manifest:
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_ORIGINAL_MANIFEST_DRIFT")
    _, subtitle_sha, record_sha, manifest_sha, receipt_sha, original_candidate = (
        _load_fmr_artifacts(manifest_path=manifest_path, manifest=manifest)
    )
    if original_candidate != candidate_id:
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_IDENTITY_DRIFT")
    proof = _fmr_title_receipt_proof(
        reference, manifest_path=manifest_path, manifest=manifest,
        context={
            "manifest_sha256": manifest_sha, "record_sha256": record_sha,
            "srt_sha256": subtitle_sha, "finished_media_review_sha256": receipt_sha,
            "candidate_id": candidate_id,
        },
        visited=visited,
    )
    if (
        proof["final_title"] != title
        or any(proof[key] != validated[key] for key in ("bvid", "aid", "cid", "candidate_id"))
    ):
        raise TitleRepairError("TITLE_REPAIR_SUCCESSOR_IDENTITY_DRIFT")


def _fmr_title_quote_record(final_title: str, subtitle_path: Path) -> dict[str, Any]:
    """Record the title-quote check; the FMR lane never lets it block (A9)."""

    try:
        result = check_title_quotes(final_title, subtitle_path.read_text(encoding="utf-8"))
        # Round-trip so the record is exactly what the receipt will store.
        result = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except Exception as exc:  # recorded, never raised
        return {"enforced": False, "status": "CHECK_ERROR", "error": type(exc).__name__}
    return {"enforced": False, "result": result}


def _load_fmr_proposal(
    *,
    args: Any,
    manifest: Mapping[str, Any],
    authority_path: Path,
    candidate_id: str,
    subtitle_path: Path,
    receipt_sha: str,
    current_title: str | None = None,
) -> dict[str, Any]:
    """Validate the 维护者-authored title authority and the new title.

    ``current_title`` is the title the predecessor proof says is live; without
    a predecessor it is the manifest title (the first publication).
    """

    authority = _read_json(authority_path, "TITLE_AUTHORITY")
    if set(authority) != FMR_AUTHORITY_FIELDS or authority.get("schema_version") != FMR_AUTHORITY_SCHEMA:
        raise TitleRepairError("TITLE_REPAIR_TITLE_AUTHORITY_SCHEMA_INVALID")
    rows = authority.get("authorizations")
    if not isinstance(rows, list) or not rows:
        raise TitleRepairError("TITLE_REPAIR_AUTHORIZATION_MISSING")
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or set(row) != FMR_AUTHORIZATION_FIELDS
            or not _positive_int(row.get("line"))
            or row.get("origin") != "human"
            or any(
                not isinstance(row.get(key), str) or not row[key].strip()
                for key in ("id", "quote", "session", "at")
            )
        ):
            raise TitleRepairError("TITLE_REPAIR_AUTHORIZATION_MISSING")
    quotes = [str(row["quote"]) for row in rows]
    if (
        authority.get("authorized_by") != FMR_AUTHORIZED_BY
        or str(args.authorized_by or "") != FMR_AUTHORIZED_BY
        or str(args.quote or "") != FMR_QUOTE_SEPARATOR.join(quotes)
        or not isinstance(authority.get("context"), str)
        or not authority["context"].strip()
    ):
        raise TitleRepairError("TITLE_REPAIR_AUTHORIZATION_MISSING")
    original_title = _required_text(
        authority.get("original_title"), "TITLE_REPAIR_ORIGINAL_TITLE_MISSING"
    )
    if (manifest.get("title") if current_title is None else current_title) != original_title:
        raise TitleRepairError("TITLE_REPAIR_MANIFEST_TITLE_DRIFT")
    final_title = _required_text(authority.get("final_title"), "TITLE_REPAIR_FINAL_TITLE_MISSING")
    if final_title == original_title:
        raise TitleRepairError("TITLE_REPAIR_TITLE_UNCHANGED")
    if authority.get("bvid") != args.bvid:
        raise TitleRepairError("TITLE_REPAIR_BVID_BINDING_INVALID")
    if authority.get("candidate_id") != candidate_id:
        raise TitleRepairError("TITLE_REPAIR_CANDIDATE_BINDING_INVALID")
    lane = publish_title_lane(original_title)
    if lane != "talk":
        raise TitleRepairError("TITLE_REPAIR_FMR_LANE_UNSUPPORTED")
    if publish_title_policy_violations(final_title, lane=lane):
        raise TitleRepairError("TITLE_REPAIR_FINAL_TITLE_POLICY_INVALID")
    # The description's partner lines derive from the prefix and a title-only
    # edit leaves the description untouched, so the envelope must not change.
    prefix = accepted_talk_title_prefix(original_title)
    if prefix is None or accepted_talk_title_prefix(final_title) != prefix:
        raise TitleRepairError("TITLE_REPAIR_TITLE_PREFIX_ENVELOPE_DRIFT")
    return {
        "bvid": str(args.bvid),
        "candidate_id": candidate_id,
        "original_title": original_title,
        "final_title": final_title,
        "binding_evidence": {
            "title_input_sha256": None,
            "title_result_sha256": None,
            "source_fact_review_sha256": None,
            "finished_media_review_sha256": receipt_sha,
            "title_authority_sha256": _sha256_file(authority_path),
        },
        "title_quote_check": _fmr_title_quote_record(final_title, subtitle_path),
    }


def _original_publication_replay(manifest_path: Path) -> bool:
    """Whether ``load_and_verify`` must replay an original publication.

    The historical replay needs the manifest's own ``uploaded`` sidecar.  An
    FMR manifest that was published by a same-BV replacement has none: its
    publication proof is the explicit repair-completed chain, replayed (and
    required) later in :func:`run`.  Everything else keeps the replay.
    """

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (
        isinstance(manifest, Mapping)
        and _is_fmr_manifest(manifest)
        and not _uploaded_sidecar_path(manifest_path).is_file()
    )


def run(
    args: Any,
    *,
    load_and_verify: Callable[..., tuple[dict[str, Any] | None, list[str]]],
    adapter_factory: Callable[[Path], Any],
    upload_lock: Callable[[Path], Any],
    default_lock: Path,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[int, dict[str, Any]]:
    """Plan, execute, or reconcile one title-only same-BV edit."""

    manifest_path = Path(args.manifest).resolve()
    title_input_path = Path(args.title_input).resolve() if getattr(args, "title_input", None) else None
    title_result_path = Path(args.title_result).resolve() if getattr(args, "title_result", None) else None
    title_authority_arg = getattr(args, "title_authority", None)
    receipt_path = Path(args.receipt).resolve()
    lock_path = Path(args.lock).resolve() if args.lock else Path(default_lock)
    with upload_lock(lock_path):
        manifest, problems = load_and_verify(
            manifest_path, frozen_plan_resume=True,
            published_title_only_replay=_original_publication_replay(manifest_path),
        )
        if problems or manifest is None:
            raise TitleRepairError("TITLE_REPAIR_MANIFEST_INVALID:" + ";".join(problems))
        fmr_lane = _is_fmr_manifest(manifest)
        predecessor_reference = None
        predecessor_title = None
        fmr_predecessor = None
        if fmr_lane:
            _fmr_reject_foreign_arguments(args)
            if not title_authority_arg:
                raise TitleRepairError("TITLE_REPAIR_AUTHORIZATION_MISSING")
            (
                subtitle_path, subtitle_sha, record_sha, manifest_sha,
                fmr_receipt_sha, fmr_candidate_id,
            ) = _load_fmr_artifacts(manifest_path=manifest_path, manifest=manifest)
            fmr_context = {
                "manifest_sha256": manifest_sha, "record_sha256": record_sha,
                "srt_sha256": subtitle_sha, "finished_media_review_sha256": fmr_receipt_sha,
                "candidate_id": fmr_candidate_id,
            }
            repair_completed_arg = getattr(args, "predecessor_repair_completed", None)
            title_receipt_arg = getattr(args, "predecessor_title_receipt", None)
            if repair_completed_arg:
                fmr_predecessor = _fmr_repair_completed_proof(
                    completed_path=Path(repair_completed_arg), manifest_path=manifest_path,
                    manifest=manifest, context=fmr_context, expected_bvid=str(args.bvid),
                )
            elif title_receipt_arg:
                title_receipt = _regular_path(title_receipt_arg, label="PREDECESSOR_TITLE_RECEIPT")
                if title_receipt == receipt_path:
                    raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_IDENTITY_DRIFT")
                fmr_predecessor = _fmr_title_receipt_proof(
                    _fmr_reference(title_receipt, _sha256_file(title_receipt)),
                    manifest_path=manifest_path, manifest=manifest, context=fmr_context,
                    visited={(str(receipt_path), "CURRENT")},
                )
            if fmr_predecessor is not None and fmr_predecessor["bvid"] != args.bvid:
                raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_IDENTITY_DRIFT")
            proposal = _load_fmr_proposal(
                args=args,
                manifest=manifest,
                authority_path=_regular_path(title_authority_arg, label="TITLE_AUTHORITY"),
                candidate_id=fmr_candidate_id,
                subtitle_path=subtitle_path,
                receipt_sha=fmr_receipt_sha,
                current_title=fmr_predecessor["final_title"] if fmr_predecessor else None,
            )
            # Same shape as the non-FMR title predecessor, so the identity,
            # published-sha and live-before handling below is shared.
            predecessor_title = fmr_predecessor
        else:
            if getattr(args, "predecessor_repair_completed", None):
                raise TitleRepairError(
                    "TITLE_REPAIR_PREDECESSOR_REPAIR_COMPLETED_REQUIRES_FMR_MANIFEST"
                )
            if title_authority_arg:
                raise TitleRepairError("TITLE_REPAIR_TITLE_AUTHORITY_REQUIRES_FMR_MANIFEST")
            if title_input_path is None or title_result_path is None:
                raise TitleRepairError("TITLE_REPAIR_TITLE_INPUT_REQUIRED")
            title_input = _read_json(title_input_path, "TITLE_INPUT")
            predecessor_paths = {
                key: getattr(args, "predecessor_title_" + key, None)
                for key in ("receipt", "input", "result")
            }
            if any(predecessor_paths.values()):
                if not all(predecessor_paths.values()) or getattr(args, "predecessor_cover_completed", None):
                    raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_ARGUMENTS_INVALID")
                predecessor_reference = {}
                for key, value in predecessor_paths.items():
                    path = _regular_path(value, label="PREDECESSOR_TITLE_" + key.upper())
                    predecessor_reference["title_" + key if key != "receipt" else key] = {
                        "path": str(path), "sha256": _sha256_file(path),
                    }
                predecessor_title = _title_predecessor_proof(
                    predecessor_reference, manifest_path=manifest_path, manifest=manifest,
                    visited={(str(receipt_path), "CURRENT")},
                )
                if predecessor_title["bvid"] != args.bvid or str(receipt_path) == predecessor_reference["receipt"]["path"]:
                    raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_IDENTITY_DRIFT")
            record, subtitle_path, subtitle_sha, record_path, record_sha, manifest_sha, _attestation = (
                _load_original_artifacts(
                    manifest_path=manifest_path,
                    manifest=manifest,
                    title_input=title_input,
                )
            )
            proposal = _load_and_validate_proposal(
                manifest_path=manifest_path,
                manifest=manifest,
                title_input_path=title_input_path,
                title_result_path=title_result_path,
                record=record,
                subtitle_path=subtitle_path,
                subtitle_sha=subtitle_sha,
                record_sha=record_sha,
                manifest_sha=manifest_sha,
                predecessor_title=predecessor_title["final_title"] if predecessor_title else None,
            )
        if proposal["bvid"] != args.bvid:
            raise TitleRepairError("TITLE_REPAIR_BVID_BINDING_INVALID")
        if not str(args.authorized_by or "").strip() or not str(args.quote or "").strip():
            raise TitleRepairError("TITLE_REPAIR_AUTHORIZATION_MISSING")
        predecessor_cover_path_value = getattr(
            args, "predecessor_cover_completed", None
        )
        predecessor_cover = None
        uploaded = None
        if predecessor_cover_path_value:
            predecessor_cover_path = _regular_path(
                predecessor_cover_path_value,
                label="PREDECESSOR_COVER_COMPLETED",
            )
            predecessor_cover = _cover_predecessor_proof(
                completed_path=predecessor_cover_path,
                manifest_path=manifest_path,
                manifest=manifest,
                expected_bvid=str(args.bvid),
            )
            aid = int(predecessor_cover["aid"])
            cid = int(predecessor_cover["cid"])
            section_id = int(predecessor_cover["section_id"])
        elif predecessor_title is not None:
            aid, cid, section_id = (predecessor_title[key] for key in ("aid", "cid", "section_id"))
        else:
            uploaded, aid, cid, section_id = _published_identity(
                manifest_path=manifest_path, manifest=manifest, bvid=args.bvid
            )
        adapter = adapter_factory(Path(args.cookie_json))
        before = adapter.observe(args.bvid, section_id)
        if not _surface_identity(
            before,
            bvid=args.bvid,
            aid=aid,
            cid=cid,
            title=None,
        ):
            raise TitleRepairError("TITLE_REPAIR_LIVE_ORIGINAL_IDENTITY_INVALID")
        if predecessor_cover is not None:
            predecessor_binding = _cover_predecessor_binding(predecessor_cover)
            published_sha = str(predecessor_binding["published_sha256"])
            public_verify_sha = str(predecessor_binding["public_verify_sha256"])
        elif predecessor_title is not None:
            published_sha = predecessor_title["published_sha256"]
            public_verify_sha = predecessor_title["public_verify_sha256"]
        else:
            assert uploaded is not None
            published_sha = _sha256_file(_uploaded_sidecar_path(manifest_path))
            public_verify_sha = _sha256_file(
                _regular_path(uploaded.get("public_verify"), label="PUBLIC_PROOF")
            )
        lane_binding = proposal.get("binding_evidence") or {
            "title_input_sha256": proposal["title_input_sha256"],
            "title_result_sha256": proposal["title_result_sha256"],
            "source_fact_review_sha256": proposal["source_fact_review_sha256"],
        }
        binding = {
            "manifest_sha256": manifest_sha,
            "record_sha256": record_sha,
            "srt_sha256": subtitle_sha,
            "published_sha256": published_sha,
            "public_verify_sha256": public_verify_sha,
            **lane_binding,
            "bvid": args.bvid,
            "aid": aid,
            "cid": cid,
            "section_id": section_id,
            "original_title": proposal["original_title"],
            "final_title": proposal["final_title"],
            "candidate_id": proposal["candidate_id"],
            "authorized_by": str(args.authorized_by),
            "quote": str(args.quote),
        }
        if predecessor_cover is not None:
            binding.update(_cover_predecessor_binding(predecessor_cover))
            if not same_stable(predecessor_cover["live_snapshot"], before, _stable_without_title):
                raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_COVER_LIVE_DRIFT")
        if predecessor_title is not None:
            if (aid, cid, section_id) != tuple(predecessor_title[key] for key in ("aid", "cid", "section_id")):
                raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_IDENTITY_DRIFT")
            if fmr_predecessor is not None:
                # Live must still be the predecessor's terminal publication; a
                # hand-edited description is the one tolerated difference and
                # is carried over verbatim by the title-only payload.
                if not same_stable(predecessor_title["after"], before, _stable_without_title_and_desc):
                    raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_NON_TITLE_DRIFT")
                binding.update(fmr_predecessor["binding_ref"])
            else:
                if not same_stable(predecessor_title["after"], before, _stable_without_title):
                    raise TitleRepairError("TITLE_REPAIR_PREDECESSOR_TITLE_NON_TITLE_DRIFT")
                binding["predecessor_title_repair"] = predecessor_reference
        receipt_exists = receipt_path.exists()
        if receipt_exists:
            receipt = _validate_receipt(
                _read_json(receipt_path, "RECEIPT"), binding=binding, before=before
            )
        else:
            if not _surface_identity(
                before,
                bvid=args.bvid,
                aid=aid,
                cid=cid,
                title=proposal["original_title"],
            ):
                raise TitleRepairError("TITLE_REPAIR_LIVE_ORIGINAL_IDENTITY_INVALID")
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "binding": binding,
                "before": before,
                "before_sha256": _snapshot_sha(before),
                "status": "PLANNED",
                "edit_calls": 0,
                "section_sync_calls": 0,
            }
            if fmr_lane:
                # Audit record only (A9 parity); deliberately outside the binding.
                receipt["title_quote_check"] = proposal["title_quote_check"]
            write_json_sidecar(receipt_path, receipt)
        if not same_stable(receipt["before"], before, _stable_without_title):
            code, receipt = _blocked(receipt, reason="live non-title drift before repair", after=before)
            write_json_sidecar(receipt_path, receipt)
            return code, receipt
        if receipt["status"] in TERMINAL_STATES:
            return (0 if receipt["status"] == "VERIFIED_TITLE_ONLY" else 5), receipt
        if receipt["status"] == "PLANNED" and not _surface_identity(
            before,
            bvid=args.bvid,
            aid=aid,
            cid=cid,
            title=proposal["original_title"],
        ):
            code, receipt = _blocked(
                receipt,
                reason="live title changed before the planned edit",
                after=before,
            )
            write_json_sidecar(receipt_path, receipt)
            return code, receipt
        if receipt["status"] == "PLANNED" and args.execute:
            raw_current = adapter.session.archive_view(args.bvid)
            current_creator = normalise_creator_snapshot(
                bvid=args.bvid, creator_data=raw_current
            )
            if not snapshots_equivalent(
                {"creator": current_creator},
                {"creator": before["creator"]},
            ):
                code, receipt = _blocked(
                    receipt,
                    reason="Creator changed before title edit",
                    after=adapter.observe(args.bvid, section_id),
                )
                write_json_sidecar(receipt_path, receipt)
                return code, receipt
            payload = _title_only_payload(
                session=adapter.session,
                raw_view=raw_current,
                final_title=proposal["final_title"],
                bvid=args.bvid,
            )
            receipt.update(status="EDIT_INTENT", edit_calls=1, edit_payload_sha256=_sha256_json(payload))
            write_json_sidecar(receipt_path, receipt)
            try:
                response = adapter.session.edit_archive(payload)
                receipt.update(status="EDIT_ACCEPTED", edit_code=response.get("code"))
            except Exception as exc:
                receipt.update(status="EDIT_OUTCOME_UNKNOWN", error=type(exc).__name__)
                write_json_sidecar(receipt_path, receipt)
                return 6, receipt
            write_json_sidecar(receipt_path, receipt)
        if receipt["status"] == "PLANNED":
            # A planning invocation must never fall through into a polling
            # timeout that rewrites a valid plan as PUBLIC_PENDING.
            return 0, receipt
        deadline = clock() + max(0.0, float(args.wait))
        while True:
            current = adapter.observe(args.bvid, section_id)
            if not all(
                isinstance(current.get(name), Mapping)
                and current[name].get("available") is True
                for name in ("creator", "public", "section")
            ):
                if clock() >= deadline:
                    receipt.update(status="PUBLIC_PENDING", after=current)
                    write_json_sidecar(receipt_path, receipt)
                    return 6, receipt
                sleeper(float(args.poll))
                continue
            if not same_stable(receipt["before"], current, _stable_without_title):
                code, receipt = _blocked(receipt, reason="live non-title drift", after=current)
                write_json_sidecar(receipt_path, receipt)
                return code, receipt
            creator_title = (current["creator"].get("metadata") or {}).get("title")
            public_title = (current["public"].get("metadata") or {}).get("title")
            matches = current["section"].get("matches") or []
            section_title = matches[0].get("title") if len(matches) == 1 else None
            if creator_title == proposal["final_title"] and public_title == proposal["final_title"]:
                if section_title == proposal["final_title"] and part_name_is_title(
                    current["creator"], proposal["final_title"]
                ) and _surface_identity(
                    current,
                    bvid=args.bvid,
                    aid=aid,
                    cid=cid,
                    title=proposal["final_title"],
                ):
                    receipt.update(status="VERIFIED_TITLE_ONLY", after=current)
                    write_json_sidecar(receipt_path, receipt)
                    return 0, receipt
                if section_title == proposal["original_title"]:
                    if not args.execute or receipt["section_sync_calls"] == 1:
                        if clock() >= deadline:
                            receipt.update(status="PUBLIC_PENDING", after=current)
                            write_json_sidecar(receipt_path, receipt)
                            return 6, receipt
                        sleeper(float(args.poll))
                        continue
                    receipt.update(
                        status="SECTION_TITLE_SYNC_INTENT",
                        section_sync_calls=1,
                    )
                    write_json_sidecar(receipt_path, receipt)
                    try:
                        response = adapter.sync_section_title(
                            args.bvid,
                            section_id,
                            expected_current_title=proposal["original_title"],
                            target_title=proposal["final_title"],
                        )
                        receipt.update(status="SECTION_TITLE_SYNC_ACCEPTED", section_sync_code=response.get("code"))
                    except Exception as exc:
                        receipt.update(
                            status="SECTION_TITLE_SYNC_OUTCOME_UNKNOWN",
                            section_sync_error=type(exc).__name__,
                        )
                        write_json_sidecar(receipt_path, receipt)
                        return 6, receipt
                    write_json_sidecar(receipt_path, receipt)
                    continue
            if clock() >= deadline:
                receipt.update(status="PUBLIC_PENDING", after=current)
                write_json_sidecar(receipt_path, receipt)
                return 6, receipt
            sleeper(float(args.poll))
