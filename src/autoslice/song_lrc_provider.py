"""External timed-lyric providers and deterministic LRC parsing."""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Mapping, Sequence
import urllib.parse
import urllib.request

from src.autoslice.song_common import (
    LrcLine,
    LrcProvider,
    LrcResult,
    _lrc_fingerprint,
    is_lrc_credit_metadata,
    normalize_lyric_text,
)

def build_netease_lrc_provider(*, timeout_seconds: float = 8.0, max_results: int = 3) -> LrcProvider:
    """Real API repair path: NetEase Cloud Music public search + lyric endpoints.

    Returns up to ``max_results`` LRC candidates per query — the caller ranks
    them by actual lyric-to-ASR alignment, so search order is advisory only.
    """

    def provider(query: str) -> list[LrcResult]:
        search_url = "https://music.163.com/api/search/get?" + urllib.parse.urlencode(
            {"s": query, "type": 1, "limit": 6, "offset": 0}
        )
        search_payload = _http_json(search_url, timeout_seconds=timeout_seconds)
        songs = (((search_payload or {}).get("result") or {}).get("songs")) or []
        results: list[LrcResult] = []
        for song in songs:
            if len(results) >= max_results:
                break
            song_id = song.get("id")
            if not isinstance(song_id, int):
                continue
            lyric_url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=0&tv=0"
            try:
                lyric_payload = _http_json(lyric_url, timeout_seconds=timeout_seconds)
            except Exception:
                continue
            lrc_text = (((lyric_payload or {}).get("lrc") or {}).get("lyric")) or ""
            lines = parse_lrc_text(lrc_text)
            if len(lines) >= 8:
                artists = song.get("artists") or []
                artist = artists[0].get("name") if artists and isinstance(artists[0], Mapping) else None
                results.append(
                    LrcResult(
                        provider="netease",
                        song_title=str(song.get("name") or ""),
                        artist=str(artist) if artist else None,
                        source_ref=f"netease://song/{song_id}",
                        lines=tuple(lines),
                    )
                )
        return results

    return provider


def build_lrclib_lrc_provider(*, timeout_seconds: float = 20.0, max_results: int = 3) -> LrcProvider:
    """Build an LRCLIB search provider that returns only usable synced LRCs.

    LRCLIB's search response can include plain-only lyrics.  Those are useful
    for reading but cannot prove clip timing, so this provider deliberately
    fails closed unless ``syncedLyrics`` parses into at least eight timed lines.
    Search order remains advisory; :func:`attempt_song_repair` reranks results
    by alignment to the actual performance.
    """

    def provider(query: str) -> list[LrcResult]:
        search_url = "https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": query})
        payload = _http_json_value(search_url, timeout_seconds=timeout_seconds)
        records = payload if isinstance(payload, list) else []
        results: list[LrcResult] = []
        for record in records:
            if len(results) >= max_results:
                break
            if not isinstance(record, Mapping):
                continue
            result = _lrclib_record_to_result(record)
            if result is not None:
                results.append(result)
        return results

    return provider


