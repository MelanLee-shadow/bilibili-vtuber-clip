import json
import sys
from pathlib import Path

import pytest

from scripts.cpa_semantic_qa_llm import build_judge_prompt, judge_request, main, normalize_judgment
from src.autoslice.cpa_semantic_qa import (
    CpaSemanticQaRequest,
    CpaSemanticSourceRef,
    CpaTerminologyContext,
    load_request_artifact,
    validate_cpa_semantic_response_payload,
    write_cpa_semantic_request_artifact,
)
from src.autoslice.llm_client import LlmCallError, LlmConfig, build_llm_call, extract_json_object


def _request(tmp_path: Path) -> CpaSemanticQaRequest:
    request_path = tmp_path / "req.json"
    response_path = tmp_path / "resp.json"
    request = CpaSemanticQaRequest(
        candidate_id="llm-judge-test",
        room_id="26730839",
        source=CpaSemanticSourceRef(video_path="/v.ts", srt_path="/s.srt", start_ms=0, end_ms=60_000),
        candidate_text="唱完整首歌之后大家都笑了",
        normalized_text="唱完整首歌之后大家都笑了",
        response_path=str(response_path),
    )
    return write_cpa_semantic_request_artifact(request, request_path)


def _good_judgment() -> dict:
    return {
        "release_ready": True,
        "semantic_complete": True,
        "terminology_ok": True,
        "title_hook_score": 0.88,
        "context_dependency_score": 0.2,
        "unsafe_upload_risk_score": 0.05,
        "viewer_context_ok": True,
        "context_expand_before_ms": 0,
        "context_expand_after_ms": 0,
        "reason_codes": [],
        "required_fixes": [],
        "summary": "完整有趣，可发布",
    }


def test_judge_binds_hash_and_paths_from_request_not_llm(tmp_path):
    request = _request(tmp_path)
    completion = json.dumps({**_good_judgment(), "candidate_id": "EVIL", "request_sha256": "sha256:evil"})

    response = judge_request(request, lambda prompt: completion, provider_label="llm:test")

    assert response.candidate_id == "llm-judge-test"
    assert response.request_sha256 == request.canonical_sha256()
    payload = response.to_dict()
    assert validate_cpa_semantic_response_payload(payload, request=request, response_path=Path(request.response_path)) == ()


