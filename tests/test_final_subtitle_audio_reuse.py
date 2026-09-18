from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.free_asr_client import to_srt
from src.autoslice import final_subtitle_audio_gate as gate
from src.autoslice.final_subtitle_audio_reuse import (
    FinalSubtitleAudioReuseError,
    reuse_final_subtitle_audio_check,
)


CID = "candidate"
ROWS = [
    (1_000, 5_000, "开场这句内容很特别"),
    (8_000, 11_000, "第二句用于首段覆盖"),
    (23_000, 27_000, "中段这里继续说明问题"),
    (42_000, 46_000, "最后我们明确完成收束"),
]


def _time(ms: int) -> str:
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _srt(rows: list[tuple[int, int, str]]) -> str:
    return "\n\n".join(
        f"{index}\n{_time(start)} --> {_time(end)}\n{text}"
        for index, (start, end, text) in enumerate(rows, start=1)
    ) + "\n"


def _raw(rows: list[tuple[int, int, str]]) -> dict:
    return {
        "utterances": [
            {
                "start_time": start,
                "end_time": end,
                "transcript": text,
                "words": [],
            }
            for start, end, text in rows
        ]
    }


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fixture(tmp_path: Path) -> dict[str, object]:
    parent_dir = tmp_path / "parent"
    current_dir = tmp_path / "current"
    parent_dir.mkdir()
    current_dir.mkdir()

    parent_media = parent_dir / f"{CID}.mp4"
    parent_media.write_bytes(b"parent burned media")
    parent_digest = _sha(parent_media)
    parent_srt = parent_dir / f"{CID}.srt"
    parent_srt.write_text(_srt(ROWS), encoding="utf-8")
    parent_record: dict[str, object] = {
        "candidate_id": CID,
        "artifact_hashes": {"burned_video_sha256": parent_digest},
        "burned_preview": {
            "status": "BURNED",
            "path": str(parent_media),
            "burned_sha256": parent_digest,
            "branding_intro": {"intro_offset_ms": 0},
        },
    }
    raw_result = _raw(ROWS)

    def extract(_media: Path, output: Path) -> None:
        output.write_bytes(b"parent extracted audio")

    parent_record = gate.capture_final_subtitle_audio_check(
        parent_record,
        parent_srt,
        parent_dir,
        CID,
        adapters=gate.FinalSubtitleAudioGateAdapters(extract, lambda _audio: raw_result, to_srt),
    )
    parent_record_path = parent_dir / f"{CID}.record.json"
    _write_json(parent_record_path, parent_record)

    current_media = current_dir / f"{CID}.mp4"
    current_media.write_bytes(b"current successor burned media")
    current_digest = _sha(current_media)
    current_srt = current_dir / f"{CID}.srt"
    current_rows = list(ROWS)
    current_rows[1] = (8_000, 11_000, "第二句的文字发生了局部修订")
    current_srt.write_text(_srt(current_rows), encoding="utf-8")
    current_record = copy.deepcopy(parent_record)
    current_record["burned_preview"] = {
        "status": "BURNED",
        "path": str(current_media),
        "burned_sha256": current_digest,
        "branding_intro": {"intro_offset_ms": 0},
    }
    current_record["artifact_hashes"] = {
        **dict(parent_record["artifact_hashes"]),  # type: ignore[index]
        "burned_video_sha256": current_digest,
    }
    current_record_path = current_dir / f"{CID}.record.json"
    _write_json(current_record_path, current_record)

    return {
        "parent_dir": parent_dir,
        "current_dir": current_dir,
        "parent_media": parent_media,
        "parent_srt": parent_srt,
        "parent_record": parent_record,
        "parent_record_path": parent_record_path,
        "current_media": current_media,
        "current_srt": current_srt,
        "current_record": current_record,
        "current_record_path": current_record_path,
        "output_dir": tmp_path / "audio-reuse-v1",
    }


