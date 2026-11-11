"""Prepared truth identity reaches ordinary scoring without parsing opaque cue IDs."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.autoslice.manual_lane import score


REPO = Path(score.__file__).resolve().parents[3]


def _label(text="alpha", **extra):
    return {"text": text, "speaker": "Guest", "abs_start_ms": 0, "abs_end_ms": 1000, **extra}


def _truth(tmp_path, labels, *, meta=None, name="truth.json"):
    doc = {"labels": labels}
    if meta is not None:
        doc["meta"] = meta
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _clip(task, cid):
    root = task / "clips" / cid
    (root / "boundary").mkdir(parents=True)
    (root / "subs").mkdir()
    (root / "boundary/decision.json").write_text('{"adopted":{"start_ms":0}}', encoding="utf-8")
    (root / "subs/final.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nalpha\n", encoding="utf-8")


def _ordinary_score(task, path):
    output = task / "score.json"
    proc = subprocess.run([sys.executable, "-B", str(REPO / "scripts/manual_lane.py"), "score",
                           "--task-dir", str(task), "--truth", str(path), "--out", str(output)],
                          cwd=REPO, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(output.read_text(encoding="utf-8"))


def test_record_identity_preserves_split_children_through_ordinary_cli(tmp_path):
    cid = "clip-with-dashes"
    _clip(tmp_path, cid)
    path = _truth(tmp_path, {"opaque-1-child-a": _label(cid=cid),
                             "opaque-1-child-b": _label(cid=cid)})
    result = _ordinary_score(tmp_path, path)
    assert [r["cid"] for r in result] == [cid]
    assert result[0]["summary"]["n"] == 2


def test_document_identity_supports_opaque_ids_through_ordinary_cli(tmp_path):
    cid = "document-clip"
    _clip(tmp_path, cid)
    path = _truth(tmp_path, {"opaqueA": _label(), "opaqueB": _label()}, meta={"cid": cid})
    result = _ordinary_score(tmp_path, path)
    assert [r["cid"] for r in result] == [cid]
    assert result[0]["summary"]["n"] == 2


def test_direct_scoring_uses_explicit_identity_without_prefix_guessing(tmp_path):
    cid = "another-clip"
    _clip(tmp_path, cid)
    path = _truth(tmp_path, {"unrelated-token-child": _label(cid=cid)})
    result = score.score_clip(cid, tmp_path / "clips", score.load_truth([path]))
    assert result["summary"]["n"] == 1
    assert result["rows"][0]["id"] == "unrelated-token-child"


def test_legacy_identity_and_note_metadata_are_unchanged(tmp_path):
    cid = "legacy-clip"
    _clip(tmp_path, cid)
    original = _label(note="metadata only")
    path = _truth(tmp_path, {cid + "-1000": original}, meta={"comment": "irrelevant"})
    assert score.load_truth([path])[cid + "-1000"] == {**original, "id": cid + "-1000"}
    assert _ordinary_score(tmp_path, path)[0]["summary"]["n"] == 1


def test_consistent_record_and_document_identity_is_accepted(tmp_path):
    path = _truth(tmp_path, {"opaque": _label(cid="same")}, meta={"cid": "same"})
    assert score.load_truth([path])["opaque"]["cid"] == "same"


@pytest.mark.parametrize("value", [None, "", 7, True, [], {}])
@pytest.mark.parametrize("where", ["record", "document"])
def test_explicit_identity_is_not_coerced_or_silently_ignored(tmp_path, value, where):
    if where == "record":
        path = _truth(tmp_path, {"legacy-1": _label(cid=value)})
    else:
        path = _truth(tmp_path, {"legacy-1": _label()}, meta={"cid": value})
    with pytest.raises(ValueError, match="nonempty string"):
        score.load_truth([path])


def test_conflicting_record_and_document_identity_is_rejected(tmp_path):
    path = _truth(tmp_path, {"legacy-1": _label(cid="one")}, meta={"cid": "two"})
    with pytest.raises(ValueError, match="conflicting truth cid"):
        score.load_truth([path])


def test_duplicate_id_rejection_is_preserved_across_explicit_owners(tmp_path):
    first = _truth(tmp_path, {"same": _label(cid="one")}, name="first.json")
    second = _truth(tmp_path, {"same": _label(cid="two")}, name="second.json")
    with pytest.raises(ValueError, match="duplicate truth id"):
        score.load_truth([first, second])
