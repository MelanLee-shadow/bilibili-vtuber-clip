import hashlib
from pathlib import Path

import pytest

from src.autoslice import recut_materialization as recut


@pytest.mark.parametrize("existing", ["burned", "generated_ass", "speaker_burned"])
def test_burn_preserves_historical_outputs_before_any_writer(tmp_path, monkeypatch, existing):
    media = tmp_path / "historical" / "clip.recut.mp4"
    media.parent.mkdir()
    media.write_bytes(b"original unburned media")
    srt = tmp_path / "current.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n当前字幕\n")
    record = {"status": "MATERIALIZED", "media_path": str(media), "subtitle_path": str(srt)}
    if existing == "speaker_burned":
        ass = tmp_path / "current.ass"
        ass.write_bytes(b"validated prebuilt ASS")
        record.update(
            subtitle_ass_path=str(ass),
            artifact_hashes={"ass_sha256": "sha256:" + hashlib.sha256(ass.read_bytes()).hexdigest()},
        )
        output = media.with_suffix(".burned-final-speaker.mp4")
    elif existing == "generated_ass":
        output = media.with_suffix(".final-sapphire72.ass")
    else:
        output = media.with_suffix(".burned-final-sapphire72.mp4")
    output.write_bytes(b"historical artifact must survive")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    def forbidden(*args, **kwargs):
        raise AssertionError("Writer or media processing reached before preservation check")

    monkeypatch.setattr(recut, "_write_sapphire_ass_from_srt", forbidden)
    monkeypatch.setattr(recut, "_video_dimensions", forbidden)
    result = recut._burn_preview_subtitles(record, run_ffmpeg=True)

    assert result["burned_preview"]["reason_code"] == "BURN_OUTPUT_ALREADY_EXISTS_PRESERVE_BASELINE"
    assert result["burned_preview"]["status"] == "FAILED"
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_versioned_burn_revision_uses_new_siblings_and_preserves_legacy_baseline(tmp_path):
    media = tmp_path / "clip.recut.mp4"
    media.write_bytes(b"unburned media")
    subtitle = tmp_path / "clip.recut.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n首版字幕\n",
        encoding="utf-8",
    )
    record = {
        "status": "MATERIALIZED",
        "media_path": str(media),
        "subtitle_path": str(subtitle),
    }

    first = recut._burn_preview_subtitles(record, run_ffmpeg=False)
    first_preview = first["burned_preview"]
    first_ass = tmp_path / Path(first_preview["ass_path"]).name
    first_burn = tmp_path / Path(first_preview["path"]).name
    baseline = {
        first_ass: first_ass.read_bytes(),
        first_burn: first_burn.read_bytes(),
    }

    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n修复后字幕\n",
        encoding="utf-8",
    )
    revision = "r" + "a" * 16
    successor = recut._burn_preview_subtitles(
        record,
        run_ffmpeg=False,
        burn_revision=revision,
    )
    successor_preview = successor["burned_preview"]
    successor_ass = Path(successor_preview["ass_path"])
    successor_burn = Path(successor_preview["path"])
    assert successor_preview["status"] == "DRY_RUN"
    assert successor_preview["burn_revision"] == revision
    assert successor_ass != first_ass and successor_burn != first_burn
    assert f".burn-{revision}." in successor_ass.name
    assert f".burn-{revision}." in successor_burn.name
    assert successor_ass.is_file() and successor_burn.is_file()
    assert successor_ass.read_text(encoding="utf-8") != first_ass.read_text(encoding="utf-8")
    successor_baseline = {
        successor_ass: successor_ass.read_bytes(),
        successor_burn: successor_burn.read_bytes(),
    }
    assert {path: path.read_bytes() for path in baseline} == baseline

    collision = recut._burn_preview_subtitles(
        record,
        run_ffmpeg=False,
        burn_revision=revision,
    )
    assert collision["burned_preview"]["status"] == "FAILED"
    assert collision["burned_preview"]["reason_code"] == (
        "BURN_OUTPUT_ALREADY_EXISTS_PRESERVE_BASELINE"
    )
    assert {path: path.read_bytes() for path in baseline} == baseline
    assert {path: path.read_bytes() for path in successor_baseline} == successor_baseline

    other_revision = "r" + "b" * 16
    other = recut._burn_preview_subtitles(
        record,
        run_ffmpeg=False,
        burn_revision=other_revision,
    )
    assert other["burned_preview"]["status"] == "DRY_RUN"
    assert Path(other["burned_preview"]["path"]) not in (first_burn, successor_burn)
    assert {path: path.read_bytes() for path in baseline} == baseline


def test_versioned_burn_revision_copies_prebuilt_ass_without_overwriting_source(tmp_path):
    media = tmp_path / "speaker.recut.mp4"
    media.write_bytes(b"unburned media")
    subtitle = tmp_path / "speaker.recut.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    source_ass = tmp_path / "speaker.source.ass"
    source_ass.write_bytes(b"validated source ASS")
    source_sha = "sha256:" + hashlib.sha256(source_ass.read_bytes()).hexdigest()
    record = {
        "status": "MATERIALIZED",
        "media_path": str(media),
        "subtitle_path": str(subtitle),
        "subtitle_ass_path": str(source_ass),
        "artifact_hashes": {"ass_sha256": source_sha},
        "subtitle_style": "lidousha-speaker-sapphire",
    }

    first = recut._burn_preview_subtitles(record, run_ffmpeg=False)
    first_burn = Path(first["burned_preview"]["path"])
    source_before = source_ass.read_bytes()
    successor = recut._burn_preview_subtitles(
        record,
        run_ffmpeg=False,
        burn_revision="r" + "c" * 16,
    )
    successor_ass = Path(successor["burned_preview"]["ass_path"])
    successor_burn = Path(successor["burned_preview"]["path"])
    assert successor["burned_preview"]["status"] == "DRY_RUN"
    assert successor_ass != source_ass and successor_ass.read_bytes() == source_before
    assert successor_burn != first_burn and successor_burn.is_file()
    assert source_ass.read_bytes() == source_before

    collision = recut._burn_preview_subtitles(
        record,
        run_ffmpeg=False,
        burn_revision="r" + "c" * 16,
    )
    assert collision["burned_preview"]["status"] == "FAILED"
    assert collision["burned_preview"]["reason_code"] == (
        "BURN_OUTPUT_ALREADY_EXISTS_PRESERVE_BASELINE"
    )
    assert source_ass.read_bytes() == source_before
