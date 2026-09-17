"""Final rendering must preserve its input words, unlike draft normalization.

Inputs and lexicons are synthetic. The real writer, media dispatcher and
independent ASS auditor are used; no model or publication authority is faked.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import recut_materialization as media
from src.autoslice import subtitle_rendering as render
from src.autoslice import term_lexicon
from src.autoslice.review_package_ass_audit import audit_review_package_ass


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _input(root: Path, text: str, *, newline: str = "\n", bom: bool = False) -> Path:
    path = root / "final.srt"
    raw = f"1\n00:00:01,005 --> 00:00:03,005\n{text}\n\n2\n00:00:04,000 --> 00:00:05,000\n后面一句不改\n"
    path.write_bytes(("\ufeff" if bom else "").encode() + raw.replace("\n", newline).encode())
    return path


def _lexicon(root: Path, source: str, target: str) -> Path:
    path = root / "term_lexicon.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-term-lexicon.v1",
                "sources": [],
                "overrides": [{"canonical": target, "aliases": [source]}],
            },
            ensure_ascii=False,
        )
    )
    return path


def _events(ass: Path) -> list[list[str]]:
    return [
        line.split(",", 9) for line in ass.read_text().splitlines() if line.startswith("Dialogue:")
    ]


def _audit(srt: Path, ass: Path, *, bad_hash: bool = False):
    text_hash, ass_hash = _sha(srt), _sha(ass)
    return audit_review_package_ass(
        root=srt.parent,
        item={
            "ass_path": ass.name,
            "ass_sha256": ("0" * 64 if bad_hash else ass_hash),
            "speaker_srt": srt.name,
            "speaker_srt_sha256": text_hash,
        },
        portable_required=True,
        max_visual_lines=2,
        max_visual_line_chars=28,
        record={"artifact_hashes": {"ass_sha256": ass_hash}},
        chat_authority={
            "speaker_ass_path": None,
            "speaker_ass_sha256": None,
            "final_text_srt_sha256": text_hash,
            "final_speaker_srt_sha256": text_hash,
        },
    )


@pytest.mark.parametrize(
    "source,target",
    [
        ("喵星人", "meow"),
        ("名字甲", "名字乙"),
        ("kmx", "KMX"),
        ("一副，一副", "一副"),
        ("PSP", "掌上游戏机"),
    ],
)
@pytest.mark.parametrize("location", ["adjacent", "ancestor", "environment"])
def test_final_ass_preserves_words_despite_discoverable_glossary(
    tmp_path, monkeypatch, source, target, location
):
    root = tmp_path / "package"
    root.mkdir()
    srt = _input(root, source + "今天来了")
    lexroot = root if location == "adjacent" else tmp_path
    lex = _lexicon(lexroot, source, target)
    monkeypatch.delenv("VTUBER_SLICE_TERM_LEXICON", raising=False)
    if location == "environment":
        # Move outside the ancestor chain so only the explicit existing setting finds it.
        other = tmp_path / "configuration"
        other.mkdir()
        new = other / lex.name
        lex.rename(new)
        lex = new
        monkeypatch.setenv("VTUBER_SLICE_TERM_LEXICON", str(lex))
    before = {p: p.read_bytes() for p in [srt, lex]}
    assert (
        render._parse_srt(srt)[0].text == target + "今天来了"
    )  # Legacy source recall remains useful.
    ass = root / "final.ass"
    render._write_sapphire_ass_from_srt(srt, ass)
    events = _events(ass)
    assert [row[9] for row in events] == [source + "今天来了", "后面一句不改"]
    assert [(row[1], row[2]) for row in events] == [
        ("0:00:01.01", "0:00:03.01"),
        ("0:00:04.00", "0:00:05.00"),
    ]
    assert not _audit(srt, ass).issues
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize("newline,bom", [("\n", False), ("\r\n", True), ("\r", False)])
def test_no_glossary_input_and_line_ending_behavior_stay_unchanged(
    tmp_path, monkeypatch, newline, bom
):
    monkeypatch.delenv("VTUBER_SLICE_TERM_LEXICON", raising=False)
    srt = _input(tmp_path, "这句原文没有变化", newline=newline, bom=bom)
    before = srt.read_bytes()
    ass = tmp_path / "final.ass"
    render._write_sapphire_ass_from_srt(srt, ass)
    assert [row[9] for row in _events(ass)] == ["这句原文没有变化", "后面一句不改"]
    assert srt.read_bytes() == before and not _audit(srt, ass).issues


def test_final_writer_does_not_consult_or_load_a_new_glossary(tmp_path, monkeypatch):
    srt = _input(tmp_path, "这份文字已经确定")

    def forbidden(_path):
        raise AssertionError("a lexical decision belongs before the final renderer")

    monkeypatch.setattr(render, "load_discovered_term_lexicon", forbidden)
    ass = tmp_path / "final.ass"
    render._write_sapphire_ass_from_srt(srt, ass)
    assert _events(ass)[0][9] == "这份文字已经确定"


def test_draft_parser_still_normalizes_and_preserves_source_offset(tmp_path, monkeypatch):
    monkeypatch.delenv("VTUBER_SLICE_TERM_LEXICON", raising=False)
    srt = _input(tmp_path, "喵星人今天来了")
    _lexicon(tmp_path, "喵星人", "meow")
    cues = render._parse_srt(srt, source_offset_ms=7000)
    assert cues[0].text == "meow今天来了" and cues[0].source_start_ms == 8005
    assert cues[0].source_end_ms == 10005


def test_invalid_draft_lexicon_is_not_a_new_final_rendering_dependency(tmp_path, monkeypatch):
    monkeypatch.delenv("VTUBER_SLICE_TERM_LEXICON", raising=False)
    srt = _input(tmp_path, "保留最终文字")
    (tmp_path / "term_lexicon.json").write_text("{not JSON")
    with pytest.raises(json.JSONDecodeError):
        render._parse_srt(srt)
    ass = tmp_path / "final.ass"
    render._write_sapphire_ass_from_srt(srt, ass)
    assert _events(ass)[0][9] == "保留最终文字" and not _audit(srt, ass).issues


def test_native_media_dispatcher_gets_preserved_ass_not_only_direct_writer(tmp_path, monkeypatch):
    monkeypatch.delenv("VTUBER_SLICE_TERM_LEXICON", raising=False)
    srt = _input(tmp_path, "喵星人今天来了")
    _lexicon(tmp_path, "喵星人", "meow")
    result = media._burn_preview_subtitles(
        {
            "status": "MATERIALIZED",
            "media_path": str(tmp_path / "synthetic.mp4"),
            "subtitle_path": str(srt),
        },
        run_ffmpeg=False,
    )
    assert result["burned_preview"]["status"] == "DRY_RUN"
    ass = Path(result["burned_preview"]["ass_path"])
    assert _events(ass)[0][9] == "喵星人今天来了" and not _audit(srt, ass).issues


def test_auditor_still_detects_rewritten_ass_even_with_rebound_hashes(tmp_path, monkeypatch):
    monkeypatch.delenv("VTUBER_SLICE_TERM_LEXICON", raising=False)
    srt = _input(tmp_path, "喵星人今天来了")
    ass = tmp_path / "final.ass"
    render._write_sapphire_ass_from_srt(srt, ass)
    assert not _audit(srt, ass).issues
    ass.write_text(ass.read_text().replace("喵星人", "meow"))
    assert _audit(srt, ass).issues
    assert "SUBTITLE_ASS_HASH_MISMATCH" in {x.code for x in _audit(srt, ass, bad_hash=True).issues}


def test_trusted_prebuilt_ass_is_not_regenerated_by_glossary_change(tmp_path, monkeypatch):
    monkeypatch.delenv("VTUBER_SLICE_TERM_LEXICON", raising=False)
    srt = _input(tmp_path, "喵星人今天来了")
    ass = tmp_path / "existing.ass"
    render._write_sapphire_ass_from_srt(srt, ass)
    before = ass.read_bytes()
    _lexicon(tmp_path, "喵星人", "meow")
    result = media._burn_preview_subtitles(
        {
            "status": "MATERIALIZED",
            "media_path": str(tmp_path / "synthetic.mp4"),
            "subtitle_path": str(srt),
            "subtitle_ass_path": str(ass),
            "artifact_hashes": {"ass_sha256": "sha256:" + _sha(ass)},
        },
        run_ffmpeg=False,
    )
    assert result["burned_preview"]["status"] == "DRY_RUN" and ass.read_bytes() == before
    assert not _audit(srt, ass).issues


def test_unmodified_lexicon_normalizer_keeps_its_draft_contract(tmp_path):
    lex = term_lexicon.load_term_lexicon(_lexicon(tmp_path, "喵星人", "meow"))
    assert term_lexicon.normalize_text("喵星人今天来了", lexicon=lex) == "meow今天来了"
