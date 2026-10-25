"""Production ASS projection: shared generation and an artifact-level SUB-24 gate."""

import ast
import hashlib
import inspect
from pathlib import Path
import runpy

import pytest

from scripts import apply_speaker_turn_overrides as talk
from src.autoslice import channel_profile, speaker_label_display, subtitle_rendering as render
from src.autoslice.review_package_ass_audit import audit_review_package_ass


ROOT = Path(__file__).resolve().parents[1]
SONG_PACKAGE = ROOT / (
    "reports/song-fastlane-20261024-publication-evidence/oci3/opt/bilive/autoslice/"
    "private-runs/output-continuation-20261023/production/song_133029_1424/"
    "REVIEW-PACKAGE-FL-8e60c667-001"
)
SKILL = ROOT / ".agent/skills/song-lyrics-timeline-aligner/scripts/align_timed_lyrics.py"
PREFIX_CODE = "SUBTITLE_ASS_SPEAKER_LABEL_VISIBLE"


def _audit(ass):
    return audit_review_package_ass(
        root=ass.parent, item={"ass_path": ass.name}, portable_required=False,
        max_visual_lines=2, max_visual_line_chars=28,
    )





@pytest.mark.parametrize("style", ["Default", "LDS", "GUEST"])
@pytest.mark.parametrize("text", [
    "[另一位嘉宾] 新的正文", "[Aurora_27 -0.08]新正文", "[名字]",
    r"{\c&HFFFFFF&}[名字] 带样式的前缀", r"\h[名字] 空白前缀",
    r"正常的第一行\N[名字] 第二行", "  [名字] 前导空白",
])
def test_artifact_gate_is_lane_and_name_independent(tmp_path, style, text):
    ass = tmp_path / "burn.ass"
    ass.write_text(f"[Events]\nDialogue: 0,0:00:00.00,0:00:02.00,{style},,0,0,0,,{text}\n")
    assert PREFIX_CODE in {i.code for i in _audit(ass).issues}


@pytest.mark.parametrize("text", ["保留[正文]方括号", "普通歌词", r"访客\{原文\}"])
def test_artifact_gate_keeps_nonprefix_content(tmp_path, text):
    ass = tmp_path / "burn.ass"
    ass.write_text(f"[Events]\nDialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{text}\n")
    assert not _audit(ass).issues


def test_new_song_enters_via_final_srt_and_reaches_artifact_gate(tmp_path):
    srt = tmp_path / "new.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n[新来的访客 +0.21]新的说话\n\n"
                   "2\n00:00:02,000 --> 00:00:04,000\n新的歌词\n")
    before = srt.read_bytes()
    ass = tmp_path / "new.ass"
    render._write_sapphire_ass_from_srt(srt, ass)
    assert not _audit(ass).issues
    events = [s.split(",", 9) for s in ass.read_text().splitlines() if s.startswith("Dialogue:")]
    assert [(s[3], s[9]) for s in events] == [("GUEST", "新的说话"), ("Default", "新的歌词")]
    assert srt.read_bytes() == before





@pytest.mark.parametrize("resolution,expected_sha", [
    ("1280x720", "6efb20f5e3d94621c3eeb45cde542b94a9b79d633df6bfc69b617688409e1b7b"),
    ("1920x1080", "bac0decf4f64b43c22b9b8006d04a541f983793b7b5ad13619dd254f9b2e8e8e"),
])
def test_timed_lyrics_skill_keeps_frozen_e795eba61_bytes(tmp_path, resolution, expected_sha):
    # Frozen from the exact prior writer, before refactoring. Includes long
    # lines, physical lines, braces, backslashes, whitespace and rounding ties.
    cues = [(0.005, 2.025, "歌词{保留}\\路径"), (2.03, 5.29, "甲" * 60),
            (5.30, 8.005, "第一行\n第二行"), (8.1, 9.999, "  空格  与文字  ")]
    output = tmp_path / "skill.ass"
    runpy.run_path(str(SKILL))["write_ass"](cues, output, play_res=resolution)
    assert hashlib.sha256(output.read_bytes()).hexdigest() == expected_sha


