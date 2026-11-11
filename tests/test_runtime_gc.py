from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import pytest

from src.autoslice import runtime_gc as gc


def put(path, *, age=10, content=b"media"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    stamp = time.time() - age * gc.DAY
    os.utime(path, (stamp, stamp))
    os.utime(path.parent, (stamp, stamp))
    return path


def state(base, cid, status, **fields):
    put(base / "state/2026-10-01.json", content=json.dumps({
        "picks": [{"candidate_id": cid, "status": status, **fields}],
    }).encode())


def plan(base, **kwargs):
    return gc.collect(base, home=base / "absent-home", live=gc.Activity(),
                      free_bytes=30 * gc.GIB, **kwargs)


def paths(value):
    return {row["path"] for row in value["targets"]}


def test_historical_reference_does_not_protect_another_run(tmp_path):
    media = put(tmp_path / "private-runs/old/clip.mp4")
    text = put(tmp_path / "reports/current/STATE.json", age=0, content=json.dumps({"note": str(media)}).encode())
    assert paths(plan(tmp_path)) == {str(media)}
    assert text.exists()
    # A recently written media file protects the whole run, including old files.
    put(media.parent / "new.wav", age=.5)
    assert not paths(plan(tmp_path))


@pytest.mark.parametrize("name", ["SPEC.json", "RESULT.json", "subtitle.srt", "native.log"])
def test_any_recent_file_write_protects_old_run_media(tmp_path, name):
    media = put(tmp_path / "private-runs/old/clip.mp4", age=3)
    put(media.parent / name, age=0, content=b'{}')
    assert not paths(plan(tmp_path))


def test_new_staging_directory_protects_copied_old_mtime_media(tmp_path):
    media = put(tmp_path / ".oci3-shadow-upload-test/assets/clip.mp4", age=10)
    # Git archive can retain old file mtimes inside a newly created staging.
    assert not paths(plan(tmp_path))
    assert media.exists()


def test_pressure_retention_is_run_wide(tmp_path):
    old = put(tmp_path / "private-runs/run/old.mp4", age=10)
    new = put(old.parent / "new.mp4", age=3)
    assert not paths(plan(tmp_path))
    pressured = gc.collect(tmp_path, home=tmp_path / "absent", live=gc.Activity(), free_bytes=19 * gc.GIB)
    assert paths(pressured) == {str(old), str(new)}
    assert pressured["pressure"]


def test_terminal_discard_respects_recent_and_open_file(tmp_path):
    media = put(tmp_path / "out/2026-10-01/c1/clip.mp4", age=2)
    state(tmp_path, "c1", "candidate_rejected")
    assert paths(plan(tmp_path)) == {str(media)}
    live = gc.Activity(paths={media})
    assert not paths(gc.collect(tmp_path, home=tmp_path / "absent", live=live))
    put(media, age=.5)
    assert not paths(plan(tmp_path))


def test_stale_run_running_note_is_not_a_permanent_protection(tmp_path):
    media = put(tmp_path / "private-runs/orphan/clip.mp4")
    put(media.parent / "STATE.json", content=b'{"status":"running"}')
    assert paths(plan(tmp_path)) == {str(media)}
    state(tmp_path, "c1", "processing")
    active = put(tmp_path / "out/2026-10-01/c1/clip.mp4")
    assert str(active) not in paths(plan(tmp_path))


def test_same_candidate_id_on_two_dates_keeps_current_date(tmp_path):
    state(tmp_path, "c1", "candidate_rejected")
    rejected = put(tmp_path / "out/2026-10-01/c1/clip.mp4")
    put(tmp_path / "state/2026-10-02.json", content=json.dumps({
        "picks": [{"candidate_id": "c1", "status": "processing"}],
    }).encode())
    current = put(tmp_path / "out/2026-10-02/c1/clip.mp4")
    assert paths(plan(tmp_path)) == {str(rejected)}
    assert str(current) not in paths(plan(tmp_path))


def test_terminal_hook_scans_only_structurally_owned_runs(tmp_path, monkeypatch):
    chosen = put(tmp_path / "out/2026-10-01/c1/clip.mp4")
    unrelated = put(tmp_path / "private-runs/unrelated/clip.mp4")
    state(tmp_path, "c1", "candidate_rejected")
    original = gc.walk_entries

    def bounded(root):
        assert root != unrelated.parent
        return original(root)

    monkeypatch.setattr(gc, "walk_entries", bounded)
    assert paths(plan(tmp_path, candidate_ids={"c1"})) == {str(chosen)}


def test_published_final_clock_and_verification(tmp_path):
    video = put(tmp_path / "repo/lidousha/2026-10-01/title__c1.mp4", age=3)
    mid = put(tmp_path / "out/2026-10-01/c1/padded.mp4", age=3)
    # The run that published holds its authority; only that run is concluded.
    authority = put(tmp_path / "out/2026-10-01/c1/c1.publication_reconciliation.json",
                    content=b'{"status": "VERIFIED_PUBLIC"}')
    proof = {"candidate_id": "c1", "bvid": "BV123", "status": "VERIFIED_PUBLIC",
             "schema_version": "publication-reconciliation.v1",
             "authority": {"path": str(authority), "sha256": "0" * 64, "bytes": 30}}
    state(tmp_path, "c1", "published", bvid="BV123", publication_reconciliation=proof)
    assert paths(plan(tmp_path)) == {str(mid)}
    pressure = gc.collect(tmp_path, home=tmp_path / "absent", live=gc.Activity(), free_bytes=10 * gc.GIB)
    assert paths(pressure) == {str(mid), str(video)}
    state(tmp_path, "c1", "published", bvid="BV123")
    assert not paths(plan(tmp_path))
    # A final older than 7 days keeps 7 days from a recent verification, even
    # outside the publishing run; the same file of a rejected candidate is collected.
    put(video, age=10)
    yesterday = datetime.fromtimestamp(time.time() - gc.DAY, timezone.utc).isoformat()
    state(tmp_path, "c1", "published", bvid="BV123", publication_reconciliation={**proof, "reconciled_at": yesterday})
    assert str(video) not in paths(plan(tmp_path))
    state(tmp_path, "c1", "candidate_rejected")
    assert str(video) in paths(plan(tmp_path))
    # Verified, but with no authority to derive the must-keep bytes: fail closed.
    state(tmp_path, "c1", "published", bvid="BV123",
          publication_reconciliation={k: v for k, v in proof.items() if k != "authority"})
    value = plan(tmp_path)
    assert not paths(value) and value["retained_logical_bytes"]["published-unresolved"] == 2 * len(b"media")


def test_prepared_extensionless_media_and_text_roles(tmp_path):
    root = tmp_path / ".prepared-deliveries/talk/c1/generation"
    video = put(root / "00-video")
    text = put(root / "01-subtitle", content=b"subtitle")
    manifest = put(root / "prepared.json", content=json.dumps({"candidate_id": "c1", "artifacts": [
        {"role": "video", "staged_path": str(video), "source_path": "/source/clip.mp4"},
        {"role": "subtitle", "staged_path": str(text), "source_path": "/source/clip.srt"},
    ]}).encode())
    assert paths(plan(tmp_path)) == {str(video)}
    assert text.exists() and manifest.exists()


def test_recordings_explicit_preserve_and_symlink_are_excluded(tmp_path):
    root = tmp_path / "reports/old"
    recording = put(root / "22966160_20261001-12-00-00.mp4")
    keep = put(root / "keep.mp4")
    media = put(root / "raw.f32le")
    (root / "link.mp4").symlink_to(recording)
    old = media.stat().st_mtime
    os.utime(root / "link.mp4", (old, old), follow_symlinks=False)
    os.utime(root, (old, old))
    value = plan(tmp_path, preserve={str(keep)})
    assert paths(value) == {str(media)}
    assert value["retained_logical_bytes"]["recording"] == recording.stat().st_size


def test_apply_exact_reviewed_set_and_refuse_metadata_drift(tmp_path, monkeypatch):
    first = put(tmp_path / "private-runs/one/clip.mp4")
    approved = plan(tmp_path)
    second = put(tmp_path / "private-runs/two/clip.mp4")
    monkeypatch.setattr(gc, "activity", lambda *_: gc.Activity())
    result = gc.apply_plan(approved)
    assert result["status"] == "APPLIED" and not first.exists() and second.exists()
    approved = plan(tmp_path)
    second.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="changed or protected"):
        gc.apply_plan(approved)
    assert second.exists()


