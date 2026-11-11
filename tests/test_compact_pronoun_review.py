"""Offline synthetic protocol controls, NOT model accuracy or provider evidence."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import stat
import subprocess
import sys

import pytest

from src.autoslice import pronoun_consistency as native


def srt(*texts: str) -> str:
    return (
        "\n\n".join(
            f"{i}\n00:00:{i * 2:02d},000 --> 00:00:{i * 2 + 1:02d},000\n{text}"
            for i, text in enumerate(texts, 1)
        )
        + "\n"
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("Offline protocol test attempted network")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


def response(request):
    # Explicit synthetic choices; not recovered or simulated historical model output.
    return {
        "request_sha256": request["request_sha256"],
        "d": [
            {
                "i": row["i"],
                "t": "她们" if row["current_token"].endswith("们") else "她",
                "r": "姐姐和妹妹" if row["current_token"].endswith("们") else "姐姐",
                "why": "  合成对照：前文指出是姐姐和妹妹。  ",
                "e": [{"s": "c1", "q": "姐姐和妹妹"}],
            }
            for row in request["occurrences"]
        ],
    }


def _native_policy_audit(srt_text: str, *, rewrite_first: bool = False):
    _, audit = native.discover_candidate_pronoun_findings(
        srt_text,
        policy_text="女性主播与男性观众的语义政策。",
        candidate_context_text="当前对话上下文。",
        llm_call=lambda _prompt: json.dumps(
            {
                "decisions": [
                    {
                        "occurrence_id": "cue-1-occurrence-1",
                        "action": "REWRITE" if rewrite_first else "KEEP_CURRENT",
                        "current_token": "她",
                        "replacement_token": "他" if rewrite_first else "她",
                        "reason": "同一段语境指向女性主播",
                    },
                    {
                        "occurrence_id": "cue-2-occurrence-1",
                        "action": "KEEP_CURRENT",
                        "current_token": "他",
                        "replacement_token": "他",
                        "reason": "同一段语境指向男性人物",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        extract_json=json.loads,
    )
    return audit


@pytest.mark.parametrize("rewrite_first", [False, True])
def test_scoped_pronoun_policy_evidence_is_current_context_only(rewrite_first):
    source = srt("她说梦", "他回话")
    audit = _native_policy_audit(source, rewrite_first=rewrite_first)

    evidence = native.scoped_pronoun_policy_evidence(source, audit, 1)

    assert evidence is not None
    assert evidence["schema_version"] == "candidate-pronoun-policy-evidence.v1"
    assert evidence["status"] == "EVIDENCE_ONLY"
    assert evidence["scope"] == "CONTEXT_ONLY"
    assert evidence["mutation_authorized"] is False
    assert [row["occurrence_id"] for row in evidence["occurrences"]] == [
        "cue-1-occurrence-1"
    ]
    assert [row["occurrence_id"] for row in evidence["decisions"]] == [
        "cue-1-occurrence-1"
    ]
    assert evidence["decisions"][0]["action"] == (
        "REWRITE" if rewrite_first else "KEEP_CURRENT"
    )
    assert "proposed_full_cue" not in evidence


@pytest.mark.parametrize(
    "damage", ["srt", "occurrences", "occurrence_order", "duplicate", "decisions", "hash"]
)
def test_scoped_pronoun_policy_evidence_rejects_stale_or_incomplete_audit(damage):
    source = srt("她说梦", "他回话")
    audit = _native_policy_audit(source)
    if damage == "srt":
        changed_source = srt("她说梦已改", "他回话")
        assert native.scoped_pronoun_policy_evidence(changed_source, audit, 1) is None
    elif damage == "occurrences":
        audit["occurrences"] = audit["occurrences"][:1]
        assert native.scoped_pronoun_policy_evidence(source, audit, 1) is None
    elif damage == "occurrence_order":
        audit["occurrences"] = list(reversed(audit["occurrences"]))
        assert native.scoped_pronoun_policy_evidence(source, audit, 1) is None
    elif damage == "duplicate":
        audit["occurrences"] = [audit["occurrences"][0]] * 2
        assert native.scoped_pronoun_policy_evidence(source, audit, 1) is None
    elif damage == "decisions":
        audit["decisions"][0]["replacement_token"] = "他"
        assert native.scoped_pronoun_policy_evidence(source, audit, 1) is None
    else:
        audit["prompt_sha256"] = "not-a-sha"
        assert native.scoped_pronoun_policy_evidence(source, audit, 1) is None


@pytest.mark.parametrize("rewrite_first", [False, True])
def test_same_candidate_reuses_complete_pronoun_discovery_without_provider(rewrite_first):
    source = srt("她说梦", "他回话")
    audit = _native_policy_audit(source, rewrite_first=rewrite_first)
    original = deepcopy(audit)

    def forbidden(_prompt):
        raise AssertionError("unchanged complete discovery must not ask again")

    findings, replay = native.discover_candidate_pronoun_findings(
        source, policy_text="女性主播与男性观众的语义政策。",
        candidate_context_text="当前对话上下文。", prior_audit=audit,
        llm_call=forbidden, extract_json=json.loads,
    )
    assert len(findings) == int(rewrite_first)
    assert replay["decisions"] == audit["decisions"]
    assert replay["completion_sha256"] == audit["completion_sha256"]
    assert replay["prior_audit_replay"]["provider_calls"] == 0
    assert replay["mutation_authorized"] is False
    assert audit == original


@pytest.mark.parametrize("damage", ["srt", "policy", "context", "prompt", "decisions", "findings"])
def test_pronoun_discovery_drift_requires_real_review(damage):
    source = srt("她说梦", "他回话")
    audit = _native_policy_audit(source)
    policy, context = "女性主播与男性观众的语义政策。", "当前对话上下文。"
    if damage == "srt":
        source = srt("她说新梦", "他回话")
    elif damage == "policy":
        policy += "新政策"
    elif damage == "context":
        context += "新语境"
    elif damage == "prompt":
        audit["prompt_sha256"] = "sha256:" + "a" * 64
    elif damage == "decisions":
        audit["decisions"].pop()
    else:
        audit["finding_count"] = 1
        audit["finding_sha256s"] = ["sha256:" + "a" * 64]
    calls = []
    _, result = native.discover_candidate_pronoun_findings(
        source, policy_text=policy, candidate_context_text=context,
        prior_audit=audit, extract_json=json.loads,
        llm_call=lambda prompt: calls.append(prompt) or json.dumps(
            {"decisions": _native_policy_audit(source)["decisions"]}, ensure_ascii=False),
    )
    assert len(calls) == 1
    assert "prior_audit_replay" not in result
