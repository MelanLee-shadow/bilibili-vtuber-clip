from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.autoslice import legacy_hls_recovery as recovery


def _playlist(tmp_path: Path) -> tuple[Path, Path]:
    raw = tmp_path / "22966160_20260722-20-05-11.m4s"
    raw.write_bytes(b"raw-media")
    playlist = raw.with_suffix(".m3u8")
    playlist.write_text(
        "#EXTM3U\n"
        f'#EXT-X-MAP:URI="{raw.name}",BYTERANGE="4@0"\n'
        "#EXTINF:1,\n"
        f"{raw.name}\n"
        "#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    return playlist, raw


def test_recover_finalized_legacy_hls_publishes_verified_mp4(
    tmp_path, monkeypatch
):
    playlist, raw = _playlist(tmp_path)

    def fake_run(command, *, timeout):
        if command[0] == "ffmpeg" and command[-1].endswith(".mp4"):
            Path(command[-1]).write_bytes(b"verified-mp4")
            return type("Completed", (), {"stdout": ""})()
        if command[0] == "ffprobe":
            return type(
                "Completed",
                (),
                {
                    "stdout": json.dumps(
                        {
                            "streams": [
                                {
                                    "codec_type": "video",
                                    "codec_name": "h264",
                                    "width": 1920,
                                    "height": 1080,
                                },
                                {
                                    "codec_type": "audio",
                                    "codec_name": "aac",
                                },
                            ],
                            "format": {"duration": "1500.35"},
                        }
                    )
                },
            )()
        return type("Completed", (), {"stdout": ""})()

    monkeypatch.setattr(recovery, "_run", fake_run)
    receipts = recovery.recover_finalized_legacy_hls(
        tmp_path,
        room_id="22966160",
    )

    target = playlist.with_suffix(".mp4")
    assert target.read_bytes() == b"verified-mp4"
    assert receipts[0]["raw_media_size"] == raw.stat().st_size
    assert receipts[0]["target_sha256"].startswith("sha256:")
    receipt = json.loads(
        target.with_suffix(".legacy-hls-recovery.json").read_text()
    )
    assert receipt == receipts[0]


def test_recover_finalized_legacy_hls_rejects_external_uri(tmp_path):
    playlist, _raw = _playlist(tmp_path)
    playlist.write_text(
        "#EXTM3U\n#EXTINF:1,\nhttps://example.test/segment.m4s\n"
        "#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )

    with pytest.raises(
        recovery.LegacyHlsRecoveryError,
        match="EXTERNAL_MEDIA_FORBIDDEN",
    ):
        recovery.recover_finalized_legacy_hls(
            tmp_path,
            room_id="22966160",
        )