def test_new_open_file_refuses_entire_approved_batch(tmp_path, monkeypatch):
    first = put(tmp_path / "private-runs/one/clip.mp4")
    second = put(tmp_path / "private-runs/two/clip.mp4")
    approved = plan(tmp_path)
    monkeypatch.setattr(gc, "activity", lambda *_: gc.Activity(paths={second}))
    with pytest.raises(RuntimeError, match="protected"):
        gc.apply_plan(approved)
    assert first.exists() and second.exists()


@pytest.mark.parametrize("preserve_alias", [False, True])
def test_apply_hardlinks_tracks_own_unlinks_and_keeps_unapproved_alias(tmp_path, monkeypatch, preserve_alias):
    first = put(tmp_path / "private-runs/one/clip.mp4")
    second = tmp_path / "reports/two/clip.mp4"
    kept = tmp_path / "candidates/kept/clip.mp4"
    for path in ((second, kept) if preserve_alias else (second,)):
        path.parent.mkdir(parents=True)
        os.link(first, path)
        os.utime(path.parent, (first.stat().st_mtime, first.stat().st_mtime))
    approved = plan(tmp_path, preserve={str(kept)} if preserve_alias else set())
    assert paths(approved) == {str(first), str(second)}
    monkeypatch.setattr(gc, "activity", lambda *_: gc.Activity())
    result = gc.apply_plan(approved)
    assert result["status"] == "APPLIED"
    assert set(result["removed"]) == {str(first), str(second)}
    assert not first.exists() and not second.exists()
    if preserve_alias:
        assert kept.read_bytes() == b"media" and kept.stat().st_nlink == 1


