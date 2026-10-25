"""One exhausted-material judgment, compiled by the existing SUB-18/editorial owners."""
from __future__ import annotations

from .review_errors import ContractRejection

from .review_evidence_store import cold_input

from copy import deepcopy
import hashlib
import json
from functools import lru_cache

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.meaning_difference import classify_difference, equivalence_key, lexical
from src.autoslice.refrain_detection import detect_refrains, refrain_support
from src.autoslice.subtitle_acceptability_policy import load_policy, validate_policy
from src.autoslice.final_review_contract import CURRENT_UTTERANCE_KEEP_BRANCH, SUB18_KEEP_BRANCH
from src.autoslice.editorial_edit_contract import apply_edit, edit_contract

MARKER = '# 字幕材料穷尽终裁 v1'
KEEP_ROUTES = {
    'B_EVIDENCE_SUPPORTS_CURRENT': {'grades': ['A', 'B'], 'branch': CURRENT_UTTERANCE_KEEP_BRANCH},
    'C_MEANING_EQUIVALENT': {'grades': ['C'], 'branch': SUB18_KEEP_BRANCH},
}


def _is_keep(request, answer):
    if answer.get('choice') == 'CURRENT':
        return True
    current = request.get('current_cue', request['check_request'].get('current_cue', ''))
    if answer.get('choice') != 'CONTEXT_FIT':
        return False
    return apply_edit(current, answer) == current


def current_support_refs(request, *, legacy=False):
    """Only real observed target readings can ground the B CURRENT route."""
    from src.autoslice.targeted_relisten import _editorial_guard_tokens
    current = request.get('current_cue', request['check_request'].get('current_cue', ''))
    supported = []
    for row in request['audio_gain_termination']['projection']['observations']:
        raw = row.get('raw_observation', {})
        if raw.get('status') != 'OBSERVED':
            continue
        if row.get('receipt_kind') == 'blind_pinyin':
            if (not raw.get('uncertain_positions') and raw.get('heard_pinyin', '').split()
                    == _editorial_guard_tokens(current)):
                supported.append(row['observation_id'])
        elif candidate_evidence_support(current, [row], substantive=not legacy)['supported']:
            supported.append(row['observation_id'])
    return supported


def available_keep_routes(request, *, legacy=False):
    """Return the keep routes reachable from this request's real evidence.

    The B route is an exact-current claim. It is therefore available only
    when the existing current-support predicate found at least one complete
    observation for the current cue. Target span labels and clear-audio guard
    fragments do not widen that predicate to the whole cue.
    """
    routes = dict(KEEP_ROUTES)
    if not current_support_refs(request, legacy=legacy):
        routes.pop('B_EVIDENCE_SUPPORTS_CURRENT', None)
    return routes


# The prompt/material contract the current producer renders. Older recorded
# requests keep their own rendered materials; they are never rebound to this.
TERMINAL_CONTRACT_VERSION = 4
TERMINAL_CONTRACT_FEATURES = {
    1: {'legacy_materials': True, 'legacy_support': True, 'lifecycle_prompt': False,
        'written_meaning_proof': False, 'qualified_clear_audio': False, 'reask_under_current_contract': True},
    2: {'legacy_materials': False, 'legacy_support': True, 'lifecycle_prompt': False,
        'written_meaning_proof': False, 'qualified_clear_audio': False, 'reask_under_current_contract': True},
    3: {'legacy_materials': False, 'legacy_support': False, 'lifecycle_prompt': True,
        'written_meaning_proof': True, 'qualified_clear_audio': False, 'reask_under_current_contract': True},
    4: {'legacy_materials': False, 'legacy_support': False, 'lifecycle_prompt': True,
        'written_meaning_proof': True, 'qualified_clear_audio': True, 'reask_under_current_contract': False},
}


def terminal_contract_features(request):
    """Resolve the recorded contract once; unknown versions have no fallback."""
    version = request.get('terminal_contract_version', 1)
    if type(version) is not int or version not in TERMINAL_CONTRACT_FEATURES:
        raise ContractRejection('TERMINAL_CONTRACT_VERSION_UNSUPPORTED')
    return TERMINAL_CONTRACT_FEATURES[version]


def offered_keep_contract(data, *, legacy_v1_answer=False):
    """Keep routes and B citations exactly as the rendered materials offered.

    A route or support citation is release authorization. It is read from the
    same materials that rendered the question, never recomputed under a later
    policy: a relaxed support predicate cannot authorize an old answer whose
    prompt offered no support citation. The v1 answer contract had only the
    complete meaning-equivalence keep (``decode_answer`` derives that route).
    """
    if legacy_v1_answer:
        return {'C_MEANING_EQUIVALENT': KEEP_ROUTES['C_MEANING_EQUIVALENT']}, []
    routes = data.get('keep_routes')
    support = data.get('current_support_observation_refs')
    if not isinstance(routes, dict) or not isinstance(support, list):
        raise ContractRejection('TERMINAL_OFFERED_KEEP_CONTRACT_UNAVAILABLE')
    return {name: KEEP_ROUTES[name] for name in routes if name in KEEP_ROUTES}, list(support)


def terminal_contract_retry(request):
    """How a rejected recorded terminal answer recovers; never a release decision.

    An answer recorded under an older material/prompt contract is re-asked
    under the current contract (a new question version, so the semantic ruling
    key differs). A rejected answer to the current contract is reused and
    revalidated only after a consumer repair or new material: re-asking the
    same question is not a retry.
    """
    recorded = request.get('terminal_contract_version')
    recorded = 1 if recorded is None else recorded  # recorded production requests carry no stamp
    older = terminal_contract_features({'terminal_contract_version': recorded})['reask_under_current_contract']
    return {'family': 'terminal', 'recorded_contract_version': recorded,
            'current_contract_version': TERMINAL_CONTRACT_VERSION,
            'kind': 'REASK_UNDER_CURRENT_CONTRACT' if older else 'REVALIDATE_AFTER_CONSUMER_REPAIR_OR_NEW_MATERIAL',
            'reask_under_current_contract': older}


def grade_a_witness_contract(request):
    """The existing A threshold and witness identity, shared with transport."""
    return {
        'receipt_kind': 'blind_range',
        'independence_fields': ['provider', 'model'],
        'minimum_distinct_witnesses': request['terminal_policy']['grades']['A']['min_independent_wide_witnesses'],
    }


def grade_a_witnesses(request, refs):
    contract = grade_a_witness_contract(request)
    return {identity
        for row in request['audio_gain_termination']['projection']['observations']
        if row['observation_id'] in refs
        and (identity := independent_wide_identity(row, contract)) is not None}


def independent_wide_identity(row, contract):
    """Read the same bound provider/model from receipts or their model view."""
    if row.get('receipt_kind') != contract['receipt_kind']:
        return None
    raw = row.get('raw_observation') or {}
    candidates = [raw, *raw.get('quality_and_provider', [])]
    identities = {tuple(value.get(key) for key in contract['independence_fields'])
        for value in candidates
        if all(isinstance(value.get(key), str) and value[key].strip()
            for key in contract['independence_fields'])}
    return next(iter(identities)) if len(identities) == 1 else None


