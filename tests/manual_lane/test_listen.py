"""Listening queue (listen-queue), label import (listen-import), anchors and hash scoping.  All fake: no ssh, no
ffmpeg, no CPA, and no audio is ever played (the page is checked statically)."""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from fakes import FakeRemote

from src.autoslice.manual_lane import cli
from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.config import load_session_config
from src.autoslice.manual_lane.listen import (
    ListenError,
    build_items,
    clip_queue,
    import_labels,
    parse_export,
    render_page,
    write_queue,
)
from src.autoslice.manual_lane.state import ClipState, sha256_file

CUT_LEN = 100.0


def _cues(texts: list[str], step: float = 10.0) -> list[dict]:
    return [{"id": i + 1, "start_s": i * step + 1.0, "end_s": i * step + 3.0, "text": t}
            for i, t in enumerate(texts)]


def _doubt(cue_id: int, *, state="UNRESOLVED", affects=True, current="", readings=None, final_text=None):
    return {"id": f"d{cue_id:03d}", "cue_id": cue_id, "current": current or f"现稿{cue_id}",
            "readings": readings if readings is not None else {"bcut": f"现稿{cue_id}", "audio:38m@P0": "别的读法"},
            "differing": ["audio:38m@P0"], "state": state, "final_text": final_text, "affects_meaning": affects,
            "reason_key": "专名", "danmaku": ["弹幕甲"], "cpa_call": "s3-adjudicate-1"}


# ----- queue builder ------------------------------------------------------------------------
def test_queue_keeps_only_unresolved_affecting_doubts_and_builds_context():
    texts = [f"字{i}" for i in range(1, 9)]
    bcut = _cues(texts)
    final = _cues(texts)
    final[4]["text"] = "字5已改"
    doubts = [_doubt(5), _doubt(2, state="KEEP"), _doubt(6, affects=False), _doubt(8, state="ADOPT", final_text="字8")]
    items = build_items(cid="c1", doubts=doubts, bcut_cues=bcut, final_cues=final, cut_len=CUT_LEN)
    assert [it["doubt_id"] for it in items] == ["d005"]
    item = items[0]
    assert item["key"] == "c1:d005" and item["cue_id"] == 5
    assert [c["cue_id"] for c in item["context"]] == [2, 3, 4, 5, 6, 7, 8]
    assert [c["target"] for c in item["context"]].count(True) == 1
    assert next(c for c in item["context"] if c["target"])["text"] == "字5已改"
    assert item["audio_start_s"] == round(max(0.0, bcut[4]["start_s"] - 6.0), 3)
    assert item["audio_dur_s"] == round(min(CUT_LEN, bcut[4]["end_s"] + 6.0) - item["audio_start_s"], 3)
    assert item["audio_file"] == "c1_d005.mp3"


def test_queue_audio_window_is_clamped_to_the_cut():
    bcut = _cues(["甲", "乙"])
    items = build_items(cid="c1", doubts=[_doubt(1)], bcut_cues=bcut, final_cues=bcut, cut_len=8.0)
    assert items[0]["audio_start_s"] == 0.0
    assert items[0]["audio_start_s"] + items[0]["audio_dur_s"] <= 8.0


def test_candidates_put_current_first_and_merge_equal_texts():
    readings = {"bcut": "现稿X", "audio:38m@P0": "读法甲", "audio:argon@P0": "读法甲", "asr:moss": "读法乙"}
    bcut = _cues(["现稿X", "后"])
    items = build_items(cid="c1", doubts=[_doubt(1, current="现稿X", readings=readings, final_text="读法乙")],
                        bcut_cues=bcut, final_cues=bcut, cut_len=CUT_LEN)
    cands = items[0]["candidates"]
    assert [c["text"] for c in cands] == ["现稿X", "读法乙", "读法甲"]
    assert cands[2]["sources"] == ["audio:38m@P0", "audio:argon@P0"]
    assert cands[0]["sources"][0] == "现稿"


# ----- clip plan, audio and page --------------------------------------------------------------
def _clip_on_disk(tmp_path, cid: str, doubts: list[dict], *, texts: list[str]) -> tuple[dict, object]:
    clip_dir = tmp_path / "clips" / cid
    (clip_dir / "subs").mkdir(parents=True)
    (clip_dir / "asr").mkdir(parents=True)
    bcut = _cues(texts)
    (clip_dir / "asr" / "bcut.srt").write_text(si.format_srt(bcut), encoding="utf-8")
    (clip_dir / "subs" / "final.srt").write_text(si.format_srt(bcut), encoding="utf-8")
    (clip_dir / "subs" / "doubts.json").write_text(json.dumps(doubts, ensure_ascii=False), encoding="utf-8")
    (clip_dir / "cut").mkdir(parents=True)
    (clip_dir / "cut" / "probe.json").write_text("{}", encoding="utf-8")
    state_data = {"clip": {"cid": cid, "start_ms": 0, "end_ms": int(CUT_LEN * 1000), "remote_dir": f"/r/{cid}"}}
    return state_data, clip_dir