def build_kugou_lrc_provider(*, timeout_seconds: float = 12.0, max_results: int = 3) -> LrcProvider:
    """Build a no-credential Kugou synced-lyric fallback.

    Kugou exposes search, lyric-candidate and lyric-download endpoints used by
    its web client.  Search ranking is still only discovery: before downloading
    a candidate, this adapter requires its title, artist and duration to agree
    with the selected search row.  The downloaded LRC then enters the same
    audio/alignment proof gates as every other provider result.

    Kugou commonly inserts ``[00:00] title - artist`` as a timed metadata row.
    That row is provider-specific metadata, not a sung lyric, and is removed
    here rather than weakening the generic LRC parser.
    """

    def provider(query: str) -> list[LrcResult]:
        search_url = "https://songsearch.kugou.com/song_search_v2?" + urllib.parse.urlencode(
            {
                "keyword": query,
                "page": 1,
                "pagesize": 6,
                "platform": "WebFilter",
                "userid": -1,
                "iscorrection": 1,
                "privilege_filter": 0,
            }
        )
        payload = _http_json_value(
            search_url,
            timeout_seconds=timeout_seconds,
            request_headers={"Referer": "https://www.kugou.com/"},
        )
        if not isinstance(payload, Mapping) or payload.get("status") != 1:
            return []
        data = payload.get("data")
        tracks = data.get("lists") if isinstance(data, Mapping) else None
        if not isinstance(tracks, list):
            return []

        results: list[LrcResult] = []
        seen_fingerprints: set[str] = set()
        # Three distinct search rows are enough for a fallback.  Bounding the
        # fan-out keeps an unsuccessful ASR-derived query from issuing dozens
        # of lyric downloads before the next independent query/provider runs.
        for track in tracks[:3]:
            if len(results) >= max_results:
                break
            if not isinstance(track, Mapping):
                continue
            song_title = _strip_kugou_markup(track.get("SongName") or track.get("OriSongName"))
            artist = _strip_kugou_markup(track.get("SingerName"))
            file_hash = str(track.get("FileHash") or "").strip().upper()
            duration_s = track.get("Duration")
            if (
                not song_title
                or not artist
                or not re.fullmatch(r"[0-9A-F]{32}", file_hash)
                or isinstance(duration_s, bool)
                or not isinstance(duration_s, (int, float))
                or duration_s <= 0
            ):
                continue
            duration_ms = int(round(float(duration_s) * 1_000))
            lyric_search_url = "https://lyrics.kugou.com/search?" + urllib.parse.urlencode(
                {
                    "ver": 1,
                    "man": "yes",
                    "client": "pc",
                    "keyword": f"{artist} - {song_title}",
                    "hash": file_hash,
                    "timelength": duration_ms,
                }
            )
            try:
                lyric_payload = _http_json_value(
                    lyric_search_url,
                    timeout_seconds=timeout_seconds,
                    request_headers={"Referer": "https://www.kugou.com/"},
                )
            except Exception:
                continue
            if not isinstance(lyric_payload, Mapping) or lyric_payload.get("status") != 200:
                continue
            candidates = lyric_payload.get("candidates")
            if not isinstance(candidates, list):
                continue

            # Candidate scores are advisory.  Only exact normalized identity
            # and a near-exact catalog duration are allowed to reach download.
            # Try at most two exact candidates for this track to bound requests.
            exact_downloads = 0
            for lyric_candidate in candidates:
                if not isinstance(lyric_candidate, Mapping):
                    continue
                if not _kugou_candidate_matches_track(
                    lyric_candidate,
                    song_title=song_title,
                    artist=artist,
                    duration_ms=duration_ms,
                ):
                    continue
                lyric_id = str(lyric_candidate.get("id") or "").strip()
                access_key = str(lyric_candidate.get("accesskey") or "").strip()
                if not lyric_id.isdigit() or not re.fullmatch(r"[0-9A-Fa-f]{32}", access_key):
                    continue
                exact_downloads += 1
                download_url = "https://lyrics.kugou.com/download?" + urllib.parse.urlencode(
                    {
                        "ver": 1,
                        "client": "pc",
                        "id": lyric_id,
                        "accesskey": access_key,
                        "fmt": "lrc",
                        "charset": "utf8",
                    }
                )
                try:
                    download_payload = _http_json_value(
                        download_url,
                        timeout_seconds=timeout_seconds,
                        request_headers={"Referer": "https://www.kugou.com/"},
                    )
                    if not isinstance(download_payload, Mapping) or download_payload.get("status") != 200:
                        raise ValueError("Kugou lyric download returned an invalid response")
                    content = download_payload.get("content")
                    if not isinstance(content, str):
                        raise ValueError("Kugou lyric download did not return base64 content")
                    lrc_text = base64.b64decode(content, validate=True).decode("utf-8")
                except (binascii.Error, UnicodeDecodeError, ValueError, OSError):
                    if exact_downloads >= 2:
                        break
                    continue
                lines = _filter_kugou_timed_metadata(
                    parse_lrc_text(lrc_text),
                    song_title=song_title,
                    artist=artist,
                )
                if len(lines) < 8:
                    if exact_downloads >= 2:
                        break
                    continue
                result = LrcResult(
                    provider="kugou",
                    song_title=song_title,
                    artist=artist,
                    source_ref=download_url,
                    lines=tuple(lines),
                )
                fingerprint = _lrc_fingerprint(result)
                if fingerprint not in seen_fingerprints:
                    seen_fingerprints.add(fingerprint)
                    results.append(result)
                break
        return results

    return provider