def candidate_evidence_support(text, observations, *, cues=(), policy=None, span_text=None, refrains=None,
                               substantive=False):
    """Deterministic support summary, never votes or a release authority.

    Provider transcripts/word projections retain their original source labels;
    matching words in subtitle context alone is not acoustic support.
    """
    policy = policy or load_policy()
    from .nonsemantic_sound import substantive_text
    comparison = substantive_text if substantive else lambda value: value
    full_key = equivalence_key(comparison(text), policy)
    span_key = equivalence_key(span_text, policy) if span_text else ''
    matches = []
    def visit(value, origin, usable=False):
        if isinstance(value, dict):
            usable = value.get('status') in {'OBSERVED', 'UNCERTAIN'} or usable
            for field, child in value.items():
                if usable and field in {'transcript', 'exact_transcript', 'full_transcript', 'heard'} and isinstance(child, str):
                    heard = equivalence_key(comparison(child), policy)
                    if full_key and full_key in heard:
                        matches.append({'origin': origin, 'field': field, 'quote': child, 'scope': 'CUE_TEXT'})
                    elif span_key and span_key in heard:
                        matches.append({'origin': origin, 'field': field, 'quote': child,
                            'scope': 'TARGET_SPAN_ONLY_OUTSIDE_OBLIGATIONS_RETAINED'})
                elif isinstance(child, (dict, list)):
                    visit(child, origin, usable)
        elif isinstance(value, list):
            for child in value:
                visit(child, origin, usable)
    for i, row in enumerate(observations):
        visit(row, row.get('observation_id', row.get('response_sha256', str(i))))
    # A saved JSON object's key order is representation, not evidence order.
    # Prompt reconstruction after persistence must produce identical lists.
    matches = [value for _, value in sorted({
        json.dumps(value, ensure_ascii=False, sort_keys=True): value for value in matches}.items())]
    refrains = (detect_refrains(cues, policy=policy) if cues else []) if refrains is None else refrains
    support = refrain_support(text, refrains, policy=policy)
    return {'transcript_matches': matches, 'refrain_support': support,
        'scope': ('CUE_TEXT' if any(m['scope'] == 'CUE_TEXT' for m in matches)
            else 'TARGET_SPAN_ONLY_OUTSIDE_OBLIGATIONS_RETAINED' if span_key else 'CUE_TEXT'),
        'supported': bool(matches or support), 'authority': 'EVIDENCE_ONLY'}


@lru_cache(maxsize=16)
def _cached_session_restatements(srt_text):
    """Pure transcript analysis shared across questions, never receipt I/O."""
    from src.autoslice.restatement_recall import discover_restatement_findings
    cues = parse_srt_cues(srt_text)
    end = max((c.end_ms for c in cues), default=0) / 1000
    return discover_restatement_findings(srt_text, max_gap_seconds=max(90, end))


@lru_cache(maxsize=32)
def _cached_refrains(srt_text, policy_json):
    return detect_refrains(parse_srt_cues(srt_text), policy=json.loads(policy_json))


def _without_planning(value):
    if isinstance(value, dict):
        return {k: _without_planning(v) for k, v in value.items()
            if 'needs_audio' not in k and k not in {'prompt', 'raw_completion', 'raw_response',
                'prior_audio_adjudication', 'history_audio_recovery'}}
    if isinstance(value, list):
        return [_without_planning(v) for v in value]
    return value


def stopped_evidence_summary(finding, *, projection=None, stopped_sha256=None):
    """Project saved evidence, not the recursively repeated audit/request trees.

    The proof retains the entire stopped row and its hash. Every distinct
    diagnostic, hearing and judge decision remains represented with its own
    source/request/response identity; old audit observations are not promoted
    to the validated projection or counted as new listening.
    """
    if not isinstance(finding, dict):
        return None
    finding_view = {k: finding.get(k) for k in ('cue_index', 'repair_class', 'suspect',
        'suggestion', 'proposed_full_cue', 'why', 'content_risk', 'risk_basis')}
    if projection is not None:
        # The ordinary cold projector has already selected this physical
        # question's hearings. Do not walk every historical judge/request
        # snapshot back into the model. Full old bindings stay in the proof.
        return {'finding': finding_view, 'stopped_audit_sha256': stopped_sha256,
            'recorded_evidence': sorted({row['observation_id'] for row in projection['observations']}),
            'rejected_evidence': projection.get('rejected_observations', []),
            'current_audio_diagnostic': model_evidence(finding.get('exact_release_adjudication') or {}),
            'authority': 'All cold-validated historical and current hearings are in observations; full audit retained by hash.'}
    fields = {'status', 'reason',
        'request_sha256', 'check_request_sha256', 'prompt_sha256', 'completion_sha256',
        'response_sha256', 'source_media_sha256', 'audio_clip_sha256', 'task_id',
        'transcript', 'exact_transcript', 'heard', 'heard_pinyin', 'uncertainties',
        'target_audible', 'candidate_exposure', 'witness_protocol', 'grade', 'best_guess'}
    seen = {}
    def visit(value):
        if isinstance(value, dict):
            selected = {key: child for key, child in value.items() if key in fields
                and (not isinstance(child, (dict, list)) or key == 'uncertainties')}
            decision_container = any(key in value for key in (
                'choice', 'decision', 'decision_authority', 'policy_branch', 'repaired'))
            if not decision_container and (any(key in selected for key in (
                    'transcript', 'exact_transcript', 'heard', 'heard_pinyin', 'uncertainties'))
                    or (selected.get('status') == 'UNCERTAIN' and selected.get('reason')
                        and ('hearing' in value or 'witness_protocol' in value))):
                encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True)
                seen[hashlib.sha256(encoded.encode()).hexdigest()] = selected
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(finding.get('exact_release_adjudication') or {})
    return {'finding': finding_view,
        'recorded_evidence': [seen[key] for key in sorted(seen)],
        'stop': {k: finding.get('exact_release_adjudication', {}).get(k)
            for k in ('status', 'reason_code', 'policy_branch')},
        'authority': 'AUDIT_CONTEXT_ONLY; cold-validated historical hearings are in observations'}


def terminal_observation_projection(request):
    """Canonical model/guard view; remove planning payloads in one place."""
    projection = request['audio_gain_termination']['projection']
    if any('raw_observation' in row and 'target_text' not in row for row in projection['observations']):
        from src.autoslice.targeted_relisten import replay_no_gain_projection
        projection = replay_no_gain_projection(request['audio_gain_termination'], request['check_request'],
            include_target_text=True)
    view = _without_planning(projection)
    view['observations'] = [model_observation(row) for row in view['observations']]
    return view


