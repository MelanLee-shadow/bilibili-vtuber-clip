"""Answer selection and audio acquisition are separate questions.

These predicates grant neither mutation nor release authority.
"""
from collections.abc import Mapping

SELECTED = frozenset({'CURRENT', 'PROPOSED', 'CURRENT_SUPPORTED', 'CONTEXT_FIT',
    'KEEP_EXISTING', 'REPLACE_WITH_EXACT_TEXT'})


def proposal_status(payload: Mapping) -> str:
    status = str(payload.get('status') or '').upper()
    # Explanations, including negations, cannot supply a structured decision.
    # Missing legacy decision data remains unresolved, never new authority.
    return status


def answer_selected(payload: Mapping) -> bool:
    return (str(payload.get('choice') or '').upper() in SELECTED
        or str(payload.get('decision') or '').upper() in SELECTED
        or proposal_status(payload) in SELECTED)
