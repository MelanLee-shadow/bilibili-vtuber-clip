"""Provider-free regression for no-change display-refresh tag routing."""
from pathlib import Path
import json

import pytest
import scripts.apply_subtitle_correction as correction


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("refresh_only", "change_text", "expected_tag_calls"),
    [(True, False, 0), (True, True, 1), (False, True, 1)],
)
def test_refresh_tag_generation_depends_on_actual_text_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refresh_only: bool,
    change_text: bool,
    expected_tag_calls: int,
) -> None:
    """A display-only refresh preserves tags; actual edits keep normal routing."""
    cid = "auto_refresh_tag_test"
    date = "2026-08-20"
    recut_dir = tmp_path / "out" / date / cid / "replacement_recuts"
    recut_dir.mkdir(parents=True)
    source = _write(recut_dir / "source.mp4", "synthetic source")
    subtitle = _write(
        recut_dir / "source.srt",
        "1\n00:00:00,000 --> 00:00:01,000\nOriginal words\n",
    )
    delivery = tmp_path / "delivery.mp4"
    record_path = recut_dir / f"{cid}.record.json"
    original_tags = {"tags": ["existing tag"], "provenance": "original"}
    record_path.write_text(json.dumps({
        "media_path": str(source),
        "subtitle_path": str(subtitle),
        "publish_staging": {"title": "Existing title"},
        "upload_tags": original_tags,
    }), encoding="utf-8")
    # Media and branding are boundaries outside this focused routing test.
    monkeypatch.setattr(correction, "_prepare_correction_branding", lambda **kw: (None, None))
    monkeypatch.setenv("AUTOSLICE_SPEAKER_MODE", "uniform_host")

    def fake_burn(materialized, **kwargs):
        media = Path(materialized["media_path"])
        video = _write(media.with_suffix(".burned-final-sapphire72.mp4"), "synthetic burn")
        ass = _write(media.with_suffix(".final-sapphire72.ass"), "synthetic ASS")
        return {"burned_preview": {"path": str(video), "ass_path": str(ass), "status": "BURNED"}}

    monkeypatch.setattr(correction, "_burn_preview_subtitles", fake_burn)
    calls = []
    updated_tags = {"tags": ["updated tag"], "provenance": "new request"}

    def fake_tags(title, srt_path, **kwargs):
        calls.append((title, srt_path.read_text(encoding="utf-8")))
        return updated_tags

    monkeypatch.setattr(correction, "generate_upload_tags", fake_tags)
    before_bytes = subtitle.read_bytes()
    argv = ["--cid", cid, "--date", date, "--delivery", str(delivery),
            "--out-base", str(tmp_path)]
    if refresh_only:
        argv.append("--refresh-only")
    if change_text:
        argv.extend(["--replace", "Original words=Updated words"])
    assert correction.main(argv) == 0
    assert len(calls) == expected_tag_calls
    final_record = json.loads(record_path.read_text(encoding="utf-8"))
    assert final_record["upload_tags"] == (updated_tags if change_text else original_tags)
    if not change_text:
        assert subtitle.read_bytes() == before_bytes
    assert final_record["publish_staging"]["title"] == "Existing title"