def test_clip_queue_and_write_queue_cut_audio_on_oci3_and_write_static_page(tmp_path):
    texts = [f"句{i}" for i in range(1, 7)]
    doubts_a = [_doubt(3, current="句3"), _doubt(4, state="KEEP")]
    doubts_b = [_doubt(2, current="句2")]
    plans = []
    for cid, doubts in (("c_a", doubts_a), ("c_b", doubts_b)):
        state_data, clip_dir = _clip_on_disk(tmp_path, cid, doubts, texts=texts)
        plan = clip_queue(cid=cid, state_data=state_data, clip_dir=clip_dir)
        assert plan is not None
        plans.append(plan)
    assert len(plans[0]["items"]) == 1 and len(plans[1]["items"]) == 1

    def on_script(name, body, remote):
        for out in re.findall(r"-b:a \S+ (\S+\.mp3)", body):
            remote.files[out] = b"ID3-fake"
        return type("R", (), {"rc": 0, "stdout": "AUDIO_DONE\n", "stderr": ""})()

    remotes: dict[str, FakeRemote] = {}

    def remote_for(plan):
        return remotes.setdefault(plan["cid"], FakeRemote(on_script=on_script))

    out_dir = tmp_path / "listen-queue"
    summary = write_queue(out_dir, plans, remote_for=remote_for, generated_at="2026-10-31T12:00:00Z")
    assert summary["items"] == 2 and summary["per_clip"] == {"c_a": 1, "c_b": 1}
    # the cut is read on OCI3 by ffmpeg, once per clip; the page only references the fetched mp3
    body = remotes["c_a"].scripts[0][1]
    assert "-i /r/c_a/cut/cut.mp4" in body and "/r/c_a/listen-queue/c_a_d003.mp3" in body
    assert (out_dir / "audio" / "c_a_d003.mp3").read_bytes() == b"ID3-fake"
    page = (out_dir / "index.html").read_text(encoding="utf-8")
    assert page.count('<audio controls muted preload="none" src="audio/') == 2


def test_clip_without_a_cut_product_is_skipped(tmp_path):
    state_data = {"clip": {"cid": "c_none", "start_ms": 0, "end_ms": 1000, "remote_dir": "/r/c_none"}}
    assert clip_queue(cid="c_none", state_data=state_data, clip_dir=tmp_path / "missing") is None


def test_page_is_static_muted_and_never_autoplays(tmp_path):
    texts = ["甲", "乙", "丙"]
    bcut = _cues(texts)
    evil = "</script><script>window.bad=1</script>"
    items = build_items(cid="c1", doubts=[_doubt(2, current="乙", readings={"bcut": "乙", "audio:38m@P0": evil})],
                        bcut_cues=bcut, final_cues=bcut, cut_len=CUT_LEN)
    page = render_page(items, generated_at="t")
    assert "autoplay" not in page.lower()
    assert ".play(" not in page and "new Audio" not in page
    assert page.count("<audio ") == 1 and page.count("muted") >= 1
    assert "</script><script>" not in page            # candidate text is escaped, not injected
    assert "&lt;/script&gt;" in page
    assert 'data-version="listen-queue-v1"' in page and "cues_version" in page
    assert 'name="c1"' in page and 'value="custom"' in page


# ----- export validation and import ------------------------------------------------------------
def _export(labels: dict, version: str = "listen-queue-v1") -> dict:
    return {"labels": labels, "exported_at": "2026-10-31T12:00:00Z", "cues_version": version}


def _row(cid="c1", doubt="d003", current="句3", text="句三", choice="custom") -> dict:
    # source-segment absolute anchor: the clip starts at 0 in these fixtures, so cue 3 is 21.0-23.0 s
    return {"cid": cid, "doubt_id": doubt, "abs_start_ms": 21000, "abs_end_ms": 23000,
            "current": current, "text": text, "choice": choice, "note": ""}


