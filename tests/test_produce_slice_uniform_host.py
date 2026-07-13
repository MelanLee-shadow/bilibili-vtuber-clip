"""Ivan 2026-07-13 policy: no speaker separation in any deliverable.

Every cue keeps the single host (李豆沙) subtitle style and speaker
uncertainty must never reject a delivery. The binary finalizer stays in the
tree for the future re-enable decision, but uniform_host is the default and
must be unreachable from the finalizer dispatch.
"""

from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
import scripts.produce_slice_package as producer


def test_default_speaker_mode_is_uniform_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOSLICE_SPEAKER_MODE", raising=False)
    assert producer._default_speaker_mode() == "uniform_host"


def test_speaker_mode_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSLICE_SPEAKER_MODE", "required")
    assert producer._default_speaker_mode() == "required"
    monkeypatch.setenv("AUTOSLICE_SPEAKER_MODE", "auto")
    assert producer._default_speaker_mode() == "auto"


def test_speaker_mode_invalid_env_falls_back_to_uniform_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # argparse never validates defaults against choices, so an unknown env
    # value must fail toward the standing policy instead of reaching dispatch.
    monkeypatch.setenv("AUTOSLICE_SPEAKER_MODE", "binary_v4")
    assert producer._default_speaker_mode() == "uniform_host"


def test_uniform_host_never_dispatches_speaker_finalization(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="uniform_host"):
        producer.run_producer_speaker_finalization(
            speaker_mode="uniform_host",
            host="localhost",
            candidate_id="candidate-1",
            media_path=tmp_path / "clip.mp4",
            text_srt_path=tmp_path / "clip.srt",
            output_srt_path=tmp_path / "speaker-final.srt",
            output_ass_path=tmp_path / "speaker-final.ass",
            output_manifest_path=tmp_path / "speaker-final.json",
            work_dir=tmp_path / "speaker-work",
            spec={},
            spec_parent=tmp_path,
            override_path=None,
            source_session_anchor_path=None,
            mixed_overlap_evidence_path=None,
            speaker_python=Path("/must-not-run/model-python"),
            final_source_start_ms=0,
            final_source_end_ms=2_000,
        )


def test_runner_default_speaker_mode_is_uniform_host() -> None:
    # The runner resolves AUTOSLICE_SPEAKER_MODE at import; the test suite
    # imports it without that env var, which is exactly the production
    # default on free (cron injects no speaker mode).
    assert runner.SPEAKER_MODE == "uniform_host"
