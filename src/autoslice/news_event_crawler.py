"""Daily crawl of circle news: events, projects, alliances and collaborations, as a candidate entity graph.

Seeds come from data the pipeline already has (news_event_seeds); the Search Router's `free_search` finds recent pages
about each seed (reusing the manual lane's `WebLookup` client and its address from the channel profile); CPA decides
which results show an event / project / alliance / collaboration, who takes part and how (news_event_judge); the
answer becomes status=candidate nodes and edges in the format of `knowledge_graph` (news_event_graph).  A run that
cannot do its job raises `NewsEventError` (the entry point exits non-zero and the timer unit shows it): no search
succeeded, CPA answered nothing, or the graph did not validate.  Partial failure is counted in the report.
"""
from __future__ import annotations

import datetime as dt
import random
import re
import time
import urllib.error
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from src.autoslice import news_event_graph as store
from src.autoslice import news_event_judge as judge
from src.autoslice.knowledge_graph import GraphError, JsonValue, load_graph
from src.autoslice.llm_client import LlmCallError
from src.autoslice.manual_lane.work_lookup import LookupRow
from src.autoslice.news_event_seeds import Dynamics, Registry, Seed, derive_seeds, dynamics_texts
from src.autoslice.structured_output import call_json

_call_json: Callable[[Callable[[str], str], str], str] = call_json  # structured_output.call_json is untyped
REPORT_SCHEMA = "vtuber-slice.news-event-crawl.v1"
MAX_SEEDS = 40               # one run's searches; the roster part of the list rotates day by day
SEARCH_ROWS = 8
SEEDS_PER_JUDGE_CALL = 4     # keeps one CPA prompt well below the community crawler's 16-card / 156 KiB incident
MIN_INTERVAL_S = 1.0         # gap between searches; doubles after each rate-limited answer, relaxes on success
MAX_INTERVAL_S = 60.0
RATE_LIMIT_ATTEMPTS = 6
MAX_WAIT_S = 120.0
MAX_ERRORS_LISTED = 20
RETRY_QUERY = "{seed} 联动 企划"
OWNER_QUERY = "{owner} 联动 企划 最新"   # a seed is searched as "<seed> <owner>": extra topic words pull in unrelated pages


class SearchBackend(Protocol):
    def web(self, query: str, *, limit: int = ...) -> Sequence[LookupRow]: ...


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
        return counts | {
            "schema_version": REPORT_SCHEMA, "generated_at": self.generated_at, "owner": self.owner,
            "inputs": {k: v for k, v in self.inputs.items()}, "errors": [e for e in self.errors],
            "seeds": {"count": len(self.seeds), "by_origin": origins, "names": names},
            "findings": {"new_nodes": [n for n in self.new_nodes], "new_edges": [e for e in self.new_edges],
                         "left_out_id_collision": self.left_out},
            "previous_discarded": self.previous_discarded}


class NewsEventError(RuntimeError):
    """The run cannot deliver; `code` names why and `report` holds what was counted until then."""

    def __init__(self, code: str, detail: str, report: Report | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.report = report


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
            try:
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


def crawl(*, owner: str, repo_root: Path, runtime_root: Path, registry: Registry | None,
          dynamics: Dynamics | None, search: RouterSearch, llm_call: Callable[[str], str], now: dt.datetime,
          max_seeds: int = MAX_SEEDS, clock: Callable[[], float] = time.monotonic,
          notes: Sequence[str] = (), clip_names: Sequence[str] = ()) -> Report:
    """One run.  Returns the report that was written next to the graph; raises NewsEventError on failure."""
    stamp = now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo = load_graph(repo_root)
    report = Report(stamp, owner, {"registry": registry is not None, "dynamics": dynamics is not None},
                    errors=list(notes))
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
    known = store.with_candidates(repo, previous)
    repo_edges = {e.id for e in repo.edges}
    fresh: store.Candidates = ({}, {})
    for start in range(0, len(found), SEEDS_PER_JUDGE_CALL):
        batch = found[start:start + SEEDS_PER_JUDGE_CALL]
        if clock() >= search.deadline:
            report.error("judge stopped: the run's time budget is spent")
            break
        report.cpa["judge_attempted"] += 1
        prompt = judge.judge_prompt(owner, now.date().isoformat(), judge.catalog(known), batch)
        try:
            got, bad = judge.parse_judgement(_call_json(llm_call, prompt), batch)
        except (LlmCallError, judge.JudgeError) as exc:
            report.cpa["judge_failed"] += 1
            report.error(f"judge batch {start // SEEDS_PER_JUDGE_CALL}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        report.cpa["judge_ok"] += 1
        report.rejected.update(bad)
        for finding in got:
            report.per_seed[finding.seed]["findings"] += 1
        added, left_out = store.candidates_from(got, known, repo_edges, now=stamp)
        report.left_out += left_out
        fresh = store.merge(fresh, added)
        known = store.with_candidates(known, added)  # later batches can point at what earlier ones found
    if found and not report.cpa["judge_ok"]:
        raise NewsEventError("cpa_unavailable", f"CPA judged none of {len(found)} searched seeds "
                             f"({report.cpa['judge_failed']} failed, {report.cpa['judge_attempted']} attempted)",
                             report)
    report.cpa["rejected_items"] = sum(report.rejected.values())

    merged = store.accumulate(previous, fresh, repo=repo, now=stamp)
    report.new_nodes = sorted(set(fresh[0]) - set(previous[0]))
    report.new_edges = sorted(set(fresh[1]) - set(previous[1]))
    report.candidates = {"nodes": len(merged[0]), "edges": len(merged[1])}
    try:
        store.write_runtime_graph(repo_root, runtime_root, merged, report.to_json())
    except (GraphError, OSError, ValueError) as exc:
        raise NewsEventError("validation_failed", f"{type(exc).__name__}: {str(exc)[:300]}", report) from exc
    return report
