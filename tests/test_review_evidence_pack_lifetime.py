"""Native evidence-writer lifetime regressions; no providers or replaced writers."""
from __future__ import annotations

import gc
import hashlib
import json
import weakref

import pytest

from src.autoslice.review_evidence_store import (
    REF_SCHEMA,
    compact_review,
    load_review,
    persist_document,
)


class _LifetimeText(str):
    """A supported JSON scalar observed through a non-owning weak reference."""


@pytest.mark.parametrize("api", ["compact_review", "persist_document"])
@pytest.mark.parametrize("reject", [False, True], ids=["success", "exception"])
def test_public_compaction_releases_original_graph_without_cyclic_gc(tmp_path, api, reject):
    output = tmp_path / "review.json"

    def call_and_drop_source():
        marker = _LifetimeText("small compaction lifetime probe")
        probe = weakref.ref(marker)
        # A request is a real reference-bearing field, even for this tiny
        # payload. The returned compact document therefore cannot own marker.
        source = {"request": {"probe": marker}}
        if reject:
            # Dict iteration publishes the completed request before this
            # sibling fails, so the failure must unwind an occupied memo.
            source["bad"] = object()

        def publish():
            if api == "compact_review":
                return compact_review(source, tmp_path)
            return persist_document(output, source)

        if reject:
            try:
                publish()
            except TypeError as exc:
                assert "not JSON serializable" in str(exc)
            else:
                pytest.fail("invalid sibling must retain its JSON encoding failure")
            return probe, None
        result = publish()
        assert result["request"]["schema_version"] == REF_SCHEMA
        assert "probe" not in result["request"]
        return probe, result

    was_enabled = gc.isenabled()
    gc.disable()
    try:
        probe, packed = call_and_drop_source()
        assert probe() is None, "completed compaction retained the source graph until cyclic GC"
        # Keep the returned compact document alive during the lifetime check.
        if not reject:
            assert packed is not None and packed["request"]["schema_version"] == REF_SCHEMA
        else:
            assert packed is None and not output.exists()
    finally:
        try:
            gc.collect()
        finally:
            if was_enabled:
                gc.enable()


def test_shared_reference_publication_preserves_exact_bytes_hashes_and_round_trip(tmp_path):
    shared = {"terms": ["中文", "😀", True, None, 1.25], "nested": {"surface": "原文"}}
    source = {"request": shared, "terminal_review": shared, "inline": {"status": "KEPT"}}
    output = tmp_path / "review.json"
    packed = persist_document(output, source)

    raw = json.dumps(shared, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    blob = tmp_path.absolute() / "evidence-store" / "blobs" / (digest + ".bin")
    reference = {"schema_version": REF_SCHEMA, "sha256": digest, "path": str(blob)}
    expected = {"request": reference, "terminal_review": reference, "inline": {"status": "KEPT"}}

    assert packed == expected
    assert output.read_bytes() == json.dumps(
        expected, ensure_ascii=False, sort_keys=True, indent=2
    ).encode() + b"\n"
    assert blob.read_bytes() == raw
    assert list(blob.parent.glob("*.bin")) == [blob]
    assert load_review(output) == source
    assert source["request"] is source["terminal_review"] is shared
