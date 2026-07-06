from scripts.cpa_semantic_review import _surrounding_context


def _write_srt(path):
    path.write_text(
        "1\n00:00:10,000 --> 00:00:13,000\n我们来看看这张AI生成的图\n\n"
        "2\n00:00:20,000 --> 00:00:23,000\n好像阿朵\n\n"
        "3\n00:00:30,000 --> 00:00:33,000\n一眼AI 好吧\n\n"
        "4\n00:00:40,000 --> 00:00:43,000\n下一张图\n\n"
        "5\n00:05:00,000 --> 00:05:03,000\n很久之后的话\n",
        encoding="utf-8",
    )
    return path


def test_surrounding_context_extracts_before_and_after_window(tmp_path):
    srt = _write_srt(tmp_path / "source.srt")
    surrounding = _surrounding_context(str(srt), start_ms=20_000, end_ms=33_000)
    assert surrounding is not None
    assert "我们来看看这张AI生成的图" in surrounding["before_text"]
    assert "好像阿朵" not in surrounding["before_text"]
    assert "下一张图" in surrounding["after_text"]
    # Cues outside the 90s window are not context.
    assert "很久之后的话" not in surrounding["after_text"]
    assert surrounding["window_ms"] == 90_000


def test_surrounding_context_missing_srt_is_none(tmp_path):
    assert _surrounding_context("", start_ms=0, end_ms=1_000) is None
    assert _surrounding_context(str(tmp_path / "nope.srt"), start_ms=0, end_ms=1_000) is None


def test_surrounding_context_empty_when_window_covers_everything(tmp_path):
    srt = _write_srt(tmp_path / "source.srt")
    surrounding = _surrounding_context(str(srt), start_ms=0, end_ms=310_000)
    assert surrounding is None
