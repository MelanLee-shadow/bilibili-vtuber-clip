"""Changes to actual V4 audit consumers must invalidate old audit receipts."""

from pathlib import Path

import pytest

from scripts.audit_review_package import ROOT, _audit_policy_fingerprint


V4_POLICY_SOURCES = (
    "src/autoslice/cover_host_identity_gate.py",
    "src/autoslice/host_only_v4_package_binding.py",
    "src/autoslice/review_package_cover_diagnostics.py",
    "src/autoslice/cover_source_composition.py",
)


@pytest.mark.parametrize("relative_path", V4_POLICY_SOURCES)
@pytest.mark.parametrize("change", ["changed", "unavailable"])
def test_v4_consumed_policy_drift_invalidates_native_audit(monkeypatch, relative_path, change):
    baseline = _audit_policy_fingerprint()
    assert _audit_policy_fingerprint() == baseline
    target = (ROOT / relative_path).resolve()
    original = Path.read_bytes

    def drift(path):
        if path.resolve() == target:
            if change == "unavailable":
                raise FileNotFoundError("synthetic missing dependency")
            return original(path) + b"\n# synthetic V4 policy change\n"
        return original(path)

    # Intercept only the hash input. Never mutate real repository policy or
    # a receipt, and never turn a model FAIL into PASS.
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", drift)
        assert _audit_policy_fingerprint() != baseline
    assert _audit_policy_fingerprint() == baseline


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/autoslice/reporting.py",
        "src/autoslice/host_only_identity_card_successor.py",
    ],
)
def test_nonconsumed_reporting_or_prepare_code_does_not_expire_audit(monkeypatch, relative_path):
    baseline = _audit_policy_fingerprint()
    target = (ROOT / relative_path).resolve()
    original = Path.read_bytes

    def drift(path):
        payload = original(path)
        return payload + b"\n# non-auditor change\n" if path.resolve() == target else payload

    monkeypatch.setattr(Path, "read_bytes", drift)
    assert _audit_policy_fingerprint() == baseline
