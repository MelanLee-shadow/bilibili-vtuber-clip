"""An in-flight answer must remain bound to the producer that sent it."""
import json
from pathlib import Path

import pytest

from scripts.predeploy_contract_gauntlet import batch_terminal_prompt, case_has_bound_answer
from tests.test_targeted_relisten_public_contract import rig  # noqa: F401
from tests.test_terminal_contract_roundtrip import c_reply, produce


@pytest.mark.parametrize('batched', [False, True])
def test_returned_answer_cold_binding_and_stale_producer_negative(rig, batched):
    _, captured = produce(rig, c_reply)
    state=rig.tmp/'dispatch-binding'
    directory=state/'case'
    directory.mkdir(parents=True)
    prompt=captured['prompt']
    answer=captured['answer']
    case={'id':'actual-producer:1','directory':str(directory)}
    prompts={case['id']:prompt}
    if batched:
        prompts['independent:2']=prompt
    (directory/'prompt.txt').write_text(prompt)
    (directory/'answer.txt').write_text(json.dumps(answer,ensure_ascii=False))
    (state/'call-01.prompt.txt').write_text(batch_terminal_prompt(prompts))
    (state/'call-01.answer.txt').write_text(json.dumps(
        {key:answer for key in prompts} if batched else answer,ensure_ascii=False))
    row={'call':1,'ids':list(prompts),'status':'RETURNED'}
    (state/'calls.json').write_text(json.dumps([row]))
    assert case_has_bound_answer(state,case)
    (directory/'prompt.txt').write_text(prompt+'\n')
    assert not case_has_bound_answer(state,case)
    (directory/'prompt.txt').write_text(prompt)
    (directory/'answer.txt').write_text(json.dumps({**answer,'reason':'different return'}))
    assert not case_has_bound_answer(state,case)
    (directory/'answer.txt').write_text(json.dumps(answer,ensure_ascii=False))
    (state/'calls.json').write_text(json.dumps([{**row,'status':'OUTCOME_UNKNOWN'}]))
    assert not case_has_bound_answer(state,case)