@pytest.mark.parametrize("coarse_ctime", [False, True])
def test_apply_hardlinks_refuses_external_ctime_change_between_aliases(tmp_path, monkeypatch, coarse_ctime):
    first = put(tmp_path / "private-runs/one/clip.mp4")
    second = tmp_path / "reports/two/clip.mp4"
    second.parent.mkdir(parents=True)
    os.link(first, second)
    os.utime(second.parent, (first.stat().st_mtime, first.stat().st_mtime))
    if coarse_ctime:
        original_identity = gc.identity

        def coarse(info):
            return {**original_identity(info), "ctime_ns": 0}

        monkeypatch.setattr(gc, "identity", coarse)
    approved = plan(tmp_path)

    def live(*_):
        if not first.exists() and second.exists():
            os.chmod(second, 0o700)
        return gc.Activity()

    monkeypatch.setattr(gc, "activity", live)
    result = gc.apply_plan(approved)
    assert result["status"] == "PARTIAL" and result["removed"] == [str(first)]
    assert "target metadata changed" in result["error"] and second.exists()


def test_apply_hardlinks_refuses_content_change_during_unlink(tmp_path, monkeypatch):
    first = put(tmp_path / "private-runs/one/clip.mp4")
    second = tmp_path / "reports/two/clip.mp4"
    second.parent.mkdir(parents=True)
    os.link(first, second)
    os.utime(second.parent, (first.stat().st_mtime, first.stat().st_mtime))
    approved = plan(tmp_path)
    unlink = Path.unlink

    def mutate(path, *args, **kwargs):
        unlink(path, *args, **kwargs)
        if path == first:
            second.write_bytes(b"external mutation")

    monkeypatch.setattr(Path, "unlink", mutate)
    monkeypatch.setattr(gc, "activity", lambda *_: gc.Activity())
    result = gc.apply_plan(approved)
    assert result["status"] == "PARTIAL" and result["removed"] == [str(first)]
    assert "inode changed during unlink" in result["error"]
    assert second.read_bytes() == b"external mutation"


