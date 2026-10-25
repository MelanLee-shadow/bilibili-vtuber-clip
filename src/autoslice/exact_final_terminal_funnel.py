"""Stopped exact-review loops enter the existing, typed material terminal.

The ContextVar carries live adapters across the ordinary reviewer callback; it
is scoped to that callback, never persisted in a JSON audit or shared globally.
"""
from __future__ import annotations

from .review_evidence_store import cold_input
from .review_errors import (ContractRejection, ProviderFailure, records_program_errors,
                            review_error_record, contract_boundary)

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from .context_assisted_audio import digest, text_sha

_capture = ContextVar('exact_final_terminal_adapters', default=None)


@contextmanager
def capture_terminal_adapters():
    adapters = {}
    token = _capture.set(adapters)
    try:
        yield adapters
    finally:
        _capture.reset(token)


def register_terminal_adapters(**kwargs):
    target = _capture.get()
    if target is not None:
        target.update(kwargs)


def unavailable_review_dependency(audit, adapters):
    """A failed reviewer cannot supply inputs for a new content check.

    This tests the upstream owner's explicit outcome, never free-form error
    text or the absence of an optional audio provider. Offline replay with a
    bound clip is still allowed when no new provider call was authorized.
    """
    discovery = audit.get('discovery') or {}
    if (audit.get('status') == 'AUDITOR_UNAVAILABLE'
            or isinstance(discovery, Mapping)
            and discovery.get('status') == 'AUDITOR_UNAVAILABLE'):
        return 'TERMINAL_REVIEW_AUDITOR_UNAVAILABLE'
    if not isinstance(adapters.get('clip_context'), Mapping):
        return 'TERMINAL_REVIEW_ADAPTERS_UNAVAILABLE'
    return None


def _defer_unavailable_review(result, *, pass_receipts, reason):
    from .finding_lifecycle import (ENGINEERING_FAILURE, State, annotate,
                                    classify, review_outcome)

    # Preserve genuine keeps, edits and content blocks and all original
    # adjudication/UNKNOWN evidence. This is an execution receipt, not a new
    # content judgment or a permission to repeat an audio request.
    for bucket in ('findings', 'unresolved_findings_disclosed'):
        rows = []
        for original in result.get(bucket) or []:
            row = deepcopy(original)
            if classify(row)['state'] not in {State.KEEP, State.TRUE_BLOCK, State.EDIT_REVIEW}:
                row['terminal_funnel_error'] = {
                    'status': 'CALL_FAILURE', 'reason_code': reason,
                    'prior_evidence_retained': True,
                }
                row = annotate(row, terminal=True)
            rows.append(row)
        result[bucket] = rows
    result['reason_codes'] = list(dict.fromkeys([
        *(result.get('reason_codes') or []), ENGINEERING_FAILURE]))
    result['terminal_funnel'] = {
        'schema_version': 'exact-final-terminal-funnel.v1',
        'status': 'CALL_FAILURE', 'reason_code': reason,
        'pass_receipts': deepcopy(pass_receipts), 'attempts': [],
        'content_checks_constructed': False,
        'recovery': 'RESUME_REVIEW_AFTER_DEPENDENCY_RECOVERY',
        'new_model_call_authorized': False,
    }
    outcome = review_outcome(result)
    # Dependency failure blocks this review even with no findings (or only
    # valid keeps). Row-derived reasons alone cannot represent that case.
    outcome['reason_codes'] = list(dict.fromkeys([*outcome['reason_codes'], ENGINEERING_FAILURE]))
    outcome.update(status='FLAGGED', release_gate='BLOCK')
    return outcome


def unresolved_review_rows(audit):
    from .final_review_contract import is_release_resolved_keep
    rows = list(audit.get('findings') or [])
    rows.extend(row for row in audit.get('unresolved_findings_disclosed') or []
        if not is_release_resolved_keep(row))
    return rows


@cold_input
def review_pass_receipt(*, pass_index, srt_text, audit, repaired_text, repairs):
    receipt = {'schema_version': 'exact-final-terminal-pass.v1', 'pass_index': pass_index,
        'input_srt_sha256': 'sha256:' + text_sha(srt_text),
        'output_srt_sha256': 'sha256:' + text_sha(repaired_text),
        'review_audit_sha256': digest(audit), 'repair_count': len(repairs),
        'repairs_sha256': digest(repairs),
        'unresolved_finding_sha256s': [digest(row) for row in unresolved_review_rows(audit)]}
    receipt['receipt_sha256'] = digest(receipt)
    return receipt


