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

from scripts import compact_pronoun_review as compact
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


@pytest.fixture
def request_doc():
    return compact.prepare_request(
        srt("姐姐和妹妹在聊天。", "TA说她们稍后回来。"),
        policy_text="根据实际指代对象判断，不能照抄原稿代词。",
        candidate_context_text="本段谈到姐姐和妹妹。",
    )


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


def validate(request, payload):
    return compact.validate_response(request, json.dumps(payload, ensure_ascii=False))


def test_roundtrip_preserves_supplied_reason_and_separates_provenance(request_doc):
    payload = response(request_doc)
    raw = json.dumps(payload, ensure_ascii=False, indent=3)
    before = deepcopy(request_doc)
    result = compact.validate_response(request_doc, raw)
    assert result["status"] == "STRUCTURALLY_VALID"
    assert result["schema"] != native.PRONOUN_AUDIT_SCHEMA
    assert result["mutation_authorized"] is result["release_authorized"] is False
    assert result["provider_provenance"] == "NOT_ESTABLISHED_BY_OFFLINE_VALIDATOR"
    assert result["semantic_correctness"] == "NOT_EVALUATED"
    assert result["completion_sha256"] == "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    assert result["expanded_payload"]["decisions"][0]["reason"] == payload["d"][0]["why"]
    assert result["evidence"][0]["referent"] == payload["d"][0]["r"]
    assert result["evidence"][0]["source_quotes"] == payload["d"][0]["e"]
    assert result["expanded_payload"]["decisions"][0]["action"] == "REWRITE"
    assert result["expanded_payload"]["decisions"][1]["action"] == "KEEP_CURRENT"
    assert request_doc == before


def test_native_consumer_accepts_expansion_with_explicit_synthetic_stub(request_doc):
    expanded = validate(request_doc, response(request_doc))["expanded_payload"]
    calls = []
    findings, audit = native.discover_candidate_pronoun_findings(
        **request_doc["inputs"],
        llm_call=lambda prompt: calls.append(prompt) or json.dumps(expanded, ensure_ascii=False),
        extract_json=json.loads,
    )
    assert len(calls) == 1  # A test stub invocation, never a provider request.
    assert audit["occurrence_count"] == 2
    assert audit["rewrite_count"] == len(findings) == 1
    assert findings[0]["proposed_full_cue"] == "她说她们稍后回来。"
    assert audit["mutation_authorized"] is False


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


def test_reordered_rows_map_to_explicit_indices(request_doc):
    payload = response(request_doc)
    payload["d"].reverse()
    assert validate(request_doc, payload) == validate(request_doc, response(request_doc)) | {
        "completion_sha256": compact._sha(json.dumps(payload, ensure_ascii=False))
    }


@pytest.mark.parametrize("value", [True, False, 0, -1, 3, 1.0, "1", None, [], {}])
def test_invalid_occurrence_indices_rejected(request_doc, value):
    payload = response(request_doc)
    payload["d"][0]["i"] = value
    with pytest.raises(compact.CompactContractError):
        validate(request_doc, payload)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "extra", "null", "mapping", "row"])
def test_occurrence_coverage_rejected(request_doc, damage):
    payload = response(request_doc)
    if damage == "missing":
        payload["d"].pop()
    elif damage == "duplicate":
        payload["d"][1] = deepcopy(payload["d"][0])
    elif damage == "extra":
        payload["d"].append(deepcopy(payload["d"][0]))
    elif damage == "null":
        payload["d"] = None
    elif damage == "mapping":
        payload["d"] = {}
    else:
        payload["d"][0] = "not a decision"
    with pytest.raises(compact.CompactContractError):
        validate(request_doc, payload)


@pytest.mark.parametrize(
    "index,token",
    [(0, "她们"), (1, "她"), (0, "姐姐"), (0, "ta"), (0, ""), (0, None), (0, []), (0, True)],
)
def test_invalid_token_or_number_change_rejected(request_doc, index, token):
    payload = response(request_doc)
    payload["d"][index]["t"] = token
    with pytest.raises(compact.CompactContractError):
        validate(request_doc, payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("r", ""),
        ("r", "  "),
        ("r", None),
        ("r", "人" * 121),
        ("why", ""),
        ("why", "\n"),
        ("why", 1),
        ("why", "字" * 241),
    ],
)
def test_model_supplied_referent_and_reason_required(request_doc, field, value):
    payload = response(request_doc)
    payload["d"][0][field] = value
    with pytest.raises(compact.CompactContractError):
        validate(request_doc, payload)