def model_evidence(value):
    """A semantic projection, never a recursive audit/request serialization."""
    if not isinstance(value, dict):
        return value
    texts, metadata = {}, {}
    text_fields = {'transcript', 'exact_transcript', 'full_transcript', 'heard', 'heard_pinyin'}
    fields = {'status', 'provider', 'model', 'target_audible', 'candidate_exposure', 'reason',
        'witness_protocol', 'uncertainties', 'uncertain_positions', 'self_count_mismatch',
        'observed_syllable_count', 'syllable_count', 'count_uncertain', 'can_sustain_question_alone'}
    def visit(node):
        if not isinstance(node, dict):
            return
        selected = {k: v for k, v in node.items() if k in fields}
        if selected:
            metadata[json.dumps(selected, ensure_ascii=False, sort_keys=True)] = selected
        for key in text_fields:
            text = node.get(key)
            if isinstance(text, str) and text:
                texts[(key, text)] = {'field': key, 'text': text}
        # Segment text is a single reading of this observation. Repeating a
        # timestamped word table in addition to that reading is not evidence.
        for key in ('native_segments', 'segments'):
            rows = node.get(key)
            if isinstance(rows, list):
                text = ' '.join(r['text'] for r in rows if isinstance(r, dict) and isinstance(r.get('text'), str))
                if text and not any(text == t for _, t in texts):
                    texts[(key, text)] = {'field': key, 'text': text}
        for key in ('native_observation', 'verdict', 'original_provider_response', 'audio_partition',
                'argon_escalation', 'argon_uncertainty_evidence', 'hearing'):
            visit(node.get(key))
    visit(value)
    return {'status': value.get('status'), 'readings': [texts[k] for k in sorted(texts)],
        'quality_and_provider': [metadata[k] for k in sorted(metadata)]}


def model_observation(row):
    return {key: (model_evidence(value) if key == 'raw_observation' else value)
        for key, value in row.items() if key in {'observation_id', 'receipt_kind', 'target_text',
            'source_media_sha256', 'usable_status', 'raw_observation', 'input_information'}}


def _factor_repeated_readings(data):
    """Lossless model-only string interning; full receipts never change."""
    from collections import Counter
    original = deepcopy(data)
    old_texts = data.pop('reading_texts', {})
    data.pop('reading_reference_contract', None)
    readings = [reading for row in data['observations']
        for reading in row.get('raw_observation', {}).get('readings', [])]
    for reading in readings:
        if 'text_ref' in reading:
            reading['text'] = old_texts[reading.pop('text_ref')]
    repeated = Counter(reading['text'] for reading in readings if isinstance(reading.get('text'), str))
    catalogue = {}
    for reading in readings:
        value = reading.get('text')
        if isinstance(value, str) and len(value) >= 40 and repeated[value] > 1:
            identity = 'text:' + hashlib.sha256(value.encode()).hexdigest()[:16]
            if identity in catalogue and catalogue[identity] != value:
                raise ContractRejection('TERMINAL_READING_REFERENCE_COLLISION')
            catalogue[identity] = value
            reading.pop('text')
            reading['text_ref'] = identity
    if catalogue:
        data['reading_texts'] = catalogue
        data['reading_reference_contract'] = 'Each observations.raw_observation.readings[].text_ref expands to reading_texts[text_ref]; identical text is stored once, with each independent observation retained.'
    if len(json.dumps(data, ensure_ascii=False)) >= len(json.dumps(original, ensure_ascii=False)):
        data.clear()
        data.update(original)


def _compact_model_receipt_metadata(data):
    """Use the existing observation catalogue for repeated receipt identities.

    This changes only the model's receipt bookkeeping. Source facts, every
    independent hearing, all readings and quality diagnostics stay intact;
    the full original cryptographic bindings remain in the cold receipt.
    """
    indexes = {row['observation_id']: i for i, row in enumerate(data['observations'])}
    stopped = data.get('stopped_finding_evidence') or {}
    recorded = stopped.get('recorded_evidence')
    if isinstance(recorded, list) and all(identity in indexes for identity in recorded):
        stopped['recorded_evidence'] = {'observation_indexes': [indexes[identity] for identity in recorded]}
    stopped.pop('stopped_audit_sha256', None)
    exhaustion = data.get('material_exhaustion') or {}
    target = exhaustion.get('target_material') or {}
    channels = target.get('completed_channels')
    if isinstance(channels, dict):
        target['completed_channels'] = {channel: [
            {'observation_index': indexes[identity]} if isinstance(identity, str) and identity in indexes else identity
            for identity in identities] for channel, identities in channels.items()}
    # Hashes are audit bindings rather than linguistic context. Keep the
    # contextual values and source-clock geometry in the same structure.
    def without_hashes(value):
        if isinstance(value, dict):
            return {key: without_hashes(child) for key, child in value.items()
                if not key.endswith(('_sha256', '_sha256s'))}
        if isinstance(value, list):
            return [without_hashes(child) for child in value]
        return value
    data['whole_session_and_session_context'] = without_hashes(data['whole_session_and_session_context'])
    if target.get('physical_target'):
        target['physical_target'] = without_hashes(target['physical_target'])
    data['receipt_reference_contract'] = 'observation_index and observation_indexes address the zero-based observations catalogue. Full hashes and source bindings remain in the cold receipt; every hearing and source fact is retained here.'


def _factor_quality_metadata(data):
    """Store repeated, identical provider diagnostics once without merging hearings."""
    from collections import Counter
    original = deepcopy(data)
    old_quality = data.pop('quality_catalogue', [])
    data.pop('quality_reference_contract', None)
    containers = [row.get('raw_observation', {}) for row in data['observations']]
    containers.append((data.get('stopped_finding_evidence') or {}).get('current_audio_diagnostic', {}))
    for value in containers:
        if 'quality_and_provider' in value:
            value['quality_and_provider'] = [old_quality[row['quality_index']]
                if 'quality_index' in row else row for row in value['quality_and_provider']]
    encoded = lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    counts = Counter(encoded(row) for value in containers for row in value.get('quality_and_provider', []))
    catalogue = sorted(key for key, count in counts.items() if count > 1 and len(key) >= 60)
    indexes = {key: i for i, key in enumerate(catalogue)}
    for value in containers:
        if 'quality_and_provider' in value:
            value['quality_and_provider'] = [
                {'quality_index': indexes[encoded(row)]} if encoded(row) in indexes else row
                for row in value['quality_and_provider']]
    if catalogue:
        data['quality_catalogue'] = [json.loads(key) for key in catalogue]
        data['quality_reference_contract'] = 'quality_index addresses zero-based quality_catalogue; each occurrence retains its original observation and all diagnostics expand exactly.'
    if len(json.dumps(data, ensure_ascii=False)) >= len(json.dumps(original, ensure_ascii=False)):
        data.clear()
        data.update(original)


def model_context(context, check):
    """Keep session facts and local transcript context, omit audit machinery."""
    result = {k: v for k, v in context.items() if k not in {
        'whole_session_transcripts', 'whole_clip_draft_srt', 'speech_memory_ledger',
        'structured_chat', 'source_human_evidence'}}
    # Chat already has the ordinary _events target-window projection. Human
    # slots retain the context producer's local_windows, compared on G6's
    # source-media clock rather than the later recut's local cue clock.
    from .audio_question_identity import physical_target
    start, end = physical_target(check)
    human = context.get('source_human_evidence')
    if isinstance(human, dict):
        entries = [entry for entry in human.get('entries', []) if any(
            window['start_ms'] < end and window['end_ms'] > start
            for window in entry.get('local_windows', []))]
        if entries:
            result['source_human_evidence'] = {**human, 'entries': entries}
    result['draft_scope'] = 'Earlier draft retained in the full receipt; current cue catalogue below is not audio truth.'
    result['whole_session_transcripts'] = []
    for session in context.get('whole_session_transcripts', []):
        cues = parse_srt_cues(session['srt_text'])
        local = [c for c in cues if c.end_ms >= start - 30_000 and c.start_ms <= end + 30_000]
        result['whole_session_transcripts'].append({
            **{k: session[k] for k in ('recording_sha256', 'srt_sha256') if k in session},
            'scope': 'SOURCE_CLOCK_CONTEXT_ONLY',
            'cues': [{'start_ms': c.start_ms, 'end_ms': c.end_ms, 'text': c.text} for c in local]})
    return result


