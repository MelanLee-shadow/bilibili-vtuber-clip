"""Diagnostic-only regression cases; synthetic responses are not model tests."""

from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import pytest

from src.autoslice.jev_response_diagnostics import diagnose_response

MODEL = "jev-test-v1"
ROOT = Path(__file__).resolve().parents[1]


def fixture():
    q = {
        "type": "choice",
        "instructions": "Choose the supported class",
        "criteria": {"a": None, "b": None},
    }
    a = {"type": "choice", "choice": "a", "confidence": 0.9, "probabilities": {"a": 0.9, "b": 0.1}}
    return (
        {
            "model": MODEL,
            "state": {"source": "synthetic"},
            "questions": {"one": copy.deepcopy(q), "two": copy.deepcopy(q)},
        },
        {
            "model": MODEL,
            "answers": {"one": copy.deepcopy(a), "two": copy.deepcopy(a)},
            "usage": {"input_tokens": 20, "output_tokens": 10},
        },
    )


def run(req, rsp):
    return diagnose_response(req, rsp, expected_model=MODEL)


def test_nonunit_answer_does_not_erase_its_neighbor_or_gain_action_rights():
    req, rsp = fixture()
    rsp["answers"]["one"]["probabilities"] = {"a": 0.89, "b": 0.1}
    before = copy.deepcopy((req, rsp))
    d = run(req, rsp)
    assert not d["strict_request_numeric_contract_pass"]
    assert d["answers"]["one"]["issues"] == ["DISTRIBUTION_INVALID"]
    assert d["answers"]["one"]["distribution_detail"] == "SUM_OUTSIDE_LEGACY_TOLERANCE"
    assert d["answers"]["two"]["individually_matches_numeric_contract"]
    assert d["counts"]["otherwise_valid_answers_dropped_with_request"] == 1
    assert d["mutation_authorized"] is False and d["release_authorized"] is False
    assert d["semantic_correctness"] == "UNASSESSED"
    assert (req, rsp) == before


@pytest.mark.parametrize("bad", [False, "0.9", float("nan"), float("inf"), -0.1, 1.1, 10**500])
def test_invalid_numeric_probabilities_are_not_promoted(bad):
    req, rsp = fixture()
    rsp["answers"]["one"]["probabilities"]["a"] = bad
    d = run(req, rsp)
    assert "DISTRIBUTION_INVALID" in d["answers"]["one"]["issues"]
    assert not d["strict_request_numeric_contract_pass"]
    json.dumps(d, allow_nan=False)


@pytest.mark.parametrize(
    "kind",
    [
        "argmax",
        "confidence",
        "type",
        "missing_option",
        "missing_question",
        "extra_question",
        "model",
        "usage",
        "request_shape",
    ],
)
def test_each_existing_contract_failure_is_reported(kind):
    req, rsp = fixture()
    if kind == "argmax":
        rsp["answers"]["one"]["choice"] = "b"
    elif kind == "confidence":
        rsp["answers"]["one"]["confidence"] = 2
    elif kind == "type":
        rsp["answers"]["one"]["type"] = "score"
    elif kind == "missing_option":
        del rsp["answers"]["one"]["probabilities"]["b"]
    elif kind == "missing_question":
        del rsp["answers"]["two"]
    elif kind == "extra_question":
        rsp["answers"]["extra"] = copy.deepcopy(rsp["answers"]["two"])
    elif kind == "model":
        rsp["model"] = "different-model"
    elif kind == "usage":
        rsp["usage"]["input_tokens"] = -1
    else:
        req["questions"] = []
    d = run(req, rsp)
    assert not d["strict_request_numeric_contract_pass"]
    assert d["global_issues"] or any(x["issues"] for x in d["answers"].values())


@pytest.mark.parametrize("p", [0.1, 0.10001, 0.10011])
def test_exact_existing_sum_tolerance_not_sdk_acceptance(p):
    req, rsp = fixture()
    rsp["answers"]["one"]["probabilities"]["b"] = p
    d = run(req, rsp)
    assert d["strict_request_numeric_contract_pass"] == (abs(0.9 + p - 1) <= 0.0001)


def test_missing_model_record_requires_explicit_expected_model_without_mutation():
    req, rsp = fixture()
    del req["model"]
    d = run(req, rsp)
    assert d["request_model"] is None and d["strict_request_numeric_contract_pass"]
    assert "model" not in req


def test_noul_and_unknown_primitive():
    req, rsp = fixture()
    req["questions"]["one"] = {"type": "noul"}
    rsp["answers"]["one"] = {"type": "noul", "noul": 0.8}
    assert run(req, rsp)["strict_request_numeric_contract_pass"]
    req["questions"]["one"] = {"type": "score"}
    rsp["answers"]["one"] = {"type": "score", "score": 1}
    assert run(req, rsp)["answers"]["one"]["issues"] == ["UNIMPLEMENTED_PRIMITIVE"]


def test_real_cli_is_read_only_and_create_only(tmp_path):
    req, rsp = fixture()
    a = tmp_path / "request.json"
    b = tmp_path / "response.body"
    out = tmp_path / "diagnostic.json"
    a.write_text(json.dumps(req))
    b.write_text(json.dumps(rsp))
    before = (a.read_bytes(), b.read_bytes())
    command = [
        sys.executable,
        "-B",
        str(ROOT / "scripts/diagnose_jev_responses.py"),
        "--request",
        str(a),
        "--response",
        str(b),
        "--expected-model",
        MODEL,
        "--output",
        str(out),
    ]
    p = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert p.returncode == 0, p.stderr
    d = json.loads(out.read_text())
    assert d["input_sha256"]["response"] == hashlib.sha256(before[1]).hexdigest()
    assert d["strict_request_numeric_contract_pass"] and not d["mutation_authorized"]
    old = out.read_bytes()
    p = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert p.returncode == 2 and out.read_bytes() == old
    assert (a.read_bytes(), b.read_bytes()) == before


def test_cli_rejects_duplicate_json_keys(tmp_path):
    req, rsp = fixture()
    a = tmp_path / "q.json"
    b = tmp_path / "r.body"
    a.write_text(json.dumps(req))
    b.write_text('{"model":"first","model":"second"}')
    command = [
        sys.executable,
        "-B",
        str(ROOT / "scripts/diagnose_jev_responses.py"),
        "--request",
        str(a),
        "--response",
        str(b),
        "--expected-model",
        MODEL,
    ]
    p = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert p.returncode == 2 and "DUPLICATE_JSON_KEY" in p.stderr