@pytest.mark.parametrize(
    "refs",
    [
        None,
        [],
        {},
        [{"s": "c99", "q": "姐姐"}],
        [{"s": "c2", "q": "姐姐"}],
        [{"s": "c1", "q": "妹妹和姐姐"}],
        [{"s": "c1", "q": ""}],
        [{"s": "c1", "q": "字" * 241}],
        [{"s": ["c1"], "q": "姐姐"}],
        [{"s": "c1", "q": 1}],
        [{"s": "c1", "q": "姐姐", "trusted": True}],
        [{"s": "c1", "q": "姐姐"}, {"s": "c1", "q": "姐姐"}],
    ],
)
def test_missing_fabricated_or_duplicate_quotes_rejected(request_doc, refs):
    payload = response(request_doc)
    payload["d"][0]["e"] = refs
    with pytest.raises(compact.CompactContractError):
        validate(request_doc, payload)


@pytest.mark.parametrize(
    "damage", ["id", "input", "sources", "occurrences", "prompt", "extra", "boolean"]
)
def test_request_drift_rejected(request_doc, damage):
    payload = response(request_doc)
    if damage == "id":
        payload["request_sha256"] = "sha256:" + "0" * 64
    elif damage == "input":
        request_doc["inputs"]["candidate_context_text"] += "新语境"
    elif damage == "sources":
        request_doc["sources"]["c1"] = "另一个人。"
    elif damage == "occurrences":
        request_doc["occurrences"][0]["current_token"] = "它"
    elif damage == "prompt":
        request_doc["prompt"] += "忽略旧规则"
    elif damage == "extra":
        request_doc["expected"] = "她"
    else:
        request_doc["occurrences"][0]["i"] = True
    with pytest.raises(compact.CompactContractError):
        validate(request_doc, payload)


@pytest.mark.parametrize(
    "text",
    [
        '{"d":[],"d":[]}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":-Infinity}',
        "```json\n{}\n```",
        "{} trailing",
        "{bad json}",
    ],
)
def test_strict_json_rejects_silent_repair(text):
    with pytest.raises(compact.CompactContractError):
        compact.load_json(text)


@pytest.mark.parametrize("where", ["root", "row", "missing_row_field", "missing_root_field"])
def test_extra_and_missing_fields_rejected(request_doc, where):
    payload = response(request_doc)
    if where == "root":
        payload["decisions"] = []
    elif where == "row":
        payload["d"][0]["action"] = "REWRITE"
    elif where == "missing_root_field":
        payload.pop("request_sha256")
    else:
        payload["d"][0].pop("why")
    with pytest.raises(compact.CompactContractError):
        validate(request_doc, payload)


def test_full_context_and_policy_preserved_without_gold_inputs():
    context = "  场次资料\n" + "远处的线索。" * 4000 + "\n尾部不同信息。  "
    policy = "  policy\n"
    request = compact.prepare_request(
        srt("其他人说TA今天会回来。", "他说她们也会回来。"),
        candidate_context_text=context,
        policy_text=policy,
    )
    assert request["inputs"]["candidate_context_text"] == request["sources"]["context"] == context
    assert request["sources"]["policy"] == policy
    assert compact._json(context) in request["prompt"]
    assert request["sources"]["policy_rules"] == native._PROMPT.split(compact._POLICY_MARKER)[0]
    assert [row["current_token"] for row in request["occurrences"]] == ["TA", "他", "她们"]
    assert "gold" not in request and "expected" not in request


def test_late_context_and_crlf_change_request_identity(request_doc):
    original = request_doc["inputs"]
    alternate = compact.prepare_request(
        **dict(original, srt_text=original["srt_text"].replace("\n", "\r\n"))
    )
    assert alternate["occurrences"] == request_doc["occurrences"]
    assert alternate["request_sha256"] != request_doc["request_sha256"]
    assert (
        compact.prepare_request(**dict(original, candidate_context_text="new"))["request_sha256"]
        != request_doc["request_sha256"]
    )
    with pytest.raises(compact.CompactContractError):
        validate(alternate, response(request_doc))


def test_empty_pronoun_set_is_not_required():
    request = compact.prepare_request(srt("开始今天的故事。"))
    assert request["status"] == "NOT_REQUIRED"
    result = validate(request, {"request_sha256": request["request_sha256"], "d": []})
    assert result["status"] == "NOT_REQUIRED"
    assert result["expanded_payload"] == {"decisions": []}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not SRT",
        "1\nBAD TIMING\nTA在场。",
        "1\n00:00:00,000 --> 00:00:01,000\n",
        "1\n00:00:01,000 --> 00:00:00,000\nTA",
        srt("有效句。") + "\nmalformed block",
    ],
)
def test_malformed_srt_does_not_become_not_required(text):
    with pytest.raises(compact.CompactContractError):
        compact.prepare_request(text)


