"""Source punctuation must not survive as half a pair after chat splicing."""
from pathlib import Path

import pytest

from src.autoslice.chat_authority import ChatEvidence, apply_authoritative_chat_evidence
from src.autoslice.chat_repair import _aligned_span_replacements, _partial_question_patch
from src.autoslice.chat_span_alignment import remove_orphaned_splice_marks
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.subtitle_fidelity import apply_title_mark_balance_guard
from src.autoslice.title_policy import _TITLE_MARK_PAIRS


@pytest.mark.parametrize("marks", tuple(_TITLE_MARK_PAIRS.items()))
@pytest.mark.parametrize("side", ("head", "tail"))
def test_chat_splice_keeps_words_without_orphaning_source_marks(marks, side):
    opening, closing = marks
    phrase = "这个新的故事"
    if side == "tail":
        before = [opening + phrase + closing + "是这一部吗"]
        expected = phrase + "是这一部吗"
    else:
        before = ["她说" + opening + phrase + closing]
        expected = "她说" + phrase
    result = _aligned_span_replacements(phrase, before)
    assert result is not None
    replacements, audit = result
    assert replacements == [expected]
    assert audit["removed_orphaned_source_marks_at_splice"] == [
        opening if side == "head" else closing
    ]


def test_nested_pairs_across_cues_are_reconciled_before_cue_redistribution():
    result = _aligned_span_replacements(
        "这个新的故事", ["《“这个新的", "故事”》是这一部吗"]
    )
    assert result is not None
    replacements, audit = result
    assert len(replacements) == 2
    assert "".join(replacements) == "这个新的故事是这一部吗"
    assert audit["removed_orphaned_source_marks_at_splice"] == ["”", "》"]


@pytest.mark.parametrize("marks", tuple(_TITLE_MARK_PAIRS.items()))
def test_question_particle_splice_uses_the_same_mark_contract(marks):
    opening, closing = marks
    assert _partial_question_patch(
        "这个新的故事吗", opening + "这个新的故事" + closing + "我们已经看了"
    ) == "这个新的故事吗，我们已经看了"


@pytest.mark.parametrize("kind", ("danmaku", "superchat"))
def test_question_particle_enters_from_normal_chat_inputs(kind):
    source = "1\n00:00:05,000 --> 00:00:09,000\n《恋死看》，我们已经看了\n"
    output, audit = apply_authoritative_chat_evidence(
        source, [ChatEvidence(kind, 0, "恋死看吗")],
        support_srt_texts=[source],
    )
    if kind == "superchat":
        # The existing particle-only lane is danmaku-only. Keep that boundary.
        assert output == source
        assert not audit["applied"]
        return
    assert [c.text for c in parse_srt_cues(output)] == ["恋死看吗，我们已经看了"]
    assert audit["applied"][0]["mode"] == "question_particle_patch"
    assert audit["applied"][0]["survived"] is True
    assert audit["status"] == "APPLIED_AND_VERIFIED"
    _, structural = apply_title_mark_balance_guard(output)
    assert structural["status"] == "CLEAN"


@pytest.mark.parametrize("text", (
    "普通句子是这一部吗",
    "《这个新的故事》",
    "她说《这个新的故事》是这一部吗",
    "《这个新的故事》是这一部吗",
))
def test_already_balanced_splice_keeps_existing_result(text):
    result = _aligned_span_replacements(text, [text])
    assert result is not None
    assert result[0] == [text]
    assert "removed_orphaned_source_marks_at_splice" not in result[1]


@pytest.mark.parametrize("source,replacement,tail", (
    ("故事》是这一部吗", "故事", "》是这一部吗"),
    ("《故事》是这一部吗", "“故事", "》是这一部吗"),
    ("《故事》是这一部吗", "故事", "》伪造的回话"),
))
def test_malformed_or_unbound_material_still_reaches_original_guard(source, replacement, tail):
    start = 0
    end = source.index("》")
    combined = replacement + tail
    actual, removed = remove_orphaned_splice_marks(
        source=source, start=start, end=end, head="", replacement=replacement,
        tail=tail, mark_pairs=_TITLE_MARK_PAIRS,
    )
    assert (actual, removed) == (combined, ())


def test_original_complex_title_guard_remains_blocking():
    source = "1\n00:00:01,000 --> 00:00:04,000\n《《未闭合标题\n"
    _, audit = apply_title_mark_balance_guard(source)
    assert audit["status"] == "UNRESOLVED_COMPLEX_IMBALANCE"


def test_actual_read_aloud_input_persists_and_reopens_without_unbalanced_title(tmp_path):
    root = Path(__file__).parent / "fixtures" / "chat_splice_title_read"
    source = (root / "cpa-reviewed.srt").read_text()
    support = (root / "asr-draft.srt").read_text()
    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("danmaku", 111719, "纯白少女的狼人杀")],
        support_srt_texts=[support],
    )
    assert audit["status"] == "APPLIED_AND_VERIFIED"
    assert len(audit["applied"]) == 1
    row = audit["applied"][0]
    assert row["owner_eligible"] is True
    assert row["survived"] is True
    assert row["cue_indexes"] == [54]
    assert row["after"] == ["纯白少女的狼人杀是这一部吗"]
    current = parse_srt_cues(output)
    previous = parse_srt_cues(source)
    assert len(current) == len(previous)
    assert [(c.start_ms, c.end_ms) for c in current] == [
        (c.start_ms, c.end_ms) for c in previous
    ]
    assert [c.text for i, c in enumerate(current) if i != 53] == [
        c.text for i, c in enumerate(previous) if i != 53
    ]
    path = tmp_path / "normal-chat-output.srt"
    path.write_text(output)
    guarded, structural_audit = apply_title_mark_balance_guard(path.read_text())
    assert guarded == output
    assert structural_audit["status"] == "CLEAN"
    assert structural_audit["repair_count"] == 0
    # Independent support and protected-cue ownership retain their rejection.
    for kwargs in ({"support_srt_texts": ()}, {
        "support_srt_texts": [support], "protected_cue_indexes": [54]
    }):
        rejected, rejected_audit = apply_authoritative_chat_evidence(
            source, [ChatEvidence("danmaku", 111719, "纯白少女的狼人杀")], **kwargs
        )
        assert rejected == source
        assert not rejected_audit["applied"]
