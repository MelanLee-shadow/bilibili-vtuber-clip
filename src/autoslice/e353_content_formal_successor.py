"""Fail-closed public boundary for private candidate successor receipts."""
class E353ContentFormalSuccessorError(ValueError):
    reason_code = "PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE"

def validate_final_review_successor(*args, **kwargs):
    raise E353ContentFormalSuccessorError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def validate_source_fact_from_scope(*args, **kwargs):
    return False
