"""One finding lifecycle, independent of provider/transport receipt statuses.

Legacy receipts are read at this boundary, never rewritten.  A lifecycle is a
derived routing view, not mutation or release authority: KEEP is revalidated
by the existing release predicate and EDIT still needs apply + whole review.
"""
from __future__ import annotations

from .review_errors import ContractRejection as ContractRejection

from collections.abc import Mapping
from copy import deepcopy
from enum import StrEnum


class State(StrEnum):
    AWAITING_EVIDENCE = "AWAITING_EVIDENCE"
    AWAITING_DECISION = "AWAITING_DECISION"
    AWAITING_TERMINAL = "AWAITING_TERMINAL"
    KEEP = "KEEP"
    EDIT_REVIEW = "EDIT_REVIEW"
    TRUE_BLOCK = "TRUE_BLOCK"
    CONTRACT_ERROR = "CONTRACT_ERROR"
    CALL_FAILURE = "CALL_FAILURE"


class Action(StrEnum):
    CONTINUE_REVIEW = "CONTINUE_REVIEW"
    DISCLOSE = "DISCLOSE"
    APPLY_AND_REVIEW = "APPLY_AND_REVIEW"
    BLOCK_CONTENT = "BLOCK_CONTENT"
    RECOVER_ENGINEERING = "RECOVER_ENGINEERING"


class Reason(StrEnum):
    EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"
    DECISION_REQUIRED = "DECISION_REQUIRED"
    TERMINAL_REQUIRED = "TERMINAL_REQUIRED"
    VALIDATED_KEEP = "VALIDATED_KEEP"
    EDIT_APPLICATION_AND_REVIEW_REQUIRED = "EDIT_APPLICATION_AND_REVIEW_REQUIRED"
    WRITTEN_MEANING_ERROR = "WRITTEN_MEANING_ERROR"
    CONSUMER_REJECTED = "CONSUMER_REJECTED"
    CALL_NOT_COMPLETED = "CALL_NOT_COMPLETED"
    TERMINAL_NOT_COMPLETED = "TERMINAL_NOT_COMPLETED"


TERMINAL_STATES = frozenset({State.KEEP, State.EDIT_REVIEW, State.TRUE_BLOCK,
                           State.CONTRACT_ERROR, State.CALL_FAILURE})
ACTIONS = {
    State.AWAITING_EVIDENCE: Action.CONTINUE_REVIEW,
    State.AWAITING_DECISION: Action.CONTINUE_REVIEW,
    State.AWAITING_TERMINAL: Action.CONTINUE_REVIEW,
    State.KEEP: Action.DISCLOSE,
    State.EDIT_REVIEW: Action.APPLY_AND_REVIEW,
    State.TRUE_BLOCK: Action.BLOCK_CONTENT,
    State.CONTRACT_ERROR: Action.RECOVER_ENGINEERING,
    State.CALL_FAILURE: Action.RECOVER_ENGINEERING,
}
# Re-entry means revalidating saved evidence, never clearing an original bill,
# answer or UNKNOWN dispatch. Edits create a new current-text review obligation.
TRANSITIONS = {
    State.AWAITING_EVIDENCE: frozenset({State.AWAITING_DECISION, *TERMINAL_STATES}),
    State.AWAITING_DECISION: frozenset({State.AWAITING_EVIDENCE, State.AWAITING_TERMINAL, *TERMINAL_STATES}),
    State.AWAITING_TERMINAL: TERMINAL_STATES,
    State.EDIT_REVIEW: frozenset({State.AWAITING_DECISION, State.CONTRACT_ERROR, State.CALL_FAILURE}),
    State.CONTRACT_ERROR: frozenset(State),
    State.CALL_FAILURE: frozenset(State),
    State.TRUE_BLOCK: frozenset({State.AWAITING_EVIDENCE, State.CONTRACT_ERROR, State.CALL_FAILURE}),
    State.KEEP: frozenset({State.AWAITING_EVIDENCE, State.CONTRACT_ERROR, State.CALL_FAILURE}),
}

ENGINEERING_FAILURE = "FINAL_REVIEW_ENGINEERING_FAILURE"
REVIEW_PENDING = "FINAL_REVIEW_FINDING_REVIEW_PENDING"
EDIT_REQUIRED = "FINAL_REVIEW_EDIT_REVIEW_REQUIRED"
CONTENT_BLOCK = "FINAL_REVIEW_TRUE_CONTENT_BLOCK"
LEGACY_UNRESOLVED = "FINAL_REVIEW_UNRESOLVED_FINDINGS"
FINDING_FAILURE_CODES = frozenset({ENGINEERING_FAILURE, REVIEW_PENDING, EDIT_REQUIRED,
                                 CONTENT_BLOCK, LEGACY_UNRESOLVED})
