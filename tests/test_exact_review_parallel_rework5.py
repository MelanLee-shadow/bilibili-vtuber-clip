"""Keep callable-owned protocol metadata through the public parallel entry."""

from types import SimpleNamespace

import pytest

from src.autoslice import final_review_auditor as auditor
from src.autoslice.exact_review_parallel import OrderedVerifierAccess


@pytest.mark.parametrize("name,bound", [
    ("observe_range_context", False),
    ("probe_witness_cache", False),
    ("observe_range_context", True),
])
def test_verifier_facade_preserves_original_callable_and_protocol(name, bound):
    class Owner:
        def observe(self, request):
            return request

    def observe(request):
        return request

    if bound:
        Owner.observe.available_providers = ("gemini",)
        Owner.observe.context_assisted_providers = ("gemini",)
        callback = Owner().observe
    else:
        observe.available_providers = ("gemini",)
        observe.context_assisted_providers = ("gemini",)
        observe.cpa_cache_identity = {"model": "offline-protocol"}
        callback = observe
    verifier = SimpleNamespace(**{name: callback})
    wrapped = OrderedVerifierAccess(verifier)
    delegated = getattr(wrapped, name)
    assert delegated is callback
    assert vars(delegated) == vars(callback)
    assert delegated({"request": "unchanged"}) == {"request": "unchanged"}


def test_public_entry_keeps_range_capabilities_at_ordinary_consumer(monkeypatch):
    def observe(request):
        return request

    observe.available_providers = ("gemini", "native")
    observe.context_assisted_providers = ("gemini",)
    verifier = SimpleNamespace(observe_range_context=observe)
    seen = []

    def adjudicate(text, finding, *, entity_verifier, **kwargs):
        callback = entity_verifier.observe_range_context
        seen.append(callback)
        assert callback.available_providers == ("gemini", "native")
        assert callback.context_assisted_providers == ("gemini",)
        return text, {"status": "UNCERTAIN", "repaired": False}

    monkeypatch.setattr(auditor, "adjudicate_context_finding", adjudicate)
    unresolved, resolved = auditor.adjudicate_exact_release_findings(
        "1\n00:00:01,000 --> 00:00:02,000\n原句\n",
        [{"cue_index": 1, "suspect": "原句", "suggestion": "候选", "repair_class": "phonetic"}],
        entity_verifier=verifier,
    )
    assert unresolved and not resolved
    assert seen == [observe]
