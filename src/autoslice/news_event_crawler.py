"""Daily crawl of circle news: events, projects, alliances and collaborations, as a candidate entity graph.

Seeds come from data the pipeline already has (news_event_seeds); the Search Router's `free_search` finds recent pages
about each seed (reusing the manual lane's `WebLookup` client and its address from the channel profile); CPA decides
which results show an event / project / alliance / collaboration, who takes part and how (news_event_judge); the
answer becomes status=candidate nodes and edges in the format of `knowledge_graph` (news_event_graph).  The recent
dynamics of the owner and of the registry accounts are judged too, as results of their own (`post_batches`: the channel's request).  A run that
cannot do its job raises `NewsEventError` (the entry point exits non-zero and the timer unit shows it): no search
succeeded, CPA answered nothing, or the graph did not validate.  Partial failure is counted in the report.
"""
from __future__ import annotations

import datetime as dt
import inspect
import json
import random
import re
import time
import urllib.error
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, cast

from src.autoslice import community_name_graph as names
from src.autoslice import news_event_graph as store
from src.autoslice.crawl_window import CrawlWindow
from src.autoslice import clip_name_identity as identity
from src.autoslice import news_event_judge as judge
from src.autoslice.infra_retry_policy import terminal_retry_scope
from src.autoslice.knowledge_graph import Graph, GraphError, JsonValue, Node, load_graph
from src.autoslice.llm_client import LlmCallError
from src.autoslice.manual_lane.work_lookup import LookupRow
from src.autoslice.news_event_seeds import (
    WORK_SEEDS_FILE, Dynamics, Registry, Seed, WorkSeed, derive_seeds, dynamics_texts, load_work_seeds, plain_text)
from src.autoslice.structured_output import call_json

_call_json: Callable[[Callable[[str], str], str], str] = call_json  # structured_output.call_json is untyped
REPORT_SCHEMA = "vtuber-slice.news-event-crawl.v1"
MAX_SEEDS = 40               # one run's searches; the roster part of the list rotates day by day
MAX_WORK_SEEDS_PER_RUN = 6   # rotating subset of class work seeds searched for work/character nodes each run
WORK_BUDGET_S = 300.0        # isolated budget for optional work seeds after the news write
WORK_QUERY = "{work} 角色 登场人物"
SEARCH_ROWS = 8
SEEDS_PER_JUDGE_CALL = 4     # keeps one CPA prompt well below the community crawler's 16-card / 156 KiB incident
MIN_INTERVAL_S = 1.0         # gap between searches; doubles after each rate-limited answer, relaxes on success
MAX_INTERVAL_S = 60.0
RATE_LIMIT_ATTEMPTS = 6
MAX_WAIT_S = 120.0
MAX_ERRORS_LISTED = 20
RETRY_QUERY = "{seed} 联动 企划"
OWNER_QUERY = "{owner} 联动 企划 最新"   # a seed is searched as "<seed> <owner>": extra topic words pull in unrelated pages
POST_DAYS = 3                # a dynamic posted this many days before the run or later is read as news (runs are daily)
POST_SEED = "B站动态·{name}"  # the seed of an account's own dynamics; news_event_judge.judge_prompt rule 8 reads it
OPUS_URL = "https://www.bilibili.com/opus/{id}"
_BEIJING = dt.timezone(dt.timedelta(hours=8))

Posts = Mapping[str, Sequence[Mapping[str, str]]]   # account -> [{"dynamic_id", "published_at", "text"}]


class SearchBackend(Protocol):
    def web(self, query: str, *, limit: int = ...) -> Sequence[LookupRow]: ...


class _TimedSearchBackend(Protocol):
    def web(self, query: str, *, limit: int = ..., timeout: float | None = ...) -> Sequence[LookupRow]: ...


class _TimedSecondsLlm(Protocol):
    def __call__(self, prompt: str, *, timeout_seconds: float | None = ...) -> str: ...


class _TimedLlm(Protocol):
    def __call__(self, prompt: str, *, timeout: float | None = ...) -> str: ...