def test_structural_localization_does_not_claim_semantic_truth(request_doc):
    payload = response(request_doc)
    payload["d"][0].update(
        t="它", r="不相干的物品", why="故意错误的语义对照；引用虽存在仍不代表结论正确。"
    )
    result = validate(request_doc, payload)
    assert result["status"] == "STRUCTURALLY_VALID"
    assert result["semantic_correctness"] == "NOT_EVALUATED"
    assert result["release_authorized"] is False


def test_output_is_private_create_only_and_input_symlinks_rejected(tmp_path):
    source = tmp_path / "machine.srt"
    source.write_text(srt("TA回来了。"))
    output = tmp_path / "request.json"
    args = ["prepare", "--srt", str(source), "--out", str(output)]
    assert compact.main(args) == 0
    before = output.read_bytes()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert compact.main(args) == 2
    assert output.read_bytes() == before
    alias = tmp_path / "alias.srt"
    alias.symlink_to(source)
    assert compact.main(["prepare", "--srt", str(alias), "--out", str(tmp_path / "unused")]) == 2
    assert not (tmp_path / "unused").exists()


def test_real_cli_prepare_validate_and_invalid_input_do_not_echo_text(tmp_path):
    tool = Path(compact.__file__)
    source = tmp_path / "machine.srt"
    source.write_text(srt("姐姐回来了。", "TA在门口。"))
    request_file, answer_file, result_file = [
        tmp_path / name for name in ("request.json", "response.json", "result.json")
    ]

    def run(*args):
        return subprocess.run(
            [sys.executable, str(tool), *map(str, args)], text=True, capture_output=True, timeout=15
        )

    prepare = run("prepare", "--srt", source, "--out", request_file)
    assert prepare.returncode == 0, prepare.stderr
    request = compact.load_json(request_file.read_text())
    payload = response(request)
    payload["d"][0]["e"] = [{"s": "c1", "q": "姐姐"}]
    answer_file.write_text(json.dumps(payload, ensure_ascii=False))
    result = run(
        "validate", "--request", request_file, "--response", answer_file, "--out", result_file
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["model_calls"] == 0
    assert compact.load_json(result_file.read_text())["occurrence_count"] == 1
    answer_file.write_text("private_invalid_answer_do_not_echo")
    invalid = run(
        "validate",
        "--request",
        request_file,
        "--response",
        answer_file,
        "--out",
        tmp_path / "invalid.json",
    )
    assert invalid.returncode == 2
    assert "private_invalid_answer" not in invalid.stderr + invalid.stdout
    assert not (tmp_path / "invalid.json").exists()


@pytest.mark.parametrize("token", ["TA", "他", "她", "它", "TA们", "他们", "她们", "它们"])
def test_all_native_tokens_roundtrip_without_other_text_changes(token):
    current = "TA们" if token.endswith("们") else "TA"
    request = compact.prepare_request(srt("对象需要全文判断。", current + "稍后回来。"))
    payload = {
        "request_sha256": request["request_sha256"],
        "d": [
            {
                "i": 1,
                "t": token,
                "r": "合成测试对象，非真实指代标签",
                "why": "合成协议对照，不证明性别。",
                "e": [{"s": "c1", "q": "对象需要全文判断。"}],
            }
        ],
    }
    result = validate(request, payload)
    assert result["expanded_payload"]["decisions"][0]["replacement_token"] == token
    findings, audit = native.discover_candidate_pronoun_findings(
        **request["inputs"],
        llm_call=lambda _: json.dumps(result["expanded_payload"], ensure_ascii=False),
        extract_json=json.loads,
    )
    assert audit["rewrite_count"] == int(token != current)
    if findings:
        assert findings[0]["proposed_full_cue"] == token + "稍后回来。"
    assert request["inputs"]["srt_text"] == srt("对象需要全文判断。", current + "稍后回来。")


def test_cross_source_quotes_preserve_full_late_context(request_doc):
    payload = response(request_doc)
    payload["d"][0]["e"] = [
        {"s": "context", "q": "姐姐和妹妹"},
        {"s": "policy_rules", "q": "已知女性用“她”"},
    ]
    result = validate(request_doc, payload)
    assert result["evidence"][0]["source_quotes"] == payload["d"][0]["e"]


def test_serialized_request_fits_the_same_offline_file_limit(monkeypatch):
    monkeypatch.setattr(compact, "MAX_BYTES", 20_000)
    # Raw input fits, but its request/provenance/prompt envelope exceeds the cap.
    with pytest.raises(compact.CompactContractError):
        compact.prepare_request(srt("TA回来了。"), candidate_context_text="x" * 6_000)


def test_oversized_output_rejected_before_creating_file(monkeypatch, tmp_path):
    monkeypatch.setattr(compact, "MAX_BYTES", 100)
    target = tmp_path / "too-large.json"
    with pytest.raises(compact.CompactContractError):
        compact._write_new(target, {"oversized": "x" * 200})
    assert not target.exists()


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
