"""Public-only synthetic coverage boundaries; no real media or private proof."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import publication_content_coverage as coverage
from src.autoslice import publication_registry as registry
from src.autoslice.publication_readiness import build_readiness_graph


def _manifest(tmp_path: Path, record: object) -> dict:
    path = tmp_path / "synthetic.record.json"
    raw = json.dumps(record).encode()
    path.write_bytes(raw)
    return {"package_attestation": {"record": {
        "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
    }}}


@pytest.mark.parametrize("document", [{}, {"content_coverage": None}])
def test_absent_coverage_is_the_ordinary_single_candidate_case(document):
    assert coverage.validate_publication_content_coverage(document) == []
    assert coverage.derive_publication_content_coverage({}, document) is None


@pytest.mark.parametrize("claim", [{}, [], "", False, 0, {"status": "PASS", "covered_candidate_ids": ["synthetic-child"]}])
def test_any_declared_coverage_is_unavailable_not_silently_accepted(claim):
    with pytest.raises(ValueError, match="PUBLICATION_CONTENT_COVERAGE_ADAPTER_UNAVAILABLE"):
        coverage.validate_publication_content_coverage({"content_coverage": claim})
    with pytest.raises(ValueError, match="PUBLICATION_CONTENT_COVERAGE_ADAPTER_UNAVAILABLE"):
        coverage.derive_publication_content_coverage({"content_coverage": claim}, {})


@pytest.mark.parametrize("record", [{}, {"story_contract": {}}, {"story_contract": {"source_fact_review": {}}}])
def test_ordinary_bound_records_do_not_require_private_adapter(tmp_path, record):
    manifest = _manifest(tmp_path, record)
    before = Path(manifest["package_attestation"]["record"]["path"]).read_bytes()
    assert coverage.derive_publication_content_coverage(manifest, {}) is None
    assert Path(manifest["package_attestation"]["record"]["path"]).read_bytes() == before


@pytest.mark.parametrize("successor", [{}, False, "", {"schema_version": "synthetic-merged-proof.v1", "status": "PASS"}])
def test_source_successor_claim_is_not_discarded(tmp_path, successor):
    manifest = _manifest(tmp_path, {"story_contract": {"source_fact_review": {"final_review_successor": successor}}})
    with pytest.raises(ValueError, match="PUBLICATION_CONTENT_COVERAGE_ADAPTER_UNAVAILABLE"):
        coverage.derive_publication_content_coverage(manifest, {})


@pytest.mark.parametrize("damage", ["hash", "size", "symlink", "nonobject"])
def test_malformed_record_binding_is_rejected(tmp_path, damage):
    manifest = _manifest(tmp_path, [] if damage == "nonobject" else {})
    binding = manifest["package_attestation"]["record"]
    if damage == "hash":
        binding["sha256"] = "0" * 64
    elif damage == "size":
        binding["bytes"] = True
    elif damage == "symlink":
        alias = tmp_path / "alias.record.json"
        alias.symlink_to(binding["path"])
        binding["path"] = str(alias)
    with pytest.raises((OSError, ValueError)):
        coverage.derive_publication_content_coverage(manifest, {})


def test_unavailable_proof_blocks_native_registry_and_cannot_hide_a_child(tmp_path):
    repo, runtime = tmp_path / "repo", tmp_path / "runtime"
    (repo / "assets/lidousha").mkdir(parents=True)
    (runtime / "state").mkdir(parents=True)
    (runtime / "reports").mkdir()
    (runtime / "reports/upload_ledger.jsonl").write_text("")
    path = repo / "assets/lidousha/publication_registry.v1.json"
    path.write_text(json.dumps({"schema_version": "publication-registry.v1", "entries": [{
        "candidate_id": "synthetic-parent", "recording_date": "2026-01-01",
        "status": "published", "bvid": "BVsynthetic",
        "publication_reconciliation": {"content_coverage": {"covered_candidate_ids": ["synthetic-child"]}},
    }]}))
    (runtime / "state/2026-01-01.json").write_text(json.dumps({
        "pending_talk": [{"candidate_id": "synthetic-child", "status": "covered_by_publication"}],
    }))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="PUBLICATION_CONTENT_COVERAGE_ADAPTER_UNAVAILABLE"):
        registry.load_publication_registry(path)
    assert registry.upload_block_reason("synthetic-child", registry_path=path)
    assert registry.cover_maintenance_block_reason("synthetic-child", registry_path=path)
    graph = build_readiness_graph(repository_root=repo, runtime_root=runtime)
    assert any(row["code"] == "PUBLICATION_REGISTRY_INVALID" for row in graph["graph_blockers"])
    assert graph["excluded_covered"] == []
    assert [row["candidate_id"] for row in graph["rows"]] == ["synthetic-child"]
    assert all(row["category"] != "READY_FOR_SERIAL_UPLOAD" for row in graph["rows"])
    assert all(p.read_bytes() == raw for p, raw in before.items())