def test_parse_export_accepts_valid_and_rejects_malformed():
    rows = parse_export(_export({"c1:d003": _row()}))
    assert rows[0]["text"] == "句三" and rows[0]["choice"] == "custom"
    bad_cases = {
        "wrong version": _export({"c1:d003": _row()}, version="v2"),
        "key mismatch": _export({"c1:d009": _row()}),
        "empty text": _export({"c1:d003": _row(text="  ")}),
        "bad choice": _export({"c1:d003": dict(_row(), choice="keep")}),
        "no abs anchor": _export({"c1:d003": {k: v for k, v in _row().items() if k != "abs_start_ms"}}),
        "end before start": _export({"c1:d003": dict(_row(), abs_end_ms=20000)}),
    }
    for obj in bad_cases.values():
        with pytest.raises(ListenError):
            parse_export(obj)


def _task_with_clip(tmp_path, cid: str, doubts: list[dict]):
    task = tmp_path / "task"
    state = ClipState.create(task / "state" / f"{cid}.json",
                             clip={"cid": cid, "remote_dir": f"/r/{cid}", "start_ms": 0, "end_ms": 60000},
                             session={"speaker_mode": "uniform_host"})
    subs = task / "clips" / cid / "subs"
    subs.mkdir(parents=True)
    (subs / "doubts.json").write_text(json.dumps(doubts, ensure_ascii=False), encoding="utf-8")
    asr = task / "clips" / cid / "asr"
    asr.mkdir(parents=True)
    (asr / "bcut.srt").write_text(si.format_srt(_cues([f"句{i}" for i in range(1, 7)])), encoding="utf-8")
    return task, state


