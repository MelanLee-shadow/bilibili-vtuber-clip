from pathlib import Path

from scripts.build_speaker_blind_review import Cue, diagnostic_sample, parse_srt


def test_parse_srt_and_diagnostic_sample_are_deterministic(tmp_path: Path) -> None:
    srt = tmp_path / "sample.srt"
    blocks = []
    for number in range(1, 21):
        start = (number - 1) * 2
        duration = 1 if number % 2 else 2
        blocks.append(
            f"{number}\n00:00:{start:02d},000 --> 00:00:{start + duration:02d},000\nline {number}"
        )
    srt.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")

    cues = parse_srt(srt)
    assert len(cues) == 20
    first = diagnostic_sample(cues, 10)
    second = diagnostic_sample(cues, 10)
    assert first == second
    assert len(first) == 10
    assert len({cue.number for cue in first}) == 10
    assert sum(cue.duration_ms < 1500 for cue in first) == 4


def test_diagnostic_sample_keeps_all_when_request_exceeds_available() -> None:
    cues = [Cue(1, 0, 1000, "a"), Cue(2, 1000, 3000, "b")]
    assert diagnostic_sample(cues, 10) == cues
