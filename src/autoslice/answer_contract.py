"""Versioned answer views; original request, prompt and raw bytes stay intact.

A migration derives representation only. It cannot invent observations, name
sources, successful listening, edit permission or a release disposition.
"""

from .review_errors import ContractRejection
from collections.abc import Mapping
from copy import deepcopy
import re

TERMINAL = 'subtitle-terminal-answer.v3'
EDITORIAL = 'subtitle-editorial-answer.v2'
PROPOSAL = 'subtitle-proposal-answer.v2'


def answer_version(request, *, prompt=None, family='terminal'):
    declared = request.get('answer_contract_version')
    known = ({TERMINAL, 'subtitle-terminal-answer.v2', 'subtitle-terminal-answer.v1'}
             if family == 'terminal' else {EDITORIAL, 'subtitle-editorial-answer.v1'})
    if declared is not None:
        if declared not in known:
            raise ContractRejection('ANSWER_CONTRACT_VERSION_UNSUPPORTED')
        return declared
    if family == 'editorial':
        schema = request.get('schema_version')
        if schema in {'editorial-subtitle-request.v1', 'editorial-subtitle-request.v2'}:
            return 'subtitle-editorial-answer.v1'
        raise ContractRejection('ANSWER_CONTRACT_VERSION_UNDETERMINED')
    version = request.get('terminal_contract_version')
    if version == 2:
        return 'subtitle-terminal-answer.v2'
    # The terminal v1 heading persisted across incompatible answer contracts.
    # Its version cannot be guessed from the heading or from a missing field.
    if prompt is not None:
        instructions = prompt.rsplit('\n', 1)[0]
        if 'keep_route' in instructions:
            return 'subtitle-terminal-answer.v2'
        if ('meaning_equivalence' in instructions
                and prompt.startswith('# 字幕材料穷尽终裁 v1\n')):
            return 'subtitle-terminal-answer.v1'
    return None


def contract_retry(request):
    """Recovery route for a rejected recorded answer, by its own contract version.

    Older contract: re-ask under the current contract (a new question version,
    hence a new semantic ruling key). Current contract: the same question is
    not re-asked; the original answer is revalidated after a consumer repair
    or new material. Unknown versions fail; nothing here releases a finding.
    """
    if 'terminal_policy' in request:
        from .semantic_terminal_adjudication import terminal_contract_retry
        return terminal_contract_retry(request)
    recorded = answer_version(request, family='editorial')
    older = recorded != EDITORIAL
    return {'family': 'editorial', 'recorded_contract_version': recorded,
            'current_contract_version': EDITORIAL,
            'kind': 'REASK_UNDER_CURRENT_CONTRACT' if older else 'REVALIDATE_AFTER_CONSUMER_REPAIR_OR_NEW_MATERIAL',
            'reask_under_current_contract': older}


def _legacy_equivalence(value, request):
    if not isinstance(value, Mapping):
        return value
    result = deepcopy(dict(value))
    ids = result.get('evidence_cue_ids')
    if isinstance(ids, list):
        # v1 named cue references in its original context catalogue. Convert
        # only exact provided cue:N, not loose numeric strings or new aliases.
        sources = request['audio_gain_termination'].get('context_sources', {})
        observations = {row['observation_id']: row for row in
            request['audio_gain_termination']['projection']['observations']}

        # The original v1 field admitted material IDs, not solely integers.
        # Preserve them separately and project only their recorded cue scope.
        projected, material_refs = [], []
        for identity in ids:
            if type(identity) is int:
                projected.append(identity)
            elif isinstance(identity, str) and re.fullmatch(r'cue:[1-9][0-9]*', identity) and identity in sources:
                projected.append(int(identity[4:]))
            elif isinstance(identity, str) and identity in observations:
                material_refs.append(identity)
                projected.extend(observations[identity].get('input_information', {}).get('cue_indexes', []))
            elif (identity == 'whole_session_and_session_context.whole_clip_draft_srt'
                    and request.get('terminal_context', {}).get('whole_clip_draft_srt')):
                material_refs.append(identity)
            else:
                projected.append(identity)  # Invalid/unknown stays invalid.
        result['evidence_cue_ids'] = projected
        if material_refs:
            result['legacy_material_refs'] = material_refs
    basis = result.get('context_basis')
    if isinstance(basis, list) and basis and all(isinstance(i, str) and i.strip() for i in basis):
        result['context_basis'] = '\n'.join(basis)
    return result


def decode_answer(request, answer, *, prompt=None, family='terminal'):
    if not isinstance(answer, Mapping):
        raise ContractRejection('ANSWER_CONTRACT_OBJECT_REQUIRED')
    version = answer_version(request, prompt=prompt, family=family)
    declared = answer.get('contract_version')
    if declared is not None and (declared != version or version is None):
        raise ContractRejection('ANSWER_CONTRACT_VERSION_MISMATCH')
    result = deepcopy(dict(answer))
    if family == 'terminal' and version == 'subtitle-terminal-answer.v1':
        result['meaning_equivalence'] = _legacy_equivalence(answer.get('meaning_equivalence'), request)
        # All old KEEP decisions required a complete meaning-equivalence
        # contract, even when graded B. Never fabricate a new B audio claim.
        keep = answer.get('choice') == 'CURRENT'
        if answer.get('choice') == 'CONTEXT_FIT':
            from .editorial_edit_contract import apply_edit
            keep = apply_edit(request['current_cue'], answer) == request['current_cue']
        if answer.get('keep_route') is None and keep:
            result['keep_route'] = 'C_MEANING_EQUIVALENT'
    return result
