"""Check interruption and ambiguity at the shared durable observation seam."""
import pytest

from src.autoslice.completed_review_checkpoint import (
    CompletedReviewCheckpoint,
    CompletedReviewCheckpointError,
)


def _checkpoint(tmp_path, stage="boundary"):
    return CompletedReviewCheckpoint(tmp_path / "observations", {"stage": stage, "current": "bound"}, "complete request")


def test_complete_staged_response_recovers_without_installed_name(tmp_path, monkeypatch):
    import src.autoslice.completed_review_checkpoint as module
    checkpoint = _checkpoint(tmp_path)
    checkpoint.raw = "complete negative response"
    real_create = module.create_staged_inode

    def stop_after_save(path, **kwargs):
        real_create(path, **kwargs)
        raise KeyboardInterrupt("simulated interruption before final-name install")

    monkeypatch.setattr(module, "create_staged_inode", stop_after_save)
    with pytest.raises(KeyboardInterrupt):
        checkpoint.save()
    originals = {p: p.read_bytes() for p in checkpoint.directory.glob(".*.tmp")}
    assert originals and not list(checkpoint.directory.glob("*.json"))
    restored = _checkpoint(tmp_path)
    assert restored.response(lambda _: pytest.fail("completed response redispatched")) == checkpoint.raw
    monkeypatch.setattr(module, "create_staged_inode", real_create)
    restored.save()
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert _checkpoint(tmp_path).load() == checkpoint.raw


def test_different_complete_responses_are_ambiguous_and_preserved(tmp_path):
    first, second = _checkpoint(tmp_path), _checkpoint(tmp_path)
    first.raw, second.raw = "first negative observation", "second negative observation"
    first.save()
    with pytest.raises(CompletedReviewCheckpointError, match="COMPLETED_REVIEW_AMBIGUOUS"):
        second.save()
    original = {p: p.read_bytes() for p in first.directory.glob("*.json")}
    with pytest.raises(CompletedReviewCheckpointError, match="COMPLETED_REVIEW_AMBIGUOUS"):
        _checkpoint(tmp_path).response(lambda _: pytest.fail("ambiguous response redispatched"))
    # A writer that already has the first raw response must reject the later
    # disagreement too, including its existing-identical-target save branch.
    with pytest.raises(CompletedReviewCheckpointError, match="COMPLETED_REVIEW_AMBIGUOUS"):
        first.save()
    assert len(original) == 2 and all(path.read_bytes() == raw for path, raw in original.items())


def test_stage_change_does_not_invalidate_unaffected_observation(tmp_path):
    boundary, facts = _checkpoint(tmp_path), _checkpoint(tmp_path, "facts")
    boundary.raw, facts.raw = "boundary BLOCK", "facts BLOCK"
    boundary.save()
    facts.save()
    changed = CompletedReviewCheckpoint(tmp_path / "observations", {"stage": "facts", "current": "changed"}, "changed request")
    assert changed.load() is None
    assert _checkpoint(tmp_path).load() == "boundary BLOCK"
    assert _checkpoint(tmp_path, "facts").load() == "facts BLOCK"
