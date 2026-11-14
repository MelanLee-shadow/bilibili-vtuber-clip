"""Ordinary focused recovery with the real CPA adapter and a fake Remote/SSE transport."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path

import pytest
from fakes import FakeRemote
from test_cpa_completeness import HEAD, _load, _message, _Resp, _sse
from test_review_doubt_authority import _focused, _remove_report, _row, _runner

from src.autoslice.manual_lane import adapters, doubt_policy, stage_package as sp
from src.autoslice.manual_lane.adapters import LocalRemote, RemoteResult, SshCpaJudge
from src.autoslice.manual_lane.runner import run_clip


@pytest.fixture(autouse=True)
def _isolate_terminal_recovery_from_post_review_listening(monkeypatch):
    """Keep the real-adapter terminal recovery controls focused on the original request."""
    monkeypatch.setattr(sp, "_finish_or_wide", lambda ctx, record, process:
                        sp._finish(ctx, cpa=record["cpa"], process=process))


def _complete_stream(text):
    return _sse(HEAD + [{'type': 'response.output_text.delta', 'delta': text},
                        {'type': 'response.output_text.done', 'text': text},
                        {'type': 'response.output_item.done', 'item': _message(text, 'completed')},
                        {'type': 'response.completed', 'response': {'status': 'completed',
                         'output': [_message(text, 'completed')],
                         'usage': {'input_tokens': 37, 'output_tokens': 5}}}])


class SseRemote(FakeRemote):
    """Real cpa_text serializer and real adapter; only the Remote and network are synthetic."""

    def __init__(self, tmp_path, monkeypatch, responses):
        super().__init__()
        self.tmp = tmp_path
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.streams = [_complete_stream(text) for text in responses]
        self.requests = []
        self.crash_after = set()
        self.recovery_rc = None
        self.cpa_text = _load(adapters.CPA_TEXT_SCRIPT, 'cpa_terminal_recovery_guard')
        monkeypatch.setattr(self.cpa_text.urllib.request, 'urlopen', self._urlopen)
        monkeypatch.setattr(self.cpa_text.time, 'sleep', lambda _s: None)
        monkeypatch.setenv('CPA_BASE_URL', 'https://cpa.invalid/v1')
        monkeypatch.setenv('CPA_API_KEY', 'test-only')

    def _urlopen(self, request, timeout):
        self.requests.append(json.loads(request.data))
        return _Resp(self.streams.pop(0))

    def run_script(self, *, name, body, timeout):
        if name.startswith('cpa-recover-'):
            if self.recovery_rc is not None:
                return RemoteResult(self.recovery_rc, '', 'synthetic read error')
            return super().run_script(name=name, body=body, timeout=timeout)
        if name.startswith('cpa-read-'):
            call_id = name.removeprefix('cpa-read-')
            out = self.files.get(f'/r/clip/cpa/{call_id}.out.json')
            return RemoteResult(0, out.decode('utf-8'), '') if out is not None else RemoteResult(1, '', '')
        if name.startswith('cpa-'):
            call_id = name.removeprefix('cpa-')
            wire = body.split("<<'MANUAL_LANE_PROMPT_EOF'\n", 1)[1].split('\nMANUAL_LANE_PROMPT_EOF', 1)[0] + '\n'
            flags = re.search(r'--model\s+\S+\s+--effort\s+\S+\s+--tries\s+\d+\s+--stream', body)
            assert flags is not None
            prompt, out = self.tmp / f'{call_id}.prompt', self.tmp / f'{call_id}.out.json'
            prompt.write_bytes(wire.encode('utf-8'))
            remote_prompt, remote_out = f'/r/clip/cpa/{call_id}.prompt.txt', f'/r/clip/cpa/{call_id}.out.json'
            self.files[remote_prompt] = prompt.read_bytes()
            self.files.pop(remote_out, None)  # the ordinary adapter's existing rm -f semantics
            rc = self.cpa_text.main([str(prompt), str(out), *shlex.split(flags.group())])
            self.files[remote_out] = out.read_bytes()
            if call_id in self.crash_after:
                self.crash_after.remove(call_id)
                raise RuntimeError('合成：远端完整终态落盘后中断')
            return RemoteResult(rc, '', '')
        return super().run_script(name=name, body=body, timeout=timeout)


def _start(tmp_path, monkeypatch, *, crash=True, focused_prompt_extra=''):
    main = json.dumps({'fixes': {}, 'holds': [], 'notes': [], 'review_md': '合成旧复审未判疑点'}, ensure_ascii=False)
    focused = _focused()
    ctx, steps, _, counts, _ = _runner(tmp_path / 'clip', monkeypatch, main)
    remote = SseRemote(tmp_path / 'remote', monkeypatch, [main, focused, focused])
    ctx.adapters.remote = remote
    ctx.adapters.cpa = SshCpaJudge(remote)
    with monkeypatch.context() as old:
        old.setattr(sp, 'review_prompt', lambda _ctx: '合成旧请求')
        assert run_clip(ctx, steps).stopped_at is None
    main_event = dict(ctx.state.data['review']['cpa'][0])
    main_files = [Path(main_event['cache_path']), Path(main_event['file'] + '.prompt.txt'),
                  Path(main_event['file'] + '.completion.txt')]
    main_original = {p: p.read_bytes() for p in main_files}
    _remove_report(ctx)
    if focused_prompt_extra:
        ctx.state.data['clip']['hook'] = focused_prompt_extra
        ctx.state.save()
    if crash:
        remote.crash_after.add('review-held')
        first = run_clip(ctx, steps)
        assert first.stopped_at == 9 and len(remote.requests) == 2
        event = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
        assert not Path(event['cache_path']).exists()
    return ctx, steps, counts, remote, main_original


def test_real_adapter_recovers_matching_terminal_without_dispatch_and_preserves_originals(tmp_path, monkeypatch):
    ctx, steps, counts, remote, main_original = _start(tmp_path, monkeypatch)
    out_path, prompt_path = '/r/clip/cpa/review-held.out.json', '/r/clip/cpa/review-held.prompt.txt'
    out_before, wire_before = remote.files[out_path], remote.files[prompt_path]
    payload = json.loads(out_before)
    local_prompt = (ctx.clip_dir / 'cpa/review-held.prompt.txt').read_bytes().decode('utf-8')
    assert wire_before == (local_prompt + '\n').encode('utf-8')
    assert payload['prompt_sha256'] == hashlib.sha256(wire_before.decode().replace('\r\n', '\n').replace('\r', '\n').encode()).hexdigest()
    assert (payload['model'], payload['effort'], payload['stream']) == ('gpt-6.1-sol', 'high', True)
    (ctx.clip_dir / 'pkg/description.txt').write_text('恢复时材料变化', encoding='utf-8')
    resumed = run_clip(ctx, steps)
    assert resumed.stopped_at is None and len(remote.requests) == 2 and not doubt_policy.is_hold(_row(ctx))
    assert remote.files[out_path] == out_before and remote.files[prompt_path] == wire_before
    assert all(p.read_bytes() == data for p, data in main_original.items())
    calls = ctx.state.data['review']['cpa']
    assert [c['call_id'] for c in calls] == ['review', 'review-held']
    event = calls[1]
    assert event['cached'] is True and event['purpose'] == 'review'
    assert event['attempts'] == payload['attempts'] and event['usage'] == payload['usage']
    assert event['http_log'] == payload['http_log'] and event['elapsed_s'] == payload['elapsed_seconds']
    cached = json.loads(Path(event['cache_path']).read_text(encoding='utf-8'))
    assert cached['http_log'] == payload['http_log'] and cached['response_text'] == payload['text']
    assert Path(event['file'] + '.completion.txt').read_text(encoding='utf-8') == payload['text']
    assert counts == {'burn': 1, 'tags': 1, 'package': 2}
    _remove_report(ctx)
    assert run_clip(ctx, steps).stopped_at is None and len(remote.requests) == 2
    assert remote.files[out_path] == out_before and remote.files[prompt_path] == wire_before


def test_raw_crlf_wire_is_exact_but_provider_sha_uses_universal_newlines(tmp_path, monkeypatch):
    ctx, steps, _, remote, _ = _start(tmp_path, monkeypatch, focused_prompt_extra='合成 CR\r\n行')
    local_prompt = (ctx.clip_dir / 'cpa/review-held.prompt.txt').read_bytes().decode('utf-8')
    wire = remote.files['/r/clip/cpa/review-held.prompt.txt']
    payload = json.loads(remote.files['/r/clip/cpa/review-held.out.json'])
    assert '\r\n' in local_prompt and wire == (local_prompt + '\n').encode('utf-8')
    universal = wire.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')
    assert payload['prompt_sha256'] == hashlib.sha256(universal).hexdigest()
    assert payload['prompt_sha256'] != hashlib.sha256(wire).hexdigest()
    assert run_clip(ctx, steps).stopped_at is None and len(remote.requests) == 2
    assert not doubt_policy.is_hold(_row(ctx))


@pytest.mark.parametrize('broken', ['wire_prompt', 'prompt_sha', 'model', 'effort', 'output_budget', 'stream', 'incomplete',
                                    'empty', 'bad_json', 'bad_utf8', 'missing_prompt', 'read_error'])
def test_bad_or_mismatched_existing_remote_terminal_stops_without_requery(tmp_path, monkeypatch, broken):
    ctx, steps, _, remote, main_original = _start(tmp_path, monkeypatch)
    out_path, prompt_path = '/r/clip/cpa/review-held.out.json', '/r/clip/cpa/review-held.prompt.txt'
    payload = json.loads(remote.files[out_path])
    if broken == 'wire_prompt':
        remote.files[prompt_path] += b'x'
    elif broken == 'missing_prompt':
        del remote.files[prompt_path]
    elif broken == 'bad_json':
        remote.files[out_path] = b'{bad'
    elif broken == 'bad_utf8':
        remote.files[out_path] = b'\xff'
    elif broken == 'read_error':
        remote.recovery_rc = 45
    else:
        mutations = {'prompt_sha': ('prompt_sha256', 'bad'), 'model': ('model', 'other-model'),
                     'effort': ('effort', 'medium'), 'output_budget': ('max_output_tokens', 32768), 'stream': ('stream', False),
                     'incomplete': ('complete', False), 'empty': ('text', '  ')}
        key, value = mutations[broken]
        payload[key] = value
        remote.files[out_path] = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    before = dict(remote.files)
    result = run_clip(ctx, steps)
    assert result.stopped_at == 9 and result.reason == 'CPA_CALL_FAILED' and len(remote.requests) == 2
    assert remote.files == before and all(p.read_bytes() == data for p, data in main_original.items())
    event = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
    assert not Path(event['cache_path']).exists()


def test_only_definitely_absent_remote_terminal_resumes_original_question(tmp_path, monkeypatch):
    ctx, steps, _, remote, _ = _start(tmp_path, monkeypatch)
    out_path = '/r/clip/cpa/review-held.out.json'
    del remote.files[out_path]
    saved_prompt = (ctx.clip_dir / 'cpa/review-held.prompt.txt').read_text(encoding='utf-8')
    (ctx.clip_dir / 'pkg/description.txt').write_text('不改原问题', encoding='utf-8')
    result = run_clip(ctx, steps)
    assert result.stopped_at is None and len(remote.requests) == 3 and not doubt_policy.is_hold(_row(ctx))
    assert remote.requests[-1]['input'] == saved_prompt + '\n'
    assert remote.requests[-1]['model'] == 'gpt-6.1-sol' and remote.requests[-1]['reasoning']['effort'] == 'high'


def test_interrupted_before_dispatch_resumes_once_using_same_real_adapter(tmp_path, monkeypatch):
    ctx, steps, _, remote, _ = _start(tmp_path, monkeypatch, crash=False)
    with monkeypatch.context() as paused:
        def interrupted(*_args, **_kwargs):
            raise RuntimeError('合成：派发前中断')
        paused.setattr(sp, '_ask_cpa', interrupted)
        first = run_clip(ctx, steps)
        assert first.stopped_at == 9 and len(remote.requests) == 1
    prompt = (ctx.clip_dir / 'cpa/review-held.prompt.txt').read_bytes().decode('utf-8')
    assert '/r/clip/cpa/review-held.out.json' not in remote.files
    assert run_clip(ctx, steps).stopped_at is None and len(remote.requests) == 2
    assert remote.requests[-1]['input'] == prompt + '\n'
    assert remote.requests[-1]['model'] == 'gpt-6.1-sol' and remote.requests[-1]['reasoning']['effort'] == 'high'
    assert not doubt_policy.is_hold(_row(ctx))


def test_recovered_unknown_metadata_remains_unknown(tmp_path, monkeypatch):
    ctx, steps, _, remote, _ = _start(tmp_path, monkeypatch)
    out_path = '/r/clip/cpa/review-held.out.json'
    payload = json.loads(remote.files[out_path])
    for key in ('attempts', 'usage', 'http_log', 'elapsed_seconds'):
        payload.pop(key, None)
    remote.files[out_path] = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    before = remote.files[out_path]
    assert run_clip(ctx, steps).stopped_at is None and len(remote.requests) == 2
    event = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
    assert all(event.get(key) is None for key in ('attempts', 'usage', 'http_log', 'elapsed_s'))
    assert remote.files[out_path] == before


@pytest.mark.parametrize(('reason', 'attempts', 'budget', 'resumes'), [
    ('ERROR TimeoutError', 4, 4, True),
    ('HTTP 429', 3, 3, True),
    ('HTTP 401', 3, 3, False),
    ('ERROR TimeoutError', 10, 10, False),
    ('ERROR TimeoutError', None, None, False),
    ('CPA_QUOTA_REFUSED: HTTP 429', 20, 0, True),
    ('TERMINAL error: {"code":"organization_spend_limit_exceeded","type":"insufficient_quota"}', 10, None, True),
    ('CPA_TEXT_RETRY_EXHAUSTED', 10, None, True),  # old state: the durable journal is the budget authority
])
def test_focused_review_resumes_only_a_bounded_unknown_failure(tmp_path, monkeypatch, reason, attempts, budget, resumes):
    ctx, steps, _, remote, _ = _start(tmp_path, monkeypatch)
    event = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
    event.update(rc=1, complete=False, reason=reason, attempts=attempts, budget_attempts=budget, stderr_tail=reason)
    ctx.state.save()
    before = len(remote.requests)
    outcome = run_clip(ctx, steps)
    if resumes:
        assert outcome.stopped_at is None, outcome
        assert len(remote.requests) == before + 1
        saved = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
        assert saved['complete'] is True
    else:
        assert outcome.stopped_at == 9 and outcome.reason == 'CPA_CALL_FAILED'
        assert len(remote.requests) == before


def test_complete_terminal_with_unparseable_review_is_preserved_without_requery(tmp_path, monkeypatch):
    ctx, steps, _, remote, _ = _start(tmp_path, monkeypatch)
    out_path = '/r/clip/cpa/review-held.out.json'
    payload = json.loads(remote.files[out_path])
    payload['text'] = '{"合成未闭合":'
    remote.files[out_path] = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    before = dict(remote.files)
    result = run_clip(ctx, steps)
    assert result.stopped_at == 9 and result.reason == 'CPA_RESPONSE_UNPARSEABLE'
    assert len(remote.requests) == 2 and remote.files == before
    event = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
    assert json.loads(Path(event['cache_path']).read_text(encoding='utf-8'))['response_text'] == payload['text']


def test_existing_different_completion_is_preserved_and_blocks_recovery(tmp_path, monkeypatch):
    ctx, steps, _, remote, _ = _start(tmp_path, monkeypatch)
    completion = ctx.clip_dir / 'cpa/review-held.completion.txt'
    completion.write_bytes(b'existing synthetic completion')
    before = dict(remote.files)
    result = run_clip(ctx, steps)
    assert result.stopped_at == 9 and result.reason == 'CPA_CALL_FAILED'
    assert len(remote.requests) == 2 and remote.files == before
    assert completion.read_bytes() == b'existing synthetic completion'
    event = next(c for c in ctx.state.data['review']['cpa'] if c['call_id'] == 'review-held')
    assert not Path(event['cache_path']).exists()


def test_remote_read_script_uses_exact_wire_and_preserves_files(tmp_path):
    remote = LocalRemote(scripts_dir=str(tmp_path / 'scripts'))
    directory = tmp_path / 'remote'
    (directory / 'cpa').mkdir(parents=True)
    prompt = '合成\r\n另一行'
    wire = (prompt + '\n').encode('utf-8')
    prompt_file = directory / 'cpa/review-held.prompt.txt'
    out_file = directory / 'cpa/review-held.out.json'
    prompt_file.write_bytes(wire)
    payload = {'model': 'gpt-6.1-sol', 'effort': 'high', 'stream': True,
               'prompt_sha256': hashlib.sha256((prompt.replace('\r\n', '\n') + '\n').encode()).hexdigest(),
               'complete': True, 'text': '合成完整终态', 'attempts': 1}
    out_file.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    originals = (prompt_file.read_bytes(), out_file.read_bytes())
    arguments = dict(call_id='review-held', prompt_text=prompt, model='gpt-6.1-sol', effort='high',
                     remote_dir=str(directory))
    result = adapters.recover_cpa_terminal(remote, **arguments)
    assert result is not None and result['complete'] and result['response_text'] == payload['text']
    assert (prompt_file.read_bytes(), out_file.read_bytes()) == originals
    prompt_file.unlink()
    with pytest.raises(RuntimeError):
        adapters.recover_cpa_terminal(remote, **arguments)
    out_file.unlink()
    assert adapters.recover_cpa_terminal(remote, **arguments) is None
