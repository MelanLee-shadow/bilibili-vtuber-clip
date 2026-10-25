#!/usr/bin/env python3
"""离线回放录下的回答；只调用当前 checkout 的消费者，不进行推理或派发。

支持 review-flags/chat-authority 中的回执及 cpa-text-exchanges。嵌套副本按
run+prompt+completion 去重；物理请求仍各计一次。accept 只代表所列校验层，
不代表音频真值、落字或发布成功。--code-root 用于离线历史源码比较。
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from collections.abc import Mapping
from functools import lru_cache
import gc
import gzip
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
# 复用既有 gauntlet 的递归加载/原始 request 投影，不另造终裁 request。
from scripts.predeploy_contract_gauntlet import records, saved_inputs


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def offline_guard(event, args):
    if event.startswith(('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system')):
        raise RuntimeError('REPLAY_NETWORK_OR_PROCESS_FORBIDDEN')


def prompt_objects(prompt):
    """只解码行首 JSON 数据块，跳过说明中的示意 JSON 和其内部重复行。"""
    decoder, end = json.JSONDecoder(), 0
    for match in re.finditer(r'(?m)^[ \t]*[\[{]', prompt):
        start = match.end() - 1
        if start < end:
            continue
        try:
            obj, end = decoder.raw_decode(prompt, start)
        except ValueError:
            continue
        yield obj


def kind_of(prompt):
    first = next(iter(prompt.splitlines()), '')
    if first.startswith('# 字幕材料穷尽终裁'):
        return '终裁'
    if first.startswith('你裁决一个字幕疑点'):
        return '编辑呈现'
    if first.startswith('# CPA 补听范围选择'):
        return '补听范围'
    if first.startswith('# 字幕缺失候选最终收敛'):
        return '候选收敛'
    if '# Exact-final 字幕反复改写收敛（CPA 最终文字裁决）' in prompt:
        return '改写收敛'
    if '切片字幕做最终定稿代词。' in first:
        return '代词定稿'
    if ('直播切片的字幕草稿。' in prompt
            and '字幕草稿(每行:[时间] 编号. 文本):' in prompt):
        return '草稿校正'
    if first.startswith('# 字幕缺失候选重建'):
        return '候选重建'
    if first.startswith('# 字幕文字闭集裁决'):
        return '文字闭集'
    if first.startswith('# 字幕闭集裁决'):
        return '声学闭集'
    if '# 字幕选字裁决' in prompt:
        return '选字裁决'
    if first.startswith('你在校对'):
        return '念弹幕'
    if first.startswith('你是李豆沙切片的终审审片员'):
        return '终审发现'
    if first.startswith('# 候选级代词'):
        return '代词审计'
    if first.startswith('你是直播切片的单独边界终审'):
        return '边界终审'
    return '其他'


CID_NAMES = {'auto_195637_47_259': 'guimi', 'auto_200311_218_282': 'sister',
             'semantictalk_293580_466980': 'stomach', 'semantictalk_1438010_1548390': 'xiamen'}


def run_of(path):
    for part in path.parts:
        if re.fullmatch(r'(guimi|sister|stomach|xiamen)-[a-f0-9]{8}(?:-.*)?', part):
            return part
    text = str(path)
    version = re.search(r'b-unified-([a-f0-9]{8})-', text)
    cid = next((cid for cid in CID_NAMES if cid in path.parts), None)
    return CID_NAMES[cid] + '-' + version[1] if cid and version else path.parent.name


def iter_exchanges(roots):
    seen = set()
    for root in roots:
        candidates = [root] if root.is_file() else root.rglob('completion.txt')
        for completion in sorted(candidates):
            if 'cpa-text-exchanges' not in completion.parts:
                continue
            if completion.resolve() in seen:
                continue
            seen.add(completion.resolve())
            p = completion.with_name('prompt.txt')
            if not p.exists():
                continue
            meta = completion.with_name('request.json')
            metadata = json.loads(meta.read_text()) if meta.exists() else {}
            yield {'run': run_of(completion), 'path': str(completion), 'prompt': p.read_text(),
                   'raw': completion.read_text(), 'metadata': metadata, 'origin': 'exchange'}


def schema_errors(value, schema, path='$'):
    """通用 JSON Schema 子集解释器；业务规则仍由原消费者执行。"""
    if 'anyOf' in schema:
        if all(list(schema_errors(value, part, path)) for part in schema['anyOf']):
            yield path + ':anyOf'
        return
    types = schema.get('type', [])
    types = [types] if isinstance(types, str) else types
    predicates = {'object': lambda: isinstance(value, dict), 'array': lambda: isinstance(value, list),
                  'string': lambda: isinstance(value, str), 'boolean': lambda: type(value) is bool,
                  'integer': lambda: type(value) is int, 'number': lambda: type(value) in (int, float) and math.isfinite(value),
                  'null': lambda: value is None}
    if types and not any(predicates[t]() for t in types):
        yield path + ':type'
        return
    if 'enum' in schema and value not in schema['enum']:
        yield path + ':enum'
    if isinstance(value, dict):
        for key in schema.get('required', []):
            if key not in value:
                yield path + '.' + key + ':required'
        props = schema.get('properties', {})
        for key, child in value.items():
            if key in props:
                yield from schema_errors(child, props[key], path + '.' + key)
            elif schema.get('additionalProperties') is False:
                yield path + '.' + key + ':additionalProperty'
    if isinstance(value, list) and 'items' in schema:
        for i, child in enumerate(value):
            yield from schema_errors(child, schema['items'], f'{path}[{i}]')


@lru_cache(None)
def consumer_slice(module_name, function_name, begin, stop):
    """从真实消费者提取纯解码段，避免复制其手写合同，也不运行后面的网络/写盘。

    锚点不匹配立即 INCOMPLETE；返回值携带源码范围及哈希，便于检查提取边界。
    """
    module = importlib.import_module('src.autoslice.' + module_name)
    path = Path(module.__file__)
    tree = ast.parse(path.read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function_name)
    body = fn.body
    def source(n):
        return ast.unparse(n)
    a = next(i for i, n in enumerate(body) if begin in source(n))
    b = next((i for i in range(a + 1, len(body)) if stop and stop in source(body[i])), len(body))
    selected = body[a:b]
    helper = ast.FunctionDef(name='_replay_decode', args=ast.arguments(posonlyargs=[], args=[],
        kwonlyargs=[], kw_defaults=[], defaults=[]), body=selected + [ast.Return(value=ast.Call(
        func=ast.Name(id='locals', ctx=ast.Load()), args=[], keywords=[]))], decorator_list=[])
    code = ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[]))
    return module, compile(code, str(path), 'exec'), {
        'file': 'src/autoslice/' + module_name + '.py', 'start': selected[0].lineno,
        'end': selected[-1].end_lineno, 'sha256': sha(ast.unparse(ast.Module(body=selected, type_ignores=[])))}


def run_slice(module, function, begin, stop, variables):
    source, code, location = consumer_slice(module, function, begin, stop)
    env = {**vars(source), **variables}
    exec(code, env)
    return env['_replay_decode'](), location


def _terminal(case, answer):
    from src.autoslice.semantic_terminal_adjudication import validate_terminal_answer, keep_from_terminal
    from src.autoslice.editorial_subtitle import _decision
    request = case.get('record', {}).get('request')
    if not isinstance(request, dict):
        raise LookupError('REPLAY_ORIGINAL_TERMINAL_REQUEST_UNAVAILABLE')
    choice = validate_terminal_answer(request, answer, prompt=case['prompt'])
    current = request['current_cue']
    start, end = answer.get('edit_start'), answer.get('edit_end')
    noop = (choice == 'CONTEXT_FIT' and type(start) is int and type(end) is int
            and isinstance(answer.get('replacement'), str)
            and 0 <= start < end <= len(current)
            and current[:start] + answer['replacement'] + current[end:] == current)
    # 与 gauntlet.legacy_guard 相同的原始输入投影和 KEEP / typed mutation 分流。
    if choice == 'CURRENT' or noop:
        finding = request['audio_gain_termination'].get('stopped_finding', {})
        original = {'old_review': case['record'], 'finding': finding}
        srt, _, _, _, _ = saved_inputs(original, {})
        keep_from_terminal(srt_text=srt, finding=finding, request=request,
                           answer=answer, prompt=case['prompt'], raw=case['raw'])
    elif choice != 'UNRESOLVABLE':
        _decision(request, answer, prompt=case['prompt'])
    return {'choice': choice, 'release_action': 'BLOCK_CONTENT' if choice == 'UNRESOLVABLE'
            else 'APPLY_AND_REVIEW' if choice not in ('CURRENT',) and not noop else 'TYPED_KEEP',
            'validator': 'semantic_terminal_adjudication.validate_terminal_answer + gauntlet.legacy_guard'}, request


def missing_artifacts(record):
    """仅检查回执中明确登记的音频/原始 witness 路径，不查配置或凭据。"""
    keys={'audio_path','prompt_path','response_path','source_media','pcm_path','receipt_path',
          'media_source_path','source_media_path','request_path'}
    missing=set()
    for row in records(record):
        for key,value in row.items():
            if key in keys and isinstance(value,str) and value.startswith('/'):
                missing.add(value)
    return sorted(value for value in missing if not Path(value).exists())


@lru_cache(maxsize=64)
def _saved_boundary_grids(root):
    from src.autoslice.boundary_semantic_review import cue_rows, cue_grid_sha256
    from src.autoslice.jingting_chunker import parse_srt_cues
    grids = {}
    for path in sorted(root.rglob('*.srt')):
        cues = parse_srt_cues(path.read_text())
        if cues:
            grids.setdefault(cue_grid_sha256(cues), (cue_rows(cues), str(path)))
    return grids


def _boundary(case, answer, objects):
    """Consume original request and real saved grids; never issue a release receipt.

    A prompt may expose only the ending window. Do not invent earlier cues or
    choose one convenient grid: require the current decision to agree on every
    compatible saved grid, and retain their actual hashes/paths as evidence.
    """
    from src.autoslice import boundary_semantic_review as consumer
    request = next(o for o in objects if isinstance(o, dict)
                   and o.get('schema_version') == 'talk-boundary-semantic-request.v1')
    path = Path(case['path'])
    root = path.parents[2] if 'cpa-text-exchanges' in path.parts else path.parent
    captured = {row['cue_index']: row for row in request['cues']}
    decisions, inputs = {}, []
    for grid_sha, (rows, source) in _saved_boundary_grids(root).items():
        by_index = {row['cue_index']: row for row in rows}
        if not all(by_index.get(index) == row for index, row in captured.items()):
            continue
        eligibility = consumer.recommendation_eligibility(rows, request['boundary_search_scope'])
        if eligibility['cue_indexes'] != request['recommendation_cue_indexes']:
            continue
        result = consumer._interpret_semantic_reply(payload=answer, rows=rows,
            visible_rows=request['cues'],
            target_pos=next(i for i, row in enumerate(rows) if row['cue_index'] == request['target_cue_index']),
            target_ms=request['target_ms'], candidate_id=request['candidate_id'],
            max_forward_ms=request['max_forward_ms'], scope=request['boundary_search_scope'],
            request=request, request_sha256=consumer._canonical_sha256(request),
            current_cue_grid_sha256=grid_sha,
            recommendation_indexes=eligibility['cue_indexes'],
            recommendation_effective_end_ms=eligibility['effective_end_ms'],
            recommendation_relaxations=eligibility['relaxations'],
            source_separation_witness=request.get('terminal_source_separation_witness'),
            terminal_source_review=None, completed_review=None, replay_audit=None)
        decision = {key: result.get(key) for key in (
            'status', 'recommended_end_cue_index', 'recommended_end_ms', 'reason_codes', 'ending_proof_kind')}
        decisions[json.dumps(decision, sort_keys=True)] = decision
        inputs.append({'cue_grid_sha256': grid_sha, 'path': source})
    if not inputs:
        raise LookupError('REPLAY_ORIGINAL_BOUNDARY_GRID_UNAVAILABLE')
    if len(decisions) != 1:
        raise LookupError('REPLAY_ORIGINAL_BOUNDARY_GRID_AMBIGUOUS_DECISION')
    return {'boundary_decision': next(iter(decisions.values())), 'saved_grids': inputs,
        'scope': 'BOUNDARY_DECISION_ACROSS_COMPATIBLE_SAVED_GRIDS',
        'validator': 'boundary_semantic_review._interpret_semantic_reply',
        'release_action': 'NO_RELEASE_AUTHORITY'}


def _prompt_line(prompt, name):
    values = re.findall(r'(?m)^- ' + re.escape(name) + r': (.*)$', prompt)
    if len(values) != 1:
        raise LookupError('REPLAY_ORIGINAL_PROMPT_FIELD_UNAVAILABLE:' + name)
    return values[0]


def _convergence(case, answer):
    """Run the entire native answer decoder, stopping only before persistence.

    All data are literal fields from the recorded prompt. This does not rebuild
    an owner request or grant a keep/mutation receipt. Missing replacement
    evidence must stay INCOMPLETE; it is never replaced with an empty witness.
    """
    from src.autoslice.missing_proposal_bootstrap import proposal_convergence_contract
    from src.autoslice.final_review_auditor import _derive_single_span_edit
    prompt = case['prompt']
    current = _prompt_line(prompt, 'CURRENT')
    current_sha = _prompt_line(prompt, 'CURRENT_CUE_SHA256').removeprefix('sha256:')
    if sha(current) != current_sha:
        raise LookupError('REPLAY_ORIGINAL_CURRENT_HASH_MISMATCH')
    variables = {'payload': answer, 'current': current, 'current_sha256': current_sha,
        'audit': {}, 'finding': {'cue_index': int(_prompt_line(prompt, 'CUE_INDEX')),
            'suspect': _prompt_line(prompt, 'SUSPECT')},
        'contract': proposal_convergence_contract(current),
        'derive_single_span_edit': _derive_single_span_edit,
        'prompt_sha256': sha(prompt),
        'context_sha256': _prompt_line(prompt, 'CONTEXT_SHA256').removeprefix('sha256:'),
        'final_srt_sha256': _prompt_line(prompt, 'FINAL_SRT_SHA256').removeprefix('sha256:')}
    if answer.get('decision') == 'REPLACE_WITH_EXACT_TEXT':
        # These are the original complete data blocks consumed by the text
        # proposal guard, not an acoustic measurement synthesized by replay.
        marker = '## 音素约束及证据等级\n'
        if marker not in prompt:
            raise LookupError('REPLAY_ORIGINAL_CONVERGENCE_PHONEMES_UNAVAILABLE')
        variables['phonemes'] = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1].lstrip())[0]
        topic_start = '## 整片主题线索与宽窗转写（数据，只作语境，非逐字真值）'
        chat_start = '## 邻近结构化消息链（按时间顺序，只作语境）\n'
        if topic_start not in prompt or chat_start not in prompt:
            raise LookupError('REPLAY_ORIGINAL_CONVERGENCE_CONTEXT_UNAVAILABLE')
        variables['context_binding'] = {'proposal_clip_context': topic_start + prompt.split(topic_start, 1)[1].split('## 提案召回与最终确认的边界', 1)[0]}
        variables['structured_chat'] = prompt.split(chat_start, 1)[1].split('## 三路结构化保真证据', 1)[0]
    out, location = run_slice('missing_proposal_bootstrap', 'converge_missing_proposal',
        "decision = str(payload.get('decision')", '_save_payload_cache(', variables)
    audit = out[1] if isinstance(out, tuple) else variables['audit']
    if audit.get('status') == 'INVALID':
        raise ValueError(audit['reason_code'])
    return {'validator': location, 'decision': audit.get('decision'),
        'native_status': audit.get('status'), 'native_reason': audit.get('reason_code'),
        'needs_audio': audit.get('needs_audio', answer.get('needs_audio')),
        'scope': 'CONVERGENCE_ANSWER_DECODER_AND_TEXT_GEOMETRY',
        'release_action': 'NO_RELEASE_AUTHORITY'}


@lru_cache(maxsize=32)
def _saved_srt_inputs(root):
    from src.autoslice.jingting_chunker import parse_srt_cues
    found = []
    for path in sorted(root.rglob('*.srt')):
        text = path.read_text()
        cues = parse_srt_cues(text)
        if cues:
            numbered = '\n'.join(f'{i}. {cue.text}' for i, cue in enumerate(cues, 1))
            found.append((numbered, text, str(path)))
    return found


def _pronoun_final(case):
    from src.autoslice.pronoun_stage_trace import apply_pronoun_pass
    prompt = case['prompt']
    match = re.fullmatch(r'你在给(.+)\(B站虚拟主播\)切片字幕做最终定稿代词。.*', prompt.splitlines()[0])
    if not match or '\n字幕:\n' not in prompt or '\n只输出 JSON' not in prompt:
        raise LookupError('REPLAY_ORIGINAL_PRONOUN_INPUT_UNAVAILABLE')
    numbered = prompt.split('\n字幕:\n', 1)[1].split('\n只输出 JSON', 1)[0].rstrip('\n')
    path = Path(case['path'])
    root = path.parents[2] if 'cpa-text-exchanges' in path.parts else path.parent
    inputs, decisions = [], set()
    saved = [(text, srt, path, {}) for text, srt, path in _saved_srt_inputs(root)]
    # The pronoun stage often runs before the on-disk SRT is overwritten by a
    # later pass. Its native trace retains the complete original SRT and call.
    from src.autoslice.jingting_chunker import parse_srt_cues
    for trace_path in sorted(root.glob('*.pronoun-trace/*.json')):
        trace_bytes = trace_path.read_bytes()
        trace = json.loads(trace_bytes)
        if trace.get('schema_version') != 'aggregate-pronoun-stage-trace.v1':
            continue
        for i, call in enumerate(trace.get('calls', [])):
            if (call.get('prompt', {}).get('text') != prompt
                    or call.get('completion', {}).get('text') != case['raw']):
                continue
            retained = [trace.get('input_srt', {}), call['prompt'], call['completion']]
            if any(row.get('retention') != 'RETAINED' or not isinstance(row.get('text'), str)
                   or sha(row['text']) != row.get('sha256')
                   or len(row['text'].encode()) != row.get('bytes') for row in retained):
                raise LookupError('REPLAY_ORIGINAL_PRONOUN_TRACE_DRIFT')
            original = retained[0]['text']
            text = '\n'.join(f'{n}. {cue.text}' for n, cue in enumerate(parse_srt_cues(original), 1))
            saved.append((text, original, str(trace_path), {'json_pointer': '/input_srt/text',
                'call_pointer': f'/calls/{i}', 'file_sha256': hashlib.sha256(trace_bytes).hexdigest(),
                'original_outcome': call.get('outcome')}))
    for saved_numbered, original, source, provenance in saved:
        if saved_numbered != numbered:
            continue
        def recorded(generated):
            # The ordinary consumer rebuilds the core prompt. Verify it against
            # the recorded prefix; extra historical context is not a new call.
            if not (prompt == generated or prompt.startswith(generated + '\n')):
                raise LookupError('REPLAY_ORIGINAL_PRONOUN_PROMPT_MISMATCH')
            return case['raw']
        revised = apply_pronoun_pass(original, cpa_llm_call=recorded,
            display_name=match[1], required=True)
        from src.autoslice.jingting_chunker import parse_srt_cues
        decisions.add(tuple(c.text for c in parse_srt_cues(revised)))
        inputs.append({'path': source, 'srt_sha256': sha(original), 'result_srt_sha256': sha(revised), **provenance})
    if not inputs:
        raise LookupError('REPLAY_ORIGINAL_PRONOUN_SRT_UNAVAILABLE')
    if len(decisions) != 1:
        raise LookupError('REPLAY_ORIGINAL_PRONOUN_SRT_AMBIGUOUS_DECISION')
    return {'validator': 'pronoun_stage_trace.apply_pronoun_pass',
        'scope': 'PRONOUN_OCCURRENCE_APPLICATION_ACROSS_SAVED_SRTS',
        'saved_inputs': inputs, 'release_action': 'NO_RELEASE_AUTHORITY'}


def _draft_correction(case):
    from src.autoslice.subtitle_draft_preparation import _required_cpa_cues
    prompt = case['prompt']
    block = prompt.split('字幕草稿(每行:[时间] 编号. 文本):\n', 1)[1].split('\n只输出一个 JSON 对象', 1)[0]
    lines = block.strip('\n').splitlines()
    indexes = [int(m[1]) for line in lines
        if (m := re.fullmatch(r'\[\d{2}:\d{2}:\d{2}\] (\d+)\. .*', line))]
    if not indexes or indexes != list(range(1, len(lines) + 1)):
        raise LookupError('REPLAY_ORIGINAL_DRAFT_CUE_SET_UNAVAILABLE')
    corrected = _required_cpa_cues(prompt, lambda _: case['raw'], len(indexes))
    return {'validator': 'subtitle_draft_preparation._required_cpa_cues',
        'scope': 'COMPLETE_DRAFT_CUE_DECODER', 'cue_count': len(corrected),
        'release_action': 'NO_RELEASE_AUTHORITY'}


def _rewrite_convergence(case):
    from src.autoslice.exact_final_convergence import _converge_one
    record = case.get('record', {})
    inputs = record.get('replay_consumer_inputs')
    if not isinstance(inputs, dict) or not all(key in inputs for key in ('srt_text', 'finding', 'history')):
        raise LookupError('REPLAY_ORIGINAL_REWRITE_CONVERGENCE_INPUTS_UNAVAILABLE')
    prompt_mismatch = []
    def recorded(prompt):
        if prompt != case['prompt']:
            prompt_mismatch.append(True)
            raise LookupError('REPLAY_ORIGINAL_REWRITE_CONVERGENCE_PROMPT_MISMATCH')
        return case['raw']
    recorded.cpa_cache_identity = {'transport': 'recorded', 'models': ['offline'], 'effort': 'none'}
    # _converge_one uses its native geometry, history, evidence and acoustic
    # gate. A cache must not substitute some other saved answer for this one.
    from src.autoslice.completed_review_checkpoint import ruling_material_scope
    with ruling_material_scope({}, cache_allowed=False):
        out = _converge_one(inputs['srt_text'], inputs['finding'],
            history=inputs['history'], judge_llm_call=recorded)
    if prompt_mismatch:
        raise LookupError('REPLAY_ORIGINAL_REWRITE_CONVERGENCE_PROMPT_MISMATCH')
    if out is None:
        raise ValueError('REPLAY_CONSUMER_RETURNED_NONE')
    finding, kept = out
    gate = finding.get('exact_release_adjudication', {}).get('history_convergence_acoustic_witness')
    return {'validator': 'exact_final_convergence._converge_one',
        'scope': 'REWRITE_CONVERGENCE_CONSUMER', 'kept_current': kept,
        'native_gate': gate,
        'native_status': finding.get('exact_release_adjudication', {}).get('status'),
        'release_action': 'NO_RELEASE_AUTHORITY'}


def validate_case(case):
    from src.autoslice.llm_client import extract_json_object
    kind = kind_of(case['prompt'])
    result = {'kind': kind, 'status': 'ACCEPT', 'code': 'PASS', 'scope': 'ANSWER_CONTRACT'}
    try:
        answer = extract_json_object(case['raw'])
        objects = list(prompt_objects(case['prompt']))
        if kind == '终裁':
            detail, request = _terminal(case, answer)
            result.update(detail)
            try:
                from src.autoslice.response_schemas import terminal_schema
                result['schema_errors'] = list(schema_errors(answer, terminal_schema(request=request)))
            except ImportError:
                result['schema_errors'] = ['HISTORICAL_SCHEMA_UNAVAILABLE']
        elif kind == '编辑呈现':
            from src.autoslice.editorial_subtitle import _decision
            request = case.get('record', {}).get('request')
            if not isinstance(request, dict):
                raise LookupError('REPLAY_ORIGINAL_EDITORIAL_REQUEST_UNAVAILABLE')
            _decision(request, answer, prompt=case['prompt'])
        elif kind == '选字裁决' or (kind == '文字闭集' and '- CURRENT' in case['prompt']):
            allowed = {'CURRENT', 'PROPOSED', 'NEITHER'}
            if '只能选择 CURRENT、PROPOSED 或 UNCERTAIN' in case['prompt']:
                allowed = {'CURRENT', 'PROPOSED', 'UNCERTAIN'}
            proposed_line = re.search(r'(?m)^- PROPOSED[^:\n]*: (.*)$', case['prompt'])
            candidates = {}
            if proposed_line and proposed_line[1].startswith('{'):
                candidates = json.loads(proposed_line[1])
            variables = {'payload':answer, 'allowed_choices':allowed,
                'require_complete_support':'## exact-final CURRENT 整句闭合要求' in case['prompt'],
                'decision_contract':'current-proposed-candidate-id-neither.v1' if candidates else 'current-proposed-neither.v1',
                'candidates':candidates, 'call_errors':[], 'prompt_sha256':sha(case['prompt'])}
            word_contract = getattr(importlib.import_module('src.autoslice.acoustic_witness_adjudication'),
                                    'WordChoiceContract', None)
            if word_contract is not None:  # Older --code-root consumers use the explicit variables above.
                variables['contract'] = word_contract(case['prompt'], allowed,
                    variables['decision_contract'], {}, candidates,
                    variables['require_complete_support'], bool(candidates))
            out, location = run_slice('acoustic_witness_adjudication','_judge_word_choice_body',
                'choice, ranking = parse_judge_choice', 'verdict =', variables)
            if out.get('reason_code'):
                raise ValueError(out['reason_code'])
            result.update(choice=out['choice'], scope='CLOSED_SET_DECODER_AND_SUPPORT', ranking=out['ranking'],
                          validator=location)
            try:
                from src.autoslice.response_schemas import closed_set_schema
                result['schema_errors'] = list(schema_errors(answer, closed_set_schema(allowed)))
            except ImportError:
                pass
        elif kind in ('文字闭集', '声学闭集'):
            candidates = next(o for o in objects if isinstance(o, list) and o
                              and isinstance(o[0], dict) and ('canonical' in o[0] or 'choice_id' in o[0]))
            slot = 'choice_id' in candidates[0]
            variables = {'payload': answer, 'slot_mode': slot, 'slot_choices': candidates,
                         'canonicals': [o.get('canonical') for o in candidates]}
            out, location = run_slice('read_aloud_llm_verifier', 'judge', 'ranking_raw =',
                                      '_install_completed', variables)
            if out is None:
                raise ValueError('REPLAY_CONSUMER_RETURNED_NONE')
            result.update(choice=out['choice'], validator=location, scope='CLOSED_SET_DECODER')
            if 'TEXT_FIRST' in case['prompt']:
                result['needs_audio_type_valid'] = type(answer.get('needs_audio')) is bool
        elif kind == '候选重建':
            from src.autoslice.final_review_auditor import _derive_single_span_edit
            prompt = case['prompt']
            current = re.search(r'(?m)^- CURRENT: (.*)$', prompt)[1]
            suspect = re.search(r'(?m)^- SUSPECT: (.*)$', prompt)[1]
            pos = current.find(suspect)
            if pos < 0 or current.count(suspect) != 1:
                raise LookupError('REPLAY_SUSPECT_SPAN_UNAVAILABLE')
            phonemes = next((o.get('phonetic_evidence') for o in objects
                             if isinstance(o, dict) and 'phonetic_evidence' in o), None)
            variables = {'payload': answer, 'current': current, 'suspect': suspect,
                'reason': '', 'audit': {}, 'original_span': (pos, pos + len(suspect)),
                'finding': {'suspect': suspect}, 'current_sha256': sha(current),
                'rejected_candidates': (), 'phonemes': phonemes or {}, 'topic_context': prompt,
                'structured_chat': '', 'derive_single_span_edit': _derive_single_span_edit,
                'proposal_audio': None, 'prompt_sha256': sha(prompt), 'completion_sha256': sha(case['raw']),
                'cache_path': None, 'served_from_cache': False, '_save_payload_cache': lambda *a, **k: None}
            proposal_contract = getattr(importlib.import_module('src.autoslice.missing_proposal_bootstrap'),
                                        'missing_proposal_answer_contract', None)
            if proposal_contract is not None:
                variables['contract'] = proposal_contract(current)
            try:
                out, location = run_slice('missing_proposal_bootstrap', 'bootstrap_missing_proposal',
                    'from src.autoslice.semantic_answer import proposal_status', None, variables)
            except StopIteration:
                out, location = run_slice('missing_proposal_bootstrap', 'bootstrap_missing_proposal',
                    "status = str(payload.get('status')", None, variables)
            rebuilt, audit = out
            result.update(validator=location, decision=audit.get('status'), native_reason=audit.get('reason_code'))
            if audit.get('status') == 'INVALID':
                raise ValueError(audit['reason_code'])
            result['scope'] = 'PROPOSAL_DECODER_AND_TEXT_GEOMETRY'
        elif kind == '候选收敛':
            result.update(_convergence(case, answer))
        elif kind == '改写收敛':
            result.update(_rewrite_convergence(case))
        elif kind == '代词定稿':
            result.update(_pronoun_final(case))
        elif kind == '草稿校正':
            result.update(_draft_correction(case))
        elif kind == '念弹幕':
            from src.autoslice.read_aloud_llm_verifier import _coerce_confidence
            result.update(choice=answer.get('is_read_aloud'), confidence=_coerce_confidence(answer.get('confidence')),
                          validator='read_aloud_llm_verifier._coerce_confidence', scope='DETECTION_DECODER')
            # 生产 consumer 将其余值当作不能取得权威，保留而不伪造拒收码。
            if type(answer.get('is_read_aloud')) is not bool:
                raise ValueError('REPLAY_CONSUMER_NO_DETECTION_AUTHORITY')
        elif kind == '终审发现':
            from src.autoslice.final_review_auditor import _request_final_review_findings
            findings = _request_final_review_findings(case['prompt'],
                llm_call=lambda _: case['raw'], extract_json=extract_json_object)
            result.update(finding_count=len(findings), scope='FINAL_REVIEW_FINDINGS_ENVELOPE',
                validator='final_review_auditor._request_final_review_findings',
                release_action='NO_RELEASE_AUTHORITY')
        elif kind == '代词审计':
            from src.autoslice.pronoun_consistency import _validated_decisions
            occurrences = next(o for o in objects if isinstance(o, list) and o
                               and isinstance(o[0], dict) and 'occurrence_id' in o[0])
            received = _validated_decisions(answer, {row['occurrence_id']: row for row in occurrences})
            result.update(decision_count=len(received), scope='PRONOUN_COVERAGE_AND_DECISION_CONTRACT',
                validator='pronoun_consistency._validated_decisions', release_action='NO_RELEASE_AUTHORITY')
        elif kind == '边界终审':
            result.update(_boundary(case, answer, objects))
        elif kind == '补听范围':
            from src.autoslice import targeted_relisten as policy
            plan_input = next(o for o in objects if isinstance(o, dict) and 'target_cue' in o)
            record = case.get('record', {})
            check = record.get('consumer_check_request')
            projection = record.get('observation_projection_before')
            cues = [SimpleNamespace(**c) for c in plan_input['cues']]
            variables = {'plan': answer, 'audit': {}, 'output': {}, 'target': plan_input['target_cue'],
                'providers': plan_input['available_providers'], 'cues': cues,
                'check_request': {'source_media_timeline_offset_ms': plan_input['source_offset_ms']}}
            if answer.get('decision') == 'NO_NEW_AUDIO':
                # 路由合法性和耗尽证明是不同层。原始 proof 需原预算账本，不能伪造。
                result.update(scope='NO_NEW_AUDIO_DIAGNOSIS_ONLY', release_action='NO_RELEASE_AUTHORITY')
                if not isinstance(answer.get('reason'), str) or not answer['reason'].strip():
                    raise ValueError('SEMANTIC_RANGE_PLAN_INVALID')
            else:
                out, location = run_slice('semantic_audio_recovery', 'recover_semantic_audio_range',
                    'first, last =', 'assistance = None', variables)
                if isinstance(out, tuple):
                    raise ValueError(out[1]['reason_code'])
                result.update(validator=location, scope='ROUTE_GEOMETRY')
                if check and projection and answer.get('observation_class', 'CANDIDATE_BLIND_AUDIO') == 'CANDIDATE_BLIND_AUDIO':
                    policy.admit_plan(answer, check, projection,
                        source_window=[out['range_start'] + plan_input['source_offset_ms'],
                                       out['range_end'] + plan_input['source_offset_ms']],
                        cue_indexes=list(range(out['first'], out['last'] + 1)),
                        prior_audit=record.get('history_audio_recovery'))
                    result['scope'] = 'ROUTE_GEOMETRY_AND_ADMISSION'
        else:
            result.update(status='INCOMPLETE', code='REPLAY_UNSUPPORTED_ANSWER_TYPE', scope='JSON_PARSE_ONLY')
    except (LookupError, ImportError, AttributeError, StopIteration) as exc:
        result.update(status='INCOMPLETE', code='REPLAY_INPUT_OR_VALIDATOR_UNAVAILABLE', detail=str(exc))
    except Exception as exc:
        code = getattr(exc, 'reason_code', None) or str(exc)
        result.update(status='INCOMPLETE' if isinstance(exc,OSError) or type(exc).__name__=='ChannelProfileError' else 'REJECT',
                      code=code if re.fullmatch(r'[A-Z][A-Z0-9_]+', code or '')
                      else 'REPLAY_' + type(exc).__name__.upper(), detail=str(exc))
    if kind in {'候选收敛', '改写收敛', '代词定稿', '草稿校正'}:
        result['recorded_exchange_status'] = case.get('metadata', {}).get('status')
    if result['status']=='REJECT' and result['code'] in {
            'FINAL_STOP_OBSERVATION_DRIFT','TERMINAL_EVIDENCE_REFS_INVALID',
            'TERMINAL_CURRENT_OBSERVATION_SUPPORT_REQUIRED'}:
        absent=missing_artifacts(case.get('record',{}))
        if absent:
            result.update(status='INCOMPLETE', native_rejection=result['code'],
                evidence_availability='ORIGINAL_ARTIFACTS_UNAVAILABLE',
                missing_artifact_count=len(absent), missing_artifacts=absent[:8])
    return result


def question_info(case):
    """只记录可见的问题身份；物理同窗和 G6 同题留作两层统计。"""
    prompt = case['prompt']
    objects = list(prompt_objects(prompt))
    record = case.get('record', {})
    request = record.get('consumer_check_request') or (record.get('request') or {}).get('check_request')
    current, proposed, cue, target, binding = '', '', None, None, None
    if isinstance(request, dict):
        from src.autoslice.audio_question_identity import physical_target, source_binding
        target = physical_target(request)
        binding = source_binding(request)
        current, proposed = request.get('current_cue'), request.get('proposed_cue')
        cue = next(iter(request.get('cue_indexes') or []), None)
    for o in objects:
        if not isinstance(o, dict):
            continue
        if 'expected_source_target_ms' in o:
            target = o['expected_source_target_ms']
        if 'physical_target' in o and isinstance(o['physical_target'], dict):
            target = o['physical_target'].get('physical_target_ms')
        cue = cue or o.get('target_cue') or o.get('cue_index')
        if 'policy' in o and 'current' in o:
            current, proposed = o['current'], o.get('proposed')
    for regex in (r'(?m)^- CURRENT(?:（现字幕整句）|（现稿）)?: (.*)$', r'(?m)^CURRENT: (.*)$'):
        match = re.search(regex, prompt)
        if match and not current:
            current = match[1]
            break
    proposed_match=re.search(r'(?m)^- PROPOSED(?:（提案整句）)?: (.*)$',prompt)
    pool={}
    if proposed_match:
        if proposed_match[1].startswith('{'):
            try:pool=json.loads(proposed_match[1])
            except ValueError:pass
        elif not proposed:
            proposed=proposed_match[1]
    if not current:
        ctx = next((o for o in objects if isinstance(o, dict) and 'current_transcript' in o), {})
        current = ctx.get('current_transcript', '')
    if not cue:
        match = re.search(r'cue (\d+) .*<目标见 CURRENT>', prompt)
        cue = int(match[1]) if match else None
    try:
        from src.autoslice.llm_client import extract_json_object
        answer = extract_json_object(case['raw'])
        signature = {k: answer[k] for k in ('choice', 'candidate_id', 'status', 'decision', 'needs_audio',
                    'keep_route', 'grade', 'proposed_cue', 'is_read_aloud', 'first_cue', 'last_cue',
                    'provider', 'purpose', 'observation_class') if k in answer}
        choice=answer.get('choice')
        selected=(current if choice=='CURRENT' else
                  (pool.get(answer.get('candidate_id'),{}).get('text') or proposed) if choice=='PROPOSED' else
                  choice if isinstance(choice,str) and choice not in {'CONTEXT_FIT','UNRESOLVABLE','NEITHER','UNCERTAIN'} else None)
        if answer.get('status')=='PROPOSED':selected=answer.get('proposed_cue')
        if choice=='CONTEXT_FIT' and type(answer.get('edit_start')) is int and type(answer.get('edit_end')) is int:
            selected=current[:answer['edit_start']]+str(answer.get('replacement') or '')+current[answer['edit_end']:]
    except Exception:
        signature,selected = {},None
    return {'cue': cue, 'target_ms': target, 'source_binding': binding, 'current': current,
            'proposed': proposed, 'g6_request': ({k:request.get(k) for k in (
                'current_cue','base_text_sha256','proposed_cue','matched_start_ms','matched_end_ms',
                'source_media_timeline_offset_ms','clip_context_binding')} if isinstance(request,dict) else None),
            'decision': signature, 'selected_text':selected,
            'started_utc': case.get('metadata', {}).get('started_utc')}


def replay(roots, *, include_embedded=True, select_run=None, cases_out=None, cases_in=None):
    results, by_key, audit_checks = [], {}, []
    cache = gzip.open(cases_out, 'wt', encoding='utf-8') if cases_out else None
    def add(case):
        key = (case['run'], sha(case['prompt']), sha(case['raw']))
        if key in by_key:
            old = by_key[key]
            old['copies'] += 1
            if case['origin'] == 'exchange':
                if case['path'] not in old.setdefault('exchanges', []):
                    old['exchanges'].append(case['path'])
                if cache:
                    cache.write(json.dumps(case,ensure_ascii=False)+'\n')
                old['question']['started_utc'] = old['question'].get('started_utc') or case.get('metadata', {}).get('started_utc')
            return
        if cache:
            cache.write(json.dumps(case,ensure_ascii=False)+'\n')
        row = {'id': ':'.join(key), 'run': case['run'], 'path': case['path'],
               'prompt_sha256': key[1], 'completion_sha256': key[2], 'copies': 1,
               'first_line': next(iter(case['prompt'].splitlines()), ''),
               'question': question_info(case), **validate_case(case)}
        try:
            from src.autoslice.llm_client import extract_json_object
            row['answer'] = extract_json_object(case['raw'])
            request=case.get('record',{}).get('request')
            if row['kind']=='终裁' and isinstance(request,dict):
                from src.autoslice.semantic_terminal_adjudication import terminal_materials
                allowed=terminal_materials(request)[1]
                row['reference_diagnostics']={'available':list(allowed),
                    'invalid':[v for v in row['answer'].get('evidence_refs',[]) if v not in allowed]}
        except (ValueError,TypeError,KeyError,ImportError):
            pass
        if case['origin'] == 'exchange':
            row['exchanges'] = [case['path']]
        results.append(row)
        by_key[key] = row
    if cases_in:
        with gzip.open(cases_in,'rt',encoding='utf-8') as source:
            for line in source:
                run_match=re.match(r'\{\s*"run"\s*:\s*"([^"\\]*)"',line)
                if select_run and run_match and not re.search(select_run,run_match[1]):
                    continue
                case=json.loads(line)
                if not select_run or re.search(select_run,case['run']):
                    add(case)
        include_embedded=False
        roots=[]
    if include_embedded:
        paths = set()
        for root in roots:
            for p in ([root] if root.is_file() else root.rglob('*.json')):
                if ('review-flags' in p.name or 'chat-authority' in p.name) and p.is_file():
                    if not select_run or re.search(select_run,run_of(p)):
                        paths.add(p)
        for p in sorted(paths):
            print('回放',p.parent.name,p.name, file=sys.stderr, flush=True)
            try:
                data = json.loads(p.read_text())
            except (ValueError, OSError) as exc:
                raise ValueError('回放输入无法读取：' + str(p)) from exc
            if 'review-flags' in p.name:
                from src.autoslice.final_review_contract import validate_final_review_release, is_release_resolved_keep
                audit_check={'path':str(p),'run':run_of(p),'findings':len(data.get('findings',[])),
                             'resolved_keep_in_findings':sum(is_release_resolved_keep(r) for r in data.get('findings',[]))}
                try:
                    validate_final_review_release(data,expected_srt_sha256=data.get('reviewed_srt_sha256'))
                    audit_check.update(status='ACCEPT',code='PASS')
                except ValueError as exc:
                    audit_check.update(status='REJECT',code=getattr(exc,'reason_code',None) or str(exc))
                audit_checks.append(audit_check)
            for row in records(data):
                if isinstance(row.get('prompt'), str) and row['prompt'] and isinstance(row.get('raw_completion'), str):
                    add({'run': run_of(p), 'path': str(p), 'prompt': row['prompt'],
                         'raw': row['raw_completion'], 'record': row, 'origin': 'embedded'})
            del data
            gc.collect()
    for case in iter_exchanges(roots):
        if not select_run or re.search(select_run,case['run']):
            add(case)
    if cache:
        cache.close()
    summary = Counter((r['kind'], r['status'], r['code']) for r in results)
    return {'schema_version': 'recorded-answer-replay.v1',
            'code_root': str(Path(importlib.import_module('src.autoslice').__path__[0]).parent.parent),
            'unique_answers': len(results), 'physical_exchanges': sum(len(r.get('exchanges', [])) for r in results),
            'summary': [{'kind': k, 'status': s, 'code': c, 'count': n} for (k,s,c),n in sorted(summary.items())],
            'audit_checks':audit_checks,'answers': results}


def replay_ruling_reuse(roots, cache_root):
    """Replay duplicate physical exchanges through the ordinary raw-answer seam.

    This tests cache dispatch/reuse and the current decoder, not audio truth or
    release. Every group starts with an empty, task-owned cache root.
    """
    from src.autoslice.completed_review_checkpoint import ruling_response
    from src.autoslice.llm_client import cached_completion
    grouped = {}
    for case in iter_exchanges(roots):
        model = case['metadata'].get('model_identity')
        if not isinstance(model, Mapping) or not model.get('models') or not model.get('effort'):
            raise ValueError('REPLAY_ANSWERING_MODEL_UNAVAILABLE')
        key = (sha(case['prompt']), json.dumps(model, sort_keys=True))
        grouped.setdefault(key, []).append(case)
    groups = []
    for (prompt_sha, _), cases in sorted(grouped.items()):
        if len(cases) < 2:
            continue
        cases.sort(key=lambda c: (c.get('metadata', {}).get('started_utc', ''), c['path']))
        first = cases[0]
        model = first['metadata']['model_identity']
        calls, hits, outcomes, signatures, revalidations = [], [], [], [], []
        expected = validate_case(first)
        def conclusion(value):
            return {key: item for key, item in value.items() if key != 'saved_grids'}
        for index, case in enumerate(cases):
            def recorded(prompt):
                calls.append(sha(prompt))
                return cached_completion(first['raw'], model)
            recorded.cpa_cache_identity = model
            recorded.completed_review_root = cache_root
            recorded.provider_runtime_binding = {
                'provider_transport': 'offline-recorded',
                'provider_credential_source': f'/offline/versions/{index:040x}/cpa.env'}
            recorded.cpa_resource_pressure = {'provider_call_count': index * 19}
            raw, hit = ruling_response(stage=kind_of(case['prompt']), prompt=case['prompt'],
                llm_call=recorded, inputs={}, parser_path=Path(__file__))
            result = validate_case({**case, 'raw': raw})
            outcomes.append(result['status'] != 'INCOMPLETE' and conclusion(result) == conclusion(expected))
            hits.append(hit)
            signatures.append(question_info({**case, 'raw': raw}))
            revalidations.append({'path': case['path'], 'cache_hit': hit,
                'reused_completion_sha256': sha(raw), 'outcome': result})
        raw_signatures = [question_info(c) for c in cases]
        def selected(rows):
            return {json.dumps([r['decision'], r['selected_text']], ensure_ascii=False, sort_keys=True) for r in rows}
        groups.append({'prompt_sha256': prompt_sha, 'model_identity': model,
            'kind': kind_of(first['prompt']), 'members': len(cases), 'new_questions': len(calls),
            'cache_hits': sum(hits), 'consumer_matches_original': all(outcomes),
            'original_outcome': expected, 'original_prompt_path': str(Path(first['path']).with_name('prompt.txt')),
            'original_answer_path': first['path'], 'original_completion_sha256': sha(first['raw']),
            'original_selected_text': signatures[0]['selected_text'],
            'recorded_flip': len(selected(raw_signatures)) > 1,
            'reused_flip': len(selected(signatures)) > 1})
        groups[-1]['revalidations'] = revalidations
    return {'schema_version': 'ruling-reuse-replay.v1',
        'scope': 'SHARED_RAW_CACHE_AND_CURRENT_CONSUMER_DECODER',
        'duplicate_groups': len(groups), 'new_questions': sum(g['new_questions'] for g in groups),
        'cache_hits': sum(g['cache_hits'] for g in groups),
        'recorded_flip_groups': sum(g['recorded_flip'] for g in groups),
        'reused_flip_groups': sum(g['reused_flip'] for g in groups),
        'all_one_question': all(g['new_questions'] == 1 for g in groups),
        'all_consumer_matches_original': all(g['consumer_matches_original'] for g in groups),
        'complete_consumer_groups': sum(g['original_outcome']['status'] != 'INCOMPLETE' for g in groups),
        'groups': groups}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', nargs='+', type=lambda x: Path(x).expanduser())
    parser.add_argument('-o', '--output', type=Path, required=True)
    parser.add_argument('--code-root', type=Path, default=ROOT)
    parser.add_argument('--exchanges-only', action='store_true')
    parser.add_argument('--select-run', help='仅回放 run 名称匹配该正则的输入，历史比较用')
    parser.add_argument('--cases-out',type=Path,help='将去重后的原始回执存成 gzip JSONL，便于无损重复校验')
    parser.add_argument('--cases-in',type=Path,help='回放上述本地缓存；不重建或改绑 request')
    parser.add_argument('--ruling-reuse-cache', type=Path, help='用全新任务缓存验证完全同 prompt/model 组的原答复用')
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.code_root.resolve()))
    sys.addaudithook(offline_guard)
    if args.ruling_reuse_cache:
        if args.ruling_reuse_cache.exists():
            raise ValueError('REPLAY_REUSE_REQUIRES_FRESH_CACHE')
        result = replay_ruling_reuse(args.evidence, args.ruling_reuse_cache)
    else:
        from scripts.predeploy_contract_gauntlet import FullHashSnapshot
        with FullHashSnapshot() as snapshot:
            result = replay(args.evidence, include_embedded=not args.exchanges_only,select_run=args.select_run,
                            cases_out=args.cases_out,cases_in=args.cases_in)
        result['immutable_input_snapshot'] = {'files_rehashed_at_exit': len(snapshot.files),
            'redundant_reads_avoided': snapshot.hits, 'unchanged_at_end': True}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k,v in result.items() if k not in {'answers', 'groups'}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
