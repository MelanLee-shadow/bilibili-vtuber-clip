"""Bounded, source-backed timely-term crawler for subtitle entity priors.

The crawler deliberately produces *candidate data*, never subtitle edits.  Raw
web text is discarded at the adapter boundary: only structured title fields,
dates, stable source URLs, and locally configured event names can enter the
snapshot consumed by the subtitle pipeline.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import datetime as dt
import email.utils
import html
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable, Mapping, Protocol
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from src.autoslice.bilibili_wbi import (
    WbiProtocolError,
    mixin_key as bilibili_wbi_mixin_key,
    signed_params as bilibili_wbi_signed_params,
)


SCHEMA_VERSION = "lidousha-timely-term-sources.v2"
DEFAULT_NETWORK_REQUEST_BUDGET = 14
NETWORK_RETRY_RESERVE = 1
BILIBILI_WBI_NAV_ENDPOINT = "https://api.bilibili.com/x/web-interface/nav"
DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "graphql.anilist.co",
        "anilist.co",
        "api.bgm.tv",
        "api.bilibili.com",
        "animenewsnetwork.com",
        "bilibili.com",
        "bgm.tv",
        "tv-tokyo.co.jp",
    }
)
USER_AGENT = "vtuber-slice-timely-terms/1.0 (+local bounded crawler)"
BILIBILI_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
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


def _identity_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        char
        for char in normalized
        if unicodedata.category(char)[:1] in {"L", "M", "N"}
    )


_HAN_RUN_RX = re.compile(r"[\u3400-\u9fff]{2,}")
# Compact, generic common-character subset of Unicode Unihan
# kSimplifiedVariant.  Unknown variants fail closed instead of fuzzy matching.
_CJK_SCRIPT_VARIANT_PAIRS = (
    "萬万 與与 專专 業业 東东 絲丝 丟丢 兩两 嚴严 喪丧 個个 豐丰 臨临 "
    "為为 麗丽 舉举 麼么 義义 烏乌 樂乐 喬乔 習习 鄉乡 書书 買买 亂乱 "
    "爭争 於于 虧亏 雲云 亞亚 產产 親亲 億亿 僅仅 從从 倉仓 儀仪 們们 "
    "價价 眾众 優优 會会 傳传 傷伤 倫伦 偽伪 體体 餘余 俠侠 側侧 偵侦 "
    "儉俭 債债 傾倾 償偿 兒儿 黨党 蘭兰 關关 興兴 養养 獸兽 內内 寫写 "
    "軍军 農农 沖冲 決决 況况 凍冻 淨净 準准 涼凉 減减 鳳凤 劃划 劉刘 "
    "則则 剛刚 創创 刪删 別别 劍剑 劇剧 勸劝 辦办 務务 動动 勵励 勞劳 "
    "勢势 區区 醫医 華华 協协 單单 賣卖 衛卫 卻却 廠厂 歷历 壓压 厭厌 "
    "廚厨 縣县 參参 雙双 發发 變变 葉叶 號号 嘆叹 嚇吓 嗎吗 啟启 員员 "
    "問问 喚唤 團团 園园 國国 圖图 圓圆 場场 壞坏 塊块 堅坚 壇坛 壽寿 "
    "夢梦 實实 寶宝 對对 導导 將将 層层 屬属 歲岁 島岛 嶺岭 幣币 幫帮 "
    "廣广 慶庆 應应 懷怀 戲戏 戶户 據据 擇择 擔担 擴扩 擺摆 敗败 數数 "
    "斷断 無无 時时 暫暂 術术 機机 殺杀 氣气 漢汉 湯汤 滿满 灣湾 滅灭 "
    "燈灯 爐炉 獨独 現现 畫画 異异 當当 疊叠 盡尽 監监 盤盘 睜睁 禮礼 "
    "種种 積积 穩稳 窮穷 競竞 筆笔 簡简 糧粮 級级 終终 組组 經经 結结 "
    "給给 統统 線线 練练 總总 績绩 繼继 續续 網网 羅罗 罰罚 聲声 聯联 "
    "聽听 腦脑 臉脸 舊旧 艦舰 藝艺 藥药 處处 虛虚 蟲虫 裝装 見见 規规 "
    "覺觉 計计 訊讯 討讨 訓训 記记 講讲 識识 議议 讀读 調调 談谈 請请 "
    "論论 證证 詞词 試试 話话 語语 誤误 說说 課课 誰谁 資资 質质 車车 "
    "軟软 轉转 輕轻 還还 這这 進进 遠远 選选 邊边 郵邮 錄录 錯错 鍵键 "
    "門门 開开 間间 陽阳 陰阴 隊队 際际 難难 電电 頁页 頂顶 項项 順顺 "
    "預预 領领 頭头 類类 顧顾 風风 飛飞 飲饮 飯饭 館馆 馬马 驗验 魚鱼 "
    "鳥鸟 麥麦 黃黄 點点 齊齐 龍龙"
).split()
_CJK_SIMPLIFIED_MAP = {
    pair[0]: pair[1] for pair in _CJK_SCRIPT_VARIANT_PAIRS if len(pair) == 2
}


def _cjk_script_stem_match(candidate: str, trusted: str) -> bool:
    """Conservatively match a Han stem across CJK script variants.

    Han runs are extracted after Unicode normalization and mapped through the
    generic Unicode Unihan ``kSimplifiedVariant`` subset above.  No
    term-specific aliases or fuzzy character substitutions enter this
    derivation.
    """
    candidate_stems = _HAN_RUN_RX.findall(unicodedata.normalize("NFKC", candidate))
    trusted_stems = _HAN_RUN_RX.findall(unicodedata.normalize("NFKC", trusted))
    for candidate_stem in candidate_stems:
        for trusted_stem in trusted_stems:
            candidate_key = "".join(
                _CJK_SIMPLIFIED_MAP.get(char, char) for char in candidate_stem
            )
            trusted_key = "".join(
                _CJK_SIMPLIFIED_MAP.get(char, char) for char in trusted_stem
            )
            if candidate_key == trusted_key:
                return True
    return False


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


def _is_community_provenance(source: Provenance) -> bool:
    try:
        host = (urllib.parse.urlsplit(source.url).hostname or "").lower()
    except ValueError:
        host = ""
    return (
        host == "bilibili.com"
        or host.endswith(".bilibili.com")
        or source.publisher.casefold().startswith("bilibili community")
    )


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

    def merge(
        self,
        other: "TermCandidate",
        *,
        intersect_active_window: bool = False,
    ) -> None:
        if intersect_active_window:
            merged_from = max(self.active_from, other.active_from)
            merged_until = min(self.active_until, other.active_until)
            if merged_from > merged_until:
                raise ValueError("cannot merge aliases with disjoint active windows")
        else:
            merged_from = min(self.active_from, other.active_from)
            merged_until = max(self.active_until, other.active_until)
        self.readings = _unique_atoms([*self.readings, *other.readings], limit=12)
        self.aliases = [
            alias
            for alias in _unique_atoms(
                [*self.aliases, other.canonical, *other.aliases], limit=16
            )
            if _identity_key(alias) != _identity_key(self.canonical)
        ]
        self.confusables = _unique_atoms([*self.confusables, *other.confusables], limit=16)
        self.topic_entities = _unique_atoms(
            [*self.topic_entities, *other.topic_entities], limit=16
        )
        self.active_from = merged_from
        self.active_until = merged_until
        # ``self`` is the confidence winner selected by _merge_candidates.
        # Keep its trusted provenance represented while also reserving room for
        # genuinely new corroboration from the merged candidate.
        preferred_urls = {source.url for source in self.sources}
        source_by_url = {source.url: source for source in self.sources}
        for source in other.sources:
            prior = source_by_url.get(source.url)
            if prior is None or source.published_at < prior.published_at:
                source_by_url[source.url] = source
        preferred = sorted(
            (source_by_url[url] for url in preferred_urls),
            key=lambda item: (item.published_at, item.url),
            reverse=True,
        )
        corroborating = sorted(
            (
                source
                for url, source in source_by_url.items()
                if url not in preferred_urls
            ),
            key=lambda item: (item.published_at, item.url),
            reverse=True,
        )
        # Pin one winner source, one genuinely new loser source, and one
        # non-community/official source whenever those categories exist.
        # Then fill the bounded remainder with winner provenance first.
        all_sources = [*preferred, *corroborating]
        preferred_anchor = min(
            preferred, key=lambda item: (item.published_at, item.url), default=None
        )
        corroborating_anchor = min(
            corroborating,
            key=lambda item: (item.published_at, item.url),
            default=None,
        )
        official_anchor = min(
            (
                source
                for source in all_sources
                if not _is_community_provenance(source)
            ),
            key=lambda item: (item.published_at, item.url),
            default=None,
        )
        selected: list[Provenance] = []
        selected_urls: set[str] = set()
        for source in (preferred_anchor, corroborating_anchor, official_anchor):
            if source and source.url not in selected_urls:
                selected.append(source)
                selected_urls.add(source.url)
        fill_order = [*preferred, *corroborating]
        for source in fill_order:
            if len(selected) >= 8:
                break
            if source.url not in selected_urls:
                selected.append(source)
                selected_urls.add(source.url)
        self.sources = sorted(
            selected,
            key=lambda item: (item.published_at, item.url),
            reverse=True,
        )
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

    def _validated_extra_headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        if not headers:
            return {}
        if set(headers) - {"User-Agent", "Referer"}:
            raise CrawlError("unsupported request header")
        result: dict[str, str] = {}
        user_agent = headers.get("User-Agent")
        if user_agent:
            if "\n" in user_agent or "\r" in user_agent or len(user_agent) > 256:
                raise CrawlError("unsafe User-Agent header")
            result["User-Agent"] = user_agent
        referer = headers.get("Referer")
        if referer:
            self._validate_endpoint(referer)
            result["Referer"] = referer
        return result

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        content_type: str = "",
        headers: dict[str, str] | None = None,
    ) -> CachedResponse:
        method = method.upper()
        if method not in {"GET", "POST"}:
            raise CrawlError("only GET and POST are allowed")
        self._validate_endpoint(url)
        extra_headers = self._validated_extra_headers(headers)
        header_material = "\n".join(
            f"{key}:{value}" for key, value in sorted(extra_headers.items())
        )
        key = self._cache_key(method, url, body, content_type + "\n" + header_material)
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
        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, application/rss+xml, text/xml",
            **extra_headers,
        }
        if content_type:
            request_headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
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


@dataclass(frozen=True)
class CommunityEntityWatch:
    canonical: str
    queries: tuple[str, ...]
    readings: tuple[str, ...]
    topic_entities: tuple[str, ...]


class BilibiliCommunityAdapter:
    """Discover community-used aliases from repeated structured video tags.

    Search results are untrusted community evidence.  A tag is never accepted
    on one uploader's say-so: an alias family must repeat across multiple
    videos and uploaders, and must contain both a short surface and an extended
    surface (for example ``梦限大`` and ``梦限大MewType``).  The adapter only
    emits priors; it cannot rewrite subtitle text by itself.
    """

    name = "bilibili_community"
    _HTML_TAG_RX = re.compile(r"<[^>]{1,256}>")
    _GENERIC_TAGS = frozenset(
        {
            "acg",
            "bilibili",
            "live",
            "op",
            "ed",
            "pv",
            "reaction",
            "动画",
            "动漫",
            "新番",
            "七月新番",
            "二次元",
            "虚拟偶像",
            "虚拟主播",
            "翻唱",
            "音乐",
            "日语",
            "中字",
            "完整版",
            "必剪创作",
        }
    )

    def __init__(
        self,
        *,
        endpoint: str,
        source_hosts: frozenset[str],
        entity_watches: tuple[CommunityEntityWatch, ...],
        auto_query_count: int = 3,
        max_results: int = 20,
        min_videos: int = 2,
        min_uploaders: int = 2,
    ) -> None:
        if not 0 <= auto_query_count <= 8:
            raise ValueError("Bilibili auto_query_count is invalid")
        if not 5 <= max_results <= 50:
            raise ValueError("Bilibili max_results is invalid")
        if not 2 <= min_videos <= max_results or not 2 <= min_uploaders <= max_results:
            raise ValueError("Bilibili evidence thresholds are invalid")
        self.endpoint = endpoint
        self.source_hosts = source_hosts
        self.entity_watches = entity_watches
        self.auto_query_count = auto_query_count
        self.max_results = max_results
        self.min_videos = min_videos
        self.min_uploaders = min_uploaders
        self.diagnostics: tuple[str, ...] = ()

    def collect(
        self,
        client: BoundedHttpClient,
        window: CrawlWindow,
        existing: dict[str, TermCandidate],
    ) -> list[TermCandidate]:
        targets = list(self.entity_watches)
        configured_keys = {_match_key(item.canonical) for item in targets}
        ranked_existing = sorted(
            (
                item
                for item in existing.values()
                if item.active_from <= window.as_of <= item.active_until
                and _match_key(item.canonical) not in configured_keys
            ),
            key=lambda item: (item.score, item.canonical.casefold()),
            reverse=True,
        )
        for candidate in ranked_existing[: self.auto_query_count]:
            targets.append(
                CommunityEntityWatch(
                    canonical=candidate.canonical,
                    queries=(candidate.canonical,),
                    readings=tuple(candidate.readings),
                    topic_entities=tuple(candidate.topic_entities),
                )
            )

        self.diagnostics = ()
        results: list[TermCandidate] = []
        successful_queries = 0
        failed_attempts: list[tuple[int, str, str]] = []
        rows_by_target: list[list[dict[str, object]]] = [[] for _ in targets]
        jobs = [
            (target_index, query)
            for target_index, target in enumerate(targets)
            for query in target.queries
        ]
        if not jobs:
            return []
        mixin_key = self._wbi_mixin_key(client)
        signed_at = getattr(client, "now", None)
        if not isinstance(signed_at, dt.datetime):
            signed_at = dt.datetime.combine(
                window.as_of, dt.time(tzinfo=dt.timezone.utc)
            )

        # Keep one real network request in reserve for a single bounded retry.
        # Cache hits do not consume the client's counter, so this calculation is
        # deliberately conservative.  Clients without an exposed budget retain
        # the historical collect() behavior (all configured queries, no retry).
        max_requests = getattr(client, "max_requests", None)
        requests_made = getattr(client, "requests_made", 0)
        budgeted_client = isinstance(max_requests, int)
        retry_reserved = budgeted_client and max_requests > requests_made
        if budgeted_client:
            base_limit = max(
                0, max_requests - requests_made - NETWORK_RETRY_RESERVE
            )
            scheduled_jobs = jobs[:base_limit]
        else:
            scheduled_jobs = jobs
        deferred = len(jobs) - len(scheduled_jobs)
        retry_candidates: list[tuple[int, str]] = []
        risk_circuit = False

        for job_index, (target_index, query) in enumerate(scheduled_jobs):
            try:
                rows_by_target[target_index].extend(
                    self._search(
                        client,
                        query,
                        mixin_key=mixin_key,
                        signed_at=signed_at,
                    )
                )
                successful_queries += 1
            except Exception as exc:
                failed_attempts.append(
                    (target_index, query, self._diagnostic_exception(exc))
                )
                if self._is_risk_control_error(exc):
                    risk_circuit = True
                    deferred += len(scheduled_jobs) - job_index - 1
                    break
                else:
                    retry_candidates.append((target_index, query))

        retried = False
        retry_succeeded = False
        if retry_candidates and retry_reserved and not risk_circuit:
            target_index, query = retry_candidates[0]
            if getattr(client, "requests_made", 0) < max_requests:
                retried = True
                try:
                    rows_by_target[target_index].extend(
                        self._search(
                            client,
                            query,
                            mixin_key=mixin_key,
                            signed_at=signed_at,
                        )
                    )
                    successful_queries += 1
                    retry_succeeded = True
                except Exception as exc:
                    failed_attempts.append(
                        (target_index, query, self._diagnostic_exception(exc))
                    )

        diagnostics: list[str] = []
        if failed_attempts:
            details = "; ".join(item[2] for item in failed_attempts[:3])
            retry_text = (
                " retry recovered one query" if retry_succeeded else
                " retry also failed" if retried else
                " risk-control circuit opened without retry" if risk_circuit else
                " no retry budget was available"
            )
            diagnostics.append(
                f"partial query failure: {len(failed_attempts)} failed attempt(s);"
                f"{retry_text}; {details}"
            )
        if deferred:
            diagnostics.append(
                f"query budget deferred {deferred} of {len(jobs)} planned "
                "Bilibili search query/queries while preserving one retry request"
            )
        self.diagnostics = tuple(diagnostics)

        for target_index, target in enumerate(targets):
            candidate = self._candidate_from_rows(
                target, rows_by_target[target_index], window
            )
            if candidate:
                results.append(candidate)
        if failed_attempts and not successful_queries:
            raise CrawlError(
                f"all {len(failed_attempts)} Bilibili community query attempts failed"
            )
        if jobs and not scheduled_jobs:
            raise FetchLimitError("no Bilibili community request budget remained")
        return results

    @staticmethod
    def _diagnostic_exception(exc: Exception) -> str:
        message = _SPACE_RX.sub(" ", str(exc)).strip()[:120]
        return f"{type(exc).__name__}: {message or 'no detail'}"

    @staticmethod
    def _is_risk_control_error(exc: Exception) -> bool:
        message = str(exc).casefold()
        return any(
            token in message
            for token in ("412", "429", "-352", "v_voucher", "risk control")
        )

    @staticmethod
    def _wbi_mixin_key(client: BoundedHttpClient) -> str:
        response = client.fetch(
            BILIBILI_WBI_NAV_ENDPOINT,
            headers={
                "User-Agent": BILIBILI_USER_AGENT,
                "Referer": "https://www.bilibili.com/",
            },
        )
        if response.stale:
            raise CrawlError("Bilibili WBI bootstrap used stale fallback")
        try:
            payload = json.loads(response.body)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CrawlError("Bilibili WBI bootstrap returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise CrawlError("Bilibili WBI bootstrap returned invalid JSON")
        try:
            return bilibili_wbi_mixin_key(payload)
        except WbiProtocolError as exc:
            raise CrawlError(str(exc)) from exc

    def _search(
        self,
        client: BoundedHttpClient,
        query: str,
        *,
        mixin_key: str,
        signed_at: dt.datetime,
    ) -> list[dict[str, object]]:
        parameters = urllib.parse.urlencode(
            bilibili_wbi_signed_params(
                {
                    "search_type": "video",
                    "keyword": query,
                    "page": 1,
                    "page_size": self.max_results,
                    "order": "pubdate",
                    "platform": "pc",
                    "web_location": 1430654,
                },
                mixin_key=mixin_key,
                signed_at=signed_at,
            )
        )
        referer = "https://search.bilibili.com/video?" + urllib.parse.urlencode(
            {"keyword": query}
        )
        response = client.fetch(
            f"{self.endpoint}?{parameters}",
            headers={
                "User-Agent": BILIBILI_USER_AGENT,
                "Referer": referer,
            },
        )
        try:
            payload = json.loads(response.body)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CrawlError("Bilibili returned malformed search JSON") from exc
        if not isinstance(payload, Mapping):
            raise CrawlError("Bilibili returned malformed search JSON")
        data = payload.get("data")
        if isinstance(data, Mapping) and data.get("v_voucher"):
            raise CrawlError("Bilibili search returned v_voucher risk control")
        if payload.get("code") in {-352, -412, -429}:
            raise CrawlError(f"Bilibili search returned risk code {payload.get('code')}")
        rows = data.get("result") if isinstance(data, Mapping) else None
        if payload.get("code") != 0 or not isinstance(rows, list) or len(rows) > self.max_results:
            raise CrawlError("Bilibili search result has an unexpected shape")
        return [row for row in rows if isinstance(row, dict)]

    @classmethod
    def _plain_text(cls, value: object) -> str:
        if not isinstance(value, str):
            return ""
        return html.unescape(cls._HTML_TAG_RX.sub("", value))

    @staticmethod
    def _has_cjk(value: str) -> bool:
        return bool(re.search(r"[\u3400-\u9fff]{2,}", value))

    @staticmethod
    def _stable_uploader_id(value: object) -> str | None:
        """Return only Bilibili's stable positive numeric mid.

        Author display names and missing mids are not identities: treating one
        valid mid plus one missing mid as two uploaders defeats the quorum.
        """
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return str(value) if value > 0 else None
        if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value):
            return value
        return None

    @staticmethod
    def _title_explicitly_links(
        title: str, alias: str, anchor_surfaces: Iterable[str]
    ) -> bool:
        """Recognize title-level alias/equivalence notation, not co-mention.

        Only semantic markers such as ``中文名`` or ``又名`` qualify.  Ordinary
        parentheses/brackets and mere co-mention are intentionally insufficient.
        """
        text = unicodedata.normalize("NFKC", title)
        alias_pattern = re.escape(unicodedata.normalize("NFKC", alias))
        marker = r"(?:中文(?:名|译名)|又名|简称|即|=|＝)"
        bridge = rf"\s*(?:\(\s*)?{marker}\s*[:：]?\s*"
        for surface in anchor_surfaces:
            anchor = unicodedata.normalize("NFKC", surface)
            if not anchor or _match_key(anchor) == _match_key(alias):
                continue
            anchor_pattern = re.escape(anchor)
            patterns = (
                rf"{anchor_pattern}{bridge}{alias_pattern}",
                rf"{alias_pattern}{bridge}{anchor_pattern}",
            )
            if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
                return True
        return False

    @classmethod
    def _related_family(
        cls,
        surfaces: dict[str, dict[str, object]],
        *,
        min_videos: int = 2,
        min_uploaders: int = 2,
    ) -> list[str]:
        names = sorted(surfaces, key=lambda name: (name.casefold(), name))
        adjacency: dict[str, set[str]] = {name: set() for name in names}
        for index, left in enumerate(names):
            left_key = _match_key(left)
            for right in names[index + 1 :]:
                right_key = _match_key(right)
                shorter, longer = sorted((left_key, right_key), key=len)
                shared_videos = set(surfaces[left]["videos"]).intersection(
                    surfaces[right]["videos"]
                )
                left_video_uploaders = surfaces[left].get("video_uploaders", {})
                right_video_uploaders = surfaces[right].get("video_uploaders", {})
                shared_uploaders = {
                    left_video_uploaders.get(video)
                    for video in shared_videos
                    if left_video_uploaders.get(video)
                    and left_video_uploaders.get(video)
                    == right_video_uploaders.get(video)
                }
                if len(shorter) >= 3 and shorter in longer and (
                    cls._has_cjk(left) or cls._has_cjk(right)
                ) and len(shared_videos) >= min_videos and len(
                    shared_uploaders
                ) >= min_uploaders:
                    adjacency[left].add(right)
                    adjacency[right].add(left)
        components: list[list[str]] = []
        unseen = set(names)
        while unseen:
            first = min(unseen, key=lambda name: (name.casefold(), name))
            unseen.remove(first)
            stack = [first]
            component = [first]
            while stack:
                current = stack.pop()
                for neighbor in sorted(
                    adjacency[current], key=lambda name: (name.casefold(), name)
                ):
                    if neighbor in unseen:
                        unseen.remove(neighbor)
                        stack.append(neighbor)
                        component.append(neighbor)
            if len(component) >= 2:
                ordered = sorted(component, key=lambda name: (name.casefold(), name))
                base = min(
                    ordered,
                    key=lambda name: (len(_match_key(name)), name.casefold(), name),
                )
                if (
                    (
                        surfaces[base].get("trusted_stem_match")
                        or surfaces[base].get("explicit_title_links")
                    )
                    and len(surfaces[base].get("title_cooccurrence_videos", set()))
                    >= min_videos
                    and len(
                        surfaces[base].get("title_cooccurrence_uploaders", set())
                    )
                    >= min_uploaders
                ):
                    components.append(ordered)
        if not components:
            return []
        components.sort(
            key=lambda component: (
                -sum(
                    len(surfaces[name].get("explicit_title_links", set()))
                    for name in component
                ),
                -sum(len(surfaces[name]["videos"]) for name in component),
                -sum(len(surfaces[name]["uploaders"]) for name in component),
                min(len(name) for name in component),
                tuple((name.casefold(), name) for name in component),
            )
        )
        return sorted(
            components[0],
            key=lambda name: (not cls._has_cjk(name), len(name), name.casefold()),
        )

    def _candidate_from_rows(
        self,
        target: CommunityEntityWatch,
        rows: list[dict[str, object]],
        window: CrawlWindow,
    ) -> TermCandidate | None:
        anchor_surfaces = tuple(
            surface
            for surface in (target.canonical, *target.queries, *target.readings)
            if len(_match_key(surface)) >= 3
        )
        anchor_keys = {
            _match_key(surface)
            for surface in anchor_surfaces
        }
        surfaces: dict[str, dict[str, object]] = {}
        for row in rows:
            published = _timestamp_date(row.get("pubdate"))
            if published is None or not window.start <= published <= window.as_of:
                continue
            url = _stable_feed_item_url(row.get("arcurl"), allowed_hosts=self.source_hosts)
            if not url:
                continue
            title = self._plain_text(row.get("title"))
            description = self._plain_text(row.get("description"))
            tag_text = self._plain_text(row.get("tag"))
            title_key = _match_key(title)
            anchor_in_title = any(anchor in title_key for anchor in anchor_keys)
            combined_key = _match_key(" ".join((title, description, tag_text)))
            if not any(anchor in combined_key for anchor in anchor_keys):
                continue
            uploader = self._stable_uploader_id(row.get("mid"))
            author = _clean_atom(row.get("author")) or "unknown uploader"
            source = Provenance(url, published, f"Bilibili community video by {author}")
            for raw_tag in re.split(r"[,，]", tag_text):
                tag = _clean_atom(raw_tag, max_chars=32)
                if not tag:
                    continue
                tag_key = _match_key(tag)
                if (
                    len(tag_key) < 3
                    or tag_key in anchor_keys
                    or tag.casefold() in self._GENERIC_TAGS
                    or tag_key.isdigit()
                ):
                    continue
                stats = surfaces.setdefault(
                    tag,
                    {
                        "videos": set(),
                        "uploaders": set(),
                        "sources": {},
                        "explicit_title_links": set(),
                        "title_cooccurrence_videos": set(),
                        "title_cooccurrence_uploaders": set(),
                        "video_uploaders": {},
                        "trusted_stem_match": False,
                    },
                )
                stats["videos"].add(url)
                if uploader:
                    stats["uploaders"].add(uploader)
                    prior_uploader = stats["video_uploaders"].get(url)
                    if prior_uploader is None:
                        stats["video_uploaders"][url] = uploader
                    elif prior_uploader != uploader:
                        stats["video_uploaders"][url] = ""
                stats["sources"][url] = source
                if anchor_in_title and tag_key in title_key:
                    stats["title_cooccurrence_videos"].add(url)
                    if uploader:
                        stats["title_cooccurrence_uploaders"].add(uploader)
                if self._title_explicitly_links(title, tag, anchor_surfaces):
                    stats["explicit_title_links"].add(url)
                if any(
                    _cjk_script_stem_match(tag, trusted)
                    for trusted in (target.canonical, *target.readings)
                ):
                    stats["trusted_stem_match"] = True
        eligible = {
            name: stats
            for name, stats in surfaces.items()
            if len(stats["videos"]) >= self.min_videos
            and len(stats["uploaders"]) >= self.min_uploaders
        }
        family = self._related_family(
            eligible,
            min_videos=self.min_videos,
            min_uploaders=self.min_uploaders,
        )
        if not family:
            return None
        source_by_url: dict[str, Provenance] = {}
        for alias in family:
            source_by_url.update(eligible[alias]["sources"])
        sources = sorted(
            source_by_url.values(), key=lambda item: (item.published_at, item.url), reverse=True
        )[:8]
        first_date = min(source.published_at for source in sources)
        aliases = _unique_atoms(family, limit=16)
        display_name = next((name for name in aliases if self._has_cjk(name)), aliases[0])
        return TermCandidate(
            canonical=target.canonical,
            readings=_unique_atoms([*target.readings, *target.queries], limit=12),
            aliases=aliases,
            confusables=[],
            topic_entities=_unique_atoms(
                [*target.topic_entities, "Bilibili community", "二次元社区"], limit=16
            ),
            active_from=max(window.start, first_date),
            active_until=min(window.end, window.as_of + dt.timedelta(days=120)),
            sources=sources,
            display_name=display_name,
            reason=(
                "Base alias is derived from a trusted CJK stem or an explicit "
                "semantic title marker, repeats in titles across distinct uploaders, "
                "and has thresholded shared-video tag support for its family."
            ),
            score=300_000 + sum(len(eligible[name]["videos"]) for name in family),
        )


@dataclass
class CrawlResult:
    snapshot: dict[str, object]
    adapter_counts: dict[str, int]
    errors: dict[str, str]
    network_requests: int
    cache_hits: int
    stale_cache_hits: int
    diagnostics: dict[str, list[str]] = field(default_factory=dict)


def _canonical_alias_match(left: TermCandidate, right: TermCandidate) -> bool:
    left_canonical = _identity_key(left.canonical)
    right_canonical = _identity_key(right.canonical)
    if left_canonical == right_canonical:
        return True
    left_aliases = {_identity_key(alias) for alias in left.aliases}
    right_aliases = {_identity_key(alias) for alias in right.aliases}
    return left_canonical in right_aliases or right_canonical in left_aliases


def _active_windows_overlap(left: TermCandidate, right: TermCandidate) -> bool:
    return max(left.active_from, right.active_from) <= min(
        left.active_until, right.active_until
    )


def _merge_candidates(
    destination: dict[str, TermCandidate],
    candidates: Iterable[TermCandidate],
    *,
    conflict_diagnostics: list[str] | None = None,
) -> int:
    count = 0
    for candidate in candidates:
        matches = [
            (key, prior)
            for key, prior in destination.items()
            if _canonical_alias_match(prior, candidate)
        ]
        if len(matches) > 1:
            if conflict_diagnostics is not None:
                matched = ", ".join(
                    sorted(prior.canonical for _, prior in matches)
                )
                conflict_diagnostics.append(
                    f"entity_merge conflict: candidate {candidate.canonical} matched "
                    f"multiple existing canonicals ({matched}); candidate dropped"
                )
            count += 1
            continue
        if not matches:
            destination[_identity_key(candidate.canonical)] = candidate
            count += 1
            continue

        prior_key, prior = matches[0]
        cross_canonical_alias = _identity_key(prior.canonical) != _identity_key(
            candidate.canonical
        )
        if cross_canonical_alias and not _active_windows_overlap(prior, candidate):
            if conflict_diagnostics is not None:
                conflict_diagnostics.append(
                    f"entity_merge conflict: candidate {candidate.canonical} has a "
                    f"disjoint active window from {prior.canonical}; candidate dropped"
                )
            count += 1
            continue
        group = [candidate, prior]
        winner = min(
            group,
            key=lambda item: (-item.score, item.canonical.casefold(), item.canonical),
        )
        losers = sorted(
            (item for item in group if item is not winner),
            key=lambda item: (-item.score, item.canonical.casefold(), item.canonical),
        )
        for loser in losers:
            winner.merge(
                loser,
                intersect_active_window=cross_canonical_alias,
            )
        del destination[prior_key]
        destination[_identity_key(winner.canonical)] = winner
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
    diagnostics: dict[str, list[str]] = {}
    for adapter in adapters:
        try:
            collected = adapter.collect(client, window, candidates)
            merge_conflicts: list[str] = []
            counts[adapter.name] = _merge_candidates(
                candidates,
                collected,
                conflict_diagnostics=merge_conflicts,
            )
            if merge_conflicts:
                diagnostics.setdefault("entity_merge", []).extend(
                    f"{adapter.name}: {message}" for message in merge_conflicts
                )
            adapter_diagnostics = getattr(adapter, "diagnostics", ())
            if adapter_diagnostics:
                diagnostics[adapter.name] = [str(item) for item in adapter_diagnostics]
        except Exception as exc:  # adapters are deliberately failure-isolated
            errors[adapter.name] = f"{type(exc).__name__}: {exc}"
            adapter_diagnostics = getattr(adapter, "diagnostics", ())
            if adapter_diagnostics:
                diagnostics[adapter.name] = [str(item) for item in adapter_diagnostics]
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
        snapshot=normalized,
        adapter_counts=counts,
        errors=errors,
        network_requests=client.requests_made,
        cache_hits=client.cache_hits,
        stale_cache_hits=client.stale_hits,
        diagnostics=diagnostics,
    )


def load_source_config(
    path: Path,
) -> tuple[
    AniListSeasonAdapter,
    AniListMangaAdapter,
    BangumiCalendarAdapter,
    list[RssNewsAdapter],
    BilibiliCommunityAdapter,
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
        "bilibili_community",
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
    raw_bilibili = payload["bilibili_community"]
    bilibili_required = {
        "endpoint",
        "source_hosts",
        "auto_query_count",
        "max_results",
        "min_videos",
        "min_uploaders",
        "entity_watches",
    }
    if not isinstance(raw_bilibili, dict) or set(raw_bilibili) != bilibili_required:
        raise CrawlError("bilibili_community block is invalid")
    community_watches: list[CommunityEntityWatch] = []
    if not isinstance(raw_bilibili["entity_watches"], list):
        raise CrawlError("Bilibili entity_watches must be a list")
    for raw in raw_bilibili["entity_watches"]:
        required = {"canonical", "queries", "readings", "topic_entities"}
        if not isinstance(raw, dict) or set(raw) != required:
            raise CrawlError("Bilibili entity watch has unknown or missing fields")
        canonical = _clean_atom(raw["canonical"])
        queries = _unique_atoms(raw["queries"], limit=4)
        readings = _unique_atoms(raw["readings"], limit=12)
        topics = _unique_atoms(raw["topic_entities"], limit=16)
        if not canonical or not queries or not readings:
            raise CrawlError("Bilibili entity watch contains unsafe names")
        community_watches.append(
            CommunityEntityWatch(canonical, tuple(queries), tuple(readings), tuple(topics))
        )
    bilibili = BilibiliCommunityAdapter(
        endpoint=str(raw_bilibili["endpoint"]),
        source_hosts=frozenset(str(item).lower() for item in raw_bilibili["source_hosts"]),
        entity_watches=tuple(community_watches),
        auto_query_count=int(raw_bilibili["auto_query_count"]),
        max_results=int(raw_bilibili["max_results"]),
        min_videos=int(raw_bilibili["min_videos"]),
        min_uploaders=int(raw_bilibili["min_uploaders"]),
    )
    return anilist, manga, bangumi, rss_adapters, bilibili


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
