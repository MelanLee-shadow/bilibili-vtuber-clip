"""Config-shape lock for the final-review CPA transports.

2026-08-19 (Ivan): the candidate-level pronoun audit is a closed 4-token
classification rerun every exact-final/self-heal round; it does not need the
same reasoning effort as the A-class entity/boundary judges, so it gets its
own builder (same approved model chain, effort medium -> low).  These tests
pin both command_templates so a future edit cannot silently widen the model
chain or re-couple the two builders while "fixing" one of them.
"""

from src.autoslice import producer_final_review_transport as transport


def _captured_config(monkeypatch, build_fn):
    captured: list = []

    def _capture(config):
        captured.append(config)
        return lambda _prompt: "{}"

    monkeypatch.setattr(transport, "build_llm_call", _capture)
    build_fn()
    assert len(captured) == 1
    return captured[0]


def test_final_review_builder_keeps_medium_effort(monkeypatch):
    config = _captured_config(monkeypatch, transport.build_final_review_llm_call)

    assert config.transport == "command"
    assert "'gpt-5.6-sol gpt-5.5 gpt-5.4'" in config.command_template
    assert config.command_template.split()[-2] == "medium"
    assert config.timeout_seconds == 600.0


def test_pronoun_audit_builder_drops_to_low_effort_same_model_chain(monkeypatch):
    config = _captured_config(monkeypatch, transport.build_pronoun_audit_llm_call)

    assert config.transport == "command"
    assert "'gpt-5.6-sol gpt-5.5 gpt-5.4'" in config.command_template
    assert config.command_template.split()[-2] == "low"
    assert config.timeout_seconds == 600.0


def test_pronoun_audit_and_final_review_share_everything_but_effort(monkeypatch):
    final_review_config = _captured_config(
        monkeypatch, transport.build_final_review_llm_call
    )
    pronoun_config = _captured_config(
        monkeypatch, transport.build_pronoun_audit_llm_call
    )

    assert final_review_config.command_template.replace(
        "medium", "low"
    ) == pronoun_config.command_template
    assert final_review_config.timeout_seconds == pronoun_config.timeout_seconds
