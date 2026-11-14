"""What the subtitle agent is given before it starts: the method, the channel's facts, the recognisers' draft and the leads.

One place for the agent's first prompt (playbook_step3 keeps the agent itself).  Everything a step knows about the clip
before the text is decided comes in here as material, never as a decision: the work on the screen (work_context), the
references the draft makes to things outside it (references), the channel's own facts (channel_facts).
"""
from __future__ import annotations

import json

from . import channel_facts, work_context
from .step_io import AGENT_PRINCIPLES, PRONOUN_RULE, PROMPTS


def base_prompt(bcut, jianying, others, cut_len, clip, work, refs, task=None) -> str:
    return '\n'.join((
        (PROMPTS / 'subtitle_agent.md').read_text(encoding='utf-8'),
        (PROMPTS / 'subtitle_playbook.md').read_text(encoding='utf-8'),
        (PROMPTS / 'subtitle_playbook_rules.md').read_text(encoding='utf-8'), PRONOUN_RULE, AGENT_PRINCIPLES,
        channel_facts.for_subtitles(task),
        '初始转写与片长：' + json.dumps({'bcut': list(bcut), 'jianying': list(jianying), **others,
                                         'cut_duration_s': cut_len, 'work_context': work_context.agent_view(work),
                                         'references': refs['leads'], 'hook': clip.get('hook')}, ensure_ascii=False)))
