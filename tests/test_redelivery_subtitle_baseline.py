import hashlib

from src.autoslice.redelivery_subtitle_baseline import (
    MODE,
    SCHEMA_VERSION,
    apply_redelivery_subtitle_baseline,
)


def _srt(*rows):
    def stamp(value):
        seconds, millis = divmod(value, 1_000)
        return f"00:00:{seconds:02d},{millis:03d}"

    return "\n\n".join(
        f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}"
        for index, (start, end, text) in enumerate(rows, start=1)
    ) + "\n"


def _config(path):
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": MODE,
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "authority": "previous reviewed delivery",
    }


def test_redelivery_preserves_prior_text_but_keeps_new_timing(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "旧审定第一句"), (1_000, 2_000, "旧审定第二句")),
        encoding="utf-8",
    )
    current = _srt(
        (20, 1_020, "随机漂移第一句"),
        (1_020, 2_020, "随机漂移第二句"),
    )

    output, audit = apply_redelivery_subtitle_baseline(
        current,
        config=_config(baseline),
        spec_parent=tmp_path,
    )

    assert "00:00:00,020 --> 00:00:01,020\n旧审定第一句" in output
    assert "00:00:01,020 --> 00:00:02,020\n旧审定第二句" in output
    assert "随机漂移" not in output
    assert audit["status"] == "APPLIED"
    assert audit["changed_cue_count"] == 2


def test_source_truth_window_remains_newer_than_redelivery_baseline(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "旧审定第一句"), (1_000, 2_000, "旧错误文字")),
        encoding="utf-8",
    )
    current = _srt(
        (0, 1_000, "随机漂移第一句"),
        (1_000, 2_000, "Ivan新源真值"),
    )

    output, audit = apply_redelivery_subtitle_baseline(
        current,
        config=_config(baseline),
        spec_parent=tmp_path,
        protected_windows=[(1_000, 2_000)],
    )

    assert "旧审定第一句" in output
    assert "Ivan新源真值" in output
    assert "旧错误文字" not in output
    assert audit["protected_cue_count"] == 1


def test_redelivery_fails_closed_on_segmentation_drift(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(
        _srt((0, 1_000, "第一句"), (1_000, 2_000, "第二句")),
        encoding="utf-8",
    )
    current = _srt((0, 2_000, "两句被合并"))

    output, audit = apply_redelivery_subtitle_baseline(
        current,
        config=_config(baseline),
        spec_parent=tmp_path,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert any(
        row["reason_code"] == "REDELIVERY_BASELINE_CUE_UNCONSUMED"
        for row in audit["failures"]
    )


def test_redelivery_fails_closed_on_stale_baseline_hash(tmp_path):
    baseline = tmp_path / "baseline.srt"
    baseline.write_text(_srt((0, 1_000, "第一句")), encoding="utf-8")
    config = _config(baseline)
    config["sha256"] = "0" * 64

    current = _srt((0, 1_000, "随机漂移"))
    output, audit = apply_redelivery_subtitle_baseline(
        current,
        config=config,
        spec_parent=tmp_path,
    )

    assert output == current
    assert audit["status"] == "FAILED"
    assert audit["failures"] == [
        {"reason_code": "REDELIVERY_BASELINE_SHA256_MISMATCH"}
    ]
