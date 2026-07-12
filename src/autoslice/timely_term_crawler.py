"""Bounded, source-backed timely-term crawler for subtitle entity priors.

The crawler deliberately produces *candidate data*, never subtitle edits.  Raw
web text is discarded at the adapter boundary: only structured title fields,
dates, stable source URLs, and locally configured event names can enter the
snapshot consumed by the subtitle pipeline.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import datetime as dt
import email.utils
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable, Protocol
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


SCHEMA_VERSION = "lidousha-timely-term-sources.v1"
DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "graphql.anilist.co",
        "anilist.co",
        "api.bgm.tv",
        "animenewsnetwork.com",
        "bgm.tv",
        "tv-tokyo.co.jp",
    }
)
USER_AGENT = "vtuber-slice-timely-terms/1.0 (+local bounded crawler)"
_INSTRUCTION_RX = re.compile(
    r"(?i)(?:\bignore\b.{0,24}\binstructions?\b|"
    r"\b(?:system|developer)\b.{0,24}\bprompt\b|"
    r"忽略.{0,12}(?:指令|提示)|系统提示|执行.{0,12}命令)"
)
_SPACE_RX = re.compile(r"\s+")
_MATCH_RX = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+")
_ATOM_PUNCTUATION = frozenset(" !！?？&+＋-_/・·.．()（）∞'")


class CrawlError(RuntimeError):
    """A bounded crawler operation could not produce safe structured data."""


class FetchLimitError(CrawlError):
    """A request exceeded the configured request or response budget."""


@dataclass(frozen=True)
class CrawlWindow:
    as_of: dt.date
    start: dt.date
    end: dt.date

    @classmethod
    def around(
        cls,
        as_of: dt.date,
        *,
        lookback_months: int = 9,
        lookahead_months: int = 6,
    ) -> "CrawlWindow":
        if not 0 <= lookback_months <= 24 or not 0 <= lookahead_months <= 24:
            raise ValueError("lookback/lookahead months must be between 0 and 24")
        return cls(
            as_of=as_of,
            start=_shift_months(as_of, -lookback_months),
            end=_shift_months(as_of, lookahead_months),
        )


def _shift_months(value: dt.date, months: int) -> dt.date:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_month = divmod(month_index, 12)
    month = zero_month + 1
    if month == 12:
        next_month = dt.date(year + 1, 1, 1)
    else:
        next_month = dt.date(year, month + 1, 1)
    last_day = (next_month - dt.timedelta(days=1)).day
    return dt.date(year, month, min(value.day, last_day))


def _fuzzy_date(value: object) -> dt.date | None:
    if not isinstance(value, dict):
        return None
    try:
        year = int(value.get("year"))
        month = int(value.get("month") or 1)
        day = int(value.get("day") or 1)
        return dt.date(year, month, day)
    except (TypeError, ValueError):
        return None


def _timestamp_date(value: object) -> dt.date | None:
    try:
        return dt.datetime.fromtimestamp(int(value), tz=dt.timezone.utc).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _clean_atom(value: object, *, max_chars: int = 64) -> str | None:
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value).strip()
    if not text or len(text) > max_chars or _INSTRUCTION_RX.search(text):
        return None
    cleaned: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category.startswith("C"):
            return None
        if category[:1] in {"L", "M", "N"} or char in _ATOM_PUNCTUATION:
            cleaned.append(char)
        elif char in {":", "：", "~", "〜", "～", "|", "｜"}:
            cleaned.append(" - ")
        elif char in {"×", "✕"}:
            cleaned.append(" x ")
        else:
            cleaned.append(" ")
    result = _SPACE_RX.sub(" ", "".join(cleaned)).strip(" -")
    if not result or len(result) > max_chars or _INSTRUCTION_RX.search(result):
        return None
    return result


def _unique_atoms(values: Iterable[object], *, limit: int = 16) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        atom = _clean_atom(value)
        if not atom or atom.casefold() in seen:
            continue
        result.append(atom)
        seen.add(atom.casefold())
        if len(result) >= limit:
            break
    return result


def _match_key(value: str) -> str:
    return _MATCH_RX.sub("", unicodedata.normalize("NFKC", value).casefold())


def _surface_in_title(surface: str, title: str, title_key: str) -> bool:
    key = _match_key(surface)
    if not key:
        return False
    if len(key) >= 4:
        return key in title_key
    # Short configured acronyms such as AX/BW must be complete tokens, not a
    # substring of an unrelated headline word.
    return bool(re.search(rf"(?i)(?<![0-9a-z]){re.escape(surface)}(?![0-9a-z])", title))


def _stable_url(value: object, *, allowed_hosts: frozenset[str]) -> str | None:
    if not isinstance(value, str) or any(char.isspace() for char in value):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port:
        return None
    if not any(host == item or host.endswith("." + item) for item in allowed_hosts):
        return None
    if not parsed.path.startswith("/") or parsed.path == "/":
        return None
    return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


def _stable_feed_item_url(value: object, *, allowed_hosts: frozenset[str]) -> str | None:
    """Normalize legacy HTTP item links only for an already allowlisted host."""
    if isinstance(value, str):
        try:
            parsed = urllib.parse.urlsplit(value)
            host = (parsed.hostname or "").lower()
        except ValueError:
            parsed = None
            host = ""
        if parsed and parsed.scheme == "http" and any(
            host == item or host.endswith("." + item) for item in allowed_hosts
        ):
            value = urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, "", ""))
    return _stable_url(value, allowed_hosts=allowed_hosts)


@dataclass(frozen=True)
class Provenance:
    url: str
    published_at: dt.date
    publisher: str

    def as_dict(self) -> dict[str, str]:
        return {
            "url": self.url,
            "published_at": self.published_at.isoformat(),
            "publisher": self.publisher,
        }


@dataclass
class TermCandidate:
    canonical: str
    readings: list[str]
    aliases: list[str]
    confusables: list[str]
    topic_entities: list[str]
    active_from: dt.date
    active_until: dt.date
    sources: list[Provenance]
    display_name: str | None = None
    reason: str | None = None
    score: int = 0

    def merge(self, other: "TermCandidate") -> None:
        self.readings = _unique_atoms([*self.readings, *other.readings], limit=12)
        self.aliases = _unique_atoms([*self.aliases, *other.aliases], limit=16)
        self.confusables = _unique_atoms([*self.confusables, *other.confusables], limit=16)
        self.topic_entities = _unique_atoms(
            [*self.topic_entities, *other.topic_entities], limit=16
        )
        self.active_from = min(self.active_from, other.active_from)
        self.active_until = max(self.active_until, other.active_until)
        source_by_url = {source.url: source for source in self.sources}
        for source in other.sources:
            prior = source_by_url.get(source.url)
            if prior is None or source.published_at < prior.published_at:
                source_by_url[source.url] = source
        self.sources = sorted(
            source_by_url.values(), key=lambda item: (item.published_at, item.url), reverse=True
        )[:8]
        self.score = max(self.score, other.score)
        self.display_name = self.display_name or other.display_name
        self.reason = self.reason or other.reason

    def as_snapshot_term(self) -> dict[str, object]:
        result: dict[str, object] = {
            "canonical": self.canonical,
            "readings": self.readings,
            "aliases": self.aliases,
            "confusables": self.confusables,
            "topic_entities": self.topic_entities,
            "active_from": self.active_from.isoformat(),
            "active_until": self.active_until.isoformat(),
            "sources": [source.as_dict() for source in self.sources],
        }
        if self.display_name:
            result["display_name"] = self.display_name
        if self.reason:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True)
class CachedResponse:
    body: bytes
    content_type: str
    fetched_at: dt.datetime
    stale: bool = False


class HttpCache:
    def __init__(
        self,
        root: Path,
        *,
        max_body_bytes: int,
        max_entries: int = 64,
        max_total_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if max_entries < 1 or max_total_bytes < max_body_bytes:
            raise ValueError("invalid cache budget")
        self.root = root
        self.max_body_bytes = max_body_bytes
        self.max_entries = max_entries
        self.max_total_bytes = max_total_bytes

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def load(self, key: str) -> CachedResponse | None:
        path = self._path(key)
        try:
            if path.is_symlink():
                return None
            if path.stat().st_size > self.max_body_bytes * 2 + 4096:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            body = base64.b64decode(payload["body_b64"], validate=True)
            fetched_at = dt.datetime.fromisoformat(payload["fetched_at"])
            if fetched_at.tzinfo is None or len(body) > self.max_body_bytes:
                return None
            if hashlib.sha256(body).hexdigest() != payload["sha256"]:
                return None
            return CachedResponse(body, str(payload["content_type"]), fetched_at)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def store(self, key: str, response: CachedResponse) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": response.fetched_at.isoformat(),
            "content_type": response.content_type,
            "sha256": hashlib.sha256(response.body).hexdigest(),
            "body_b64": base64.b64encode(response.body).decode("ascii"),
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        fd, temporary_name = tempfile.mkstemp(prefix=".fetch-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        self.prune()

    def prune(self) -> None:
        try:
            files = [
                item
                for item in self.root.glob("*/*.json")
                if item.is_file() and not item.is_symlink()
            ]
            files.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
        except OSError:
            return
        total = 0
        for index, path in enumerate(files):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            total += size
            if index >= self.max_entries or total > self.max_total_bytes:
                try:
                    path.unlink()
                except OSError:
                    pass


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        parsed = urllib.parse.urlsplit(newurl)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(
            host == item or host.endswith("." + item) for item in self.allowed_hosts
        ):
            raise CrawlError("redirect left the HTTPS source allowlist")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class BoundedHttpClient:
    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
        max_requests: int = 8,
        max_response_bytes: int = 2 * 1024 * 1024,
        timeout_seconds: float = 15.0,
        cache: HttpCache | None = None,
        cache_ttl: dt.timedelta = dt.timedelta(hours=18),
        stale_if_error: dt.timedelta = dt.timedelta(days=7),
        offline: bool = False,
        now: dt.datetime | None = None,
    ) -> None:
        if max_requests < 1 or max_response_bytes < 1024 or timeout_seconds <= 0:
            raise ValueError("invalid HTTP budget")
        self.allowed_hosts = allowed_hosts
        self.max_requests = max_requests
        self.max_response_bytes = max_response_bytes
        self.timeout_seconds = timeout_seconds
        self.cache = cache
        self.cache_ttl = cache_ttl
        self.stale_if_error = stale_if_error
        self.offline = offline
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self.requests_made = 0
        self.cache_hits = 0
        self.stale_hits = 0
        self._opener = urllib.request.build_opener(_SafeRedirectHandler(allowed_hosts))

    def _validate_endpoint(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port:
            raise CrawlError("fetch endpoint must be credential-free HTTPS")
        if not any(host == item or host.endswith("." + item) for item in self.allowed_hosts):
            raise CrawlError(f"fetch host is not allowlisted: {host}")

    @staticmethod
    def _cache_key(method: str, url: str, body: bytes | None, content_type: str) -> str:
        material = b"\0".join(
            [method.encode("ascii"), url.encode("utf-8"), content_type.encode("ascii"), body or b""]
        )
        return hashlib.sha256(material).hexdigest()

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        content_type: str = "",
    ) -> CachedResponse:
        method = method.upper()
        if method not in {"GET", "POST"}:
            raise CrawlError("only GET and POST are allowed")
        self._validate_endpoint(url)
        key = self._cache_key(method, url, body, content_type)
        cached = self.cache.load(key) if self.cache else None
        if cached and self.now - cached.fetched_at <= self.cache_ttl:
            self.cache_hits += 1
            return cached
        if self.offline:
            if cached and self.now - cached.fetched_at <= self.stale_if_error:
                self.stale_hits += 1
                return CachedResponse(cached.body, cached.content_type, cached.fetched_at, stale=True)
            raise CrawlError(f"offline cache miss for {url}")
        if self.requests_made >= self.max_requests:
            raise FetchLimitError("network request budget exhausted")
        self.requests_made += 1
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, application/rss+xml, text/xml"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > self.max_response_bytes:
                    raise FetchLimitError("response Content-Length exceeds budget")
                response_body = response.read(self.max_response_bytes + 1)
                if len(response_body) > self.max_response_bytes:
                    raise FetchLimitError("response body exceeds budget")
                result = CachedResponse(
                    response_body,
                    response.headers.get_content_type(),
                    self.now,
                )
                if self.cache:
                    self.cache.store(key, result)
                return result
        except (OSError, ValueError, urllib.error.URLError):
            if cached and self.now - cached.fetched_at <= self.stale_if_error:
                self.stale_hits += 1
                return CachedResponse(cached.body, cached.content_type, cached.fetched_at, stale=True)
            raise


class Adapter(Protocol):
    name: str

    def collect(
        self,
        client: BoundedHttpClient,
        window: CrawlWindow,
        existing: dict[str, TermCandidate],
    ) -> list[TermCandidate]: ...


class SeedSnapshotAdapter:
    name = "trusted_seed_snapshot"

    def __init__(self, path: Path) -> None:
        self.path = path

    def collect(
        self,
        client: BoundedHttpClient,
        window: CrawlWindow,
        existing: dict[str, TermCandidate],
    ) -> list[TermCandidate]:
        del client, existing
        from scripts.gemini_slice_jingting import load_validated_timely_terms_snapshot

        payload = load_validated_timely_terms_snapshot(self.path)
        results: list[TermCandidate] = []
        for term in payload["terms"]:
            active_from = dt.date.fromisoformat(str(term["active_from"]))
            active_until = dt.date.fromisoformat(str(term["active_until"]))
            if active_until < window.start or active_from > window.end:
                continue
            sources = [
                Provenance(
                    url=str(source["url"]),
                    published_at=dt.date.fromisoformat(str(source["published_at"])),
                    publisher=str(source["publisher"]),
                )
                for source in term["sources"]
            ]
            results.append(
                TermCandidate(
                    canonical=str(term["canonical"]),
                    readings=list(term["readings"]),
                    aliases=list(term["aliases"]),
                    confusables=list(term["confusables"]),
                    topic_entities=list(term["topic_entities"]),
                    active_from=active_from,
                    active_until=active_until,
                    sources=sources,
                    display_name=str(term.get("display_name") or "") or None,
                    reason=str(term.get("reason") or "") or None,
                    score=1_000_000,
                )
            )
        return results


class AniListSeasonAdapter:
    name = "anilist_anime_catalog"

    _QUERY = """
