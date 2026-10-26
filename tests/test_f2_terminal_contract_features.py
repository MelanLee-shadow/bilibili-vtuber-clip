"""Contract-feature refactor must preserve original bytes and decisions."""
from copy import deepcopy
import json

import pytest

from tests.f2_baseline import baseline_module
from tests.test_targeted_relisten_public_contract import rig  # noqa: F401
from tests.test_terminal_contract_roundtrip import c_reply, produce


def outcome(function, *args, **kwargs):
    try:
        return ("OK", function(*args, **kwargs))
    except (ValueError, TypeError, KeyError) as exc:
        return ("ValueError" if isinstance(exc, ValueError) else type(exc).__name__, str(exc))


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_versions_one_to_four_preserve_material_prompt_and_validation_bytes(rig):
    from src.autoslice import semantic_terminal_adjudication as terminal
    _, captured = produce(rig, c_reply, native=True,
        wide=("我听到了，呜呜", "我听到了，呜呜"))
    assert isinstance(captured["request"], dict)
    old = baseline_module("semantic_terminal_adjudication")
    for version in range(1, 5):
        request = {**deepcopy(captured["request"]), "terminal_contract_version": version}
        old_materials, old_refs = old.terminal_materials(request)
        materials, refs = terminal.terminal_materials(request)
        assert json_bytes((materials, refs)) == json_bytes((old_materials, old_refs))
        old_prompt = old.terminal_prompt(request)
        prompt = terminal.terminal_prompt(request)
        assert prompt.encode("utf-8") == old_prompt.encode("utf-8")
        assert terminal.terminal_contract_retry(request) == old.terminal_contract_retry(request)
        answers = [captured["answer"],
            {**captured["answer"], "grade": "B", "evidence_decisive": True,
             "keep_route": "B_EVIDENCE_SUPPORTS_CURRENT"},
            {**captured["answer"], "choice": "UNRESOLVABLE", "grade": "D", "keep_route": None,
             "evidence_decisive": False, "context_can_decide": False,
             "written_meaning_error": "原比较不能证明写错意思"}]
        for answer in answers:
            assert outcome(terminal.validate_terminal_answer, request, deepcopy(answer), prompt=prompt) == outcome(
                old.validate_terminal_answer, request, deepcopy(answer), prompt=old_prompt)
    unstamped = {key: value for key, value in captured["request"].items()
                 if key not in {"terminal_contract_version", "answer_contract_version"}}
    assert terminal.terminal_prompt(unstamped).encode() == old.terminal_prompt(unstamped).encode()


@pytest.mark.parametrize("invalid", [0, 5, -1, "4", 2.0, True, False, [], {}])
def test_unknown_contract_versions_fail_at_every_contract_entry(invalid):
    from src.autoslice import semantic_terminal_adjudication as terminal
    request = {"terminal_contract_version": invalid}
    for function, args in (
        (terminal.terminal_contract_features, (request,)),
        (terminal.terminal_contract_retry, (request,)),
        (terminal.terminal_materials, (request,)),
        (terminal.terminal_prompt, (request,)),
        (terminal.validate_terminal_answer, (request, {})),
        (terminal.terminal_prompt_matches, (request, "")),
    ):
        with pytest.raises(ValueError, match="TERMINAL_CONTRACT_VERSION_UNSUPPORTED"):
            function(*args)