def test_normal_state_writer_calls_readonly_terminal_hook(tmp_path, monkeypatch):
    from src.autoslice.runner_state_writeback import write_state
    put(tmp_path / "runtime-gc.json", content=b'{"apply_enabled":false}')
    media = put(tmp_path / "out/2026-10-01/c1/clip.mp4")
    monkeypatch.setattr(gc, "activity", lambda *_: gc.Activity())
    output = []
    write_state(tmp_path / "state/2026-10-01.json", {
        "picks": [{"candidate_id": "c1", "status": "candidate_rejected"}],
    }, runtime_root=tmp_path, updated_at="now", log=output.append)
    assert any('"status": "DRY_RUN_SKIPPED"' in line and '"c1"' in line for line in output)
    assert media.exists()


@pytest.mark.skipif(not Path("/proc/locks").exists(), reason="Linux live descriptors")
def test_actual_open_descriptor_and_runner_flock(tmp_path):
    import fcntl
    media = put(tmp_path / "private-runs/one/clip.mp4")
    with media.open("rb"), (tmp_path / "runner.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed = gc.activity(tmp_path)
        assert media in observed.paths and observed.runner_locked
        assert not gc.collect(tmp_path, home=tmp_path / "absent", live=observed)["targets"]


def test_fresh_text_file_still_protects_old_media_with_pressure(tmp_path):
    media = put(tmp_path / "private-runs/run/clip.mp4", age=3)
    put(media.parent / "notes.txt", age=.5, content=b"log")
    pressured = gc.collect(tmp_path, home=tmp_path / "absent", live=gc.Activity(), free_bytes=19 * gc.GIB)
    assert not paths(pressured)
    assert pressured["retained_logical_bytes"]["recent-write"] == len(b"media")


def pressured_plan(base):
    return gc.collect(base, home=base / "absent", live=gc.Activity(), free_bytes=19 * gc.GIB)


def apply_one(base, monkeypatch, victim):
    approved = plan(base)
    approved["targets"] = [row for row in approved["targets"] if row["path"] == str(victim)]
    assert len(approved["targets"]) == 1
    monkeypatch.setattr(gc, "activity", lambda *_: gc.Activity())
    return gc.apply_plan(approved)


@pytest.fixture
def observed_unlinks(monkeypatch):
    # Simulate directory event delivery on Mac; production Linux integration
    # exercises the actual inotify implementation separately.
    class Events:
        def __init__(self, path):
            self.path = path
            self.before = {p.name for p in path.parent.iterdir()}
        def only_our_delete(self):
            return {p.name for p in self.path.parent.iterdir()} == self.before - {self.path.name}
        def close(self):
            pass
    monkeypatch.setattr(gc, "UnlinkEvents", Events)


def test_own_unlink_dir_mtime_does_not_self_protect_remaining_media(tmp_path, monkeypatch, observed_unlinks):
    first = put(tmp_path / "private-runs/run/a.mp4", age=10)
    second = put(first.parent / "b.mp4", age=10)
    assert apply_one(tmp_path, monkeypatch, first)["status"] == "APPLIED"
    assert first.parent.stat().st_mtime > time.time() - gc.DAY
    ledger = json.loads((tmp_path / "state/runtime-gc-self-touch.json").read_text())
    assert ledger == {"schema_version": "runtime-gc-self-touch.v2",
                      "dirs": {str(first.parent): gc.directory_stamp(first.parent.stat())}}
    pressured = pressured_plan(tmp_path)
    assert paths(pressured) == {str(second)}
    assert "recent-write" not in pressured["retained_logical_bytes"]
    # A later non-GC write in the same directory is real activity again.
    time.sleep(.02)
    (second.parent / "tmp.partial").write_bytes(b"x")
    (second.parent / "tmp.partial").unlink()
    assert second.parent.stat().st_mtime_ns != ledger["dirs"][str(first.parent)]["mtime_ns"]
    pressured = pressured_plan(tmp_path)
    assert not paths(pressured)
    assert pressured["retained_logical_bytes"]["recent-write"] == len(b"media")


def test_ledger_is_pruned_and_written_on_partial_apply(tmp_path, monkeypatch, observed_unlinks):
    first = put(tmp_path / "private-runs/one/clip.mp4", age=10)
    second = put(tmp_path / "private-runs/two/clip.mp4", age=10)
    approved = plan(tmp_path)
    monkeypatch.setattr(gc, "activity", lambda *_: gc.Activity())
    real_unlink = Path.unlink

    def failing(path, *args, **kwargs):
        if path == second:
            raise OSError("boom")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing)
    result = gc.apply_plan(approved)
    assert result["status"] == "PARTIAL" and result["removed"] == [str(first)]
    ledger = gc.read_self_touch(tmp_path)
    assert set(ledger) == {str(first.parent)}
    first.parent.rmdir()  # gone directory is pruned on the next write
    gc.write_self_touch(tmp_path, {})
    assert gc.read_self_touch(tmp_path) == {}


