"""OSS stub: private final-review recovery authority is not redistributed."""
from __future__ import annotations


class SelectedFinalReviewRecoveryAuthorityError(ValueError):
    """The private recovery authority is unavailable in an OSS snapshot."""


def _unavailable(*_args, **_kwargs):
    raise SelectedFinalReviewRecoveryAuthorityError(
        "SELECTED_FINAL_REVIEW_PRIVATE_AUTHORITY_UNAVAILABLE"
    )


load_selected_final_review_recovery_authority = _unavailable
validate_selected_final_review_recovery_authority = _unavailable
build_authorized_selected_final_review_recovery_receipt = _unavailable
materialize_selected_final_review_recovery_scope = _unavailable
validate_selected_final_review_scope_predecessor = _unavailable
validate_selected_final_review_recovery_rejection = _unavailable
