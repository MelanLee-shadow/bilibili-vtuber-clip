"""Route consumed decisions and replay legacy non-release receipts unchanged."""
from collections.abc import Callable

from .finding_closure import ClosureContractError, FindingKey, ReviewCompleted, State, Work
from .finding_closure_ports import RetainedObject, ReviewResult


def consumed_decision_state(selected: State, key: FindingKey, work: Work | None) -> State:
    """An answer already consumed for this P,M proceeds to its terminal owner."""
    if (selected == State.AWAITING_DECISION and work is not None
            and work.kind == 'decision' and work.task is not None and work.task.key == key):
        # The decision owner has consumed this exact P,M answer. A
        # non-final observation cannot ask the identical decision
        # again; the existing terminal owner must close or refuse it.
        # Evidence, UNKNOWN and unbound answers retain their guards.
        return State.AWAITING_TERMINAL
    return selected


def validate_cold_review_result(
    event: ReviewCompleted, finding: RetainedObject, *,
    load_receipt: Callable[[], RetainedObject], current_result: Callable[[], ReviewResult],
    legacy: Callable[..., object],
) -> None:
    if event.state in {State.CONTRACT_ERROR, State.CALL_FAILURE}:
        # Historical engineering outcomes are not content authority.
        # Retain the original answer even when today's consumer can
        # now validate it; only a new revalidation event can close P.
        receipt = load_receipt()
        value = receipt.value()
        if 'owner_error' in value:
            if RetainedObject.from_value(value.get('finding')) != finding:
                raise ClosureContractError('FINDING_CLOSURE_COLD_FAILURE_ORIGINAL_DRIFT')
        elif receipt != finding:
            raise ClosureContractError('FINDING_CLOSURE_COLD_FAILURE_ORIGINAL_DRIFT')
        return
    actual = current_result()
    from .finding_lifecycle import adjudication_failure_layer
    original = finding.value().get('exact_release_adjudication')
    if (event.state == State.AWAITING_EVIDENCE and actual.state == State.CONTRACT_ERROR
            and isinstance(original, dict) and 'failure_layer' not in original
            and legacy(adjudication_failure_layer, original) == 'CONTRACT_ERROR'
            and actual.receipt == load_receipt()):
        # Retain an old non-release routing event verbatim. The next
        # ordinary effect projects the rejected answer as engineering
        # work; this cannot validate KEEP/EDIT/D or a new-style event.
        return
    if actual.state != event.state or actual.receipt != load_receipt():
        raise ClosureContractError('FINDING_CLOSURE_COLD_RULING_DRIFT')
