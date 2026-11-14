"""Single-speaker step 3: CPA selects evidence and reviews its own decisions."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time

from . import (doubt_policy, evidence_attach, mishearing, name_match, normalize, parallel,
               playbook_evidence as pe, playbook_vote, references, step3_materials,
               step3_text_classes, work_context, work_lookup)
from .lane_rules import rules
from .srt_text import _json_from_text, format_srt
from .step3_doubts import decided_doubt, doubt_entry, gated_outcome, lone_and_holds, write_subtitles
from .step3_evidence import ARGON, M38, _asr_window_pass, _asr_witnesses, _listen_pass, upload_listen_tools
from .step_io import PROMPTS, _ask_cpa, _cpa_ok, _fail, _local, _write_atomic


# These are runaway guards, not a target number of investigations. Usage includes both providers.
MAX_TEXT_CALLS = 80
MAX_LISTEN_CALLS = 160
MAX_TOOL_CALLS = 500
MAX_TOTAL_TOKENS = 4_000_000
HINTS_NOTE = ('机械候选，可能有误，只是证据：readout_matches 是同期弹幕/SC 中可能被念出或回响的原记录'
              '（relation=readout_candidate 为句前或句内已发出）；near_names 是句中与名字表拼音相近而写法不同的名字；'
              'sender_of_message_read_next 是紧接着被念出正文的那条付费留言的发送者：她通常在念正文前或同一句里叫这个名字致谢，'
              '这一带若有一段听不成词、又与该名字音节数相近或部分同音的称呼，多半就是它（平台后缀不念）；'
              '若她清楚地叫的是另一个短称呼，照她叫的写；'
              'mishearing 是频道已知误听对；begins_like_a_known_mishearing 是有的识别在这里写出了某个已知误听写法的开头（heard），'
              '有的识别写了别的，那个误听写法（known_mishearing）代表 standard。各家在这一处互不相同，就没有哪一家的写法算'
              '“现稿”可以原样保留：在 standard 和各家写法里，取最能同时解释各家音形、音节数和这句话在说谁的那个；'
              'standard 讲得通而各家的写法放在句里讲不通时写 standard（可标低把握），不因“不足以证明”留下一家的错听；'
              '带 chat_just_before 的，是句前一分半钟内有观众在弹幕里写了 standard：她多半正在回应这条弹幕，这时写 standard；'
              'only_one_recogniser_wrote 是夹在这一句里、只有 BCUT 写了而其余识别器同一位置都没有对应文字的那几个字：'
              '其余几家把这句话写出来了却都没有它，它多半是背景声或误触发，不是这句话的一部分——去掉这几个字，'
              '不把它改写或扩写成别的词句（包括复听听出的外语台词）；只有它是主播清楚说出的实词时才保留。'
              '由你核对后决定，系统不会替你改字。')
UNSUBTITLED_NOTE = ('unsubtitled 里的时间段，识别器有成句的内容，而你的字幕在这段时间没有任何一行。这段话若已并入邻行，'
                    '接收行要用 parts 把它的时间带过去：有 source_words 的用 first_word/last_word；没有词时间的文字'
                    '（外语原声、被识别成怪音串的句子）用 {"text":"…","first_cue":a,"last_cue":b}，a、b 是 bcut 的 cue id，'
                    '含首尾，时间取这些 cue 的整段边界。这段若确实不该出字幕，在对应行的 reason 里写明它是什么声音，'
                    '再原样交 final。')
SEGMENTS_NOTE = ('这些同场录像分段读取失败或无法对齐时钟，其中的 SC/礼物/舰长是未知，不是没有；'
                 '可再请求，或按其余证据判断，不得当作不存在。')
SELF_CHECK_INSTRUCTION = ('交叉核对全部证据；需要时再请求工具，然后修订。附上的 hints 是流水线已算好的逐句机械候选，'
                          '可能有误，由你核对后决定。凡命中 readout_candidate 的句，若最终文字与原记录不同，'
                          '须在 self_check 里逐句写明偏离了哪些字、意思是否改变；意思改变的视为听错，改按原记录。')


def _save(ctx, name, obj):
    path = _local(ctx, name)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    return path


def _listen(ctx, windows, model, label, on_quota_wait=None):
    """Dispatch only windows selected by CPA; keep each exact request."""
    template = (PROMPTS / 'listen_single.md').read_text(encoding='utf-8')
    for window in windows:
        lead = window['core'][0] - window['audio'][0]
        tail = window['audio'][1] - window['core'][1]
        prompt = template.replace('{lead}', f'{lead:.1f}').replace('{tail}', f'{tail:.1f}')
        _save(ctx, f"listen/requests/{window['win']}-{model['slug']}.json",
              {'model': model, 'prompt': prompt, 'audio': window['audio'],
               'core': window['core'], 'tempo': window['tempo'], 'triggers': window['triggers']})
    return _listen_pass(ctx, label, windows, [model], prompt='listen_single.md', prepared=True,
                        on_quota_wait=on_quota_wait)


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


_CLIP_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_CLIP_LOCKS_GUARD = threading.Lock()


def _clip_lock(ctx, what):
    """The runs of one clip share the clip's files: one listening pass at a time (the remote listening directory),
    one reading of the session chat at a time (its downloaded segments)."""
    with _CLIP_LOCKS_GUARD:
        return _CLIP_LOCKS.setdefault((str(ctx.clip_dir), what), threading.Lock())


class _Agent:
    def __init__(self, ctx, bcut, jianying, clip, cut_len, tag='', others=None):
        """`tag` names one of several independent runs on the same clip ('' for the first, then a letter): its trace,
        its CPA call ids and its listening windows carry the tag, so the runs never share a file."""
        self.ctx, self.bcut, self.jianying, self.clip, self.cut_len = ctx, bcut, jianying, clip, cut_len
        self.tag = tag
        self.others = others or {}          # whole-clip readings of the other recognisers: {'mai': [...], 'moss': [...]}
        self.started = time.monotonic()
        self.calls = []
        self.words = None
        self.uploaded = self.argon_down = False
        self.jy = evidence_attach.attach_asr(bcut, evidence_attach.segments_from_srt_cues(jianying), source='jianying')
        # every other recogniser's reading divided among the cues by the lane's one rule (evidence_attach.segment_slices)
        self.at_cue = {**({'jianying': self.jy} if jianying else {}),
                       **{name: evidence_attach.attach_asr(bcut, lines, source=name) for name, lines in self.others.items()}}
        # the work named by the host's on-screen bar (a lead; never fails the step). A run that already started keeps
        # the input it started with, so a saved UNAVAILABLE is not made again once the trace exists.
        self.work = work_context.build(ctx, cut_len=cut_len,
                                       retry_unavailable=not _local(ctx, 'subs/agent-trace.json').exists()) if work_context.enabled() else dict(work_context.OFF)
        self.refs = references.for_subtitles(ctx, bcut)      # what the draft refers to outside itself (a lead; never fails)
        self.base_prompt = step3_materials.base_prompt(bcut, jianying, self.others, cut_len, clip, self.work, self.refs,
                                                       ctx.clip_dir.parent.parent)
        identity = json.dumps([self.base_prompt, clip, ctx.session], sort_keys=True, ensure_ascii=False)
        self.identity = hashlib.sha256(identity.encode()).hexdigest()
        self.path = _local(ctx, f"subs/agent-trace{'.' + tag if tag else ''}.json")
        self.trace = {'input_sha256': self.identity, 'events': [], 'text_calls': 0, 'tool_calls': 0,
                      'listening_calls': {'38m': 0, 'argon': 0}, 'total_tokens': 0,
                      'limits': {'text_calls': MAX_TEXT_CALLS, 'listening_calls': MAX_LISTEN_CALLS,
                                 'tool_calls': MAX_TOOL_CALLS,
                                 'total_tokens': MAX_TOTAL_TOKENS}, 'status': 'RUNNING'}
        for call in [*self.work['cpa'], *self.refs['cpa']]:   # the picture reading and the reference lookup are paid
            self.calls.append(call)
            self.charge(call.get('usage'))      # unknown usage here never refuses the clip: it is a lead, not evidence
        self.trace['events'].append({'event': 'work_context', 'status': self.work['status'], 'cpa': self.work['cpa'],
                                     'references': self.refs['line'],
                                     **({'run': tag} if tag else {})})   # a run's prompts are its own: an identical
        # first prompt would be answered from the clip's answer cache with another run's plan

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

    def other_recognisers(self, window):
        """The window recognised by the two recognisers that are neither BCUT, jianying nor Gemini (MAI, MOSS): what each
        heard, or that it failed.  No CPA tokens; a failure is a result, not a refusal."""
        with _clip_lock(self.ctx, 'witness'):
            records = _asr_window_pass(self.ctx, window['win'], [window], ('mai', 'moss'), stats_out={})
        if records is None:
            return [{'status': 'TOOL_FAILED'}]
        return [{'recogniser': r.get('provider'), 'status': r.get('status'),
                 'text': ''.join(str(seg.get('text') or '') for seg in r.get('segments') or []),
                 **({'error': str(r.get('error'))[:200]} if r.get('status') != 'OK' and r.get('error') else {})}
                for r in records]

    def chat(self):
        with _clip_lock(self.ctx, 'chat'):
            return pe.danmaku_evidence(self.ctx, self.cut_len)

    def _window_request(self, req):
        """A checked listening request: (start, end, core, cue ids, model)."""
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
        if slug == 'argon' and self.argon_down:
            raise ValueError('argon cannot be heard now; decide this from the other evidence')
        if slug == 'argon' and not any(r['model'] == '38m' and r['start'] < b and a < r['end']
                                       for r in self.trace.get('completed_listens', [])):
            raise ValueError('argon requires an overlapping completed 3.8 observation first')
        return a, b, list(core), ids, slug

    def _hear(self, window, model, slug):
        """One Gemini listening pass of one window (a rate limit is waited out inside the pass, step3_evidence)."""
        with _clip_lock(self.ctx, 'listen'):
            return _listen(self.ctx, [window], model, window['win'],
                           lambda seconds, _n: self.event('listen_quota_wait', window=window['win'], model=slug,
                                                          seconds=round(seconds)))

    def listening(self, req):
        a, b, core, ids, slug = self._window_request(req)
        window = {'audio': [a, b], 'core': core, 'cue_ids': ids, 'tempo': 1.,
                  'triggers': [{'reason': req['reason'], 'cue_ids': ids}]}
        if sum(self.trace['listening_calls'].values()) >= MAX_LISTEN_CALLS:
            return None, 'CPA_AGENT_CALL_CAP'
        n = sum(self.trace['listening_calls'].values()) + 1
        window = {'win': f'AG{self.tag.upper()}{n:04d}', **window}
        model = M38 if slug == '38m' else ARGON
        if not self.uploaded:
            upload_listen_tools(self.ctx, 'listen_single.md')
            self.uploaded = True
        self.trace['listening_calls'][slug] += 1
        self.event('listen_dispatch', window=window, model=slug)
        records = self._hear(window, model, slug)
        if records is None or len(records) != 1:
            self.event('listen_failed', records=records)
            return None, 'CPA_AGENT_USAGE_UNKNOWN'
        suffix = hashlib.sha256(records[0].get('model', model['name']).encode()).hexdigest()[:12]
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
            if records[0].get('status') in ('QUOTA', 'UNAVAILABLE'):    # cannot be heard now: not left unheard
                result['other_recognisers'] = self.other_recognisers(window)
                self.argon_down = self.argon_down or (slug, records[0]['status']) == ('argon', 'UNAVAILABLE')
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
        rows, unknown = self.chat()
        names = name_match.entries(self.ctx.session.get('participants') or [])
        known = {name for name, _ in names}
        from_work = [(name, ()) for name in self.work.get('names') or [] if name not in known]
        names += from_work
        spelled_by_work = {name for name, _ in from_work}
        spellings = {name: [name, *aliases] for name, aliases in names}
        pairs = mishearing.load_pairs()
        found = []
        heard = {cue['id']: [t for t in (cue['text'], self.jy.text(cue['id'])) if t] for cue in self.bcut}
        all_hits = {cue['id']: pe.readout_matches(cue, heard[cue['id']], rows) for cue in self.bcut}
        thanked = pe.senders_read_next(list(self.bcut), all_hits, heard)
        wrote = {cue['id']: {'bcut': cue['text'], **{name: lines.text(cue['id']) for name, lines in self.at_cue.items()}}
                 for cue in self.bcut}                  # what every recogniser wrote at each cue
        begun = pe.begun_mishearings({k: [t for t in v.values() if t] for k, v in wrote.items()}, pe.misheard_heads(pairs))
        for cue in cues:
            readings = heard[cue['id']]
            item = {'id': cue['id']}
            hits = all_hits[cue['id']]
            if hits:
                item['readout_matches'] = hits
            if cue['id'] in thanked:
                item['sender_of_message_read_next'] = thanked[cue['id']]
            near = list(dict.fromkeys(n for t in readings for n in name_match.near_names(t, names)))
            if near:
                item['near_names'] = [dict({'name': n, 'spellings': spellings[n]}, **({'source': 'work'}
                                                                                     if n in spelled_by_work else {}))
                                      for n in near]
            lines = mishearing.render_lines(mishearing.relevant_pairs(pairs, readings))
            if lines:
                item['mishearing'] = lines.split('\n')
            if cue['id'] in begun:
                item['begins_like_a_known_mishearing'] = [
                    dict(row, **pe.said_in_chat_before(row['standard'], cue, rows)) for row in begun[cue['id']]]
            alone = pe.written_by_one(cue['text'], [t for name, t in wrote[cue['id']].items() if name != 'bcut'])
            if alone:
                item['only_one_recogniser_wrote'] = alone
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
            rows, unknown = self.chat()
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
            except (RuntimeError, OSError) as exc:      # a lookup whose source cannot be read: unknown, not absent
                if req.get('tool') == 'listen':
                    raise
                result = {'error': f'读取失败，结果未知（不是没有）：{type(exc).__name__}: {exc}'[:400]}
            self.event('tool_result', request=req, result=result)
        return ''

    def ask(self):
        if self.trace['text_calls'] >= MAX_TEXT_CALLS:
            return None, 'CPA_AGENT_CALL_CAP'
        call_id = f"agent{self.tag}-{self.trace['text_calls'] + 1:03d}"
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
                if not isinstance(pending_call, str) or not re.fullmatch(rf'agent{re.escape(self.tag)}-\d{{3,}}', pending_call):
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

    def unsubtitled(self, answers):
        """The stretches of real speech these rows would leave without a subtitle: the package step's own check
        (package_guards.gaps_with_speech), asked here while the agent can still give the sentence its time."""
        from .package_guards import gaps_with_speech

        _rows, final, partitioned = self.partition(answers)
        dropped = {d['cue_id'] for d in final if doubt_policy.is_dropped(d)}
        shown = partitioned if partitioned is not None else [c for c in self.bcut if c['id'] not in dropped]
        return gaps_with_speech(sorted(shown, key=lambda c: (c['start_s'], c['end_s'])), list(self.bcut))

    def final_stands(self, answers):
        """Whether these final rows can be taken; when not, what is wrong has been recorded for the agent to answer."""
        try:
            gaps = self.unsubtitled(answers)
        except (ValueError, KeyError, TypeError) as exc:
            self.event('protocol_error', error=f'invalid selected parts: {exc}')
            return False
        asked = [g for e in self.trace['events'] for g in e.get('unsubtitled') or []]
        if gaps and not asked:                            # said once; then its answer stands
            self.event('protocol_error', unsubtitled=gaps, error=UNSUBTITLED_NOTE)
            return False
        self.trace['unsubtitled_kept'] = asked            # asked by name and kept without a line: the package step reads this
        return True

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

    def _pick_up(self, finish):
        """An earlier attempt's trace.  Returns (result, planned, drafted): `result` is the step's outcome or the rows
        when nothing is left to ask (also a refusal), else None and the conversation goes on from where it stopped."""
        if not self.path.exists():
            self.save()
            return None, False, False
        try:
            old = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return _fail('CPA_AGENT_EXISTING_TRACE', 'existing trace is unreadable and retained'), False, False
        if (not finish and isinstance(old, dict) and old.get('status') == 'COMPLETE'
                and old.get('input_sha256') == self.identity and not _row_problem(old.get('final_rows'), self.bcut)):
            self.trace = old              # one of several runs, finished in an earlier attempt of the step: its
            if self.final_stands(old['final_rows']):      # recorded rows stand; nothing is asked or charged again
                return old['final_rows'], True, True
            self.trace['status'] = 'RUNNING'              # ... unless they leave speech without a subtitle: the
            del self.trace['final_rows']                  # conversation goes on from the note just recorded
            self.save()
            return None, True, True
        if not self.resume(old):
            return _fail('CPA_AGENT_EXISTING_TRACE',
                         'existing trace retained; input differs, result is complete, or a dispatch is unknown'), False, False
        snapshot = _local(self.ctx, f"subs/agent-trace{'.' + self.tag if self.tag else ''}.pre-resume.json")
        if not snapshot.exists():
            _write_atomic(snapshot, self.path.read_text(encoding='utf-8'))
        planned = any(e['event'] == 'cpa_result' and _agent_action(e['response']) == 'plan'
                      for e in self.trace['events'])
        drafted = any(e['event'] == 'draft_recorded' for e in self.trace['events'])
        if 'final_rows' not in self.trace:
            return None, planned, drafted
        last = next(e for e in reversed(self.trace['events']) if e['event'] == 'cpa_result')
        answer = _json_from_text(last['response'])
        if (not drafted or not isinstance(answer, dict) or answer.get('action') != 'final'
                or not str(answer.get('self_check') or '').strip()
                or answer.get('rows') != self.trace['final_rows'] or _row_problem(answer['rows'], self.bcut)):
            return _fail('CPA_AGENT_EXISTING_TRACE', 'saved final does not match the recorded CPA self-check'), planned, drafted
        try:
            self.partition(answer['rows'])
        except (ValueError, KeyError, TypeError) as exc:
            del self.trace['final_rows']
            self.event('protocol_error', error=f'invalid selected parts: {exc}')
            return None, planned, drafted
        self.event('resume_final', note='verified recorded CPA final; no new model call')
        return self._settle(answer['rows'], finish), planned, drafted

    def _settle(self, rows, finish):
        outcome = self.finish(rows) if finish else rows
        self.trace['status'] = 'COMPLETE'
        self.save()
        return outcome

    def execute(self, finish=True):
        """The agent's conversation to its checked final rows.  With `finish` the subtitles are written and the step
        outcome returned; without it the rows are returned (a list) for the caller to decide among several runs."""
        result, planned, drafted = self._pick_up(finish)
        if result is not None:
            return result
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
                if not self.final_stands(answer['rows']):
                    continue
                self.trace['final_rows'] = answer['rows']
                self.save()
                return self._settle(answer['rows'], finish)
            else:
                self.event('protocol_error', error='first produce a plan; then tools, draft, and final')
                continue
            if problem:
                return self.refuse(problem, 'requested evidence failed or the per-clip guard was reached')

    def finish(self, answers):
        from .package_guards import record_decided
        record_decided(self.ctx.clip_dir, self.unsubtitled(answers), self.trace.get('unsubtitled_kept') or [])
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


def samples() -> int:
    """How many independent runs decide a clip (lane_rules subtitle_agent.samples)."""
    try:
        return max(1, int(rules('subtitle_agent').get('samples') or 1))
    except KeyError:                      # a rules file without the section (an old experiment copy)
        return 1


def other_recognitions(ctx):
    """The whole clip as the lane's other recognisers heard it (MAI, MOSS; once per clip, cached on the media host):
    {'mai': [{'start_s', 'end_s', 'text'}], ...} for those that answered.  One that is unavailable is simply absent."""
    lines, _status = _asr_witnesses(ctx)
    return {name.split(':', 1)[1]: [{'start_s': round(float(seg['abs_start_s']), 2), 'end_s': round(float(seg['end_s']), 2),
                                     'text': seg['text']} for seg in segs]
            for name, segs in lines.items() if segs}


def run(ctx, *, bcut, jianying, clip, cut_len):
    count = samples()
    others = other_recognitions(ctx)
    if count == 1:
        return _Agent(ctx, bcut, jianying, clip, cut_len, others=others).execute()
    agents = [_Agent(ctx, bcut, jianying, clip, cut_len, tag=tag, others=others) for tag in ('', *'bcdefgh'[:count - 1])]
    upload_listen_tools(ctx, 'listen_single.md')          # once, before the runs start
    for agent in agents:
        agent.uploaded = True
    results = parallel.run(agents, lambda agent: agent.execute(finish=False), provider='subtitle_agent')
    done = [(agent, rows) for agent, rows in zip(agents, results, strict=True) if isinstance(rows, list)]
    if not done:
        return results[0]                                  # every run failed: the first run's failure
    rows, record = playbook_vote.merge([r for _agent, r in done])
    lead = done[record['lead']][0]
    try:
        problem = _row_problem(rows, bcut)
        if problem:
            raise ValueError(problem)
        lead.partition(rows)
    except (ValueError, KeyError, TypeError) as exc:       # the majority cannot be laid out: the lead's own rows stand
        rows, record['fallback_to_lead'] = done[record['lead']][1], str(exc)
    record['runs'] = [{'tag': agent.tag, 'status': agent.trace['status'], 'text_calls': agent.trace['text_calls'],
                       'listening_calls': agent.trace['listening_calls'], 'total_tokens': agent.trace['total_tokens'],
                       'refusal': agent.trace.get('refusal')} for agent in agents]
    record['lead'] = lead.tag
    _save(ctx, 'subs/agent-vote.json', record)
    lead.calls = [call for agent in agents for call in agent.calls]
    outcome = lead.finish(rows)
    if outcome.ok:      # every run's trace is the step's product: `redo` removes them all, so no old run is taken up by new code
        outcome.products.update({f"agent_trace_{agent.tag or 'a'}": str(agent.path) for agent in agents if agent.path.exists()})
    outcome.process.append(f"字幕代理并行 {count} 遍（完成 {len(done)} 遍），逐字取多数：以第 {record['lead'] or 'a'} 遍为底，"
                           f"{len(record['applied'])} 处改按多数写法、{len(record['unapplied'])} 处多数写法未能写入"
                           + ('；改后排不进分句，沿用底稿' if 'fallback_to_lead' in record else ''))
    outcome.process.extend(filter(None, [*playbook_vote.risk_lines(rows), lead.refs['line']]))
    return outcome