def test_reuse_copies_parent_witness_and_rebinds_current_receipt(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    calls: list[str] = []

    def extract(media: Path, output: Path) -> None:
        assert media == fixture["current_media"]
        calls.append("extract")
        output.write_bytes(b"parent extracted audio")

    parent_witness = fixture["parent_dir"] / f"{CID}.subtitle-audio-witness.srt"  # type: ignore[operator]
    parent_raw = fixture["parent_dir"] / f"{CID}.subtitle-audio-bcut.raw.json"  # type: ignore[operator]
    source_bytes = {path.name: path.read_bytes() for path in (parent_witness, parent_raw)}
    current_before = copy.deepcopy(fixture["current_record"])

    result = reuse_final_subtitle_audio_check(
        fixture["parent_record"],  # type: ignore[arg-type]
        fixture["current_record"],  # type: ignore[arg-type]
        parent_final_srt=fixture["parent_srt"],  # type: ignore[arg-type]
        current_final_srt=fixture["current_srt"],  # type: ignore[arg-type]
        parent_evidence_dir=fixture["parent_dir"],  # type: ignore[arg-type]
        output_dir=fixture["output_dir"],  # type: ignore[arg-type]
        parent_record_path=fixture["parent_record_path"],  # type: ignore[arg-type]
        current_record_path=fixture["current_record_path"],  # type: ignore[arg-type]
        _extract_audio=extract,
    )

    assert calls == ["extract"]
    assert result.status == "PASS"
    assert result.current_receipt["status"] == "PASS"
    assert result.audio_sha256 == result.parent_audio_sha256
    assert result.record["subtitle_audio_correspondence"]["final_srt_sha256"] == _sha(fixture["current_srt"])  # type: ignore[arg-type]
    reuse = result.record["subtitle_audio_correspondence"]["audio_reuse"]
    assert reuse["new_provider_calls"] == 0
    assert reuse["new_asr_calls"] == 0
    assert reuse["parent"]["actual_media_sha256"] == _sha(fixture["parent_media"])  # type: ignore[arg-type]
    assert reuse["current"]["actual_media_sha256"] == _sha(fixture["current_media"])  # type: ignore[arg-type]
    assert fixture["current_record"] == current_before
    assert result.record_path.name == "AUDIO-VERIFIED-RECORD.json"
    assert json.loads(result.record_path.read_text(encoding="utf-8")) == result.record

    output_dir = fixture["output_dir"]
    for name, payload in source_bytes.items():  # type: ignore[union-attr]
        assert (output_dir / name).read_bytes() == payload  # type: ignore[operator]
    assert gate.validate_final_subtitle_audio_check(
        result.record,
        final_srt=fixture["current_srt"],  # type: ignore[arg-type]
        actual_media=fixture["current_media"],  # type: ignore[arg-type]
        package_root=output_dir,  # type: ignore[arg-type]
    )["status"] == "PASS"


def test_parent_evidence_drift_blocks_before_extraction(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    witness = fixture["parent_dir"] / f"{CID}.subtitle-audio-witness.srt"  # type: ignore[operator]
    witness.write_bytes(witness.read_bytes() + b"\n")
    calls: list[str] = []

    def extract(_media: Path, _output: Path) -> None:
        calls.append("extract")

    with pytest.raises(FinalSubtitleAudioReuseError, match="AUDIO_REUSE_PARENT_NATIVE_VALIDATION_FAILED"):
        reuse_final_subtitle_audio_check(
            fixture["parent_record"],  # type: ignore[arg-type]
            fixture["current_record"],  # type: ignore[arg-type]
            parent_final_srt=fixture["parent_srt"],  # type: ignore[arg-type]
            current_final_srt=fixture["current_srt"],  # type: ignore[arg-type]
            parent_evidence_dir=fixture["parent_dir"],  # type: ignore[arg-type]
            output_dir=fixture["output_dir"],  # type: ignore[arg-type]
            _extract_audio=extract,
        )
    assert calls == []
    assert not fixture["output_dir"].exists()  # type: ignore[union-attr]


def test_different_successor_audio_never_creates_reuse_record(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    def extract(_media: Path, output: Path) -> None:
        output.write_bytes(b"different extracted audio")

    with pytest.raises(FinalSubtitleAudioReuseError, match="AUDIO_REUSE_AUDIO_HASH_MISMATCH"):
        reuse_final_subtitle_audio_check(
            fixture["parent_record"],  # type: ignore[arg-type]
            fixture["current_record"],  # type: ignore[arg-type]
            parent_final_srt=fixture["parent_srt"],  # type: ignore[arg-type]
            current_final_srt=fixture["current_srt"],  # type: ignore[arg-type]
            parent_evidence_dir=fixture["parent_dir"],  # type: ignore[arg-type]
            output_dir=fixture["output_dir"],  # type: ignore[arg-type]
            _extract_audio=extract,
        )
    assert not fixture["output_dir"].exists()  # type: ignore[union-attr]