SCHEMA = "finding-lifecycle.v1"


class LifecycleError(ValueError):
    pass


def state(value):
    try:
        return State(value)
    except (ValueError, TypeError) as exc:
        raise LifecycleError("FINDING_LIFECYCLE_UNKNOWN_STATE:" + str(value)) from exc


def transition(previous, following):
    previous, following = state(previous), state(following)
    if previous != following and following not in TRANSITIONS[previous]:
        raise LifecycleError(f"FINDING_LIFECYCLE_TRANSITION_INVALID:{previous}:{following}")
    return following


# These are receipt protocols emitted by the adjudication owners, not a list
# of resolved strings. Every entry remains non-releasable without typed proof.
LEGACY_STAGES = {
    "UNCERTAIN": State.AWAITING_EVIDENCE,
    "MISSING_PROPOSAL_UNRESOLVED": State.AWAITING_DECISION,
    "OBSERVED": State.AWAITING_DECISION,
    "CONTEXT_REVIEW": State.AWAITING_TERMINAL,
    "ARGON_PENDING": State.AWAITING_EVIDENCE,
    "ARGON_OBSERVED": State.AWAITING_DECISION,
    "TEXT_FIRST_CURRENT": State.AWAITING_DECISION,
    "TEXT_FIRST_CURRENT_CLOSURE_UNRESOLVED": State.AWAITING_TERMINAL,
    "UNRESOLVED": State.AWAITING_DECISION,
    "CURRENT_SUPPORTED": State.AWAITING_DECISION,
    "PROPOSED": State.AWAITING_DECISION,
    "RETRY_BLOCKED": State.AWAITING_TERMINAL,
    "BLOCK": State.AWAITING_EVIDENCE,
    "INVALID": State.CONTRACT_ERROR,
    "INVALID_SUGGESTION": State.CONTRACT_ERROR,
    "CONTRACT_ERROR": State.CONTRACT_ERROR,
    "CALL_FAILURE": State.CALL_FAILURE,
    "UNAVAILABLE": State.CALL_FAILURE,
    "AUDITOR_UNAVAILABLE": State.CALL_FAILURE,
    "SKIPPED_BUDGET": State.CALL_FAILURE,
    "SKIPPED_PROVIDER_DISABLED": State.CALL_FAILURE,
    "UNKNOWN": State.CALL_FAILURE,
}
_LEGACY_CALL_ERRORS = frozenset({"LlmCallError", "TimeoutError", "ConnectionError",
    "CONTEXT_VERIFIER_ERROR", "CONTEXT_VERIFIER_UNAVAILABLE", "TERMINAL_JUDGE_UNAVAILABLE",
    "EDITORIAL_PROVIDER_UNAVAILABLE", "EDITORIAL_AUDIO_UNAVAILABLE",
    "SEMANTIC_RANGE_OBSERVER_UNAVAILABLE", "SEMANTIC_RANGE_OBSERVATION_FAILED",
    "ARGON_INFRASTRUCTURE_BLOCKED"})


def _view(selected, reason, detail=None, *, terminal=False, retry=None):
    selected = state(selected)
    recovery = None
    if selected == State.CONTRACT_ERROR:
        # Engineering failures are retryable, never content: an answer to an
        # older contract is re-asked under the current one by its owner; an
        # answer to the current contract is revalidated after consumer repair
        # or new material (the identical question is not asked again).
        recovery = ("REASK_UNDER_CURRENT_CONTRACT" if (retry or {}).get("reask_under_current_contract")
                    else "REVALIDATE_ORIGINAL_ANSWER_AFTER_CONSUMER_REPAIR")
    elif selected == State.CALL_FAILURE:
        recovery = "RESUME_ORIGINAL_CALL_OWNER_WITH_UNKNOWN_AND_BUDGET_GUARDS"
    material_recovery = (detail or {}).get("material_recovery") if isinstance(detail, Mapping) else None
    if selected in {State.AWAITING_EVIDENCE, State.AWAITING_DECISION, State.AWAITING_TERMINAL}:
        recovery = "RESUME_ORIGINAL_CALL_OWNER_WITH_UNKNOWN_AND_BUDGET_GUARDS"
    pending_call = (material_recovery is not None
                    or isinstance(detail, Mapping) and detail.get("status") == "UNKNOWN")
    action = (Action.CONTINUE_REVIEW if pending_call and selected == State.CALL_FAILURE
              else ACTIONS[selected])
    view = {"schema_version": SCHEMA, "state": selected.value,
            "reason_code": Reason(reason).value, "release_action": action.value,
            "terminal": selected in TERMINAL_STATES, "detail": detail,
            "recovery": recovery, "new_model_call_authorized": False}
    if selected == State.CONTRACT_ERROR and retry is not None:
        view["contract_retry"] = dict(retry)
    return view


