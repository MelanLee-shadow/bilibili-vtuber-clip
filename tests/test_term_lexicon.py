import json
from pathlib import Path

from src.autoslice.source_context_executor import _parse_srt as parse_source_context_srt
from src.autoslice.term_lexicon import load_term_lexicon, normalize_text


def test_load_term_lexicon_extracts_seed_terms_from_manual_edited_srt(tmp_path):
    source_srt = tmp_path / "sample.manual-edited.zh.srt"
    source_srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nkmx, kmx，你看有花\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nkmx被骗到了\n",
        encoding="utf-8",
    )
    lexicon_path = tmp_path / "term_lexicon.json"
    lexicon_path.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-term-lexicon.v1",
                "sources": [{"path": str(source_srt)}],
                "overrides": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    lexicon = load_term_lexicon(lexicon_path)

    assert "kmx" in lexicon.seed_terms
    assert lexicon.seed_terms["kmx"].count == 3
    assert str(source_srt) in lexicon.seed_terms["kmx"].sources


def test_normalize_text_treats_kimo_as_developer_alias_not_display_text(tmp_path):
    lexicon_path = tmp_path / "term_lexicon.json"
    lexicon_path.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-term-lexicon.v1",
                "sources": [],
                "overrides": [
                    {
                        "canonical": "kmx",
                        "display": "kmx",
                        "aliases": ["天不熊", "kimo熊"],
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    lexicon = load_term_lexicon(lexicon_path)

    assert normalize_text("天不熊被骗到了", lexicon=lexicon) == "kmx被骗到了"
    assert normalize_text("kimo熊被骗到了", lexicon=lexicon) == "kmx被骗到了"
    assert normalize_text("天不熊被骗到了", lexicon=lexicon, variant="display") == "kmx被骗到了"


def test_normalize_text_handles_phrase_alias_from_ivan_edited_subtitle(tmp_path):
    lexicon_path = tmp_path / "term_lexicon.json"
    lexicon_path.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-term-lexicon.v1",
                "sources": [],
                "overrides": [
                    {
                        "canonical": "kmx, kmx，",
                        "display": "kmx, kmx，",
                        "aliases": ["给我小给我小"],
                    },
                    {
                        "canonical": "kmx",
                        "display": "kmx",
                        "aliases": ["给我小", "天不熊", "kimo熊"],
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    lexicon = load_term_lexicon(lexicon_path)

    assert normalize_text("给我小给我小你看有花", lexicon=lexicon) == "kmx, kmx，你看有花"
    assert normalize_text("给我小被骗到了", lexicon=lexicon) == "kmx被骗到了"


def test_parse_srt_applies_discovered_term_lexicon(tmp_path):
    repo_root = tmp_path / "repo"
    source_srt = repo_root / "lidousha" / "2026-06-19" / "sample.srt"
    source_srt.parent.mkdir(parents=True, exist_ok=True)
    source_srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n天不熊被骗到了\n",
        encoding="utf-8",
    )
    lexicon_path = repo_root / "lidousha" / "term_lexicon.json"
    lexicon_path.write_text(
        json.dumps(
            {
                "schema_version": "lidousha-term-lexicon.v1",
                "sources": [],
                "overrides": [
                    {
                        "canonical": "kmx",
                        "display": "kmx",
                        "aliases": ["天不熊", "kimo熊"],
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    cues = parse_source_context_srt(source_srt)

    assert [cue.text for cue in cues] == ["kmx被骗到了"]