def _accepts_kwarg(fn: object, name: str) -> bool:
    try:
        params = inspect.signature(cast(Callable[..., object], fn)).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


@dataclass
class Report:
    """What one run counted; `to_json` is the crawl_report.json written next to the graph."""
    generated_at: str
    owner: str
    inputs: dict[str, bool]
    errors: list[str] = field(default_factory=list)
    seeds: list[Seed] = field(default_factory=list)
    search: Counter[str] = field(default_factory=Counter)
    cpa: Counter[str] = field(default_factory=Counter)
    rejected: Counter[str] = field(default_factory=Counter)
    per_seed: dict[str, dict[str, int]] = field(default_factory=dict)
    new_nodes: list[str] = field(default_factory=list)
    new_edges: list[str] = field(default_factory=list)
    left_out: int = 0
    candidates: dict[str, int] = field(default_factory=dict)
    previous_discarded: str | None = None
    community: dict[str, JsonValue] | None = None
    clip_names: dict[str, JsonValue] | None = None
    works: dict[str, JsonValue] | None = None

    def error(self, text: str) -> None:
        if len(self.errors) < MAX_ERRORS_LISTED:
            self.errors.append(text[:300])

    def to_json(self) -> dict[str, JsonValue]:
        counts: dict[str, JsonValue] = {
            "search": {k: v for k, v in self.search.items()}, "cpa": {k: v for k, v in self.cpa.items()},
            "rejected_reasons": {k: v for k, v in self.rejected.items()},
            "per_seed": {k: {n: c for n, c in v.items()} for k, v in self.per_seed.items()},
            "candidates": {k: v for k, v in self.candidates.items()}}
        names: list[JsonValue] = [s.name for s in self.seeds]
        origins: dict[str, JsonValue] = {k: v for k, v in Counter(s.origin for s in self.seeds).items()}
        doc: dict[str, JsonValue] = counts | {
            "schema_version": REPORT_SCHEMA, "generated_at": self.generated_at, "owner": self.owner,
            "inputs": {k: v for k, v in self.inputs.items()}, "errors": [e for e in self.errors],
            "seeds": {"count": len(self.seeds), "by_origin": origins, "names": names},
            "findings": {"new_nodes": [n for n in self.new_nodes], "new_edges": [e for e in self.new_edges],
                         "left_out_id_collision": self.left_out},
            "previous_discarded": self.previous_discarded}
        doc["community"] = self.community
        doc["clip_names"] = self.clip_names
        doc["works"] = self.works
        return doc


