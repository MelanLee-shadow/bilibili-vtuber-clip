from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import review_package_owner_audit as package_audit
from src.autoslice import source_language_preservation_supersession as owner


CANDIDATE_ID = "auto_test_source_language_owner"
ENTITY_ROW_INDEX = 1
CUE_INDEX = 7
FINAL_CUE_INDEX = 3
PADDED_START_MS = 12_000
PADDED_END_MS = 13_400
DELIVERY_START_MS = 10_000
DRAFT_TEXT = "原始中文表述"
ATTEMPTED_TEXT = "原始かな表述"


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _srt() -> str:
    blocks = []
    for index in range(1, 5):
        start = (index - 1) * 1_000
        end = start + 900
        text = DRAFT_TEXT if index == FINAL_CUE_INDEX else f"句{index}"
        if index == FINAL_CUE_INDEX:
            start = PADDED_START_MS - DELIVERY_START_MS
            end = PADDED_END_MS - DELIVERY_START_MS
        blocks.append(
            f"{index}\n"
            f"00:00:{start // 1000:02d},{start % 1000:03d} --> "
            f"00:00:{end // 1000:02d},{end % 1000:03d}\n"
            f"{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _parent() -> dict[str, object]:
    rows: list[dict[str, object]] = [
        {"matched_start_ms": 1, "matched_end_ms": 2},
        {
            "matched_start_ms": PADDED_START_MS,
            "matched_end_ms": PADDED_END_MS,
            "before": [DRAFT_TEXT],
            "after": [ATTEMPTED_TEXT],
            "mode": "final_review_context_adjudication",
            "decision_authority": "CPA_JUDGE",
        },
    ]
    return {
        "candidate_id": CANDIDATE_ID,
        "entity_repairs": rows,
        "final_source_language_preservation_audit": {
            "schema_version": "source-language-preservation-audit.v1",
            "status": "BLOCKED_UNPROVEN_FOREIGN_SPEAKER",
            "unproven_foreign_introductions": [
                {
                    "attempted": ATTEMPTED_TEXT,
                    "cue_index": CUE_INDEX,
                    "draft": DRAFT_TEXT,
                    "end_ms": PADDED_END_MS,
                    "reason": owner.REASON_CODE,
                    "start_ms": PADDED_START_MS,
                }
            ],
        },
    }


def _fixture(
    tmp_path: Path,
    *,
    receipt_schema_version: str = owner.GENERIC_RECEIPT_SCHEMA_VERSION,
) -> tuple[dict[str, object], dict[str, object], Path, Path, Path]:
    root = tmp_path / "package"
    evidence = root / "evidence/source-language"
    evidence.mkdir(parents=True)
    parent = _parent()
    parent_path = evidence / f"{CANDIDATE_ID}.parent-chat-authority.json"
    parent_path.write_text(json.dumps(parent, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    srt_path = root / f"{CANDIDATE_ID}.srt"
    srt_path.write_text(_srt(), encoding="utf-8")
    authority = {
        "candidate_id": CANDIDATE_ID,
        "receipt_schema_version": receipt_schema_version,
        "status": owner.STATUS,
        "authority_scope": owner.AUTHORITY_SCOPE,
        "boundary_owner_retention_authorized": True,
        "subtitle_text_truth_authorized": False,
        "public_text_authorized": False,
        "parent_chat_preimage_path": str(parent_path.relative_to(root)),
        "parent_chat_preimage_sha256": _sha256_bytes(parent_path.read_bytes()),
        "parent_chat_projected_sha256": owner._canonical_sha256(
            owner.project_historical_preimage(parent)
        ),
        "final_srt_sha256": _sha256_bytes(srt_path.read_bytes()),
        "entity_row_index": ENTITY_ROW_INDEX,
        "cue_index": CUE_INDEX,
        "final_cue_index": FINAL_CUE_INDEX,
        "padded_start_ms": PADDED_START_MS,
        "padded_end_ms": PADDED_END_MS,
        "delivery_start_ms": DELIVERY_START_MS,
        "draft_text": DRAFT_TEXT,
        "attempted_text": ATTEMPTED_TEXT,
        "reason_code": owner.REASON_CODE,
    }
    authority_path = tmp_path / "authority.json"
    authority_path.write_text(
        json.dumps(
            {
                "schema_version": owner.AUTHORITY_SCHEMA_VERSION,
                "entries": [authority],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    current = copy.deepcopy(parent)
    row = current["entity_repairs"][ENTITY_ROW_INDEX]
    assert isinstance(row, dict)
    row["boundary_required"] = True
    row["boundary_owner_id"] = (
        f"entity_repair:{ENTITY_ROW_INDEX + 1}:{PADDED_START_MS}:{PADDED_END_MS}"
    )
    owner.register_reconciliation(
        chat_authority=current,
        parent_chat_path=parent_path,
        final_srt_path=srt_path,
        authority=authority,
    )
    chat_path = root / f"{CANDIDATE_ID}.chat-authority.json"
    chat_path.write_text(json.dumps(current, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return current, authority, authority_path, chat_path, srt_path


def _valid(current: dict[str, object], authority_path: Path, chat_path: Path) -> bool:
    rows = current["entity_repairs"]
    assert isinstance(rows, list)
    row = rows[ENTITY_ROW_INDEX]
    assert isinstance(row, dict)
    return owner.reconciled_boundary_owner_valid(
        row=row,
        row_index=ENTITY_ROW_INDEX,
        chat_authority=current,
        chat_authority_path=chat_path,
        authority_path=authority_path,
    )


def test_generic_authority_validates_hash_bound_parent_srt_and_owner(tmp_path: Path):
    current, _authority, authority_path, chat_path, _srt_path = _fixture(tmp_path)
    assert _valid(current, authority_path, chat_path)


def test_package_receipt_cannot_self_authorize_uncommitted_text(tmp_path: Path):
    current, _authority, authority_path, chat_path, _srt_path = _fixture(tmp_path)
    forged = copy.deepcopy(current)
    rows = forged["entity_repairs"]
    assert isinstance(rows, list) and isinstance(rows[ENTITY_ROW_INDEX], dict)
    receipt = rows[ENTITY_ROW_INDEX]["reconciliation"]
    assert isinstance(receipt, dict)
    receipt["draft_text"] = "包内自称正确但仓库 authority 未授权"
    assert not _valid(forged, authority_path, chat_path)


def test_authority_or_final_srt_drift_fails_closed(tmp_path: Path):
    current, _authority, authority_path, chat_path, srt_path = _fixture(tmp_path)
    srt_path.write_text(_srt().replace(DRAFT_TEXT, "被篡改"), encoding="utf-8")
    assert not _valid(current, authority_path, chat_path)

    current, _authority, authority_path, chat_path, _srt_path = _fixture(tmp_path / "second")
    document = json.loads(authority_path.read_text(encoding="utf-8"))
    document["entries"][0]["parent_chat_projected_sha256"] = "sha256:" + "0" * 64
    authority_path.write_text(json.dumps(document), encoding="utf-8")
    assert not _valid(current, authority_path, chat_path)


def test_custom_receipt_schema_is_authorized_only_by_exact_candidate_entry(tmp_path: Path):
    custom = "historical-candidate-specific-supersession.v7"
    current, authority, authority_path, chat_path, _srt_path = _fixture(
        tmp_path, receipt_schema_version=custom
    )
    rows = current["entity_repairs"]
    assert isinstance(rows, list) and isinstance(rows[ENTITY_ROW_INDEX], dict)
    receipt = rows[ENTITY_ROW_INDEX]["reconciliation"]
    assert isinstance(receipt, dict)
    assert receipt["schema_version"] == custom
    assert owner.receipt_routes_to_source_language_supersession(
        receipt, authority_path=authority_path
    )
    assert _valid(current, authority_path, chat_path)

    borrowed = {**receipt, "candidate_id": "auto_borrowed_schema"}
    assert not owner.receipt_routes_to_source_language_supersession(
        borrowed, authority_path=authority_path
    )
    assert authority["receipt_schema_version"] == custom


def test_malformed_receipt_schema_in_authority_fails_closed(tmp_path: Path):
    _current, _authority, authority_path, _chat_path, _srt_path = _fixture(tmp_path)
    document = json.loads(authority_path.read_text(encoding="utf-8"))
    document["entries"][0]["receipt_schema_version"] = "../../not-a-schema"
    authority_path.write_text(json.dumps(document), encoding="utf-8")
    try:
        owner.load_authority_entry(CANDIDATE_ID, authority_path=authority_path)
    except ValueError as exc:
        assert str(exc) == "SOURCE_LANGUAGE_SUPERSESSION_RECEIPT_SCHEMA_INVALID"
    else:
        raise AssertionError("malformed receipt schema unexpectedly accepted")


def test_receipt_scope_never_authorizes_text_or_public_copy(tmp_path: Path):
    current, _authority, _authority_path, _chat_path, _srt_path = _fixture(tmp_path)
    rows = current["entity_repairs"]
    assert isinstance(rows, list) and isinstance(rows[ENTITY_ROW_INDEX], dict)
    receipt = rows[ENTITY_ROW_INDEX]["reconciliation"]
    assert isinstance(receipt, dict)
    assert receipt["authority_scope"] == owner.AUTHORITY_SCOPE
    assert receipt["boundary_owner_retention_authorized"] is True
    assert receipt["subtitle_text_truth_authorized"] is False
    assert receipt["public_text_authorized"] is False


def test_malformed_unrelated_entry_invalidates_entire_authority(tmp_path: Path):
    _current, _authority, authority_path, _chat_path, _srt_path = _fixture(tmp_path)
    document = json.loads(authority_path.read_text(encoding="utf-8"))
    document["entries"].append({"candidate_id": "auto_other"})
    authority_path.write_text(json.dumps(document), encoding="utf-8")
    try:
        owner.load_authority_entry(CANDIDATE_ID, authority_path=authority_path)
    except ValueError as exc:
        assert str(exc).startswith("SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_FIELD_INVALID:")
    else:
        raise AssertionError("malformed unrelated authority entry was ignored")


def test_duplicate_candidate_entries_fail_closed(tmp_path: Path):
    _current, _authority, authority_path, _chat_path, _srt_path = _fixture(tmp_path)
    document = json.loads(authority_path.read_text(encoding="utf-8"))
    document["entries"].append(copy.deepcopy(document["entries"][0]))
    authority_path.write_text(json.dumps(document), encoding="utf-8")
    try:
        owner.load_authority_entry(CANDIDATE_ID, authority_path=authority_path)
    except ValueError as exc:
        assert str(exc) == "SOURCE_LANGUAGE_SUPERSESSION_AUTHORITY_DUPLICATE_CANDIDATE"
    else:
        raise AssertionError("duplicate candidate authority unexpectedly accepted")


def test_symlinked_authority_chat_parent_or_final_srt_fails_closed(tmp_path: Path):
    current, _authority, authority_path, chat_path, srt_path = _fixture(tmp_path)

    real_authority = tmp_path / "real-authority.json"
    authority_path.replace(real_authority)
    authority_path.symlink_to(real_authority)
    assert not _valid(current, authority_path, chat_path)

    current, authority, authority_path, chat_path, srt_path = _fixture(tmp_path / "chat")
    real_chat = chat_path.with_name("real-chat.json")
    chat_path.replace(real_chat)
    chat_path.symlink_to(real_chat)
    assert not _valid(current, authority_path, chat_path)

    current, authority, authority_path, chat_path, srt_path = _fixture(tmp_path / "parent")
    parent_path = chat_path.parent / str(authority["parent_chat_preimage_path"])
    real_parent = parent_path.with_name("real-parent.json")
    parent_path.replace(real_parent)
    parent_path.symlink_to(real_parent)
    assert not _valid(current, authority_path, chat_path)

    current, _authority, authority_path, chat_path, srt_path = _fixture(tmp_path / "srt")
    real_srt = srt_path.with_name("real-final.srt")
    srt_path.replace(real_srt)
    srt_path.symlink_to(real_srt)
    assert not _valid(current, authority_path, chat_path)


def test_candidate_identity_is_required_on_current_chat_authority(tmp_path: Path):
    current, _authority, authority_path, chat_path, _srt_path = _fixture(tmp_path)
    current.pop("candidate_id")
    assert not _valid(current, authority_path, chat_path)


def test_review_package_owner_audit_consumes_candidate_bound_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current, _authority, authority_path, chat_path, _srt_path = _fixture(tmp_path)
    rows = current["entity_repairs"]
    assert isinstance(rows, list)
    outside = rows[0]
    target = rows[ENTITY_ROW_INDEX]
    assert isinstance(outside, dict) and isinstance(target, dict)
    outside["boundary_required"] = False
    outside["boundary_owner_rejection"] = "OUTSIDE_IMMUTABLE_STORY_SCOPE"

    monkeypatch.setattr(
        package_audit,
        "receipt_routes_to_source_language_supersession",
        lambda value: owner.receipt_routes_to_source_language_supersession(
            value, authority_path=authority_path
        ),
    )
    monkeypatch.setattr(
        package_audit,
        "source_language_reconciled_boundary_owner_valid",
        lambda **kwargs: owner.reconciled_boundary_owner_valid(
            **kwargs, authority_path=authority_path
        ),
    )

    owner_id = str(target["boundary_owner_id"])
    owner_key = ("entity_repair", owner_id)
    frozen = {
        owner_key: {
            "required": True,
            "local_windows": [{"start_ms": PADDED_START_MS, "end_ms": PADDED_END_MS}],
        }
    }
    assert package_audit._story_owner_set_valid(
        chat_authority=current,
        chat_authority_path=chat_path,
        frozen_owner_by_key=frozen,
        frozen_owner_keys=[owner_key],
        story_start=PADDED_START_MS - 100,
        story_end=PADDED_END_MS + 100,
    )

    receipt = target["reconciliation"]
    assert isinstance(receipt, dict)
    receipt["candidate_id"] = "auto_borrowed_schema"
    assert not package_audit._story_owner_set_valid(
        chat_authority=current,
        chat_authority_path=chat_path,
        frozen_owner_by_key=frozen,
        frozen_owner_keys=[owner_key],
        story_start=PADDED_START_MS - 100,
        story_end=PADDED_END_MS + 100,
    )