@cold_input
def terminal_materials(request):
    """Produce ALL model materials and their exact citation index together.

    Schema integration seam: terminal_schema(request) must use
    list(terminal_materials(request)[1]) as evidence_refs.items.enum.
    Values are JSON paths into the returned materials, never guessed aliases.
    """
    features = terminal_contract_features(request)
    if features['legacy_materials']:
        from .terminal_legacy_v1 import terminal_materials as legacy_materials
        return legacy_materials(request)
    policy = validate_policy(request['terminal_policy'])
    check = request['check_request']
    proof = request['audio_gain_termination']
    cues = parse_srt_cues(check.get('whole_clip_current_srt', ''))
    refrains = deepcopy(_cached_refrains(check.get('whole_clip_current_srt', ''), json.dumps(policy, sort_keys=True)))
    session_restatements = []
    sessions = request['terminal_context'].get('whole_session_transcripts', [])
    for row in sessions:
        findings, receipt = _cached_session_restatements(row['srt_text'])
        session_restatements.append({'recording_sha256': row['recording_sha256'],
            'srt_sha256': row['srt_sha256'], 'scope': 'WHOLE_SESSION_SOURCE_CLOCK_CONTEXT_ONLY',
            'candidates': findings, 'refrain_context': receipt.get('refrain_context', []),
            'refrain_excluded_pairs': receipt.get('refrain_excluded_pairs', [])})
    current = request.get('current_cue', check.get('current_cue', ''))
    indexes = set(check.get('cue_indexes', []))
    for index in list(indexes):
        indexes.update(range(max(1, index - 4), min(len(cues), index + 4) + 1))
    for value in check.get('evidence_cue_ids', []):
        try:
            indexes.add(int(str(value).removeprefix('cue:')))
        except (ValueError, TypeError):
            pass
    for refrain in refrain_support(current, refrains, policy=policy):
        indexes.update(refrain['cue_indexes'])
    context_sources = {key: value['text'] for key, value in proof['context_sources'].items()
        if not key.startswith('cue:') or int(key[4:]) in indexes}
    event_sources, events = {}, []
    for event in request.get('events', []):
        source = {key: event[key] for key in ('source', 'source_sha256') if key in event}
        source_ref = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()[:16]
        event_sources[source_ref] = source
        events.append({**{k: v for k, v in event.items() if k not in source}, 'source_ref': source_ref})
    support = candidate_evidence_support(current, proof['projection']['observations'],
        cues=cues, policy=policy, refrains=refrains)
    # Readings already appear once, under their observation identity. The
    # support index points there instead of copying a whole wide transcript
    # for every matching native/provider field in the same observation.
    support['transcript_matches'] = [json.loads(value) for value in sorted({
        json.dumps({k: v for k, v in match.items() if k != 'quote'},
            ensure_ascii=False, sort_keys=True) for match in support['transcript_matches']})]
    support['reading_source'] = 'observations[observation_id=origin].raw_observation.readings'
    data = {'policy': {k: v for k, v in policy.items() if k != 'features'}, 'current': current,
        'proposed': check.get('proposed_cue'), 'cue_index': request.get('cue_index'),
        'whole_clip_scope': 'Target context, nominated evidence cues and matching full-clip refrains are projected once as cue:N; full clip retained in receipt. Context only, not audio truth.',
        'whole_session_and_session_context': model_context(request['terminal_context'], check),
        'whole_session_transcript_status': 'FULL_RECEIPT_RETAINED_LOCAL_PROJECTION_AND_FULL_SESSION_RESTATEMENT_SEARCH' if sessions else 'NOT_PROVIDED',
        'whole_session_restatements': session_restatements,
        'glossary': request['terminal_glossary'],
        'refrains': refrains, 'current_refrain_support': refrain_support(current, refrains, policy=policy),
        'observations': terminal_observation_projection(request)['observations'],
        'audio': {'observation_ids': [r['observation_id'] for r in proof['projection']['observations']],
            'scope': 'THE_SAME_COLD_VALIDATED_OBSERVATIONS; not a second witness'},
        'material_exhaustion': deepcopy(proof.get('material_exhaustion')),
        'no_gain_proof_sha256': proof.get('proof_sha256'),
        'context_sources': context_sources,
        'events': events, 'event_sources': event_sources,
        'context_before': request.get('context_before'), 'context_after': request.get('context_after'),
        'current_support_summary': support,
        'editorial_request_sha256': hashlib.sha256(json.dumps(request, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    data['keep_routes'] = available_keep_routes(request,
        legacy=features['legacy_support'])
    if request.get('answer_contract_version'):
        data['answer_contract_version'] = request['answer_contract_version']
    target_material = (data.get('material_exhaustion') or {}).get('target_material') or {}
    owner_state = target_material.get('argon')
    if isinstance(owner_state, dict):
        # Owner requests/audits are retained in the proof. The model needs
        # their completed-channel conclusion, rather than another recursive
        # copy of all earlier evidence and prompts.
        target_material['argon'] = {key: owner_state[key] for key in ('state', 'status', 'reason')
            if key in owner_state}
    data['terminal_group_questions'] = check.get('terminal_group_questions', [])
    data['edit_contract'] = edit_contract(current)
    data['current_support_observation_refs'] = current_support_refs(request,
        legacy=features['legacy_support'])
    from src.autoslice.targeted_relisten import terminal_clear_audio_catalogue
    data['clear_audio_constraints'] = terminal_clear_audio_catalogue(
        terminal_observation_projection(request), current_text=check['current_cue'],
        qualification_policy=policy if features['qualified_clear_audio'] else None)
    if proof.get('stopped_finding') is not None:
        data.update(stopped_finding_evidence=stopped_evidence_summary(proof['stopped_finding'],
                projection=proof['projection'], stopped_sha256=proof.get('stopped_finding_sha256')),
            stop_reason_code=proof['stop_reason_code'])
    data = _without_planning(data)
    refs = _material_refs(data)
    if len(_render_terminal_prompt(data, refs)) > 30_000:
        # Large catalogues use a reference index for repeated support labels,
        # just as the evidence store does for repeated audit objects. No
        # reading or quality assessment is truncated or dropped.
        matches = {}
        for match in support['transcript_matches']:
            matches.setdefault(match['origin'], set()).add(match['scope'])
        for row in data['observations']:
            scopes = matches.get(row['observation_id'])
            if scopes:
                row['current_support_scopes'] = sorted(scopes)
        data['current_support_summary']['transcript_matches'] = 'See each observation.current_support_scopes and its readings.'
        data['current_support_summary'].pop('reading_source', None)
        data['audio'] = {'scope': 'observations is the complete cold-validated audio catalogue; not a second witness'}
        for row in data['observations']:
            row.pop('source_media_sha256', None)  # Sealed in the full receipt.
        for row in data['terminal_group_questions']:
            row.pop('finding_sha256', None)
        for key in ('editorial_request_sha256', 'no_gain_proof_sha256'):
            data.pop(key, None)
        for key in ('context_sha256', 'whole_clip_draft_srt_sha256', 'schema_version'):
            data['whole_session_and_session_context'].pop(key, None)
        _compact_model_receipt_metadata(data)
        _factor_repeated_readings(data)
        _factor_quality_metadata(data)
        refs = _material_refs(data)
    return data, refs


def _material_refs(data):
    from .prompt_materials import terminal_material_index
    return terminal_material_index(data)


def terminal_prompt(request):
    features = terminal_contract_features(request)
    if features['legacy_materials']:
        from .terminal_legacy_v1 import terminal_prompt as legacy_prompt
        return legacy_prompt(request)
    if request['audio_gain_termination'].get('material_exhaustion') is None:
        raise ContractRejection('TERMINAL_MATERIAL_EXHAUSTION_REQUIRED')
    data, refs = terminal_materials(request)
    return _render_terminal_prompt(data, refs,
        lifecycle_policy=features['lifecycle_prompt'])


def _keep_route_instructions(data):
    """Render only the keep-route contract available in the supplied materials."""
    routes = data.get('keep_routes') or {}
    names = '、'.join(routes) if routes else '无'
    text = '保留原稿时 keep_route 只能取 keep_routes 中列出的路由；当前可用路由：' + names + '。'
    if 'B_EVIDENCE_SUPPORTS_CURRENT' in routes:
        text += ('证据直接支持完整现稿时使用 B_EVIDENCE_SUPPORTS_CURRENT，'
                 'evidence_refs 至少引用 current_support_observation_refs 中一条真实观察，'
                 '不要求虚构另一种读法；')
    else:
        text += ('当前材料没有直接支持完整现稿的保留路由；不要把只覆盖局部的 target_text、'
                 'clear_audio_constraints 或盲拼音不一致当作整句支持；')
    if 'C_MEANING_EQUIVALENT' in routes:
        text += ('仍有不影响意思的不确定读法时使用 C_MEANING_EQUIVALENT，必须给 meaning_equivalence 对象：'
                 'status=EQUIVALENT；current_meaning/alternative_meaning/shared_meaning/uncertain_detail/context_basis '
                 '均为非空字符串；evidence_cue_ids 为相关字幕 cue 的整数编号数组（不是证据编号或字符串）。')
    return text


def _render_terminal_prompt(data, refs, *, lifecycle_policy=False):
    route_instructions = _keep_route_instructions(data)
    if 'B_EVIDENCE_SUPPORTS_CURRENT' in (data.get('keep_routes') or {}):
        unresolved_instructions = (
            'UNRESOLVABLE 只在直接支持现稿的证据仍定不下、写出来的话会把否定/指代/对象/立场说错且'
            '语境也定不下时使用；不能因少了完全听不清的片段或不影响意思的差异判 D。')
    else:
        unresolved_instructions = (
            'UNRESOLVABLE 只在现有材料仍定不下、写出来的话会把否定/指代/对象/立场说错且语境也定不下时使用；'
            '不能因少了完全听不清的片段或不影响意思的差异判 D。')
    return (MARKER + '\n'
        '材料已按原合同核验穷尽，本轮必须终裁，不再规划补听。先综合多路宽窗、原生词时间、'
        'refrain、后文重述、整场转写、场次语境、词表与音素。'
        'Argon 只是稍高权威；自相矛盾的窄窗证人可以提疑，不能构成冲突或维持未决。'
        '反复台词是 refrain，其变体作语境支持，不能用后一次覆盖前一句。清楚原话必须保留。'
        'clear_audio_constraints 是同一观察投影中的可靠目标原话，输出必须保留其中清楚的片段；'
        '若拟改字与其冲突，采用与清楚原话一致的读法完成终裁，不留未决。窄窗拼音不能单独维持阻挡。\n'
        '选项读 policy.terminal_choices。CURRENT 保留；PROPOSED 采用已提供候选；CONTEXT_FIT '
        '仅对听不清部分按整篇语境拟合，按 policy.c_rendering 尽量保留原文、只改到通顺；'
        '只有被省部分完全听不清且留下的话与交际作用没有说错才可缩短，不补造事实、姓名或立场。'
        '无来源专名只差人名用字/是否人名时保留现稿，在 best_guess 内部给最好猜测，不拦下；'
        '有真实来源才按来源写，嘉宾称呼不进入主播词表。\n'
        + route_instructions + '\n'
        + unresolved_instructions
        + ('没有唯一原音读法不是D：先按语境形成不改变含义的通顺写法；'
        'D须说明为什么任何保留、缩短或语境拟合都会让写出来的话主张错误含义或立场。'
        'D的comparison_text必须是已提供的具体候选原句或有绑定的目标听写，不能放解释段落；解释写在written_meaning_error。'
        '用 comparison_text 与 CURRENT 的确定性含义差异核对；未知词形差异和未听清的省略本身不是错误含义证据。'
        '不得用当前稿默认正确绕过清楚原音冲突。\n' if lifecycle_policy else
        '用 comparison_text 与 CURRENT 的确定性含义差异核对。不得用当前稿默认正确绕过清楚原音冲突。\n')
        + edit_contract(data['current'])['instructions'] + '\n'
        + '同一物理问题上可冷校验的历史听音与本轮听音同样有效，按质量比较；不是新增不等于无效。\n'
        + ('回答 contract_version=' + data['answer_contract_version'] + '。\n'
           if data.get('answer_contract_version') else '')
        + '只回 JSON：choice、grade(A/B/C/D)、reason、evidence_refs（实际 observation_id 或 context_refs）、'
        'evidence_decisive（布尔）、context_can_decide（布尔）、comparison_text、name_only（布尔）、'
        'written_meaning_error（会主张什么错误的具体意思）、best_guess（内部）；无来源姓名保留时必须给 '
        'name_span=[原稿姓名槽起点,终点]；有来源时给 name_source_ref（场次字段名）、name_source_text '
        '（来源逐字引文）、sourced_name（引文中的确切称呼），写出该来源称呼，槽外清楚原话照原话。'
        '保留原稿时另给 keep_route，取值和适用档位只读 keep_routes。'
        '改字和UNRESOLVABLE的keep_route为null。改字时同时给既有 typed 编辑字段：'
        'status=RESOLVED、mode=faithful_paraphrase、edit_start/edit_end（严格按上述编辑区间合同）、'
        'replacement、reliable_fragments[{text,basis}]、evidence_refs、meaning_basis、'
        'literal_uncertainty、no_new_facts=true；只在真实来源支持时可用既有 bound_event_spelling 模式。'
        'UNRESOLVED_BY_AUDIO 等仅供内部审计，任何观众文字不得出现。\n'
        + '可引用编号（只用这些精确编号，禁止自行拼 JSON 路径）：' + json.dumps(refs, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n'
        + json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


@cold_input
def validate_terminal_answer(request, answer, *, prompt=None):
    from .answer_contract import answer_version, decode_answer
    features = terminal_contract_features(request)
    version = answer_version(request, prompt=prompt)
    answer = decode_answer(request, answer, prompt=prompt)
    policy = validate_policy(request['terminal_policy'])
    choice = answer.get('choice')
    if choice not in policy['terminal_choices'] or 'needs_audio' in answer:
        raise ContractRejection('TERMINAL_CHOICE_CONTRACT_INVALID')
    if not isinstance(answer.get('reason'), str) or not answer['reason'].strip():
        raise ContractRejection('TERMINAL_REASON_REQUIRED')
    check = request['check_request']
    proof = request['audio_gain_termination']
    if version == 'subtitle-terminal-answer.v1' and prompt is not None:
        if not terminal_prompt_matches(request, prompt):
            raise ContractRejection('TERMINAL_ORIGINAL_PROMPT_BINDING_INVALID')
        available = _material_refs(json.loads(prompt.rsplit('\n', 1)[1]))
        routes, offered_support = offered_keep_contract({}, legacy_v1_answer=True)
    else:
        materials, available = terminal_materials(request)
        # Citations, routes and B support all come from the one rendering of
        # this request's own contract version (T1), never a later recomputation.
        routes, offered_support = offered_keep_contract(materials)
    refs = answer.get('evidence_refs')
    if not isinstance(refs, list) or not refs or any(r not in available for r in refs):
        raise ContractRejection('TERMINAL_EVIDENCE_REFS_INVALID')
    other = answer.get('comparison_text') or check.get('proposed_cue') or check['current_cue']
    diff = classify_difference(check['current_cue'], other,
        glossary_terms=request['terminal_glossary'], policy=policy)
    if answer.get('grade') == 'B' and answer.get('evidence_decisive') is not True:
        raise ContractRejection('TERMINAL_B_EVIDENCE_REQUIRED')
    if answer.get('grade') not in policy['grades']:
        raise ContractRejection('TERMINAL_GRADE_REQUIRED')
    if _is_keep(request, answer):
        route_name = answer.get('keep_route')
        route = routes.get(route_name)
        if route is None and route_name in KEEP_ROUTES:
            raise ContractRejection('TERMINAL_KEEP_ROUTE_UNAVAILABLE')
        legacy_meaning_keep = (version == 'subtitle-terminal-answer.v1'
            and route_name == 'C_MEANING_EQUIVALENT' and isinstance(answer.get('meaning_equivalence'), dict))
        if route is None or (answer.get('grade') not in route['grades'] and not legacy_meaning_keep):
            raise ContractRejection('TERMINAL_KEEP_ROUTE_REQUIRED')
        if (route_name == 'B_EVIDENCE_SUPPORTS_CURRENT'
                and not set(refs).intersection(offered_support)):
            raise ContractRejection('TERMINAL_CURRENT_OBSERVATION_SUPPORT_REQUIRED')
    elif answer.get('keep_route') is not None:
        raise ContractRejection('TERMINAL_KEEP_ROUTE_NOT_APPLICABLE')
    if answer.get('grade') == 'A':
        if len(grade_a_witnesses(request, refs)) < grade_a_witness_contract(request)['minimum_distinct_witnesses']:
            raise ContractRejection('TERMINAL_A_INDEPENDENT_WIDE_EVIDENCE_REQUIRED')
    # The name-only exception cannot suppress a separate polarity/referent/action error.
    name_only = name_only_difference(diff, answer)
    if name_only and choice != 'CURRENT' and not answer.get('name_source_ref'):
        raise ContractRejection('TERMINAL_UNSOURCED_NAME_RETAIN_CURRENT')
    if name_only:
        span = answer.get('name_span')
        current = check['current_cue']
        if (not isinstance(span, list) or len(span) != 2 or any(type(i) is not int for i in span)
                or not 0 <= span[0] < span[1] <= len(current)
                or not other.startswith(current[:span[0]])
                or (current[span[1]:] and not other.endswith(current[span[1]:]))):
            raise ContractRejection('TERMINAL_NAME_SLOT_BINDING_REQUIRED')
    if answer.get('name_source_ref'):
        value = request['terminal_context'].get(answer['name_source_ref'])
        quote = answer.get('name_source_text')
        if not isinstance(quote, str) or not quote.strip() or quote not in json.dumps(value, ensure_ascii=False):
            raise ContractRejection('TERMINAL_NAME_SOURCE_REQUIRED')
        name = answer.get('sourced_name')
        output = check['current_cue']
        if choice in {'PROPOSED', 'CONTEXT_FIT'}:
            start, end = answer.get('edit_start'), answer.get('edit_end')
            if type(start) is not int or type(end) is not int:
                raise ContractRejection('TERMINAL_NAME_SOURCE_REQUIRED')
            output = output[:start] + str(answer.get('replacement') or '') + output[end:]
        if not isinstance(name, str) or not name or name not in quote or name not in output:
            raise ContractRejection('TERMINAL_NAME_OUTPUT_NOT_FROM_SOURCE')
    if choice == 'UNRESOLVABLE':
        # Preserve the recorded answer's old decoding contract. The shared
        # lifecycle separately revalidates D proof under the current policy;
        # consuming an old D response is not proof of a true content block.
        meaning_error = (written_meaning_conflict(check['current_cue'], diff, policy)
                         if features['written_meaning_proof'] else diff['meaning_carrying'])
        if (not meaning_error or name_only or answer.get('evidence_decisive') is not False
                or answer.get('context_can_decide') is not False
                or not str(answer.get('written_meaning_error') or '').strip()):
            raise ContractRejection('TERMINAL_D_WRITTEN_MEANING_ERROR_REQUIRED')
        if answer.get('grade') != 'D':
            raise ContractRejection('TERMINAL_D_GRADE_REQUIRED')
        if features['written_meaning_proof']:
            written_meaning_evidence(request, answer)
    elif answer.get('grade') == 'D':
        raise ContractRejection('TERMINAL_D_CANNOT_RELEASE')
    if choice == 'PROPOSED':
        start, end = answer.get('edit_start'), answer.get('edit_end')
        if type(start) is not int or type(end) is not int:
            raise ContractRejection('TERMINAL_PROPOSED_EDIT_REQUIRED')
        proposed = check['current_cue'][:start] + str(answer.get('replacement') or '') + check['current_cue'][end:]
        if proposed != check.get('proposed_cue'):
            raise ContractRejection('TERMINAL_PROPOSED_OUTSIDE_CLOSED_SET')
    return choice


def written_meaning_conflict(current, difference, policy):
    """Unclassified uncertainty/omission is not proof of a wrong assertion."""
    from .nonsemantic_sound import sound_only
    meaningful = set(policy['features']['meaning_carrying']) - {'unclassified_lexical'}
    return not sound_only(current, policy=policy) and bool(meaningful.intersection(difference['features']))


def written_meaning_evidence(request, answer):
    """Compare actual candidate utterances, never the prose explaining a doubt.

    A source-bound alternative and an actual meaning feature are necessary
    before an old/new model D label can become the content-block lifecycle.
    Missing proof remains an engineering recovery; it grants no release.
    """
    check = request['check_request']
    candidates = {check.get('proposed_cue')}
    for row in request['audio_gain_termination']['projection']['observations']:
        target = row.get('target_text')
        if isinstance(target, str):
            candidates.add(target)
    alternate = answer.get('comparison_text')
    if not isinstance(alternate, str) or alternate not in candidates:
        raise ContractRejection('TERMINAL_D_BOUND_COMPARISON_REQUIRED')
    difference = classify_difference(check['current_cue'], alternate,
        glossary_terms=request['terminal_glossary'], policy=request['terminal_policy'])
    if not written_meaning_conflict(check['current_cue'], difference, request['terminal_policy']):
        raise ContractRejection('TERMINAL_D_WRITTEN_MEANING_ERROR_REQUIRED')
    return {'current_text': check['current_cue'], 'alternative_text': alternate,
            'difference': difference}


def request_terminal_answer(call, prompt, request, *, wallclock_seconds=None):
    from .llm_client import extract_json_object
    from .response_schemas import terminal_schema
    from .structured_output import call_json
    from .infra_retry_policy import (
        TERMINAL_RETRY_DEFAULT_WALLCLOCK_SECONDS,
        terminal_retry_call,
        terminal_retry_scope,
    )

    if wallclock_seconds is None:
        wallclock_seconds = getattr(
            call, "terminal_retry_wallclock_seconds", None
        )
    if wallclock_seconds is None:
        wallclock_seconds = TERMINAL_RETRY_DEFAULT_WALLCLOCK_SECONDS

    # A native build_llm_call retries inside the provider dispatch so the
    # SemanticAttemptRecorder sees one logical terminal request while CPA's
    # HTTP/archive recorder sees every physical attempt.  Small test/adaptor
    # callables without that capability use the same policy at this seam.
    provider_retry_capable = bool(
        getattr(call, "terminal_retry_capable", False)
    )
    recorder = getattr(call, "_semantic_attempt_recorder", None)
    send = call
    if not provider_retry_capable and recorder is None:
        timeout_seconds = getattr(call, "timeout_seconds", None)
        send = lambda actual_prompt: terminal_retry_call(
            lambda: call(actual_prompt), timeout_seconds=timeout_seconds
        )
    try:
        with terminal_retry_scope(wallclock_seconds=wallclock_seconds):
            from pathlib import Path
            from .completed_review_checkpoint import ruling_response
            # Keep provenance on the native owner even if the retry adapter is
            # a lambda. UNKNOWN and dispatch admission remain inside send.
            for name, value in getattr(call, '__dict__', {}).items():
                if send is not call:
                    setattr(send, name, value)
            raw, _ = ruling_response(stage='semantic-terminal', prompt=prompt,
                llm_call=send, inputs={'request': request}, parser_path=Path(__file__),
                response_schema=terminal_schema(request), name='semantic_terminal')
            # editorial_subtitle.adjudicate records these bytes and invokes
            # validate_terminal_answer on every path, including a cache hit.
            return raw
    except Exception as exc:
        reason = getattr(exc, "terminal_retry_reason", None)
        if reason:
            # The underlying recorder has already retained the transport
            # attempt as UNKNOWN; surface the typed wall-clock reason to the
            # terminal audit instead of collapsing it back to LlmCallError.
            raise ContractRejection(str(reason)) from exc
        raise


def terminal_prompt_matches(request, prompt):
    from .prompt_materials import same_prompt_materials
    from .answer_contract import answer_version
    terminal_contract_features(request)
    if answer_version(request, prompt=prompt) == 'subtitle-terminal-answer.v1':
        from .terminal_legacy_v1 import original_meaning_prompt
        return same_prompt_materials(prompt, original_meaning_prompt(request))
    return same_prompt_materials(prompt, terminal_prompt(request))


def name_only_difference(diff, answer):
    # A discussion of a name (including "不能认定人名") is not a name slot.
    # The classifier's sourced term or the explicit typed name claim supplies
    # that fact; the ordinary slot/source guards below still validate it.
    return ((answer.get('name_only') is True or 'proper_name' in diff['features']) and not set(diff['features']) & {
        'negation_polarity', 'referent', 'number_time', 'action', 'evaluation_stance',
        'derogatory', 'object_place', 'persona_action'})


def _protect_terminal_current(request, answer, *, equivalent_current=False):
    """One guard dispatch for the actual keep producer and its cold reader."""
    from src.autoslice.targeted_relisten import protect_clear_audio
    comparison = answer.get('comparison_text') or request['check_request'].get('proposed_cue') or request['current_cue']
    diff = classify_difference(request['current_cue'], comparison,
        glossary_terms=request['terminal_glossary'], policy=request['terminal_policy'])
    projection = terminal_observation_projection(request)
    if name_only_difference(diff, answer) and not answer.get('name_source_ref'):
        protect_outside_name_slot(request['check_request'], request['audio'], answer['name_span'],
            projection=projection, policy=request['terminal_policy'])
    else:
        protect_clear_audio(request['check_request'], request['audio'], request['current_cue'],
            projection=projection, equivalent_current=equivalent_current,
            policy=request['terminal_policy'], glossary_terms=request['terminal_glossary'])


@cold_input
def keep_from_terminal(*, srt_text, finding, request, answer, prompt, raw):
    """Compile the real final answer to SUB-18/H's existing typed risk and keep."""
    from src.autoslice.final_review_meaning_policy import bind_current_risk
    from src.autoslice.targeted_relisten import sub18_meaning_equivalent_keep
    from .answer_contract import decode_answer
    answer = decode_answer(request, answer, prompt=prompt)
    if validate_terminal_answer(request, answer, prompt=prompt) not in {'CURRENT', 'CONTEXT_FIT'}:
        raise ContractRejection('TERMINAL_KEEP_CHOICE_REQUIRED')
    from src.autoslice.targeted_relisten import validate_no_gain
    validate_no_gain(request['audio_gain_termination'], request['check_request'])
    review = {'prompt': prompt, 'raw_completion': raw,
        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'completion_sha256': hashlib.sha256(raw.encode()).hexdigest(),
        'choice': answer['choice'], 'grade': answer['grade'], 'request': deepcopy(request)}
    route = answer['keep_route']
    if route == 'B_EVIDENCE_SUPPORTS_CURRENT':
        row = {**finding.get('_terminal_original_finding', finding),
            'base_text_sha256': hashlib.sha256(request['current_cue'].encode()).hexdigest()}
        keep = evidence_supported_current_keep(request, answer)
        _protect_terminal_current(request, answer)
        keep.update(rebuilt_finding=row, terminal_review=review, keep_route=route)
        return keep
    row = {**finding.get('_terminal_original_finding', finding),
        'content_risk': 'noncritical_uncertainty', 'risk_basis': answer['reason'],
        'proposed_full_cue': None, 'suggestion': '',
        'base_text_sha256': hashlib.sha256(request['current_cue'].encode()).hexdigest()}
    row['current_content_risk'] = bind_current_risk(row, srt_text=srt_text,
        cue_count=len(parse_srt_cues(srt_text)), prompt=prompt,
        meaning_equivalence=answer.get('meaning_equivalence'))
    # No C declaration can waive a changed clear unit or add new text. Run the
    # guard itself first: the SUB-18 predicate below is a boolean cold reader,
    # and would otherwise rename a clear-audio failure as a meaning-contract one.
    # Retained unresolved detail is still released only by the complete C
    # contract built next; both checks are required, as before.
    _protect_terminal_current(request, answer, equivalent_current=True)
    keep = sub18_meaning_equivalent_keep(finding=row, recovery_audit={
        'terminal_check_request': request['check_request'], 'no_gain_proof': request['audio_gain_termination'],
        'terminal_review': review}, prior={})
    if keep is None:
        raise ContractRejection('TERMINAL_KEEP_MEANING_CONTRACT_REQUIRED')
    keep['rebuilt_finding'] = row
    keep['terminal_review'] = review
    keep['keep_route'] = route
    return keep


def evidence_supported_current_keep(request, answer):
    """Reuse the complete-utterance CURRENT object and its existing validator."""
    from src.autoslice.acoustic_witness_adjudication import build_witness_request, valid_current_utterance_support
    check = request['check_request']
    judge = {'status': 'JUDGED', 'choice': 'CURRENT', 'needs_audio': False,
        'current_utterance_supported': True, 'current_utterance_support_reason': answer['reason'],
        'check_request_sha256': check['request_sha256'], 'evidence_refs': list(answer['evidence_refs'])}
    witness_judge = {'witness_request_sha256': build_witness_request(check)['request_sha256'], 'judge': judge}
    if not valid_current_utterance_support(check_request=check, judge=judge, witness_judge=witness_judge):
        raise ContractRejection('TERMINAL_CURRENT_UTTERANCE_CONTRACT_REQUIRED')
    return {'schema_version': 'subtitle-span-adjudication.v1', 'status': 'OBSERVED', 'repaired': False,
        'timing_immutable': True, 'policy_branch': KEEP_ROUTES[answer['keep_route']]['branch'],
        'decision_authority': 'CPA_JUDGE', 'witness_authority': 'EVIDENCE_ONLY',
        'request': deepcopy(check), 'witness_judge': witness_judge,
        'mutation_authority': {'schema_version': 'subtitle-correction-mutation-authority.v1',
            'status': 'NOT_APPLIED', 'basis': KEEP_ROUTES[answer['keep_route']]['branch']}}


def protect_outside_name_slot(check, witness, span, *, projection=None, policy=None):
    """The unsourced-name exception covers the declared name, never clear words outside it."""
    from src.autoslice.span_phoneme_resolution import observation_catalogue
    from src.autoslice.targeted_relisten import _editorial_guard_tokens
    before, after = check['current_cue'][:span[0]], check['current_cue'][span[1]:]
    if projection is None:
        catalogue, _ = observation_catalogue(check, witness)
    else:
        from src.autoslice.targeted_relisten import terminal_clear_audio_catalogue
        catalogue = terminal_clear_audio_catalogue(projection, current_text=check['current_cue'],
            qualification_policy=policy or load_policy())
    for row in catalogue:
        if row['kind'] == 'blind_pinyin':
            units = row['text'].split()
            prefix, suffix = _editorial_guard_tokens(before), _editorial_guard_tokens(after)
            if (units[:len(prefix)] != prefix or (suffix and units[-len(suffix):] != suffix)):
                raise ContractRejection('EDITORIAL_CLEAR_AUDIO_CONFLICT_OUTSIDE_NAME')
        elif row['kind'] in {'native_transcript', 'original_language', 'range_transcript', 'argon_native_transcript'}:
            if row.get('derived_from_conflicting_observations'):
                # Only the name slot is exempt. A source reading whose whole
                # outside matches is sufficient. Otherwise the common view's
                # literal prefix/suffix must match exactly as below; its
                # unclear gap cannot absorb extra words OUTSIDE the name.
                if any(lexical(text).startswith(lexical(before)) and lexical(text).endswith(lexical(after))
                        for text in row['readings']):
                    continue
            text = lexical(row['text'])
            if not text.startswith(lexical(before)) or not text.endswith(lexical(after)):
                raise ContractRejection('EDITORIAL_CLEAR_AUDIO_CONFLICT_OUTSIDE_NAME')


@cold_input
def terminal_keep_matches(row, audit):
    """Cold consumer of the real final judgment; no scalar reclassification."""
    from src.autoslice.llm_client import extract_json_object
    from src.autoslice.final_review_meaning_policy import bind_current_risk
    review = audit['terminal_review']
    request, prompt, raw = review['request'], review['prompt'], review['raw_completion']
    from src.autoslice.targeted_relisten import validate_no_gain
    answer = extract_json_object(raw)
    from .answer_contract import decode_answer
    answer = decode_answer(request, answer, prompt=prompt)
    choice = validate_terminal_answer(request, answer, prompt=prompt)
    if choice not in {'CURRENT', 'CONTEXT_FIT'}:
        return False
    validate_no_gain(request['audio_gain_termination'], request['check_request'])
    if (not terminal_prompt_matches(request, prompt)
            or review['prompt_sha256'] != hashlib.sha256(prompt.encode()).hexdigest()
            or review['completion_sha256'] != hashlib.sha256(raw.encode()).hexdigest()
            or request['current_cue'] != request['check_request']['current_cue']):
        return False
    route = answer['keep_route']
    if audit.get('keep_route', route) != route or audit.get('policy_branch') != KEEP_ROUTES[route]['branch']:
        return False
    if route == 'B_EVIDENCE_SUPPORTS_CURRENT':
        expected = evidence_supported_current_keep(request, answer)
        _protect_terminal_current(request, answer)
        return (all(audit.get(key) == value for key, value in expected.items())
            and row.get('base_text_sha256') == hashlib.sha256(request['current_cue'].encode()).hexdigest())
    if request['audio_gain_termination'] != audit['sub18_context_meaning_equivalent_keep']['no_gain_proof']:
        return False
    from src.autoslice.final_review_meaning_policy import current_meaning_equivalent
    matches = row.get('current_content_risk') == bind_current_risk(row,
        srt_text=request['check_request']['whole_clip_current_srt'],
        cue_count=len(parse_srt_cues(request['check_request']['whole_clip_current_srt'])),
        prompt=prompt, meaning_equivalence=answer.get('meaning_equivalence'))
    if matches and current_meaning_equivalent(row):
        _protect_terminal_current(request, answer, equivalent_current=True)
        return True
    return False


def terminal_keep_failure(row, audit):
    """The first real reason a saved terminal keep fails its cold consumer.

    Release predicates answer only yes/no. This keeps a guard's own reason
    (for example EDITORIAL_CLEAR_AUDIO_CONFLICT) instead of letting it pose as
    a generic meaning-contract or content failure. It grants nothing.
    """
    try:
        if terminal_keep_matches(row, audit):
            return None
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        return str(exc) if isinstance(exc, ValueError) and str(exc) else type(exc).__name__
    return 'TERMINAL_KEEP_RECEIPT_BINDING_MISMATCH'
