"""Replay completed retained preparation as an input to ordinary production.

The original review receipt remains immutable. This consumer neither supplies
a language witness nor grants source-finalization, boundary or release success.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

SCHEMA = "retained-entity-current-review.v1"
RETAINED_PARTIAL_SCHEMA = "producer-retained-partial-source-text-checkpoint.v1"


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _proof_identity(proof: Mapping) -> dict:
    # Paths in history name the consumer's immutable copies. All original
    # source/parent/cut identities and the exact typed nomination stay bound.
    return {key: copy.deepcopy(value) for key, value in proof.items()
            if key != "history_paths"}


def retained_history_materialization_paths(path: Path) -> tuple[Path, Path]:
    store = path.parent / ".retained-input-materializations" / path.name
    return store / "intent.json", store / "receipt.json"


def _content_receipt(value):
    """A materialization receipt with every file row reduced to its content."""
    from src.autoslice.evidence_identity import content_view
    if isinstance(value, Mapping):
        if isinstance(value.get("stat"), Mapping) and "sha256" in value and "path" in value:
            return content_view(value)
        return {key: _content_receipt(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_content_receipt(item) for item in value]
    return value


def replay_retained_history_copy(path: Path, existing: bytes, requested: bytes) -> bool:
    """Bind immutable retained input copies without rewriting their old proof.

    A sole normal writer can emit the same input in a new output directory.
    Only independently copied history locators may differ; every other source,
    parent, clock, projection and pending-review field must remain identical.
    """
    from src.autoslice.retained_reviewed_draft import _SCHEMA
    from src.autoslice.media_copy_provenance import file_snapshot
    from src.autoslice.argon_escalation_storage import directory, put_json, read

    old, new = json.loads(existing), json.loads(requested)
    if (not isinstance(old, Mapping) or not isinstance(new, Mapping)
            or old.get("schema_version") != _SCHEMA or new.get("schema_version") != _SCHEMA
            or _proof_identity(old) != _proof_identity(new)):
        return False
    prior, current = old.get("history_paths"), new.get("history_paths")
    if (not isinstance(prior, Mapping) or not prior or not isinstance(current, Mapping)
            or set(prior) != set(current) or prior == current):
        return False
    origin_roots = {Path(str(value)).parent for value in prior.values()}
    if len(origin_roots) != 1:
        return False
    origin_root = next(iter(origin_roots))
    if origin_root == path.parent:
        return False
    origin = origin_root / path.name
    if origin.read_bytes() != existing or path.read_bytes() != existing:
        return False
    source_input = file_snapshot(origin, _sha(existing).removeprefix("sha256:"))
    output_input = file_snapshot(path, _sha(existing).removeprefix("sha256:"))
    pairs = []
    for key in sorted(prior):
        source, output = Path(str(prior[key])), Path(str(current[key]))
        if (output.parent != path.parent or source.name != output.name
                or source == output):
            return False
        a, b = file_snapshot(source), file_snapshot(output)
        if (a["sha256"] != b["sha256"] or b["stat"]["st_nlink"] != 1
                or (a["stat"]["st_dev"], a["stat"]["st_ino"])
                == (b["stat"]["st_dev"], b["stat"]["st_ino"])):
            return False
        pairs.append({"key":key,"source":a,"output":b})
    if (output_input["stat"]["st_nlink"] != 1
            or (source_input["stat"]["st_dev"], source_input["stat"]["st_ino"])
            == (output_input["stat"]["st_dev"], output_input["stat"]["st_ino"])):
        return False
    receipt = {
        "schema_version":"retained-input-independent-copy.v1",
        "operation":"INDEPENDENT_BYTE_COPY", "source_input":source_input,
        "output_input":output_input, "history":pairs,
        "source_proof_sha256":_sha(existing), "consumer_proof_sha256":_sha(requested),
        "provider_observation":False, "release_authorized":False,
    }
    # Normal output directories need not be private. Keep this new receipt in
    # a private child store without changing any existing directory's mode.
    intent_path, receipt_path = retained_history_materialization_paths(path)
    # Each native snapshot protects its own read. Recheck the complete set so
    # an earlier file cannot drift while a later history member is inspected.
    for row in [source_input, output_input, *(item[side] for item in pairs
                                             for side in ("source", "output"))]:
        if file_snapshot(Path(row["path"]), row["sha256"]) != row:
            raise ValueError("RETAINED_INPUT_CHANGED_DURING_MATERIALIZATION")
    intent = {**receipt, "stage": "MATERIALIZATION_STARTED", "status": "PREPARING"}
    if receipt_path.exists() or receipt_path.is_symlink():
        # The stored receipt binds content; the files' current placement was
        # checked above (independent copies, nlink 1). A byte-identical restore
        # changes inode/ctime and is still the same evidence.
        if (_content_receipt(json.loads(read(intent_path))) != _content_receipt(intent)
                or _content_receipt(json.loads(read(receipt_path))) != _content_receipt(receipt)):
            raise ValueError("RETAINED_INPUT_MATERIALIZATION_DRIFT")
    else:
        # Reserve this consumer once. An interrupted or damaged existing store
        # cannot silently re-certify changed files as a first materialization.
        directory(receipt_path.parent.parent)
        try:
            receipt_path.parent.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ValueError("MATERIALIZED_COPY_INCOMPLETE_OR_UNPROVEN:retained_history") from exc
        put_json(intent_path, intent)
        put_json(receipt_path, receipt)
    return True


def retained_attempt_present(out_root: Path, cid: str) -> bool:
    return (any(out_root.glob(f"{cid}.retained-entity-review.*.json"))
            or (out_root / f"{cid}.retained-entity-review-input.json").exists())


def _validate_completed_normal_repair(
    *, finding: Mapping, adjudication: Mapping, request: Mapping,
    cue, srt_text: str, clip_context: Mapping,
) -> None:
    """Replay an ordinary saved decision, including direct and rebuilt proposals."""
    from src.autoslice import acoustic_witness_adjudication as acoustic
    from src.autoslice.exact_final_witness_authority import binding_from_adjudication
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from src.autoslice.llm_client import extract_json_object

    # The native binding rechecks the complete original request, witness and
    # typed judge hashes. Keep its historical inventories and evidence intact.
    if binding_from_adjudication(adjudication) is None:
        raise ValueError("RETAINED_PREPARATION_TYPED_ACOUSTIC_BINDING_INVALID")
    parser = getattr(acoustic, "parse_judge_choice", None)
    if not callable(parser):
        raise ValueError("RETAINED_PREPARATION_PENDING_ENTITY_CONTRACT_REQUIRED")
    judge = adjudication["witness_judge"]["judge"]
    raw = judge.get("span_review_raw_completion")
    if (not isinstance(raw, str)
            or _sha(raw.encode()).removeprefix("sha256:")
            != str(judge.get("completion_sha256") or "").removeprefix("sha256:")):
        raise ValueError("RETAINED_PREPARATION_TYPED_RAW_RESPONSE_DRIFT")
    payload = extract_json_object(raw)
    choice, ranking = parser(payload, set(judge.get("choice_set") or []))
    if (choice != judge.get("choice") or ranking != (judge.get("ranking") or [])
            or payload.get("needs_audio") is not False
            or judge.get("needs_audio") is not False
            or (judge.get("candidate_id") is not None
                and payload.get("candidate_id", judge["candidate_id"]) != judge["candidate_id"])):
        raise ValueError("RETAINED_PREPARATION_TYPED_RAW_DECISION_DRIFT")
    inputs = {**dict(finding),
              **({"_caption_audio_roles": request["caption_audio_roles"]}
                 if "caption_audio_roles" in request else {}),
              **({"_prior_range_acoustic_observations": request["context_only_range_acoustic_observations"]}
                 if "context_only_range_acoustic_observations" in request else {})}
    # Retained preparation is padded-local; it cannot inherit a delivery-local
    # offset merely because that number is recorded in a request.
    rebuilt = build_context_adjudication_request(
        srt_text, inputs, clip_context=clip_context,
        source_media_timeline_offset_ms=0,
        require_complete_utterance_support=request.get("require_complete_utterance_support") is True)
    current_keys = (
        "schema_version", "kind", "repair_class", "cue_indexes", "matched_start_ms",
        "matched_end_ms", "context_start_ms", "context_end_ms",
        "source_media_timeline_offset_ms", "clip_context_binding", "current_cue",
        "whole_clip_current_srt", "base_text_sha256", "span_start_codepoint",
        "span_end_codepoint", "suspect", "replacement", "proposed_cue",
        "require_complete_utterance_support",
    )
    if (any(rebuilt.get(key) != request.get(key) for key in current_keys)
            or request.get("current_cue") != cue.text
            or request.get("whole_clip_current_srt") != srt_text
            or request.get("matched_start_ms") != cue.start_ms
            or request.get("matched_end_ms") != cue.end_ms):
        raise ValueError("RETAINED_PREPARATION_TYPED_SOURCE_CLOCK_CANDIDATE_DRIFT")


def load_completed_retained_review(
    *, retained: Mapping, out_root: Path, cid: str,
    clip_context: Mapping, required: bool, spec: Mapping, durations: list[int],
    padded: Path, padded_dur: int,
) -> dict | None:
    """Read an existing normal receipt before any provider-capable preparation.

    The pending-entity validator is the deployed normal contract. A checkout
    predating that contract cannot reinterpret its receipts as completed work.
    """
    from src.autoslice import retained_reviewed_draft as retained_reader
    from src.autoslice.producer_text_pipeline import _replay_saved_correction_review

    paths = sorted(out_root.glob(f"{cid}.retained-entity-review.*.json"))
    if not paths:
        if required:
            raise ValueError("RETAINED_PREPARATION_COMPLETION_MISSING_OR_UNKNOWN")
        return None
    validator = getattr(retained_reader, "validate_pending_entity_checkpoint", None)
    if not callable(validator):
        raise ValueError("RETAINED_PREPARATION_PENDING_ENTITY_CONTRACT_REQUIRED")
    from src.autoslice.clip_context import validate_clip_context
    from src.autoslice.producer_text_pipeline import (
        _validate_current_source_context, _continuation_read_json,
        _continuation_sha256_path, _continuation_require_sha,
    )
    _continuation_require_sha(_continuation_sha256_path(padded, label="RETAINED_PREPARATION_PADDED"),
                              retained.get("padded_sha256"), label="RETAINED_PREPARATION_PADDED")
    clip_context = validate_clip_context(
        clip_context, candidate_id=cid, recording_date=str(spec.get("date") or "") or None,
        source_media_sha256s=retained["source_media_sha256s"])
    padded_proof, _ = _continuation_read_json(
        padded.with_suffix(".provenance.json"), label="RETAINED_PREPARATION_PADDED_PROVENANCE")
    _validate_current_source_context(
        spec=spec, durations=durations, padded_dur=padded_dur,
        srt_text=retained["srt_text"], clip_context=clip_context,
        piece_media_paths=[Path(row["path"]) for row in padded_proof["inputs"]],
        padded_media_path=padded)
    proof = retained.get("proof")
    if not isinstance(proof, Mapping) or proof.get("candidate_id") != cid:
        raise ValueError("RETAINED_PREPARATION_PARENT_INVALID")
    _raw, original_proof = retained_reader._read_local_json(
        out_root / f"{cid}.retained-entity-review-input.json",
        label="PREPARATION_INPUT")
    if _proof_identity(original_proof) != _proof_identity(proof):
        raise ValueError("RETAINED_PREPARATION_SOURCE_PARENT_CLOCK_DRIFT")
    if original_proof.get("history_paths") != proof.get("history_paths"):
        if not replay_retained_history_copy(
                out_root / f"{cid}.retained-entity-review-input.json", _raw,
                json.dumps(dict(proof), ensure_ascii=False, sort_keys=True, indent=2).encode()):
            raise ValueError("RETAINED_PREPARATION_HISTORY_COPY_INVALID")
    matched = None
    for path in paths:
        raw, saved = retained_reader._read_local_json(path, label="PREPARATION_COMPLETION")
        digest = _sha(raw)
        if path.name != f"{cid}.retained-entity-review.{digest.removeprefix('sha256:')}.json":
            raise ValueError("RETAINED_PREPARATION_COMPLETION_HASH_DRIFT")
        if (not isinstance(saved, Mapping) or saved.get("schema_version") != SCHEMA
                or saved.get("candidate_id") != cid
                or saved.get("parent_authority_sha256") != proof.get("parent_authority_sha256")
                or saved.get("mutation_authorized") is not False
                or saved.get("release_authorized") is not False):
            raise ValueError("RETAINED_PREPARATION_COMPLETION_BINDING_DRIFT")
        if saved.get("status") != "CURRENT_REVIEW_COMPLETED" or saved.get("pending") != []:
            raise ValueError("RETAINED_PREPARATION_REVIEW_INCOMPLETE_OR_UNKNOWN")
        before, after = saved.get("input_srt_text"), saved.get("output_srt_text")
        if (not isinstance(before, str) or not before or not isinstance(after, str) or not after
                or _sha(before.encode()) != saved.get("input_srt_sha256")
                or _sha(after.encode()) != saved.get("output_srt_sha256")
                or before != retained.get("srt_text")):
            raise ValueError("RETAINED_PREPARATION_COMPLETION_TEXT_DRIFT")
        pointer = {
            "status": saved["status"], "path": str(path), "sha256": digest,
            "parent_authority_sha256": proof["parent_authority_sha256"],
            "mutation_authorized": False, "release_authorized": False,
        }
        # This is a transient reader argument, never a fabricated old audit.
        validator(retained, {"chat_authority": {
            "retained_reviewed_draft": proof, "retained_entity_review": pointer}})
        review = saved.get("normal_correction_review")
        if not isinstance(review, Mapping) or review.get("schema_version") != "final-review-audit.v1":
            raise ValueError("RETAINED_PREPARATION_REVIEW_SCHEMA_INVALID")
        if review.get("status") not in {"APPLIED", "PARTIAL", "FLAGGED", "CLEAN"}:
            raise ValueError("RETAINED_PREPARATION_REVIEW_INCOMPLETE_OR_UNKNOWN")
        from src.autoslice.jingting_chunker import parse_srt_cues
        left, right = parse_srt_cues(before), parse_srt_cues(after)
        if ([(c.index, c.start_ms, c.end_ms) for c in left]
                != [(c.index, c.start_ms, c.end_ms) for c in right]):
            raise ValueError("RETAINED_PREPARATION_CUE_GRID_DRIFT")
        from src.autoslice.final_review_auditor import audit_correction_mutation_authority
        if audit_correction_mutation_authority(review).get("status") != "PASS":
            raise ValueError("RETAINED_PREPARATION_TYPED_MUTATION_AUTHORITY_INVALID")
        owners = _replay_saved_correction_review(
            input_srt_text=before, output_srt_text=after,
            review=review, clip_context=clip_context,
            validate_normal_repair=_validate_completed_normal_repair)
        from src.autoslice.producer_text_pipeline import _retained_parent_cpa_usage_seed
        prior = _retained_parent_cpa_usage_seed(retained["parent_authority"])["provider_adjudication_count"]
        usage = review.get("cpa_adjudication_usage") or {}
        count = review.get("context_adjudication_count")
        if (type(count) is not int or count < 0
                or usage.get("prior_provider_adjudication_count") != prior
                or usage.get("provider_adjudication_count") != prior + count):
            raise ValueError("RETAINED_PREPARATION_CUMULATIVE_USAGE_DRIFT")
        result = {"input_srt_text": before, "output_srt_text": after,
                  "review": copy.deepcopy(dict(review)), "owners": owners,
                  "completion": pointer, "release_authorized": False,
                  "clip_context_binding": {"candidate_id": cid,
                                           "context_sha256": clip_context["context_sha256"]},
                  "provider_calls_this_replay": 0}
        if matched is not None and result != matched:
            raise ValueError("RETAINED_PREPARATION_COMPLETION_AMBIGUOUS")
        matched = result
    return matched


def validate_completed_retained_context(completed: Mapping, clip_context: Mapping) -> None:
    """Keep replayed typed owners bound to the current producer context."""
    from src.autoslice.clip_context import validate_clip_context
    binding = completed["clip_context_binding"]
    current = validate_clip_context(clip_context, candidate_id=binding["candidate_id"])
    if current["context_sha256"] != binding["context_sha256"]:
        raise ValueError("RETAINED_PREPARATION_CURRENT_CONTEXT_DRIFT")


REPLAYED_INPUT_STATUS = "REPLAYED_COMPLETED_RETAINED_REVIEW"


def _completed_input_binding(completed: Mapping) -> dict:
    def structured_sha(value):
        return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode())
    return {
        "schema_version": "completed-retained-review-input.v1",
        "role": "RESTORED_REVIEWED_INPUT",
        "completion": copy.deepcopy(completed["completion"]),
        "clip_context_binding": copy.deepcopy(completed["clip_context_binding"]),
        "input_srt_sha256": _sha(completed["input_srt_text"].encode()),
        "output_srt_sha256": _sha(completed["output_srt_text"].encode()),
        "typed_owners_sha256": structured_sha(completed["owners"]),
        "review_sha256": structured_sha(completed["review"]),
        "pre_entity_arbitration": "NOT_REEXECUTED",
        "provider_calls_this_replay": 0, "release_authorized": False,
    }


def replayed_input_present(authority: Mapping) -> bool:
    """Select the input contract independently of a mutable outer status."""
    transcript = authority.get("transcript_entity_audit")
    return ("retained_preparation_input" in authority
            or authority.get("status") == REPLAYED_INPUT_STATUS
            or isinstance(transcript, Mapping)
            and transcript.get("schema_version") == "transcript-entity-input-replay.v1")


def _validate_completed_input_text(completed: Mapping, authority: Mapping) -> None:
    """Prove the entire restored text, not merely five surviving owner rows.

    Additional native effects must retain the original owners and replay on
    their actual input generation. Unsupported transformations fail closed;
    their saved phase and original observations remain available for a normal
    consumer with that transformation's own replay contract.
    """
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.producer_text_pipeline import _render_cues_to_srt, SrtCue
    from src.autoslice.producer_text_checkpoint import load_post_transcript_text

    owners = authority["entity_repairs"]
    encoded = [json.dumps(row, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) for row in owners]
    if len(encoded) != len(set(encoded)):
        raise ValueError("RETAINED_PREPARATION_INPUT_REPLAY_DUPLICATE_OWNER")
    current = completed["output_srt_text"]
    for row in owners[len(completed["owners"]):]:
        if not isinstance(row, Mapping):
            raise ValueError("RETAINED_PREPARATION_INPUT_REPLAY_TRANSITION_INVALID")
        cues = parse_srt_cues(current)
        indexes = [i for i, cue in enumerate(cues)
                   if (cue.start_ms, cue.end_ms) ==
                   (row.get("matched_start_ms"), row.get("matched_end_ms"))]
        before, after = row.get("before"), row.get("after")
        if (len(indexes) != 1 or not isinstance(before, list) or len(before) != 1
                or not isinstance(after, list) or len(after) != 1
                or not isinstance(after[0], str) or before == after
                or before != [cues[indexes[0]].text]
                or row.get("same_cue_mutation_chain")):
            raise ValueError("RETAINED_PREPARATION_INPUT_REPLAY_TRANSITION_INVALID")
        index = indexes[0]
        cue = cues[index]
        if row.get("action") == "DROP_CUE":
            if after != [""]:
                raise ValueError("RETAINED_PREPARATION_INPUT_REPLAY_TRANSITION_INVALID")
            del cues[index]
        else:
            cues[index] = SrtCue(cue.index, cue.start_ms, cue.end_ms, after[0])
        current = _render_cues_to_srt(cues)
    if load_post_transcript_text(authority) != current:
        raise ValueError("RETAINED_PREPARATION_INPUT_REPLAY_UNPROVEN_TEXT_TRANSITION")


def validate_partial_predecessors(*, native: Mapping, authority: Mapping) -> None:
    """Read actual native phase ancestry and preserve already persisted usage."""
    from src.autoslice.retained_reviewed_draft import _read_local_json
    from src.autoslice.producer_text_pipeline import _retained_parent_cpa_usage_seed
    from src.autoslice.native_audio_budget_receipt import (
        _require_budget_history_extension, _validate_existing)

    history = native.get("preparation_history")
    if history is None:
        if replayed_input_present(authority):
            raise ValueError("RETAINED_PREPARATION_PHASE_HISTORY_MISSING")
        return  # An unchanged legacy pointer has no invented ancestry.
    if (not isinstance(history, Mapping)
            or history.get("schema_version") != "producer-preparation-history.v1"
            or "previous_checkpoint" not in history
            or "restored_input_origin" not in history):
        raise ValueError("RETAINED_PREPARATION_PHASE_HISTORY_INVALID")
    origin = authority.get("retained_preparation_input")
    if history["restored_input_origin"] != origin:
        raise ValueError("RETAINED_PREPARATION_PHASE_ORIGIN_DRIFT")
    current_usage = _retained_parent_cpa_usage_seed(authority)["provider_adjudication_count"]

    def artifact(pointer, key):
        binding = pointer.get(key)
        if not isinstance(binding, Mapping):
            raise ValueError("RETAINED_PREPARATION_PHASE_ARTIFACT_MISSING")
        raw, payload = _read_local_json(Path(str(binding.get("path") or "")),
                                       label="PREPARATION_PHASE_" + key.upper())
        if not isinstance(payload, Mapping) or _sha(raw) != binding.get("sha256"):
            raise ValueError("RETAINED_PREPARATION_PHASE_ARTIFACT_DRIFT")
        return payload

    current_budget = artifact(native, "budget") if "budget" in native else None
    media_sha = str(native.get("padded", {}).get("sha256") or "").removeprefix("sha256:")
    seen = set()
    pointer = native
    while history is not None:
        if (not isinstance(history, Mapping)
                or history.get("schema_version") != "producer-preparation-history.v1"
                or "previous_checkpoint" not in history):
            raise ValueError("RETAINED_PREPARATION_PHASE_HISTORY_INVALID")
        previous = history["previous_checkpoint"]
        if previous is None:
            break
        if (not isinstance(previous, Mapping)
                or previous.get("role") != "PREVIOUS_NORMAL_PREPARATION_CHECKPOINT"):
            raise ValueError("RETAINED_PREPARATION_PHASE_PARENT_INVALID")
        digest = previous.get("sha256")
        if not isinstance(digest, str) or digest in seen or len(seen) >= 1024:
            raise ValueError("RETAINED_PREPARATION_PHASE_CYCLE_OR_LIMIT")
        seen.add(digest)
        raw, prior = _read_local_json(Path(str(previous.get("path") or "")),
                                      label="PREPARATION_PREVIOUS_CHECKPOINT")
        if (not isinstance(prior, Mapping)
                or prior.get("schema_version") != native.get("schema_version")
                or _sha(raw) != digest or any(prior.get(key) != native.get(key) for key in (
                "candidate_id", "time_domain", "padded"))):
            raise ValueError("RETAINED_PREPARATION_PHASE_PARENT_DRIFT")
        prior_authority = artifact(prior, "authority")
        if (prior_authority.get("retained_reviewed_draft") != authority.get("retained_reviewed_draft")
                or current_usage < _retained_parent_cpa_usage_seed(
                    prior_authority)["provider_adjudication_count"]):
            raise ValueError("RETAINED_PREPARATION_PHASE_PARENT_OR_USAGE_DRIFT")
        if "budget" in prior:
            if current_budget is None:
                raise ValueError("RETAINED_PREPARATION_PHASE_BUDGET_MISSING")
            prior_budget = artifact(prior, "budget")
            _validate_existing(prior_budget, source_media_sha256=media_sha)
            _validate_existing(current_budget, source_media_sha256=media_sha)
            _require_budget_history_extension(prior_budget, current_budget)
        pointer = prior
        history = pointer.get("preparation_history")


def completed_retained_input_authority(
    completed: Mapping, *, input_srt_text: str, clip_context: Mapping,
) -> dict:
    """Describe restored input, without inventing earlier entity observations.

    The original normal review already owns the input-to-output mutation
    chain. Re-entering pre-review acoustic arbitration could acquire a new
    observation before that completed receipt is consumed. This input record
    claims only legal restoration; all later source/final gates still run.
    """
    validate_completed_retained_context(completed, clip_context)
    if input_srt_text != completed["input_srt_text"]:
        raise ValueError("RETAINED_PREPARATION_CURRENT_INPUT_DRIFT")
    digest = _sha(input_srt_text.encode()).removeprefix("sha256:")
    transcript = {
        "schema_version": "transcript-entity-input-replay.v1",
        "status": "NOT_REEXECUTED_COMPLETED_RETAINED_INPUT",
        "input_srt_sha256": digest, "output_srt_sha256": digest,
        "completion": copy.deepcopy(completed["completion"]),
        "confirmed": [], "repairs": [], "entity_verdict_required": [],
    }
    return {
        "schema_version": "chat-authority-audit.v2",
        "status": REPLAYED_INPUT_STATUS,
        "input_srt_sha256": digest, "output_srt_sha256": digest,
        "applied": [], "sender_repairs": [], "gift_repairs": [],
        "coreference_repairs": [], "entity_repairs": [],
        "entity_verdict_required": [], "sender_verdict_required": [],
        "pending_text_overrides": [],
        "retained_preparation_input": _completed_input_binding(completed),
        "transcript_entity_audit": transcript,
    }


def _restored_owner_prefix_matches(completed_owners: list, current: list, authority: Mapping) -> bool:
    """Compare restored owners, allowing only the later boundary freeze's marks.

    Source finalization freezes boundary owners on the same authority object
    (``freeze_story_chat_boundary_owners`` writes ``boundary_required`` /
    ``boundary_owner_id`` / ``boundary_owner_rejection`` into each row), and
    the source checkpoint binds that annotated authority. Those marks are the
    boundary stage's own contract, not an owner change: replay the production
    annotator on the completed owners under the authority's validated frozen
    contract and require exact equality. Any other row difference still drifts.
    """
    if current == completed_owners:
        return True
    from src.autoslice.producer_boundary_owner_contract import (
        freeze_story_chat_boundary_owners, validate_frozen_boundary_owner_contract)
    try:
        contract = validate_frozen_boundary_owner_contract(
            authority.get("frozen_boundary_owner_contract"))
    except (RuntimeError, TypeError, KeyError, AttributeError):
        return False
    start, end = contract.get("story_start_ms"), contract.get("story_end_ms")
    if type(start) is not int or type(end) is not int:
        return False
    replay = {"entity_repairs": copy.deepcopy(list(completed_owners))}
    owners = freeze_story_chat_boundary_owners(replay, story_start_ms=start, story_end_ms=end)
    return (replay["entity_repairs"] == current
            and all(owner in contract["owners"] for owner in owners))


def validate_completed_retained_input_authority(
    completed: Mapping | None, *, authority: Mapping, clip_context: Mapping,
) -> dict:
    """Revalidate an input restoration against the normal completion reader.

    The caller supplies the result of load_completed_retained_review after the
    ordinary source/parent/materialization/clock loader. A status string alone
    cannot replace that result or turn this input receipt into a release pass.
    """
    if not isinstance(completed, Mapping):
        raise ValueError("RETAINED_PREPARATION_INPUT_REPLAY_UNVERIFIED")
    expected = completed_retained_input_authority(
        completed, input_srt_text=completed["input_srt_text"], clip_context=clip_context)
    if any(authority.get(key) != expected[key] for key in (
            "status", "input_srt_sha256", "retained_preparation_input",
            "transcript_entity_audit")):
        raise ValueError("RETAINED_PREPARATION_INPUT_REPLAY_DRIFT")
    if any(authority.get(key) != [] for key in (
            "applied", "sender_repairs", "gift_repairs", "coreference_repairs",
            "entity_verdict_required", "sender_verdict_required", "pending_text_overrides")):
        raise ValueError("RETAINED_PREPARATION_INPUT_REPLAY_EXTRA_AUTHORITY")
    owners = authority.get("entity_repairs")
    if (not isinstance(owners, list)
            or not _restored_owner_prefix_matches(
                completed["owners"], owners[:len(completed["owners"])], authority)
            or authority.get("retained_entity_review") != completed["completion"]
            or authority.get("retained_preparation_replay") != {
                "completion": completed["completion"],
                "provider_calls_this_replay": 0, "release_authorized": False}):
        raise ValueError("RETAINED_PREPARATION_INPUT_REPLAY_OWNER_DRIFT")
    # Later native stages may append their own independently checked owners.
    # They cannot erase the original completion or its cumulative usage floor.
    usage = authority.get("cpa_adjudication_usage")
    current = usage.get("provider_adjudication_count") if isinstance(usage, Mapping) else None
    original = completed["review"]["cpa_adjudication_usage"]["provider_adjudication_count"]
    if type(current) is not int or current < original:
        raise ValueError("RETAINED_PREPARATION_INPUT_REPLAY_USAGE_DRIFT")
    _validate_completed_input_text(completed, authority)
    return expected["transcript_entity_audit"]


def consume_completed_retained_review(
    completed: Mapping, *, input_srt_text: str, authority: dict, clip_context: Mapping,
) -> tuple[str, dict]:
    """Restore the exact ordinary review result and its original typed owners."""
    validate_completed_retained_context(completed, clip_context)
    if input_srt_text != completed["input_srt_text"]:
        raise ValueError("RETAINED_PREPARATION_CURRENT_INPUT_DRIFT")
    existing = authority.setdefault("entity_repairs", [])
    if not isinstance(existing, list):
        raise ValueError("RETAINED_PREPARATION_ENTITY_REPAIRS_INVALID")
    existing.extend(copy.deepcopy(completed["owners"]))
    authority["retained_entity_review"] = copy.deepcopy(completed["completion"])
    review = copy.deepcopy(completed["review"])
    authority["cpa_adjudication_usage"] = copy.deepcopy(review["cpa_adjudication_usage"])
    authority["retained_preparation_replay"] = {
        "completion": copy.deepcopy(completed["completion"]),
        "provider_calls_this_replay": 0, "release_authorized": False,
    }
    return completed["output_srt_text"], review


def validate_retained_partial(
    *, native: Mapping, authority: Mapping, retained: Mapping | None,
) -> None:
    """An absent ASR witness is legal only for this validated retained input."""
    from src.autoslice.retained_reviewed_draft import _read_local_json
    validate_partial_predecessors(native=native, authority=authority)
    if (retained is None or native.get("schema_version") != RETAINED_PARTIAL_SCHEMA
            or native.get("source_language_witness") != "NOT_AVAILABLE_RETAINED_INPUT"
            or "language_witness" in native
            or native.get("completed_stage") not in {"post_review", "post_entity"}
            or native.get("release_authorized") is not False):
        raise ValueError("RETAINED_PREPARATION_PARTIAL_BINDING_INVALID")
    binding = native.get("retained_input") or {}
    raw, proof = _read_local_json(Path(str(binding.get("path") or "")), label="PREPARATION_RETAINED_INPUT")
    if (_sha(raw) != binding.get("sha256")
            or _proof_identity(proof) != _proof_identity(retained["proof"])
            or _proof_identity(authority.get("retained_reviewed_draft") or {})
            != _proof_identity(proof)):
        raise ValueError("RETAINED_PREPARATION_PARTIAL_PARENT_DRIFT")
    materialization = native.get("retained_materialization")
    history = (retained.get("proof") or {}).get("history_paths") or {}
    roots = {Path(str(value)).parent for value in history.values()}
    if len(roots) == 1:
        input_path = next(iter(roots)) / f'{proof["candidate_id"]}.retained-entity-review-input.json'
        intent_path, receipt_path = retained_history_materialization_paths(input_path)
        if materialization is not None or intent_path.parent.exists():
            if not isinstance(materialization, Mapping):
                raise ValueError("RETAINED_PREPARATION_PARTIAL_COPY_BINDING_MISSING")
            from src.autoslice.argon_escalation_storage import read
            for key, live_path in (("intent", intent_path), ("receipt", receipt_path)):
                bound = materialization.get(key) or {}
                frozen, _ = _read_local_json(Path(str(bound.get("path") or "")),
                                             label="PREPARATION_COPY")
                if (_sha(frozen) != bound.get("sha256")
                        or materialization.get(f"live_{key}_path") != str(live_path)
                        or read(live_path) != frozen):
                    raise ValueError("RETAINED_PREPARATION_PARTIAL_COPY_DRIFT")
    elif materialization is not None:
        raise ValueError("RETAINED_PREPARATION_PARTIAL_COPY_BINDING_MISSING")
    completion = authority.get("retained_entity_review")
    if (proof.get("pending_entity_reviews") or completion is not None):
        from src.autoslice import retained_reviewed_draft as reader
        validator = getattr(reader, "validate_pending_entity_checkpoint", None)
        if not callable(validator):
            raise ValueError("RETAINED_PREPARATION_PENDING_ENTITY_CONTRACT_REQUIRED")
        validator(retained, {"chat_authority": authority})
        bound = native.get("retained_completion") or {}
        raw, saved = _read_local_json(Path(str(bound.get("path") or "")), label="PREPARATION_COMPLETION")
        if (_sha(raw) != bound.get("sha256") or bound.get("sha256") != completion.get("sha256")
                or saved.get("schema_version") != SCHEMA
                or saved.get("status") != "CURRENT_REVIEW_COMPLETED"
                or saved.get("release_authorized") is not False):
            raise ValueError("RETAINED_PREPARATION_PARTIAL_COMPLETION_DRIFT")
