from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.apply_speaker_turn_overrides import Cue, write_srt
from src.autoslice.reviewed_text_only_speaker_successor import (
    ReviewedTextOnlySpeakerSuccessorError,
    materialize_text_only_speaker_successor,
)
from src.autoslice.speaker_common import SPEAKER_FINALIZATION_SCHEMA


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _plain_srt(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.write_text("\n\n".join(
        f"{index}\n{start} --> {end}\n{text}"
        for index, (start, end, text) in enumerate(rows, 1)
    ) + "\n", encoding="utf-8")


def _bundle(tmp_path: Path) -> dict[str, object]:
    media = tmp_path / "recut.mp4"; media.write_bytes(b"same-media")
    old_plain, new_plain, old_speaker = (tmp_path / "old.srt", tmp_path / "new.srt", tmp_path / "old.speaker.srt")
    plain = [Cue(1, "00:00:00,000", "00:00:01,000", "李豆沙", "旧文本", "old"), Cue(2, "00:00:01,000", "00:00:02,000", "李豆沙", "不变", "old")]
    _plain_srt(old_plain, [(cue.start, cue.end, cue.text) for cue in plain])
    _plain_srt(new_plain, [(plain[0].start, plain[0].end, plain[0].text), (plain[1].start, plain[1].end, "新文本")])
    write_srt(plain, old_speaker)
    ledger = tmp_path / "ledger.json"
    _write(ledger, {"candidate_id":"cid", "pipeline_srt_sha256": _sha(old_plain), "cue_decisions":[
        {"cue":1,"disposition":"OPERATOR_UNCHANGED_FREEZE"},
        {"cue":2,"disposition":"OPERATOR_EXACT_TEXT","decision_authority":"LEDGER_OPERATOR_AUTHORITY","release_text":"新文本"},
    ]})
    manifest = tmp_path / "old.speaker.json"
    decisions = [{"source_index":i, "start":cue.start, "end":cue.end, "speaker":"李豆沙", "text":cue.text, "decision_source":"old", "authority":None, "note":None, "speaker_detail":None, "layer":0, "placement":"main"} for i,cue in enumerate(plain,1)]
    _write(manifest, {"schema_version":SPEAKER_FINALIZATION_SCHEMA,"status":"READY","production_ready":True,"text_final_srt":str(old_plain),"text_final_srt_sha256":_sha(old_plain).removeprefix("sha256:"),"output_review_srt":str(old_speaker),"output_review_srt_sha256":_sha(old_speaker).removeprefix("sha256:"),"source_media_sha256":_sha(media),"source_cue_count":2,"output_cue_count":2,"final_decisions":decisions})
    record = {"speaker_mode":"auto","speaker_finalization":json.loads(manifest.read_text()),"speaker_finalization_manifest_sha256":_sha(manifest)}
    return locals()


def test_successor_only_rebinds_ledger_named_text_and_keeps_labels(tmp_path: Path) -> None:
    b = _bundle(tmp_path)
    result = materialize_text_only_speaker_successor(candidate_id="cid", old_record=b["record"], old_record_sha256="sha256:"+"1"*64, old_manifest_path=b["manifest"], old_manifest_sha256=_sha(b["manifest"]), reviewed_baseline_path=b["new_plain"], reviewed_baseline_sha256=_sha(b["new_plain"]), ledger_path=b["ledger"], ledger_sha256=_sha(b["ledger"]), new_plain_srt=b["new_plain"], new_media=b["media"], expected_media_sha256=_sha(b["media"]), output_srt=tmp_path/"new.speaker.srt", output_ass=tmp_path/"new.speaker.ass", output_manifest=tmp_path/"new.speaker.json")
    assert result["status"] == "READY"
    assert result["reviewed_baseline_text_only_successor"]["speaker_label_mutation_authorized"] is False
    assert result["final_decisions"][0]["text"] == "旧文本"
    assert result["final_decisions"][1]["text"] == "新文本"
    assert "[李豆沙] 新文本" in (tmp_path/"new.speaker.srt").read_text()


def test_successor_rejects_text_change_not_named_by_ledger(tmp_path: Path) -> None:
    b = _bundle(tmp_path)
    b["new_plain"].write_text(b["new_plain"].read_text().replace("旧文本", "越权"), encoding="utf-8")
    with pytest.raises(ReviewedTextOnlySpeakerSuccessorError, match="TEXT_DELTA_OUTSIDE_LEDGER"):
        materialize_text_only_speaker_successor(candidate_id="cid", old_record=b["record"], old_record_sha256="sha256:"+"1"*64, old_manifest_path=b["manifest"], old_manifest_sha256=_sha(b["manifest"]), reviewed_baseline_path=b["new_plain"], reviewed_baseline_sha256=_sha(b["new_plain"]), ledger_path=b["ledger"], ledger_sha256=_sha(b["ledger"]), new_plain_srt=b["new_plain"], new_media=b["media"], expected_media_sha256=_sha(b["media"]), output_srt=tmp_path/"new.speaker.srt", output_ass=tmp_path/"new.speaker.ass", output_manifest=tmp_path/"new.speaker.json")