def build_composite_lrc_provider(*providers: LrcProvider, max_results: int | None = None) -> LrcProvider:
    """Combine providers as independent fallbacks without erasing provenance.

    A temporary failure in one public lyric service must not prevent another
    provider from supplying evidence.  Results are deduplicated by canonical
    ``source_ref`` while each result's original ``provider`` and ``source_ref``
    are preserved for the alignment report and proof gate.
    """

    def provider(query: str) -> list[LrcResult]:
        results: list[LrcResult] = []
        seen_refs: set[str] = set()
        for candidate_provider in providers:
            try:
                found = candidate_provider(query)
            except Exception:
                continue
            for item in _coerce_lrc_results(found):
                if item.source_ref in seen_refs:
                    continue
                seen_refs.add(item.source_ref)
                results.append(item)
                if max_results is not None and len(results) >= max_results:
                    return results
        return results

    return provider


def fetch_lrclib_lrc(song_ref: str, *, timeout_seconds: float = 20.0) -> LrcResult | None:
    """Fetch one LRCLIB synced lyric by numeric id or canonical LRCLIB ref.

    Accepted forms include ``"33542202"``, ``"lrclib://track/33542202"``,
    and ``"https://lrclib.net/api/get/33542202"``.  Plain, unsynchronised lyrics are rejected:
    the song repair path needs timestamps, not merely lyric text.
    """

    song_id = _parse_lrclib_id(song_ref)
    if song_id is None:
        return None
    try:
        payload = _http_json(
            f"https://lrclib.net/api/get/{song_id}",
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    return _lrclib_record_to_result(payload, fallback_id=song_id)


def fetch_netease_lrc(song_ref: str, *, timeout_seconds: float = 8.0) -> LrcResult | None:
    """Fetch one NetEase song's LRC by id/ref, bypassing flaky text search.

    Accepts a bare id (``"2615403834"``) or a ref (``"netease://song/2615403834"``).
    Used to pin a known recurring song so its identification is deterministic.
    """

    match = re.search(r"(\d{4,})", str(song_ref or ""))
    if not match:
        return None
    song_id = int(match.group(1))
    try:
        lyric_payload = _http_json(
            f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=0&tv=0",
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        return None
    lrc_text = (((lyric_payload or {}).get("lrc") or {}).get("lyric")) or ""
    lines = parse_lrc_text(lrc_text)
    if len(lines) < 8:
        return None
    song_title = ""
    artist: str | None = None
    try:
        detail = _http_json(
            f"https://music.163.com/api/song/detail?ids=%5B{song_id}%5D",
            timeout_seconds=timeout_seconds,
        )
        songs = (detail or {}).get("songs") or []
        if songs and isinstance(songs[0], Mapping):
            song_title = str(songs[0].get("name") or "")
            artists = songs[0].get("artists") or []
            if artists and isinstance(artists[0], Mapping):
                artist = str(artists[0].get("name") or "") or None
    except Exception:
        pass
    return LrcResult(
        provider="netease",
        song_title=song_title,
        artist=artist,
        source_ref=f"netease://song/{song_id}",
        lines=tuple(lines),
    )


def parse_lrc_text(lrc_text: str) -> list[LrcLine]:
    lines: list[LrcLine] = []
    for raw_line in lrc_text.splitlines():
        matches = re.findall(r"\[(\d+):(\d+)(?:\.(\d+))?\]", raw_line)
        text = re.sub(r"\[[^\]]*\]", "", raw_line).strip()
        if not matches or not text:
            continue
        # Metadata/credit lines are not sung lyrics or proof checkpoints.  The
        # structural bilingual classifier covers both the historical Chinese
        # outro credits and English/bilingual rows such as
        # ``录音师 Recording Engineer：...`` without deleting ordinary lyrics.
        if is_lrc_credit_metadata(text):
            continue
        for minute, second, fraction in matches:
            fraction_ms = int((fraction or "0").ljust(3, "0")[:3])
            lines.append(LrcLine(time_ms=(int(minute) * 60 + int(second)) * 1000 + fraction_ms, text=text))
    lines.sort(key=lambda line: line.time_ms)
    return lines

def _http_json(url: str, *, timeout_seconds: float) -> Mapping[str, object] | None:
    payload = _http_json_value(url, timeout_seconds=timeout_seconds)
    return payload if isinstance(payload, Mapping) else None


def _http_json_value(
    url: str,
    *,
    timeout_seconds: float,
    request_headers: Mapping[str, str] | None = None,
) -> object:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Referer": "https://music.163.com/",
    }
    if request_headers:
        headers.update({str(key): str(value) for key, value in request_headers.items()})
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _coerce_lrc_results(found: LrcResult | Sequence[LrcResult] | None) -> list[LrcResult]:
    if found is None:
        return []
    if isinstance(found, LrcResult):
        return [found]
    return [item for item in found if isinstance(item, LrcResult)]


def _strip_kugou_markup(value: object) -> str:
    """Remove search-result highlighting while preserving the exact identity text."""

    return re.sub(r"<[^>]*>", "", str(value or "")).strip()


def _kugou_identity_text(value: object) -> str:
    return normalize_lyric_text(_strip_kugou_markup(value))


def _kugou_candidate_matches_track(
    candidate: Mapping[str, object],
    *,
    song_title: str,
    artist: str,
    duration_ms: int,
) -> bool:
    """Fail closed unless the lyric row belongs to this exact catalog track."""

    if _kugou_identity_text(candidate.get("song")) != _kugou_identity_text(song_title):
        return False
    if _kugou_identity_text(candidate.get("singer")) != _kugou_identity_text(artist):
        return False
    candidate_duration = candidate.get("duration")
    if isinstance(candidate_duration, bool) or not isinstance(candidate_duration, (int, float)):
        return False
    return abs(int(candidate_duration) - duration_ms) <= 3_000


def _filter_kugou_timed_metadata(
    lines: Sequence[LrcLine],
    *,
    song_title: str,
    artist: str,
) -> list[LrcLine]:
    """Drop Kugou's timed title card without dropping a real opening lyric."""

    title_identity = _kugou_identity_text(song_title)
    artist_identity = _kugou_identity_text(artist)
    filtered: list[LrcLine] = []
    for line in lines:
        line_identity = _kugou_identity_text(line.text)
        is_timed_title_card = (
            line.time_ms <= 1_000
            and bool(title_identity)
            and bool(artist_identity)
            and title_identity in line_identity
            and artist_identity in line_identity
        )
        if not is_timed_title_card:
            filtered.append(line)
    return filtered


def _parse_lrclib_id(song_ref: str) -> int | None:
    value = str(song_ref or "").strip()
    if value.isdigit():
        return int(value)
    match = re.fullmatch(r"lrclib://(?:track|song|lyrics)/(\d+)", value, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"https?://(?:www\.)?lrclib\.net/api/get/(\d+)", value, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _lrclib_record_to_result(record: Mapping[str, object], *, fallback_id: int | None = None) -> LrcResult | None:
    raw_id = record.get("id")
    song_id = raw_id if isinstance(raw_id, int) else fallback_id
    if song_id is None:
        return None
    synced_lyrics = record.get("syncedLyrics")
    if not isinstance(synced_lyrics, str) or not synced_lyrics.strip():
        return None
    lines = parse_lrc_text(synced_lyrics)
    if len(lines) < 8:
        return None
    song_title = str(record.get("trackName") or record.get("name") or "")
    artist = str(record.get("artistName") or "").strip() or None
    return LrcResult(
        provider="lrclib",
        song_title=song_title,
        artist=artist,
        source_ref=f"https://lrclib.net/api/get/{song_id}",
        lines=tuple(lines),
    )
