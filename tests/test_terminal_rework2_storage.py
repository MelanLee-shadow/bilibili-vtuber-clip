"""Concurrency contracts for content-addressed review evidence blobs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest


def _publish_process(root: str, payloads: tuple[bytes, ...], loops: int, output) -> None:
    """Publish and cold-read blobs in a fresh interpreter."""

    from src.autoslice import argon_escalation_storage as store

    try:
        for index in range(loops):
            raw = payloads[index % len(payloads)]
            digest = store.blob(Path(root), raw)
            if digest != hashlib.sha256(raw).hexdigest():
                raise AssertionError("blob digest drifted")
            if store.get_blob(Path(root), digest) != raw:
                raise AssertionError("blob bytes drifted")
    except BaseException as exc:  # pragma: no cover - reported by the parent
        output.put((type(exc).__name__, str(exc)))
    else:
        output.put(None)


def test_blob_publishers_are_content_addressed_and_do_not_change_immutable_writes(tmp_path):
    from src.autoslice import argon_escalation_storage as store

    root = tmp_path / "evidence-store"
    first = b"same immutable artifact"
    second = b"different immutable artifact"
    assert store.blob(root, first) == hashlib.sha256(first).hexdigest()
    assert store.blob(root, first) == hashlib.sha256(first).hexdigest()
    assert store.get_blob(root, hashlib.sha256(first).hexdigest()) == first

    ordinary = root / "ordinary.json"
    assert store.atomic_write(ordinary, first, create_only=True)
    assert not store.atomic_write(ordinary, second, create_only=True)
    assert store.read(ordinary) == first

    immutable = root / "immutable.json"
    store.immutable(immutable, first)
    store.immutable(immutable, first)
    with pytest.raises(ValueError, match="IMMUTABLE_ARTIFACT_CONFLICT"):
        store.immutable(immutable, second)


def test_blob_publish_and_cold_read_survive_threaded_same_hash_replacement(tmp_path):
    from src.autoslice import argon_escalation_storage as store

    root = tmp_path / "threaded"
    raw = (b"threaded content-addressed payload\0" * 32_768)
    digest = store.blob(root, raw)

    def writer(_index: int) -> str:
        for _ in range(30):
            assert store.blob(root, raw) == digest
        return digest

    def reader(_index: int) -> bytes:
        for _ in range(60):
            assert store.get_blob(root, digest) == raw
        return raw

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(writer, index) for index in range(4)]
        futures.extend(pool.submit(reader, index) for index in range(8))
        assert [future.result() for future in futures[:4]] == [digest] * 4
        assert [future.result() for future in futures[4:]] == [raw] * 8


def test_blob_publish_and_cold_read_survive_multiprocess_same_and_distinct_hashes(tmp_path):
    from src.autoslice import argon_escalation_storage as store

    root = tmp_path / "multiprocess"
    payloads = (
        b"multiprocess-a\0" * 65_536,
        b"multiprocess-b\0" * 65_536,
        b"multiprocess-c\0" * 65_536,
    )
    # Create the private root before workers race to create its blobs child.
    store.directory(root)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(target=_publish_process, args=(str(root), payloads, 24, output))
        for _ in range(6)
    ]
    for process in processes:
        process.start()
    results = [output.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert results == [None] * len(processes)
    for raw in payloads:
        digest = hashlib.sha256(raw).hexdigest()
        assert (root / "blobs" / f"{digest}.bin").is_file()
        assert store.get_blob(root, digest) == raw


def test_locked_keeps_nonblocking_create_only_signature():
    from inspect import signature

    from src.autoslice import argon_escalation_storage as store

    assert "blocking" not in signature(store.locked).parameters


def test_persist_review_write_failure_is_visible(tmp_path, monkeypatch):
    from src.autoslice import final_review_auditor, review_evidence_store

    def fail(*_args, **_kwargs):
        raise OSError("synthetic evidence-store write failure")

    monkeypatch.setattr(review_evidence_store, "persist_review", fail)
    with pytest.raises(OSError, match="synthetic evidence-store write failure"):
        final_review_auditor.persist_review_audit(tmp_path / "review-flags.json", {})


def test_script_evidence_readers_expand_compact_records(tmp_path):
    """Every migrated script seam must consume the same compact record."""

    from src.autoslice.review_evidence_store import (
        persist_document,
        persist_review,
    )

    original = {
        "schema_version": "final-review-audit.v2",
        "findings": [],
        "request": {"payload": "compact evidence" * 2_000},
    }
    review_path = tmp_path / "clip.review-flags.json"
    chat_path = tmp_path / "clip.chat-authority.json"
    persist_review(review_path, original)
    chat_document = {"final_review_audit": original}
    persist_document(chat_path, chat_document)

    from scripts import ab_model_replay_closed_set as ab_replay
    from scripts import audit_review_package as package_audit
    from scripts import build_fastlane_c10_media_successor as c10_builder
    from scripts import build_daily_review_manifest as daily_manifest
    from scripts import build_recovery_review_manifest as recovery_manifest
    from scripts import build_manual_review_manifest as manual_manifest
    from scripts import build_t06_bookshop_same_bv_extension_audio_package as t06_audio
    from scripts import build_t06_bookshop_same_bv_extension_package as t06_package
    from scripts import finalize_b2_caption_same_bv_recovery_package as b2_finalizer
    from scripts import finalize_e353_content_package as e353_finalizer
    from scripts import finalize_t06_bookshop_same_bv_extension_recovery_package as t06_finalizer
    from scripts import replay_exact_final_funnel as exact_replay
    from scripts import replay_reviewed_subtitle_baseline as baseline_replay
    from scripts import resume_frozen_talk_package as frozen_resume
    from scripts import scan_state_dangling_media_refs as state_scan
    from scripts import subtitle_regression_eval as regression_eval

    readers = (
        ("ab replay", lambda: ab_replay.load_review(chat_path), chat_document),
        ("package audit", lambda: package_audit._load_json(chat_path), chat_document),
        ("C10 builder", lambda: c10_builder.load_review(chat_path), chat_document),
        ("daily manifest", lambda: daily_manifest.load_review(chat_path), chat_document),
        ("recovery manifest", lambda: recovery_manifest._load_json(chat_path), chat_document),
        ("manual manifest", lambda: manual_manifest.load_review(chat_path), chat_document),
        ("T06 audio", lambda: t06_audio._load(chat_path), chat_document),
        ("T06 package", lambda: t06_package._load(chat_path), chat_document),
        ("B2 finalizer", lambda: b2_finalizer._json(chat_path), chat_document),
        ("E353 finalizer", lambda: e353_finalizer._json(chat_path), chat_document),
        ("T06 finalizer", lambda: t06_finalizer._json(chat_path), chat_document),
        ("exact replay", lambda: exact_replay.load_review(review_path), original),
        (
            "baseline replay",
            lambda: baseline_replay._read_bound_small_json(review_path, label="REVIEW_FLAGS"),
            original,
        ),
        ("frozen resume", lambda: frozen_resume._read_json(chat_path, label="chat"), chat_document),
        (
            "state scan",
            lambda: state_scan.expand(json.loads(review_path.read_text())),
            original,
        ),
        (
            "subtitle regression checkpoint",
            lambda: regression_eval.review_from_bytes(
                chat_path.read_bytes(), directory=chat_path.parent
            ),
            {"final_review_audit": original},
        ),
    )
    for label, reader, expected in readers:
        assert reader() == expected, label


def _compact_review_fixture(tmp_path: Path) -> tuple[dict, Path]:
    from src.autoslice.review_evidence_store import persist_review

    review = {
        "schema_version": "final-review-audit.v2",
        "findings": [],
        "request": {"payload": "compact source reader evidence" * 2_000},
    }
    path = tmp_path / "review-flags.json"
    persist_review(path, review)
    assert "review-evidence-reference.v1" in path.read_text(encoding="utf-8")
    return review, path


def test_src_review_readers_expand_compact_records(tmp_path):
    from src.autoslice import producer_package_finalization as finalization
    from src.autoslice import producer_text_pipeline as pipeline
    from src.autoslice import retained_reviewed_draft as retained

    review, path = _compact_review_fixture(tmp_path)
    loaded, digest = pipeline._continuation_read_json(
        path, label="TEST_REVIEW", review=True,
    )
    assert loaded == review
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()

    retained_path, retained_raw, retained_value, retained_digest = retained._read_json(
        str(path), "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        label="PARENT_AUTHORITY", review=True,
    )
    assert retained_path == path and retained_raw == path.read_bytes()
    assert retained_digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert retained_value == review
    assert finalization._read_review_document(path, label="TEST_REVIEW") == review


def test_checkpoint_review_readers_expand_compact_nested_authority(tmp_path):
    from src.autoslice import producer_text_checkpoint as checkpoint
    from src.autoslice.boundary_semantic_review import cue_grid_sha256
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.review_evidence_store import persist_document

    srt = "1\n00:00:01,000 --> 00:00:03,000\n保留原文\n"
    srt_path = tmp_path / "input.srt"
    srt_path.write_text(srt, encoding="utf-8")
    boundary_path = tmp_path / "boundary.srt"
    boundary_path.write_text(srt, encoding="utf-8")
    boundary_sha = hashlib.sha256(srt.encode()).hexdigest()
    grid = cue_grid_sha256(parse_srt_cues(srt))
    boundary_context = {
        "schema_version": "source-boundary-transcript-context.v1",
        "role": "SOURCE_DOMAIN_BOUNDARY_REVIEW_CONTEXT",
        "source_srt_path": str(boundary_path),
        "source_srt_sha256": "sha256:" + boundary_sha,
        "source_cue_grid_sha256": grid,
        "padding": "x" * 9_000,
    }
    review = {
        "schema_version": "final-review-audit.v2",
        "findings": [],
        "source_boundary_context": boundary_context,
        "boundary_semantic_review": {
            "review_scope": "source_full_window",
            "cue_grid_sha256": grid,
        },
    }
    authority = {
        "final_output_srt_sha256": boundary_sha,
        "post_transcript_entity_output_srt": srt,
        "post_transcript_entity_output_srt_sha256": boundary_sha,
        "final_review_audit": review,
    }
    authority_path = tmp_path / "authority.json"
    persist_document(authority_path, authority)
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"synthetic media")
    context_path = tmp_path / "context.json"
    context_path.write_text("{}\n", encoding="utf-8")

    checkpoint.write_source_text_checkpoint(
        tmp_path / "source-checkpoint.json", candidate_id="candidate",
        srt_path=srt_path, authority_path=authority_path,
        context_path=context_path, padded=padded,
    )
    checkpoint.write_current_text_checkpoint(
        tmp_path / "current-checkpoint.json", candidate_id="candidate",
        srt_path=srt_path, authority_path=authority_path,
        context_path=context_path, padded=padded, source_clock={},
    )


def test_subtitle_recovery_reads_compact_authority_and_review_blobs(tmp_path):
    from src.autoslice import subtitle_regression_recovery as recovery
    from src.autoslice.review_evidence_store import persist_document, review_from_bytes

    srt = "1\n00:00:01,000 --> 00:00:03,000\n保留原文\n".encode()
    review = {
        "schema_version": "final-review-audit.v2",
        "findings": [],
        "request": {"payload": "compact recovery evidence" * 2_000},
    }
    authority = {"final_review_audit": review}
    authority_path = tmp_path / "authority.json"
    persist_document(authority_path, authority)
    compact_authority = json.loads(authority_path.read_bytes())
    out = tmp_path / "package"
    out.mkdir()
    recovery.package_checkpoint(
        out, phase="REVIEW_STARTED", run={"would_release": None},
        text=srt, authority=compact_authority,
    )
    # The stage blob itself is compact, and the normal review pointer accepts
    # the same representation when no completed source verdict is imported.
    result = recovery.validate_stage_history(out, srt, compact_authority)
    assert result["stages"] == 1
    authority_raw = authority_path.read_bytes()
    expanded_authority = review_from_bytes(authority_raw, directory=authority_path.parent)
    expanded_result = recovery.validate_stage_history(
        out, srt, expanded_authority,
        authority_sha256=recovery.sha(recovery.canonical(compact_authority)),
    )
    assert expanded_result["current_matches_last_stage"] is True


def test_additional_src_review_readers_expand_compact_records_and_reject_hash_drift(tmp_path):
    """The remaining source seams parse the captured compact wire bytes."""

    from dataclasses import replace

    from src.autoslice import c6_exact_final_boundary_adapter as c6
    from src.autoslice import operator_exact_title_source_fact_authority as title
    from src.autoslice import producer_prepared_recovery as prepared
    from src.autoslice import retained_source_input as retained
    from src.autoslice import review_package_source_boundary as source_boundary
    from src.autoslice import reviewed_baseline_replay as replay
    from src.autoslice.boundary_semantic_review import semantic_review_sha256
    from src.autoslice.review_evidence_store import persist_document, persist_review

    review = {
        "schema_version": "final-review-audit.v2",
        "status": "CLEAN",
        "findings": [],
        "request": {"payload": "source seam compact evidence" * 2_000},
    }
    review_path = tmp_path / "candidate.review-flags.json"
    persist_review(review_path, review)
    raw = review_path.read_bytes()
    expected = "sha256:" + hashlib.sha256(raw).hexdigest()

    assert c6._load_json(review_path, expected, label="CHAT_AUTHORITY", review=True) == review
    with pytest.raises(c6.C6ExactFinalBoundaryAdapterError, match="HASH_MISMATCH"):
        c6._load_json(review_path, "sha256:" + "0" * 64, label="CHAT_AUTHORITY", review=True)

    binding = replay.regular_binding(review_path, label="CHAT_AUTHORITY")
    assert replay._load_json(binding, label="CHAT_AUTHORITY", review=True) == review
    with pytest.raises(replay.ReviewedBaselineReplayError, match="REPLAY_CHAT_AUTHORITY_DRIFT"):
        replay._load_json(
            replace(binding, sha256="sha256:" + "0" * 64),
            label="CHAT_AUTHORITY",
            review=True,
        )

    assert prepared._document(review_path, review=True) == review

    retained_path, retained_value = retained._bound_json(
        review_path, expected, review=True
    )
    assert retained_path == review_path and retained_value == review
    with pytest.raises(ValueError, match="RETAINED_SOURCE_INPUT_PARENT_DRIFT"):
        retained._bound_json(review_path, "sha256:" + "0" * 64, review=True)

    # A compact chat-authority snapshot is the source-boundary reader's actual
    # input.  Its filename remains a hash of the captured wire bytes.
    source = {
        "schema_version": "talk-boundary-semantic-review.v1",
        "status": "PASS",
        "review_scope": "source_full_window",
        "request_sha256": "sha256:" + "d" * 64,
        "cue_grid_sha256": "sha256:" + "e" * 64,
        "final_endpoint_binding": {"final_end_ms": 2_000},
    }
    prior = {key: value for key, value in source.items() if key != "final_endpoint_binding"}
    chat_snapshot = tmp_path / "current-text-input"
    chat_snapshot.mkdir()
    chat_document = {
        "final_review_audit": {
            "schema_version": "final-review-audit.v2",
            "boundary_semantic_review": prior,
            "request": {"payload": "boundary snapshot compact evidence" * 2_000},
        }
    }
    chat_path = chat_snapshot / "authority.pending.json"
    persist_document(chat_path, chat_document)
    wire = chat_path.read_bytes()
    hashed_chat = chat_snapshot / ("authority." + hashlib.sha256(wire).hexdigest() + ".json")
    chat_path.rename(hashed_chat)
    expected_source = semantic_review_sha256(source)
    later = dict(source, summary="later source review")
    record = {
        "chat_authority_audit_path": str(tmp_path / "current.chat-authority.json"),
        "boundary_audit": {"boundary_semantic_review": later},
        "story_contract": {
            "boundary_semantic_review": {
                "source_separation_witness": {"source_review_sha256": expected_source}
            }
        },
    }
    assert source_boundary.original_source_review(record, snapshot_root=chat_snapshot) == source
    bad = hashed_chat.read_bytes() + b"\n"
    hashed_chat.write_bytes(bad)
    with pytest.raises(ValueError, match="preimage"):
        source_boundary.original_source_review(record, snapshot_root=chat_snapshot)

    title_review_path = tmp_path / "title.review-flags.json"
    persist_review(title_review_path, review)
    assert title._json_file(
        title_review_path,
        title.bytes_sha256(title_review_path.read_bytes()),
        "FINAL_REVIEW",
        review=True,
    ) == review
    with pytest.raises(title.OperatorExactTitleSourceFactAuthorityError, match="HASH_MISMATCH"):
        title._json_file(
            title_review_path, "sha256:" + "0" * 64, "FINAL_REVIEW", review=True
        )
