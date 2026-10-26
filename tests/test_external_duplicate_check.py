"""Offline response replay and conservative blocking/extra-interval controls."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

from src.autoslice.external_duplicate_check import (
    Cue, Package, PublicAudioASR, PublicBilibili, check, compare, duration_evidence, load_package, queries, same_session,
)
from src.autoslice.timely_term_crawler import CachedResponse, CrawlError


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures/external_duplicate_check'
NOW = dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc)
OWN = (Cue(.5, 2.5, '今天来聊契约恋爱'), Cue(3.5, 5.5, '我觉得这个故事很好看'), Cue(6.5, 8.5, '最后再补充一句话'))


def fixture():
    return json.loads((FIXTURES / 'protocol_cases.json').read_text())


def package(**changes):
    fields = dict(candidate_id='fixture', recording_date=dt.date(2026, 10, 24),
                  participants=(('示例主播',), ('嘉宾Guest', '嘉宾', 'Guest')),
                  source_intervals=({'recording': 'example.mp4', 'start_seconds': 100, 'end_seconds': 109},),
                  duration_seconds=9, cues=OWN, subtitle_sha256='0'*64,
                  subtitle_stage='final', own_uploader_mids=(99999,))
    fields.update(changes)
    return Package(**fields)


class ReplayClient:
    def __init__(self, payload=None, *, fail_search=False):
        self.payload = payload or fixture()
        self.urls = []
        self.fail_search = fail_search

    def fetch(self, url, *, headers=None):
        self.urls.append(url)
        assert headers is not None and set(headers) == {'User-Agent', 'Referer'}
        assert 'Cookie' not in headers and 'Authorization' not in headers
        if '/nav' in url:
            payload = self.payload['nav']
        elif '/search/type' in url:
            if self.fail_search:
                raise OSError('network unavailable')
            payload = self.payload['search']
        elif '/view?' in url:
            payload = self.payload['view']
            if self.payload.get('recorded_replay') and parse_qs(urlsplit(url).query)['bvid'][0] != payload['data']['bvid']:
                raise OSError('HTTP Error 412: Precondition Failed (recorded transport failure)')
        elif '/player/wbi/v2?' in url:
            payload = self.payload['player']
        elif url == 'https://aisubtitle.hdslb.com/bfs/subtitle/fixture.json':
            payload = self.payload['subtitle']
        else:
            raise AssertionError(f'unexpected URL {url}')
        return CachedResponse(json.dumps(payload).encode(), 'application/json', NOW)


def run(payload=None, **changes):
    client = ReplayClient(payload)
    return check(package(**changes), PublicBilibili(client, now=NOW)), client


def test_full_duplicate_single_external_clip_and_signed_credential_free_requests():
    result, client = run()
    assert result['decision'] == 'FULL_DUPLICATE'
    assert result['block_upload'] is True
    part = result['videos'][0]['parts'][0]
    assert part['coverage_ratio'] == 1
    assert part['extra_seconds'] == 0
    for url in client.urls:
        if '/wbi/' in url:
            assert {'w_rid', 'wts'} <= set(parse_qs(urlsplit(url).query))
    assert not any('playurl' in url for url in client.urls)


@pytest.mark.parametrize('duration,expected', [(10, 'FULL_DUPLICATE'), (20, 'FULL_DUPLICATE')])
def test_external_interval_equal_or_longer(duration, expected):
    data = fixture()
    data['view']['data']['pages'][0]['duration'] = duration
    assert run(data)[0]['decision'] == expected


def test_shorter_external_interval_reports_extra_sentence_and_seconds():
    data = fixture()
    data['subtitle']['body'] = data['subtitle']['body'][:2]
    data['view']['data']['pages'][0]['duration'] = 7
    result, _ = run(data)
    assert result['decision'] == 'HAS_EXTRA'
    assert result['block_upload'] is False
    part = result['videos'][0]['parts'][0]
    assert part['extra_seconds'] == 3
    assert part['extra_sentences'][0]['text'] == OWN[-1].text
    assert part['covered_sentences'] == 2


def test_extra_silent_seconds_are_not_hidden_by_identical_subtitles():
    result, _ = run(duration_seconds=12)
    assert result['decision'] == 'HAS_EXTRA'
    assert result['videos'][0]['parts'][0]['extra_seconds'] == 3


@pytest.mark.parametrize('kind', ['no_subtitles', 'search_failed', 'unmatched', 'wrong_session', 'missing_own_mid'])
def test_unknown_never_blocks(kind):
    data = fixture()
    changes = {}
    if kind == 'no_subtitles':
        data['player']['data']['subtitle']['subtitles'] = []
    elif kind == 'search_failed':
        data['search'] = {'code': -412, 'data': {}}
    elif kind == 'unmatched':
        data['subtitle']['body'][0]['content'] = '完全不同的话题内容'
    elif kind == 'wrong_session':
        data['view']['data']['desc'] = ''
        data['view']['data']['title'] = '2026-10-23 示例主播 嘉宾Guest'
    else:
        changes['own_uploader_mids'] = ()
    result, _ = run(data, **changes)
    assert result['decision'] == 'UNKNOWN'
    assert result['block_upload'] is False


def test_mismatched_video_identity_cannot_block():
    data = fixture()
    data['view']['data']['bvid'] = 'BV1000000002'
    result, _ = run(data)
    assert result['decision'] == 'UNKNOWN'
    assert 'VIDEO_RESPONSE_IDENTITY_MISMATCH' in str(result)


def test_search_variants_and_guest_aliases_do_not_mutate_dialogue():
    value = package(participants=(('李豆沙',), ('雾深Girimi', '雾深', 'Girimi')))
    assert '李豆沙 雾深' in queries(value)
    assert '李豆沙 Girimi' in queries(value)
    for alias in ('雾深Girimi', '雾深', 'Girimi'):
        assert same_session(value, {'title': f'2026-10-24 李豆沙 x {alias}', 'pubdate': NOW.timestamp()})
    assert not same_session(value, {'title': '2026-10-23 李豆沙 雾深', 'pubdate': NOW.timestamp()})
    assert not same_session(value, {'title': '李豆沙 雾深', 'pubdate': NOW.timestamp()})


def test_same_day_upload_alone_and_conflicting_explicit_date_do_not_prove_same_session():
    value = package()
    assert not same_session(value, {'title': '示例主播 嘉宾Guest', 'pubdate': NOW.timestamp()})
    assert not same_session(value, {'title': '2025-10-24 示例主播 嘉宾Guest', 'pubdate': NOW.timestamp()})


def test_old_and_own_uploads_are_excluded():
    data = fixture()
    data['view']['data']['owner']['mid'] = 99999
    result, _ = run(data)
    assert result['decision'] == 'UNKNOWN'
    assert result['videos'][0]['excluded'] is True
    data['search']['data']['result'][0]['pubdate'] = 1
    assert run(data)[0]['videos'] == []


def test_preview_cannot_block_even_when_subtitles_are_identical():
    result, _ = run(subtitle_stage='source_preview')
    assert result['decision'] == 'FULL_DUPLICATE'
    assert result['block_upload'] is False


def test_different_caption_segmentation_is_supported():
    external = [Cue(1.5, 6.5, OWN[0].text + '，' + OWN[1].text), Cue(7.5, 9.5, OWN[2].text)]
    assert compare(OWN, 9, external, 10).decision == 'FULL_DUPLICATE'


def test_repeated_generic_sentences_or_reused_occurrences_cannot_block():
    own = (Cue(0, 1, '好的'), Cue(2, 3, '好的'))
    external = [Cue(0, 1, '好的')]
    assert compare(own, 3, external, 3).decision == 'UNKNOWN'
    external = [Cue(1.5, 9.5, ''.join(c.text for c in OWN)), Cue(11.5, 19.5, ''.join(c.text for c in OWN))]
    assert compare(OWN, 9, external, 20).reason == 'AMBIGUOUS_REPEATED_DIALOGUE'


def test_speed_changes_or_timing_uncertainty_cannot_block():
    data = fixture()
    data['subtitle']['body'][-1]['to'] += .01
    assert run(data)[0]['decision'] == 'UNKNOWN'
    data['subtitle']['body'][-1]['to'] += .8
    assert run(data)[0]['decision'] == 'UNKNOWN'


def test_short_caption_timing_also_participates_in_complete_proof():
    own = (OWN[0], Cue(3, 3.3, '嗯'), OWN[1], OWN[2])
    external = [Cue(c.start + 1, c.end + 1, c.text) for c in own]
    external[1] = Cue(4.1, 4.4, '嗯')
    assert compare(own, 9, external, 10).decision == 'UNKNOWN'


def test_missing_caption_within_external_interval_is_not_extra_video_proof():
    data = fixture()
    data['subtitle']['body'][1]['content'] = '我觉得这个故事好看'
    assert run(data)[0]['decision'] == 'UNKNOWN'


def test_two_external_tracks_cannot_be_unioned_to_make_full_duplicate():
    data = fixture()
    data['view']['data']['pages'] *= 2
    data['subtitle']['body'] = data['subtitle']['body'][:2]
    assert run(data)[0]['decision'] == 'UNKNOWN'


def test_search_failure_retains_unknown_even_if_some_extra_is_proven():
    data = fixture()
    data['subtitle']['body'] = data['subtitle']['body'][:2]
    data['view']['data']['pages'][0]['duration'] = 7
    client = ReplayClient(data)
    api = PublicBilibili(client, now=NOW)
    original = api.search
    def partial(query):
        if '20261024' in query:
            raise OSError('one query failed')
        return original(query)
    api.search = partial
    result = check(package(), api)
    assert result['decision'] == 'UNKNOWN'
    assert result['videos'][0]['decision'] == 'HAS_EXTRA'


def test_no_network_on_transport_error_and_no_inferred_nonduplication():
    result = check(package(), PublicBilibili(ReplayClient(fail_search=True), now=NOW))
    assert result['decision'] == 'UNKNOWN'
    assert all('error' in query for query in result['searches'])


def test_foreign_subtitle_url_is_refused():
    data = fixture()
    data['player']['data']['subtitle']['subtitles'][0]['subtitle_url'] = 'https://example.com/track.json'
    result, client = run(data)
    assert result['decision'] == 'UNKNOWN'
    assert not any('example.com' in url for url in client.urls)
    assert 'UNSAFE_SUBTITLE_URL' in str(result)


def test_video_media_url_cannot_be_downloaded_as_a_subtitle():
    data = fixture()
    data['player']['data']['subtitle']['subtitles'][0]['subtitle_url'] = 'https://aisubtitle.hdslb.com/video.m4s'
    result, client = run(data)
    assert 'UNSAFE_SUBTITLE_URL' in str(result)
    assert not any(url.endswith('.m4s') for url in client.urls)


def test_real_empty_search_response_is_not_reported_as_a_transport_failure():
    data = fixture()
    data['search'] = {'code': 0, 'data': {'numResults': 0, 'numPages': 0}}
    result, _ = run(data)
    assert result['decision'] == 'UNKNOWN'
    assert all(query['count'] == 0 and 'error' not in query for query in result['searches'])


def test_package_input_and_srt_binding(tmp_path):
    srt = '\n\n'.join(f'{i}\n00:00:0{c.start:.3f}'.replace('.', ',') + ' --> ' + f'00:00:0{c.end:.3f}'.replace('.', ',') + f'\n{c.text}' for i, c in enumerate(OWN, 1))
    (tmp_path / 'final.srt').write_text(srt)
    data = {'candidate_id': 'example', 'recording_date': '2026-10-24', 'participants': [['示例主播'], ['嘉宾Guest', 'Guest']],
            'source_intervals': [{'recording': 'example.mp4', 'start_seconds': 100, 'end_seconds': 109}],
            'duration_seconds': 9, 'srt': 'final.srt', 'own_uploader_mids': [99999]}
    path = tmp_path / 'package.json'
    path.write_text(json.dumps(data))
    value = load_package(path)
    assert value.cues == OWN
    assert len(value.subtitle_sha256) == 64
    data['duration_seconds'] = 1
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='beyond'):
        load_package(path)


def test_cli_help_runs_without_credentials():
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/check_external_duplicate_clips.py'), '--help'], text=True, capture_output=True)
    assert result.returncode == 0
    assert '--package' in result.stdout


@pytest.mark.parametrize('own,proven', [(17.999, False), (18, False), (18.002, True)])
def test_duration_speed_cap_and_margin_boundary(own, proven):
    evidence = duration_evidence(own, 10)
    assert evidence['proves_extra'] is proven
    assert evidence['external_capacity_seconds'] == 18
    result, _ = run(duration_seconds=own)
    if proven:
        assert result['decision'] == 'HAS_EXTRA'
        assert result['videos'][0]['reason'] == 'DURATION_LOWER_BOUND'


@pytest.mark.parametrize('duration', [0, -1, float('nan'), float('inf')])
def test_invalid_duration_is_not_extra_proof(duration):
    with pytest.raises(ValueError):
        duration_evidence(20, duration)


def test_search_duration_can_prove_extra_when_view_returns_412():
    api = PublicBilibili(ReplayClient(), now=NOW)
    api.view = lambda _bvid: (_ for _ in ()).throw(OSError('HTTP 412'))
    result = check(package(duration_seconds=19), api)
    assert result['decision'] == 'HAS_EXTRA'
    assert result['videos'][0]['duration_source'] == 'public_search'
    assert '412' in result['videos'][0]['metadata_error']


@pytest.mark.parametrize('decision', ['FULL_DUPLICATE', 'HAS_EXTRA', 'UNKNOWN'])
def test_missing_subtitle_audio_fallback_uses_same_comparator(decision):
    data = fixture()
    data['player']['data']['subtitle']['subtitles'] = []
    cues = [Cue(c.start + 1, c.end + 1, c.text) for c in OWN]
    if decision == 'HAS_EXTRA':
        data['view']['data']['pages'][0]['duration'] = 7
        cues = cues[:2]
    elif decision == 'UNKNOWN':
        cues = [Cue(1, 2, '不相关的内容')]
    class Audio:
        def transcribe(self, api, bvid, cid):
            return cues, {'status': 'TRANSCRIBED', 'provider': 'bcut', 'temporary_audio_deleted': True}
    result = check(package(), PublicBilibili(ReplayClient(data), now=NOW), audio_asr=Audio())
    assert result['decision'] == decision
    assert result['block_upload'] is (decision == 'FULL_DUPLICATE')
    part = result['videos'][0]['parts'][0]
    assert part['transcript_source'] == 'free_audio_asr'
    assert part['audio_receipt']['provider'] == 'bcut'


@pytest.mark.parametrize('reason', ['ASR failed', 'audio unavailable', 'HTTP 412'])
def test_audio_failure_keeps_unknown_and_reason(reason):
    data = fixture()
    data['player']['data']['subtitle']['subtitles'] = []
    class Audio:
        def transcribe(self, api, bvid, cid):
            return [], {'status': 'UNKNOWN', 'reason': reason, 'temporary_audio_deleted': True}
    result = check(package(), PublicBilibili(ReplayClient(data), now=NOW), audio_asr=Audio())
    assert result['decision'] == 'UNKNOWN'
    assert result['block_upload'] is False
    assert reason in result['videos'][0]['parts'][0]['reason']


@pytest.mark.parametrize('host', ['evil.example', 'bilivideo.com.evil.example', ''])
def test_audio_url_rejects_foreign_hosts(host):
    api = PublicBilibili(ReplayClient(), now=NOW)
    api.signed_get = lambda *a: {'data': {'dash': {'audio': [{'baseUrl': f'https://{host}/audio.m4s'}]}}}
    with pytest.raises(CrawlError, match='UNSAFE_PUBLIC_AUDIO_URL'):
        api.audio_url('BV1000000001', 98765)


def test_audio_url_selects_only_low_bandwidth_audio_not_video_or_muxed():
    api = PublicBilibili(ReplayClient(), now=NOW)
    api.signed_get = lambda *a: {'data': {'dash': {
        'audio': [{'baseUrl': 'https://a.bilivideo.com/audio.m4s', 'bandwidth': 64},
                  {'baseUrl': 'https://a.bilivideo.com/high.m4s', 'bandwidth': 128}],
        'video': [{'baseUrl': 'https://a.bilivideo.com/video.m4s'}]},
        'durl': [{'url': 'https://a.bilivideo.com/muxed.mp4'}]}}
    assert api.audio_url('BV1000000001', 98765) == 'https://a.bilivideo.com/audio.m4s'


def test_public_audio_alternate_on_allowed_cdn_is_selected_without_expanding_hosts():
    api = PublicBilibili(ReplayClient(), now=NOW)
    api.signed_get = lambda *a: {'data': {'dash': {'audio': [{
        'baseUrl': 'https://upos-hz-mirrorakam.akamaized.net/audio.m4s',
        'backupUrl': ['https://upos-sz-mirrorcosov.bilivideo.com/audio.m4s']}]}}}
    assert api.audio_url('BV1000000001', 98765) == 'https://upos-sz-mirrorcosov.bilivideo.com/audio.m4s'


@pytest.mark.parametrize('fail_at', ['success', 'download', 'transcribe', 'convert'])
def test_actual_audio_adapter_receipt_cleanup_and_cache(tmp_path, monkeypatch, fail_at):
    import scripts.free_asr_client as free_asr
    import src.autoslice.external_duplicate_check as module
    api = PublicBilibili(ReplayClient(), now=NOW)
    api.audio_url = lambda *a: 'https://a.bilivideo.com/audio.m4s'
    def fetch(*a, **kw):
        if fail_at == 'download':
            raise OSError('HTTP 412')
        return CachedResponse(b'audio-bytes', 'audio/mp4', NOW)
    monkeypatch.setattr(module.BoundedHttpClient, 'fetch', fetch)
    def convert(*a, **kw):
        if fail_at == 'convert':
            raise subprocess.CalledProcessError(1, 'ffmpeg')
        return subprocess.CompletedProcess([], 0, stdout=b'mp3-bytes')
    monkeypatch.setattr(module.subprocess, 'run', convert)
    providers = []
    def transcribe(sound, provider, **kw):
        providers.append(provider)
        assert sound == b'mp3-bytes'
        if fail_at == 'transcribe':
            raise free_asr.AsrError('recognition failed')
        return {'provider': 'bcut', 'utterances': [
            {'start_time': 1000, 'end_time': 2000, 'transcript': '示例转写'}]}
    monkeypatch.setattr(free_asr, 'transcribe', transcribe)
    adapter = PublicAudioASR(work_dir=tmp_path / 'temporary', evidence_dir=tmp_path / 'evidence')
    cues, receipt = adapter.transcribe(api, 'BV1000000001', 98765)
    assert receipt['temporary_audio_deleted'] is True
    assert not list((tmp_path / 'temporary').glob('*'))
    assert (tmp_path / 'evidence/BV1000000001-98765.json').exists()
    if fail_at == 'success':
        assert cues == [Cue(1, 2, '示例转写')]
        assert receipt['size_bytes'] == len(b'audio-bytes')
        assert len(receipt['sha256']) == 64
        assert providers == ['auto']
        assert adapter.transcribe(api, 'BV1000000001', 98765) == (cues, receipt)
        assert providers == ['auto']
    else:
        assert not cues and receipt['status'] == 'UNKNOWN'
        assert 'AUDIO_ASR_FAILED' in receipt['reason']


def test_real_bounded_audio_transport_accepts_referer_and_sends_no_credentials(tmp_path, monkeypatch):
    from email.message import Message
    import io
    import urllib.request
    import scripts.free_asr_client as free_asr
    import src.autoslice.external_duplicate_check as module
    api = PublicBilibili(ReplayClient(), now=NOW)
    api.audio_url = lambda *a: 'https://a.bilivideo.com/audio.m4s'
    class Response(io.BytesIO):
        headers = Message()
    Response.headers['Content-Type'] = 'audio/mp4'
    def open_response(self, request, **kwargs):
        assert request.get_header('Referer') == 'https://www.bilibili.com/video/BV1000000001/'
        assert not request.has_header('Cookie') and not request.has_header('Authorization')
        return Response(b'audio')
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', open_response)
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=b'mp3'))
    def fail(*a, **kw):
        raise free_asr.AsrError('deliberate ASR failure after download')
    monkeypatch.setattr(free_asr, 'transcribe', fail)
    cues, receipt = PublicAudioASR(work_dir=tmp_path).transcribe(api, 'BV1000000001', 98765)
    assert not cues
    assert receipt['size_bytes'] == 5
    assert 'deliberate ASR failure' in receipt['reason']
    assert receipt['temporary_audio_deleted'] is True
