"""Recover an omitted source-review increment from its original observation.

Legacy retained ingress seeded the parent usage but did not add the new source
correction pass. This reconciles logical adjudication counts, not HTTP totals.
It never changes or promotes the original review verdict.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from .final_review_meaning_policy import digest


def reconcile_legacy_source_usage(authority: dict) -> dict | None:
    retained = authority.get("retained_source_input")
    if not isinstance(retained, Mapping) or retained.get("schema_version") != "retained-source-input-consumption.v1":
        return None
    review = authority.get("final_review_audit") or {}
    source_review = review.get("correction_pass") or review
    if not isinstance(source_review, Mapping):
        raise ValueError("RETAINED_SOURCE_REVIEW_USAGE_INVALID")
    count = source_review.get("context_adjudication_count")
    if count is None:
        return None
    usage = authority.get("cpa_adjudication_usage") or {}
    reported = usage.get("provider_adjudication_count")
    inherited = retained.get("inherited_cpa_adjudication_count")
    if any(type(value) is not int or value < 0 for value in (count, reported, inherited)) or reported < inherited:
        raise ValueError("RETAINED_SOURCE_REVIEW_USAGE_INVALID")
    binding = {"parent_record": deepcopy(retained.get("parent_record")),
        "padded_sha256": retained.get("padded_sha256"),
        "post_transcript_entity_output_srt_sha256": authority.get("post_transcript_entity_output_srt_sha256"),
        "source_review_sha256": digest(source_review), "omitted_context_adjudication_count": count}
    previous = authority.get("legacy_retained_source_usage_reconciliation")
    if previous is not None:
        previous_binding = previous.get("binding") if isinstance(previous, Mapping) else None
        if (not isinstance(previous_binding, Mapping)
                or set(previous_binding) != set(binding)
                or previous.get("schema_version") != "retained-source-review-usage-reconciliation.v1"
                or previous.get("scope") != "LOGICAL_CONTEXT_ADJUDICATIONS_NOT_HTTP"
                or previous.get("release_authorized") is not False
                or not isinstance(previous_binding.get("source_review_sha256"), str)
                or not previous_binding["source_review_sha256"].startswith("sha256:")
                or any(type(value) is not int or value < 0 for value in (
                    previous.get("reported_count"), previous.get("reconciled_count"),
                    previous_binding.get("omitted_context_adjudication_count")))
                or previous["reconciled_count"] != (
                    previous["reported_count"] + previous_binding["omitted_context_adjudication_count"])
                or reported < previous["reconciled_count"]
                or any(previous_binding[key] != binding[key] for key in ("parent_record", "padded_sha256"))):
            raise ValueError("RETAINED_SOURCE_REVIEW_USAGE_RECONCILIATION_DRIFT")
    # Current source passes already report their cumulative input and output.
    cumulative = source_review.get("cpa_adjudication_usage")
    if isinstance(cumulative, Mapping):
        value = cumulative.get("provider_adjudication_count")
        if type(value) is not int or not 0 <= value <= reported:
            raise ValueError("RETAINED_SOURCE_REVIEW_USAGE_INVALID")
        if value >= inherited + count:
            if previous is not None:
                prior = cumulative.get("prior_provider_adjudication_count")
                if type(prior) is not int or not previous["reconciled_count"] <= prior <= value:
                    raise ValueError("RETAINED_SOURCE_REVIEW_USAGE_RECONCILIATION_DRIFT")
            return None
        # The first local-truth consumer copied the still-omitted source
        # seed into this field and added only its own saved observations.
        # Recognize that exact accounting shape; do not infer an arbitrary
        # deficit from a partially understood cumulative counter.
        local_ids = [*authority.get('source_truth_satisfaction_accounted', []),
                     *authority.get('source_truth_term_alignment_accounted', [])]
        if (cumulative.get('prior_provider_adjudication_count') != inherited
                or len(local_ids) != len(set(local_ids))
                or value != inherited + len(local_ids)):
            raise ValueError('RETAINED_SOURCE_REVIEW_USAGE_AMBIGUOUS')
    if previous is not None:
        if previous_binding == binding:
            return None
        # A later ordinary review may annotate the retained correction pass.
        # Its outer cumulative usage must explicitly include the reconciled
        # input; the receipt alone does not justify ignoring a changed review.
        outer_usage = review.get("cpa_adjudication_usage")
        if source_review is not review and isinstance(outer_usage, Mapping):
            prior = outer_usage.get("prior_provider_adjudication_count")
            current = outer_usage.get("provider_adjudication_count")
            if (type(prior) is int and type(current) is int
                    and previous["reconciled_count"] <= prior <= current == reported
                    and all(previous_binding[key] == binding[key] for key in binding
                            if key != "source_review_sha256")):
                return None
        raise ValueError("RETAINED_SOURCE_REVIEW_USAGE_RECONCILIATION_DRIFT")
    receipt = {"schema_version": "retained-source-review-usage-reconciliation.v1",
        "binding": binding, "reported_count": reported, "reconciled_count": reported + count,
        "scope": "LOGICAL_CONTEXT_ADJUDICATIONS_NOT_HTTP", "release_authorized": False}
    authority["legacy_retained_source_usage_reconciliation"] = receipt
    authority["cpa_adjudication_usage"] = {**usage, "provider_adjudication_count": reported + count}
    return receipt
