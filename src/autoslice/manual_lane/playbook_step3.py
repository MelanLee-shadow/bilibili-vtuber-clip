"""Single-speaker step 3: CPA selects evidence and reviews its own decisions."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time

from . import (doubt_policy, evidence_attach, mishearing, name_match, normalize, playbook_evidence as pe,
               step3_text_classes, work_context, work_lookup)
from .srt_text import _json_from_text, format_srt
from .step3_doubts import decided_doubt, doubt_entry, gated_outcome, lone_and_holds, write_subtitles
from .step3_evidence import ARGON, M38, _listen_pass, upload_listen_tools
from .step_io import AGENT_PRINCIPLES, PRONOUN_RULE, PROMPTS, _ask_cpa, _cpa_ok, _fail, _local, _write_atomic


# These are runaway guards, not a target number of investigations. Usage includes both providers.
MAX_TEXT_CALLS = 80
MAX_LISTEN_CALLS = 160
MAX_TOOL_CALLS = 500
MAX_TOTAL_TOKENS = 4_000_000
HINTS_NOTE = ('机械候选，可能有误，只是证据：readout_matches 是同期弹幕/SC 中可能被念出或回响的原记录'
              '（relation=readout_candidate 为句前或句内已发出）；near_names 是句中与名字表拼音相近而写法不同的名字；'
              'mishearing 是频道已知误听对。由你核对后决定，系统不会替你改字。')
SEGMENTS_NOTE = ('这些同场录像分段读取失败或无法对齐时钟，其中的 SC/礼物/舰长是未知，不是没有；'
                 '可再请求，或按其余证据判断，不得当作不存在。')
SELF_CHECK_INSTRUCTION = ('交叉核对全部证据；需要时再请求工具，然后修订。附上的 hints 是流水线已算好的逐句机械候选，'
                          '可能有误，由你核对后决定。凡命中 readout_candidate 的句，若最终文字与原记录不同，'
                          '须在 self_check 里逐句写明偏离了哪些字、意思是否改变；意思改变的视为听错，改按原记录。')


def _save(ctx, name, obj):
    path = _local(ctx, name)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    return path


def _listen(ctx, windows, model, label):
    """Dispatch only windows selected by CPA; keep each exact request."""
    template = (PROMPTS / 'listen_single.md').read_text(encoding='utf-8')
    for window in windows:
        lead = window['core'][0] - window['audio'][0]
        tail = window['audio'][1] - window['core'][1]
        prompt = template.replace('{lead}', f'{lead:.1f}').replace('{tail}', f'{tail:.1f}')
        _save(ctx, f"listen/requests/{window['win']}-{model['slug']}.json",
              {'model': model, 'prompt': prompt, 'audio': window['audio'],
               'core': window['core'], 'tempo': window['tempo'], 'triggers': window['triggers']})
    return _listen_pass(ctx, label, windows, [model], prompt='listen_single.md', prepared=True)


def _decision(doubt, row):
    decision_row = dict(row)
    if row.get('state') == 'UNRESOLVED' and row.get('drop') is True:
        decision_row.update(state='ADOPT', confidence='低')
    return decided_doubt(doubt, doubt_policy.decide(decision_row, doubt['current'], maintained=False), row)


def _token_count(usage):
    if not isinstance(usage, dict):
        return None
    for key in ('total_tokens', 'totalTokenCount'):
        value = usage.get(key)
        if type(value) is int and value >= 0:
            return value
    return None


def _agent_action(response):
    try:
        value = _json_from_text(response)
    except ValueError:
        return None
    return value.get('action') if isinstance(value, dict) else None


def _content(text):
    return ''.join(ch for ch in text if not ch.isspace())


def _row_problem(rows, bcut):
    if not isinstance(rows, list) or len(rows) != len(bcut):
        return 'rows must contain exactly one answer for every cue'
    original = {f"d{int(c['id']):03d}": c['text'] for c in bcut}
    got = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('id') not in original or row['id'] in got:
            return 'row id is missing, unknown, or duplicated'
        got.add(row['id'])
        if (row.get('state') not in ('KEEP', 'ADOPT', 'FIT_CONTEXT', 'UNRESOLVED')
                or not isinstance(row.get('final_text'), str) or type(row.get('drop')) is not bool
                or row.get('confidence') not in ('高', '低') or type(row.get('affects_meaning')) is not bool
                or not isinstance(row.get('reason'), str) or not row['reason'].strip()):
            return 'row has missing or invalid decision fields'
        if row['state'] == 'KEEP' and (row['drop'] or row['final_text'] != original[row['id']]):
            return 'KEEP must preserve current text and cannot drop'
        if not row['drop'] and not row['final_text'].strip():
            return 'a kept cue needs CPA text'
    return ''


def _range(req, duration, *, before=False):
    a, b = req.get('start'), req.get('end')
    low, high = (-math.inf, duration + 30) if before else (0, duration)   # paid events reach back to the session start
    if type(a) not in (int, float) or type(b) not in (int, float) or not low <= a < b <= high:
        raise ValueError('invalid requested time range')
    return float(a), float(b)


class _Agent:
    def __init__(self, ctx, bcut, jianying, clip, cut_len):
        self.ctx, self.bcut, self.jianying, self.clip, self.cut_len = ctx, bcut, jianying, clip, cut_len
        self.started = time.monotonic()
        self.calls = []
        self.words = None
        self.uploaded = False
        self.jy = evidence_attach.attach_asr(bcut, evidence_attach.segments_from_srt_cues(jianying), source='jianying')
        # the work named by the host's on-screen bar (a lead; never fails the step). A run that already started keeps
        # the input it started with, so a saved UNAVAILABLE is not made again once the trace exists.
        self.work = work_context.build(ctx, cut_len=cut_len,
                                       retry_unavailable=not _local(ctx, 'subs/agent-trace.json').exists()) if work_context.enabled() else dict(work_context.OFF)
        self.base_prompt = '\n'.join((
            (PROMPTS / 'subtitle_agent.md').read_text(encoding='utf-8'),
            (PROMPTS / 'subtitle_playbook.md').read_text(encoding='utf-8'),
            (PROMPTS / 'subtitle_playbook_rules.md').read_text(encoding='utf-8'), PRONOUN_RULE, AGENT_PRINCIPLES,
            '初始转写与片长：' + json.dumps({'bcut': list(bcut), 'jianying': list(jianying),
                                             'cut_duration_s': cut_len, 'work_context': work_context.agent_view(self.work),
                                             'hook': clip.get('hook')}, ensure_ascii=False)))
        identity = json.dumps([self.base_prompt, clip, ctx.session], sort_keys=True, ensure_ascii=False)
        self.identity = hashlib.sha256(identity.encode()).hexdigest()
        self.path = _local(ctx, 'subs/agent-trace.json')
        self.trace = {'input_sha256': self.identity, 'events': [], 'text_calls': 0, 'tool_calls': 0,
                      'listening_calls': {'38m': 0, 'argon': 0}, 'total_tokens': 0,
                      'limits': {'text_calls': MAX_TEXT_CALLS, 'listening_calls': MAX_LISTEN_CALLS,
                                 'tool_calls': MAX_TOOL_CALLS,
                                 'total_tokens': MAX_TOTAL_TOKENS}, 'status': 'RUNNING'}
        for call in self.work['cpa']:           # the picture reading is a paid vision request of this clip
            self.calls.append(call)
            self.charge(call.get('usage'))      # unknown usage here never refuses the clip: it is a lead, not evidence
        self.trace['events'].append({'event': 'work_context', 'status': self.work['status'], 'cpa': self.work['cpa']})

    def save(self):
        self.trace['wall_seconds'] = round(time.monotonic() - self.started, 3)
        _write_atomic(self.path, json.dumps(self.trace, ensure_ascii=False, indent=1))

    def event(self, kind, **data):
        self.trace['events'].append({'event': kind, **data})
        self.save()

    def refuse(self, code, detail):
        self.trace.update(status='REFUSED', refusal=code, detail=detail)
        self.save()
        return _fail(code, detail, cpa=self.calls)

    def charge(self, usage):
        tokens = _token_count(usage)
        if tokens is None:
            return 'CPA_AGENT_USAGE_UNKNOWN'
        self.trace['total_tokens'] += tokens
        return 'CPA_AGENT_TOKEN_CAP' if self.trace['total_tokens'] >= MAX_TOTAL_TOKENS else ''

    def word_rows(self):
        if self.words is None:
            path = self.ctx.clip_dir / 'asr/jianying.json'
            self.words = pe.word_times(json.loads(path.read_text(encoding='utf-8'))) if path.exists() else []
        return self.words

    def listening(self, req):
        a, b = _range(req, self.cut_len)
        core = req.get('core', [a, b])
        if (not isinstance(core, list) or len(core) != 2 or any(type(x) not in (int, float) for x in core)
                or not a <= core[0] < core[1] <= b):
            raise ValueError('core must be inside the requested window')
        ids = req.get('cue_ids')
        valid_ids = {c['id'] for c in self.bcut}
        if not isinstance(ids, list) or not ids or any(type(i) is not int or i not in valid_ids for i in ids):
            raise ValueError('cue_ids must identify input cues')
        if not isinstance(req.get('reason'), str) or not req['reason'].strip():
            raise ValueError('listening request needs a reason')
        slug = req.get('model')
        if slug not in ('38m', 'argon'):
            raise ValueError('model must be 38m or argon')
        if slug == 'argon' and not any(r['model'] == '38m' and r['start'] < b and a < r['end']
                                       for r in self.trace.get('completed_listens', [])):
            raise ValueError('argon requires an overlapping completed 3.8 observation first')
        if sum(self.trace['listening_calls'].values()) >= MAX_LISTEN_CALLS:
            return None, 'CPA_AGENT_CALL_CAP'
        n = sum(self.trace['listening_calls'].values()) + 1
        window = {'win': f'AG{n:04d}', 'audio': [a, b], 'core': list(core), 'cue_ids': ids,
                  'tempo': 1., 'triggers': [{'reason': req['reason'], 'cue_ids': ids}]}
        model = M38 if slug == '38m' else ARGON
        if not self.uploaded:
            upload_listen_tools(self.ctx, 'listen_single.md')
            self.uploaded = True
        self.trace['listening_calls'][slug] += 1
        self.event('listen_dispatch', window=window, model=slug)
        records = _listen(self.ctx, [window], model, window['win'])
        if records is None or len(records) != 1:
            self.event('listen_failed', records=records)
            return None, 'CPA_AGENT_USAGE_UNKNOWN'
        suffix = hashlib.sha256(model['name'].encode()).hexdigest()[:12]
        remote = f"{self.ctx.remote_dir}/listen/out/audio/{window['win']}.{suffix}.receipt.json"
        local = _local(self.ctx, f"listen/receipts/{window['win']}.{slug}.json")
        try:
            self.ctx.adapters.remote.get(remote, local)
            receipt = json.loads(local.read_text(encoding='utf-8'))
        except (OSError, ValueError, RuntimeError) as exc:
            self.event('listen_receipt_missing', error=str(exc))
            if records[0].get('status') == 'OK':    # an answer was used but its spend is unknown: a real gap
                return None, 'CPA_AGENT_USAGE_UNKNOWN'
            # A provider failure (empty answer, quota ...) leaves no receipt and no usage figure in the listening
            # layer: hand the failure to CPA as a tool result; CPA decides whether to retry, move or go on.
            result = {'window': window, 'record': records[0], 'usage': None, 'usage_unavailable_on_error': True}
            self.event('listen_result', result=result)
            return result, ''
        problem = self.charge(receipt.get('usage'))
        result = {'window': window, 'record': records[0], 'usage': receipt.get('usage'),
                  'receipt': str(local)}
        earlier = records[0].get('attempts')
        if type(earlier) is int and earlier > 1:    # the receipt is the last try's; earlier failed tries have none
            result['earlier_failed_attempts_without_receipt'] = earlier - 1
        if records[0].get('status') == 'OK':
            self.trace.setdefault('completed_listens', []).append({'start': a, 'end': b, 'model': slug})
        else:
            raw_remote = f"{self.ctx.remote_dir}/listen/out/{window['win']}.{slug}.raw.txt"
            raw_local = _local(self.ctx, f"listen/raw/{window['win']}.{slug}.txt")
            try:
                self.ctx.adapters.remote.get(raw_remote, raw_local)
                raw = raw_local.read_text(encoding='utf-8')
            except (OSError, ValueError, RuntimeError) as exc:
                self.event('listen_raw_missing', error=str(exc))
                self.event('listen_result', result=result)
                return None, 'LISTEN_TOOL_FAILED'
            if hashlib.sha256(raw.encode('utf-8')).hexdigest() != receipt.get('text_sha256'):
                self.event('listen_raw_mismatch', path=str(raw_local))
                self.event('listen_result', result=result)
                return None, 'LISTEN_TOOL_FAILED'
            result['raw_response'] = raw
        self.event('listen_result', result=result)
        return result, problem

    def hints(self, ids=None):
        """The pipeline's own mechanical per-cue candidates (same functions and inputs as the evidence path);
        evidence for CPA to check, never a decision. `ids` None means every cue."""
        valid = {c['id'] for c in self.bcut}
        if ids is not None and (not isinstance(ids, list) or not ids
                                or any(type(i) is not int or i not in valid for i in ids)):
            raise ValueError('cue_ids must identify input cues')
        cues = [c for c in self.bcut if ids is None or c['id'] in ids]
        rows, unknown = pe.danmaku_evidence(self.ctx, self.cut_len)
        names = name_match.entries(self.ctx.session.get('participants') or [])
        known = {name for name, _ in names}
        from_work = [(name, ()) for name in self.work.get('names') or [] if name not in known]
        names += from_work
        spelled_by_work = {name for name, _ in from_work}
        spellings = {name: [name, *aliases] for name, aliases in names}
        pairs = mishearing.load_pairs()
        found = []
        for cue in cues:
            readings = [t for t in (cue['text'], self.jy.text(cue['id'])) if t]
            item = {'id': cue['id']}
            hits = pe.readout_matches(cue, readings, rows)
            if hits:
                item['readout_matches'] = hits
            near = list(dict.fromkeys(n for t in readings for n in name_match.near_names(t, names)))
            if near:
                item['near_names'] = [dict({'name': n, 'spellings': spellings[n]}, **({'source': 'work'}
                                                                                     if n in spelled_by_work else {}))
                                      for n in near]
            lines = mishearing.render_lines(mishearing.relevant_pairs(pairs, readings))
            if lines:
                item['mishearing'] = lines.split('\n')
            if len(item) > 1:
                found.append(item)
        result = {'note': HINTS_NOTE, 'checked_cues': [c['id'] for c in cues], 'hints': found}
        if unknown:
            result.update(unknown_segments=unknown, segments_note=SEGMENTS_NOTE)
        return result

    def self_check_hints(self):
        try:
            return self.hints()
        except (OSError, RuntimeError, ValueError) as exc:
            return {'note': HINTS_NOTE, 'error': f'{type(exc).__name__}: {exc}'}

    def lookup(self, req):
        tool = req.get('tool')
        if tool == 'danmaku':
            a, b = _range(req, self.cut_len, before=True)
            rows, unknown = pe.danmaku_evidence(self.ctx, self.cut_len)
            rows = [r for r in rows if a <= r['t_s'] <= b]
            return rows if not unknown else {'rows': rows, 'unknown_segments': unknown, 'note': SEGMENTS_NOTE}
        if tool == 'words':
            a, b = _range(req, self.cut_len)
            words = self.word_rows()
            return {'available': bool(words), 'note': '' if words else '没有逐词时间，不得编造',
                    'words': [dict(w, index=i) for i, w in enumerate(words)
                              if w['start_s'] < b and a < w['end_s']]}
        if tool == 'spelling':
            return step3_text_classes.spelling_rows()
        if tool == 'names':
            return self.ctx.session.get('participants') or []
        if tool == 'hints':
            return self.hints(req.get('cue_ids'))
        if tool == 'compare':
            ids = req.get('cue_ids')
            valid = {c['id'] for c in self.bcut}
            if not isinstance(ids, list) or not ids or any(type(i) is not int or i not in valid for i in ids):
                raise ValueError('cue_ids must identify input cues')
            return [{'id': c['id'], 'bcut': c['text'], 'jianying': self.jy.text(c['id']),
                     'slots': pe.slots(c['text'], self.jy.text(c['id'])), 'notes': self.jy.notes(c['id'])}
                    for c in self.bcut if c['id'] in ids]
        if tool == 'work':
            query = req.get('query')
            if not isinstance(query, str) or not query.strip():
                raise ValueError('work lookup needs a query')
            graph = json.loads(work_lookup.GRAPH_PATH.read_text(encoding='utf-8'))
            return work_context.with_characters(work_lookup.lookup(query, graph, web=self.ctx.adapters.lookup),
                                                self.ctx.adapters.lookup)
        raise ValueError('unknown tool')

    def tools(self, requests):
        if not isinstance(requests, list):
            return 'CPA_AGENT_PROTOCOL_ERROR'
        for req in requests:
            if self.trace['tool_calls'] >= MAX_TOOL_CALLS:
                return 'CPA_AGENT_CALL_CAP'
            self.trace['tool_calls'] += 1
            if not isinstance(req, dict):
                self.event('tool_result', request=req, result={'error': 'tool request must be an object'})
                continue
            self.event('tool_request', request=req)
            try:
                if req.get('tool') == 'listen':
                    result, problem = self.listening(req)
                    if problem:
                        return problem
                else:
                    result = self.lookup(req)
            except ValueError as exc:
                result = {'error': str(exc)}
            self.event('tool_result', request=req, result=result)
        return ''

    def ask(self):
        if self.trace['text_calls'] >= MAX_TEXT_CALLS:
            return None, 'CPA_AGENT_CALL_CAP'
        call_id = f"agent-{self.trace['text_calls'] + 1:03d}"
        prompt = self.base_prompt + '\n此前代理和工具轨迹：\n' + json.dumps(self.trace['events'], ensure_ascii=False)
        self.trace['text_calls'] += 1
        self.event('cpa_dispatch', call_id=call_id)
        call = _ask_cpa(self.ctx, call_id, prompt, purpose='subtitle-agent')   # the pipeline's own recovery tries
        self.calls.append(call)
        self.trace['text_calls'] += call['requests'] - 1     # the cap counts every request actually made
        rejected = call.get('rejected_answers', [])
        self.event('cpa_result', call_id=call_id, response=call.get('response_text'), usage=call.get('usage'),
                   rc=call.get('rc'), attempts=call.get('attempts'), requests=call['requests'],
                   rejected_answers=rejected, cached=call.get('cached', False),
                   purpose=call.get('purpose'), model=call.get('model'), effort=call.get('effort'),
                   max_output_tokens=call.get('max_output_tokens'), elapsed_s=call.get('elapsed_s'))
        problem = ''
        for usage in [r.get('usage') for r in rejected] + ([call.get('usage')] if _cpa_ok(call) else []):
            found = self.charge(usage)                  # a whole-but-rejected answer still spent its tokens
            problem = problem or found
        if not _cpa_ok(call):
            self.save()
            return None, 'CPA_CALL_FAILED'
        self.save()
        if problem:
            return None, problem
        try:
            result = _json_from_text(call['response_text'])
        except ValueError as exc:
            self.event('protocol_error', error=str(exc))
            return {}, ''
        return result, ''

    def resume(self, old):
        """Recover a complete recorded conversation; never resend an unknown dispatch."""
        if not isinstance(old, dict) or old.get('input_sha256') != self.identity or old.get('status') != 'RUNNING':
            return False
        events = old.get('events')
        if not isinstance(events, list):
            return False
        calls, pending_call, pending_tool, tokens, text_calls = [], None, None, 0, 0
        listen_counts = {'38m': 0, 'argon': 0}
        tool_count = 0
        for i, event in enumerate(events):
            if not isinstance(event, dict):
                return False
            kind = event.get('event')
            if kind == 'cpa_dispatch':
                if pending_call is not None or pending_tool is not None:
                    return False
                pending_call = event.get('call_id')
                if not isinstance(pending_call, str) or not re.fullmatch(r'agent-\d{3,}', pending_call):
                    return False
                prompt = self.base_prompt + '\n此前代理和工具轨迹：\n' + json.dumps(events[:i], ensure_ascii=False)
                path = self.ctx.clip_dir / f'cpa/{pending_call}.prompt.txt'
                if not path.is_file() or path.read_text(encoding='utf-8') != prompt:
                    return False
            elif kind == 'cpa_result':
                if event.get('call_id') != pending_call or event.get('rc') != 0:
                    return False
                path = self.ctx.clip_dir / f'cpa/{pending_call}.completion.txt'
                if not path.is_file() or path.read_text(encoding='utf-8') != event.get('response'):
                    return False
                usage = _token_count(event.get('usage'))
                if usage is None:
                    return False
                tokens += usage
                requests = event.get('requests')
                rejected = event.get('rejected_answers')
                if type(requests) is not int or requests < 1 or not isinstance(rejected, list):
                    return False
                spent = [_token_count(r.get('usage')) if isinstance(r, dict) else None for r in rejected]
                if None in spent:
                    return False
                tokens += sum(spent)
                text_calls += requests
                calls.append({'call_id': pending_call, 'rc': event['rc'], 'usage': event['usage'],
                              'attempts': event.get('attempts'), 'requests': requests,
                              'rejected_answers': rejected, 'cached': event.get('cached', False),
                              'response_text': event['response'],
                              **{k: event[k] for k in ('purpose', 'model', 'effort', 'max_output_tokens', 'elapsed_s')
                                 if k in event}})
                pending_call = None
            elif kind == 'work_context':
                for record in event.get('cpa') or []:
                    tokens += _token_count(record.get('usage')) or 0
                    calls.append(record)
            elif kind == 'tool_request':
                if pending_tool is not None or pending_call is not None or not isinstance(event.get('request'), dict):
                    return False
                pending_tool = event.get('request')
                tool_count += 1
            elif kind == 'tool_result':
                if pending_tool is None or event.get('request') != pending_tool:
                    return False
                if pending_tool.get('tool') == 'words' and isinstance(event.get('result'), dict):
                    current = self.word_rows()
                    if event['result'].get('available') != bool(current):
                        return False
                    for word in event['result'].get('words', []):
                        index = word.get('index')
                        if type(index) is not int or not 0 <= index < len(current):
                            return False
                        if dict(current[index], index=index) != word:
                            return False
                pending_tool = None
            elif kind == 'listen_dispatch':
                model = event.get('model')
                if pending_tool is None or not isinstance(model, str) or model not in listen_counts:
                    return False
                listen_counts[model] += 1
            elif kind == 'listen_result':
                found = event.get('result', {})
                if found.get('usage_unavailable_on_error') is True and found.get('usage') is None \
                        and isinstance(found.get('record'), dict) and found['record'].get('status') != 'OK':
                    continue
                usage = _token_count(found.get('usage'))
                if usage is None:
                    return False
                tokens += usage
        if (pending_call is not None or pending_tool is not None or text_calls != old.get('text_calls')
                or tokens != old.get('total_tokens') or listen_counts != old.get('listening_calls')
                or tool_count != old.get('tool_calls') or len(calls) > MAX_TEXT_CALLS
                or sum(listen_counts.values()) > MAX_LISTEN_CALLS or tool_count > MAX_TOOL_CALLS
                or tokens >= MAX_TOTAL_TOKENS):
            return False
        self.trace, self.calls = old, calls
        self.started = time.monotonic() - float(old.get('wall_seconds', 0))
        return True

    def decisions(self, answers):
        final_call = next(e['call_id'] for e in reversed(self.trace['events']) if e['event'] == 'cpa_result')
        rows = {str(r['id']): dict(r, cpa_call=final_call) for r in answers}
        doubts = []
        for cue in self.bcut:
            readings = {'bcut': cue['text'], 'jianying': self.jy.text(cue['id'])}
            ev = {'readings': readings, 'danmaku': [], 'asr_notes': self.jy.notes(cue['id'])}
            doubts.append(doubt_entry(cue, self.clip, ev, differing=[]))
        return rows, [_decision(d, rows[d['id']]) for d in doubts]

    def partition(self, answers):
        rows, final = self.decisions(answers)
        words = self.word_rows() if any('parts' in row for row in answers) else []
        supplied = {w['index'] for e in self.trace['events'] if e['event'] == 'tool_result'
                    and isinstance(e.get('request'), dict) and e['request'].get('tool') == 'words'
                    and isinstance(e.get('result'), dict) for w in e['result'].get('words', [])}
        for row in answers:
            for part in row.get('parts', []):
                if 'first_word' in part and (part['first_word'] not in supplied or part.get('last_word') not in supplied):
                    raise ValueError('selected word span was not supplied by a word-time lookup')
        original = ''.join(row['final_text'] for row in answers if not row['drop'])
        split = ''.join(''.join(part['text'] for part in row['parts']) if 'parts' in row else row['final_text']
                        for row in answers if not row['drop'])
        if _content(original) != _content(split):
            raise ValueError('selected parts do not preserve CPA final text')
        partitioned = step3_text_classes.partitioned_cues(list(self.bcut), final, rows, words, self.cut_len)
        return rows, final, partitioned

    def execute(self):
        planned = drafted = False
        if self.path.exists():
            try:
                old = json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return _fail('CPA_AGENT_EXISTING_TRACE', 'existing trace is unreadable and retained')
            if not self.resume(old):
                return _fail('CPA_AGENT_EXISTING_TRACE',
                             'existing trace retained; input differs, result is complete, or a dispatch is unknown')
            snapshot = _local(self.ctx, 'subs/agent-trace.pre-resume.json')
            if not snapshot.exists():
                _write_atomic(snapshot, self.path.read_text(encoding='utf-8'))
            planned = any(e['event'] == 'cpa_result' and _agent_action(e['response']) == 'plan'
                          for e in self.trace['events'])
            drafted = any(e['event'] == 'draft_recorded' for e in self.trace['events'])
            if 'final_rows' in self.trace:
                last = next(e for e in reversed(self.trace['events']) if e['event'] == 'cpa_result')
                answer = _json_from_text(last['response'])
                if (not drafted or not isinstance(answer, dict) or answer.get('action') != 'final'
                        or not str(answer.get('self_check') or '').strip()
                        or answer.get('rows') != self.trace['final_rows'] or _row_problem(answer['rows'], self.bcut)):
                    return _fail('CPA_AGENT_EXISTING_TRACE', 'saved final does not match the recorded CPA self-check')
                try:
                    self.partition(answer['rows'])
                except (ValueError, KeyError, TypeError) as exc:
                    del self.trace['final_rows']
                    self.event('protocol_error', error=f'invalid selected parts: {exc}')
                else:
                    self.event('resume_final', note='verified recorded CPA final; no new model call')
                    outcome = self.finish(answer['rows'])
                    self.trace['status'] = 'COMPLETE'
                    self.save()
                    return outcome
        else:
            self.save()
        while True:
            answer, problem = self.ask()
            if problem:
                return self.refuse(problem, 'CPA request failed, exceeded the per-clip guard, or lacked usage')
            if not isinstance(answer, dict):
                self.event('protocol_error', error='answer must be a JSON object')
                continue
            action = answer.get('action')
            if action == 'plan' and not planned and isinstance(answer.get('doubts'), list):
                planned = True
                problem = self.tools(answer.get('tools', []))
            elif action == 'tools' and planned:
                problem = self.tools(answer.get('tools'))
            elif action in ('draft', 'final') and planned:
                problem = _row_problem(answer.get('rows'), self.bcut)
                if not problem and any('parts' in row for row in answer['rows']) and self.words is None:
                    problem = 'request the word-time evidence before selecting word spans'
                if not problem and action == 'final' and (not drafted or not isinstance(answer.get('self_check'), str)
                                                           or not answer['self_check'].strip()):
                    problem = 'final requires an earlier draft and a self-check'
                if problem:
                    self.event('protocol_error', error=problem)
                    continue
                if action == 'draft':
                    drafted = True
                    if any(e['event'] == 'draft_recorded' and 'hints' in e for e in self.trace['events']):
                        self.event('draft_recorded', instruction=SELF_CHECK_INSTRUCTION + ' hints 见此前的 draft_recorded 记录。')
                    else:
                        self.event('draft_recorded', instruction=SELF_CHECK_INSTRUCTION,
                                   hints=self.self_check_hints())
                    continue
                try:
                    self.partition(answer['rows'])
                except (ValueError, KeyError, TypeError) as exc:
                    self.event('protocol_error', error=f'invalid selected parts: {exc}')
                    continue
                self.trace['final_rows'] = answer['rows']
                self.save()
                outcome = self.finish(answer['rows'])
                self.trace['status'] = 'COMPLETE'
                self.save()
                return outcome
            else:
                self.event('protocol_error', error='first produce a plan; then tools, draft, and final')
                continue
            if problem:
                return self.refuse(problem, 'requested evidence failed or the per-clip guard was reached')

    def finish(self, answers):
        _rows, final, partitioned = self.partition(answers)
        dp, fp, norm = write_subtitles(self.ctx, self.bcut, final, preserve_decisions=True)
        retained_unsplit = False
        if partitioned is not None:
            shaped, lone = normalize.release_shape(partitioned, one_speaker=True)
            before = _content(''.join(cue['text'] for cue in partitioned))
            after = _content(''.join(cue['text'] for cue in shaped))
            if not any(row['action'] == 'dropped' for row in lone) and before == after:
                norm['lone_chars'] = lone
                fp.write_text(format_srt(normalize.shape_decided_cues(shaped)), encoding='utf-8')
            else:
                retained_unsplit = True
        accounting = {k: self.trace[k] for k in ('text_calls', 'listening_calls', 'total_tokens', 'wall_seconds')}
        accounting['holds'] = [{'id': d['id'], 'text': d['final_text'], 'current': d['current']}
                               for d in doubt_policy.holds(final)]
        _save(self.ctx, 'subs/playbook-accounting.json', accounting)
        process = [f'单人 CPA 代理自主取证与自查：{accounting}']
        if retained_unsplit:
            process.append('分句排版会改变或丢失 CPA 文字，保留其原有未分句结果。')
        lone_and_holds(self.ctx, norm, final, process)
        return gated_outcome(self.ctx, final, self.calls, process,
                             products={'doubts': str(dp), 'final_srt': str(fp), 'agent_trace': str(self.path)},
                             extra_products={}, note='CPA 自选证据、自查后逐句裁决')


def run(ctx, *, bcut, jianying, clip, cut_len):
    return _Agent(ctx, bcut, jianying, clip, cut_len).execute()
