import hashlib
import json
import shutil

import pytest

from src.autoslice import producer_source_media
from src.autoslice.source_subtitle_truth import apply_source_subtitle_truth


def _srt_ms(start_ms: int, end_ms: int, text: str) -> str:
    def timestamp(value: int) -> str:
        seconds, millis = divmod(value, 1_000)
        return f"00:00:{seconds:02d},{millis:03d}"

    return (
        "1\n"
        f"{timestamp(start_ms)} --> {timestamp(end_ms)}\n"
        f"{text}\n"
    )


def _install_cached_media(monkeypatch, tmp_path, *, source_sha256: str):
    out_root = tmp_path / "out"
    out_root.mkdir()
    piece_path = out_root / "piece_0_1000_5000.mp4"
    piece_path.write_bytes(b"piece")
    piece_path.with_suffix(".provenance.json").write_text(
        json.dumps({"source_sha256": source_sha256}),
        encoding="utf-8",
    )
    padded_path = out_root / "padded_1000_5000.mp4"
    padded_path.write_bytes(b"padded")
    monkeypatch.setattr(
        producer_source_media,
        "_source_media_sha256",
        lambda _host, source: (str(source), source_sha256),
    )
    monkeypatch.setattr(
        producer_source_media,
        "_valid_cached_provenance",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        producer_source_media,
        "ffprobe_duration_ms",
        lambda _path: 4_000,
    )
    return out_root


def test_prepare_source_media_binds_runner_piece_for_subtitle_truth_alias(
    monkeypatch, tmp_path
):
    source_sha256 = "a" * 64
    out_root = _install_cached_media(
        monkeypatch,
        tmp_path,
        source_sha256=source_sha256,
    )
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/official-replay.mp4",
                "start_ms": 1_000,
                "end_ms": 5_000,
            }
        ]
    }

    prepared = producer_source_media.prepare_source_media(
        spec=spec,
        cid="candidate",
        out_root=out_root,
        host="free",
    )

    binding = "sha256:" + source_sha256
    assert spec["pieces"][0]["source_media_sha256"] == binding
    assert prepared.piece_provenance_rows[0]["source_sha256"] == source_sha256

    ledger_path = tmp_path / "truth.json"
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "source_aliases": [
                    {
                        "alias_id": "official-complete-replay",
                        "alias_recording_basename": "official-replay.mp4",
                        "alias_source_sha256": binding,
                        "canonical_recording_basename": "canonical.mp4",
                        "alias_timeline_offset_ms": 0,
                        "authority": "exact source bytes plus reviewed alignment",
                    }
                ],
                "entries": [
                    {
                        "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                        "truth_id": "runner-shape-regression",
                        "recording_basename": "canonical.mp4",
                        "source_start_ms": 2_000,
                        "source_end_ms": 3_000,
                        "action": "replace_cue",
                        "text": "审定文本",
                        "required": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(1_000, 2_000, "误听文本"),
        spec=spec,
        durations=prepared.durations,
        ledger_path=ledger_path,
    )

    assert "审定文本" in corrected
    assert audit["status"] == "APPLIED"


def test_prepare_source_media_rejects_declared_hash_before_using_cache(
    monkeypatch, tmp_path
):
    source_sha256 = "a" * 64
    out_root = _install_cached_media(
        monkeypatch,
        tmp_path,
        source_sha256=source_sha256,
    )
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/official-replay.mp4",
                "source_media_sha256": "sha256:" + "b" * 64,
                "start_ms": 1_000,
                "end_ms": 5_000,
            }
        ]
    }

    with pytest.raises(RuntimeError, match="SOURCE_MEDIA_DECLARED_SHA256_MISMATCH"):
        producer_source_media.prepare_source_media(
            spec=spec,
            cid="candidate",
            out_root=out_root,
            host="free",
        )

    assert spec["pieces"][0]["source_media_sha256"] == "sha256:" + "b" * 64


def _install_exact_piece_provenance(tmp_path, *, source_sha256: str):
    out_root = tmp_path / "out"
    out_root.mkdir()
    local = out_root / "piece_0_1000_5000.mp4"
    local.write_bytes(b"exact cached piece")
    source_path = "/recordings/official-replay.mp4"
    provenance = {
        "source_path": source_path,
        "source_sha256": source_sha256,
        "source_media_binding": "sha256:" + source_sha256,
        "start_ms": 1_000,
        "end_ms": 5_000,
        "output_path": str(local.resolve()),
        "output_sha256": hashlib.sha256(local.read_bytes()).hexdigest(),
    }
    local.with_suffix(".provenance.json").write_text(
        json.dumps(provenance),
        encoding="utf-8",
    )
    return out_root, local, source_path


def test_prepare_source_media_reuses_exact_hash_bound_piece_when_source_root_unavailable(
    monkeypatch, tmp_path
):
    source_sha256 = "a" * 64
    out_root, local, source_path = _install_exact_piece_provenance(
        tmp_path,
        source_sha256=source_sha256,
    )
    monkeypatch.setattr(
        producer_source_media,
        "_source_media_sha256",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError(
                "SOURCE_RECORDING_ROOT_UNAVAILABLE: "
                "/recordings/official-replay.mp4"
            )
        ),
    )
    monkeypatch.setattr(
        producer_source_media,
        "ffprobe_duration_ms",
        lambda _path: 4_000,
    )
    monkeypatch.setattr(
        producer_source_media,
        "run",
        lambda command, **_kwargs: (
            shutil.copyfile(command[1], command[2])
            if command[0] == "cp"
            else pytest.fail("must not recut a valid cache")
        ),
    )
    spec = {
        "pieces": [
            {
                "remote_media": source_path,
                "start_ms": 1_000,
                "end_ms": 5_000,
            }
        ]
    }

    prepared = producer_source_media.prepare_source_media(
        spec=spec,
        cid="candidate",
        out_root=out_root,
        host="localhost",
    )

    assert local.is_file()
    assert spec["pieces"][0]["source_media_sha256"] == "sha256:" + source_sha256
    assert prepared.piece_provenance_rows[0]["source_revalidation_status"] == (
        "HASH_BOUND_CACHE_SOURCE_ROOT_UNAVAILABLE"
    )


@pytest.mark.parametrize(
    "failure",
    [
        "SOURCE_MEDIA_MISSING: /recordings/official-replay.mp4",
        "SOURCE_RECORDING_ROOT_UNAVAILABLE: /recordings/official-replay.mp4",
    ],
)
def test_prepare_source_media_does_not_reuse_ineligible_or_corrupt_cache(
    monkeypatch, tmp_path, failure
):
    out_root, local, source_path = _install_exact_piece_provenance(
        tmp_path,
        source_sha256="a" * 64,
    )
    if failure.startswith("SOURCE_RECORDING_ROOT_UNAVAILABLE"):
        local.write_bytes(b"tampered cached piece")
    monkeypatch.setattr(
        producer_source_media,
        "_source_media_sha256",
        lambda *_args: (_ for _ in ()).throw(RuntimeError(failure)),
    )
    spec = {
        "pieces": [
            {
                "remote_media": source_path,
                "start_ms": 1_000,
                "end_ms": 5_000,
            }
        ]
    }

    with pytest.raises(RuntimeError, match=failure.split(":", 1)[0]):
        producer_source_media.prepare_source_media(
            spec=spec,
            cid="candidate",
            out_root=out_root,
            host="localhost",
        )