def test_interleaved_copy_with_old_file_mtime_is_not_gc_activity(tmp_path, monkeypatch, observed_unlinks):
    first = put(tmp_path / "private-runs/old/a.mp4")
    second = put(first.parent / "b.mp4")
    real_unlink = Path.unlink
    def interleaved(path, *args, **kwargs):
        result = real_unlink(path, *args, **kwargs)
        if path == first:
            put(first.parent / "copied-old.txt", content=b"other writer")
            os.utime(first.parent, None)
        return result
    monkeypatch.setattr(Path, "unlink", interleaved)
    result = apply_one(tmp_path, monkeypatch, first)
    assert result["status"] == "PARTIAL" and result["removed"] == [str(first)]
    assert second.exists() and (first.parent / "copied-old.txt").exists()
    assert str(first.parent) not in gc.read_self_touch(tmp_path)
    assert not paths(pressured_plan(tmp_path))


def test_replaced_directory_cannot_inherit_gc_time_exclusion(tmp_path, monkeypatch, observed_unlinks):
    first = put(tmp_path / "private-runs/old/a.mp4")
    put(first.parent / "b.mp4")
    assert apply_one(tmp_path, monkeypatch, first)["status"] == "APPLIED"
    parent = first.parent
    stamp = parent.stat().st_mtime_ns
    parent.rename(tmp_path / "moved")
    parent.mkdir()
    copied = put(parent / "copied.mp4")
    os.utime(parent, ns=(stamp, stamp))
    assert gc.directory_stamp(parent.stat()) != gc.read_self_touch(tmp_path)[str(parent)]
    assert str(copied) not in paths(pressured_plan(tmp_path))


def test_write_between_collection_and_watch_registration_is_refused(tmp_path, monkeypatch, observed_unlinks):
    first = put(tmp_path / "private-runs/old/a.mp4")
    second = put(first.parent / "b.mp4")
    original = gc.UnlinkEvents
    def delayed_registration(path):
        put(path.parent / "copied-old.txt", content=b"other writer")
        os.utime(path.parent, None)
        return original(path)
    monkeypatch.setattr(gc, "UnlinkEvents", delayed_registration)
    result = apply_one(tmp_path, monkeypatch, first)
    assert result["status"] == "REFUSED" and result["removed"] == []
    assert first.exists() and second.exists()
    assert str(first.parent) not in gc.read_self_touch(tmp_path)
    assert not paths(pressured_plan(tmp_path))


def test_copied_old_mtime_run_with_fresh_dir_and_no_ledger_is_protected(tmp_path):
    media = put(tmp_path / "private-runs/staged/clip.mp4", age=10)
    os.utime(media.parent, None)  # rsync -a / cp -p: old file mtimes, new directory
    assert not paths(plan(tmp_path))
    assert not paths(pressured_plan(tmp_path))


