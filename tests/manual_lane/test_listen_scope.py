"""Listening records are scoped to the pass that asked for them (TASK-5, coordinator's defect report).

The remote all.json holds the whole clip directory: other models, other windows, an old thinking tier.  A pass must
return only its own (window, model) records; each caller accumulates; C's reading at level n is the records of levels
1..n for the models actually requested on that window; A reads Gemini 3.8 and argon only (U122: 3.6 / 3.7 removed).
"""

from __future__ import annotations

import json

from fakes import FakeRemote, make_ctx
from test_variant_c import RD, CpaFake, _ctx, _rec, _suspicious

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult


def _polluted_remote(tmp_path, everything):
    """A remote whose all.json holds the whole clip directory, whatever pass is asked."""
    scripts: list[str] = []

    def script(name, body, remote):
        if name.startswith("s3-listen-"):
            scripts.append(name)
            remote.files[f"{RD}/listen/out/all.json"] = json.dumps(everything, ensure_ascii=False).encode()
            return RemoteResult(0, 'LISTEN_DONE 1 records 1 {"OK": 1}\n', "")
        return RemoteResult(0, "", "")
    return FakeRemote(on_script=script), scripts


def test_listen_pass_returns_only_the_records_of_its_own_windows_and_models(tmp_path):
    def rec(win, model, slug):
        return {"win": win, "model": model, "slug": slug, "status": "OK", "lines": [{"start_s": 0.0, "text": "x"}]}
    everything = [
        rec("C000", si.M38["name"], "38m"),              # requested: window C000, model 3.8 Low
        rec("C000", si.ARGON["name"], "argon"),          # same window, another model: not this pass
        rec("C005", si.M38["name"], "38m"),              # another window
        rec("C000", "Gemini 3.8 Flash (High)", "38m"),   # an old thinking tier with the same slug
        rec("W00", si.M38["name"], "38m"),               # an old grouped window
    ]
    remote, _ = _polluted_remote(tmp_path, everything)
    ctx = make_ctx(tmp_path, remote=remote, cpa=CpaFake({}))
    recs = si._listen_pass(ctx, "L2", [{"win": "C000"}], [si.M38])
    assert [(r["win"], r["model"]) for r in recs] == [("C000", si.M38["name"])]
    assert ctx.remote_dir == RD


def test_c_settled_at_level_one_reads_only_3_8_and_asr_not_the_other_models(tmp_path, monkeypatch):
    scripts: list[str] = []
    everything = [_rec("C000", "38m", "句子甲"), _rec("C000", "argon", "句子丁")]
    ctx, _ = _ctx(tmp_path, monkeypatch, review=_suspicious(1), records_by_label={"C-L1": everything},
                  scripts=scripts,
                  rows={"adjudicate-c-L1-1": [{"id": "d001", "state": "ADOPT", "final_text": "句子甲",
                                               "confidence": "高", "affects_meaning": True, "reason_key": "同音",
                                               "basis": "3.8"}]})
    assert si.step3_subtitles(ctx).ok
    doubts = json.loads((ctx.clip_dir / "subs" / "doubts.json").read_text(encoding="utf-8"))
    readings = next(d for d in doubts if d["cue_id"] == 1)["readings"]
    assert "audio:38m@P0" in readings
    assert "audio:argon@P0" not in readings


def test_c_escalated_to_level_two_reads_level_one_3_8_together_with_level_two_argon(tmp_path, monkeypatch):
    scripts: list[str] = []
    everything = [_rec("C000", "38m", "句子甲"), _rec("C000", "argon", "句子丙")]
    unsettled = {"id": "d001", "state": "UNRESOLVED", "final_text": "", "affects_meaning": True, "reason_key": "数字"}
    ctx, _ = _ctx(tmp_path, monkeypatch, review=_suspicious(1),
                  records_by_label={"C-L1": everything, "C-L2": everything}, scripts=scripts,
                  rows={"adjudicate-c-L1-1": [unsettled],
                        "adjudicate-c-L2-1": [{"id": "d001", "state": "ADOPT", "final_text": "句子丙",
                                               "confidence": "高", "affects_meaning": True, "reason_key": "数字",
                                               "basis": "两级"}]})
    assert si.step3_subtitles(ctx).ok
    assert scripts == ["C-L1", "C-L2"]
    doubts = json.loads((ctx.clip_dir / "subs" / "doubts.json").read_text(encoding="utf-8"))
    readings = next(d for d in doubts if d["cue_id"] == 1)["readings"]
    assert {"audio:38m@P0", "audio:argon@P0"} <= set(readings)


def test_mode_a_reads_3_8_and_argon_as_its_witnesses(tmp_path, monkeypatch):
    from test_steps import _subs_ctx

    monkeypatch.setenv("MANUAL_LANE_ADJUDICATE_MODE", "A")
    four = [_rec("C000", "38m", "句子甲"), _rec("C000", "argon", "句子甲")]
    ctx, _ = _subs_ctx(tmp_path, four, [{"id": "d001", "state": "KEEP", "final_text": "", "confidence": "高",
                                         "affects_meaning": False}])
    out = si.step3_subtitles(ctx)
    assert out.ok, out.note
    readings = next(d for d in json.loads((ctx.clip_dir / "subs/doubts.json").read_text(encoding="utf-8"))
                    if d["cue_id"] == 1)["readings"]
    assert {k for k in readings if k.startswith("audio:")} == {"audio:38m@P0", "audio:argon@P0"}
