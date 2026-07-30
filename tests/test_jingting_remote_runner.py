import hashlib

import pytest

from src.autoslice.jingting_remote_runner import build_ssh_agy_runner
from src.autoslice.source_context_executor import AgyRunnerError


def test_attested_runner_binds_direct_chunk_and_merged_output(
    tmp_path,
    monkeypatch,
):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"source media")
    draft = tmp_path / "draft.srt"
    draft_text = (
        "1\n00:00:00,000 --> 00:00:01,000\n"
        "李豆莎\n"
    )
    draft.write_text(draft_text, encoding="utf-8")
    output = tmp_path / "refined.srt"
    runner = build_ssh_agy_runner("free")

    def fake_encode(_media_path, _chunk, out_path):
        out_path.write_bytes(b"encoded chunk")

    refined = (
        "1\n00:00:00,000 --> 00:00:01,000\n"
        "李豆沙\n"
    )
    monkeypatch.setattr(runner, "_encode_chunk_clip", fake_encode)
    monkeypatch.setattr(
        runner,
        "_run_chunk_agy",
        lambda _job, _clip, _draft, _chunk: refined,
    )

    result = runner(media, draft, output)

    assert output.read_text(encoding="utf-8") == refined
    assert result.executed_provider == "agy"
    assert result.provider_fallback_used is False
    assert result.source_media_sha256 == hashlib.sha256(
        media.read_bytes()
    ).hexdigest()
    assert result.draft_srt_sha256 == hashlib.sha256(
        draft_text.encode()
    ).hexdigest()
    assert result.refined_srt_sha256 == hashlib.sha256(
        refined.encode()
    ).hexdigest()
    assert result.chunk_count == 1
    assert result.agy_chunk_count == 1
    assert result.api_fallback_chunk_count == 0
    assert result.chunk_attestations[0].executed_provider == "agy"
    assert result.chunk_attestations[0].audio_input_attested is True


def test_attested_runner_never_sends_audio_to_non_agy_fallback(
    tmp_path,
    monkeypatch,
):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"source media")
    draft = tmp_path / "draft.srt"
    draft.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n旧字\n",
        encoding="utf-8",
    )
    output = tmp_path / "refined.srt"
    runner = build_ssh_agy_runner("free")
    runner.attempts_per_chunk = 1
    monkeypatch.setattr(
        runner,
        "_encode_chunk_clip",
        lambda _media, _chunk, out: out.write_bytes(b"encoded"),
    )
    monkeypatch.setattr(
        runner,
        "_run_chunk_agy",
        lambda *_args: (_ for _ in ()).throw(
            AgyRunnerError("AGY_QUOTA_EXHAUSTED", "quota")
        ),
    )

    with pytest.raises(AgyRunnerError, match="quota"):
        runner(media, draft, output)

    assert not output.exists()
