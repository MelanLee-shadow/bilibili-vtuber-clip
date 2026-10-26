"""Retain the native intermediate text inside its existing chat audit.

This is recovery data, not final-review authority. It preserves the exact
post-correction string even when a later language gate interrupts production.
Current delivery checkpoints also retain native artifact bindings for ordinary
recovery. Their reader replays source/clock/typed contracts; neither form is a
release verdict or a substitute for missing review.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
import hashlib
import json
from pathlib import Path
import copy

TEXT_KEY = "post_transcript_entity_output_srt"
SHA_KEY = "post_transcript_entity_output_srt_sha256"


def retain_post_transcript_text(audit: MutableMapping[str, object], srt_text: str) -> None:
    """Bind the exact string at the existing native post-transcript checkpoint."""
    # Preserve the established raw UTF-8 digest; do not normalize newlines or cues.
    digest = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    audit[SHA_KEY] = digest
    audit[TEXT_KEY] = srt_text


def load_post_transcript_text(audit: Mapping[str, object]) -> str | None:
    """Read a validated intermediate snapshot; old hash-only audits return None.

    A returned string still needs its original candidate/source/context and
    mutation receipts and all later native gates. A matching hash is not a
    claim that its words are correct or that it is the final delivery text.
    """
    if TEXT_KEY not in audit:
        return None
    text = audit[TEXT_KEY]
    expected = audit.get(SHA_KEY)
    if not isinstance(text, str) or not isinstance(expected, str):
        raise ValueError("POST_TRANSCRIPT_TEXT_CHECKPOINT_INVALID")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected:
        raise ValueError("POST_TRANSCRIPT_TEXT_CHECKPOINT_INVALID")
    return text


CURRENT_SCHEMA = "producer-current-text-checkpoint.v1"
SOURCE_SCHEMA = "producer-source-text-checkpoint.v1"
PARTIAL_SCHEMA = "producer-partial-source-text-checkpoint.v1"


def _frozen_bytes(root: Path, key: str, raw: bytes, suffix: str) -> dict[str, str]:
    from src.autoslice.package_import import atomic_write_bytes
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()
    path = root / f'{key}.{digest}{suffix}'
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != raw:
            raise ValueError('TEXT_CHECKPOINT_ARTIFACT_DRIFT')
    else:
        atomic_write_bytes(path, raw)
    return {'path':str(path.resolve()), 'sha256':'sha256:' + digest}


def write_partial_source_text_checkpoint(
    path: Path, *, candidate_id: str, srt_text: str, authority: Mapping,
    final_review: Mapping, context_path: Path, padded: Path,
    source_language_witness_srt: str, protected_name_source_srt: str,
    native_budget_receipt_path: Path | None = None,
    retained_reviewed_input: Mapping | None = None,
) -> None:
    """Persist the actual post-review input before pending finalization.

    This records an unfinished native phase, not a source-stage or release
    PASS. Later attempts still validate typed mutations and all pending gates.
    """
    from src.autoslice.producer_media import _write_json_atomic
    root = path.parent / 'partial-source-text-input'
    saved = copy.deepcopy(dict(authority))
    saved['final_review_audit'] = copy.deepcopy(dict(final_review))
    retain_post_transcript_text(saved, srt_text)
    from src.autoslice.review_evidence_store import project_document
    saved = project_document(saved, path.parent)
    artifacts = {
        'srt':_frozen_bytes(root,'srt',srt_text.encode(),'.srt'),
        'authority':_frozen_bytes(root,'authority',(json.dumps(saved,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode(),'.json'),
        'context':_frozen_binding(root,'context',context_path),
        'name_reference':_frozen_bytes(root,'name_reference',protected_name_source_srt.encode(),'.srt'),
    }
    if padded.is_symlink() or not padded.is_file():
        raise ValueError('SOURCE_TEXT_CHECKPOINT_ARTIFACT_INVALID')
    from .evidence_identity import file_sha256
    media_sha = file_sha256(padded)
    schema = PARTIAL_SCHEMA
    if source_language_witness_srt:
        artifacts['language_witness'] = _frozen_bytes(
            root, 'language_witness', source_language_witness_srt.encode(), '.srt')
    else:
        from src.autoslice.retained_preparation_checkpoint import RETAINED_PARTIAL_SCHEMA
        proof = (retained_reviewed_input or {}).get('proof')
        if (not isinstance(proof, Mapping) or proof.get('candidate_id') != candidate_id
                or str(proof.get('padded_sha256') or '').removeprefix('sha256:') != media_sha
                or dict(proof) != saved.get('retained_reviewed_draft')):
            raise ValueError('RETAINED_PREPARATION_WITNESS_ROLE_INVALID')
        schema = RETAINED_PARTIAL_SCHEMA
        artifacts['source_language_witness'] = 'NOT_AVAILABLE_RETAINED_INPUT'
        artifacts['retained_input'] = _frozen_bytes(
            root, 'retained_input', json.dumps(dict(proof), ensure_ascii=False,
                sort_keys=True, indent=2).encode(), '.json')
        # Freeze the already verified copy identity into the native partial
        # checkpoint. A cold loader cannot lose it and adopt new file stats.
        from src.autoslice.retained_preparation_checkpoint import retained_history_materialization_paths
        intent, receipt = retained_history_materialization_paths(
            path.parent / f'{candidate_id}.retained-entity-review-input.json')
        if intent.parent.exists() or intent.parent.is_symlink():
            artifacts['retained_materialization'] = {
                'intent': _frozen_binding(root, 'retained_copy_intent', intent),
                'receipt': _frozen_binding(root, 'retained_copy_receipt', receipt),
                'live_intent_path': str(intent), 'live_receipt_path': str(receipt),
            }
        completion = saved.get('retained_entity_review')
        if completion is not None:
            if not isinstance(completion, Mapping):
                raise ValueError('RETAINED_PREPARATION_COMPLETION_BINDING_INVALID')
            bound = _frozen_binding(root, 'retained_completion', Path(str(completion.get('path') or '')))
            if bound['sha256'] != completion.get('sha256'):
                raise ValueError('RETAINED_PREPARATION_COMPLETION_HASH_DRIFT')
            artifacts['retained_completion'] = bound
    budget = native_budget_receipt_path or path.parent / 'native-audio-budget.json'
    if budget.exists() or budget.is_symlink():
        artifacts['budget'] = _frozen_binding(root,'budget',budget)
    source_parent = path.parent / f'{candidate_id}.source-text-checkpoint.json'
    if source_parent.exists() or source_parent.is_symlink():
        artifacts['source_checkpoint_parent'] = _frozen_binding(root,'source_checkpoint_parent',source_parent)
    # Link the actual prior pointer, including a legacy pointer on first new
    # write. A new consumer never invents success or re-signs missing history.
    previous = None
    previous_raw = None
    if path.exists() or path.is_symlink():
        previous = _frozen_binding(root, 'checkpoint', path)
        previous_raw = Path(previous['path']).read_bytes()
    native = {
        'schema_version':schema,'candidate_id':candidate_id,'time_domain':'padded_local',
        'completed_stage':('post_entity' if final_review.get('status') == 'AUDITOR_UNAVAILABLE' else 'post_review'),
        'pending_stage':('review_and_source_finalization' if final_review.get('status') == 'AUDITOR_UNAVAILABLE' else 'source_finalization'),
        'padded':{'path':str(padded.resolve()),'sha256':'sha256:' + media_sha},
        **artifacts,'release_authorized':False,
    }
    if schema != PARTIAL_SCHEMA:
        from src.autoslice.retained_preparation_checkpoint import (
            replayed_input_present, validate_partial_predecessors)
        origin = saved.get('retained_preparation_input')
        if replayed_input_present(saved) and not isinstance(origin, Mapping):
            raise ValueError('RETAINED_PREPARATION_PHASE_ORIGIN_MISSING')
        native['preparation_history'] = {
            'schema_version': 'producer-preparation-history.v1',
            'restored_input_origin': copy.deepcopy(origin),
            'previous_checkpoint': ({**previous,
                'role': 'PREVIOUS_NORMAL_PREPARATION_CHECKPOINT'} if previous else None),
        }
        validate_partial_predecessors(native=native, authority=saved)
    if ((path.exists() or path.is_symlink()) != (previous_raw is not None)
            or previous_raw is not None and (path.is_symlink() or path.read_bytes() != previous_raw)):
        raise ValueError('RETAINED_PREPARATION_PHASE_CHANGED_DURING_SAVE')
    _write_json_atomic(path, native)


def _frozen_binding(root: Path, key: str, source: Path) -> dict[str, str]:
    from src.autoslice.package_import import atomic_write_bytes

    if source.is_symlink() or not source.is_file():
        raise ValueError("TEXT_CHECKPOINT_ARTIFACT_INVALID")
    root.mkdir(parents=True, exist_ok=True)
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    target = root / f"{key}.{digest}{source.suffix}"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or target.read_bytes() != raw:
            raise ValueError("TEXT_CHECKPOINT_ARTIFACT_DRIFT")
    else:
        # The producer's candidate lease is the sole writer. Reuse the shared
        # fsync/replace writer so a killed copy leaves only an unreferenced
        # temporary, never a truncated content-addressed target.
        atomic_write_bytes(target, raw)
    return {"path": str(target.resolve()), "sha256": "sha256:" + digest}


_MISSING = object()


def _authority_field_reader(authority_path: Path):
    """Resolve single fields of a persisted authority without materialising it.

    Checkpoint writers bind a handful of top-level identity fields. Reading
    them through ``review_from_bytes`` first expands every reference of the
    whole document: each blob is re-read and re-hashed at every occurrence and
    the complete nested legacy object is rebuilt in memory. On the
    闺蜜 rerun (c0a3b2f0c) that cold read of a 386 MB compact authority ran
    50 minutes after the review had already decided FINAL_REVIEW_RELEASE_BLOCKED
    and grew past 12 GB until the kernel OOM killer ended the producer inside
    this function. Reference semantics stay with ``review_evidence_store.expand``
    (same co-located store rule as ``review_from_bytes``); only the values a
    writer actually reads are expanded. The checkpoint's reader still
    cold-reads and validates the complete chain before any continuation.
    """
    from src.autoslice.review_evidence_store import evidence_store_root, expand, is_reference

    document = json.loads(authority_path.read_bytes())
    if not isinstance(document, Mapping):
        raise ValueError("TEXT_CHECKPOINT_AUTHORITY_INVALID")
    root = evidence_store_root(authority_path.parent)
    store_root = root if root.is_dir() else None

    def field(*keys: str, default: object = None) -> object:
        value: object = document
        for key in keys:
            if is_reference(value):
                value = expand(value, store_root=store_root)
            if not isinstance(value, Mapping) or key not in value:
                return default
            value = value[key]
        return expand(value, store_root=store_root)

    return field


def write_source_text_checkpoint(
    path: Path, *, candidate_id: str, srt_path: Path,
    authority_path: Path, context_path: Path, padded: Path,
) -> None:
    """Freeze a validated source-stage input before a later mutable attempt.

    Keep exact native bytes recoverable even when a later guard overwrites the
    working audit. The ordinary reader must still replay every source-stage
    contract; this file authorizes neither a repair nor release.
    """
    from src.autoslice.producer_media import _write_json_atomic

    root = path.parent / "source-text-input"
    artifacts = {}
    for key, source in (("srt", srt_path), ("authority", authority_path), ("context", context_path)):
        artifacts[key] = _frozen_binding(root, key, source)
    authority_path = Path(artifacts["authority"]["path"])
    field = _authority_field_reader(authority_path)
    srt_sha = artifacts["srt"]["sha256"].removeprefix("sha256:")
    # The retained post-transcript snapshot precedes deterministic source
    # truth and release hygiene. Keep that history intact: it need not equal
    # the completed source text, but both snapshots must bind their own bytes.
    snapshot = {key: value for key in (TEXT_KEY, SHA_KEY)
                if (value := field(key, default=_MISSING)) is not _MISSING}
    if (str(field("final_output_srt_sha256") or "").removeprefix("sha256:") != srt_sha
            or load_post_transcript_text(snapshot) is None):
        raise ValueError("SOURCE_TEXT_CHECKPOINT_SRT_AUTHORITY_DRIFT")
    context = field('final_review_audit', 'source_boundary_context')
    if context is not None:
        from src.autoslice.boundary_semantic_review import cue_grid_sha256
        from src.autoslice.jingting_chunker import parse_srt_cues
        boundary_path = Path(str(context.get('source_srt_path') or ''))
        if (context.get('schema_version') != 'source-boundary-transcript-context.v1'
                or context.get('role') != 'SOURCE_DOMAIN_BOUNDARY_REVIEW_CONTEXT'
                or not boundary_path.resolve().is_relative_to(path.parent.resolve())):
            raise ValueError('SOURCE_TEXT_CHECKPOINT_BOUNDARY_BINDING_INVALID')
        boundary = _frozen_binding(root, 'boundary_srt', boundary_path)
        grid = cue_grid_sha256(parse_srt_cues(Path(boundary['path']).read_text()))
        semantic = field('final_review_audit', 'boundary_semantic_review') or {}
        if (context.get('source_srt_sha256') != boundary['sha256']
                or context.get('source_cue_grid_sha256') != grid
                or semantic.get('review_scope') != 'source_full_window'
                or semantic.get('cue_grid_sha256') != grid):
            raise ValueError('SOURCE_TEXT_CHECKPOINT_BOUNDARY_DRIFT')
        artifacts['boundary_srt'] = boundary
    if padded.is_symlink() or not padded.is_file():
        raise ValueError("SOURCE_TEXT_CHECKPOINT_ARTIFACT_INVALID")
    from .evidence_identity import file_sha256
    padded_sha = file_sha256(padded)
    _write_json_atomic(path, {
        "schema_version": SOURCE_SCHEMA, "candidate_id": candidate_id,
        "time_domain": "padded_local", **artifacts,
        "padded": {"path": str(padded.resolve()), "sha256": "sha256:" + padded_sha},
        "release_authorized": False,
    })


def validate_incomplete_review_input(audit: Mapping[str, object], *, expected_srt_sha256: str) -> None:
    """Validate saved mutations when discovery itself still needs a retry.

    This input check omits the failed stage, never marks it COMPLETE, and
    grants no release. The caller must separately replay the original clock
    and typed text chain. The shared release validator stays unchanged.
    """
    from src.autoslice.final_review_contract import (
        FinalReviewContractError, SCHEMA_VERSION, unconsumed_correction_carryover_count,
    )

    if audit.get("schema_version") != SCHEMA_VERSION:
        raise FinalReviewContractError("FINAL_REVIEW_AUDIT_SCHEMA_INVALID")
    if audit.get("reviewed_srt_sha256") != expected_srt_sha256:
        raise FinalReviewContractError("FINAL_REVIEW_SRT_BINDING_MISMATCH")
    mutations = audit.get("correction_mutation_authority")
    if (not isinstance(mutations, Mapping)
            or mutations.get("schema_version") != "subtitle-correction-mutation-audit.v1"
            or mutations.get("status") != "PASS"):
        raise FinalReviewContractError("FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID")
    if unconsumed_correction_carryover_count(audit):
        raise FinalReviewContractError("FINAL_REVIEW_CARRYOVER_UNCONSUMED")


def write_current_text_checkpoint(
    path: Path, *, candidate_id: str, srt_path: Path,
    authority_path: Path, context_path: Path, padded: Path,
    source_clock: Mapping[str, object],
) -> None:
    """Save native artifact bindings, never a release verdict.

    The text owner calls this after the exact gate has persisted its repair
    chain, including REVIEW_PENDING. The reader must still validate that
    chain, source provenance, original clock and current semantic gates.
    """
    from src.autoslice.producer_media import _write_json_atomic

    def binding(artifact: Path) -> dict[str, str]:
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError("CURRENT_TEXT_CHECKPOINT_ARTIFACT_INVALID")
        from .evidence_identity import file_sha256
        digest = file_sha256(artifact)
        return {"path": str(artifact.resolve()), "sha256": "sha256:" + digest}

    root = path.parent / "current-text-input"
    srt = _frozen_binding(root, "srt", srt_path)
    authority_binding = _frozen_binding(root, "authority", authority_path)
    context_binding = _frozen_binding(root, "context", context_path)
    authority_path = Path(authority_binding["path"])
    field = _authority_field_reader(authority_path)
    digest = srt["sha256"].removeprefix("sha256:")
    output = str(field("final_output_srt_sha256") or field("final_text_srt_sha256") or "").removeprefix("sha256:")
    pending = field("exact_final_cpa_self_heal")
    terminal = str((pending.get("final_srt_sha256") if isinstance(pending, Mapping) else None) or "").removeprefix("sha256:")
    if digest not in {output, terminal}:
        raise ValueError("CURRENT_TEXT_CHECKPOINT_SRT_AUTHORITY_DRIFT")
    _write_json_atomic(path, {
        "schema_version": CURRENT_SCHEMA, "candidate_id": candidate_id,
        "time_domain": "delivery_local", "srt": srt,
        "authority": authority_binding, "context": context_binding,
        "padded": binding(padded), "source_clock": dict(source_clock),
        "release_authorized": False,
    })