def _answer_contract_retry(adjudication):
    """The recorded answer's own contract decides how it can be retried."""
    from .answer_contract import contract_retry
    for owner in ("terminal_review", "editorial_authority"):
        receipt = adjudication.get(owner)
        if isinstance(receipt, Mapping) and isinstance(receipt.get("request"), Mapping):
            try:
                return contract_retry(receipt["request"])
            except (ValueError, KeyError, TypeError) as exc:
                # An undeterminable version is never presumed older: no re-ask.
                return {"family": owner, "kind": "CONTRACT_VERSION_UNDETERMINED",
                        "reask_under_current_contract": False, "error": str(exc) or type(exc).__name__}
    return None


def true_block_evidence(adjudication):
    """A model's D label is insufficient; reopen its original consumer/proof."""
    from .llm_client import extract_json_object
    from .semantic_terminal_adjudication import (validate_terminal_answer, terminal_prompt_matches,
                                                 written_meaning_evidence)
    from .targeted_relisten import validate_current_no_gain
    from .context_assisted_audio import text_sha
    review = adjudication.get("terminal_review")
    if not isinstance(review, Mapping):
        raise LifecycleError("FINDING_D_ORIGINAL_ANSWER_REQUIRED")
    request, prompt, raw = (review[k] for k in ("request", "prompt", "raw_completion"))
    for field, value in (("prompt_sha256", prompt), ("completion_sha256", raw)):
        if review.get(field) is not None and review[field] != text_sha(value):
            raise LifecycleError("FINDING_D_ORIGINAL_BINDING_DRIFT")
    if not terminal_prompt_matches(request, prompt):
        raise LifecycleError("FINDING_D_ORIGINAL_BINDING_DRIFT")
    validate_current_no_gain(request["audio_gain_termination"], request["check_request"])
    answer = extract_json_object(raw)
    if validate_terminal_answer(request, answer, prompt=prompt) != "UNRESOLVABLE":
        raise LifecycleError("FINDING_D_WRITTEN_MEANING_ERROR_REQUIRED")
    bound = written_meaning_evidence(request, answer)
    return {**bound, "written_meaning_error": answer["written_meaning_error"],
            "comparison_text": answer.get("comparison_text"),
            "evidence_refs": answer["evidence_refs"], "original_answer_sha256": text_sha(raw)}