query TimelyAnime($page:Int!,$perPage:Int!,$start:FuzzyDateInt!,$end:FuzzyDateInt!){
  Page(page:$page,perPage:$perPage){
    pageInfo{hasNextPage}
    media(type:ANIME,startDate_greater:$start,startDate_lesser:$end,sort:POPULARITY_DESC){
      id popularity status updatedAt siteUrl
      title{romaji english native}
      synonyms
      startDate{year month day}
      endDate{year month day}
      relations{nodes{title{romaji english native}}}
    }
  }
}
""".strip()

    def __init__(self, endpoint: str, *, max_pages: int = 3, per_page: int = 50) -> None:
        if not 1 <= max_pages <= 5 or not 1 <= per_page <= 50:
            raise ValueError("AniList page bounds are invalid")
        self.endpoint = endpoint
        self.max_pages = max_pages
        self.per_page = per_page

    def collect(
        self,
        client: BoundedHttpClient,
        window: CrawlWindow,
        existing: dict[str, TermCandidate],
    ) -> list[TermCandidate]:
        del existing
        results: list[TermCandidate] = []
        start_bound = int((window.start - dt.timedelta(days=1)).strftime("%Y%m%d"))
        end_bound = int((window.end + dt.timedelta(days=1)).strftime("%Y%m%d"))
        for page in range(1, self.max_pages + 1):
            body = json.dumps(
                {
                    "query": self._QUERY,
                    "variables": {
                        "page": page,
                        "perPage": self.per_page,
                        "start": start_bound,
                        "end": end_bound,
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
            response = client.fetch(
                self.endpoint,
                method="POST",
                body=body,
                content_type="application/json",
            )
            try:
                payload = json.loads(response.body)
                page_data = payload["data"]["Page"]
                media = page_data["media"]
            except (UnicodeError, json.JSONDecodeError, TypeError, KeyError) as exc:
                raise CrawlError("AniList returned malformed structured data") from exc
            if not isinstance(media, list):
                raise CrawlError("AniList media is not a list")
            for record in media:
                candidate = self._candidate(record, window, topic="Anime")
                if candidate:
                    results.append(candidate)
            if not bool(page_data.get("pageInfo", {}).get("hasNextPage")):
                break
        return results

    @staticmethod
    def _candidate(
        record: object, window: CrawlWindow, *, topic: str = "Anime"
    ) -> TermCandidate | None:
        if not isinstance(record, dict):
            return None
        title = record.get("title")
        if not isinstance(title, dict):
            return None
        title_atoms = _unique_atoms(
            [title.get("english"), title.get("romaji"), title.get("native")], limit=3
        )
        if not title_atoms:
            return None
        canonical = title_atoms[0]
        start = _fuzzy_date(record.get("startDate"))
        if start is None or not window.start <= start <= window.end:
            return None
        end = _fuzzy_date(record.get("endDate")) or (start + dt.timedelta(days=180))
        updated = _timestamp_date(record.get("updatedAt"))
        if updated is None or updated > window.as_of:
            return None
        site_url = _stable_url(record.get("siteUrl"), allowed_hosts=frozenset({"anilist.co"}))
        if not site_url:
            return None
        aliases = _unique_atoms(
            [*title_atoms[1:], *(record.get("synonyms") or [])], limit=16
        )
        aliases = [item for item in aliases if item.casefold() != canonical.casefold()]
        readings = _unique_atoms([title.get("romaji"), title.get("native"), canonical], limit=12)
        related: list[object] = []
        relations = record.get("relations")
        if isinstance(relations, dict) and isinstance(relations.get("nodes"), list):
            for node in relations["nodes"][:8]:
                if isinstance(node, dict) and isinstance(node.get("title"), dict):
                    relation_title = node["title"]
                    related.extend(
                        [relation_title.get("english"), relation_title.get("romaji"), relation_title.get("native")]
                    )
        topics = _unique_atoms([topic, *related], limit=16)
        active_from = max(window.start, start - dt.timedelta(days=90))
        active_until = min(window.end, max(end, start) + dt.timedelta(days=90))
        popularity = record.get("popularity")
        score = int(popularity) if isinstance(popularity, int) and popularity >= 0 else 0
        return TermCandidate(
            canonical=canonical,
            readings=readings or [canonical],
            aliases=aliases,
            confusables=[],
            topic_entities=topics,
            active_from=active_from,
            active_until=active_until,
            sources=[Provenance(site_url, updated, "AniList structured anime catalog")],
            reason=(
                f"{topic} title in the configured nine-month lookback/"
                "six-month lookahead window."
            ),
            score=score,
        )


class AniListMangaAdapter(AniListSeasonAdapter):
    """One-page structured manga/light-novel lane for wider ACG projects."""

    name = "anilist_manga_catalog"
    _QUERY = AniListSeasonAdapter._QUERY.replace("type:ANIME", "type:MANGA")

    def __init__(self, endpoint: str, *, per_page: int = 50) -> None:
        super().__init__(endpoint, max_pages=1, per_page=per_page)

    def collect(
        self,
        client: BoundedHttpClient,
        window: CrawlWindow,
        existing: dict[str, TermCandidate],
    ) -> list[TermCandidate]:
        del existing
        results: list[TermCandidate] = []
        body = json.dumps(
            {
                "query": self._QUERY,
                "variables": {
                    "page": 1,
                    "perPage": self.per_page,
                    "start": int((window.start - dt.timedelta(days=1)).strftime("%Y%m%d")),
                    "end": int((window.end + dt.timedelta(days=1)).strftime("%Y%m%d")),
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        response = client.fetch(
            self.endpoint,
            method="POST",
            body=body,
            content_type="application/json",
        )
        try:
            media = json.loads(response.body)["data"]["Page"]["media"]
        except (UnicodeError, json.JSONDecodeError, TypeError, KeyError) as exc:
            raise CrawlError("AniList manga lane returned malformed structured data") from exc
        if not isinstance(media, list):
            raise CrawlError("AniList manga media is not a list")
        for record in media:
            candidate = self._candidate(record, window, topic="Manga")
            if candidate:
                candidate.topic_entities = _unique_atoms(
                    ["ACG project", *candidate.topic_entities], limit=16
                )
                results.append(candidate)
        return results


class BangumiCalendarAdapter:
    """Add Chinese canon candidates for current weekly anime in one request.

    Bangumi's calendar is intentionally used as a bounded enrichment source,
    not as the 15-month recall mechanism.  A Chinese name is accepted only
    when its Japanese source name exactly matches a structured AniList surface.
    """

    name = "bangumi_current_calendar"

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def collect(
        self,
        client: BoundedHttpClient,
        window: CrawlWindow,
        existing: dict[str, TermCandidate],
    ) -> list[TermCandidate]:
        response = client.fetch(self.endpoint)
        try:
            payload = json.loads(response.body)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CrawlError("Bangumi returned malformed calendar JSON") from exc
        if not isinstance(payload, list) or len(payload) > 8:
            raise CrawlError("Bangumi calendar has an unexpected shape")
        surface_index: dict[str, TermCandidate] = {}
        for candidate in existing.values():
            for surface in [candidate.canonical, *candidate.aliases, *candidate.readings]:
                key = _match_key(surface)
                if len(key) >= 2:
                    surface_index.setdefault(key, candidate)
        results: list[TermCandidate] = []
        seen_ids: set[int] = set()
        for day in payload:
            if not isinstance(day, dict) or not isinstance(day.get("items"), list):
                continue
            for item in day["items"][:100]:
                if not isinstance(item, dict):
                    continue
                subject_id = item.get("id")
                if not isinstance(subject_id, int) or subject_id in seen_ids:
                    continue
                seen_ids.add(subject_id)
                original = _clean_atom(item.get("name"))
                chinese = _clean_atom(item.get("name_cn"))
                if not original or not chinese or original.casefold() == chinese.casefold():
                    continue
                matched = surface_index.get(_match_key(original))
                if matched is None:
                    continue
                source_url = f"https://bgm.tv/subject/{subject_id}"
                results.append(
                    TermCandidate(
                        canonical=chinese,
                        readings=_unique_atoms([original, *matched.readings], limit=12),
                        aliases=_unique_atoms(
                            [original, matched.canonical, *matched.aliases], limit=16
                        ),
                        confusables=[],
                        topic_entities=_unique_atoms(
                            ["Anime", "Bangumi", *matched.topic_entities], limit=16
                        ),
                        active_from=matched.active_from,
                        active_until=matched.active_until,
                        sources=[
                            Provenance(
                                source_url,
                                window.as_of,
                                "Bangumi structured current-anime calendar",
                            )
                        ],
                        display_name=chinese,
                        reason="Chinese title matched to an existing Japanese title by exact structured-name equality.",
                        score=matched.score + 20_000,
                    )
                )
        return results


@dataclass(frozen=True)
class EventWatch:
    canonical: str
    aliases: tuple[str, ...]
    readings: tuple[str, ...]
    topic_entities: tuple[str, ...]


class RssNewsAdapter:
    name = "rss_news"

    def __init__(
        self,
        *,
        feed_name: str,
        url: str,
        publisher: str,
        allowed_categories: frozenset[str],
        event_watches: tuple[EventWatch, ...],
        source_hosts: frozenset[str],
        max_items: int = 100,
    ) -> None:
        self.feed_name = feed_name
        self.url = url
        self.publisher = publisher
        self.allowed_categories = allowed_categories
        self.event_watches = event_watches
        self.source_hosts = source_hosts
        self.max_items = max_items
        self.name = f"rss_news:{feed_name}"

    def collect(
        self,
        client: BoundedHttpClient,
        window: CrawlWindow,
        existing: dict[str, TermCandidate],
    ) -> list[TermCandidate]:
        response = client.fetch(self.url)
        try:
            root = ET.fromstring(response.body)
        except ET.ParseError as exc:
            raise CrawlError(f"{self.feed_name} returned malformed RSS") from exc
        results: list[TermCandidate] = []
        searchable: list[tuple[str, TermCandidate]] = []
        for candidate in existing.values():
            for surface in [candidate.canonical, *candidate.aliases]:
                key = _match_key(surface)
                if len(key) >= 4:
                    searchable.append((key, candidate))
        for item in root.findall(".//item")[: self.max_items]:
            title = item.findtext("title") or ""
            title_key = _match_key(title)
            if not title_key or _INSTRUCTION_RX.search(title):
                continue
            categories = {
                (node.text or "").strip().casefold() for node in item.findall("category")
            }
            if self.allowed_categories and not categories.intersection(self.allowed_categories):
                continue
            published = self._rss_date(item.findtext("pubDate"))
            if published is None or not window.start <= published <= window.as_of:
                continue
            source_url = _stable_feed_item_url(
                item.findtext("link"), allowed_hosts=self.source_hosts
            )
            if not source_url:
                continue
            source = Provenance(source_url, published, self.publisher)
            matched_canonicals: set[str] = set()
            for surface_key, candidate in searchable:
                if surface_key in title_key and candidate.canonical not in matched_canonicals:
                    matched_canonicals.add(candidate.canonical)
                    clone = TermCandidate(
                        canonical=candidate.canonical,
                        readings=list(candidate.readings),
                        aliases=list(candidate.aliases),
                        confusables=list(candidate.confusables),
                        topic_entities=_unique_atoms(
                            [*candidate.topic_entities, *sorted(categories)], limit=16
                        ),
                        active_from=candidate.active_from,
                        active_until=max(candidate.active_until, min(window.end, published + dt.timedelta(days=120))),
                        sources=[source],
                        score=candidate.score + 10_000,
                    )
                    results.append(clone)
            for watch in self.event_watches:
                if not any(
                    _surface_in_title(alias, title, title_key)
                    for alias in (watch.canonical, *watch.aliases)
                ):
                    continue
                canonical = _clean_atom(watch.canonical)
                if not canonical:
                    continue
                results.append(
                    TermCandidate(
                        canonical=canonical,
                        readings=_unique_atoms(watch.readings, limit=12) or [canonical],
                        aliases=_unique_atoms(watch.aliases, limit=16),
                        confusables=[],
                        topic_entities=_unique_atoms(watch.topic_entities, limit=16),
                        active_from=max(window.start, published - dt.timedelta(days=90)),
                        active_until=min(window.end, published + dt.timedelta(days=120)),
                        sources=[source],
                        reason="Configured ACG event name observed in a bounded news feed.",
                        score=100_000,
                    )
                )
        return results

    @staticmethod
    def _rss_date(value: str | None) -> dt.date | None:
        if not value:
            return None
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.date()
        except (TypeError, ValueError, OverflowError):
            return None


@dataclass
class CrawlResult:
    snapshot: dict[str, object]
    adapter_counts: dict[str, int]
    errors: dict[str, str]
    network_requests: int
    cache_hits: int
    stale_cache_hits: int


def _merge_candidates(
    destination: dict[str, TermCandidate], candidates: Iterable[TermCandidate]
) -> int:
    count = 0
    for candidate in candidates:
        key = candidate.canonical.casefold()
        prior = destination.get(key)
        if prior:
            prior.merge(candidate)
        else:
            destination[key] = candidate
        count += 1
    return count


def crawl(
    *,
    client: BoundedHttpClient,
    window: CrawlWindow,
    generated_at: dt.datetime,
    adapters: Iterable[Adapter],
    max_terms: int = 240,
    snapshot_ttl: dt.timedelta = dt.timedelta(days=2),
) -> CrawlResult:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    if not 1 <= max_terms <= 256:
        raise ValueError("max_terms must be between 1 and 256")
    candidates: dict[str, TermCandidate] = {}
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    for adapter in adapters:
        try:
            collected = adapter.collect(client, window, candidates)
            counts[adapter.name] = _merge_candidates(candidates, collected)
        except Exception as exc:  # adapters are deliberately failure-isolated
            errors[adapter.name] = f"{type(exc).__name__}: {exc}"
    if not candidates:
        raise CrawlError("all adapters failed or produced no safe terms")
    ranked = sorted(
        candidates.values(),
        key=lambda item: (
            item.score,
            item.active_from <= window.as_of <= item.active_until,
            max((source.published_at for source in item.sources), default=dt.date.min),
            item.canonical.casefold(),
        ),
        reverse=True,
    )[:max_terms]
    snapshot: dict[str, object] = {
        "schema_version": "lidousha-timely-terms.v1",
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "expires_at": (generated_at + snapshot_ttl).isoformat(timespec="seconds"),
        "status": "fresh",
        "terms": [item.as_snapshot_term() for item in ranked],
    }
    from scripts.gemini_slice_jingting import validate_timely_terms_payload

    normalized = validate_timely_terms_payload(snapshot)
    return CrawlResult(
        normalized,
        counts,
        errors,
        client.requests_made,
        client.cache_hits,
        client.stale_hits,
    )


def load_source_config(
    path: Path,
) -> tuple[
    AniListSeasonAdapter,
    AniListMangaAdapter,
    BangumiCalendarAdapter,
    list[RssNewsAdapter],
]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CrawlError(f"cannot load source config: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "anilist",
        "anilist_manga",
        "bangumi",
        "rss_feeds",
        "event_watches",
    }:
        raise CrawlError("source config has unknown or missing top-level fields")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise CrawlError("source config schema_version is unsupported")
    raw_anilist = payload["anilist"]
    if not isinstance(raw_anilist, dict) or set(raw_anilist) != {
        "endpoint",
        "max_pages",
        "per_page",
    }:
        raise CrawlError("source config anilist block is invalid")
    anilist = AniListSeasonAdapter(
        str(raw_anilist["endpoint"]),
        max_pages=int(raw_anilist["max_pages"]),
        per_page=int(raw_anilist["per_page"]),
    )
    raw_manga = payload["anilist_manga"]
    if not isinstance(raw_manga, dict) or set(raw_manga) != {"endpoint", "per_page"}:
        raise CrawlError("source config anilist_manga block is invalid")
    manga = AniListMangaAdapter(
        str(raw_manga["endpoint"]), per_page=int(raw_manga["per_page"])
    )
    raw_bangumi = payload["bangumi"]
    if not isinstance(raw_bangumi, dict) or set(raw_bangumi) != {"endpoint"}:
        raise CrawlError("source config bangumi block is invalid")
    bangumi = BangumiCalendarAdapter(str(raw_bangumi["endpoint"]))
    watches: list[EventWatch] = []
    if not isinstance(payload["event_watches"], list):
        raise CrawlError("event_watches must be a list")
    for raw in payload["event_watches"]:
        if not isinstance(raw, dict) or set(raw) != {
            "canonical",
            "aliases",
            "readings",
            "topic_entities",
        }:
            raise CrawlError("event watch has unknown or missing fields")
        canonical = _clean_atom(raw["canonical"])
        aliases = _unique_atoms(raw["aliases"])
        readings = _unique_atoms(raw["readings"], limit=12)
        topics = _unique_atoms(raw["topic_entities"])
        if not canonical or not readings:
            raise CrawlError("event watch contains unsafe names")
        watches.append(EventWatch(canonical, tuple(aliases), tuple(readings), tuple(topics)))
    rss_adapters: list[RssNewsAdapter] = []
    if not isinstance(payload["rss_feeds"], list):
        raise CrawlError("rss_feeds must be a list")
    for raw in payload["rss_feeds"]:
        required = {"name", "url", "publisher", "allowed_categories", "source_hosts", "max_items"}
        if not isinstance(raw, dict) or set(raw) != required:
            raise CrawlError("RSS feed has unknown or missing fields")
        rss_adapters.append(
            RssNewsAdapter(
                feed_name=str(raw["name"]),
                url=str(raw["url"]),
                publisher=str(raw["publisher"]),
                allowed_categories=frozenset(str(item).casefold() for item in raw["allowed_categories"]),
                event_watches=tuple(watches),
                source_hosts=frozenset(str(item).lower() for item in raw["source_hosts"]),
                max_items=int(raw["max_items"]),
            )
        )
    return anilist, manga, bangumi, rss_adapters


def snapshot_json(snapshot: dict[str, object]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_snapshot_atomically(snapshot: dict[str, object], destination: Path) -> tuple[str, bool]:
    from scripts.gemini_slice_jingting import validate_timely_terms_payload

    normalized = validate_timely_terms_payload(snapshot)
    text = snapshot_json(normalized)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        if destination.read_text(encoding="utf-8") == text:
            return digest, False
    except OSError:
        pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o444)
        os.replace(temporary, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return digest, True