def applied_pass_receipt(original):
    """Bind a resumed pass already validated by the normal self-heal owner."""
    receipt = {'schema_version': 'exact-final-terminal-pass.v1',
        'pass_index': original['pass_index'], 'input_srt_sha256': original['input_srt_sha256'],
        'output_srt_sha256': original['output_srt_sha256'], 'repair_count': len(original['repairs']),
        'repairs_sha256': digest(original['repairs']), 'source_pass_receipt_sha256': digest(original),
        'unresolved_finding_sha256s': []}
    receipt['receipt_sha256'] = digest(receipt)
    return receipt


def _records(value):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _records(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _records(child)


@cold_input
def terminal_check(srt_text, finding, *, clip_context, offset_ms, ranges=(), secondary=(),
                  related_findings=(), historical_findings=()):
    from .final_review_auditor import (
        _candidate_blind_missing_proposal_scaffold, build_context_adjudication_request,
    )
    from .targeted_relisten import TARGET_MATERIAL_CONTRACT
    from .audio_question_identity import same_audio_question
    from .acoustic_witness_adjudication import build_witness_request, same_hearing
    from .semantic_audio_recovery import argon_terminal_kind

    scaffold = dict(finding)
    if not scaffold.get('proposed_full_cue') and not scaffold.get('suggestion'):
        scaffold = _candidate_blind_missing_proposal_scaffold(srt_text, finding)
    if scaffold is None:
        raise ContractRejection('TERMINAL_FINDING_GEOMETRY_INVALID')
    old, old_keys = [], set()
    for row in (finding, *related_findings, *historical_findings):
        value = row.get('exact_release_adjudication') or {}
        key = digest(value)
        if key in old_keys:
            continue
        old_keys.add(key)
        old.append(value)
    bound_ranges = list(ranges)
    for row in _records(old):
        if row.get('schema_version') == 'subtitle-span-acoustic-check-request.v1':
            bound_ranges.extend(row.get('context_only_range_acoustic_observations') or [])
    scaffold['_prior_range_acoustic_observations'] = list({digest(r): r for r in bound_ranges}.values())
    check = build_context_adjudication_request(srt_text, scaffold, clip_context=clip_context,
        source_media_timeline_offset_ms=offset_ms, require_complete_utterance_support=True)
    # The producer binds the complete cue text and its physical audio window,
    # covering the union of every G6 member's character spans. A narrower
    # representative suspect never narrows the terminal question.
    target = build_witness_request(check)
    observations = list(secondary)
    source_observations, rejected_history, reopened_argon = {}, [], set()
    processed_requests, witness_requests = set(), {}
    historical_argon_upgrade = check.get('argon_upgrade_available') is True

    def self_hashed_request(value):
        """Return a historical check request only when its own bytes verify."""
        if (not isinstance(value, Mapping)
                or value.get('schema_version') != 'subtitle-span-acoustic-check-request.v1'):
            return None
        request_sha256 = value.get('request_sha256')
        unsigned = {key: item for key, item in value.items() if key != 'request_sha256'}
        if not isinstance(request_sha256, str) or digest(unsigned) != request_sha256:
            return None
        return value

    def historical_requests(row):
        """Yield the original request carried by either historical owner shape."""
        if row.get('schema_version') == 'subtitle-span-acoustic-check-request.v1':
            yield row, 'request'
        for field in ('request', 'consumer_check_request'):
            value = row.get(field)
            if isinstance(value, Mapping):
                yield value, field

    def historical_argon_state(row, request, request_field):
        """Project a validated historical Argon owner end state.

        ``consumer_check_request`` is the owner seam used by semantic range
        recovery; older span adjudications carry the same request as
        ``request``.  A status is terminal only through the shared Argon
        classifier.  An explicit semantic recovery state is accepted only
        when its owner audit agrees (or when no separate Argon audit exists).
        """
        explicit = row.get('argon_state')
        owner_audit = row.get('argon_escalation')
        explicit_state = explicit.get('state') if isinstance(explicit, Mapping) else None
        owner_kind = argon_terminal_kind(owner_audit) if isinstance(owner_audit, Mapping) else None
        if owner_kind == 'HEARING_TERMINAL':
            state = 'TERMINAL'
        elif owner_kind == 'STRUCTURALLY_UNAVAILABLE':
            state = 'STRUCTURALLY_UNAVAILABLE'
        elif owner_kind is not None:
            # INFRASTRUCTURE_BLOCKED is a real owner result, but hearing is
            # still unresolved and the target must remain open.
            return None
        elif (explicit_state in {'TERMINAL', 'STRUCTURALLY_UNAVAILABLE'}
              and not isinstance(owner_audit, Mapping)):
            explicit_kind = argon_terminal_kind(explicit) if isinstance(explicit, Mapping) else None
            explicit_status = explicit.get('status') if isinstance(explicit, Mapping) else None
            if explicit_kind is not None:
                expected_kind = ('HEARING_TERMINAL' if explicit_state == 'TERMINAL'
                                 else 'STRUCTURALLY_UNAVAILABLE')
                if explicit_kind != expected_kind:
                    return None
            else:
                # A claimed state without a real terminal owner status is
                # not a channel attempt. A consumed observation can satisfy
                # the channel through its ordinary cold-validated envelope.
                return None
            state = explicit_state
        elif (explicit_state in {'TERMINAL', 'STRUCTURALLY_UNAVAILABLE'}
              and isinstance(owner_audit, Mapping)):
            # A claimed terminal state cannot override a pending/unknown or
            # infrastructure-failed owner audit.
            return None
        else:
            return None
        audit = (deepcopy(dict(owner_audit)) if isinstance(owner_audit, Mapping)
                 else deepcopy(dict(row)))
        return {
            'state': state,
            **({'status': explicit.get('status')} if isinstance(explicit, Mapping)
               and explicit.get('status') is not None else {}),
            **({'reason': explicit.get('reason')} if isinstance(explicit, Mapping)
               and explicit.get('reason') is not None else {}),
            'owner_audit': audit,
            'source_request': deepcopy(dict(request)),
            'provenance': {
                'source_field': request_field,
                'request_sha256': request['request_sha256'],
                'owner_audit_sha256': digest(audit),
            },
        }

    historical_argon_states = []
    for row in _records(old):
        request, request_field = next(
            ((candidate, field) for candidate, field in historical_requests(row)
             if self_hashed_request(candidate) is not None
             and same_audio_question(candidate, check, compare_proposed=False, strict_source=True)[0]),
            (None, None),
        )
        if request is None:
            continue
        # Preserve only an explicit target-scoped availability declaration from
        # a matched historical check. Provider/model labels elsewhere are not
        # evidence that this target had an Argon upgrade configured.
        if request.get('argon_upgrade_available') is True:
            historical_argon_upgrade = True
        state = historical_argon_state(row, request, request_field)
        if state is not None:
            historical_argon_states.append(state)
        # Saved checks already contain validated earlier native/secondary
        # receipts. Preserve their OWN witness requests (G1), including the
        # pre-candidate request: rebuilding it from the later check mixes a
        # syllable hint/evidence_id into the hash and loses these hearings.
        from .acoustic_witness_adjudication import own_witness_request
        request_key = digest(request)
        first = request_key not in processed_requests
        processed_requests.add(request_key)
        direct = {k: row[k] for k in ('verdict', 'acoustic_witness') if k in row}
        if not first and not direct:
            continue
        containers = ([request] if first else []) + [direct]
        envelope = request.get('argon_escalation_evidence')
        if isinstance(envelope, Mapping):
            key = digest(envelope)
            if key not in reopened_argon:
                try:
                    with contract_boundary():
                        from .argon_escalation_integration import transcript_witness, spec_from_envelope
                        restored = transcript_witness(request, envelope)
                        reopened_argon.add(key)
                        observations.append({'witness_request': build_witness_request(request),
                            'check_request': deepcopy(dict(request)), 'verdict': restored,
                            'media_source_path': str(spec_from_envelope(envelope).source_path)})
                except (ContractRejection, ProviderFailure) as exc:
                    rejected_history.append({'request_sha256': request.get('request_sha256'), 'reason_code': exc.reason_code})
        own_requests = list(witness_requests.get(request_key, [build_witness_request(request)]))
        saved = list(_records(containers))
        for value in saved:
            if value.get('schema_version') == 'exact-final-source-transcript-observation.v2':
                source_observations[digest(value)] = deepcopy(dict(value))
        own_requests.extend(value for value in saved
            if value.get('kind') == 'subtitle_span_acoustic_witness')
        witness_requests[request_key] = own_requests
        for value in saved:
            if value.get('schema_version') != 'subtitle-span-acoustic-witness.v1':
                continue
            if (value.get('argon_escalation_evidence')
                    and digest(value['argon_escalation_evidence']) in reopened_argon):
                continue  # Already reopened once under its original G6 question.
            own = own_witness_request(value, *own_requests)
            if own is not None and same_hearing(value, target, bound_request=own):
                entry = {'witness_request': deepcopy(dict(own)), 'verdict': deepcopy(dict(value))}
                if value.get('argon_escalation_evidence'):
                    entry['check_request'] = deepcopy(dict(request))
                for wrapper in saved:
                    if wrapper.get('verdict') is value:
                        entry.update({k: deepcopy(v) for k, v in wrapper.items()
                            if k not in {'witness_request', 'verdict'}})
                observations.append(entry)
    check['prior_acoustic_observations'] = list({digest(r): r for r in observations}.values())
    if source_observations:
        check['primary_original_language_observations'] = list(source_observations.values())
    if rejected_history:
        check['terminal_rejected_history'] = list({digest(r): r for r in rejected_history}.values())
    if historical_argon_upgrade:
        check['argon_upgrade_available'] = True
    if historical_argon_states and not reopened_argon and not check.get('target_argon_state'):
        # Preserve a real owner terminal without turning it into a transcript
        # observation.  Prefer hearing-terminal evidence over structural
        # unavailability when multiple historical wrappers repeat the target.
        historical_argon_states.sort(
            key=lambda value: value['state'] != 'TERMINAL')
        check['target_argon_state'] = historical_argon_states[0]
    check['terminal_group_questions'] = [{
        'finding_sha256': digest(member),
        **{key: member.get(key) for key in ('cue_index', 'suspect', 'proposed_full_cue', 'suggestion', 'why')},
    } for member in (finding, *related_findings)]
    # Ordinary terminal checks opt into the target-level material contract.
    # Legacy persisted checks without this marker remain readable by the cold
    # proof validator and retain their historical no-new-material scope.
    check['target_material_contract'] = TARGET_MATERIAL_CONTRACT
    check.pop('request_sha256', None)
    check['request_sha256'] = digest(check)
    return check


@cold_input
@records_program_errors
def terminalize_review(srt_text, audit, *, pass_receipts, adapters):
    """Visit every unresolved row once. Only real typed keeps move buckets."""
    from .final_review_contract import is_release_resolved_keep
    from .semantic_retry_progress import SemanticAttemptRecorder
    from . import targeted_relisten
    from . import span_judgment_history
    from .targeted_relisten import compile_loop_stop_no_gain, close_with_existing_editorial
    from .finding_lifecycle import annotate, review_outcome, reenter_terminal_review

    # This owner always replaces the prior funnel below. Copy the actual
    # findings/G gates, not every other row's recursively repeated old request.
    result = deepcopy({key: value for key, value in audit.items() if key != 'terminal_funnel'})
    unavailable = unavailable_review_dependency(result, adapters)
    if unavailable is not None:
        return _defer_unavailable_review(result, pass_receipts=pass_receipts, reason=unavailable)
    unresolved, closed, attempts = [], [row for row in result.get('unresolved_findings_disclosed') or []
        if is_release_resolved_keep(row)], []
    from .exact_review_parallel import (
        exact_review_parallelism, execute_exact_review_groups, group_exact_review_agenda,
    )
    agenda = unresolved_review_rows(result)
    historical_findings = [*(adapters.get('historical_findings') or []),
                           *(result.get('findings') or []),
                           *(result.get('unresolved_findings_disclosed') or [])]
    context = adapters.get('clip_context')
    offset = adapters.get('source_media_timeline_offset_ms', 0)
    def question(finding):
        from .final_review_auditor import (build_context_adjudication_request,
            _candidate_blind_missing_proposal_scaffold)
        try:
            with contract_boundary():
                scaffold = dict(finding)
                if not scaffold.get('proposed_full_cue') and not scaffold.get('suggestion'):
                    scaffold = _candidate_blind_missing_proposal_scaffold(srt_text, finding)
                if scaffold is None:
                    return None
                return build_context_adjudication_request(srt_text, scaffold, clip_context=context,
                    source_media_timeline_offset_ms=offset, require_complete_utterance_support=True)
        except (ContractRejection, ProviderFailure):
            # The scheduler keeps an unbuildable row separate. Its ordinary
            # terminal path below retains the precise failure and prior evidence.
            return None
    groups = group_exact_review_agenda(agenda, request_for_finding=question)
    group_owner = {index: group[0] for group in groups for index in group}
    group_rows = {}
    group_history_deltas = {}

    def run_owner_with_isolated_history(owner_index, group):
        """Execute one owner against a private history buffer.

        ``copy_context`` preserves the active ContextVar binding, but a
        mutable history dict would otherwise be shared by every worker.  Keep
        the inherited state as a private copy and return only its delta; the
        ordered collector absorbs that delta back into the caller's state.
        This preserves serial history bytes and insertion order without making
        provider work serial.
        """
        inherited = span_judgment_history._active.get()
        if not isinstance(inherited, Mapping):
            return terminal_for_owner(owner_index, group), None
        local = deepcopy(dict(inherited))
        token = span_judgment_history._active.set(local)
        try:
            output = terminal_for_owner(owner_index, group)
            completed = deepcopy(local)
        finally:
            span_judgment_history._active.reset(token)
        delta = {}
        for key in ('expected', 'outcomes'):
            before = inherited.get(key) if isinstance(inherited.get(key), Mapping) else {}
            after = completed.get(key) if isinstance(completed.get(key), Mapping) else {}
            changed = {item: deepcopy(value) for item, value in after.items()
                       if item not in before or before[item] != value}
            if changed:
                delta[key] = changed
        return output, delta

    def terminal_for_owner(owner_index, group):
        """Run one G6 question and shape every member's ordered result.

        The callback itself is the scheduler's one unit of provider work for a
        group.  Members are projected here, then returned one-at-a-time by
        ``prepare`` so the scheduler's ordered collector can merge rows and
        attempts exactly as the serial loop did.
        """
        finding = agenda[owner_index]
        row = deepcopy(finding)
        row.pop('terminal_funnel_error', None)
        prior = row.get('exact_release_adjudication') or {}
        try:
            with contract_boundary():
                check = terminal_check(srt_text, row, clip_context=context, offset_ms=offset,
                    ranges=adapters.get('prior_range_acoustic_observations') or (),
                    secondary=adapters.get('prior_secondary_audio_observations') or (),
                    related_findings=[agenda[i] for i in group if i != owner_index],
                    historical_findings=historical_findings)
                from .editorial_subtitle import replay_terminal_adjudication, needs_contract_retry
                terminal = replay_terminal_adjudication(srt_text, row,
                    check_request=check, clip_context=context)
                if needs_contract_retry(terminal):
                    # A rejected old-contract answer is re-asked under the current
                    # contract by the ordinary owner below; prior stays stopped history.
                    terminal = None
                replayed_original = terminal is not None
                if terminal is not None:
                    proof = ((terminal.get('terminal_review') or {}).get('request') or {}).get(
                        'audio_gain_termination') or {'stop_reason_code': prior.get('reason_code'), 'proof_sha256': None}
                    owner, recorder = None, None
                else:
                    # D owns target-level material completion before the terminal proof.
                    completed = targeted_relisten.complete_target_materials(
                        check=check, finding=row, adapters=adapters)
                    if completed is not None:
                        check = completed
                    # Target-material completion may append its own attempt receipt to
                    # ``row``.  The stopped pass binds the agenda finding that was
                    # actually recorded; the updated check carries the new evidence.
                    proof = compile_loop_stop_no_gain(check=check, finding=finding,
                        pass_receipts=pass_receipts)
                    row = reenter_terminal_review(row, check_request=check,
                        no_gain_proof=proof, clip_context=context)
                    call = adapters.get('judge_llm_call')
                    if not adapters.get('provider_calls_allowed', True) or call is None:
                        raise ProviderFailure('TERMINAL_JUDGE_UNAVAILABLE', provider='terminal_judge')
                    owner = adapters.get('terminal_owner')
                    recorder = getattr(call, '_semantic_attempt_recorder', None)
                    if not callable(owner) and not isinstance(recorder, SemanticAttemptRecorder):
                        recorder = SemanticAttemptRecorder(srt_text=srt_text, finding=row,
                            clip_context=context, offset_ms=offset)
                        call = recorder.wrap(call)
                    recovery = {'terminal_check_request': check, 'no_gain_proof': proof}
                    if callable(owner):
                        owner_prepare = getattr(owner, 'prepare', None)
                        if callable(owner_prepare):
                            # The scheduler's native-turn gate makes logical budget
                            # admission agenda-ordered while the returned callback
                            # performs provider I/O concurrently. Always close this
                            # item's gate, including a failed preparation, so later
                            # independent groups cannot wait forever.
                            from .exact_review_parallel import finish_native_admission, wait_for_native_turn
                            try:
                                wait_for_native_turn(require_open=True)
                                prepared = owner_prepare(srt_text, row, recovery, prior)
                            finally:
                                finish_native_admission()
                            terminal = prepared()
                        else:
                            terminal = owner(srt_text, row, recovery, prior)
                    else:
                        terminal = close_with_existing_editorial(srt_text=srt_text, finding=row,
                            recovery_audit=recovery, prior=prior, clip_context=context,
                            entity_verifier=adapters.get('entity_verifier'), judge_llm_call=call)
                terminal['stopped_adjudication'] = prior
                if (isinstance(terminal.get('terminal_review'), Mapping)
                        and isinstance(terminal['terminal_review'].get('raw_completion'), str)
                        and terminal.get('reason_code') not in {None, 'TERMINAL_UNRESOLVABLE'}
                        and terminal.get('repaired') is not True):
                    terminal['failure_layer'] = 'CONTRACT_ERROR'
                returned_check = ((terminal.get('terminal_review') or {}).get('request') or {}).get('check_request')
                if returned_check is not None:
                    from .audio_question_identity import same_audio_question
                    if not same_audio_question(check, returned_check,
                            compare_proposed=False, strict_source=True)[0]:
                        raise ContractRejection('TERMINAL_GROUP_ANSWER_QUESTION_MISMATCH')
                terminal['terminal_question_members'] = [digest(agenda[i]) for i in group]
                # Original rows are already bound by the stopped proof. Member
                # projection needs their identities, not n copies of all n rows.
                terminal['terminal_question_originals'] = list(terminal['terminal_question_members'])
                if not callable(owner) and recorder is not None and (recorder.events or recorder.blocked):
                    terminal['semantic_retry_progress'] = recorder.audit()
                rebuilt = terminal.get('rebuilt_finding')
                if isinstance(rebuilt, Mapping):
                    row.update(deepcopy({key: value for key, value in rebuilt.items()
                        if key not in {'lifecycle', 'lifecycle_reentries',
                                       'exact_release_adjudication', 'context_audio_adjudication'}}))
                    for key in ('cue_index', 'suspect', 'span_start_codepoint', 'span_end_codepoint',
                                'origin_span_obligation'):
                        if key in finding:
                            row[key] = finding[key]
                        else:
                            row.pop(key, None)
                row['exact_release_adjudication'] = terminal
                member = {'row': row, 'attempt': {
                    'cue_index': row['cue_index'], 'original_reason_code': proof['stop_reason_code'],
                    'proof_sha256': proof['proof_sha256'],
                    'reason_code': terminal.get('reason_code') or terminal.get('policy_branch'),
                    'terminal_review': deepcopy(terminal.get('terminal_review'))},
                    'closed': is_release_resolved_keep(row)}
                output = {owner_index: member}
                for index in group:
                    if index == owner_index:
                        continue
                    member_row = deepcopy(agenda[index])
                    member_terminal = dict(terminal)
                    rebuilt = member_terminal.get('rebuilt_finding')
                    if isinstance(rebuilt, Mapping):
                        member_row.update(deepcopy({key: value for key, value in rebuilt.items()
                            if key not in {'lifecycle', 'lifecycle_reentries',
                                           'exact_release_adjudication', 'context_audio_adjudication'}}))
                        for key in ('cue_index', 'suspect', 'span_start_codepoint', 'span_end_codepoint',
                                    'origin_span_obligation'):
                            if key in agenda[index]:
                                member_row[key] = agenda[index][key]
                            else:
                                member_row.pop(key, None)
                    if not replayed_original:
                        member_row = reenter_terminal_review(member_row, check_request=check,
                            no_gain_proof=proof, clip_context=context)
                    member_row['exact_release_adjudication'] = member_terminal
                    member_row['terminal_question_owner'] = owner_index
                    output[index] = {'row': member_row, 'attempt': {
                        'cue_index': member_row['cue_index'], 'shared_question_owner': owner_index,
                        'reason_code': member_terminal.get('reason_code') or member_terminal.get('policy_branch')},
                        'closed': is_release_resolved_keep(member_row)}
                return output
        except (ContractRejection, ProviderFailure) as exc:
            classification = review_error_record(exc)
            error = classification['reason_code']
            output = {}
            recovery = classification.get('material_recovery')
            failure = classification['status']
            row['terminal_funnel_error'] = {'reason_code': error, 'status': failure,
                'prior_evidence_retained': True, 'review_error': classification}
            if recovery is not None:
                row['terminal_funnel_error']['material_recovery'] = deepcopy(recovery)
            output[owner_index] = {'row': row, 'attempt': {
                'cue_index': row.get('cue_index'), 'reason_code': error}, 'closed': False}
            for index in group:
                if index == owner_index:
                    continue
                member_row = deepcopy(agenda[index])
                member_row['terminal_funnel_error'] = {
                    'reason_code': error, 'status': failure, 'prior_evidence_retained': True,
                    'review_error': classification}
                if 'material_recovery' in row['terminal_funnel_error']:
                    member_row['terminal_funnel_error']['material_recovery'] = deepcopy(
                        row['terminal_funnel_error']['material_recovery'])
                output[index] = {'row': member_row, 'attempt': {
                    'cue_index': member_row.get('cue_index'), 'reason_code': error}, 'closed': False}
            return output

    def prepare(index):
        owner_index = group_owner[index]
        group = next(group for group in groups if group[0] == owner_index)
        if index == owner_index:
            def run_owner():
                result, history_delta = run_owner_with_isolated_history(owner_index, group)
                group_rows[owner_index] = result
                group_history_deltas[owner_index] = history_delta
                return result[index]
            return run_owner

        def run_member():
            return group_rows[owner_index][index]
        return run_member

    def collect(index, member):
        if member is None:
            return
        owner_index = group_owner[index]
        if index == owner_index:
            delta = group_history_deltas.pop(owner_index, None)
            state = span_judgment_history._active.get()
            if isinstance(delta, Mapping) and isinstance(state, dict):
                for key in ('expected', 'outcomes'):
                    values = delta.get(key)
                    if isinstance(values, Mapping):
                        target = state.setdefault(key, {})
                        if isinstance(target, dict):
                            target.update(deepcopy(dict(values)))
        row = annotate(member['row'], terminal=True)
        attempts.append(member['attempt'])
        (closed if member['closed'] else unresolved).append(row)

    execute_exact_review_groups(groups, parallelism=exact_review_parallelism(), prepare=prepare,
        collect_result=collect)
    result.update(findings=unresolved, validated_finding_count=len(unresolved),
        unresolved_findings_disclosed=closed,
        terminal_funnel={'schema_version': 'exact-final-terminal-funnel.v1',
            'pass_receipts': deepcopy(pass_receipts), 'attempts': attempts})
    usage = adapters.get('terminal_usage')
    if callable(usage):
        previous = deepcopy(result.get('cpa_adjudication_usage') or {})
        previous['provider_adjudication_count'] = max(usage(), previous.get('provider_adjudication_count') or 0)
        result['cpa_adjudication_usage'] = previous
    return review_outcome(result, terminal=True)
