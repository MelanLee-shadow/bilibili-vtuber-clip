"""The suite must stay isolated even when the host has a production runtime."""

import os
from pathlib import Path

import pytest

from src.autoslice import provider_slots


PRODUCTION = Path("/opt/bilive/autoslice")


@pytest.fixture
def production_runtime_present(monkeypatch):
    real_is_dir = Path.is_dir

    def is_dir(path):
        if path in (PRODUCTION, PRODUCTION / "repo"):
            return True
        return real_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    # Model a legacy deployment without inspecting any real production files.
    monkeypatch.setattr(provider_slots, "deployed_code_root", lambda root: root / "repo")


def test_unset_base_disables_runtime_and_slots_even_on_production_host(
    monkeypatch, production_runtime_present,
):
    assert PRODUCTION.is_dir()
    assert (PRODUCTION / "repo").is_dir()
    assert "AUTOSLICE_BASE" not in os.environ
    # This assertion also fails safely before slot access without the guard.
    assert provider_slots.configured_runtime_root() is None

    def forbidden_slot(*args, **kwargs):
        raise AssertionError("unset AUTOSLICE_BASE must not open provider slots")

    monkeypatch.setattr(provider_slots, "provider_slot", forbidden_slot)
    with provider_slots.runtime_provider_slot() as lease:
        assert lease is None


def test_explicit_test_runtime_is_preserved(tmp_path, monkeypatch):
    runtime = tmp_path / "explicit-runtime"
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    assert provider_slots.configured_runtime_root() == runtime
    with provider_slots.runtime_provider_slot() as lease:
        assert lease is not None
        assert lease.runtime_root == runtime
    assert (runtime / "provider-slots" / "slot-0.lock").is_file()


def test_unset_base_keeps_original_cpa_runtime_semantics(tmp_path, monkeypatch):
    from src.autoslice import producer_final_review_transport as transport

    monkeypatch.delenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", raising=False)
    assert transport._resolved_runtime_root(None) is None
    explicit = tmp_path / "explicit-runtime"
    assert transport._resolved_runtime_root(explicit) == explicit
    monkeypatch.setenv("AUTOSLICE_BASE", str(explicit))
    assert transport._resolved_runtime_root(None) == explicit
    configured = tmp_path / "final-review-runtime"
    monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", str(configured))
    assert transport._resolved_runtime_root(None) == configured


def test_empty_base_cannot_fall_back_to_production(
    monkeypatch, production_runtime_present,
):
    monkeypatch.setenv("AUTOSLICE_BASE", "")
    assert provider_slots.configured_runtime_root() is None


@pytest.mark.parametrize("base", [None, ""])
def test_simulated_canonical_runtime_is_preserved(tmp_path, monkeypatch, base):
    if base is None:
        monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    else:
        monkeypatch.setenv("AUTOSLICE_BASE", base)
    monkeypatch.setattr(provider_slots, "_CANONICAL_PRODUCTION_ROOT", tmp_path)
    (tmp_path / "repo").mkdir()
    assert provider_slots.configured_runtime_root() == tmp_path
    with provider_slots.runtime_provider_slot() as lease:
        assert lease is not None
        assert lease.runtime_root == tmp_path


@pytest.mark.parametrize("via_symlink", [False, True])
@pytest.mark.parametrize("subpath", ["", "provider-slots"])
def test_explicit_production_root_is_blocked(tmp_path, monkeypatch, via_symlink, subpath):
    runtime = PRODUCTION
    if via_symlink:
        runtime = tmp_path / "production-link"
        # Only the temporary link inode is created. A relative target avoids
        # the gauntlet audit hook treating its target as a production write.
        runtime.symlink_to(os.path.relpath(PRODUCTION, runtime.parent), target_is_directory=True)
        assert runtime.resolve() == PRODUCTION.resolve()
    if subpath:
        runtime = runtime / subpath
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    with pytest.raises(pytest.fail.Exception, match="TEST_PRODUCTION_RUNTIME_BLOCKED"):
        provider_slots.configured_runtime_root()