def test_all_ordinary_writers_call_one_renderer(tmp_path, monkeypatch):
    calls = []
    original = render.render_burned_ass

    def observed(cues, **kwargs):
        calls.append((cues, kwargs))
        return original(cues, **kwargs)

    monkeypatch.setattr(render, "render_burned_ass", observed)
    srt = tmp_path / "final.srt"
    srt.write_text(f"1\n00:00:00,000 --> 00:00:02,000\n[{talk.HOST_SPEAKER}] 说话\n")
    render._write_sapphire_ass_from_srt(srt, tmp_path / "song.ass")
    talk.write_ass(talk.parse_labelled_srt(srt), tmp_path / "talk.ass")
    runpy.run_path(str(SKILL))["write_ass"]([(0, 2, f"[{talk.HOST_SPEAKER}] 说话")],
                                          tmp_path / "skill.ass", play_res="1920x1080")
    assert len(calls) == 3
    for ass in tmp_path.glob("*.ass"):
        assert not _audit(ass).issues
        assert "说话" in ass.read_text() and f"[{talk.HOST_SPEAKER}]" not in ass.read_text()


def test_label_syntax_has_one_definition_and_explicit_talk_authority(tmp_path):
    from src.autoslice import song_spoken_interlude, review_package_ass_audit

    assert talk.LABEL_RE is channel_profile.LABEL_RE
    assert song_spoken_interlude.LABEL_RE is channel_profile.LABEL_RE
    assert speaker_label_display.LABEL_RE is channel_profile.LABEL_RE
    assert review_package_ass_audit.LABEL_RE is channel_profile.LABEL_RE
    srt = tmp_path / "unknown.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n[未知嘉宾 +0.06] 不可获准\n")
    with pytest.raises(ValueError, match="not labelled"):
        talk.parse_labelled_srt(srt)


@pytest.mark.parametrize("body", ["[未知嘉宾] 正文", "[李豆沙 +0.06] 正文", "[李豆沙]正文"])
def test_shared_syntax_does_not_widen_canonical_release_speaker_authority(tmp_path, body):
    from src.autoslice.review_package_ass_audit import _speaker_cues

    srt = tmp_path / "speaker.srt"
    srt.write_text(f"1\n00:00:00,000 --> 00:00:02,000\n{body}\n")
    cues, issues = _speaker_cues(srt)
    assert not cues and {i.code for i in issues} == {"SUBTITLE_SPEAKER_SRT_INVALID"}


def test_no_public_label_option_or_production_caller_remains():
    assert "show_speaker_labels" not in inspect.signature(talk.write_ass).parameters
    assert "show_speaker_labels" not in inspect.signature(speaker_label_display.speaker_display).parameters
    for directory in (ROOT / "src", ROOT / "scripts", ROOT / ".agent/skills"):
        for path in directory.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            assert not any(isinstance(node, ast.Call) and
                           any(k.arg == "show_speaker_labels" for k in node.keywords)
                           for node in ast.walk(tree)), str(path.relative_to(ROOT))


def test_no_second_speaker_prefix_regex_in_repository():
    duplicates = []
    for directory in (ROOT / "src", ROOT / "scripts", ROOT / ".agent/skills"):
        for path in directory.rglob("*.py"):
            if path == ROOT / "src/autoslice/channel_profile.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "re"
                        and node.func.attr in {"compile", "sub", "match", "search", "fullmatch"}
                        and node.args):
                    continue
                pattern = node.args[0]
                while isinstance(pattern, ast.BinOp):
                    pattern = pattern.left
                if not (isinstance(pattern, ast.Constant) and isinstance(pattern.value, str)
                        and pattern.value.startswith(r"^\[")):
                    continue
                # These are a fixed glossary command and a whole-string chat
                # emote, not a speaker prefix followed by subtitle body text.
                if (pattern.value.startswith(r"^\[exact-cue\]")
                        or pattern.value == r"^\[[^\[\]\n]{1,48}\]$"):
                    continue
                duplicates.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not duplicates, duplicates