def classify(finding, *, terminal=False):
    """Exhaustive routing at the producer/consumer boundary; no unknown fallback."""
    from .final_review_contract import is_release_resolved_keep
    if not isinstance(finding, Mapping):
        raise LifecycleError("FINDING_LIFECYCLE_ROW_INVALID")
    saved = finding.get("lifecycle")
    if saved is not None:
        if not isinstance(saved, Mapping) or saved.get("schema_version") != SCHEMA:
            raise LifecycleError("FINDING_LIFECYCLE_SCHEMA_INVALID")
        state(saved.get("state"))
    adjudication = finding.get("exact_release_adjudication") or finding.get("context_audio_adjudication")
    error = finding.get("terminal_funnel_error")
    if isinstance(error, Mapping):
        if "status" not in error:
            # The original funnel emitted an exception receipt before it had
            # a status column. Its retained-evidence/error shape identifies
            # an engineering failure, independent of the free-text cause.
            # An explicitly supplied unknown status still fails below.
            if (error.get('prior_evidence_retained') is not True
                    or not isinstance(error.get('reason_code'), str)
                    or not error['reason_code'].strip()):
                raise LifecycleError('FINDING_LIFECYCLE_LEGACY_ERROR_RECEIPT_INVALID')
            selected = State.CONTRACT_ERROR
        else:
            selected = state(error['status'])
        if selected not in {State.CONTRACT_ERROR, State.CALL_FAILURE}:
            raise LifecycleError("FINDING_LIFECYCLE_ERROR_STATUS_INVALID")
        return _view(selected, Reason.CALL_NOT_COMPLETED if selected == State.CALL_FAILURE
                     else Reason.CONSUMER_REJECTED, dict(error), terminal=terminal)
    if adjudication is None:
        return _view(State.AWAITING_EVIDENCE, Reason.EVIDENCE_REQUIRED, terminal=terminal)
    if not isinstance(adjudication, Mapping):
        raise LifecycleError("FINDING_LIFECYCLE_RECEIPT_INVALID")
    status = adjudication.get("status")
    if status not in LEGACY_STAGES:
        raise LifecycleError("FINDING_LIFECYCLE_UNKNOWN_RECEIPT_STATUS:" + str(status))
    detail = {"status": status, "reason_code": adjudication.get("reason_code"),
              "policy_branch": adjudication.get("policy_branch")}
    if is_release_resolved_keep(finding):
        return _view(State.KEEP, Reason.VALIDATED_KEEP)
    retry = _answer_contract_retry(adjudication)
    if (adjudication.get("terminal_review") and adjudication.get("repaired") is False
            and adjudication.get("reason_code") is None):
        # A saved terminal keep that no longer revalidates: report the guard's
        # or binding's own reason, not a generic meaning/content failure.
        from .semantic_terminal_adjudication import terminal_keep_failure
        detail["validation_error"] = (terminal_keep_failure(finding, adjudication)
                                      or "TERMINAL_KEEP_MEANING_CONTRACT_REQUIRED")
    if adjudication.get("repaired") is True:
        mutation = adjudication.get("mutation_authority") or {}
        if mutation.get("status") == "PASS" and adjudication.get("decision_authority") == "CPA_JUDGE":
            return _view(State.EDIT_REVIEW, Reason.EDIT_APPLICATION_AND_REVIEW_REQUIRED, detail)
        return _view(State.CONTRACT_ERROR, Reason.CONSUMER_REJECTED, detail, retry=retry)
    if adjudication.get("reason_code") == "TERMINAL_UNRESOLVABLE":
        try:
            evidence = true_block_evidence(adjudication)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return _view(State.CONTRACT_ERROR, Reason.CONSUMER_REJECTED,
                         {**detail, "validation_error": str(exc)}, retry=retry)
        return _view(State.TRUE_BLOCK, Reason.WRITTEN_MEANING_ERROR, evidence)
    layer = adjudication.get("failure_layer")
    if layer is not None and layer not in {"CONTRACT_ERROR", "CALL_FAILURE", "PROVIDER",
                                          "LOCAL_AFTER_PROVIDER_RETURN", "LOCAL_RETRY_ADMISSION"}:
        raise LifecycleError("FINDING_LIFECYCLE_UNKNOWN_FAILURE_LAYER:" + str(layer))
    if layer in {"CALL_FAILURE", "PROVIDER"} or adjudication.get("reason_code") in _LEGACY_CALL_ERRORS:
        return _view(State.CALL_FAILURE, Reason.CALL_NOT_COMPLETED, detail)
    if layer in {"CONTRACT_ERROR", "LOCAL_AFTER_PROVIDER_RETURN", "LOCAL_RETRY_ADMISSION"} or adjudication.get("terminal_review") or adjudication.get("editorial_authority"):
        return _view(State.CONTRACT_ERROR, Reason.CONSUMER_REJECTED, detail, retry=retry)
    selected = LEGACY_STAGES[status]
    reason = {State.AWAITING_EVIDENCE: Reason.EVIDENCE_REQUIRED,
              State.AWAITING_DECISION: Reason.DECISION_REQUIRED,
              State.AWAITING_TERMINAL: Reason.TERMINAL_REQUIRED,
              State.CONTRACT_ERROR: Reason.CONSUMER_REJECTED,
              State.CALL_FAILURE: Reason.CALL_NOT_COMPLETED}[selected]
    return _view(selected, reason, detail, terminal=terminal)


def annotate(finding, *, terminal=False):
    view = classify(finding, terminal=terminal)
    if finding.get('lifecycle') is not None:
        transition(finding['lifecycle']['state'], view['state'])
    return {**finding, "lifecycle": view}


