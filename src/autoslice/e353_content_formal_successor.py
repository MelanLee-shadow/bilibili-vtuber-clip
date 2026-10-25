"""Candidate-bound formal successor for E353 corrected content bytes.

The historical E223 CPA/AGY authority remains immutable evidence.  This module
advances only the four independently evidenced text corrections and binds the
already accepted final media/audio receipts.  It performs no provider, ASR,
render, production-state, or upload action.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from src.autoslice.boundary_semantic_review import (
    cue_grid_sha256,
    semantic_review_sha256,
)
from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues
from src.autoslice.review_package_boundary_validators import semantic_boundary_review_is_valid
from src.autoslice.review_evidence_store import review_from_bytes

CANDIDATE_ID = "auto_134025_1011_1089"
FINAL_REVIEW_SCHEMA_VERSION = "e353-content-final-review-successor.v1"
CHAT_SUCCESSOR_SCHEMA_VERSION = "e353-content-chat-authority-successor.v1"
PARENT_CHAT_FILE_SHA256 = "sha256:8d8f6a128e2eefc5f5f84ccfc6dcabc9ff71179edd5377366679c8b03ed69728"
PARENT_SRT_SHA256 = "sha256:a898676093deaccd99c6d358a6c3c178643225842b00aaa9b54d68e8cb9ef24e"
CURRENT_SRT_SHA256 = "sha256:5c56c894915d10a435ce33221ed3e9eaedd85cfde86f299ced2ee7eb2d46f7b5"
PADDED_SRT_SHA256 = "sha256:5a4735b022f2357aa8abf0764bb2872da9498861de101df249c0997908124557"
FIDELITY_CLOSURE_SHA256 = "sha256:5ab20719cac1e234c0a3a4e835392ff1f6a164d63b30d554fac04bab320fcfc5"
FOREIGN_CONSUMER_SHA256 = "sha256:8ae82b1c3f635f1f09059c7cbb6be32d85f7aee110647708de7afc45ce0e0194"
FINAL_AV_ACCEPTANCE_SHA256 = "sha256:c08f10d8496bd68a8a2a894284f3fe8a3c62a2c114610533f304337488eb2e20"
FINAL_MEDIA_RESULT_SHA256 = "sha256:eea9178ffbebd8fc8b64bfe54c0d987d85bc86ff0a39c271451e7dd27d0178c4"
AUDIO_VERIFIED_RECORD_SHA256 = "sha256:3092e8098eadbec3ee3f27402a96da056ebdeac237f2904a9326e0e497b9a9bb"
FINAL_MEDIA_SHA256 = "sha256:21c9cb4702ac11f3e40b2f33010822aebe2fad36571adaaa40fa2af0e898bcb6"
FINAL_ASS_SHA256 = "sha256:0e499c9bb16cd76c4a0319e6370af2e7b9006b625d7c4a3a6b213482841af1b2"
FINAL_AUDIO_RECEIPT_SHA256 = "sha256:c7460530a6192b22e6c608bdcc8856cfa8370d76ae3b1d126f74b41ef98fa485"
BOUNDARY_EVIDENCE_SHA256 = "sha256:55f267c198bcd5ed3b07aa6d8d5898ba3f6c291ff7a4dd5ae0f715a69a3965e9"
PARENT_REVIEW_SHA256 = "sha256:3edf9c5c04feab75dac3543dc933b5030ba09ffc3e42a3215efc54921f596c99"
BOUNDARY_EVIDENCE_PATH = Path(__file__).resolve().parents[2] / "assets/lidousha/e353-genuine-boundary-reviews.v1.json"
STORY_DURATION_MS = 78_290
STORY_OFFSET_MS = 10_000
EXPECTED_MUTATIONS = {
    1: ("这首歌的名字叫做东京相地优惠", "这首歌的名字叫做东京香蒂幽会"),
    9: ("香 DU", "香蒂幽"),
    30: ("谢谢刚刚乐比TA的三个人气票和粉丝团灯牌", "谢谢刚刚乐比他的三个人气票和粉丝团灯牌"),
    31: ("谢谢你，林哥多", "谢谢你，ありがとう"),
}


class E353ContentFormalSuccessorError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _bytes_sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _seal(document: Mapping[str, object]) -> dict[str, object]:
    out = deepcopy(dict(document)); out.pop("receipt_sha256", None)
    out["receipt_sha256"] = _canonical_sha256(out)
    return out


def _is_sealed(document: Mapping[str, object]) -> bool:
    body = deepcopy(dict(document)); declared = body.pop("receipt_sha256", None)
    return isinstance(declared, str) and declared == _canonical_sha256(body)


def _read_bound(path: Path, expected: str, reason: str) -> bytes:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise E353ContentFormalSuccessorError(reason)
    raw = path.read_bytes()
    if _bytes_sha256(raw) != expected:
        raise E353ContentFormalSuccessorError(reason)
    return raw


def _json(
    raw: bytes,
    reason: str,
    *,
    review: bool = False,
    directory: Path | None = None,
) -> dict[str, object]:
    try:
        value = (
            review_from_bytes(raw, directory=directory or Path.cwd())
            if review
            else json.loads(raw)
        )
    except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise E353ContentFormalSuccessorError(reason) from exc
    if not isinstance(value, dict):
        raise E353ContentFormalSuccessorError(reason)
    return value


def _cues(raw: bytes, reason: str) -> list[SrtCue]:
    try:
        cues = [cue for cue in parse_srt_cues(raw.decode("utf-8")) if cue.text.strip()]
    except (UnicodeDecodeError, ValueError) as exc:
        raise E353ContentFormalSuccessorError(reason) from exc
    if not cues:
        raise E353ContentFormalSuccessorError(reason)
    return cues


def typed_mutations(parent_raw: bytes, current_raw: bytes) -> list[dict[str, object]]:
    parent = _cues(parent_raw, "E353_PARENT_SRT_INVALID")
    current = _cues(current_raw, "E353_CURRENT_SRT_INVALID")
    if len(parent) != 33 or len(current) != 33:
        raise E353ContentFormalSuccessorError("E353_CUE_COUNT_DRIFT")
    mutations: list[dict[str, object]] = []
    for position, (before, after) in enumerate(zip(parent, current, strict=True), start=1):
        if (before.start_ms, before.end_ms) != (after.start_ms, after.end_ms):
            raise E353ContentFormalSuccessorError("E353_TIMING_DRIFT")
        expected = EXPECTED_MUTATIONS.get(position)
        if expected is None:
            if before.text != after.text:
                raise E353ContentFormalSuccessorError("E353_UNAUTHORIZED_TEXT_DRIFT")
            continue
        if (before.text, after.text) != expected:
            raise E353ContentFormalSuccessorError("E353_MUTATION_DRIFT")
        row: dict[str, object] = {
            "story_cue": position,
            "start_ms": before.start_ms,
            "end_ms": before.end_ms,
            "before": before.text,
            "after": after.text,
            "timing_unchanged": True,
        }
        if position == 30:
            row["sender_account_identity_verified"] = False
            row["identity_boundary"] = "乐比他 is source-chain transcription only; no account orthography claim"
        if position == 31:
            row["existing_cpa_surface_restored"] = True
        mutations.append(row)
    if [row["story_cue"] for row in mutations] != [1, 9, 30, 31]:
        raise E353ContentFormalSuccessorError("E353_MUTATION_SET_DRIFT")
    return mutations


def _validate_fidelity(raw: bytes) -> dict[str, object]:
    doc = _json(raw, "E353_FIDELITY_INVALID")
    outputs = doc.get("outputs")
    truth = doc.get("truth_boundary")
    calls = doc.get("new_calls")
    if not (
        doc.get("schema_version") == "e353-text-fidelity-closure.v1"
        and doc.get("status") == "PASS_TEXT_FIDELITY_CLOSED"
        and doc.get("candidate_id") == CANDIDATE_ID
        and isinstance(outputs, Mapping)
        and isinstance(outputs.get("story_srt"), Mapping)
        and outputs["story_srt"].get("sha256") == CURRENT_SRT_SHA256.removeprefix("sha256:")
        and isinstance(outputs.get("padded_srt"), Mapping)
        and outputs["padded_srt"].get("sha256") == PADDED_SRT_SHA256.removeprefix("sha256:")
        and doc.get("mutated_story_cues") == [1, 9, 30, 31]
        and doc.get("all_other_text_unchanged") is True
        and doc.get("timings_unchanged") is True
        and doc.get("remaining_text_fidelity_blockers") == []
        and isinstance(truth, Mapping)
        and truth.get("cue32_sender_account_identity_verified") is False
        and truth.get("human_full_listen_claimed") is False
        and truth.get("new_model_output_used_as_self_proving_truth") is False
        and isinstance(calls, Mapping)
        and all(calls.get(key) == 0 for key in ("agy", "asr", "cpa", "provider"))
    ):
        raise E353ContentFormalSuccessorError("E353_FIDELITY_INVALID")
    return doc


def _project_source_cues(padded_raw: bytes) -> list[SrtCue]:
    padded = _cues(padded_raw, "E353_PADDED_SRT_INVALID")
    selected: list[SrtCue] = []
    for cue in padded:
        if cue.start_ms < STORY_OFFSET_MS:
            continue
        selected.append(SrtCue(index=str(len(selected) + 1), start_ms=cue.start_ms - STORY_OFFSET_MS, end_ms=cue.end_ms - STORY_OFFSET_MS, text=cue.text))
    if len(selected) < 35 or selected[32].end_ms != STORY_DURATION_MS:
        raise E353ContentFormalSuccessorError("E353_SOURCE_GRID_DRIFT")
    if selected[33].text != "我要唱这个" or "瞬き" not in selected[34].text:
        raise E353ContentFormalSuccessorError("E353_NEXT_TOPIC_WITNESS_DRIFT")
    return selected


def _boundary_evidence(raw: bytes) -> dict[str, object]:
    if _bytes_sha256(raw) != BOUNDARY_EVIDENCE_SHA256:
        raise E353ContentFormalSuccessorError("E353_BOUNDARY_EVIDENCE_DRIFT")
    doc = _json(raw, "E353_BOUNDARY_EVIDENCE_INVALID")
    if not all(semantic_boundary_review_is_valid(doc.get(key), expected_scope=scope)
               for key, scope in (("source", "source_full_window"), ("final", "final_delivery"))):
        raise E353ContentFormalSuccessorError("E353_BOUNDARY_INVALID")
    return doc


def build_source_boundary_review(padded_raw: bytes) -> dict[str, object]:
    doc = _boundary_evidence(_read_bound(BOUNDARY_EVIDENCE_PATH, BOUNDARY_EVIDENCE_SHA256, "E353_BOUNDARY_EVIDENCE_DRIFT"))
    source = doc["source"]
    if source["cue_grid_sha256"] != cue_grid_sha256(_project_source_cues(padded_raw)):
        raise E353ContentFormalSuccessorError("E353_SOURCE_GRID_DRIFT")
    return deepcopy(source)


def build_final_boundary_review(current_raw: bytes, source_review: Mapping[str, object]) -> dict[str, object]:
    doc = _boundary_evidence(_read_bound(BOUNDARY_EVIDENCE_PATH, BOUNDARY_EVIDENCE_SHA256, "E353_BOUNDARY_EVIDENCE_DRIFT"))
    final = doc["final"]
    if (final["cue_grid_sha256"] != cue_grid_sha256(_cues(current_raw, "E353_CURRENT_SRT_INVALID"))
            or final["source_separation_witness"]["source_review_sha256"] != semantic_review_sha256(source_review)):
        raise E353ContentFormalSuccessorError("E353_BOUNDARY_WITNESS_INVALID")
    return deepcopy(final)


def _build_from_bound_inputs(*, parent_review: Mapping[str, object], parent_raw: bytes, current_raw: bytes, padded_raw: bytes, fidelity_raw: bytes, foreign_raw: bytes, final_av_raw: bytes, media_result_raw: bytes, audio_record_raw: bytes, boundary_raw: bytes) -> dict[str, object]:
    if _bytes_sha256(parent_raw) != PARENT_SRT_SHA256 or _bytes_sha256(current_raw) != CURRENT_SRT_SHA256 or _bytes_sha256(padded_raw) != PADDED_SRT_SHA256:
        raise E353ContentFormalSuccessorError("E353_SRT_BINDING_DRIFT")
    if _canonical_sha256(parent_review) != PARENT_REVIEW_SHA256:
        raise E353ContentFormalSuccessorError("E353_PARENT_REVIEW_DRIFT")
    evidence = {
        "fidelity": (fidelity_raw, FIDELITY_CLOSURE_SHA256),
        "foreign": (foreign_raw, FOREIGN_CONSUMER_SHA256),
        "final_av": (final_av_raw, FINAL_AV_ACCEPTANCE_SHA256),
        "media_result": (media_result_raw, FINAL_MEDIA_RESULT_SHA256),
        "audio_record": (audio_record_raw, AUDIO_VERIFIED_RECORD_SHA256),
        "boundary": (boundary_raw, BOUNDARY_EVIDENCE_SHA256),
    }
    if any(_bytes_sha256(raw) != expected for raw, expected in evidence.values()):
        raise E353ContentFormalSuccessorError("E353_EVIDENCE_BINDING_DRIFT")
    fidelity = _validate_fidelity(fidelity_raw)
    foreign = _json(foreign_raw, "E353_FOREIGN_CONSUMER_INVALID")
    final_av = _json(final_av_raw, "E353_FINAL_AV_INVALID")
    media_result = _json(media_result_raw, "E353_FINAL_MEDIA_RESULT_INVALID")
    audio_record = _json(audio_record_raw, "E353_AUDIO_RECORD_INVALID")
    audio_gate = audio_record.get("subtitle_audio_correspondence")
    if not (
        isinstance(foreign.get("audit"), Mapping)
        and foreign["audit"].get("status") == "OUTSIDE_IMMUTABLE_STORY_SCOPE"
        and foreign.get("new_provider_calls") == 0
        and foreign.get("new_asr_calls") == 0
        and foreign.get("text_mutations") == 0
        and final_av.get("status") == "PASS_PRIVATE_FINAL_AV"
        and media_result.get("status") == "PASS_PRIVATE_WITH_INTRO_AND_FINAL_AUDIO_TIMING"
        and audio_record.get("status") == "PRIVATE_MEDIA_ONLY"
        and isinstance(audio_gate, Mapping)
        and audio_gate.get("status") == "PASS"
        and audio_gate.get("timing_status") == "PASS"
        and audio_gate.get("text_correctness_status") == "UNASSESSED"
        and audio_gate.get("actual_media_sha256") == FINAL_MEDIA_SHA256
        and audio_gate.get("final_srt_sha256") == CURRENT_SRT_SHA256
        and isinstance(audio_gate.get("receipt"), Mapping)
        and audio_gate.get("receipt_sha256") == FINAL_AUDIO_RECEIPT_SHA256
    ):
        raise E353ContentFormalSuccessorError("E353_MEDIA_EVIDENCE_INVALID")
    mutations = typed_mutations(parent_raw, current_raw)
    boundary = _boundary_evidence(boundary_raw)
    source_boundary, final_boundary = boundary["source"], boundary["final"]
    if (source_boundary["cue_grid_sha256"] != cue_grid_sha256(_project_source_cues(padded_raw))
            or final_boundary["cue_grid_sha256"] != cue_grid_sha256(_cues(current_raw, "E353_CURRENT_SRT_INVALID"))
            or final_boundary["source_separation_witness"]["source_review_sha256"] != semantic_review_sha256(source_boundary)):
        raise E353ContentFormalSuccessorError("E353_BOUNDARY_WITNESS_INVALID")
    document: dict[str, object] = {
        "schema_version": FINAL_REVIEW_SCHEMA_VERSION,
        "status": "CLEAN",
        "release_gate": "PASS",
        "candidate_id": CANDIDATE_ID,
        "reviewed_srt_sha256": CURRENT_SRT_SHA256,
        "parent_reviewed_srt_sha256": PARENT_SRT_SHA256,
        "parent_review": deepcopy(dict(parent_review)),
        "parent_review_sha256": _canonical_sha256(parent_review),
        "parent_srt_raw_b64": base64.b64encode(parent_raw).decode("ascii"),
        "current_srt_raw_b64": base64.b64encode(current_raw).decode("ascii"),
        "padded_srt_raw_b64": base64.b64encode(padded_raw).decode("ascii"),
        "evidence_raw_b64": {key: base64.b64encode(raw).decode("ascii") for key, (raw, _) in evidence.items()},
        "typed_mutations": mutations,
        "typed_mutation_count": 4,
        "all_other_text_unchanged": True,
        "all_timings_unchanged": True,
        "cue30_sender_account_identity_verified": False,
        "human_full_listen_claimed": False,
        "text_correctness_from_audio_gate": "UNASSESSED",
        "fidelity_closure": deepcopy(fidelity),
        "fidelity_closure_file_sha256": FIDELITY_CLOSURE_SHA256,
        "foreign_consumer_file_sha256": FOREIGN_CONSUMER_SHA256,
        "final_av_acceptance_file_sha256": FINAL_AV_ACCEPTANCE_SHA256,
        "final_media_result_file_sha256": FINAL_MEDIA_RESULT_SHA256,
        "audio_verified_record_file_sha256": AUDIO_VERIFIED_RECORD_SHA256,
        "final_media_sha256": FINAL_MEDIA_SHA256,
        "final_ass_sha256": FINAL_ASS_SHA256,
        "source_boundary_semantic_review": source_boundary,
        "boundary_semantic_review": final_boundary,
        "findings": [],
        "resolved_findings": [],
        "validated_finding_count": 0,
        "unresolved_findings_disclosed": [],
        "provider_calls": 0,
        "paid_calls": 0,
        "new_audio_calls": 0,
        "asr_calls": 0,
        "render_calls": 0,
        "production_state_mutations": 0,
        "upload_calls": 0,
        "timing_mutated": False,
        "text_mutated": True,
        "parent_exact_receipts_preserved": True,
        "authority": "E353_E223_PARENT_PLUS_FIDELITY_CLOSURE_AND_ROOT_FINAL_MEDIA_ACCEPTANCE",
    }
    return _seal(document)


def build_final_review_successor(*, parent_chat_authority: Path, parent_srt: Path, current_srt: Path, padded_srt: Path, fidelity_closure: Path, foreign_consumer: Path, final_av_acceptance: Path, final_media_result: Path, audio_verified_record: Path) -> dict[str, object]:
    chat_raw = _read_bound(parent_chat_authority, PARENT_CHAT_FILE_SHA256, "E353_PARENT_CHAT_DRIFT")
    parent_chat = _json(
        chat_raw,
        "E353_PARENT_CHAT_INVALID",
        review=True,
        directory=Path(parent_chat_authority).parent,
    )
    parent_review = parent_chat.get("final_review_audit")
    if not isinstance(parent_review, Mapping):
        raise E353ContentFormalSuccessorError("E353_PARENT_REVIEW_INVALID")
    return _build_from_bound_inputs(
        parent_review=parent_review,
        parent_raw=_read_bound(parent_srt, PARENT_SRT_SHA256, "E353_PARENT_SRT_DRIFT"),
        current_raw=_read_bound(current_srt, CURRENT_SRT_SHA256, "E353_CURRENT_SRT_DRIFT"),
        padded_raw=_read_bound(padded_srt, PADDED_SRT_SHA256, "E353_PADDED_SRT_DRIFT"),
        fidelity_raw=_read_bound(fidelity_closure, FIDELITY_CLOSURE_SHA256, "E353_FIDELITY_DRIFT"),
        foreign_raw=_read_bound(foreign_consumer, FOREIGN_CONSUMER_SHA256, "E353_FOREIGN_CONSUMER_DRIFT"),
        final_av_raw=_read_bound(final_av_acceptance, FINAL_AV_ACCEPTANCE_SHA256, "E353_FINAL_AV_DRIFT"),
        media_result_raw=_read_bound(final_media_result, FINAL_MEDIA_RESULT_SHA256, "E353_FINAL_MEDIA_RESULT_DRIFT"),
        audio_record_raw=_read_bound(audio_verified_record, AUDIO_VERIFIED_RECORD_SHA256, "E353_AUDIO_RECORD_DRIFT"),
        boundary_raw=_read_bound(BOUNDARY_EVIDENCE_PATH, BOUNDARY_EVIDENCE_SHA256, "E353_BOUNDARY_EVIDENCE_DRIFT"),
    )


def validate_final_review_successor(review: object, *, expected_srt_sha256: str | None = None) -> dict[str, object]:
    if not isinstance(review, Mapping) or review.get("schema_version") != FINAL_REVIEW_SCHEMA_VERSION:
        raise E353ContentFormalSuccessorError("E353_FINAL_REVIEW_SCHEMA_INVALID")
    document = deepcopy(dict(review))
    if not _is_sealed(document):
        raise E353ContentFormalSuccessorError("E353_FINAL_REVIEW_RECEIPT_INVALID")
    if expected_srt_sha256 is not None and expected_srt_sha256 != CURRENT_SRT_SHA256:
        raise E353ContentFormalSuccessorError("E353_FINAL_REVIEW_SRT_BINDING_MISMATCH")
    try:
        parent_raw = base64.b64decode(str(document["parent_srt_raw_b64"]), validate=True)
        current_raw = base64.b64decode(str(document["current_srt_raw_b64"]), validate=True)
        padded_raw = base64.b64decode(str(document["padded_srt_raw_b64"]), validate=True)
    except (KeyError, ValueError) as exc:
        raise E353ContentFormalSuccessorError("E353_FINAL_REVIEW_PAYLOAD_INVALID") from exc
    try:
        raw = {key: base64.b64decode(str(document["evidence_raw_b64"][key]), validate=True)
               for key in ("fidelity", "foreign", "final_av", "media_result", "audio_record", "boundary")}
        parent_review = document["parent_review"]
    except (KeyError, TypeError, ValueError) as exc:
        raise E353ContentFormalSuccessorError("E353_FINAL_REVIEW_PAYLOAD_INVALID") from exc
    expected = _build_from_bound_inputs(
        parent_review=parent_review, parent_raw=parent_raw, current_raw=current_raw,
        padded_raw=padded_raw, fidelity_raw=raw["fidelity"], foreign_raw=raw["foreign"],
        final_av_raw=raw["final_av"], media_result_raw=raw["media_result"],
        audio_record_raw=raw["audio_record"], boundary_raw=raw["boundary"],
    )
    if document != expected:
        raise E353ContentFormalSuccessorError("E353_FINAL_REVIEW_REPLAY_MISMATCH")
    return document


def build_chat_authority_successor(*, parent_chat_authority: Path, final_review_successor: Mapping[str, object]) -> dict[str, object]:
    raw = _read_bound(parent_chat_authority, PARENT_CHAT_FILE_SHA256, "E353_PARENT_CHAT_DRIFT")
    parent = _json(
        raw,
        "E353_PARENT_CHAT_INVALID",
        review=True,
        directory=Path(parent_chat_authority).parent,
    )
    review = validate_final_review_successor(final_review_successor, expected_srt_sha256=CURRENT_SRT_SHA256)
    chat = deepcopy(parent)
    ledger = {
        "schema_version": CHAT_SUCCESSOR_SCHEMA_VERSION,
        "status": "PASS",
        "candidate_id": CANDIDATE_ID,
        "parent_chat_file_sha256": PARENT_CHAT_FILE_SHA256,
        "parent_srt_sha256": PARENT_SRT_SHA256,
        "current_srt_sha256": CURRENT_SRT_SHA256,
        "fidelity_closure_file_sha256": FIDELITY_CLOSURE_SHA256,
        "final_review_successor_receipt_sha256": review["receipt_sha256"],
        "provider_calls": 0,
        "asr_calls": 0,
        "render_calls": 0,
        "production_state_mutations": 0,
        "upload_calls": 0,
    }
    chat["e353_content_successor"] = _seal(ledger)
    chat["final_review_audit"] = deepcopy(review)
    plain = CURRENT_SRT_SHA256.removeprefix("sha256:")
    for field in ("final_text_srt_sha256", "final_speaker_srt_sha256", "final_output_srt_sha256"):
        chat[field] = plain
    chat["candidate_id"] = CANDIDATE_ID
    chat["speaker_mode"] = "uniform_host"
    chat["speaker_ass_path"] = None
    return chat
