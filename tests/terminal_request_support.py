"""Synthetic terminal policy request shared by public regressions."""
from src.autoslice.subtitle_acceptability_policy import load_policy

def terminal_request(current, proposed):
    return {'terminal_policy': load_policy(), 'terminal_glossary': [], 'terminal_context': {},
        'check_request': {'current_cue': current, 'proposed_cue': proposed},
        'audio_gain_termination': {'projection': {'observations': [{'observation_id': 'wide'}]},
            'context_sources': {'cue:1': {'text': current}}}}
