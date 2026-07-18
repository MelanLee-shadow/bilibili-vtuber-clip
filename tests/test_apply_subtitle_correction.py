import json
from pathlib import Path

from scripts.apply_subtitle_correction import (
    _project_existing_text_onto_timing,
    _project_reviewed_text_onto_timing,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_reviewed_text_is_projected_onto_authoritative_timing_with_drop(
    tmp_path: Path,
) -> None:
    text_source = _write(
        tmp_path / "automatic.srt",
        """1
00:00:01,000 --> 00:00:01,500
原文一

2
00:00:02,000 --> 00:00:02,500
背景日语

3
00:00:03,000 --> 00:00:03,400
旧专名
""",
    )
    decision_output = _write(
        tmp_path / "decision.srt",
        """1
00:00:01,000 --> 00:00:01,500
原文一

2
00:00:03,000 --> 00:00:03,400
正确专名
""",
    )
    timing_source = _write(
        tmp_path / "timing.srt",
        """1
00:00:00,800 --> 00:00:01,900
旧文本不构成文字 authority

2
00:00:02,000 --> 00:00:02,900
旧文本不构成文字 authority

3
00:00:03,000 --> 00:00:04,800
旧文本不构成文字 authority
""",
    )
    override = tmp_path / "override.json"
    override.write_text(
        json.dumps(
            {
                "overrides": [
                    {"source_cue": 2, "action": "drop"},
                    {"source_cue": 3, "action": "replace"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    output = _project_reviewed_text_onto_timing(
        text_source=text_source,
        decision_output=decision_output,
        timing_source=timing_source,
        text_override=override,
    )

    assert "00:00:00,800 --> 00:00:01,900\n原文一" in output
    assert "00:00:03,000 --> 00:00:04,800\n正确专名" in output
    assert "背景日语" not in output
    assert "00:00:02,000 --> 00:00:02,900" not in output


def test_refresh_only_keeps_text_but_replaces_every_timing_boundary() -> None:
    text_srt = """1
00:00:01,000 --> 00:00:01,200
完整语音

2
00:00:02,000 --> 00:00:02,300
第二句
"""
    timing_srt = """1
00:00:00,800 --> 00:00:01,900
旧文本

2
00:00:02,000 --> 00:00:03,800
旧文本
"""

    output = _project_existing_text_onto_timing(
        text_srt=text_srt,
        timing_srt=timing_srt,
    )

    assert "00:00:00,800 --> 00:00:01,900\n完整语音" in output
    assert "00:00:02,000 --> 00:00:03,800\n第二句" in output
