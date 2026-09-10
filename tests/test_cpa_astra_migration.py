"""GPT-6 migration must not reuse a different model/effort's judgment."""

import json
import os
from pathlib import Path
import subprocess

from src.autoslice import llm_client
from src.autoslice.acoustic_witness_adjudication import judge_word_choice
from src.autoslice.cpa_runtime import build_cpa_qa_command


def test_qa_factory_uses_astra_without_gpt5_fallback():
    cmd = build_cpa_qa_command(
        Path("/unused"),
        load_env_file=lambda _: {"CPA_BASE_URL": "https://example.invalid/v1"},
        environment={},
    )
    assert "--model gpt-6-astra" in cmd
    assert "--fallback-model" not in cmd
    assert "gpt-5" not in cmd


def test_ordinary_first_pass_is_astra_low(monkeypatch):
    from src.autoslice import full_session_transcription

    configs = []

    def builder(config):
        configs.append(config)
        return lambda _: "{}"

    monkeypatch.setattr(llm_client, "build_llm_call", builder)
    full_session_transcription._build_aggregate_asr_transcriber("localhost", correct="cpa")
    assert "'gpt-6-astra' low" in configs[0].command_template
    assert configs[0].timeout_seconds == 600.0


def test_stale_gpt5_and_unsupported_none_fail_before_credentials(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/llm_via_cpa.sh"
    env = dict(os.environ)
    env.pop("CPA_API_KEY", None)
    for model, effort in [("gpt-5.6-sol", "low"), ("gpt-5.5", "medium"), ("gpt-6-astra", "none")]:
        result = subprocess.run(
            [
                "bash",
                str(script),
                str(tmp_path / "prompt"),
                str(tmp_path / "output"),
                model,
                effort,
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 2
        assert "missing" not in result.stderr
        assert not (tmp_path / "output").exists()


def test_factory_cache_identity_contains_model_effort_but_not_secret():
    call = llm_client.build_llm_call(
        llm_client.LlmConfig(
            transport="command",
            command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-6-astra' low 1",
            command_child_env={"CPA_API_KEY": "never-print-this-key"},
        )
    )
    identity = call.cpa_cache_identity
    assert identity["models"] == ["gpt-6-astra"]
    assert identity["effort"] == "low"
    assert "never-print" not in json.dumps(identity)


def test_judge_cache_is_separate_per_model_and_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    calls = []

    def build(model, effort, choice):
        def cpa(_):
            calls.append((model, effort))
            return json.dumps({"choice": choice})

        cpa.cpa_cache_identity = {"models": [model], "effort": effort}
        return cpa

    request = {"current_cue": "原句", "proposed_cue": "候选", "request_sha256": "a" * 64}
    witness = {"status": "UNCERTAIN", "reason_code": "CPA_TEXT_FIRST_NOT_REQUESTED"}
    low = build("gpt-6-astra", "low", "CURRENT")
    one = judge_word_choice(llm_call=low, check_request=request, witness=witness)
    two = judge_word_choice(llm_call=low, check_request=request, witness=witness)
    med = judge_word_choice(
        llm_call=build("gpt-6-astra", "medium", "PROPOSED"), check_request=request, witness=witness
    )
    assert one["choice"] == two["choice"] == "CURRENT"
    assert two.get("served_from_cache") is True
    assert med["choice"] == "PROPOSED"
    assert calls == [("gpt-6-astra", "low"), ("gpt-6-astra", "medium")]


def test_unconfigured_direct_model_has_no_cache_identity():
    call = llm_client.build_llm_call(llm_client.LlmConfig(transport="direct"))
    assert call.cpa_cache_identity is None


def test_unknown_model_identity_never_reuses_a_global_judge_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    choices = iter(("CURRENT", "PROPOSED"))

    def unlabelled(_):
        return json.dumps({"choice": next(choices)})

    req = {"current_cue": "甲", "proposed_cue": "乙"}
    witness = {"status": "UNCERTAIN"}
    assert (
        judge_word_choice(llm_call=unlabelled, check_request=req, witness=witness)["choice"]
        == "CURRENT"
    )
    assert (
        judge_word_choice(llm_call=unlabelled, check_request=req, witness=witness)["choice"]
        == "PROPOSED"
    )