def test_judge_retries_once_then_succeeds(tmp_path):
    request = _request(tmp_path)
    calls = []

    def flaky(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            raise LlmCallError("transient")
        return json.dumps(_good_judgment())

    response = judge_request(request, flaky, provider_label="llm:test", retries=1)
    assert len(calls) == 2
    assert response.release_ready is True


def test_not_ready_without_reasons_is_forced_to_block_code():
    judgment = normalize_judgment({**_good_judgment(), "release_ready": False})
    assert judgment["reason_codes"] == ("CPA_SEMANTIC_INCOMPLETE",)


@pytest.mark.parametrize(
    "mutation",
    [
        {"release_ready": "yes"},
        {"title_hook_score": "high"},
        {"reason_codes": "TERMINOLOGY_QA_FAILED"},
        {"summary": ""},
        {"viewer_context_ok": "yes"},
        {"context_expand_before_ms": "long"},
    ],
)
def test_malformed_judgment_fields_raise(mutation):
    with pytest.raises(LlmCallError):
        normalize_judgment({**_good_judgment(), **mutation})


def test_viewer_context_incomplete_forces_block_and_reason_code():
    judgment = normalize_judgment(
        {
            **_good_judgment(),
            "viewer_context_ok": False,
            "context_expand_before_ms": 25_000,
            "context_expand_after_ms": 4_000,
        }
    )
    assert judgment["release_ready"] is False
    assert "VIEWER_CONTEXT_INCOMPLETE" in judgment["reason_codes"]
    assert judgment["context_expand_before_ms"] == 25_000
    assert judgment["required_fixes"]


def test_viewer_context_expansion_is_clamped():
    judgment = normalize_judgment(
        {
            **_good_judgment(),
            "viewer_context_ok": False,
            "context_expand_before_ms": 10_000_000,
            "context_expand_after_ms": -500,
        }
    )
    assert judgment["context_expand_before_ms"] == 300_000
    assert judgment["context_expand_after_ms"] == 0


def test_viewer_context_verdict_lands_in_response_metadata(tmp_path):
    request = _request(tmp_path)
    completion = json.dumps(
        {**_good_judgment(), "viewer_context_ok": False, "context_expand_before_ms": 30_000}
    )
    response = judge_request(request, lambda prompt: completion, provider_label="llm:test")
    viewer_context = response.metadata["viewer_context"]
    assert viewer_context == {
        "viewer_context_ok": False,
        "expand_before_ms": 30_000,
        "expand_after_ms": 0,
    }
    assert response.release_ready is False
    assert "VIEWER_CONTEXT_INCOMPLETE" in tuple(response.reason_codes)


def test_prompt_contains_viewer_perspective_and_surrounding_context(tmp_path):
    request_path = tmp_path / "req.json"
    response_path = tmp_path / "resp.json"
    request = CpaSemanticQaRequest(
        candidate_id="viewer-ctx-test",
        room_id="22966160",
        source=CpaSemanticSourceRef(video_path="/v.ts", srt_path="/s.srt", start_ms=60_000, end_ms=120_000),
        candidate_text="这真的不是融了阿朵吗",
        normalized_text="这真的不是融了阿朵吗",
        response_path=str(response_path),
        metadata={
            "surrounding_context": {
                "window_ms": 90_000,
                "before_text": "我们来看看这张AI生成的图",
                "after_text": "下一张图",
            }
        },
    )
    request = write_cpa_semantic_request_artifact(request, request_path)
    prompt = build_judge_prompt(request)
    assert "viewer_context_ok" in prompt
    assert "我们来看看这张AI生成的图" in prompt
    assert "VIEWER_CONTEXT_INCOMPLETE" in prompt
    assert "观众视角" in prompt


def test_scores_are_clamped_into_unit_range():
    judgment = normalize_judgment({**_good_judgment(), "title_hook_score": 1.7, "unsafe_upload_risk_score": -3})
    assert judgment["title_hook_score"] == 1.0
    assert judgment["unsafe_upload_risk_score"] == 0.0


def test_prompt_injects_terminology_canon_and_mishear_blacklist(tmp_path):
    request_path = tmp_path / "req.json"
    request = CpaSemanticQaRequest(
        candidate_id="term-test",
        room_id="22966160",
        source=CpaSemanticSourceRef(video_path="/v.ts", srt_path="/s.srt", start_ms=0, end_ms=60_000),
        candidate_text="停放熊被骗到了",
        normalized_text="停放熊被骗到了",
        response_path=str(tmp_path / "resp.json"),
        terminology=CpaTerminologyContext(applied_terms=("kmx", "142", "沙豆李", "Ado")),
        metadata={"terminology_blacklist": ["停放熊", "沙特琳", "一四二", "苏丹", "康姆叉"]},
    )
    request = write_cpa_semantic_request_artifact(request, request_path)
    prompt = build_judge_prompt(request)

    # canon proper nouns drive the "must use" contract line
    for canon in ("kmx", "142", "沙豆李", "Ado"):
        assert canon in prompt, canon
    # every mishearing variant is injected into the terminology_ok gate
    for variant in ("停放熊", "沙特琳", "一四二", "苏丹", "康姆叉"):
        assert variant in prompt, variant
    assert "TERMINOLOGY_QA_FAILED" in prompt
    # the raw metadata key must not be dumped verbatim into the metadata block
    assert "terminology_blacklist" not in prompt


def test_prompt_terminology_falls_back_to_repo_glossary(tmp_path):
    # A request carrying no applied_terms / blacklist still gets the full
    # glossary-derived terminology gate (covers requests built by other runners).
    request_path = tmp_path / "req.json"
    request = CpaSemanticQaRequest(
        candidate_id="term-fallback",
        room_id="22966160",
        source=CpaSemanticSourceRef(video_path="/v.ts", srt_path="/s.srt", start_ms=0, end_ms=60_000),
        candidate_text="x",
        normalized_text="x",
        response_path=str(tmp_path / "resp.json"),
    )
    request = write_cpa_semantic_request_artifact(request, request_path)
    prompt = build_judge_prompt(request)

    # terms present only via the real glossary parse (not hardcoded in the script)
    for term in ("倒反天罡", "奶油苏打", "康姆叉", "幺四二", "阿朵"):
        assert term in prompt, term


def test_prompt_contains_candidate_contract_and_complete_song_rule(tmp_path):
    request = _request(tmp_path)
    prompt = build_judge_prompt(request)
    assert "唱完整首歌之后大家都笑了" in prompt
    assert "release_ready" in prompt
    assert "title_hook_score" in prompt
    assert "机器证据 metadata" in prompt
    assert "完整歌曲切片" in prompt
    assert "NOT_INTERESTING" in prompt


def test_extract_json_object_handles_fences_and_prose():
    completion = "好的，以下是评审：\n```json\n{\"a\": 1, \"b\": {\"c\": \"x\"}}\n```\n完毕"
    assert extract_json_object(completion) == {"a": 1, "b": {"c": "x"}}
    with pytest.raises(LlmCallError):
        extract_json_object("没有对象")


def test_command_transport_end_to_end_via_script_main(tmp_path):
    request = _request(tmp_path)
    completion_json = json.dumps(_good_judgment(), ensure_ascii=False)
    fake_llm = tmp_path / "fake_llm.py"
    fake_llm.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path(sys.argv[2]).write_text({completion_json!r}, encoding='utf-8')\n",
        encoding="utf-8",
    )

    rc = main(
        [
            "--request",
            str(tmp_path / "req.json"),
            "--response",
            str(request.response_path),
            "--transport",
            "command",
            "--llm-command",
            f"{sys.executable} {fake_llm} {{prompt_file}} {{completion_file}}",
        ]
    )

    assert rc == 0
    payload = json.loads(Path(request.response_path).read_text(encoding="utf-8"))
    loaded_request = load_request_artifact(tmp_path / "req.json")
    assert validate_cpa_semantic_response_payload(payload, request=loaded_request, response_path=Path(request.response_path)) == ()


def test_command_transport_failure_exits_nonzero(tmp_path):
    _request(tmp_path)
    rc = main(
        [
            "--request",
            str(tmp_path / "req.json"),
            "--response",
            str(tmp_path / "resp.json"),
            "--transport",
            "command",
            "--llm-command",
            f"{sys.executable} -c import_sys_broken",
            "--retries",
            "0",
        ]
    )
    assert rc == 3


def test_direct_transport_requires_key_env(monkeypatch):
    monkeypatch.delenv("NO_SUCH_KEY_ENV", raising=False)
    call = build_llm_call(
        LlmConfig(transport="direct", model="m", api_base="https://example.invalid/v1", api_key_env="NO_SUCH_KEY_ENV")
    )
    with pytest.raises(LlmCallError):
        call("hi")
