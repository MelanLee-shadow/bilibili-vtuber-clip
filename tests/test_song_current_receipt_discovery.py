from types import SimpleNamespace

from src.autoslice import live_source_review as review


def test_ordinary_song_directory_replays_its_own_completed_observations(tmp_path, monkeypatch):
    output = tmp_path / "song-selector" / "candidate"
    old = (
        output / "song_repair" / "agy_audio_lrc" / "variant-01" / "prior-run" / "run.manifest.json"
    )
    old.parent.mkdir(parents=True)
    old.write_text("{}")
    seen = []
    receipt = object()
    monkeypatch.setattr(review, "_job_is_song_candidate", lambda job: True)
    monkeypatch.setattr(review, "_song_boundary_ready", lambda job: False)
    monkeypatch.setattr(review, "_job_anchor_candidate", lambda job: None)
    monkeypatch.setattr(
        review, "load_hash_bound_audio_lrc_run", lambda path: (seen.append(path), receipt)[1]
    )

    def repair(**kwargs):
        assert kwargs["prior_audio_lrc_runs"] == (receipt,)
        return SimpleNamespace(
            repaired=False,
            reason_codes=("SONG_AUDIO_LRC_PROOF_UNRESOLVED",),
            live_performance=None,
            report_path="current-report",
        )

    monkeypatch.setattr(review, "attempt_song_repair", repair)
    job, result = review._attempt_song_repair_stage(
        {"timeline": {"source_duration_ms": 1000}},
        [],
        candidate_id="candidate",
        output_dir=output,
        lrc_provider=None,
        pinned_lrc_resolver=lambda cues: [],
    )
    assert seen == [old]
    assert job["song_repair_gate"]["status"] == "BLOCKED"
    assert old.read_text() == "{}"


def test_ordinary_song_discovery_orders_new_completed_run_before_old_unresolved_report(
    tmp_path, monkeypatch
):
    import os

    output = tmp_path / "song-selector" / "candidate"
    root = output / "song_repair" / "agy_audio_lrc"
    old = root / "variant-02" / "old-unresolved" / "run.manifest.json"
    new = root / "variant-01" / "new-http200" / "run.manifest.json"
    for p, stamp in [(old, 100), (new, 200)]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
        os.utime(p, (stamp, stamp))
    monkeypatch.setattr(review, "_job_is_song_candidate", lambda _job: True)
    monkeypatch.setattr(review, "_song_boundary_ready", lambda _job: False)
    monkeypatch.setattr(review, "_job_anchor_candidate", lambda _job: None)
    monkeypatch.setattr(review, "load_hash_bound_audio_lrc_run", lambda path: path)

    def repair(**kwargs):
        assert kwargs["prior_audio_lrc_runs"] == (new, old)
        return SimpleNamespace(
            repaired=False,
            reason_codes=("SONG_AUDIO_LRC_PROOF_UNRESOLVED",),
            live_performance=None,
            report_path="current-report",
        )

    monkeypatch.setattr(review, "attempt_song_repair", repair)
    review._attempt_song_repair_stage(
        {"timeline": {"source_duration_ms": 1000}},
        [],
        candidate_id="candidate",
        output_dir=output,
        lrc_provider=None,
        pinned_lrc_resolver=lambda _cues: [],
    )