class NewsEventError(RuntimeError):
    """The run cannot deliver; `code` names why and `report` holds what was counted until then."""

    def __init__(self, code: str, detail: str, report: Report | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.report = report


def _work_is_covered(seed: WorkSeed, known: Graph, *, resolved: Node | None = None) -> bool:
    """True when the seed resolves to a non-retired `work` node with at least one `character_of` edge."""
    node = resolved if resolved is not None else store.resolve_work_seed(known, name=seed.name, aliases=seed.aliases)
    if node is None or node.kind != "work":
        return False
    return any(
        e.type == "character_of" and e.to_id == node.id
        and e.from_id in known.nodes and known.nodes[e.from_id].kind == "character"
        for e in known.edges
    )


def _load_work_cursor(runtime_root: Path) -> int:
    """Read the persisted work-seed rotation cursor from the previous crawl report, or 0."""
    try:
        raw: JsonValue = json.loads((Path(runtime_root) / store.REPORT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    works = raw.get("works") if isinstance(raw, dict) else None
    cursor = works.get("next_cursor") if isinstance(works, dict) else None
    return cursor if type(cursor) is int and cursor >= 0 else 0


def _call_work_llm(llm_call: Callable[[str], str], prompt: str, *, deadline: float,
                   clock: Callable[[], float], sleep: Callable[[float], None]) -> str:
    """Run one work-seed CPA judgement bounded by the remaining work pass wall-clock budget.

    Forwards the remaining budget both via `terminal_retry_scope` and via `timeout_seconds`/`timeout` when the
    callable accepts it.  A call already in flight can still overrun `deadline` by at most its own per-call timeout.
    """
    remaining = deadline - clock()
    if remaining <= 0:
        raise NewsEventError("runtime_budget_exhausted", "the work-seed time budget ends before model judgement")
    timed_call: Callable[[str], str] = llm_call
    if _accepts_kwarg(llm_call, "timeout_seconds"):
        def with_seconds(p: str) -> str:
            return cast(_TimedSecondsLlm, llm_call)(p, timeout_seconds=remaining)
        timed_call = with_seconds
    elif _accepts_kwarg(llm_call, "timeout"):
        def with_timeout(p: str) -> str:
            return cast(_TimedLlm, llm_call)(p, timeout=remaining)
        timed_call = with_timeout
    with terminal_retry_scope(wallclock_seconds=remaining, clock=clock, sleeper=sleep):
        return _call_json(timed_call, prompt)


def _crawl_work_seeds(work_seeds: Sequence[WorkSeed], known: Graph, repo_edges: set[str], *, search: RouterSearch,
                      llm_call: Callable[[str], str], report: Report, clock: Callable[[], float],
                      deadline: float, start_cursor: int, stamp: str
                      ) -> tuple[store.Candidates, dict[str, JsonValue]]:
    """Crawl uncovered work seeds under an independent `WORK_BUDGET_S` wall-clock budget starting when the pass starts.

    Checks `clock() < deadline` before each seed and hands remaining budget as timeout to search and model adapters
    that accept one; a call already in flight can overrun `deadline` by at most its own per-call timeout.
    """
    total = len(work_seeds)
    start = start_cursor % total
    next_cursor = start
    added: store.Candidates = ({}, {})
    work_rejected: Counter[str] = Counter()
    skipped = attempted = ok = failed = 0
    for step in range(total):
        idx = (start + step) % total
        seed = work_seeds[idx]
        resolved_work = store.resolve_work_seed(known, name=seed.name, aliases=seed.aliases)
        if _work_is_covered(seed, known, resolved=resolved_work):
            skipped += 1
            next_cursor = (idx + 1) % total
            continue
        if attempted >= MAX_WORK_SEEDS_PER_RUN or clock() >= deadline:
            break
        attempted += 1
        next_cursor = (idx + 1) % total
        try:
            rows = search.search(WORK_QUERY.format(work=seed.name))
        except NewsEventError as exc:
            report.error(f"work search stopped: {exc}")
            break
        except Exception as exc:  # noqa: BLE001 - per-seed search error is counted and logged
            failed += 1
            report.error(f"work search {seed.name!r}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        if not rows:
            ok += 1
            continue
        try:
            raw = _call_work_llm(
                llm_call,
                judge.work_prompt(seed.category, seed.name, seed.aliases, judge.work_catalog(known), rows),
                deadline=deadline, clock=clock, sleep=search.sleep)
            result, rejected = judge.parse_work_judgement(
                raw, rows, seed_name=seed.name, seed_aliases=seed.aliases,
                resolved_work_id=resolved_work.id if resolved_work is not None else None, known=known)
        except NewsEventError as exc:
            report.error(f"work judge stopped: {exc}")
            break
        except (LlmCallError, judge.JudgeError) as exc:
            failed += 1
            report.error(f"work judge {seed.name!r}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        ok += 1
        report.rejected.update(rejected)
        work_rejected.update(rejected)
        if result is not None:
            more, left_out = store.candidates_from_work(
                result, known, repo_edges, now=stamp,
                seed_name=seed.name, seed_aliases=seed.aliases, resolved_work=resolved_work)
            report.left_out += left_out
            added = store.merge(added, more)
            known = store.with_candidates(known, more)
    rejected_json: dict[str, JsonValue] = {k: v for k, v in work_rejected.items()}
    stats: dict[str, JsonValue] = {
        "total": total, "skipped_covered": skipped, "attempted": attempted,
        "ok": ok, "failed": failed, "start_cursor": start, "next_cursor": next_cursor,
        "rejected": rejected_json,
        "new_nodes": list[JsonValue](sorted(added[0])), "new_edges": list[JsonValue](sorted(added[1])),
    }
    return added, stats


class RouterSearch:
    """`free_search` through WebLookup, paced: a rate-limited answer is waited out (Retry-After, else exponential
    backoff with jitter), the gap between searches adapts, and the run's deadline is never overrun."""

    def __init__(self, lookup: SearchBackend, *, deadline: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, jitter: Callable[[], float] = random.random) -> None:
        self.lookup, self.deadline, self.clock, self.sleep, self.jitter = lookup, deadline, clock, sleep, jitter
        self.interval = MIN_INTERVAL_S
        self.waits = 0
        self._last: float | None = None

    @staticmethod
    def _rate_limited(exc: Exception) -> float | None:
        """Seconds to wait if `exc` is a rate-limit answer (0 when no Retry-After), else None."""
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
            retry = exc.headers.get("Retry-After") if exc.headers else None
            return float(retry) if retry and retry.strip().isdigit() else 0.0
        if isinstance(exc, RuntimeError) and re.search(r"\b429\b", str(exc)):
            return 0.0
        return None

    def _pause(self, seconds: float) -> None:
        if self.clock() + seconds > self.deadline:
            raise NewsEventError("runtime_budget_exhausted", "the run's time budget ends before the next search")
        self.sleep(seconds)

    def search(self, query: str) -> list[judge.Row]:
        if self._last is not None and (gap := self.interval - (self.clock() - self._last)) > 0:
            self._pause(gap)
        for attempt in range(RATE_LIMIT_ATTEMPTS):
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise NewsEventError("runtime_budget_exhausted", "the run's time budget ends before the next search")
            try:
                if _accepts_kwarg(self.lookup.web, "timeout"):
                    rows = cast(_TimedSearchBackend, self.lookup).web(query, limit=SEARCH_ROWS, timeout=remaining)
                else:
                    rows = self.lookup.web(query, limit=SEARCH_ROWS)
            except Exception as exc:  # noqa: BLE001 - classified below; anything else is the caller's failure count
                retry = self._rate_limited(exc)
                if retry is None or attempt == RATE_LIMIT_ATTEMPTS - 1:
                    self._last = self.clock()
                    raise
                self.waits += 1
                self.interval = min(MAX_INTERVAL_S, self.interval * 2)
                backoff = min(MAX_WAIT_S, 2.0 ** (attempt + 1) * (1 + self.jitter()))
                self._pause(min(MAX_WAIT_S, max(retry, backoff)))
                continue
            self._last = self.clock()
            self.interval = max(MIN_INTERVAL_S, self.interval * 0.8)
            return [judge.Row(title=r.get("title"), url=str(r["url"]), snippet=r.get("snippet"))
                    for r in rows if r.get("url")]
        raise AssertionError("unreachable")


def _query(seed_name: str, owner: str) -> str:
    return OWNER_QUERY.format(owner=owner) if seed_name == owner else f"{seed_name} {owner}"


def _names_it(seed_name: str, rows: Sequence[judge.Row]) -> bool:
    """Some result carries the seed's own words.  A fuzzy engine answers almost any keyword with something, and the
    router rotates its providers, so a first answer that never mentions the seed is asked for once more."""
    key = "".join(ch for ch in seed_name.casefold() if ch.isalnum())
    return any(key in "".join(ch for ch in f"{r['title']} {r['url']} {r['snippet']}".casefold() if ch.isalnum())
               for r in rows)


def _search_seed(search: RouterSearch, report: Report, seed: Seed, owner: str) -> list[judge.Row]:
    rows = search.search(_query(seed.name, owner))
    if seed.name != owner and not _names_it(seed.name, rows):
        report.search["asked_again"] += 1
        more = search.search(RETRY_QUERY.format(seed=seed.name))
        rows = [*rows, *(r for r in more if r["url"] not in {x["url"] for x in rows})]
    return rows


def _extract_seeds(report: Report, owner: str, texts: Sequence[str], llm_call: Callable[[str], str]) -> list[str]:
    """Names CPA read out of the owner's dynamics (only those that occur in the text); a failure is counted, and the
    other seeds still run."""
    if not texts:
        return []
    try:
        names = judge.parse_mentions(_call_json(llm_call, judge.mention_prompt(owner, texts)), texts)
    except (LlmCallError, judge.JudgeError) as exc:
        report.cpa["mention_failed"] += 1
        report.error(f"mentions: {type(exc).__name__}: {exc}")
        return []
    report.cpa["mention_ok"] += 1
    return names


def _search_all(report: Report, search: RouterSearch, seeds: Sequence[Seed], owner: str
                ) -> list[tuple[str, list[judge.Row]]]:
    found: list[tuple[str, list[judge.Row]]] = []
    for seed in seeds:
        report.search["attempted"] += 1
        try:
            rows = _search_seed(search, report, seed, owner)
        except NewsEventError as exc:  # the run's time budget: stop searching, judge what was found
            report.error(f"search stopped: {exc}")
            break
        except Exception as exc:  # noqa: BLE001 - each failed source is counted, the run goes on
            report.search["failed"] += 1
            report.error(f"search {seed.name!r}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        report.search["ok"] += 1
        report.search["rows"] += len(rows)
        report.per_seed[seed.name] = {"rows": len(rows), "findings": 0}
        if rows:
            found.append((seed.name, rows))
    report.search["rate_limit_waits"] = search.waits
    return found


def post_batches(owner: str, dynamics: Dynamics | None, peers: Posts, now: dt.datetime
                 ) -> list[tuple[str, list[judge.Row]]]:
    """The owner's dynamics (the dynamics crawler's snapshot) and the registry accounts' (`peers`, the accounts step 6
    reads when it asks who is in a call) posted from POST_DAYS before `now`, as results to judge: one seed per account,
    a row per dynamic with its Beijing date in the title and its opus page as url.  A collab the host announces and her
    guests post about is news no web search finds: 11-23 the host wrote 「明天晚上八点的女大矿工见」, 安晚awa
    「矿工是对的但我要晕倒了」, and the search had 0 results for 女大矿工."""
    since = now - dt.timedelta(days=POST_DAYS)
    accounts: dict[str, Sequence[Mapping[str, str]]] = {owner: list(dynamics["items"]) if dynamics else []}
    accounts.update((name, items) for name, items in peers.items() if name != owner)
    out: list[tuple[str, list[judge.Row]]] = []
    for name, items in accounts.items():
        rows: list[judge.Row] = []
        for item in items:
            ident, text = str(item.get("dynamic_id") or ""), plain_text(str(item.get("text") or ""))
            try:
                posted = dt.datetime.fromisoformat(str(item.get("published_at") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if ident.isdigit() and text and posted.tzinfo is not None and posted >= since:
                day = posted.astimezone(_BEIJING).date().isoformat()
                rows.append(judge.Row(title=f"{name} 自己发的 B 站动态（发布于 {day}）", url=OPUS_URL.format(id=ident),
                                      snippet=text))
        if rows:
            out.append((POST_SEED.format(name=name), rows))
    return out


def _in_window(findings: Sequence[judge.Finding], window: CrawlWindow) -> tuple[list[judge.Finding], int]:
    """The findings whose time as news overlaps the window, and how many fell outside it."""
    kept = [f for f in findings if window.holds(f.valid_from, f.valid_until)]
    return kept, len(findings) - len(kept)


def _date_undated(report: Report, previous: store.Candidates, llm_call: Callable[[str], str], now: dt.datetime
                  ) -> store.Candidates:
    """Events and projects collected before every item carried its time are dated by CPA from their own summary and
    URLs; one it cannot date is dropped (its edges go in store.accumulate), since it cannot be placed in time.  A call
    that fails leaves its batch as it was, for the next run."""
    nodes = dict(previous[0])
    undated = [n for n in nodes.values() if n.kind in store.DATED_KINDS and not n.valid]
    for start in range(0, len(undated), judge.DATE_BATCH):
        batch = undated[start:start + judge.DATE_BATCH]
        items: list[dict[str, JsonValue]] = [
            {"id": n.id, "kind": n.kind, "name": n.names["display"], "summary": n.summary,
             "urls": [s.ref for s in n.sources]} for n in batch]
        report.cpa["date_attempted"] += 1
        try:
            dated = judge.parse_dates(_call_json(llm_call, judge.date_prompt(now.date().isoformat(), items)),
                                      [n.id for n in batch])
        except (LlmCallError, judge.JudgeError) as exc:
            report.cpa["date_failed"] += 1
            report.error(f"date batch {start // judge.DATE_BATCH}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        report.cpa["date_ok"] += 1
        for node in batch:
            if node.id in dated:
                nodes[node.id] = replace(node, valid=dated[node.id])
                report.cpa["dated"] += 1
            else:
                del nodes[node.id]
                report.cpa["undatable_dropped"] += 1
    return nodes, previous[1]


def crawl(*, owner: str, repo_root: Path, runtime_root: Path, registry: Registry | None,
          dynamics: Dynamics | None, search: RouterSearch, llm_call: Callable[[str], str], now: dt.datetime,
          peer_posts: Posts | None = None, max_seeds: int = MAX_SEEDS, clock: Callable[[], float] = time.monotonic,
          notes: Sequence[str] = (), clip_names: Sequence[str] = (),
          community: names.Community | None = None,
          name_videos: Callable[[str], Sequence[names.Video]] | None = None,
          origin_limit: int = names.ORIGIN_LIMIT, work_budget_s: float = WORK_BUDGET_S) -> Report:
    """One run.  Returns the report that was written next to the graph; raises NewsEventError on failure."""
    stamp = now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    window = CrawlWindow.around(now.date())
    repo = load_graph(repo_root)
    prev_work_cursor = _load_work_cursor(runtime_root)
    report = Report(stamp, owner, {"registry": registry is not None, "dynamics": dynamics is not None,
                                   "peer_posts": peer_posts is not None}, errors=list(notes))
    texts = dynamics_texts(dynamics)
    seeds = derive_seeds(owner=owner, graph=repo, registry=registry, dynamics=dynamics,
                         extracted=_extract_seeds(report, owner, texts, llm_call), day=now.toordinal(),
                         limit=max_seeds, clip_names=clip_names)
    report.seeds = seeds
    found = _search_all(report, search, seeds, owner)
    if not report.search["ok"]:
        raise NewsEventError("sources_unavailable", f"no search succeeded ({report.search['attempted']} attempted)",
                             report)

    try:
        previous = store.load_previous(repo_root, runtime_root)
    except (GraphError, OSError, ValueError) as exc:  # re-derivable from search; discarded loudly, not silently
        previous = ({}, {})
        report.previous_discarded = f"{type(exc).__name__}: {str(exc)[:300]}"
    previous = _date_undated(report, previous, llm_call, now)
    known = store.with_candidates(repo, previous)
    repo_edges = {e.id for e in repo.edges}
    fresh: store.Candidates = ({}, {})
    posts = post_batches(owner, dynamics, peer_posts or {}, now)
    for seed, rows in posts:
        report.per_seed[seed] = {"rows": len(rows), "findings": 0}
    report.search["post_rows"] = sum(len(rows) for _, rows in posts)
    # every account's dynamics in one call, so posts of several people about one collab are read side by side
    batches = ([("posts", posts)] if posts else []) + [
        (f"judge batch {start // SEEDS_PER_JUDGE_CALL}", found[start:start + SEEDS_PER_JUDGE_CALL])
        for start in range(0, len(found), SEEDS_PER_JUDGE_CALL)]
    for label, batch in batches:
        if clock() >= search.deadline:
            report.error("judge stopped: the run's time budget is spent")
            break
        report.cpa["judge_attempted"] += 1
        prompt = judge.judge_prompt(owner, now.date().isoformat(), judge.catalog(known), batch)
        try:
            got, bad = judge.parse_judgement(_call_json(llm_call, prompt), batch)
        except (LlmCallError, judge.JudgeError) as exc:
            report.cpa["judge_failed"] += 1
            report.error(f"{label}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        report.cpa["judge_ok"] += 1
        report.rejected.update(bad)
        got, late = _in_window(got, window)
        report.rejected["outside the 9-month-back / 6-month-ahead window"] += late
        for finding in got:
            report.per_seed[finding.seed]["findings"] += 1
        added, left_out = store.candidates_from(got, known, repo_edges, now=stamp)
        report.left_out += left_out
        fresh = store.merge(fresh, added)
        known = store.with_candidates(known, added)  # later batches can point at what earlier ones found
    if batches and not report.cpa["judge_ok"]:
        raise NewsEventError("cpa_unavailable", f"CPA judged none of {len(found)} searched seeds and {len(posts)} "
                             "accounts' dynamics "
                             f"({report.cpa['judge_failed']} failed, {report.cpa['judge_attempted']} attempted)",
                             report)
    report.cpa["rejected_items"] = sum(report.rejected.values())
    if clip_names and clock() < search.deadline:  # whose name it is: the names the clips could not place (owner)
        placed, report.clip_names = identity.run(clip_names, known, owner=owner, search=search.search,
                                                 ask_json=lambda prompt: _call_json(llm_call, prompt), at=stamp,
                                                 page=getattr(search.lookup, "page", None))
        fresh = store.merge(fresh, placed)
        known = store.with_candidates(known, placed)
    if community is not None:  # the community-name crawler's names, deduped against this graph (owner)
        name_notes: list[str] = []
        folded, report.community = names.run(
            community, known, search=search.search if clock() < search.deadline else None,
            ask_json=lambda prompt: _call_json(llm_call, prompt), today=now.date(), at=stamp,
            notes=name_notes, owner=owner, videos=name_videos if clock() < search.deadline else None,
            limit=origin_limit)
        for line in name_notes:
            report.error(line)
        fresh = store.merge(fresh, folded)

    merged = store.accumulate(previous, fresh, repo=repo, now=stamp, window=window)
    report.new_nodes = sorted(set(fresh[0]) - set(previous[0]))
    report.new_edges = sorted(set(fresh[1]) - set(previous[1]))
    report.candidates = {"nodes": len(merged[0]), "edges": len(merged[1])}
    try:
        store.write_runtime_graph(repo_root, runtime_root, merged, report.to_json())
    except (GraphError, OSError, ValueError) as exc:
        raise NewsEventError("validation_failed", f"{type(exc).__name__}: {str(exc)[:300]}", report) from exc

    # Optional work-seed enrichment runs strictly after the main crawl's outputs are saved on disk,
    # inside its own exception guard and independent wall-clock budget started here, so neither the news deadline
    # nor any work-seed failure can harm news delivery.  A call already in flight can overrun by at most its timeout.
    try:
        if (repo_root / WORK_SEEDS_FILE).is_file():
            work_seeds = load_work_seeds(repo_root)
            if work_seeds:
                work_deadline = clock() + max(0.0, work_budget_s)
                work_search = RouterSearch(
                    search.lookup, deadline=work_deadline, clock=clock, sleep=search.sleep, jitter=search.jitter)
                work_added, report.works = _crawl_work_seeds(
                    work_seeds, known, repo_edges, search=work_search, llm_call=llm_call,
                    report=report, clock=clock, deadline=work_deadline,
                    start_cursor=prev_work_cursor, stamp=stamp)
                fresh = store.merge(fresh, work_added)
                merged = store.accumulate(previous, fresh, repo=repo, now=stamp, window=window)
                report.new_nodes = sorted(set(fresh[0]) - set(previous[0]))
                report.new_edges = sorted(set(fresh[1]) - set(previous[1]))
                report.candidates = {"nodes": len(merged[0]), "edges": len(merged[1])}
                store.write_runtime_graph(repo_root, runtime_root, merged, report.to_json())
    except Exception as exc:  # noqa: BLE001 - optional work seeds must never fail or abort the news run
        msg = f"{type(exc).__name__}: {str(exc)[:200]}"
        report.error(f"work_seeds: {msg}")
        report.works = {"error": msg, "next_cursor": prev_work_cursor}
        try:
            store.write_runtime_graph(repo_root, runtime_root, merged, report.to_json())
        except Exception:  # noqa: BLE001 - main news graph/report was already persisted above
            pass
    return report