def test_import_stores_labels_with_export_hash_and_refuses_anchor_drift(tmp_path):
    task, state = _task_with_clip(tmp_path, "c1", [_doubt(3, current="句3")])
    good = tmp_path / "ok.json"
    good.write_text(json.dumps(_export({"c1:d003": _row()}), ensure_ascii=False), encoding="utf-8")
    summary = import_labels(task, good)
    assert summary["labels"] == 1 and summary["clips"] == {"c1": 1}
    stored = ClipState.load(task / "state" / "c1.json").data["reviewer_labels"]
    assert stored["source"] == "REVIEWER_LISTENED" and stored["export_sha256"] == sha256_file(good)
    assert stored["labels"][0]["text"] == "句三"

    drift = tmp_path / "drift.json"
    drift.write_text(json.dumps(_export({"c1:d003": _row(current="句3已改")}), ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ListenError):
        import_labels(task, drift)
    assert ClipState.load(task / "state" / "c1.json").data["reviewer_labels"]["export_sha256"] == sha256_file(good)


def test_import_is_all_or_nothing_across_clips(tmp_path):
    task, _ = _task_with_clip(tmp_path, "c1", [_doubt(3, current="句3")])
    ClipState.create(task / "state" / "c2.json", clip={"cid": "c2", "remote_dir": "/r/c2"},
                     session={"speaker_mode": "uniform_host"})
    mixed = tmp_path / "mixed.json"
    mixed.write_text(json.dumps(_export({"c1:d003": _row(), "c2:d003": _row(cid="c2")}), ensure_ascii=False),
                     encoding="utf-8")
    with pytest.raises(ListenError):  # c2 has no doubts.json
        import_labels(task, mixed)
    assert ClipState.load(task / "state" / "c1.json").data["reviewer_labels"] == {}


def test_cli_registers_listen_commands_and_import_runs_end_to_end(tmp_path, capsys):
    args = cli.build_parser().parse_args(["listen-queue", "--task-dir", "t"])
    assert args.func is cli.cmd_listen_queue
    args = cli.build_parser().parse_args(["listen-import", "--task-dir", "t", "--export", "e.json"])
    assert args.func is cli.cmd_listen_import
    task, _ = _task_with_clip(tmp_path, "c1", [_doubt(3, current="句3")])
    export = tmp_path / "e.json"
    export.write_text(json.dumps(_export({"c1:d003": _row()}), ensure_ascii=False), encoding="utf-8")
    assert cli.main(["listen-import", "--task-dir", str(task), "--export", str(export)]) == 0
    assert json.loads(capsys.readouterr().out)["labels"] == 1
    assert cli.main(["listen-import", "--task-dir", str(task), "--export", str(tmp_path / "nope.json")]) == 2


def test_session_config_refuses_reviewer_labels_in_config(tmp_path):
    cfg = tmp_path / "session.json"
    cfg.write_text(json.dumps({"recording_date": "2026-10-24", "participants": [["李豆沙"]],
                               "own_uploader_mids": [1], "reviewer_labels": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_session_config(cfg)


def test_generated_remote_bodies_are_valid_bash(tmp_path):
    texts = ["甲", "乙", "丙"]
    state_data, clip_dir = _clip_on_disk(tmp_path, "c_b", [_doubt(2, current="乙")], texts=texts)
    plan = clip_queue(cid="c_b", state_data=state_data, clip_dir=clip_dir)
    remote = FakeRemote(on_script=lambda name, body, r: type("R", (), {"rc": 0, "stdout": "AUDIO_DONE", "stderr": ""})())
    for it in plan["items"]:
        remote.files[f"/r/c_b/listen-queue/{it['audio_file']}"] = b"x"
    from src.autoslice.manual_lane.listen import cut_audio
    cut_audio(remote, plan, tmp_path / "a")
    body = remote.scripts[0][1]
    assert subprocess.run(["bash", "-n"], input=body.encode(), capture_output=True, check=False).returncode == 0


def test_queue_builds_when_the_final_subtitles_have_more_cues_than_bcut():
    # generation splits long cues, so final.srt can have more cues than the ASR cues; the context text is matched by time
    bcut = _cues(["甲", "乙长句", "丙"])
    final = [{"start_s": 1.0, "end_s": 2.0, "text": "甲"},
             {"start_s": 11.0, "end_s": 12.0, "text": "乙长"},
             {"start_s": 12.0, "end_s": 13.0, "text": "句"},
             {"start_s": 21.0, "end_s": 23.0, "text": "丙"}]
    doubt = _doubt(2, current="乙长句")
    items = build_items(cid="c1", doubts=[doubt], bcut_cues=bcut, final_cues=final, cut_len=CUT_LEN)
    assert len(items) == 1
    ctx_text = {c["cue_id"]: c["text"] for c in items[0]["context"]}
    assert ctx_text[2] == "乙长 句"


# ----- explicitly named doubts (listen-queue --doubt) ------------------------------------------------
def _maintained_override():
    """A doubt CPA decided and the maintained two-family reading then replaced: state ADOPT, not UNRESOLVED."""
    return {"id": "d051", "cue_id": 2, "current": "对，我觉得有点搞的", "final_text": "对 对 我偷偷搞的",
            "state": "ADOPT", "affects_meaning": True, "reason_key": "近音", "danmaku": [], "cpa_call": "adjudicate-6",
            "readings": {"bcut": "对，我觉得有点搞的", "jianying": "对我觉得有点搞的", "asr:mai": "对，我觉得有点搞的。",
                         "audio:argon@P0": "对。 对 我觉得有点搞的", "audio:argon@P2": "对 对 我偷偷搞的",
                         "audio:38m@P2": "对。 对， 我偷偷搞的。"}}


def test_decided_doubt_is_not_queued_unless_named():
    bcut = _cues(["甲", "对，我觉得有点搞的", "丙"])
    assert build_items(cid="c1", doubts=[_maintained_override()], bcut_cues=bcut, final_cues=bcut,
                       cut_len=CUT_LEN) == []


def test_named_doubt_shows_burned_text_and_pre_override_reading_with_their_sources():
    from src.autoslice.manual_lane.listen import render_page
    bcut = _cues(["甲", "对，我觉得有点搞的", "丙"])
    items = build_items(cid="c1", doubts=[_maintained_override()], bcut_cues=bcut, final_cues=bcut,
                        cut_len=CUT_LEN, include=["d051"])
    assert len(items) == 1
    it = items[0]
    assert it["burned"] == "对 对 我偷偷搞的"
    assert it["current"] == "对，我觉得有点搞的"   # the import contract keeps the pre-decision current text
    assert [(c["label"], c["text"], c["sources"]) for c in it["candidates"]] == [
        ("现稿", "对 对 我偷偷搞的", ["Gemini"]),
        ("判定前读法", "对，我觉得有点搞的", ["剪映", "MAI", "Gemini"]),
    ]
    page = render_page(items, generated_at="t")
    assert "判定前：对，我觉得有点搞的" in page
    assert "<b>判定前读法</b>" in page


def test_named_doubt_must_exist_in_doubts_json():
    bcut = _cues(["甲", "乙"])
    with pytest.raises(ListenError, match="d999"):
        build_items(cid="c1", doubts=[_maintained_override()], bcut_cues=bcut, final_cues=bcut, cut_len=CUT_LEN,
                    include=["d999"])


def test_listen_queue_refuses_a_malformed_doubt_spec(tmp_path):
    assert cli.main(["listen-queue", "--task-dir", str(tmp_path), "--doubt", "no-colon"]) == 2
