"""OSS compatibility stub: candidate-specific operator title authorities are private."""
from __future__ import annotations

SCHEMA_VERSION = "lidousha-operator-exact-title-source-fact-authority.v1"
CONSUMPTION_SCHEMA_VERSION = "operator-exact-title-source-fact-consumption.v1"
PASS_DECISION = "OPERATOR_EXACT_TITLE_WITH_RECORDED_SOURCE_FACT_DISSENT"


class OperatorExactTitleSourceFactAuthorityError(ValueError):
    """The public snapshot cannot supply a private operator title authority."""


def load_operator_exact_title_source_fact_authority(*_args, **_kwargs):
    return None


def validate_operator_exact_title_source_fact_receipt(*_args, **_kwargs):
    return False


def _unavailable(*_args, **_kwargs):
    raise OperatorExactTitleSourceFactAuthorityError("PRIVATE_OPERATOR_TITLE_AUTHORITY_UNAVAILABLE")


validate_authority_document = _unavailable
consume_operator_exact_title_source_fact_authority = _unavailable
operator_exact_title_source_fact_receipt_body = _unavailable
authorize_operator_exact_title_source_fact = _unavailable
