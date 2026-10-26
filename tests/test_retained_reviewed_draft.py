import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice.clip_context import SCHEMA_VERSION as CLIP_CONTEXT_SCHEMA
from src.autoslice.boundary_semantic_review import cue_grid_sha256
from src.autoslice.producer_boundary_owner_contract import freeze_required_boundary_owner_contract
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.producer_text_pipeline import (
    CurrentTextContinuationError,
    TextPipelineAdapters,
    _retained_parent_cpa_usage_seed,
    _validate_retained_reviewed_parent_input,
    _transcribe_draft,
)
from src.autoslice.retained_reviewed_draft import (
    RetainedReviewedDraftError,
    build_retained_source_boundary_context,
    load_retained_reviewed_draft,
)
from src.autoslice.talk_filler import verify_automatic_filler_plan


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def psha(raw: bytes) -> str:
    return "sha256:" + sha(raw)


def canonical_context(payload: dict) -> dict:
    payload = dict(payload)
    payload["context_sha256"] = psha(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    return payload


def _payload() -> tuple[dict, str, str]:
    current_srt = (
        "1\n00:00:00,010 --> 00:00:00,030\n保留前\n\n"
        "2\n00:00:00,110 --> 00:00:00,120\n删除段\n\n"
        "3\n00:00:00,150 --> 00:00:00,170\n保留后\n"
    )
    source_raw = current_srt.encode()
    media_sha = "a" * 64
    plan = {
        "schema_version": "talk-filler-plan.v1",
        "status": "active",
        "source_start_ms": 0,
        "source_end_ms": 200,
        "source_srt_sha256": psha(source_raw),
        "effective_duration_ms": 160,
        "removed_duration_ms": 40,
        "retained_intervals": [
            {"start_ms": 0, "end_ms": 100},
            {"start_ms": 140, "end_ms": 200},
        ],
        "removals": [
            {
                "proposal_id": "gift-thanks",
                "source_start_ms": 100,
                "source_end_ms": 140,
                "authorization_kind": "automatic",
                "reason": "housekeeping",
            }
        ],
    }
    cues = [
        SimpleNamespace(
            index=cue.index,
            source_start_ms=cue.start_ms,
            source_end_ms=cue.end_ms,
            text=cue.text,
        )
        for cue in parse_srt_cues(current_srt)
    ]
    payload = {
        "topic_complete": True,
        "cause_answer_chain_complete": True,
        "referents_resolved": True,
        "corrections_preserved": True,
        "punchline_preserved": True,
        "closing_resolution_preserved": True,
        "audience_interactions_consistent": True,
        "audience_feedback_preserved": True,
        "meaningful_repeats_preserved": True,
        "meaning_or_stance_changed": False,
        "pass": True,
        "reason": "保留内容完整",
    }
    prompts: list[str] = []
    first = verify_automatic_filler_plan(
        cues=cues,
        plan=plan,
        llm_call=lambda prompt: prompts.append(prompt) or json.dumps(payload, ensure_ascii=False),
    )
    assert first["status"] == "PASS"
    plan["global_verification"] = first
    assert len(prompts) == 1
    return {"current_srt": source_raw, "plan": plan, "request": prompts[0], "response": json.dumps(payload, ensure_ascii=False), "media_sha": media_sha}, prompts[0], json.dumps(payload, ensure_ascii=False)


def _fixture(tmp_path: Path):
    data, request, response = _payload()
    out = tmp_path / "prepared"
    out.mkdir()
    source_raw = data["current_srt"]
    source_sha = data["media_sha"]
    piece_bytes = [b"left-piece", b"right-piece"]
    piece_rows = []
    for index, (start, end) in enumerate(((0, 100), (140, 200))):
        media = out / f"piece_{index}_{start}_{end}.mp4"
        media.write_bytes(piece_bytes[index])
        row = {
            "source_path": "/recording/source.mp4",
            "source_sha256": source_sha,
            "source_media_binding": "sha256:" + source_sha,
            "start_ms": start,
            "end_ms": end,
            "output_path": str(media.resolve()),
            "output_sha256": sha(piece_bytes[index]),
        }
        provenance = media.with_suffix(".provenance.json")
        provenance.write_text(json.dumps(row, sort_keys=True), encoding="utf-8")
        piece_rows.append(row)
    padded_bytes = b"".join(piece_bytes)
    padded = out / "padded_0_200.mp4"
    padded.write_bytes(padded_bytes)
    padded.with_suffix(".provenance.json").write_text(
        json.dumps(
            {
                "inputs": [
                    {"path": row["output_path"], "sha256": row["output_sha256"]}
                    for row in piece_rows
                ],
                "output_path": str(padded.resolve()),
                "output_sha256": sha(padded_bytes),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    parent_bytes = b"immutable-parent-materialization"
    parent_piece = out / "parent-piece.mp4"
    parent_piece.write_bytes(parent_bytes)
    parent_piece_row = {
        "source_path": "/recording/source.mp4",
        "source_sha256": source_sha,
        "source_media_binding": "sha256:" + source_sha,
        "start_ms": 0,
        "end_ms": 200,
        "output_path": str(parent_piece.resolve()),
        "output_sha256": sha(parent_bytes),
    }
    parent_piece.with_suffix(".provenance.json").write_text(
        json.dumps(parent_piece_row, sort_keys=True), encoding="utf-8"
    )
    parent_padded = out / "parent-padded.mp4"
    parent_padded.write_bytes(parent_bytes)
    parent_padded.with_suffix(".provenance.json").write_text(
        json.dumps(
            {
                "inputs": [
                    {"path": str(parent_piece.resolve()), "sha256": sha(parent_bytes)}
                ],
                "output_path": str(parent_padded.resolve()),
                "output_sha256": sha(parent_bytes),
                "duration_ms": 200,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    context = canonical_context(
        {
            "schema_version": CLIP_CONTEXT_SCHEMA,
            "candidate_id": "candidate-1",
            "recording_date": "2026-10-07",
            "selection_hook": "talk",
            "pieces": [{"source_media_sha256": "sha256:" + source_sha, "start_ms": 0, "end_ms": 200}],
            "whole_clip_draft_srt": source_raw.decode(),
            "whole_clip_draft_srt_sha256": psha(source_raw),
            "structured_chat": [],
            "topic_resolution": {},
            "session_topic_authorities": [],
            "speech_memory": {},
            "retrieval_budget": {
                "whole_clip_transcript_truncated": False,
                "structured_chat_row_cap": 240,
                "structured_chat_total_rows": 0,
                "structured_chat_selected_rows": 0,
                "structured_chat_truncated": False,
            },
            "mutation_authorized": False,
        }
    )
    authority = {
        "schema_version": "chat-authority-audit.v2",
        "final_review_audit": {
            "boundary_semantic_review": {
                "final_endpoint_binding": {
                    "status": "PASS",
                    "final_start_ms": 0,
                    "final_end_ms": 200,
                },
                "source_separation_witness": {
                    "status": "PASS",
                    "source_final_start_ms": 0,
                    "source_final_end_ms": 200,
                },
            }
        },
    }
    paths = {
        "current_srt_path": out / "current.srt",
        "parent_authority_path": out / "parent-authority.json",
        "parent_clip_context_path": out / "parent-context.json",
        "filler_plan_path": out / "plan.json",
        "verifier_request_path": out / "verifier-request.txt",
        "verifier_response_path": out / "verifier-response.txt",
    }
    paths["current_srt_path"].write_bytes(source_raw)
    paths["parent_authority_path"].write_text(json.dumps(authority, sort_keys=True), encoding="utf-8")
    paths["parent_clip_context_path"].write_text(json.dumps(context, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    plan_raw = json.dumps(data["plan"], ensure_ascii=False, sort_keys=True).encode()
    paths["filler_plan_path"].write_bytes(plan_raw)
    paths["verifier_request_path"].write_text(request, encoding="utf-8")
    paths["verifier_response_path"].write_text(response, encoding="utf-8")
    spec = {
        "date": "2026-10-07",
        "pieces": [
            {"remote_media": "/recording/source.mp4", "start_ms": 0, "end_ms": 100},
            {"remote_media": "/recording/source.mp4", "start_ms": 140, "end_ms": 200},
        ],
        "retained_current_text": {
            "schema_version": "retained-reviewed-draft.v1",
            "current_srt_path": str(paths["current_srt_path"]),
            "current_srt_sha256": psha(source_raw),
            "parent_authority_path": str(paths["parent_authority_path"]),
            "parent_authority_sha256": psha(paths["parent_authority_path"].read_bytes()),
            "parent_clip_context_path": str(paths["parent_clip_context_path"]),
            "parent_clip_context_sha256": psha(paths["parent_clip_context_path"].read_bytes()),
            "filler_plan_path": str(paths["filler_plan_path"]),
            "filler_plan_sha256": psha(plan_raw),
            "verifier_request_path": str(paths["verifier_request_path"]),
            "verifier_request_sha256": psha(paths["verifier_request_path"].read_bytes()),
            "verifier_response_path": str(paths["verifier_response_path"]),
            "verifier_response_sha256": psha(paths["verifier_response_path"].read_bytes()),
            "padded_sha256": psha(padded_bytes),
            "parent_materialization_path": str(parent_padded),
            "parent_materialization_sha256": psha(parent_bytes),
            "parent_materialization_duration_ms": 200,
            "parent_materialization_provenance_path": str(parent_padded.with_suffix(".provenance.json")),
            "parent_materialization_provenance_sha256": psha(parent_padded.with_suffix(".provenance.json").read_bytes()),
            "source_timeline_offset_ms": 0,
            "source_media_sha256s": ["sha256:" + source_sha],
            "source_binding": {
                "source_srt_sha256": psha(source_raw),
                "source_sha256": source_sha,
                "source_media_binding": "sha256:" + source_sha,
            },
        },
    }
    return spec, out, padded, data


def _identity_fixture(tmp_path: Path):
    """Convert the normal fixture into one strict no-deletion identity plan."""

    spec, out, padded, data = _fixture(tmp_path)
    config = spec["retained_current_text"]
    plan = data["plan"]
    plan.update(
        retained_intervals=[{"start_ms": 0, "end_ms": 200}],
        removals=[],
        effective_duration_ms=200,
        removed_duration_ms=0,
        global_verification={
            "status": "NOT_REQUIRED",
            "reason_code": "NO_AUTOMATIC_REMOVAL",
        },
    )
    plan_path = Path(config["filler_plan_path"])
    plan_raw = json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()
    plan_path.write_bytes(plan_raw)
    config["filler_plan_sha256"] = psha(plan_raw)
    for key in (
        "verifier_request_path",
        "verifier_request_sha256",
        "verifier_response_path",
        "verifier_response_sha256",
    ):
        config.pop(key, None)

    left = out / "piece_0_0_100.mp4"
    right = out / "piece_1_140_200.mp4"
    identity_media = out / "piece_0_0_200.mp4"
    identity_bytes = left.read_bytes() + right.read_bytes()
    identity_media.write_bytes(identity_bytes)
    identity_row = {
        "source_path": "/recording/source.mp4",
        "source_sha256": data["media_sha"],
        "source_media_binding": "sha256:" + data["media_sha"],
        "start_ms": 0,
        "end_ms": 200,
        "output_path": str(identity_media.resolve()),
        "output_sha256": sha(identity_bytes),
    }
    (out / "piece_0_0_200.provenance.json").write_text(
        json.dumps(identity_row, sort_keys=True), encoding="utf-8"
    )
    padded.with_suffix(".provenance.json").write_text(
        json.dumps(
            {
                "inputs": [
                    {"path": identity_row["output_path"], "sha256": identity_row["output_sha256"]}
                ],
                "output_path": str(padded.resolve()),
                "output_sha256": sha(identity_bytes),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    spec["pieces"] = [
        {"remote_media": "/recording/source.mp4", "start_ms": 0, "end_ms": 200}
    ]
    return spec, out, padded, data


def _load(spec, out, padded, *, validator=None, durations=None):
    return load_retained_reviewed_draft(
        spec=spec,
        durations=durations or [100, 100],
        padded=padded,
        padded_dur=200,
        out_root=out,
        cid="candidate-1",
        validate_parent=validator or (lambda **kwargs: {"status": "INPUT_VALIDATED"}),
    )


def _focused_parent_fixture(tmp_path: Path) -> tuple[dict, dict, dict, dict, Path, bytes]:
    """Build the smallest real-shaped BLOCK parent for focused input validation.

    The resolver audit is in the local delivery clock (29,950 ms), while the
    original finite piece starts at 395,460 ms.  The focused validator must
    therefore return 425,410 ms for the projection clock and retain BLOCK as
    historical state.
    """

    srt_text = "1\n00:00:00,010 --> 00:00:00,030\n保留文字\n"
    source_sha = "a" * 64
    padded_raw = b"new-padded-media"
    parent_padded_raw = b"parent-padded-media"
    padded_sha = sha(parent_padded_raw)
    request_raw = b"source-full-window-request"
    source_srt = (
        "1\n00:00:29,960 --> 00:00:29,980\n保留文字\n\n"
        "2\n00:04:41,300 --> 00:04:41,400\n源窗末句\n"
    )
    source_grid = cue_grid_sha256(parse_srt_cues(source_srt))
    frozen_audit = {}
    target, scope = freeze_required_boundary_owner_contract(
        spec={"candidate_id": "candidate-1", "pieces": [{"start_ms": 395460, "end_ms": 722000}],
              "semantic_start_ms": 395460, "semantic_end_ms": 676860},
        durations=[326540], chat_authority_audit=frozen_audit,
        required_boundary_owners=[],
    )

    audit = {
        "schema_version": "boundary-audit.v1",
        "final_start_ms": 29950,
        "final_end_ms": 281400,
        "frozen_required_boundary_owners": frozen_audit["frozen_boundary_owner_contract"]["owners"],
        "required_boundary_owner_verification": {"status": "PASS", "failures": []},
        "boundary_semantic_review": {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "PASS",
            "review_scope": "source_full_window",
            "candidate_id": "candidate-1",
            "request_sha256": psha(request_raw),
            "cue_grid_sha256": source_grid,
            "target_ms": target,
            "boundary_search_scope": scope,
            "final_endpoint_binding": {
                "schema_version": "talk-boundary-final-endpoint-binding.v1",
                "status": "PASS",
                "final_start_ms": 29950,
                "final_end_ms": 281400,
                "semantic_cue_grid_sha256": source_grid,
                "semantic_request_sha256": psha(request_raw),
            },
        },
    }
    audit_path = tmp_path / "source-boundary-audit.json"
    audit_raw = json.dumps(audit, ensure_ascii=False, sort_keys=True).encode()
    audit_path.write_bytes(audit_raw)

    context = {
        "schema_version": CLIP_CONTEXT_SCHEMA,
        "candidate_id": "candidate-1",
        "recording_date": "2026-10-07",
        "pieces": [
            {
                "source_media_sha256": "sha256:" + source_sha,
                "start_ms": 395460,
                "end_ms": 722000,
            }
        ],
    }
    source_context = {
        "schema_version": "source-boundary-transcript-context.v1",
        "source_srt_sha256": psha(source_srt.encode()),
        "source_cue_grid_sha256": source_grid,
    }
    final_review = {
        "schema_version": "final-review-audit.v2",
        "status": "FLAGGED",
        "release_gate": "BLOCK",
        "reviewed_srt_sha256": psha(srt_text.encode()),
        "discovery": {"status": "COMPLETE"},
        "correction_mutation_authority": {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "PASS",
        },
        "findings": [{"finding_id": "cue-55", "status": "UNRESOLVED"}],
        "validated_finding_count": 1,
        "source_boundary_context": source_context,
        "boundary_semantic_review": {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "BLOCK",
        },
        "correction_pass": {
            "findings": [],
        },
    }
    authority = {
        "schema_version": "chat-authority-audit.v2",
        "mutation_authorized": False,
        "final_output_srt_sha256": psha(srt_text.encode()),
        "final_review_audit": final_review,
        "entity_repairs": [],
        "post_transcript_entity_output_srt": source_srt,
        "post_transcript_entity_output_srt_sha256": psha(source_srt.encode()),
        "frozen_boundary_owner_contract": frozen_audit["frozen_boundary_owner_contract"],
        # This is the immutable parent binding for the resolver's local clock;
        # it is deliberately separate from the finite source SHA in context.
        "source_media_timeline_offset_ms": 29950,
        "source_media_sha256": padded_sha,
    }
    config = {
        "source_boundary_audit_path": str(audit_path),
        "source_boundary_audit_sha256": psha(audit_raw),
        "source_boundary_delivery_offset_ms": 29950,
        "source_timeline_offset_ms": 425410,
        "parent_materialization_sha256": psha(parent_padded_raw),
        "parent_materialization_duration_ms": 326540,
        "parent_materialization_path": str(tmp_path / "parent-padded.mp4"),
        "parent_materialization_provenance_path": str(
            tmp_path / "parent-padded.provenance.json"
        ),
        "parent_materialization_provenance_sha256": psha(
            b'parent-materialization-provenance'
        ),
    }
    parent_path = tmp_path / "parent-padded.mp4"
    parent_path.write_bytes(parent_padded_raw)
    parent_piece_path = tmp_path / "parent-piece.mp4"
    parent_piece_path.write_bytes(parent_padded_raw)
    parent_piece_path.with_suffix(".provenance.json").write_text(
        json.dumps(
            {
                "start_ms": 395460,
                "end_ms": 722000,
                "source_media_binding": "sha256:" + source_sha,
                "source_sha256": source_sha,
                "output_path": str(parent_piece_path.resolve()),
                "output_sha256": sha(parent_padded_raw),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    parent_provenance = {
        "inputs": [
            {"path": str(parent_piece_path.resolve()), "sha256": sha(parent_padded_raw)}
        ],
        "output_path": str(parent_path.resolve()),
        "output_sha256": sha(parent_padded_raw),
        "duration_ms": 326540,
    }
    parent_provenance_raw = json.dumps(parent_provenance, sort_keys=True).encode()
    parent_provenance_path = parent_path.with_suffix(".provenance.json")
    parent_provenance_path.write_bytes(parent_provenance_raw)
    config["parent_materialization_provenance_sha256"] = psha(parent_provenance_raw)
    return authority, context, config, audit, audit_path, padded_raw


def _validate_focused_parent(
    authority: dict,
    context: dict,
    config: dict,
    padded_raw: bytes,
    *,
    srt_text: str = "1\n00:00:00,010 --> 00:00:00,030\n保留文字\n",
    new_padded_duration_ms: int = 326540,
):
    return _validate_retained_reviewed_parent_input(
        authority=authority,
        srt_text=srt_text,
        srt_sha256=sha(srt_text.encode()),
        clip_context=context,
        candidate_id="candidate-1",
        boundary_semantic_review=authority["final_review_audit"]["boundary_semantic_review"],
        retained_config=config,
        padded_sha256=psha(padded_raw),
        source_media_sha256s=["sha256:" + "a" * 64],
        padded_duration_ms=new_padded_duration_ms,
        parent_materialization_path=config.get("parent_materialization_path"),
        parent_materialization_sha256=config.get("parent_materialization_sha256"),
        parent_materialization_duration_ms=config.get("parent_materialization_duration_ms"),
        parent_materialization_provenance_path=config.get(
            "parent_materialization_provenance_path"
        ),
        parent_materialization_provenance_sha256=config.get(
            "parent_materialization_provenance_sha256"
        ),
    )


def test_replays_exact_verifier_and_returns_projection_only(tmp_path):
    spec, out, padded, _ = _fixture(tmp_path)
    result = _load(spec, out, padded)

    assert result["proof"]["status"] == "PROJECTED"
    assert result["proof"]["fresh_review_required"] is True
    assert result["projection"]["status"] == "PROJECTED"
    assert [row["new_cue_index"] for row in result["projection"]["cue_map"]] == ["1", "2"]
    assert "release_gate" not in result["proof"]
    assert "global_verification" not in result["proof"]
    assert (out / "candidate-1.retained-parent-verifier-response.txt").is_file()


def test_loader_accepts_identity_without_verifier_artifacts_and_keeps_projection_only(tmp_path):
    spec, out, padded, _data = _identity_fixture(tmp_path)

    result = _load(spec, out, padded, durations=[200])

    assert result["proof"]["status"] == "PROJECTED"
    assert result["proof"]["fresh_review_required"] is True
    assert result["projection"]["status"] == "PROJECTED"
    assert [row["old_cue_index"] for row in result["projection"]["cue_map"]] == ["1", "2", "3"]
    assert result["projection"]["dropped_cues"] == []
    assert "verifier_response_sha256" not in result["proof"]
    assert "verifier_response_sha256" not in result
    assert "verifier_request" not in result["proof"]["history_paths"]
    assert "verifier_response" not in result["proof"]["history_paths"]
    assert not (out / "candidate-1.retained-parent-verifier-request.txt").exists()
    assert not (out / "candidate-1.retained-parent-verifier-response.txt").exists()


def test_loader_does_not_treat_human_only_removal_as_identity(tmp_path):
    spec, out, padded, data = _fixture(tmp_path)
    plan = data["plan"]
    plan["removals"][0]["authorization_kind"] = "reviewed"
    plan["global_verification"] = {
        "status": "NOT_REQUIRED",
        "reason_code": "NO_AUTOMATIC_REMOVAL",
    }
    plan_raw = json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()
    plan_path = Path(spec["retained_current_text"]["filler_plan_path"])
    plan_path.write_bytes(plan_raw)
    spec["retained_current_text"]["filler_plan_sha256"] = psha(plan_raw)

    with pytest.raises(RetainedReviewedDraftError, match="VERIFIER_REQUEST_NOT_REPLAYED"):
        _load(spec, out, padded)


def test_loader_identity_missing_duration_fails_projection_without_verifier_files(tmp_path):
    spec, out, padded, data = _identity_fixture(tmp_path)
    plan = data["plan"]
    plan.pop("effective_duration_ms")
    plan_raw = json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()
    plan_path = Path(spec["retained_current_text"]["filler_plan_path"])
    plan_path.write_bytes(plan_raw)
    spec["retained_current_text"]["filler_plan_sha256"] = psha(plan_raw)

    with pytest.raises(
        RetainedReviewedDraftError,
        match="RETAINED_PROJECTION_FAILED:FILLER_PLAN_EFFECTIVE_DURATION_MS_REQUIRED",
    ):
        _load(spec, out, padded, durations=[200])


def test_loader_passes_new_projection_and_old_parent_materialization_separately(tmp_path):
    spec, out, padded, data = _fixture(tmp_path)
    seen = {}

    def validator(**kwargs):
        seen.update(kwargs)
        return {"status": "INPUT_VALIDATED"}

    _load(spec, out, padded, validator=validator)

    assert seen["padded_sha256"] == psha(padded.read_bytes())
    assert seen["padded_duration_ms"] == 200
    config = spec["retained_current_text"]
    assert seen["parent_materialization_sha256"] == config["parent_materialization_sha256"]
    assert seen["parent_materialization_duration_ms"] == 200
    assert seen["parent_materialization_sha256"] != seen["padded_sha256"]
    assert seen["parent_materialization_provenance_sha256"] == config[
        "parent_materialization_provenance_sha256"
    ]
    assert data["media_sha"] == "a" * 64


def test_loader_rejects_new_projection_hash_as_old_parent_materialization(tmp_path):
    spec, out, padded, _data = _fixture(tmp_path)
    config = spec["retained_current_text"]
    config["parent_materialization_sha256"] = config["padded_sha256"]

    with pytest.raises(RetainedReviewedDraftError, match="PARENT_MATERIALIZATION_HASH_MISMATCH"):
        _load(spec, out, padded)


def test_loader_rejects_parent_materialization_byte_drift(tmp_path):
    spec, out, padded, _data = _fixture(tmp_path)
    parent_path = Path(spec["retained_current_text"]["parent_materialization_path"])
    parent_path.write_bytes(b"drifted-parent-materialization")

    with pytest.raises(RetainedReviewedDraftError, match="PARENT_MATERIALIZATION_HASH_MISMATCH"):
        _load(spec, out, padded)


def test_loader_rejects_parent_materialization_provenance_drift(tmp_path):
    spec, out, padded, _data = _fixture(tmp_path)
    provenance_path = Path(
        spec["retained_current_text"]["parent_materialization_provenance_path"]
    )
    provenance_path.write_bytes(b"{}")

    with pytest.raises(RetainedReviewedDraftError, match="PARENT_MATERIALIZATION_PROVENANCE_HASH_MISMATCH"):
        _load(spec, out, padded)


def test_verifier_request_mismatch_is_fail_closed(tmp_path):
    spec, out, padded, _ = _fixture(tmp_path)
    request_path = Path(spec["retained_current_text"]["verifier_request_path"])
    request_path.write_text("different request", encoding="utf-8")
    spec["retained_current_text"]["verifier_request_sha256"] = psha(request_path.read_bytes())

    with pytest.raises(RetainedReviewedDraftError, match="VERIFIER_REQUEST_MISMATCH"):
        _load(spec, out, padded)


def test_parent_validation_failure_blocks_before_projection(tmp_path):
    spec, out, padded, _ = _fixture(tmp_path)
    called = []

    def reject(**kwargs):
        called.append(kwargs)
        raise RuntimeError("FINAL_REVIEW_UNRESOLVED_FINDINGS")

    with pytest.raises(RetainedReviewedDraftError, match="PARENT_VALIDATION_FAILED:FINAL_REVIEW_UNRESOLVED_FINDINGS"):
        _load(spec, out, padded, validator=reject)
    assert len(called) == 1
    assert not (out / "candidate-1.retained-parent-filler-plan.json").exists()


def test_piece_output_drift_is_rejected(tmp_path):
    spec, out, padded, _ = _fixture(tmp_path)
    piece = out / "piece_1_140_200.mp4"
    piece.write_bytes(b"drifted piece")

    with pytest.raises(RetainedReviewedDraftError, match="PIECE_1_OUTPUT_HASH_MISMATCH"):
        _load(spec, out, padded)


def test_piece_provenance_range_drift_is_rejected(tmp_path):
    spec, out, padded, _ = _fixture(tmp_path)
    sidecar = out / "piece_1_140_200.provenance.json"
    row = json.loads(sidecar.read_text(encoding="utf-8"))
    row["start_ms"] = 141
    sidecar.write_text(json.dumps(row, sort_keys=True), encoding="utf-8")

    with pytest.raises(RetainedReviewedDraftError, match="PIECE_PROVENANCE_SPEC_RANGE_MISMATCH"):
        _load(spec, out, padded)


def _source_boundary_projection_fixture() -> dict[str, object]:
    source_text = (
        "1\n00:00:00,005 --> 00:00:00,010\n首部padding\n\n"
        "2\n00:00:00,025 --> 00:00:00,035\n旧正文一\n\n"
        "3\n00:00:00,105 --> 00:00:00,135\n删除缝\n\n"
        "4\n00:00:00,150 --> 00:00:00,160\n旧正文二\n\n"
        "5\n00:00:00,210 --> 00:00:00,220\n尾部padding\n"
    )
    source_cues = parse_srt_cues(source_text)
    return {
        "source_boundary_context": {
            "source_srt_text": source_text,
            "source_srt_sha256": psha(source_text.encode()),
            "source_cue_grid_sha256": cue_grid_sha256(source_cues),
            "parent_context_piece_start_ms": 0,
        },
        "projection": {
            "source_range_ms": {"start_ms": 20, "end_ms": 180},
            "physical_source_range_ms": {"start_ms": 20, "end_ms": 180},
            "source_timeline_offset_ms": 0,
            "piece_clock": [
                {
                    "piece_index": 0,
                    "source_start_ms": 0,
                    "source_end_ms": 100,
                    "concat_start_ms": 0,
                    "concat_end_ms": 103,
                    "actual_duration_ms": 103,
                },
                {
                    "piece_index": 1,
                    "source_start_ms": 140,
                    "source_end_ms": 240,
                    "concat_start_ms": 103,
                    "concat_end_ms": 200,
                    "actual_duration_ms": 97,
                },
            ],
        },
    }


def test_source_boundary_projection_preserves_padding_and_replaces_body_on_measured_clock():
    retained = _source_boundary_projection_fixture()
    reviewed = (
        "1\n00:00:00,020 --> 00:00:00,035\n新正文一\n\n"
        "2\n00:00:00,113 --> 00:00:00,123\n新正文二\n"
    )

    cues, proof = build_retained_source_boundary_context(retained, reviewed)

    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in cues] == [
        (5, 10, "首部padding"),
        (20, 35, "新正文一"),
        (113, 123, "新正文二"),
        (173, 183, "尾部padding"),
    ]
    assert all(type(cue).__name__ == "SrtCue" for cue in cues)
    assert proof["fresh_review_required"] is True
    assert proof["prior_boundary_pass_inherited"] is False
    assert proof["dropped_source_cue_count"] == 1
    assert proof["replaced_source_cue_count"] == 2
    assert proof["padding_cue_count"] == 2


def test_source_boundary_projection_uses_parent_piece_start_for_physical_source_clock():
    retained = _source_boundary_projection_fixture()
    projection = retained["projection"]
    projection["source_timeline_offset_ms"] = 400
    projection["physical_source_range_ms"] = {"start_ms": 420, "end_ms": 580}
    for piece, start, end in zip(
        projection["piece_clock"], (400, 540), (500, 640), strict=True
    ):
        piece["source_start_ms"] = start
        piece["source_end_ms"] = end
    retained["source_boundary_context"]["parent_context_piece_start_ms"] = 400

    cues, proof = build_retained_source_boundary_context(
        retained,
        "1\n00:00:00,020 --> 00:00:00,035\n新正文一\n\n"
        "2\n00:00:00,113 --> 00:00:00,123\n新正文二\n",
    )

    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in cues] == [
        (5, 10, "首部padding"),
        (20, 35, "新正文一"),
        (113, 123, "新正文二"),
        (173, 183, "尾部padding"),
    ]
    assert proof["parent_context_piece_start_ms"] == 400


def test_retained_body_projection_reaches_normal_current_text_and_closure_consumers():
    from src.autoslice.producer_boundary_resolution import _current_delivery_closure_binding
    from src.autoslice.producer_source_boundary_review import _current_text_context

    reviewed = (
        "1\n00:00:00,020 --> 00:00:00,035\n新正文一\n\n"
        "2\n00:00:00,113 --> 00:00:00,123\n新正文二\n"
    )
    source_cues, proof = build_retained_source_boundary_context(
        _source_boundary_projection_fixture(), reviewed,
    )
    body_cues = parse_srt_cues(reviewed)
    projection = proof["current_delivery_source_projection"]
    _context, evidence = _current_text_context(source_cues, body_cues, projection)
    assert evidence["unmatched_current_cue_indexes"] == []
    terminal = next(cue for cue in source_cues if cue.text == "新正文二")
    text, binding = _current_delivery_closure_binding(
        closure_cue=terminal, snapped_end_ms=123,
        current_delivery_cues=body_cues, current_delivery_projection=projection,
        media_end_ms=200,
    )
    assert text == "新正文二"
    assert binding["projection_rows_verified"] is True
    assert projection["source_final_start_ms"] == 20
    assert projection["source_final_end_ms"] == 143  # measured second-piece clock
    assert projection["semantic_authority"] is False
    assert proof["status"] == "EVIDENCE_ONLY"

    projection["projected_source_cue_grid_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="SOURCE_REVIEW_CURRENT_TEXT_PROJECTION_DRIFT"):
        _current_text_context(source_cues, body_cues, projection)


def test_source_boundary_projection_rejects_crossing_gap_or_body_and_hash_drift():
    retained = _source_boundary_projection_fixture()
    reviewed = "1\n00:00:00,020 --> 00:00:00,035\n新正文一\n"

    crossing_gap = dict(retained)
    crossing_gap["source_boundary_context"]["source_srt_text"] = retained["source_boundary_context"]["source_srt_text"] + (
        "\n6\n00:00:00,095 --> 00:00:00,150\n跨删除缝\n"
    )
    crossing_gap["source_boundary_context"]["source_srt_sha256"] = psha(crossing_gap["source_boundary_context"]["source_srt_text"].encode())
    crossing_gap["source_boundary_context"]["source_cue_grid_sha256"] = cue_grid_sha256(parse_srt_cues(crossing_gap["source_boundary_context"]["source_srt_text"]))
    with pytest.raises(RetainedReviewedDraftError, match="SOURCE_BOUNDARY_CUE_CROSSES_PIECE_BOUNDARY"):
        build_retained_source_boundary_context(crossing_gap, reviewed)

    crossing_body = dict(retained)
    crossing_body["source_boundary_context"]["source_srt_text"] = (
        "1\n00:00:00,010 --> 00:00:00,025\n跨正文起点\n"
    )
    crossing_body["source_boundary_context"]["source_srt_sha256"] = psha(crossing_body["source_boundary_context"]["source_srt_text"].encode())
    crossing_body["source_boundary_context"]["source_cue_grid_sha256"] = cue_grid_sha256(parse_srt_cues(crossing_body["source_boundary_context"]["source_srt_text"]))
    with pytest.raises(RetainedReviewedDraftError, match="SOURCE_BOUNDARY_CUE_CROSSES_BODY_RANGE"):
        build_retained_source_boundary_context(crossing_body, reviewed)

    drifted = dict(retained)
    drifted["source_boundary_context"]["source_srt_sha256"] = "sha256:" + "d" * 64
    with pytest.raises(RetainedReviewedDraftError, match="SOURCE_BOUNDARY_SRT_HASH_MISMATCH"):
        build_retained_source_boundary_context(drifted, reviewed)


def test_source_boundary_projection_rejects_padding_past_measured_piece_end():
    retained = _source_boundary_projection_fixture()
    retained["projection"]["piece_clock"][1]["concat_end_ms"] = 180

    with pytest.raises(
        RetainedReviewedDraftError, match="SOURCE_BOUNDARY_CUE_EXCEEDS_MEASURED_PIECE"
    ):
        build_retained_source_boundary_context(
            retained,
            "1\n00:00:00,020 --> 00:00:00,035\n新正文一\n\n"
            "2\n00:00:00,113 --> 00:00:00,123\n新正文二\n",
        )


def test_source_boundary_projection_preserves_complete_edge_gap_context_cues():
    retained = _source_boundary_projection_fixture()
    context = retained["source_boundary_context"]
    source = context["source_srt_text"].replace(
        "00:00:00,005 --> 00:00:00,010", "00:00:00,010 --> 00:00:00,025"
    ).replace("00:00:00,210 --> 00:00:00,220", "00:00:00,170 --> 00:00:00,185")
    context.update(source_srt_text=source, source_srt_sha256=psha(source.encode()),
                   source_cue_grid_sha256=cue_grid_sha256(parse_srt_cues(source)))
    reviewed = (
        "1\n00:00:00,025 --> 00:00:00,035\n新正文一\n\n"
        "2\n00:00:00,113 --> 00:00:00,123\n新正文二\n"
    )
    original = json.dumps(retained, sort_keys=True)

    cues, proof = build_retained_source_boundary_context(retained, reviewed)

    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in cues] == [
        (10, 25, "首部padding"), (25, 35, "新正文一"),
        (113, 123, "新正文二"), (133, 148, "尾部padding"),
    ]
    assert proof["padding_cue_count"] == 2
    assert proof["fresh_review_required"] is True
    assert proof["prior_boundary_pass_inherited"] is False
    assert json.dumps(retained, sort_keys=True) == original


def test_source_boundary_projection_still_rejects_edge_context_overlapping_current_caption():
    retained = _source_boundary_projection_fixture()
    context = retained["source_boundary_context"]
    source = "1\n00:00:00,150 --> 00:00:00,185\n跨当前末句\n"
    context.update(source_srt_text=source, source_srt_sha256=psha(source.encode()),
                   source_cue_grid_sha256=cue_grid_sha256(parse_srt_cues(source)))
    with pytest.raises(RetainedReviewedDraftError, match="SOURCE_BOUNDARY_CUE_CROSSES_BODY_RANGE"):
        build_retained_source_boundary_context(
            retained, "1\n00:00:00,113 --> 00:00:00,123\n当前末句\n"
        )


def test_loader_returns_validated_source_boundary_projection_without_changing_body(tmp_path):
    spec, out, padded, _data = _fixture(tmp_path)
    authority_path = Path(spec["retained_current_text"]["parent_authority_path"])
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    source_text = Path(spec["retained_current_text"]["current_srt_path"]).read_text(encoding="utf-8")
    authority["post_transcript_entity_output_srt"] = source_text
    authority["post_transcript_entity_output_srt_sha256"] = psha(source_text.encode())
    authority_raw = json.dumps(authority, sort_keys=True, ensure_ascii=False).encode()
    authority_path.write_bytes(authority_raw)
    spec["retained_current_text"]["parent_authority_sha256"] = psha(authority_raw)
    source_context = {
        "schema_version": "source-boundary-transcript-context.v1",
        "source_srt_sha256": psha(source_text.encode()),
        "source_cue_grid_sha256": cue_grid_sha256(parse_srt_cues(source_text)),
    }

    def validator(**_kwargs):
        return {
            "status": "INPUT_VALIDATED",
            "source_timeline_offset_ms": 0,
            "source_endpoint": {"final_start_ms": 0, "final_end_ms": 200},
            "source_boundary_context": source_context,
        }

    result = _load(spec, out, padded, validator=validator)

    assert result["source_boundary_context"]["source_srt_sha256"] == psha(source_text.encode())
    assert result["source_boundary_projection"]["fresh_review_required"] is True
    assert result["source_boundary_projection"]["prior_boundary_pass_inherited"] is False
    assert result["srt_text"] == result["projection"]["srt_text"]
    assert (out / "candidate-1.retained-source-boundary.srt").read_text(encoding="utf-8") == source_text


def test_focused_parent_accepts_historical_block_and_derives_finite_source_offset(tmp_path):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)

    result = _validate_focused_parent(authority, context, config, padded_raw)

    assert result["status"] == "INPUT_VALIDATED"
    assert result["fresh_review_required"] is True
    assert result["historical_release_gate"] == "BLOCK"
    assert result["unresolved_finding_count"] == 1
    assert result["source_timeline_offset_ms"] == 425410
    assert result["source_endpoint"] == {
        "final_start_ms": 29950,
        "final_end_ms": 281400,
        "delivery_offset_ms": 29950,
    }


def test_ordinary_parent_without_continuation_disclosure_uses_exact_source_audit(tmp_path):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    authority["final_review_audit"].pop("source_boundary_context")
    before = json.dumps(authority, sort_keys=True)

    result = _validate_focused_parent(authority, context, config, padded_raw)

    assert result["status"] == "INPUT_VALIDATED"
    assert result["source_boundary_context"]["prior_boundary_pass_inherited"] is False
    assert result["source_boundary_context"]["fresh_review_required"] is True
    assert authority["final_review_audit"]["release_gate"] == "BLOCK"
    assert json.dumps(authority, sort_keys=True) == before


def test_retained_parent_consumes_explicit_original_source_grid_without_rebinding_authority(tmp_path):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    source_raw = authority["post_transcript_entity_output_srt"].encode()
    source_path = tmp_path / "original-source-boundary.srt"
    source_path.write_bytes(source_raw)
    config["source_boundary_srt_path"] = str(source_path)
    config["source_boundary_srt_sha256"] = psha(source_raw)
    # A later text repair changed authority's grid; the resolver's original
    # grid remains a distinct immutable input, never a new review verdict.
    later = authority["post_transcript_entity_output_srt"].replace("29,960", "29,961")
    authority["post_transcript_entity_output_srt"] = later
    authority["post_transcript_entity_output_srt_sha256"] = psha(later.encode())
    before = json.dumps(authority, sort_keys=True)

    result = _validate_focused_parent(authority, context, config, padded_raw)

    assert result["historical_release_gate"] == "BLOCK"
    assert result["fresh_review_required"] is True
    assert result["source_boundary_context"]["source_srt_sha256"] == psha(source_raw)
    assert result["source_boundary_context"]["source_srt_path"] == str(source_path)
    assert result["source_boundary_context"]["prior_boundary_pass_inherited"] is False
    assert json.dumps(authority, sort_keys=True) == before


def _retained_transcript_entity_row():
    # This is the normal source-stage writer's receipt, before delivery trim.
    return {
        "mode": "transcript_entity_only", "cue_indexes": [1],
        "matched_start_ms": 29960, "matched_end_ms": 29980,
        "before": ["保留文子"], "after": ["保留文字"],
        "transcript_surface": "文子", "transcript_canonical": "文字",
        "resolved_canonical": "文字", "expected_entity": "文字",
        "survived": True, "request_sha256": "a" * 64,
        "verdict": {
            "schema_version": "chat-entity-verdict.v1", "status": "RESOLVED",
            "canonical_entity": "文字", "request_sha256": "a" * 64,
            "authority_kind": "cpa_context_only_closed_set_adjudication",
            "decision_authority": "CPA_JUDGE", "witness_authority": "EVIDENCE_ONLY",
            "witness_status": "UNCERTAIN", "confidence": 0.95,
            "witness_request_sha256": "b" * 64,
            "judge_prompt_sha256": "c" * 64,
            "judge_completion_sha256": "d" * 64, "needs_audio": False,
        },
    }


def test_retained_parent_replays_normal_entity_verdict_in_original_source_clock(tmp_path):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    row = _retained_transcript_entity_row()
    authority["entity_repairs"] = [row]
    authority["transcript_entity_audit"] = {"repairs": [copy.deepcopy(row)]}
    original = json.dumps(authority, sort_keys=True)

    result = _validate_focused_parent(authority, context, config, padded_raw)

    assert result["status"] == "INPUT_VALIDATED"
    assert result["historical_release_gate"] == "BLOCK"
    assert result["fresh_review_required"] is True
    assert result["source_boundary_context"]["prior_boundary_pass_inherited"] is False
    assert "judge" not in row and "mutation_authority" not in row
    assert json.dumps(authority, sort_keys=True) == original


@pytest.mark.parametrize("drift", [
    "audit", "request", "schema", "status", "needs_audio", "canonical",
    "timing", "cue_index", "current", "extra_rewrite", "foreign_mode",
])
def test_retained_parent_entity_verdict_keeps_normal_binding_guards(tmp_path, drift):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    row = _retained_transcript_entity_row()
    if drift == "request":
        row["request_sha256"] = "e" * 64
    elif drift == "schema":
        row["verdict"]["schema_version"] = "acoustic-witness-adjudication.v1"
    elif drift == "status":
        row["verdict"]["status"] = "JUDGED"
    elif drift == "needs_audio":
        row["verdict"]["needs_audio"] = True
    elif drift == "canonical":
        row["resolved_canonical"] = "其他实体"
    elif drift == "timing":
        row["matched_start_ms"] = 10  # delivery clock cannot replace source clock
    elif drift == "cue_index":
        row["cue_indexes"] = [2]
    elif drift == "current":
        row["after"] = ["保留文字另加一句"]
    elif drift == "extra_rewrite":
        row["before"] = ["其他文子"]
    elif drift == "foreign_mode":
        row["mode"] = "context_audio_adjudication"
    authority["entity_repairs"] = [row]
    authority["transcript_entity_audit"] = {"repairs": [copy.deepcopy(row)]}
    if drift == "audit":
        authority["transcript_entity_audit"]["repairs"] = []
    if drift == "needs_audio":
        from src.autoslice.producer_text_pipeline import _validate_source_stage_transcript_entity_repair
        with pytest.raises(CurrentTextContinuationError, match="CURRENT_TRANSCRIPT_ENTITY_VERDICT_INVALID"):
            _validate_source_stage_transcript_entity_repair(row, authority=authority,
                srt_text=authority["post_transcript_entity_output_srt"])
        result = _validate_focused_parent(authority, context, config, padded_raw)
        assert result["status"] == "INPUT_VALIDATED_PENDING_REVIEW"
        assert len(result["pending_entity_reviews"]) == 1
        assert result["pending_entity_reviews"][0]["release_authorized"] is False
        assert row["verdict"]["needs_audio"] is True
        return
    reason = "CURRENT_ENTITY_REPAIR_JUDGE_MISSING" if drift == "foreign_mode" else "CURRENT_TRANSCRIPT_ENTITY"
    with pytest.raises(CurrentTextContinuationError, match=reason):
        _validate_focused_parent(authority, context, config, padded_raw)


@pytest.mark.parametrize("mutation", ["missing_hash", "byte_drift", "different_grid"])
def test_retained_parent_explicit_source_artifact_still_requires_original_binding(tmp_path, mutation):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    source_raw = authority["post_transcript_entity_output_srt"].encode()
    source_path = tmp_path / "original-source-boundary.srt"
    source_path.write_bytes(source_raw)
    config["source_boundary_srt_path"] = str(source_path)
    config["source_boundary_srt_sha256"] = psha(source_raw)
    if mutation == "missing_hash":
        config.pop("source_boundary_srt_sha256")
        expected = "CURRENT_SOURCE_BOUNDARY_ARTIFACT_PAIR_INVALID"
    else:
        changed = source_raw.replace(b"29,960", b"29,961")
        source_path.write_bytes(changed)
        expected = "CURRENT_SOURCE_BOUNDARY_ARTIFACT_DRIFT"
        if mutation == "different_grid":
            config["source_boundary_srt_sha256"] = psha(changed)
            expected = "CURRENT_SOURCE_BOUNDARY_GRID_DRIFT"
    with pytest.raises(CurrentTextContinuationError, match=expected):
        _validate_focused_parent(authority, context, config, padded_raw)


@pytest.mark.parametrize("mutation", ["grid", "request", "scope", "owners"])
def test_rehashed_source_audit_cannot_substitute_another_grid_request_or_owner_scope(tmp_path, mutation):
    authority, context, config, audit, audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    authority["final_review_audit"].pop("source_boundary_context")
    review = audit["boundary_semantic_review"]
    if mutation == "grid":
        review["cue_grid_sha256"] = "sha256:" + "d" * 64
    elif mutation == "request":
        review["final_endpoint_binding"]["semantic_request_sha256"] = "sha256:" + "d" * 64
    elif mutation == "scope":
        review["boundary_search_scope"] = {**review["boundary_search_scope"], "review_target_ms": 281401}
    else:
        audit["frozen_required_boundary_owners"] = [{"owner_id": "substitute"}]
    raw = json.dumps(audit, ensure_ascii=False, sort_keys=True).encode()
    audit_path.write_bytes(raw)
    config["source_boundary_audit_sha256"] = psha(raw)

    with pytest.raises(CurrentTextContinuationError, match="CURRENT_SOURCE_BOUNDARY_(GRID|OWNER_SCOPE)_DRIFT"):
        _validate_focused_parent(authority, context, config, padded_raw)


def test_outer_source_context_byte_binding_is_checked(tmp_path):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    authority["final_review_audit"]["source_boundary_context"]["source_srt_sha256"] = "sha256:" + "d" * 64
    with pytest.raises(CurrentTextContinuationError, match="CURRENT_SOURCE_BOUNDARY_CONTEXT_DRIFT"):
        _validate_focused_parent(authority, context, config, padded_raw)


def test_missing_external_audit_binding_still_blocks_ordinary_parent(tmp_path):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    authority["final_review_audit"].pop("source_boundary_context")
    config.pop("source_boundary_audit_sha256")
    with pytest.raises(CurrentTextContinuationError, match="CURRENT_SOURCE_BOUNDARY_AUDIT_BINDING_MISSING"):
        _validate_focused_parent(authority, context, config, padded_raw)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("authority_srt", "FINAL_REVIEW_SRT_BINDING_MISMATCH"),
        ("current_srt", "CURRENT_FINAL_OUTPUT_SRT_BINDING_MISMATCH"),
        ("source", "CURRENT_PARENT_SOURCE_HASHES_DRIFT"),
        ("offset", "CURRENT_SOURCE_BOUNDARY_OFFSET_DRIFT"),
        ("context_range", "CURRENT_SOURCE_BOUNDARY_CONTEXT_RANGE_DRIFT"),
    ],
)
def test_focused_parent_rejects_mutated_history_current_source_or_offset(
    tmp_path, mutation, expected
):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    srt_text = "1\n00:00:00,010 --> 00:00:00,030\n保留文字\n"

    if mutation == "authority_srt":
        authority["final_review_audit"]["reviewed_srt_sha256"] = psha(b"different parent bytes")
    elif mutation == "current_srt":
        srt_text = "1\n00:00:00,010 --> 00:00:00,030\n漂移文字\n"
    elif mutation == "source":
        context["pieces"][0]["source_media_sha256"] = "sha256:" + "b" * 64
    elif mutation == "offset":
        config["source_boundary_delivery_offset_ms"] = 29951
    elif mutation == "context_range":
        context["pieces"][0]["end_ms"] = 595460

    with pytest.raises(CurrentTextContinuationError) as caught:
        _validate_focused_parent(authority, context, config, padded_raw, srt_text=srt_text)
    assert expected in str(caught.value)


def test_focused_parent_rejects_audit_byte_drift_without_rebinding(tmp_path):
    authority, context, config, _audit, audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    audit_path.write_text("{\"schema_version\":\"boundary-audit.v1\"}", encoding="utf-8")

    with pytest.raises(CurrentTextContinuationError, match="CURRENT_SOURCE_BOUNDARY_AUDIT_DRIFT"):
        _validate_focused_parent(authority, context, config, padded_raw)


def test_focused_parent_uses_old_materialization_clock_not_new_projection_duration(tmp_path):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)

    result = _validate_focused_parent(
        authority, context, config, padded_raw, new_padded_duration_ms=200
    )

    assert result["parent_materialization_sha256"] == config["parent_materialization_sha256"]
    assert result["parent_materialization_duration_ms"] == 326540
    assert result["new_padded_sha256"] == psha(padded_raw)


def test_focused_parent_rejects_new_hash_when_it_is_used_for_old_clock_evidence(tmp_path):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    config["parent_materialization_sha256"] = psha(padded_raw)

    with pytest.raises(CurrentTextContinuationError, match="CURRENT_SOURCE_BOUNDARY_OFFSET_EVIDENCE_MISSING"):
        _validate_focused_parent(authority, context, config, padded_raw)


@pytest.mark.parametrize("mutation", [None, "media", "offset", "target", "crop", "sibling"])
def test_normal_acoustic_timeline_is_consumed_with_its_own_media_binding(tmp_path, mutation):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    media_sha = authority.pop("source_media_sha256")
    offset = authority.pop("source_media_timeline_offset_ms")
    witness = {
        "source_media_sha256": media_sha,
        "audio_start_ms": 29950, "audio_end_ms": 30050,
        "timeline_binding": {
            "schema_version": "subtitle-audio-timeline-binding.v1",
            "source_media_timeline_offset_ms": offset,
            "delivery_local": {"target_start_ms": 10, "target_end_ms": 30},
            "source_media": {"target_start_ms": 29960, "target_end_ms": 29980,
                             "crop_start_ms": 29950, "crop_end_ms": 30050},
        },
    }
    authority["existing_audio_evidence"] = witness
    if mutation == "media":
        witness["source_media_sha256"] = "d" * 64
    elif mutation == "offset":
        witness["timeline_binding"]["source_media_timeline_offset_ms"] += 1
    elif mutation == "target":
        witness["timeline_binding"]["source_media"]["target_end_ms"] += 1
    elif mutation == "crop":
        witness["audio_end_ms"] -= 1
    elif mutation == "sibling":
        authority["unrelated_media"] = {"source_media_sha256": witness.pop("source_media_sha256")}

    if mutation is None:
        result = _validate_focused_parent(authority, context, config, padded_raw)
        assert result["status"] == "INPUT_VALIDATED"
        assert result["historical_release_gate"] == "BLOCK"
    else:
        with pytest.raises(CurrentTextContinuationError, match="CURRENT_SOURCE_BOUNDARY_OFFSET_EVIDENCE_MISSING"):
            _validate_focused_parent(authority, context, config, padded_raw)


def test_focused_parent_requires_terminal_current_srt_binding(tmp_path):
    authority, context, config, _audit, _audit_path, padded_raw = _focused_parent_fixture(tmp_path)
    authority.pop("final_output_srt_sha256")

    with pytest.raises(CurrentTextContinuationError, match="CURRENT_FINAL_OUTPUT_SRT_BINDING_MISSING"):
        _validate_focused_parent(authority, context, config, padded_raw)


def test_retained_parent_cpa_usage_seed_preserves_count_without_release_fields():
    usage = {
        "schema_version": "cpa-adjudication-usage.v1",
        "provider_adjudication_count": 320,
        "soft_limit": 480,
        "policy": "SOFT",
    }
    seeded = _retained_parent_cpa_usage_seed(
        {"cpa_adjudication_usage": usage, "release_gate": "BLOCK"}
    )

    assert seeded == usage
    assert "release_gate" not in seeded


@pytest.mark.parametrize("count", [True, -1, 1.5, "320", None])
def test_retained_parent_cpa_usage_seed_rejects_invalid_count(count):
    with pytest.raises(CurrentTextContinuationError, match="CURRENT_PARENT_CPA_USAGE_INVALID"):
        _retained_parent_cpa_usage_seed(
            {"cpa_adjudication_usage": {"provider_adjudication_count": count}}
        )


def test_retained_seed_skips_initial_transcriber_and_witness(monkeypatch, tmp_path):
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"media")
    calls = []

    def forbidden_builder(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("retained seed must not build initial ASR")

    monkeypatch.setattr(
        "src.autoslice.producer_text_pipeline.build_ssh_silero_vad_provider",
        lambda host: lambda path, start, end: [SimpleNamespace(start_ms=0, end_ms=20)],
    )
    adapters = TextPipelineAdapters(
        build_aggregate_transcriber=forbidden_builder,
        build_agy_transcriber=forbidden_builder,
        load_term_boundary_surfaces=lambda spec: [],
        profile_asset_file=lambda name: tmp_path / f"{name}.json",
        review_glossary=lambda: "",
        topic_graph_disabled=lambda: True,
        topic_graph_path=lambda: tmp_path / "graph.json",
        topic_graph_expected_sha256=lambda: "sha256:" + "0" * 64,
    )
    retained = "1\n00:00:00,000 --> 00:00:00,020\n已审文字\n"
    draft = _transcribe_draft(
        spec={"pieces": [], "song_name_candidates": []},
        padded=padded,
        padded_dur=20,
        host="localhost",
        substrate="aggregate_asr",
        correct="none",
        screen_text=False,
        merged=[],
        adapters=adapters,
        retained_srt_text=retained,
    )

    assert draft.srt_text == retained
    assert draft.source_language_witness_srt == ""
    assert draft.spans[0].start_ms == 0
    assert calls == []


def test_future_native_entity_writer_save_restart_uses_one_format(tmp_path):
    from src.autoslice.chat_repair import apply_audio_entity_verification
    from src.autoslice.chat_evidence import ReferentEntity, ReferentGroup
    from src.autoslice.transcript_entity_contract import SCHEMA
    from src.autoslice.producer_text_pipeline import _validate_source_stage_transcript_entity_repair

    source = '1\n00:00:29,960 --> 00:00:29,980\n保留文子\n'
    def verifier(request):
        verdict = _retained_transcript_entity_row()['verdict']
        return {**verdict, 'request_sha256': request['request_sha256']}
    output, audit = apply_audio_entity_verification(
        source, referent_groups=[ReferentGroup((ReferentEntity('文字', ('文子',)),),
                                              positions=('transcript_only',))], entity_verifier=verifier)
    assert '保留文字' in output
    assert len(audit['repairs']) == 1
    saved = tmp_path / 'source-audit.json'; saved.write_text(json.dumps(audit, ensure_ascii=False))
    restored = json.loads(saved.read_text())
    row = restored['repairs'][0]
    assert row['schema_version'] == SCHEMA
    assert row['cue_binding']['coordinate_space'] == 'transcript-entity-input-srt'
    original = saved.read_bytes()
    _validate_source_stage_transcript_entity_repair(row, authority={'transcript_entity_audit': restored}, srt_text=output)
    assert saved.read_bytes() == original
    for key, value in [('coordinate_space', 'delivery'), ('before_sha256', 'e' * 64)]:
        changed = copy.deepcopy(row); changed['cue_binding'][key] = value
        with pytest.raises(CurrentTextContinuationError, match='FORMAT_BINDING'):
            _validate_source_stage_transcript_entity_repair(changed, authority={'transcript_entity_audit': {'repairs': [changed]}}, srt_text=output)
