"""Scripted CPA agent for ordinary-entry tests; no provider or reviewed subtitles."""
import json

from fakes import FakeCpa
from src.autoslice.manual_lane.adapters import CPA_TEXT_TRIES


def answer_rows(rows):
    return [dict({'drop': False, 'reason': 'synthetic evidence'}, **row) for row in rows]


class AgentCpa(FakeCpa):
    def __init__(self, rows, tools=(), *, answers=None):
        rows = answer_rows(rows)
        if answers is None:
            answers = [{'action': 'plan', 'doubts': [], 'tools': list(tools)},
                       {'action': 'draft', 'rows': rows},
                       {'action': 'final', 'self_check': 'checked evidence and all cues', 'rows': rows}]
        super().__init__([json.dumps(a, ensure_ascii=False) for a in answers])

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir, tries=CPA_TEXT_TRIES, max_output_tokens=None):
        result = super().ask(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort,
                             remote_dir=remote_dir)
        result.update(usage={'total_tokens': 10}, attempts=1, max_output_tokens=max_output_tokens)
        return result


class FaultyCpa(AgentCpa):
    """The adapter's contract: one HTTP request per try, at most `tries` of them.  `faults` is consumed one per
    request: 'overloaded' is a provider refusal (a try spent, the next try may answer), 'cut' is an answer the
    provider called complete whose JSON stops mid-value (7 tokens spent, the scripted answer is not consumed)."""

    def __init__(self, *args, faults=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.faults = list(faults)
        self.requests = 0
        self.tries_seen = []

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir, tries=CPA_TEXT_TRIES, max_output_tokens=None):
        self.tries_seen.append(tries)
        used = 0
        while True:
            used += 1
            self.requests += 1
            fault = self.faults.pop(0) if self.faults else None
            if fault != 'overloaded':
                break
            if used >= tries:
                return {'call_id': call_id, 'model': model, 'effort': effort, 'rc': 1, 'complete': False,
                        'elapsed_s': 0.1, 'response_text': '', 'usage': None, 'attempts': used,
                        'stderr_tail': 'CPA_HTTP_ERROR servers are currently overloaded'}
        if fault == 'cut':
            return {'call_id': call_id, 'model': model, 'effort': effort, 'rc': 0, 'complete': True,
                    'elapsed_s': 0.1, 'response_text': self.responses[0][:-3], 'usage': {'total_tokens': 7},
                    'attempts': used, 'stderr_tail': ''}
        result = super().ask(call_id=call_id, prompt_text=prompt_text, model=model, effort=effort,
                             remote_dir=remote_dir, tries=tries, max_output_tokens=max_output_tokens)
        result['attempts'] = used
        return result
