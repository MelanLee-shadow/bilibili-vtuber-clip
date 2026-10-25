"""Public, credential-free clip comparison. Only positive complete proof blocks.

No login, video download, publication, or queue integration is performed here.
When captions are unavailable, only the public audio representation is temporarily
downloaded and transcribed by the existing free ASR client, then deleted.
Caption differences alone are not proof of extra video: absent/edited captions,
ambiguous repeated dialogue, speed changes and uncertain timing stay UNKNOWN.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import datetime as dt
from difflib import SequenceMatcher
import hashlib
import html
import json
import math
import importlib
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Literal, TypeAlias, cast
import unicodedata
import urllib.parse
from zoneinfo import ZoneInfo

from src.autoslice.acoustic_pinyin import text_pinyin_tokens
from src.autoslice.bilibili_wbi import signed_params
from src.autoslice.community_comment_discovery import fetch_wbi_mixin_key
from src.autoslice.timely_term_crawler import BILIBILI_USER_AGENT, BoundedHttpClient, CrawlError

Json: TypeAlias = 'None | bool | int | float | str | list[Json] | dict[str, Json]'
JsonMap: TypeAlias = dict[str, Json]
Decision: TypeAlias = Literal['FULL_DUPLICATE', 'HAS_EXTRA', 'UNKNOWN']
API = 'https://api.bilibili.com'
_BVID = re.compile(r'BV[0-9A-Za-z]{10}\Z')
_TIME = re.compile(r'(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})')
MAX_EXTERNAL_SPEED = 1.5
DURATION_MARGIN_SECONDS = 3.0
SENTENCE_CHARACTER_COVERAGE = 0.8
FULL_CHARACTER_COVERAGE = 0.9
MIN_INFORMATIVE_CHARACTERS = 6
MAX_SUBSTITUTION_RUN = 2


def duration_evidence(own_duration: float, external_duration: float) -> JsonMap:
    """H62 operational bound, conditional on the documented 1.5x speed cap."""
    if not all(math.isfinite(v) and v > 0 for v in (own_duration, external_duration)):
        raise ValueError('invalid media duration')
    capacity = external_duration * MAX_EXTERNAL_SPEED + DURATION_MARGIN_SECONDS
    return {'max_external_speed': MAX_EXTERNAL_SPEED, 'margin_seconds': DURATION_MARGIN_SECONDS,
            'external_capacity_seconds': capacity,
            'extra_seconds_lower_bound': max(0.0, own_duration - capacity),
            'proves_extra': own_duration > capacity + 0.001,
            'assumption': 'external edit speed <= 1.5x; not an unrestricted content proof'}


class PublicAudioASR:
    """Bounded audio-only download; retain receipts/transcript, never media bytes."""

    def __init__(self, *, work_dir: Path | None = None, evidence_dir: Path | None = None) -> None:
        self.work_dir = work_dir
        self.evidence_dir = evidence_dir
        self.cache: dict[tuple[str, int], tuple[list[Cue], JsonMap]] = {}

    def transcribe(self, api: PublicBilibili, bvid: str, cid: int) -> tuple[list[Cue], JsonMap]:
        cache_key = (bvid, cid)
        if cache_key in self.cache:
            return self.cache[cache_key]
        receipt: JsonMap = {'bvid': bvid, 'cid': cid, 'status': 'UNKNOWN', 'temporary_audio_deleted': True}
        temporary_path: Path | None = None
        cues: list[Cue] = []
        try:
            url = api.audio_url(bvid, cid)
            receipt['url'] = url
            # The transport validates Referer against the same host allowlist.
            # audio_url has already constrained the media endpoint to the CDN.
            client = BoundedHttpClient(allowed_hosts=frozenset({'bilivideo.com', 'bilivideo.cn', 'bilibili.com'}),
                                       max_requests=1, max_response_bytes=32 * 1024 * 1024,
                                       timeout_seconds=60, now=api.now)
            response = client.fetch(url, headers={'User-Agent': BILIBILI_USER_AGENT,
                                                  'Referer': f'https://www.bilibili.com/video/{bvid}/'})
            sound = response.body
            if response.stale or not sound:
                raise CrawlError('EMPTY_OR_STALE_PUBLIC_AUDIO')
            receipt.update({'size_bytes': len(sound), 'sha256': hashlib.sha256(sound).hexdigest(),
                            'content_type': response.content_type, 'fetched_at': response.fetched_at.isoformat()})
            if self.work_dir is not None:
                self.work_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='external-audio-', dir=self.work_dir) as temporary:
                temporary_path = Path(temporary)
                media = Path(temporary) / 'audio.m4s'
                media.write_bytes(sound)
                # Pipe conversion output: ffmpeg has no network input or retained MP3.
                converted = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                                            '-protocol_whitelist', 'file,pipe', '-i', str(media), '-vn',
                                            '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame', '-b:a', '64k',
                                            '-f', 'mp3', 'pipe:1'], capture_output=True, check=True, timeout=120).stdout
                receipt['asr_input_sha256'] = hashlib.sha256(converted).hexdigest()
                free_asr = importlib.import_module('scripts.free_asr_client')
                # Existing auto chain is bcut first, then jianying; no credentials.
                result = obj(cast(Json, free_asr.transcribe(converted, 'auto', poll_timeout=180,
                                                          log=lambda _message: None)), 'ASR result')
                utterances = result.get('utterances')
                if not isinstance(utterances, list):
                    raise ValueError('ASR utterances unavailable')
                cues = []
                for value in utterances:
                    segment = obj(value, 'ASR segment')
                    cues.append(Cue(number(segment.get('start_time')) / 1000,
                                    number(segment.get('end_time')) / 1000, string(segment.get('transcript'))))
                validate_cues(cues)
                receipt.update({'status': 'TRANSCRIBED', 'provider': result.get('provider'),
                                'elapsed_s': result.get('elapsed_s'), 'utterances': utterances})
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            cues = []
            receipt['reason'] = f'AUDIO_ASR_FAILED: {type(exc).__name__}: {exc}'
        finally:
            deleted = temporary_path is None or not temporary_path.exists()
            receipt['temporary_audio_deleted'] = deleted
            if not deleted:
                cues = []
                receipt.update({'status': 'UNKNOWN', 'reason': 'TEMPORARY_AUDIO_CLEANUP_FAILED'})
        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            (self.evidence_dir / f'{bvid}-{cid}.json').write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.cache[cache_key] = (cues, receipt)
        return cues, receipt


def plain(value: str) -> str:
    return html.unescape(re.sub(r'<[^>]*>', '', value)).strip()


def key(value: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKC', plain(value)).casefold()
                   if unicodedata.category(c)[0] in {'L', 'N'})


def obj(value: Json, label: str) -> JsonMap:
    if not isinstance(value, dict):
        raise ValueError(f'{label}: expected object')
    return value


def number(value: Json) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('expected finite number')
    return float(value)


def string(value: Json) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('expected nonempty string')
    return value


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str

    def __post_init__(self) -> None:
        if not math.isfinite(self.start) or not math.isfinite(self.end) or self.start < 0 or self.end <= self.start:
            raise ValueError('invalid subtitle time')
        if not self.text.strip():
            raise ValueError('empty subtitle text')


def parse_srt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r'\n\s*\n', text.replace('\r\n', '\n').strip().lstrip('\ufeff')):
        lines = block.splitlines()
        if not lines:
            continue
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines or '-->' not in lines[0] or len(lines) < 2:
            raise ValueError('invalid SRT block')
        times = _TIME.findall(lines[0])
        if len(times) != 2:
            raise ValueError('invalid SRT timestamp')
        seconds = [int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000 for h, m, s, ms in times]
        cues.append(Cue(seconds[0], seconds[1], plain(' '.join(lines[1:]))))
    validate_cues(cues)
    return cues


def validate_cues(cues: list[Cue]) -> None:
    if not cues or len(cues) > 5000 or sum(len(c.text) for c in cues) > 60000:
        raise ValueError('empty or oversized subtitle track')
    if any(a.start > b.start for a, b in zip(cues, cues[1:])):
        raise ValueError('subtitle track is not ordered')


@dataclass(frozen=True)
class Package:
    candidate_id: str
    recording_date: dt.date
    participants: tuple[tuple[str, ...], ...]
    source_intervals: tuple[JsonMap, ...]
    duration_seconds: float
    cues: tuple[Cue, ...]
    subtitle_sha256: str
    subtitle_stage: Literal['final', 'source_preview']
    own_uploader_mids: tuple[int, ...]


def load_package(path: Path) -> Package:
    data = obj(cast(Json, json.loads(path.read_text(encoding='utf-8'))), 'package')
    groups = data.get('participants')
    if not isinstance(groups, list) or len(groups) < 1:
        raise ValueError('participants must contain streamer and any guests, each with aliases')
    participants = []
    for group in groups:
        if not isinstance(group, list) or not group:
            raise ValueError('each participant needs at least one name')
        participants.append(tuple(string(v) for v in group))
    intervals = data.get('source_intervals')
    if not isinstance(intervals, list) or not intervals:
        raise ValueError('source_intervals required')
    sources = []
    for value in intervals:
        interval = obj(value, 'source interval')
        string(interval.get('recording'))
        if not 0 <= number(interval.get('start_seconds')) < number(interval.get('end_seconds')):
            raise ValueError('invalid source interval')
        sources.append(interval)
    duration = number(data.get('duration_seconds'))
    if duration <= 0:
        raise ValueError('duration_seconds must be positive')
    srt_path = path.parent / string(data.get('srt'))
    raw = srt_path.read_bytes()
    cues = parse_srt(raw.decode('utf-8-sig'))
    if max(c.end for c in cues) > duration + 0.001:
        raise ValueError('SRT extends beyond package duration')
    stage = data.get('subtitle_stage', 'final')
    if stage not in ('final', 'source_preview'):
        raise ValueError('unknown subtitle_stage')
    mids = data.get('own_uploader_mids', [])
    if not isinstance(mids, list) or any(type(mid) is not int or mid <= 0 for mid in mids):
        raise ValueError('own_uploader_mids must be positive integers')
    return Package(string(data.get('candidate_id')), dt.date.fromisoformat(string(data.get('recording_date'))),
                   tuple(participants), tuple(sources), duration, tuple(cues), hashlib.sha256(raw).hexdigest(),
                   cast(Literal['final', 'source_preview'], stage), tuple(cast(int, mid) for mid in mids))


@dataclass(frozen=True)
class SentenceAlignment:
    sentence: Cue
    matched_characters: int
    substituted_characters: int
    unmapped_characters: int
    coverage_ratio: float
    covered: bool
    external_start_seconds: float | None
    external_end_seconds: float | None


@dataclass(frozen=True)
class Comparison:
    decision: Decision
    reason: str
    covered_sentences: int
    total_sentences: int
    coverage_ratio: float
    extra_sentences: tuple[Cue, ...] = ()
    unmatched_sentences: tuple[Cue, ...] = ()
    extra_seconds: float = 0.0
    offset_range_seconds: tuple[float, ...] = ()
    character_coverage_ratio: float = 0.0
    sentence_alignments: tuple[SentenceAlignment, ...] = ()
    unmatched_seconds: float = 0.0


def _compare_exact(own: tuple[Cue, ...], own_duration: float, external: list[Cue], external_duration: float) -> Comparison:
    """Compare one track at a time; never combine different external clips.

    Exact normalized characters handle different caption line breaks. Distinct
    informative anchors establish a constant offset at shared cue boundaries.
    FULL requires exact millisecond timing and containment of the *whole* media
    interval, including uncaptioned margins. Missing text inside an aligned
    external interval is uncertain, not evidence of extra video.
    """
    validate_cues(list(own))
    validate_cues(external)
    if own_duration < max(c.end for c in own) or external_duration < max(c.end for c in external):
        raise ValueError('caption exceeds media duration')
    own_keys = [key(c.text) for c in own]
    ext_keys = [key(c.text) for c in external]
    a, b = ''.join(own_keys), ''.join(ext_keys)
    total = len(own)
    if not a or not b:
        return Comparison('UNKNOWN', 'NO_LEXICAL_CONTENT', 0, total, 0)
    ext_starts, ext_ends = {}, {}
    pos = 0
    for cue, text in zip(external, ext_keys):
        if text:
            ext_starts[pos] = cue.start
            pos += len(text)
            ext_ends[pos] = cue.end
    full_pos = b.find(a)
    if full_pos >= 0:
        if b.find(a, full_pos + 1) >= 0:
            return Comparison('UNKNOWN', 'AMBIGUOUS_REPEATED_DIALOGUE', 0, total, 0)
        mapping = {i: i + full_pos for i in range(len(a))}
    else:
        mapping = {block.a + i: block.b + i
                   for block in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
                   for i in range(block.size)}
    covered, unmatched, anchors, informative = [], [], [], set()
    pos = 0
    for cue, text in zip(own, own_keys):
        end = pos + len(text)
        start_ext = mapping.get(pos)
        matches = bool(text) and start_ext is not None and all(mapping.get(i) == start_ext + i - pos for i in range(pos, end))
        if matches and start_ext is not None:
            covered.append(cue)
            if start_ext in ext_starts:
                anchors.append(ext_starts[start_ext] - cue.start)
            if start_ext + len(text) in ext_ends:
                anchors.append(ext_ends[start_ext + len(text)] - cue.end)
            if len(text) >= 6 and b.count(text) == 1 and a.count(text) == 1:
                informative.add(text)
        else:
            unmatched.append(cue)
        pos = end
    ratio = len(covered) / total
    common = (len(covered), total, ratio)
    if len(informative) < 2 or len(anchors) < 2:
        return Comparison('UNKNOWN', 'INSUFFICIENT_UNIQUE_TIMING_ANCHORS', *common, unmatched_sentences=tuple(unmatched))
    low, high = min(anchors), max(anchors)
    offsets = (round(low, 6), round(high, 6))
    if high - low > 0.25:
        return Comparison('UNKNOWN', 'EDITED_OR_INCONSISTENT_TIMELINE', *common,
                          unmatched_sentences=tuple(unmatched), offset_range_seconds=offsets)
    extra_seconds = max(0.0, -high) + max(0.0, own_duration + low - external_duration)
    extra = tuple(c for c in unmatched if c.end + high <= 0 or c.start + low >= external_duration)
    if extra_seconds > 0.001 or extra:
        return Comparison('HAS_EXTRA', 'ALIGNED_MEDIA_EXTENDS_BEYOND_EXTERNAL', *common, extra,
                          tuple(unmatched), round(extra_seconds, 6), offsets)
    if not unmatched and full_pos >= 0 and high - low < 0.000001 and low >= 0 and high + own_duration <= external_duration + 0.000001:
        return Comparison('FULL_DUPLICATE', 'ALL_SENTENCES_AND_WHOLE_INTERVAL_COVERED', *common,
                          offset_range_seconds=offsets)
    return Comparison('UNKNOWN', 'CAPTION_DIFFERENCE_OR_BOUNDARY_UNCERTAINTY', *common,
                      unmatched_sentences=tuple(unmatched), offset_range_seconds=offsets)


def _character_mapping(a: str, b: str) -> tuple[dict[int, int], set[int]]:
    """One monotone, one-to-one mapping; short replacements need exact context.

    No phonetic dictionary rewrites the evidence. An isolated ASR homophone or
    similar glyph is tolerated as a substitution, with its original text kept.
    Insertions/deletions remain unmapped and cannot support complete proof.
    """
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    opcodes = matcher.get_opcodes()
    mapping: dict[int, int] = {}
    substituted: set[int] = set()
    for index, (kind, i, end, j, stop) in enumerate(opcodes):
        if kind == 'equal':
            mapping.update((p, j + p - i) for p in range(i, end))
        elif kind == 'replace' and 0 < end - i == stop - j <= MAX_SUBSTITUTION_RUN:
            left = opcodes[index - 1] if index else None
            right = opcodes[index + 1] if index + 1 < len(opcodes) else None
            context = sum(op[2] - op[1] for op in (left, right) if op is not None and op[0] == 'equal')
            if context >= MIN_INFORMATIVE_CHARACTERS:
                mapping.update((p, j + p - i) for p in range(i, end))
                substituted.update(range(i, end))
    return mapping, substituted


def _cue_seconds(cues: list[Cue]) -> float:
    """Duration of the union, so overlapping subtitles are not double counted."""
    seconds, end = 0.0, 0.0
    for cue in cues:
        seconds += max(0.0, cue.end - max(end, cue.start))
        end = max(end, cue.end)
    return round(seconds, 6)


def compare(own: tuple[Cue, ...], own_duration: float, external: list[Cue], external_duration: float) -> Comparison:
    """Add tolerant character coverage without relaxing the media/timing gates."""
    exact = _compare_exact(own, own_duration, external, external_duration)
    own_keys, ext_keys = [key(c.text) for c in own], [key(c.text) for c in external]
    a, b = ''.join(own_keys), ''.join(ext_keys)
    if not a or not b:
        return exact
    mapping, substituted = _character_mapping(a, b)
    starts: dict[int, float] = {}
    ends: dict[int, float] = {}
    char_cues: list[Cue] = []
    pos = 0
    for cue, text in zip(external, ext_keys):
        if text:
            starts[pos] = cue.start
            pos += len(text)
            ends[pos] = cue.end
            char_cues.extend([cue] * len(text))
    rows, covered, unmatched, anchors, informative = [], [], [], [], set()
    pos = 0
    for cue, text in zip(own, own_keys):
        end = pos + len(text)
        aligned = [mapping[p] for p in range(pos, end) if p in mapping]
        replacements = len(substituted.intersection(range(pos, end)))
        matched = len(aligned) - replacements
        # Penalize inserted external text as well as absent own characters.
        span = b[aligned[0]:aligned[-1] + 1] if aligned else ''
        ratio = matched / max(len(text), len(span), 1)
        is_covered = bool(text) and ratio >= SENTENCE_CHARACTER_COVERAGE
        if len(text) < MIN_INFORMATIVE_CHARACTERS:
            is_covered = matched == len(text) > 0 and len(span) == len(text)
        rows.append(SentenceAlignment(cue, matched, replacements, len(text) - len(aligned),
                                      ratio, is_covered, char_cues[aligned[0]].start if aligned else None,
                                      char_cues[aligned[-1]].end if aligned else None))
        if is_covered:
            covered.append(cue)
            start_ext, end_ext = mapping.get(pos), mapping.get(end - 1)
            if start_ext is not None and start_ext in starts:
                anchors.append(starts[start_ext] - cue.start)
            if end_ext is not None and end_ext + 1 in ends:
                anchors.append(ends[end_ext + 1] - cue.end)
            if matched >= MIN_INFORMATIVE_CHARACTERS and a.count(text) == 1 and b.count(span) == 1:
                informative.add(text)
        else:
            unmatched.append(cue)
        pos = end
    character_ratio = (len(mapping) - len(substituted)) / len(a)
    result = replace(exact, covered_sentences=len(covered), coverage_ratio=len(covered) / len(own),
                     unmatched_sentences=tuple(unmatched), character_coverage_ratio=character_ratio,
                     sentence_alignments=tuple(rows), unmatched_seconds=_cue_seconds(unmatched))
    if exact.decision != 'UNKNOWN' or exact.reason == 'AMBIGUOUS_REPEATED_DIALOGUE':
        return result
    if len(informative) < 2 or len(anchors) < 2:
        return replace(result, reason='INSUFFICIENT_UNIQUE_TIMING_ANCHORS')
    low, high = min(anchors), max(anchors)
    result = replace(result, offset_range_seconds=(round(low, 6), round(high, 6)))
    if high - low > 0.25:
        return replace(result, reason='EDITED_OR_INCONSISTENT_TIMELINE')
    extra_seconds = max(0.0, -high) + max(0.0, own_duration + low - external_duration)
    extra = tuple(c for c in unmatched if c.end + high <= 0 or c.start + low >= external_duration)
    if extra_seconds > 0.001 or extra:
        return replace(result, decision='HAS_EXTRA', reason='CHARACTER_ALIGNED_MEDIA_EXTENDS_BEYOND_EXTERNAL',
                       extra_sentences=extra, extra_seconds=round(extra_seconds, 6))
    first = mapping.get(0)
    continuous = first is not None and all(mapping.get(i) == first + i for i in range(len(a)))
    # Similar spelling can also be a genuinely different utterance (来/不).
    # Only independently phonetic-compatible replacements support blocking;
    # absent optional pinyin data leaves fuzzy evidence diagnostic-only.
    same_sound = all((tokens := text_pinyin_tokens(a[p])) is not None and bool(tokens)
                     and tokens == text_pinyin_tokens(b[mapping[p]]) for p in substituted)
    if (not unmatched and continuous and character_ratio >= FULL_CHARACTER_COVERAGE
            and same_sound
            and high - low < 0.000001 and low >= 0 and high + own_duration <= external_duration + 0.000001):
        return replace(result, decision='FULL_DUPLICATE', reason='CHARACTER_ALIGNED_ALL_SENTENCES_AND_WHOLE_INTERVAL_COVERED')
    return replace(result, reason='CAPTION_DIFFERENCE_OR_BOUNDARY_UNCERTAINTY')


def queries(package: Package) -> list[str]:
    host, *guests = package.participants
    day = package.recording_date.strftime('%Y%m%d')
    result = [' '.join([host[0], *(g[0] for g in guests), day])]
    for group in guests:
        result.extend(f'{host[0]} {alias}' for alias in group)
    result.extend((f'{host[0]} {day}', f'{host[0]} {package.recording_date:%m.%d}'))
    return list(dict.fromkeys(result))[:8]


class PublicBilibili:
    def __init__(self, client: BoundedHttpClient, *, now: dt.datetime, evidence_dir: Path | None = None) -> None:
        self.client = client
        self.now = now
        self.evidence_dir = evidence_dir
        self.mixin = ''

    def get(self, url: str) -> JsonMap:
        response = self.client.fetch(url, headers={'User-Agent': BILIBILI_USER_AGENT, 'Referer': 'https://www.bilibili.com/'})
        if response.stale:
            raise CrawlError('STALE_RESPONSE')
        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(url.encode()).hexdigest()
            (self.evidence_dir / f'{name}.json').write_text(json.dumps({
                'url': url, 'fetched_at': response.fetched_at.isoformat(),
                'body': response.body.decode('utf-8'), 'content_type': response.content_type,
            }, ensure_ascii=False), encoding='utf-8')
        data = obj(cast(Json, json.loads(response.body)), 'response')
        if 'code' in data and data['code'] != 0:
            raise CrawlError(f'BILIBILI_CODE_{data["code"]}')
        return data

    def signed_get(self, endpoint: str, params: JsonMap) -> JsonMap:
        if not self.mixin:
            self.mixin, fresh = fetch_wbi_mixin_key(self.client, endpoint=API + '/x/web-interface/nav')
            if not fresh:
                raise CrawlError('STALE_WBI_BOOTSTRAP')
        return self.get(API + endpoint + '?' + urllib.parse.urlencode(signed_params(params, mixin_key=self.mixin, signed_at=self.now)))

    def search(self, query: str) -> list[JsonMap]:
        # Same request fields, signer and bounded HTTP transport as the existing
        # community-name search; there is no cookie jar or credential path.
        payload = self.signed_get('/x/web-interface/wbi/search/type', {
            'search_type': 'video', 'keyword': query, 'page': 1, 'page_size': 20,
            'order': 'pubdate', 'platform': 'pc', 'web_location': 1430654,
        })
        data = obj(payload.get('data'), 'search data')
        rows = data.get('result')
        if rows is None and data.get('numResults') == 0:
            return []
        if not isinstance(rows, list):
            raise ValueError('invalid search results')
        return [obj(row, 'search row') for row in rows]

    def view(self, bvid: str) -> JsonMap:
        return obj(self.get(API + '/x/web-interface/view?' + urllib.parse.urlencode({'bvid': bvid})).get('data'), 'video')

    def pages(self, bvid: str) -> list[Json]:
        pages = self.get(API + '/x/player/pagelist?' + urllib.parse.urlencode({'bvid': bvid})).get('data')
        if not isinstance(pages, list) or not pages:
            raise CrawlError('PUBLIC_PAGES_UNAVAILABLE')
        return pages

    def audio_url(self, bvid: str, cid: int) -> str:
        payload = self.signed_get('/x/player/wbi/playurl', {
            'bvid': bvid, 'cid': cid, 'fnval': 16, 'fnver': 0, 'fourk': 0,
        })
        dash = obj(obj(payload.get('data'), 'playurl').get('dash'), 'DASH')
        tracks = dash.get('audio')
        if not isinstance(tracks, list) or not tracks:
            raise CrawlError('PUBLIC_AUDIO_UNAVAILABLE')
        # Read only DASH.audio; muxed durl and DASH.video are never downloaded.
        audio = [obj(track, 'audio') for track in tracks]
        for track in sorted(audio, key=lambda t: number(t.get('bandwidth', 0))):
            alternatives = track.get('backupUrl') or track.get('backup_url') or []
            urls = [track.get('baseUrl') or track.get('base_url')]
            if isinstance(alternatives, list):
                urls.extend(alternatives)
            for value in urls:
                if not isinstance(value, str):
                    continue
                parsed = urllib.parse.urlsplit(value)
                host = parsed.hostname or ''
                if (parsed.scheme == 'https' and not parsed.username and not parsed.password and not parsed.port
                        and any(host == h or host.endswith('.' + h) for h in ('bilivideo.com', 'bilivideo.cn'))):
                    return value
        raise CrawlError('UNSAFE_PUBLIC_AUDIO_URL')

    def subtitles(self, bvid: str, cid: int) -> list[Cue]:
        payload = self.signed_get('/x/player/wbi/v2', {'bvid': bvid, 'cid': cid})
        subtitles = obj(obj(payload.get('data'), 'player').get('subtitle'), 'subtitle').get('subtitles')
        if not isinstance(subtitles, list) or not subtitles:
            raise CrawlError('PUBLIC_SUBTITLE_UNAVAILABLE')
        tracks = [obj(t, 'track') for t in subtitles]
        tracks.sort(key=lambda t: not str(t.get('lan', '')).startswith('zh'))
        url = string(tracks[0].get('subtitle_url'))
        if url.startswith('//'):
            url = 'https:' + url
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or not (host == 'hdslb.com' or host.endswith('.hdslb.com'))
                or not parsed.path.startswith(('/bfs/subtitle/', '/bfs/ai_subtitle/'))
                or not parsed.path.endswith('.json')):
            raise CrawlError('UNSAFE_SUBTITLE_URL')
        body = self.get(url).get('body')
        if not isinstance(body, list):
            raise ValueError('invalid subtitle body')
        cues = []
        for item in body:
            cue = obj(item, 'cue')
            cues.append(Cue(number(cue.get('from')), number(cue.get('to')), string(cue.get('content'))))
        validate_cues(cues)
        return cues


def same_session(package: Package, metadata: JsonMap) -> bool:
    text = str(metadata.get('title', '')) + ' ' + str(metadata.get('desc', ''))
    normalized = key(text)
    if not all(any(key(alias) in normalized for alias in group) for group in package.participants):
        return False
    day = package.recording_date
    # Dated titles/descriptions are evidence, query membership alone is not.
    dates = re.findall(r'(?<!\d)(20\d{2})[.年/\-]?(\d{2})[.月/\-]?(\d{2})(?!\d)', text)
    if dates:
        return (str(day.year), f'{day.month:02}', f'{day.day:02}') in dates
    return bool(re.search(rf'(?<!\d){day.month:02}[./\-]{day.day:02}(?!\d)', text)) and dt.datetime.fromtimestamp(number(metadata.get('pubdate')), ZoneInfo('Asia/Shanghai')).year == day.year


def duration_seconds(value: Json) -> float:
    if isinstance(value, str):
        parts = value.strip().split(':')
        if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
            raise ValueError('invalid video duration')
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + int(part)
        return seconds
    return number(value)


def check(package: Package, api: PublicBilibili, *, max_videos: int = 20,
          audio_asr: PublicAudioASR | None = None) -> JsonMap:
    if not 1 <= max_videos <= 50:
        raise ValueError('max_videos must be 1..50')
    earliest = dt.datetime.combine(package.recording_date, dt.time(), ZoneInfo('Asia/Shanghai')).timestamp()
    discovered: dict[str, JsonMap] = {}
    searches: list[Json] = []
    for query in queries(package):
        try:
            rows = api.search(query)
            searches.append({'query': query, 'count': len(rows)})
            for row in rows:
                bvid = str(row.get('bvid', ''))
                if _BVID.fullmatch(bvid) and number(row.get('pubdate')) >= earliest:
                    discovered.setdefault(bvid, row)
        except (OSError, ValueError, CrawlError) as exc:
            searches.append({'query': query, 'error': str(exc)})
    videos: list[Json] = []
    decisions: list[str] = []
    for bvid, row in list(discovered.items())[:max_videos]:
        item: JsonMap = {'bvid': bvid, 'url': f'https://www.bilibili.com/video/{bvid}/',
                         'title': plain(str(row.get('title', ''))), 'uploader': row.get('author'),
                         'uploader_mid': row.get('mid'), 'duration': row.get('duration'),
                         'duration_seconds': None,
                         'published_at': dt.datetime.fromtimestamp(number(row.get('pubdate')), dt.timezone.utc).isoformat(),
                         'decision': 'UNKNOWN'}
        try:
            try:
                item['duration_seconds'] = duration_seconds(row.get('duration'))
            except ValueError:
                pass  # The authoritative view response can still provide it.
            # Search's aggregate duration remains useful if the details endpoint
            # is unavailable. It can only prove HAS_EXTRA under the stated cap.
            search_bound = None
            try:
                search_bound = duration_evidence(package.duration_seconds, number(item.get('duration_seconds')))
            except ValueError:
                pass
            try:
                metadata = api.view(bvid)
            except (OSError, ValueError, CrawlError) as exc:
                item['metadata_error'] = str(exc)
                if search_bound is not None and search_bound['proves_extra']:
                    item.update({'decision': 'HAS_EXTRA', 'reason': 'DURATION_LOWER_BOUND',
                                 'duration_evidence': search_bound, 'duration_source': 'public_search'})
                    decisions.append('HAS_EXTRA')
                    videos.append(item)
                    continue
                if audio_asr is None:
                    raise
                # Public pagelist can supply a CID without login; missing full
                # metadata still cannot authorize FULL_DUPLICATE blocking.
                metadata = {'bvid': bvid, 'title': plain(str(row.get('title', ''))),
                            'desc': '', 'owner': {'mid': row.get('mid'), 'name': row.get('author')},
                            'pubdate': row.get('pubdate'), 'duration': item.get('duration_seconds'),
                            'pages': api.pages(bvid)}
            if metadata.get('bvid') != bvid:
                raise ValueError('VIDEO_RESPONSE_IDENTITY_MISMATCH')
            owner = obj(metadata.get('owner'), 'owner')
            if type(owner.get('mid')) is not int or number(owner.get('mid')) <= 0:
                raise ValueError('INVALID_UPLOADER_IDENTITY')
            item.update({'title': metadata.get('title'), 'uploader': owner.get('name'), 'uploader_mid': owner.get('mid'),
                         'duration': metadata.get('duration'),
                         'duration_seconds': duration_seconds(metadata.get('duration')),
                         'published_at': dt.datetime.fromtimestamp(number(metadata.get('pubdate')), dt.timezone.utc).isoformat()})
            if number(metadata.get('pubdate')) < earliest or owner.get('mid') in package.own_uploader_mids:
                item['reason'] = 'EXCLUDED_OLD_OR_OWN_UPLOAD'
                item['excluded'] = True
                videos.append(item)
                continue
            confirmed = same_session(package, metadata)
            item['same_session_confirmed'] = confirmed
            bound = duration_evidence(package.duration_seconds, number(item.get('duration_seconds')))
            item['duration_evidence'] = bound
            if bound['proves_extra']:
                item.update({'decision': 'HAS_EXTRA', 'reason': 'DURATION_LOWER_BOUND',
                             'duration_source': 'public_search' if 'metadata_error' in item else 'public_view'})
                decisions.append('HAS_EXTRA')
                videos.append(item)
                continue
            pages = metadata.get('pages')
            if not isinstance(pages, list) or not pages:
                raise ValueError('video pages unavailable')
            comparisons: list[Json] = []
            for page_value in pages[:4]:
                page = obj(page_value, 'page')
                part: JsonMap = {'cid': page.get('cid'), 'duration_seconds': page.get('duration'), 'decision': 'UNKNOWN'}
                try:
                    cid = int(number(page.get('cid')))
                    try:
                        cues = api.subtitles(bvid, cid)
                        part['transcript_source'] = 'public_subtitle'
                    except (OSError, ValueError, CrawlError) as exc:
                        part['public_subtitle_error'] = str(exc)
                        if audio_asr is None:
                            raise
                        cues, receipt = audio_asr.transcribe(api, bvid, cid)
                        part['audio_receipt'] = receipt
                        part['transcript_source'] = 'free_audio_asr'
                        if not cues:
                            raise CrawlError(str(receipt.get('reason', 'AUDIO_ASR_EMPTY')))
                    comparison = compare(package.cues, package.duration_seconds, cues, number(page.get('duration')))
                    part.update(cast(JsonMap, asdict(comparison)))
                    part['subtitle_cues'] = cast(list[Json], [asdict(cue) for cue in cues])
                    if comparison.decision == 'FULL_DUPLICATE' and (not confirmed or not package.own_uploader_mids or 'metadata_error' in item):
                        part.update({'decision': 'UNKNOWN', 'reason': 'SESSION_OR_OWN_UPLOADER_UNCONFIRMED'})
                except (OSError, ValueError, CrawlError) as exc:
                    part['reason'] = str(exc)
                comparisons.append(part)
            part_decisions = [obj(v, 'part').get('decision') for v in comparisons]
            decision = 'FULL_DUPLICATE' if 'FULL_DUPLICATE' in part_decisions else ('HAS_EXTRA' if part_decisions and all(d == 'HAS_EXTRA' for d in part_decisions) and len(pages) <= 4 else 'UNKNOWN')
            item.update({'decision': decision, 'parts': comparisons, 'parts_truncated': len(pages) > 4})
        except (OSError, ValueError, CrawlError) as exc:
            item['reason'] = str(exc)
        decisions.append(str(item['decision']))
        videos.append(item)
    decision = 'FULL_DUPLICATE' if 'FULL_DUPLICATE' in decisions else ('HAS_EXTRA' if decisions and all(d == 'HAS_EXTRA' for d in decisions) else 'UNKNOWN')
    if decision == 'HAS_EXTRA' and (len(discovered) > max_videos or any('error' in obj(s, 'search') for s in searches)):
        decision = 'UNKNOWN'
    return {
        'schema_version': 'external-duplicate-check.v1', 'candidate_id': package.candidate_id,
        'duration_policy': {'max_external_speed': MAX_EXTERNAL_SPEED, 'margin_seconds': DURATION_MARGIN_SECONDS},
        'checked_at': api.now.isoformat(), 'recording_date': package.recording_date.isoformat(),
        'subtitle_stage': package.subtitle_stage, 'subtitle_sha256': package.subtitle_sha256,
        'source_intervals': list(package.source_intervals), 'duration_seconds': package.duration_seconds,
        'decision': decision, 'block_upload': decision == 'FULL_DUPLICATE' and package.subtitle_stage == 'final',
        'reason': 'SINGLE_EXTERNAL_CLIP_PROVES_FULL_COVERAGE' if decision == 'FULL_DUPLICATE' else ('EXTRA_PROVEN_AGAINST_EACH_INSPECTED_CLIP' if decision == 'HAS_EXTRA' else 'SEARCH_OR_PUBLIC_SUBTITLE_OR_ALIGNMENT_INCOMPLETE'),
        'searches': searches, 'videos': videos, 'discovered_videos': len(discovered),
        'videos_truncated': len(discovered) > max_videos,
        'scope': 'bounded public search; not an exhaustive inventory or upload authorization',
    }