def reenter_terminal_review(finding, *, check_request, no_gain_proof, clip_context):
    """Reopen a completed KEEP/D only at the owner of validated new material.

    A new pass number or an altered explanation is not material. This changes
    the review phase, never the old decision or the new decision's authority.
    EDIT_REVIEW stays with the apply + whole-review owner; it has no shortcut
    through this evidence-only entry.
    """
    saved = finding.get('lifecycle')
    if saved is None:
        return finding
    if not isinstance(saved, Mapping) or saved.get('schema_version') != SCHEMA:
        raise LifecycleError('FINDING_LIFECYCLE_SCHEMA_INVALID')
    previous = state(saved.get('state'))
    if previous not in {State.KEEP, State.TRUE_BLOCK}:
        return finding
    from .context_assisted_audio import digest, text_sha
    from .targeted_relisten import validate_current_no_gain
    prior = finding.get('exact_release_adjudication') or finding.get('context_audio_adjudication') or {}
    request = (prior.get('terminal_review') or {}).get('request')
    if not isinstance(request, Mapping):
        raise ContractRejection('FINDING_REENTRY_ORIGINAL_TERMINAL_REQUIRED')
    old = validate_current_no_gain(request['audio_gain_termination'], request['check_request'])
    new = validate_current_no_gain(no_gain_proof, check_request)
    old_ids = {row['observation_id'] for row in old['projection']['observations']}
    new_ids = {row['observation_id'] for row in new['projection']['observations']}
    changes = []
    previous_check = request['check_request']
    if 'finding_context' in check_request:
        from .finding_context import context_for_check, local_material_view, physical_question, validate_context_for_check
        if physical_question(previous_check) != physical_question(check_request):
            raise ContractRejection('FINDING_REENTRY_QUESTION_DRIFT')
        # A legacy saved request is projected from its own retained SRT. Its
        # receipt/proof bytes are never rewritten to impersonate a v2 request.
        previous_context = previous_check.get('finding_context') or context_for_check(previous_check)
        validate_context_for_check(previous_check, previous_context)
        validate_context_for_check(check_request, check_request['finding_context'])
        if local_material_view(previous_context) != local_material_view(check_request['finding_context']):
            changes.append('LOCAL_TEXT')
    else:
        if request.get('srt_sha256') != text_sha(check_request['whole_clip_current_srt']):
            changes.append('CURRENT_TEXT')
        if request.get('clip_context_sha256') != digest(dict(clip_context or {})):
            changes.append('CLIP_CONTEXT')
    if request['check_request'].get('proposed_cue') != check_request.get('proposed_cue'):
        changes.append('CANDIDATE')
    if new_ids - old_ids:
        changes.append('BOUND_OBSERVATION')
    if not changes:
        raise ContractRejection('FINDING_REENTRY_NEW_MATERIAL_REQUIRED')
    following = transition(previous, State.AWAITING_EVIDENCE)
    entry = {'previous_lifecycle': deepcopy(dict(saved)), 'material_changes': changes,
             'previous_adjudication_sha256': digest(prior),
             'previous_request_sha256': digest(request),
             'current_check_request_sha256': check_request['request_sha256'],
             'current_no_gain_proof_sha256': new['proof_sha256'],
             'new_observation_ids': sorted(new_ids - old_ids)}
    return {**finding, 'lifecycle': _view(following, Reason.EVIDENCE_REQUIRED, entry),
            'lifecycle_reentries': [*finding.get('lifecycle_reentries', []), entry]}


def release_reason_codes(findings, *, terminal=False):
    actions = set()
    for row in findings:
        saved = row.get('lifecycle') if isinstance(row, Mapping) else None
        closed = (isinstance(saved, Mapping) and saved.get('schema_version') == SCHEMA
                  and state(saved.get('state')) in TERMINAL_STATES)
        # Older audit envelopes do not have a terminal_funnel marker. A saved
        # terminal view preserves the completed phase, never its authority:
        # classify reopens the original proof before selecting any action.
        actions.add(Action(classify(row, terminal=terminal or closed)['release_action']))
    return [code for action, code in (
        (Action.RECOVER_ENGINEERING, ENGINEERING_FAILURE),
        (Action.BLOCK_CONTENT, CONTENT_BLOCK),
        (Action.APPLY_AND_REVIEW, EDIT_REQUIRED),
        (Action.CONTINUE_REVIEW, REVIEW_PENDING),
    ) if action in actions]


def is_finding_failure(code):
    return code in FINDING_FAILURE_CODES


def only_finding_failures(codes):
    return isinstance(codes, list) and bool(codes) and set(codes) <= FINDING_FAILURE_CODES


def review_outcome(audit, *, terminal=False):
    """Shared writer for ordinary review and the final stopped-loop funnel."""
    rows, disclosed = [], []
    for row in [*audit.get('findings', []), *audit.get('unresolved_findings_disclosed', [])]:
        row = annotate(row, terminal=terminal)
        (disclosed if row['lifecycle']['state'] == State.KEEP else rows).append(row)
    reasons = [code for code in audit.get("reason_codes", []) if code not in FINDING_FAILURE_CODES]
    reasons.extend(release_reason_codes(rows, terminal=terminal))
    return {**audit, "findings": rows, "unresolved_findings_disclosed": disclosed,
            "validated_finding_count": len(rows), "reason_codes": list(dict.fromkeys(reasons)),
            "status": "FLAGGED" if reasons else "CLEAN", "release_gate": "BLOCK" if reasons else "PASS"}
