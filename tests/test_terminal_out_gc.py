"""Disposable fixtures for the rejected-candidate terminal collector."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


gc = importlib.import_module("scripts.terminal_out_gc")


DATE = "2026-10-01"
CID = "songvis_123_456_abcdef12"


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, source_exists: bool = True):
    base = tmp_path / "runtime"
    candidate = base / "out" / DATE / CID
    candidate.mkdir(parents=True)
    recording_dir = base / "recordings" / DATE
    recording_dir.mkdir(parents=True)
    recording = recording_dir / "22966160_20261001-20-00-00.mp4"
    source_bytes = b"recording-source\n" * 128
    if source_exists:
        recording.write_bytes(source_bytes)
    row = {
        "candidate_id": CID,
        "status": "candidate_rejected",
        "segment": recording.name,
        "segment_size_bytes": len(source_bytes),
        "seg_dur_ms": 10_000,
        "start_ms": 1_000,
        "end_ms": 9_000,
        "context_start_ms": 0,
        "context_end_ms": 10_000,
        "context_duration_ms": 10_000,
    }
    state = {
        "songs": [row],
        "picks": [],
        "pending_song": [],
        "pending_talk": [],
        "source_integrity": {
            "schema_version": "recording-inventory-audit.v1",
            "status": "PASS",
            "can_select": True,
            "issues": [],
            "date_dir": str(recording_dir),
            "consumer_segments": [str(recording)],
        },
    }
    (base / "state").mkdir()
    (base / "state" / f"{DATE}.json").write_text(json.dumps(state), encoding="utf-8")
    state_dir = tmp_path / "gc-state"
    monkeypatch.setattr(gc, "quiet_window", lambda _base: [])
    monkeypatch.setattr(gc, "authority_references", lambda _base: (set(), set()))
    # Lifecycle/recipe unit tests use a quiet census when the test runner cannot
    # inspect every user's processes. Run this module as root for real /proc.
    if not Path("/proc").is_dir() or os.geteuid() != 0:
        monkeypatch.setattr(gc, "_proc_census", lambda *_args: {"available": True, "open_inodes": [], "active_namespaces": []})
    return base, candidate, recording, state_dir


def _source_target(candidate: Path) -> Path:
    target = candidate / f"{CID}-source-context.context.mp4"
    target.write_bytes(b"window-bytes\n")
    return target


def _cache_alias(candidate: Path, target: Path) -> Path:
    cache = candidate / ".context_clip_cache"
    cache.mkdir()
    alias = cache / ("a" * 64 + ".mp4")
    os.link(target, alias)
    return alias


def test_external_source_requires_configured_root_and_still_binds_health(tmp_path, monkeypatch):
    base, candidate, recording, state_dir = _fixture(tmp_path, monkeypatch)
    cloud_root = tmp_path / "cloud-recordings"
    moved = cloud_root / DATE / recording.name
    moved.parent.mkdir(parents=True)
    recording.rename(moved)
    state_path = base / "state" / f"{DATE}.json"
    state = json.loads(state_path.read_text())
    state["source_integrity"]["date_dir"] = str(moved.parent)
    state["source_integrity"]["consumer_segments"] = [str(moved)]
    state_path.write_text(json.dumps(state))
    target = _source_target(candidate)
    monkeypatch.delenv("AUTOSLICE_GC_SOURCE_ROOTS", raising=False)
    assert gc.build_plan(base, state_dir)["planned_files"] == 0
    monkeypatch.setenv("AUTOSLICE_GC_SOURCE_ROOTS", str(cloud_root))
    assert gc.build_plan(base, state_dir)["planned_files"] == 1
    assert not gc._source_root_allowed(base, tmp_path / "cloud-recordings-other" / "raw.mp4")
    state["source_integrity"]["consumer_segments"] = []
    state_path.write_text(json.dumps(state))
    assert gc.build_plan(base, state_dir)["planned_files"] == 0
    assert target.exists() and moved.exists()


@pytest.mark.parametrize("root", ["relative/root", "/", "/approved/../other"])
def test_invalid_external_source_root_is_rejected(tmp_path, monkeypatch, root):
    monkeypatch.setenv("AUTOSLICE_GC_SOURCE_ROOTS", root)
    with pytest.raises(gc.Keep, match="absolute non-root paths"):
        gc._source_root_allowed(tmp_path, tmp_path / "recordings" / "raw.mp4")


def test_valid_closed_source_context_cache_group_deletes_both_once(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    alias = _cache_alias(candidate, target)

    report = gc.build_plan(base, state_dir)
    assert report["planned_files"] == 2
    assert report["groups"][0]["link_class"] == "source-context-cache"
    applied = gc.apply_plan(report)
    assert applied["removed_files"] == 2
    assert not target.exists() and not alias.exists()
    receipts = list((state_dir / "recoveries" / "terminal-out").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["status"] == "REMOVED"
    assert len(receipt["removed_paths"]) == 2


def test_outside_hardlink_alias_keeps_the_group(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    os.link(target, base / "outside.mp4")
    report = gc.build_plan(base, state_dir)
    assert report["groups"] == []
    assert any(item["reason"] == "inode_alias_outside_known_set" for item in report["kept"])


def test_cited_cache_alias_keeps_whole_inode_group(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    alias = _cache_alias(candidate, target)
    monkeypatch.setattr(gc, "authority_references", lambda _base: ({str(alias)}, set()))
    report = gc.build_plan(base, state_dir)
    assert report["groups"] == []
    assert any(item["reason"] == "authority_reference" for item in report["kept"])


def test_pending_and_unknown_owner_are_kept(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    _source_target(candidate)
    state_path = base / "state" / f"{DATE}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pending_song"] = [{"cid": CID}]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    pending = gc.build_plan(base, state_dir)
    assert pending["groups"] == []
    assert any(item["reason"] == "owner_has_pending_work" for item in pending["kept"])

    unknown_cid = "songvis_999_999_abcdef12"
    unknown = base / "out" / DATE / unknown_cid / f"{unknown_cid}_source.mp4"
    unknown.parent.mkdir()
    unknown.write_bytes(b"unknown")
    state["pending_song"] = []
    state_path.write_text(json.dumps(state), encoding="utf-8")
    report = gc.build_plan(base, state_dir)
    assert any(item["reason"] == "owner_status_unknown" for item in report["kept"])


def test_lost_source_is_kept(tmp_path, monkeypatch):
    base, candidate, recording, state_dir = _fixture(tmp_path, monkeypatch)
    _source_target(candidate)
    recording.unlink()
    report = gc.build_plan(base, state_dir)
    assert report["groups"] == []
    assert any("recording source" in item["reason"] for item in report["kept"])


def test_source_proof_binds_exact_segment_metadata_and_ignores_other_warn(tmp_path, monkeypatch):
    base, candidate, recording, state_dir = _fixture(tmp_path, monkeypatch)
    _source_target(candidate)
    state_path = base / "state" / f"{DATE}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    row = state["songs"][0]
    row.pop("segment_size_bytes")
    row.pop("seg_dur_ms")
    stem = recording.stem
    state["segment_durations_ms"] = {stem: 10_000, "other-segment": 999_000}
    state["segment_scene_contexts"] = {
        stem: {"input_signatures": {"segment": {"size_bytes": recording.stat().st_size}}},
        "other-segment": {"input_signatures": {"segment": {"size_bytes": 1}},},
    }
    state["source_integrity"]["issues"] = [{"severity": "WARN", "segment_stem": "other-segment"}]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    report = gc.build_plan(base, state_dir)
    assert report["planned_files"] == 1

    state["source_integrity"]["issues"] = [{"severity": "BLOCK", "segment_stem": stem, "code": "SOURCE_BAD"}]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    blocked = gc.build_plan(base, state_dir)
    assert blocked["groups"] == []
    assert any("SOURCE_BAD" in item["reason"] for item in blocked["kept"])


def test_preimage_hash_drift_stops_apply_without_deleting(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    report = gc.build_plan(base, state_dir)
    target.write_bytes(b"window-drift")
    applied = gc.apply_plan(report)
    assert applied["removed_files"] == 0
    assert target.exists()
    assert applied["complete"] is False


def test_apply_requires_complete_proc_visibility(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    report = gc.build_plan(base, state_dir)
    monkeypatch.setattr(gc, "_proc_census", lambda *_args: {"available": False})
    result = gc.apply_plan(report)
    assert result["removed_files"] == 0
    assert result["complete"] is False
    assert target.exists()


def test_registered_runtime_alias_is_allowed_but_unknown_link_blocks(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    runtime = base / "venv-main"
    runtime.mkdir()
    private = base / "private-runs" / "overflow"
    private.mkdir(parents=True)
    (private / "venv-main").symlink_to(runtime, target_is_directory=True)
    assert gc.build_plan(base, state_dir)["planned_files"] == 1
    (private / "unknown-source").symlink_to(target)
    with pytest.raises(gc.GateError, match="unsafe link"):
        gc.build_plan(base, state_dir)


def test_private_canonical_directory_aliases_scan_once_and_keep_referenced_target(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    private = base / "private-runs"
    canonical = private / "source-run"
    canonical.mkdir(parents=True)
    reference = canonical / "run.json"
    reference.write_text(json.dumps({"target": str(target)}), encoding="utf-8")
    for alias_name in ("run-a", "run-b"):
        alias_parent = private / alias_name
        alias_parent.mkdir()
        (alias_parent / "runtime").symlink_to("../source-run", target_is_directory=True)

    original_safe_text = gc._safe_text
    scanned: list[Path] = []

    def count_scan(path: Path):
        scanned.append(path)
        return original_safe_text(path)

    monkeypatch.setattr(gc, "_safe_text", count_scan)
    report = gc.build_plan(base, state_dir)
    assert scanned.count(reference) == 1
    assert report["groups"] == []
    assert any(row["reason"] == "authority_reference" for row in report["kept"])


@pytest.mark.parametrize("alias_kind", ("outside", "chain", "dangling"))
def test_private_directory_alias_outside_chain_or_dangling_blocks(tmp_path, monkeypatch, alias_kind):
    base, _candidate, _recording, _state_dir = _fixture(tmp_path, monkeypatch)
    private = base / "private-runs"
    private.mkdir()
    alias = private / "runtime"
    if alias_kind == "outside":
        alias.symlink_to(base / "out", target_is_directory=True)
    elif alias_kind == "chain":
        canonical = private / "canonical"
        canonical.mkdir()
        (private / "chain-target").symlink_to("canonical", target_is_directory=True)
        alias.symlink_to("chain-target", target_is_directory=True)
    else:
        alias.symlink_to("missing-directory", target_is_directory=True)
    with pytest.raises(gc.GateError, match="unsafe link"):
        gc._scan_private_runs(base)


@pytest.mark.parametrize("target_exists", (False, True))
def test_private_same_basename_mp4_alias_accepts_scanned_or_missing_target(tmp_path, monkeypatch, target_exists):
    base, _candidate, _recording, _state_dir = _fixture(tmp_path, monkeypatch)
    private = base / "private-runs"
    canonical = private / "canonical"
    alias_parent = private / "run"
    canonical.mkdir(parents=True)
    alias_parent.mkdir()
    target = canonical / "input.mp4"
    if target_exists:
        target.write_bytes(b"private input")
    (alias_parent / "input.mp4").symlink_to("../canonical/input.mp4")
    assert gc._scan_private_runs(base) == (set(), set())


def test_private_text_alias_is_not_an_internal_mp4_alias(tmp_path, monkeypatch):
    base, _candidate, _recording, _state_dir = _fixture(tmp_path, monkeypatch)
    private = base / "private-runs"
    canonical = private / "canonical"
    alias_parent = private / "run"
    canonical.mkdir(parents=True)
    alias_parent.mkdir()
    (canonical / "metadata.txt").write_text("private text", encoding="utf-8")
    (alias_parent / "metadata.txt").symlink_to("../canonical/metadata.txt")
    with pytest.raises(gc.GateError, match="unsafe link"):
        gc._scan_private_runs(base)


def test_private_missing_mp4_target_appearing_during_census_blocks(tmp_path, monkeypatch):
    base, _candidate, _recording, _state_dir = _fixture(tmp_path, monkeypatch)
    private = base / "private-runs"
    canonical = private / "canonical"
    alias_parent = private / "run"
    canonical.mkdir(parents=True)
    alias_parent.mkdir()
    manifest = canonical / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    target = canonical / "input.mp4"
    (alias_parent / "input.mp4").symlink_to("../canonical/input.mp4")
    original_safe_text = gc._safe_text

    def create_target(path: Path):
        if path == manifest:
            target.write_bytes(b"appeared after directory enumeration")
        return original_safe_text(path)

    monkeypatch.setattr(gc, "_safe_text", create_target)
    with pytest.raises(gc.GateError, match="unsafe link"):
        gc._scan_private_runs(base)


def test_partial_unlink_is_reported_even_when_receipt_updates_fail(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    alias = _cache_alias(candidate, target)
    report = gc.build_plan(base, state_dir)
    original_unlink = gc.os.unlink
    original_write = gc._write_json
    unlinks = []
    def stop_after_first(name, **kwargs):
        if unlinks:
            raise OSError("injected second-unlink failure")
        original_unlink(name, **kwargs)
        unlinks.append(name)
    def prepared_only(path, value):
        if value["status"] != "PREPARED":
            raise OSError("injected receipt persistence failure")
        return original_write(path, value)
    monkeypatch.setattr(gc.os, "unlink", stop_after_first)
    monkeypatch.setattr(gc, "_write_json", prepared_only)
    result = gc.apply_plan(report)
    assert result["removed_files"] == 1
    assert result["removed_allocated_bytes"] == 0
    assert result["complete"] is False
    assert len(result["partial"][0]["removed_paths"]) == 1
    assert int(target.exists()) + int(alias.exists()) == 1


def test_unrelated_final_source_and_png_are_never_selected(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    (candidate / "clip.recut.mp4").write_bytes(b"recut")
    (candidate / "clip.burned-final-sapphire72.mp4").write_bytes(b"final")
    (candidate / "thumbnail.png").write_bytes(b"png")
    report = gc.build_plan(base, state_dir)
    assert report["groups"] == []
    assert not any(str(path) in json.dumps(report["groups"]) for path in candidate.iterdir())


@pytest.mark.skipif(
    not Path("/proc").is_dir() or os.geteuid() != 0,
    reason="real complete Linux /proc census requires root",
)
def test_open_target_fd_keeps_group(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys,time; f=open(sys.argv[1],'rb'); time.sleep(5)", str(target)]
    )
    try:
        time.sleep(0.1)
        report = gc.build_plan(base, state_dir)
        assert report["groups"] == []
        assert any(item["reason"] == "target_or_candidate_namespace_active" for item in report["kept"])
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_closed_cache_with_multiple_attempt_aliases_deletes_one_inode(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    cache = _cache_alias(candidate, target)
    another = candidate / "attempt-two" / "source-context.context.mp4"
    another.parent.mkdir()
    os.link(target, another)
    plan = gc.build_plan(base, state_dir)
    assert plan["planned_files"] == 3
    assert plan["planned_allocated_bytes"] == target.stat().st_blocks * 512
    result = gc.apply_plan(plan)
    assert result["removed_files"] == 3
    assert result["complete"]
    assert not any(p.exists() for p in (target, cache, another))


def test_unknown_alias_in_multi_attempt_group_blocks_all(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    cache = _cache_alias(candidate, target)
    unknown = candidate / "current-preview.mp4"
    os.link(target, unknown)
    plan = gc.build_plan(base, state_dir)
    assert plan["groups"] == []
    assert any(r["reason"] == "inode_alias_not_allowed" for r in plan["kept"])
    assert all(p.exists() for p in (target, cache, unknown))


def test_runtime_mutex_replaces_only_disabled_marker(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    monkeypatch.setattr(gc, "quiet_window", lambda _base: [gc.DISABLED_REASON])
    with pytest.raises(gc.GateError):
        gc.build_plan(base, state_dir)
    plan = gc.build_plan(base, state_dir, runtime=True)
    assert gc.apply_plan(plan)["removed_files"] == 1
    assert not target.exists()
    monkeypatch.setattr(gc, "quiet_window", lambda _base: [gc.DISABLED_REASON, "in-flight production process"] )
    with pytest.raises(gc.GateError):
        gc.build_plan(base, state_dir, runtime=True)


def test_budget_defers_whole_inode_group_without_hashing(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    _cache_alias(candidate, target)
    monkeypatch.setattr(gc, "_group_preimages", lambda _paths: pytest.fail("budgeted group must not be hashed"))
    plan = gc.build_plan(base, state_dir, max_bytes=1)
    assert not plan["groups"] and not plan["complete"]
    assert plan["unprocessed_by_max_files"] == 2


def _real_recipe_fixture(tmp_path, monkeypatch):
    import hashlib
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    state_path = base / 'state' / f'{DATE}.json'
    state = json.loads(state_path.read_text())
    for key in ('context_start_ms', 'context_end_ms', 'context_duration_ms'):
        state['songs'][0].pop(key)
    state_path.write_text(json.dumps(state))
    seed = candidate / 'song_selector' / 'attempt-one' / 'seededsong_1000_7000'
    context_root = seed / 'source_context' / 'seededsong_1000_7000'
    context_root.mkdir(parents=True)
    target = context_root / 'source-context.context.mp4'
    target.write_bytes(b'context bytes')
    source = candidate / f'{CID}_tight_1000_9000_source.mp4'
    manifest = context_root / 'source-context.jingting.manifest.json'
    manifest.write_text(json.dumps({'source_sha256': 'sha256:' + 'c'*64, 'source_offset_ms': 0}))
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', '0.000', '-i', str(source), '-t', '8.000', '-c', 'copy', '<output>']
    key = hashlib.sha256(json.dumps({'source_sha256': 'c'*64, 'context_start_ms': 0, 'context_duration_ms': 8000, 'command': command}, sort_keys=True).encode()).hexdigest()
    cache = candidate / '.context_clip_cache' / (key + '.mp4')
    cache.parent.mkdir()
    os.link(target, cache)
    summary = seed / 'summary.json'
    summary.write_text(json.dumps({'input': {'source_video': str(source)}, 'source_integrity': {'ledger': {'can_use_local_source': True, 'issues': []}}, 'records': [{
        'source_context': {'context_media_path': str(target), 'decision': 'READY', 'jingting_done': True, 'jingting_manifest_path': str(manifest)},
        'source_context_job': {'timeline': {'context_start_ms': 0, 'context_end_ms': 8000, 'context_duration_ms': 8000, 'source_duration_ms': 8000}}
    }]}))
    return base, candidate, state_dir, target, cache, summary


def test_context_recipe_recovers_nested_timeline_and_validates_real_cache_key(tmp_path, monkeypatch):
    base, _candidate, state_dir, target, cache, _summary = _real_recipe_fixture(tmp_path, monkeypatch)
    plan = gc.build_plan(base, state_dir)
    assert plan['planned_files'] == 2
    recipe = plan['groups'][0]['source_recovery']['context_recipe']
    assert recipe['source_window']['start_ms'] == 1000
    assert recipe['context']['duration_ms'] == 8000
    assert recipe['cache_key'] == cache.stem
    assert gc.apply_plan(plan)['removed_files'] == 2
    assert not target.exists()


def test_context_recipe_drift_stops_apply_before_unlink(tmp_path, monkeypatch):
    base, _candidate, state_dir, target, cache, summary = _real_recipe_fixture(tmp_path, monkeypatch)
    plan = gc.build_plan(base, state_dir)
    doc = json.loads(summary.read_text())
    doc['records'][0]['source_context_job']['timeline']['context_duration_ms'] = 7000
    summary.write_text(json.dumps(doc))
    with pytest.raises(gc.Keep):
        gc.apply_plan(plan)
    assert target.exists() and cache.exists()


def test_forged_context_cache_key_keeps_exact_group(tmp_path, monkeypatch):
    base, _candidate, state_dir, target, cache, _summary = _real_recipe_fixture(tmp_path, monkeypatch)
    cache.rename(cache.with_name('e'*64 + '.mp4'))
    plan = gc.build_plan(base, state_dir)
    assert not plan['groups']
    assert any('cache key differs' in r['reason'] for r in plan['kept'])
    assert target.exists()


def _agy_variant(
    candidate: Path,
    source: Path,
    variant: int,
    *,
    source_sha: str | None = None,
    agy_rc: int = 0,
    finished_at: str | None = '2026-10-01T22:00:00Z',
) -> tuple[Path, Path]:
    job = candidate / 'song_selector_full' / 'attempt-one' / 'seededsong_1000_7000' / 'song_repair' / 'agy_audio_lrc' / f'variant-{variant:02d}' / f'seededsong_1000_7000-variant-{variant}'
    job.mkdir(parents=True)
    input_path = job / 'input.mp4'
    os.link(source, input_path)
    output = job / 'alignment.canonical.json'
    output.write_text(json.dumps({'alignment': [], 'variant': variant}))
    manifest = job / 'run.manifest.json'
    manifest.write_text(json.dumps({
        'schema_version': 'agy-audio-lrc-run.v3',
        'agy_rc': agy_rc,
        'finished_at': finished_at,
        'artifacts': {
            'source_path': str(input_path),
            'source_origin_path': str(source),
            'source_sha256': source_sha or hashlib.sha256(source.read_bytes()).hexdigest(),
            'output_path': str(output),
            'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
        },
    }))
    return input_path, manifest


def test_completed_agy_alias_group_binds_wider_retry_and_output(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    state_path = base / 'state' / f'{DATE}.json'
    state = json.loads(state_path.read_text())
    state['songs'][0]['full_source_retry'] = {'start_ms': 0, 'end_ms': 10000}
    state_path.write_text(json.dumps(state))
    source = candidate / f'{CID}_full_0_10000_source.mp4'
    source.write_bytes(b'wide source bytes')
    input_paths = [_agy_variant(candidate, source, variant)[0] for variant in (1, 2)]
    plan = gc.build_plan(base, state_dir)
    assert plan['planned_files'] == 3
    assert plan['groups'][0]['source_recovery']['intervals']['window'] == {'start_ms': 0, 'end_ms': 10000}
    assert len(plan['groups'][0]['source_recovery']['agy_recipes']) == 2
    assert {recipe['input'] for recipe in plan['groups'][0]['source_recovery']['agy_recipes']} == {str(path) for path in input_paths}
    assert gc.apply_plan(plan)['removed_files'] == 3
    assert all(path.parent.joinpath('alignment.canonical.json').exists() for path in input_paths)
    assert not source.exists() and not any(path.exists() for path in input_paths)


def test_incomplete_agy_variant_blocks_whole_inode_group(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    state_path = base / 'state' / f'{DATE}.json'
    state = json.loads(state_path.read_text())
    state['songs'][0]['full_source_retry'] = {'start_ms': 0, 'end_ms': 10000}
    state_path.write_text(json.dumps(state), encoding='utf-8')
    source = candidate / f'{CID}_full_0_10000_source.mp4'
    source.write_bytes(b'wide source bytes')
    _agy_variant(candidate, source, 1)
    _agy_variant(candidate, source, 2, agy_rc=1)

    plan = gc.build_plan(base, state_dir)
    assert plan['groups'] == []
    assert any('completed producer receipt' in row['reason'] for row in plan['kept'])
    assert source.exists()


@pytest.mark.parametrize('hashes, expected_reason', [
    (('f' * 64, '0' * 64), 'AGY producer input hashes disagree'),
    (('0' * 64, '0' * 64), 'AGY producer input hash differs from exact target inode'),
])
def test_agy_source_hashes_must_agree_and_match_target(tmp_path, monkeypatch, hashes, expected_reason):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    state_path = base / 'state' / f'{DATE}.json'
    state = json.loads(state_path.read_text())
    state['songs'][0]['full_source_retry'] = {'start_ms': 0, 'end_ms': 10000}
    state_path.write_text(json.dumps(state), encoding='utf-8')
    source = candidate / f'{CID}_full_0_10000_source.mp4'
    source.write_bytes(b'wide source bytes')
    _agy_variant(candidate, source, 1, source_sha=hashes[0])
    _agy_variant(candidate, source, 2, source_sha=hashes[1])

    plan = gc.build_plan(base, state_dir)
    assert plan['groups'] == []
    assert any(row['reason'] == expected_reason for row in plan['kept'])
    assert source.exists()


def test_runtime_apply_rejects_a_busy_real_runner_mutex(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = _source_target(candidate)
    plan = gc.build_plan(base, state_dir, runtime=True)
    lock, _ = gc._runner_lock(base)
    try:
        with pytest.raises(gc.GateError, match='held by another process'):
            gc.apply_plan(plan)
        assert target.exists()
    finally:
        os.close(lock)


def test_context_only_aliases_resume_after_cache_unlink(tmp_path, monkeypatch):
    base, candidate, state_dir, target, cache, summary = _real_recipe_fixture(tmp_path, monkeypatch)
    other = candidate / 'song_selector' / 'attempt-two' / 'seededsong_1000_7000'
    job = other / 'source_context' / 'seededsong_1000_7000'
    job.mkdir(parents=True)
    alias = job / target.name
    os.link(target, alias)
    manifest = job / 'source-context.jingting.manifest.json'
    manifest.write_bytes(target.with_name(manifest.name).read_bytes())
    doc = json.loads(summary.read_text())
    doc['records'][0]['source_context'].update(context_media_path=str(alias), jingting_manifest_path=str(manifest))
    (other / 'summary.json').write_text(json.dumps(doc))
    cache.unlink()
    plan = gc.build_plan(base, state_dir)
    assert plan['planned_files'] == 2
    assert gc.apply_plan(plan)['removed_files'] == 2
    assert not target.exists() and not alias.exists()


def test_context_recipe_records_wider_retry_interval(tmp_path, monkeypatch):
    import hashlib
    base, candidate, state_dir, target, cache, summary = _real_recipe_fixture(tmp_path, monkeypatch)
    state_path = base / 'state' / f'{DATE}.json'
    state = json.loads(state_path.read_text())
    state['songs'][0]['full_source_retry'] = {'start_ms': 0, 'end_ms': 10000}
    state_path.write_text(json.dumps(state))
    source = candidate / f'{CID}_full_0_10000_source.mp4'
    doc = json.loads(summary.read_text())
    doc['input']['source_video'] = str(source)
    doc['records'][0]['source_context_job']['timeline'].update(context_end_ms=10000, context_duration_ms=10000, source_duration_ms=10000)
    summary.write_text(json.dumps(doc))
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', '0.000', '-i', str(source), '-t', '10.000', '-c', 'copy', '<output>']
    key = hashlib.sha256(json.dumps({'source_sha256': 'c'*64, 'context_start_ms': 0, 'context_duration_ms': 10000, 'command': command}, sort_keys=True).encode()).hexdigest()
    cache.rename(cache.with_name(key+'.mp4'))
    plan = gc.build_plan(base, state_dir)
    assert plan['planned_files'] == 2
    assert plan['groups'][0]['source_recovery']['intervals']['window'] == {'start_ms': 0, 'end_ms': 10000}


def test_busy_cli_does_not_start_the_large_proof_scan(tmp_path, monkeypatch):
    base, _candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    lock, _ = gc._runner_lock(base)
    monkeypatch.setattr(gc, 'build_plan', lambda *_a, **_k: pytest.fail('busy runner must skip planning'))
    try:
        assert gc.main(['--base', str(base), '--state-dir', str(state_dir), '--apply', '--runtime']) == 2
        assert 'held by another process' in json.loads((state_dir/'terminal-out-gc-plan.json').read_text())['errors'][0]
    finally:
        os.close(lock)


def test_unbound_legacy_window_keeps_media(tmp_path, monkeypatch):
    base, candidate, _recording, state_dir = _fixture(tmp_path, monkeypatch)
    target = candidate / f'{CID}_full_source.mp4'
    target.write_bytes(b'full recording cannot be bound to a tight interval')
    plan = gc.build_plan(base, state_dir)
    assert not plan['groups']
    assert any('no exact producer interval' in row['reason'] for row in plan['kept'])
    assert target.exists()
