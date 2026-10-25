"""In-run Argon retrieval: the 360 s budget bounds waiting, not the judging of a retrieved hearing.

闺蜜 rerun (c0a3b2f0c) round 5 logged 1430 s under
``exact_final_review.argon_inrun_retrieval`` with ``max_wait_s=360`` after a single
poll: consuming two retrieved hearings was charged as waiting, and a job still
pending after that poll would have lost its remaining GETs.
"""
from __future__ import annotations

import pytest

from src.autoslice import final_review_auditor as auditor
from src.autoslice import semantic_retry_progress as retry

CONTEXT = {"candidate_id": "offline", "source_media_sha256": "a" * 64,
           "pieces": [{"source_media_sha256": "a" * 64, "start_ms": 0, "end_ms": 100000}]}


def agenda(count):
    texts = [f"这是第{i}句" for i in range(1, count + 1)]
    srt = "\n\n".join(f"{i}\n00:00:{2 * i:02d},000 --> 00:00:{2 * i + 1:02d},000\n{text}"
                      for i, text in enumerate(texts, 1)) + "\n"
    rows = [{"cue_index": i, "suspect": text, "suggestion": text + "吗",
             "repair_class": "phonetic", "why": f"question-{i}"} for i, text in enumerate(texts, 1)]
    return srt, rows


def pending(cue, *, dispatched_now):
    return {"status": "UNCERTAIN", "repaired": False,
            "reason_code": "ARGON_ESCALATION_PENDING_OR_UNRESOLVED",
            "argon_escalation": {"status": "PENDING", "job_name": f"job-{cue}",
                                 "dispatches_this_advance": 1 if dispatched_now else 0}}


RETRIEVED = {"status": "UNCERTAIN", "repaired": False, "reason_code": "RETRIEVED"}


class Verifier:
    argon_escalation = object()

    def __call__(self, _request):
        raise AssertionError("unexpected audio")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    monkeypatch.setenv("AUTOSLICE_EXACT_REVIEW_PARALLELISM", "1")
    monkeypatch.setattr(retry.time, "time_ns", lambda: 123456789)
    clock, sleeps = [0.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(auditor, "_argon_retrieval_clock", lambda: clock[0])
    monkeypatch.setattr(auditor, "_argon_retrieval_sleep", sleep)
    return clock, sleeps


def run(monkeypatch, adjudicate, count=2):
    srt, findings = agenda(count)
    monkeypatch.setattr(auditor, "adjudicate_context_finding", adjudicate)
    unresolved, resolved = auditor.adjudicate_exact_release_findings(
        srt, findings, entity_verifier=Verifier(), clip_context=CONTEXT)
    return {row["cue_index"]: row["exact_release_adjudication"] for row in [*unresolved, *resolved]}


def test_judging_a_retrieved_hearing_does_not_consume_the_waiting_budget(rig, monkeypatch, capsys):
    clock, sleeps = rig
    counts = {}

    def adjudicate(text, finding, **_kwargs):
        cue = finding["cue_index"]
        n = counts[cue] = counts.get(cue, 0) + 1
        if n == 1:
            return text, pending(cue, dispatched_now=True)
        if cue == 1:
            clock[0] += 900.0  # judge calls and bookkeeping for the retrieved hearing
            return text, dict(RETRIEVED)
        # cue 2 stays pending for polls 1-5 and is retrieved at the sixth GET.
        return text, (dict(RETRIEVED) if n == 7 else pending(cue, dispatched_now=False))

    rows = run(monkeypatch, adjudicate)
    assert rows[1]["reason_code"] == "RETRIEVED" and rows[2]["reason_code"] == "RETRIEVED"
    # Six polls of 60 s: the 900 s spent judging cue 1 never counted as waiting.
    assert sleeps == [auditor.ARGON_INRUN_RETRIEVAL_POLL_S] * 6
    assert counts == {1: 2, 2: 7}
    log = capsys.readouterr().err
    result = next(line for line in log.splitlines()
                  if "RESULT exact_final_review.argon_inrun_retrieval " in line)
    assert "retrieved=2" in result and "still_pending=0" in result and "polls=6" in result
    assert "waited_s=360.000" in result and "readjudicated_s=900.000" in result
    assert log.count("START exact_final_review.argon_inrun_retrieval.poll ") == 6
    assert log.count("START exact_final_review.argon_inrun_retrieval.readjudicate ") == 7
    assert "cue_index=1" in log and "cue_index=2" in log


def test_a_job_still_pending_keeps_its_remaining_polls_after_a_slow_consumption(rig, monkeypatch, capsys):
    clock, sleeps = rig
    counts = {}

    def adjudicate(text, finding, **_kwargs):
        cue = finding["cue_index"]
        n = counts[cue] = counts.get(cue, 0) + 1
        if n == 1:
            return text, pending(cue, dispatched_now=True)
        if cue == 1:
            clock[0] += 900.0
            return text, dict(RETRIEVED)
        clock[0] += 5.0  # the bounded GET pass of a still-pending job is waiting
        return text, pending(cue, dispatched_now=False)

    rows = run(monkeypatch, adjudicate)
    assert rows[1]["reason_code"] == "RETRIEVED"
    assert rows[2]["reason_code"] == "ARGON_ESCALATION_PENDING_OR_UNRESOLVED"
    # waiting = 60 s sleep + 5 s GET pass per poll; 5 polls fit in 360 s, a sixth would not.
    assert sleeps == [auditor.ARGON_INRUN_RETRIEVAL_POLL_S] * 5
    assert counts[2] == 6
    result = next(line for line in capsys.readouterr().err.splitlines()
                  if "RESULT exact_final_review.argon_inrun_retrieval " in line)
    assert "retrieved=1" in result and "still_pending=1" in result and "polls=5" in result
    assert "waited_s=325.000" in result and "readjudicated_s=900.000" in result
