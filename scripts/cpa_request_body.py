#!/usr/bin/env python3
"""CPA command transport's shared Responses body builder; no credentials."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.autoslice.structured_output import guard_json_object


def build_body(prompt, model, effort, response_format=None):
    body = {'model': model, 'input': prompt, 'reasoning': {'effort': effort}, 'max_output_tokens': 16000}
    guarded = guard_json_object(response_format, prompt)
    if response_format and guarded is None:
        print('[autoslice-structured-output] JSON_MODE_SKIPPED_NO_JSON_WORD', file=sys.stderr)
    response_format = guarded
    if response_format:
        body['text'] = {'format': response_format}
    return body


def load_response_format(prompt_file):
    sidecar = Path(str(prompt_file) + '.response-format.json')
    if sidecar.is_file():
        return json.loads(sidecar.read_text(encoding='utf-8'))
    value = os.environ.get('AUTOSLICE_RESPONSE_FORMAT')
    return json.loads(value) if value is not None else None


if __name__ == '__main__':
    response_format = load_response_format(sys.argv[1])
    print(json.dumps(build_body(Path(sys.argv[1]).read_text(encoding='utf-8'),
        sys.argv[2], sys.argv[3], response_format), ensure_ascii=False))
