"""Scoped evidence survives authority -> generated JSON -> ordinary CPA prompt."""
from __future__ import annotations

import json
from pathlib import Path

from src.autoslice.manual_lane import step3_adjudicate as adjudicate

ROOT = Path(__file__).resolve().parents[2]


def _render(tmp_path, monkeypatch, rows):
    path = tmp_path / "pairs.json"
    # The old reader accepts a list; keep that representation for the old-code
    # guard, so failure is its unsafe rendering, not a missing import/schema.
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    names = tmp_path / "names.json"
    names.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(adjudicate, "MISHEARING_PAIRS", path)
    monkeypatch.setattr(adjudicate, "PROPER_NOUNS", names)
    return adjudicate._confusion_lines()


def _row(kind="recording_interval"):
    return {"misheard": "甲词", "standard": "乙词", "conflicts": [], "evidence": [{
        "scope": {"kind": kind, "description": "虚构作品语境", "recording_basename": "synthetic.wav",
                  "source_start_ms": 100, "source_end_ms": 200, "truth_id": "synthetic-1"},
        "authority": "合成裁定",
    }]}


def test_single_sentence_scope_is_not_an_unconditional_replacement(tmp_path, monkeypatch):
    text = _render(tmp_path, monkeypatch, [_row()])
    assert "甲词 → 乙词" not in text
    assert "synthetic.wav" in text and "100–200 ms" in text
    assert "『乙词』被误听/误写为『甲词』" in text
    assert "不是替换规则，按本句音频和语境判断" in text


def test_alias_collision_is_confusable_not_arrow(tmp_path, monkeypatch):
    row = _row()
    row["conflicts"] = [{"entity": "丙实体"}]
    text = _render(tmp_path, monkeypatch, [row])
    assert "→" not in text
    assert "两个实体易混" in text and "丙实体" in text


def test_global_hint_is_unchanged(tmp_path, monkeypatch):
    assert _render(tmp_path, monkeypatch, [_row("global")]) == "- 甲词 → 乙词"


def test_generated_json_cannot_drift():
    from src.autoslice.manual_lane import mishearing

    assert json.loads(mishearing.OUTPUT.read_text(encoding="utf-8")) == mishearing.generated_document()


def test_new_authority_member_enters_both_consumers_without_registration(tmp_path, monkeypatch):
    from src.autoslice.manual_lane import mishearing, suspicion

    sources = {
        "entities": {"groups": [{"reason": "虚构作品语境", "entities": [
            {"canonical": "乙词", "surfaces": ["甲词"]}]}]},
        "ledger": {"entries": [{"truth_id": "new-1", "recording_basename": "synthetic.wav",
            "source_start_ms": 100, "source_end_ms": 200, "authority": "合成裁定",
            "replacements": [{"surface": "甲词", "canonical": "乙词"}]}]},
        "memory": {"entries": []},
        "roster": {"member_overrides": {"丙": {"canonical": "丙实体", "aliases": ["甲词"]}}},
        "names": [],
    }
    for key, relative in mishearing.SOURCES.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sources[key], ensure_ascii=False), encoding="utf-8")
    rows = mishearing.derive_pairs(tmp_path)
    assert len(rows) == 1 and len(rows[0]["evidence"]) == 2
    assert rows[0]["evidence"][0]["scope"]["kind"] == "context"
    assert rows[0]["evidence"][1]["scope"]["source_start_ms"] == 100
    assert suspicion.lexicon_hits("今天说甲词", rows) == ["甲词"]
    assert "两个实体易混" in _render(tmp_path, monkeypatch, rows)


def test_derivation_preserves_each_interval_and_explicit_global_authority(tmp_path):
    from src.autoslice.manual_lane import mishearing

    for key, relative in mishearing.SOURCES.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        empty = [] if key == "names" else {"groups": []} if key == "entities" else {"entries": []}
        path.write_text(json.dumps(empty), encoding="utf-8")
    entities = {"groups": [{"reason": "合成全局授权", "scope": {"kind": "global"}, "entities": [
        {"canonical": "乙词", "surfaces": ["甲词"]}]}]}
    ledger = {"entries": [{"truth_id": f"span-{i}", "recording_basename": "synthetic.wav",
        "source_start_ms": i * 100, "source_end_ms": i * 100 + 50, "authority": "合成局部授权",
        "replacements": [{"surface": "丁词", "canonical": "戊词"}]} for i in (1, 2)]}
    (tmp_path / mishearing.SOURCES["entities"]).write_text(json.dumps(entities), encoding="utf-8")
    (tmp_path / mishearing.SOURCES["ledger"]).write_text(json.dumps(ledger), encoding="utf-8")
    rows = mishearing.derive_pairs(tmp_path)
    text = mishearing.render_lines(rows)
    assert "- 甲词 → 乙词" in text
    assert "丁词 → 戊词" not in text
    assert "span-1" in text and "span-2" in text
    assert text.count("『戊词』被误听/误写为『丁词』") == 2