@pytest.mark.parametrize("junk", [b"not json", b'{"schema_version":"x","dirs":{}}', b'{"schema_version":"runtime-gc-self-touch.v1","dirs":[1]}'])
def test_corrupt_ledger_means_no_exclusions(tmp_path, junk):
    media = put(tmp_path / "private-runs/staged/clip.mp4", age=10)
    os.utime(media.parent, None)
    put(tmp_path / "state/runtime-gc-self-touch.json", content=junk)
    assert not paths(plan(tmp_path))



def raw_lease(base, media, *, until="2099-01-01T00:00:00Z", lease_id="input"):
    # Independent input contract also runs against the old GC as the guard.
    import hashlib
    destination = base / f"state/evidence-leases/{lease_id}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"schema": "evidence-lease.v1", "path": str(media),
        "sha256": hashlib.sha256(b"media").hexdigest(), "holder": "four-repair",
        "purpose": "native-audio source_media", "until_utc": until}))
    return destination


@pytest.mark.parametrize("relative", [
    "out/2026-10-11/auto_195637_47_259/source.mp4",
    "private-runs/reaction-nanaliya-20261023/manual/out-r3/final.mp4",
])
def test_bound_old_idle_media_lease_protects_and_expiry_releases(tmp_path, relative):
    media = put(tmp_path / relative)
    # The private-run fixture has nested directories: age the entire idle run,
    # not just the media's immediate parent (fresh directories protect a run).
    for parent in media.parents:
        if parent == tmp_path:
            break
        os.utime(parent, (media.stat().st_mtime, media.stat().st_mtime))
    assert paths(plan(tmp_path)) == {str(media)}
    raw_lease(tmp_path, media)
    assert not paths(plan(tmp_path))
    raw_lease(tmp_path, media, until="2000-01-01T00:00:00Z")
    assert paths(plan(tmp_path)) == {str(media)}


def test_lease_directory_covers_descendants_but_file_does_not_cover_siblings(tmp_path):
    first = put(tmp_path / "out/2026-10-01/c1/input.mp4")
    second = put(first.parent / "other.wav")
    unrelated = put(tmp_path / "out/2026-10-01/c10/idle.mp4")
    raw_lease(tmp_path, first)
    assert paths(plan(tmp_path)) == {str(second), str(unrelated)}
    raw_lease(tmp_path, first.parent)
    assert paths(plan(tmp_path)) == {str(unrelated)}


@pytest.mark.parametrize("change", ["add-target", "add-unrelated", "renew", "revoke", "expire"])
def test_apply_refuses_entire_batch_on_lease_snapshot_change(tmp_path, monkeypatch, change):
    first = put(tmp_path / "private-runs/one/clip.mp4")
    second = put(tmp_path / "private-runs/two/clip.mp4")
    unrelated = tmp_path / "protected/unrelated.mp4"
    if change in {"renew", "revoke", "expire"}:
        raw_lease(tmp_path, unrelated)
    approved = plan(tmp_path)
    assert paths(approved) == {str(first), str(second)}
    monkeypatch.setattr(gc, "activity", lambda *_: gc.Activity())
    if change == "add-target":
        raw_lease(tmp_path, second)
    elif change == "add-unrelated":
        raw_lease(tmp_path, unrelated)
    elif change == "renew":
        raw_lease(tmp_path, unrelated, until="2099-02-01T00:00:00Z")
    elif change == "revoke":
        (tmp_path / "state/evidence-leases/input.json").unlink()
    else:
        raw_lease(tmp_path, unrelated, until="2000-01-01T00:00:00Z")
    with pytest.raises(RuntimeError, match="evidence leases changed"):
        gc.apply_plan(approved)
    assert first.exists() and second.exists()