@pytest.mark.parametrize("raw_label,stripped", [
    ("李豆沙", True), ("连线", True), ("新嘉宾", False), ("李豆沙 +0.06", False),
])
def test_evidence_and_regression_keep_exact_label_membership(raw_label, stripped):
    from src.autoslice import chat_evidence as chat, subtitle_regression as regression

    body = f"[{raw_label}] 正文[保留]"
    srt = f"1\n00:00:00,000 --> 00:00:02,000\n{body}\n"
    expected = chat.normalize_chat_text("正文[保留]" if stripped else body)
    assert chat.normalize_srt_payload_text(srt, strip_speaker_labels=True) == expected
    assert chat.normalize_srt_payload_window(
        srt, start_ms=0, end_ms=2000, strip_speaker_labels=True,
    ) == expected
    assert chat.normalize_srt_owner_payload_window(
        srt, start_ms=0, end_ms=2000, min_overlap_ms=1, strip_speaker_labels=True,
    ) == expected
    assert chat.normalize_srt_payload_text(srt) == chat.normalize_chat_text(body)
    assert regression._surface_payload(srt)[0] == expected


@pytest.mark.parametrize("body,accepted", [
    ("[李豆沙]正文", True), ("[连线] 第一行\n第二行", True),
    ("[新嘉宾] 正文", False), ("[李豆沙 +0.06] 正文", False), ("[李豆沙]", False),
])
def test_addressee_keeps_canonical_nonempty_speaker_authority(body, accepted):
    from src.autoslice.addressee_attribution import parse_speaker_labelled_srt

    rows = parse_speaker_labelled_srt(f"1\n00:00:00,000 --> 00:00:02,000\n{body}\n")
    if accepted:
        assert rows == [(body[1:body.index("]")], body[body.index("]") + 1:].strip())]
    else:
        assert rows is None


def test_text_baseline_guard_still_checks_each_line_and_iterable():
    from src.autoslice.text_baseline_guard import contains_speaker_label_prefix

    assert contains_speaker_label_prefix("普通正文\n[新嘉宾 -0.12] 第二行")
    assert contains_speaker_label_prefix(value for value in ("普通正文", "[连线] 说话"))
    assert not contains_speaker_label_prefix("正文[方括号]\n普通正文")
    assert not contains_speaker_label_prefix(["正文", " [不是行首标签] 正文"])


def test_restatement_probe_and_harvest_preserve_raw_labels_and_authority(tmp_path):
    from scripts import harvest_human_review_truth as harvest, probe_restatement_recall as probe
    from src.autoslice.restatement_recall import _split_label_prefix

    raw = "[李豆沙 +0.06]  正文\n第二行"
    assert _split_label_prefix(raw) == ("[李豆沙 +0.06]", "李豆沙 +0.06", "正文\n第二行")
    assert harvest.split_label(raw) == ("李豆沙 +0.06", " 正文\n第二行")
    srt = tmp_path / "probe.srt"
    srt.write_text(f"1\n00:00:00,000 --> 00:00:02,000\n{raw}\n")
    row, = probe.load_cues(srt)
    assert (row.label, row.text) == harvest.split_label(raw)
    pristine, = harvest.parse_srt(srt.read_text())
    changed, = harvest.parse_srt(srt.read_text().replace("+0.06", "+0.07"))
    with pytest.raises(harvest.HarvestError, match="label prefix edited"):
        harvest.harvest_cue(pristine, changed)


def test_successor_keeps_raw_speaker_decision_binding():
    from src.autoslice.reviewed_text_only_speaker_successor import (
        ReviewedTextOnlySpeakerSuccessorError, _validate_old_speaker_against_diagnostic,
    )

    old = talk.Cue(1, "00:00:00,000", "00:00:02,000", "", "正文", "")
    labelled = talk.Cue(1, old.start, old.end, "", "[李豆沙 +0.06] 正文", "")
    decision = dict(source_index=1, start=old.start, end=old.end, speaker="李豆沙",
                    text="正文", layer=0, placement="main", decision_source="frozen")
    with pytest.raises(ReviewedTextOnlySpeakerSuccessorError, match="SPEAKER_LABEL_OR_DECISION_DRIFT"):
        _validate_old_speaker_against_diagnostic(old_cues=[old], speaker_cues=[labelled], decisions=[decision])
    decision["speaker"] = "李豆沙 +0.06"
    assert _validate_old_speaker_against_diagnostic(
        old_cues=[old], speaker_cues=[labelled], decisions=[decision],
    ) == [decision]
