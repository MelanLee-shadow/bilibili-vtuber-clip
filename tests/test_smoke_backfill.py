"""--smoke-segment 的有界 backfill：单候选 fail-closed 不再让整次冒烟空手。"""

import json
from pathlib import Path
from types import SimpleNamespace

import scripts.session_autoslice as runner


def _candidate(cid: str, start_ms: int, end_ms: int) -> SimpleNamespace:
    return SimpleNamespace(
        anchor=SimpleNamespace(candidate_id=cid),
        boundary=SimpleNamespace(
            resolved_start_ms=start_ms, resolved_end_ms=end_ms
        ),
        content_type_hint="talk",
    )


def _drive_smoke(monkeypatch, tmp_path, produce_results):
    segment = tmp_path / "22966160_20260801-20-00-00.flv"
    segment.write_bytes(b"x")
    srt = tmp_path / "smoke.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    candidates = [
        _candidate("cand_a", 1_000, 61_000),
        _candidate("cand_b", 90_000, 150_000),
        _candidate("cand_c", 200_000, 260_000),
        _candidate("cand_d", 300_000, 360_000),
    ]
    produced: list[str] = []

    def fake_produce(date, item):
        produced.append(item["cid"])
        return produce_results[len(produced) - 1]

    base = tmp_path / "autoslice-base"
    monkeypatch.setattr(runner, "BASE", base)
    monkeypatch.setattr(runner, "CPA_ENV", base / "cpa.env")
    monkeypatch.setattr(runner, "bcut_transcribe", lambda seg, date: srt)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda seg: None)
    monkeypatch.setattr(
        runner, "resolve_structured_chat_binding", lambda seg: {}
    )
    monkeypatch.setattr(runner, "danmaku_hints", lambda xml: [])
    monkeypatch.setattr(
        runner,
        "recall_candidates",
        lambda srt_text, hints, danmaku_xml=None: (candidates, "semantic", {}),
    )
    monkeypatch.setattr(runner, "ffprobe_ms", lambda seg: 1_200_000)
    monkeypatch.setattr(runner, "produce_talk", fake_produce)
    monkeypatch.setattr(runner, "log", lambda *a, **k: None)
    rc = runner.main(["--smoke-segment", str(segment)])
    return rc, produced


def test_smoke_backfills_to_next_candidate_on_fail_closed(
    monkeypatch, tmp_path, capsys
):
    rc, produced = _drive_smoke(
        monkeypatch,
        tmp_path,
        [
            {"status": "candidate_rejected"},
            {"status": "review_ready", "cid": "second"},
        ],
    )
    assert rc == 0
    assert len(produced) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "review_ready"
    assert payload["smoke_backfill_attempt"] == 2


def test_smoke_first_success_produces_exactly_one(monkeypatch, tmp_path, capsys):
    rc, produced = _drive_smoke(
        monkeypatch, tmp_path, [{"status": "ok", "cid": "first"}]
    )
    assert rc == 0
    assert len(produced) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "smoke_backfill_attempt" not in payload


def test_smoke_attempts_are_capped_and_failure_reported(
    monkeypatch, tmp_path, capsys
):
    rc, produced = _drive_smoke(
        monkeypatch,
        tmp_path,
        [
            {"status": "candidate_rejected"},
            {"status": "failed"},
            {"status": "quarantine_rejected"},
            {"status": "ok"},  # 第 4 个候选存在，但帽=3，绝不该被尝试
        ],
    )
    assert rc == 1
    assert len(produced) == runner.SMOKE_TALK_ATTEMPT_CAP == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "quarantine_rejected"