def test_lease_added_after_preflight_is_rechecked_before_unlink(tmp_path, monkeypatch):
    media = put(tmp_path / "private-runs/one/clip.mp4")
    approved = plan(tmp_path)
    monkeypatch.setattr(gc, "activity", lambda *_: gc.Activity())
    original = gc.UnlinkEvents

    def register(path):
        raw_lease(tmp_path, media)
        return original(path)

    monkeypatch.setattr(gc, "UnlinkEvents", register)
    result = gc.apply_plan(approved)
    assert result["status"] == "REFUSED" and result["removed"] == []
    assert "evidence leases changed" in result["error"] and media.exists()


def test_lease_does_not_change_disabled_recording_recent_and_lock_controls(tmp_path):
    put(tmp_path / "DISABLED", age=0)
    idle = put(tmp_path / "out/2026-10-01/c1/idle.mp4")
    recording = put(idle.parent / "22966160_20261001-12-00-00.mp4")
    recent = put(tmp_path / "private-runs/recent/clip.mp4", age=.5)
    assert paths(plan(tmp_path)) == {str(idle)}
    locked = gc.collect(tmp_path, home=tmp_path / "absent", free_bytes=30 * gc.GIB,
                        live=gc.Activity(runner_locked=True))
    assert not paths(locked) and recording.exists() and recent.exists()


def test_invalid_lease_blocks_collection_without_removing_media(tmp_path):
    media = put(tmp_path / "private-runs/one/clip.mp4")
    lease = raw_lease(tmp_path, media)
    lease.write_text('{"schema":"unknown"}')
    with pytest.raises(ValueError, match="schema"):
        plan(tmp_path)
    assert media.exists()


def test_invalid_lease_blocks_apply_before_any_unlink(tmp_path, monkeypatch):
    media = put(tmp_path / 'private-runs/one/clip.mp4')
    approved = plan(tmp_path)
    raw_lease(tmp_path, media).write_text('{"schema":"unknown"}')
    monkeypatch.setattr(gc, 'activity', lambda *_: gc.Activity())
    with pytest.raises(ValueError, match='schema'):
        gc.apply_plan(approved)
    assert media.exists()


def test_lease_protects_terminal_nonmedia_and_leaves_unbound_cache_collectable(tmp_path):
    root = tmp_path / 'private-runs/terminal'
    first = put(root / 'bulk.json', content=b' ' * (gc.DECISION_RECORD_MAX_BYTES + 1))
    second = put(root / 'other.json', content=b' ' * (gc.DECISION_RECORD_MAX_BYTES + 1))
    put(root / 'RUN-LIFECYCLE.json', content=json.dumps({'schema_version': gc.RUN_LIFECYCLE_SCHEMA,
                                                       'status': 'abandoned'}).encode())
    raw_lease(tmp_path, first)
    assert paths(plan(tmp_path)) == {str(second)}


@pytest.mark.parametrize('path,parent,expected', [
    ('/a/b', '/a', True), ('/ab', '/a', False), ('a/b', '.', True), ('/a', '.', False)])
def test_hot_ancestry_matches_component_boundaries(path, parent, expected):
    assert gc.under(Path(path), Path(parent)) is expected


def test_walk_samples_directory_activity_on_entry_not_parent_listing(tmp_path, monkeypatch):
    from contextlib import contextmanager
    child = tmp_path / 'a-child'
    media = put(child / 'old.mp4')
    put(tmp_path / 'z-anchor.json')
    original = gc.os.scandir
    @contextmanager
    def ordered(path):
        with original(path) as entries:
            yield iter(sorted(entries, key=lambda entry: entry.name))
    monkeypatch.setattr(gc.os, 'scandir', ordered)
    entries = gc.walk_entries(tmp_path)
    assert next(entries)[0] == tmp_path
    assert next(entries)[0].name == 'z-anchor.json'
    touch = child / 'new.tmp'
    touch.write_bytes(b'activity')
    touch.unlink()
    path, observed = next(entries)
    assert path == child and observed.st_mtime_ns == child.stat().st_mtime_ns
    assert media.exists()
