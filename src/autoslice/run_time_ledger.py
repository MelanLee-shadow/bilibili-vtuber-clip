"""Offline, decision-neutral producer timing (stdlib only).

This module owns the source catalogue, interval rules and v1 schema. UTC/unix
timestamps are wall clock; audio ``duration_ms`` is *media* length, never latency.
Only explicit paired timestamps contribute to call-in-flight time. Missing ends
remain unknown (including process kills); cache hits are not provider calls.

``no_call_observed_s`` is the complement of the call interval union. It is NOT
CPU idle time. ``dead_time_s`` is the sum of gaps >= 300 s with no timed call,
completed leaf-stage/wait span, or observed artifact/stage-log write. Shorter
gaps are reported separately. This conservative operational indicator excludes
instrumented processing; untimed calls and overwritten mtimes limit certainty.
No threshold time is subtracted from a qualifying gap. All gaps remain in JSON.
Nested stages are reported individually but never summed into wall clock.

Future task-level instrumentation: append ``time-spans.jsonl`` records with
schema_version="run-time-span.v1", span_id, kind (stage/call/wait/write), name,
started_utc, ended_utc; calls also have modality (text/audio), status and optional
http_status/error_type. Emit a write as a zero-length paired timestamp. An open
span may omit ended_utc. Do not duplicate an existing exchange/Argon call here.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager, redirect_stderr
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Iterable

SCHEMA = "run-time-ledger.v1"
DEAD_GAP_SECONDS = 300.0
SOURCE_CATALOGUE = [
    {"id": "producer_stage", "files": "explicit logs / producer.log",
     "fields": ["UTC timestamp", "START/END", "stage", "n", "elapsed_s"],
     "missing": "no END on exceptions/kills; no pid/thread id; elapsed_s is monotonic"},
    {"id": "text_exchange", "files": "cpa-text-exchanges/request-*/request.json + terminal.json",
     "fields": ["started_utc", "ended_utc", "status", "error_type", "provider_diagnostics"],
     "missing": "terminal may be absent; retry HTTP history is not always archived"},
    {"id": "audio_execution", "files": "gemini-range-{context,acquisition}/**/execution.json",
     "fields": ["status", "provider", "model", "provider_failures[].http_status", "response_source.path"],
     "missing": "legacy execution has no call start/end; mtime is only a write point; duration_ms is media length"},
    {"id": "argon", "files": "**/calls/*.start.json + *.outcome.json",
     "fields": ["started_unix_s", "finished_unix_s", "method", "http_status", "transport_reason"],
     "missing": "batch polling HTTP latency is not remote model computation; missing outcome remains open"},
    {"id": "artifact_write", "files": "non-media output metadata / time-ledger-artifacts.json",
     "fields": ["path", "mtime_unix_s", "size_bytes"],
     "missing": "mtime records latest write only, not write duration or overwritten history; no contents read"},
    {"id": "task_span", "files": "**/time-spans.jsonl",
     "fields": ["schema_version", "span_id", "kind", "name", "started_utc", "ended_utc", "modality"],
     "missing": "optional future source; open spans are not closed by inference"},
]
_STAGE = re.compile(r"^PRODUCER_STAGE (\S+) (START|END) (\S+)(.*)$")
_EXTENSIONS = {".json", ".jsonl", ".srt", ".ass", ".txt", ".md", ".stdout"}
_FAILURE_FIELDS = {"http_status", "provider_http_status", "error_type", "transport_reason",
                   "safe_reason", "reason_code", "failure_category", "category", "status"}


def timestamp(value: object) -> float | None:
    try:
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            result = float(value)
        else:
            # GNU date emits nanoseconds; Python 3.10 accepts at most six digits.
            iso = re.sub(r"(\.\d{6})\d+(?=Z|[+-]\d\d:\d\d$)", r"\1", str(value))
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return None
            result = dt.timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def merge_intervals(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Union half-open intervals; touching intervals merge, reversed ones reject."""
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if not (math.isfinite(start) and math.isfinite(end)) or end < start:
            raise ValueError("invalid timing interval")
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _seconds(intervals: Iterable[tuple[float, float]]) -> float:
    return round(sum(e - s for s, e in merge_intervals(intervals)), 6)


def _failure_counts(value: object) -> tuple[int, int]:
    """Count leaf failure records, not duplicate parent summaries/free text."""
    if isinstance(value, list):
        rows = [_failure_counts(x) for x in value]
        return sum(x[0] for x in rows), sum(x[1] for x in rows)
    if not isinstance(value, dict):
        return 0, 0
    children = [_failure_counts(v) for v in value.values() if isinstance(v, (list, dict))]
    rate = sum(x[0] for x in children)
    timeout = sum(x[1] for x in children)
    fields = [str(v).upper() for k, v in value.items() if k in _FAILURE_FIELDS]
    return (rate or int(any(v == "429" or "HTTP_STATUS_429" in v for v in fields)),
            timeout or int(any("TIMEOUT" in v or "TIMED_OUT" in v for v in fields)))


def _read(path: Path, issues: Counter) -> dict:
    try:
        if path.is_symlink() or path.stat().st_size > 8_000_000:
            issues["skipped_symlink_or_large_metadata"] += 1
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        issues["missing_or_invalid_json"] += 1
        return {}


def _walk(root: Path):
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink()
                         and d not in {".git", "__pycache__"})
        for name in sorted(files):
            path = Path(directory) / name
            if not path.is_symlink():
                yield path


def _stage_rows(lines: Iterable[str], source: str, issues: Counter) -> list[dict]:
    rows = []
    active: dict[tuple[str, str], dict] = {}
    for line in lines:
        match = _STAGE.match(line)
        if not match:
            continue
        stamp, event, name, tail = match.groups()
        fields = dict(re.findall(r"(\w+)=(\S+)", tail))
        ts = timestamp(stamp)
        if ts is None:
            issues["invalid_stage_timestamp"] += 1
            continue
        key = name, fields.get("n", "")
        if event == "START":
            row = {"kind": "stage", "name": name, "source": source,
                   "sequence": key[1], "start": ts, "end": None}
            rows.append(row)
            active[key] = row
        elif key in active:
            row = active.pop(key)
            row["end"] = ts
            elapsed = timestamp(float(fields["elapsed_s"])) if "elapsed_s" in fields else None
            row["monotonic_elapsed_s"] = elapsed
        else:
            issues["unmatched_stage_end"] += 1
    return rows


def collect_sources(root: Path, *, log_paths: Iterable[Path] = (),
                    stage_lines: Iterable[str] = (), artifact_manifests: Iterable[Path] = ()) -> dict:
    """Read only timing receipts and stat non-media files; never read media/audits."""
    issues: Counter = Counter()
    rows: list[dict] = []
    writes: list[dict] = []
    counts: Counter = Counter()
    files = list(_walk(root))
    manifests = list(artifact_manifests)
    stage_lines = list(stage_lines)
    logs = list(log_paths) or ([] if stage_lines else [p for p in files if p.name == "producer.log"])
    for path in logs:
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                rows.extend(_stage_rows(stream, str(path), issues))
            counts["producer_stage"] += 1
        except OSError:
            issues["unreadable_log"] += 1
    rows.extend(_stage_rows(stage_lines, "live_stderr", issues))
    if stage_lines:
        counts["producer_stage"] += 1
    audio: dict[str, tuple[Path, dict]] = {}

    def add(kind, name, path, start, end, metadata=None, **extra):
        rate, timeout = _failure_counts(metadata or {})
        rows.append({"kind": kind, "name": name, "source": str(path),
                     "start": timestamp(start), "end": timestamp(end),
                     "rate_limit_count": rate, "timeout_count": timeout, **extra})

    for path in files:
        name = path.name
        if name.endswith(".time-ledger.json") or name == "time-ledger-artifacts.json":
            if name == "time-ledger-artifacts.json":
                manifests.append(path)
            continue
        try:
            if path.suffix in _EXTENSIONS:
                st = path.stat()
                writes.append({"path": str(path), "time": st.st_mtime, "size_bytes": st.st_size})
        except OSError:
            issues["unreadable_stat"] += 1
        if name == "request.json" and path.parent.name.startswith("request-") and "cpa-text-exchanges" in path.parts:
            request = _read(path, issues)
            terminal = _read(path.with_name("terminal.json"), issues)
            add("call", "text_exchange", path, request.get("started_utc"), terminal.get("ended_utc"),
                terminal, modality="text", status=terminal.get("status", "OUTCOME_UNKNOWN"))
            counts["text_exchange"] += 1
        elif name == "execution.json" and any(p.startswith("gemini-range-") for p in path.parts):
            data = _read(path, issues)
            # Context copies point back to acquisition bytes. Count only one.
            ref = data.get("response_source") or data.get("response") or {}
            identity = str(ref.get("path") or path) if isinstance(ref, dict) else str(path)
            if identity not in audio or "gemini-range-acquisition" in path.parts:
                audio[identity] = path, data
        elif name.endswith(".start.json") and path.parent.name == "calls":
            start = _read(path, issues)
            end = _read(path.with_name(name.replace(".start.json", ".outcome.json")), issues)
            # GET/DELETE and batch submissions are transport/wait, not measured inference.
            realtime = start.get("method") == "POST" and "generateContent" in str(start.get("path", ""))
            add("call" if realtime else "wait", "argon_http", path,
                start.get("started_unix_s"), end.get("finished_unix_s"), end,
                modality="audio", status=str(end.get("http_status", "OUTCOME_UNKNOWN")))
            counts["argon"] += 1
        elif name == "time-spans.jsonl":
            try:
                for line in path.read_text().splitlines():
                    try:
                        data = json.loads(line)
                        if data.get("schema_version") != "run-time-span.v1" or data.get("kind") not in {"stage", "call", "wait", "write"}:
                            raise ValueError("invalid span")
                        add(data["kind"], str(data.get("name", "task")), path,
                            data.get("started_utc"), data.get("ended_utc"), data,
                            span_id=data.get("span_id"), modality=data.get("modality"),
                            status=data.get("status"))
                        counts["task_span"] += 1
                    except (ValueError, TypeError, AttributeError):
                        issues["invalid_task_span"] += 1
            except OSError:
                issues["unreadable_task_spans"] += 1
    for path, data in audio.values():
        add("call", "audio_execution", path, data.get("started_utc"), data.get("ended_utc"),
            {"provider_failures": data.get("provider_failures", []),
             "failure_category": data.get("failure_category"), "adapter_exception": data.get("adapter_exception")},
            modality="audio", status=data.get("status"), observed_at=path.stat().st_mtime)
        counts["audio_execution"] += 1
    for path in dict.fromkeys(manifests):
        data = _read(path, issues)
        if data.get("schema_version") != "run-time-ledger-artifacts.v1":
            issues["invalid_artifact_manifest"] += 1
            continue
        for item in data.get("files", []):
            stamp = timestamp(item.get("mtime_unix_s"))
            if stamp is not None:
                writes.append({"path": str(item.get("path", "")), "time": stamp,
                               "size_bytes": item.get("size_bytes"), "manifest": str(path)})
        counts["artifact_manifest"] += 1
    return {"rows": rows, "writes": writes, "source_counts": dict(counts), "issues": dict(issues)}


def build_ledger(sources: dict, *, start: object = None, end: object = None,
                 candidate_id: str | None = None, dead_gap_s: float = DEAD_GAP_SECONDS) -> dict:
    if not math.isfinite(dead_gap_s) or dead_gap_s < 0:
        raise ValueError("invalid dead gap threshold")
    rows = sources["rows"]
    explicit_start, explicit_end = timestamp(start), timestamp(end)
    if (start is not None and explicit_start is None) or (end is not None and explicit_end is None):
        raise ValueError("invalid explicit run timestamp (timezone required)")
    observed = [r[k] for r in rows for k in ("start", "end", "observed_at") if r.get(k) is not None]
    a = explicit_start if explicit_start is not None else min(observed, default=None)
    b = explicit_end if explicit_end is not None else max(observed, default=None)
    if a is None or b is None or b < a:
        raise ValueError("no valid run window; provide --start/--end")
    window = b - a
    issues = Counter(sources.get("issues", {}))
    selected = []
    for original in rows:
        r = dict(original)
        s, e, at = r.get("start"), r.get("end"), r.get("observed_at")
        if s is None:
            if at is None or not a <= at <= b:
                continue
        elif s > b or (e is not None and e < a) or (e is None and s < a):
            continue
        r["duration_s"] = round(e - s, 6) if s is not None and e is not None and e >= s else None
        if r["duration_s"] is None:
            issues["untimed_or_open_" + r["kind"]] += 1
        selected.append(r)

    def interval(r):
        s, e = r.get("start"), r.get("end")
        return (max(a, s), min(b, e)) if s is not None and e is not None and e >= s else None

    calls = [r for r in selected if r["kind"] == "call"]
    stages = [r for r in selected if r["kind"] == "stage"]
    waits = [r for r in selected if r["kind"] == "wait"]
    call_intervals = [i for r in calls if (i := interval(r)) is not None]
    # Parent spans describe scope, not continuous local work. Any nested START
    # makes a stage non-leaf, including an open child after an exception.
    leaves = [r for r in stages if interval(r) is not None and not any(
        q is not r and q.get("start") is not None and r["start"] < q["start"] < r["end"]
        for q in stages)]
    busy = call_intervals + [interval(r) for r in leaves + waits if interval(r) is not None]
    writes = [w for w in sources["writes"] if a <= w["time"] <= b]
    points = [w["time"] for w in writes]
    points += [r[k] for r in selected for k in ("start", "end")
               if r["kind"] in {"stage", "write"} and r.get(k) is not None and a <= r[k] <= b]
    merged = merge_intervals(busy + [(p, p) for p in points] + [(a, a), (b, b)])
    gaps = []
    cursor = a
    for s, e in merged:
        if s > cursor:
            gaps.append({"started_utc": utc(cursor), "ended_utc": utc(s),
                         "duration_s": round(s - cursor, 6), "dead": s - cursor >= dead_gap_s})
        cursor = max(cursor, e)
    stage_table = []
    for name in sorted({r["name"] for r in stages}):
        group = [r for r in stages if r["name"] == name]
        spans = [interval(r) for r in group if interval(r) is not None]
        stage_table.append({"name": name, "count": len(group),
                            "open_count": sum(r["duration_s"] is None for r in group),
                            "union_s": _seconds(spans),
                            "summed_s": round(sum(e - s for s, e in spans), 6)})
    counters = {}
    for modality in ("text", "audio"):
        group = [r for r in calls + waits if r.get("modality") == modality]
        counters[modality] = {"records": len(group), "http_429": sum(r.get("rate_limit_count", 0) for r in group),
                              "timeouts": sum(r.get("timeout_count", 0) for r in group),
                              "untimed_or_open": sum(r["duration_s"] is None for r in group)}
    dead = round(sum(g["duration_s"] for g in gaps if g["dead"]), 6)
    call_s = _seconds(call_intervals)
    for r in selected:
        r["started_utc"] = utc(r.pop("start")) if r.get("start") is not None else None
        r["ended_utc"] = utc(r.pop("end")) if r.get("end") is not None else None
    timed_calls = sorted((r for r in calls if r["duration_s"] is not None), key=lambda r: r["duration_s"], reverse=True)
    timed_waits = sorted((r for r in waits if r["duration_s"] is not None), key=lambda r: r["duration_s"], reverse=True)
    return {"schema_version": SCHEMA, "candidate_id": candidate_id,
            "window": {"started_utc": utc(a), "ended_utc": utc(b),
                       "basis": "explicit" if explicit_start is not None and explicit_end is not None else "inferred_observations"},
            "definitions": {"dead_gap_s": dead_gap_s, "dead_time": "long gaps outside timed calls, leaf stages and waits, split at observed writes",
                            "call_inflight": "union of explicit paired timestamps, clipped to window; lower bound if calls untimed/open",
                            "write_time": "point observation only; overwritten history unavailable",
                            "stage_time": "inclusive per stage; nested rows must not be added"},
            "totals": {"wall_s": round(window, 6), "call_inflight_s": call_s,
                       "call_duration_sum_s": round(sum(r["duration_s"] for r in calls if r["duration_s"] is not None), 6),
                       "no_call_observed_s": round(window - call_s, 6),
                       "instrumented_busy_s": _seconds(busy), "dead_time_s": dead,
                       "dead_time_pct": round(100 * dead / window, 3) if window else 0.0,
                       "short_unattributed_s": round(sum(g["duration_s"] for g in gaps if not g["dead"]), 6)},
            "coverage": {"source_counts": sources["source_counts"], "issues": dict(issues),
                         "artifact_write_points": len(writes),
                         "caveat": "dead time is an observed-gap indicator, not proof of CPU/disk idle; untimed calls may overlap"},
            "sources": SOURCE_CATALOGUE, "failure_counts": counters,
            "stage_table": stage_table, "spans": selected, "artifact_writes": writes,
            "gaps": gaps, "slowest_calls": timed_calls[:10],
            "slowest_waits": timed_waits[:10],
            "slowest_gaps": sorted(gaps, key=lambda r: r["duration_s"], reverse=True)[:10]}


def render_summary(ledger: dict) -> str:
    t = ledger["totals"]
    lines = [f"运行耗时账本：{ledger.get('candidate_id') or 'unknown'}", "",
             f"窗口：{ledger['window']['started_utc']} → {ledger['window']['ended_utc']} ({ledger['window']['basis']})",
             f"总墙钟 {t['wall_s']:.3f} s；已知调用在途并集 {t['call_inflight_s']:.3f} s；",
             f"长死时间 {t['dead_time_s']:.3f} s ({t['dead_time_pct']:.3f}%)；短未归因 {t['short_unattributed_s']:.3f} s。",
             f"无已知调用 {t['no_call_observed_s']:.3f} s；调用时长直接相加 {t['call_duration_sum_s']:.3f} s。",
             f"口径：{ledger['definitions']['dead_time']}；阈值 {ledger['definitions']['dead_gap_s']} s。",
             "缺起止的音频/未完成调用不补造时间；在途为下界，死时间为可观测空档，不证明 CPU/磁盘空闲。", "",
             "|阶段（嵌套不可相加）|次数|未闭合|窗口内并集 s|窗口内加总 s|", "|---|---:|---:|---:|---:|"]
    lines.extend(f"|{r['name']}|{r['count']}|{r['open_count']}|{r['union_s']:.3f}|{r['summed_s']:.3f}|" for r in ledger["stage_table"])
    for label, key in [("最慢 10 个调用", "slowest_calls"), ("最慢 10 个显式等待", "slowest_waits"), ("最慢 10 个无活动空档", "slowest_gaps")]:
        lines += ["", label, ""]
        lines += [f"- {r['duration_s']:.3f} s：{r.get('name', 'gap')}；{r.get('source', '')}；{r['started_utc']} → {r['ended_utc']}" for r in ledger[key]] or ["无已知完整区间。"]
    lines += ["", "失败计数（只计回执可见记录，缺失不等于零次）：", ""]
    lines += [f"- {m}：429={r['http_429']}，超时={r['timeouts']}，记录={r['records']}，缺起止/未结束={r['untimed_or_open']}。" for m, r in ledger["failure_counts"].items()]
    lines += ["", "覆盖情况：" + json.dumps(ledger["coverage"], ensure_ascii=False), ""]
    return "\n".join(lines)


def write_ledger(path: Path, ledger: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".time-ledger-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(ledger, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _StageTee:
    def __init__(self, stream):
        self.stream = stream
        self.lines: list[str] = []

    def write(self, text):
        result = self.stream.write(text)
        try:
            # producer_stage emits whole lines in one write; never retain free text.
            self.lines.extend(line for line in text.splitlines() if _STAGE.match(line))
        except Exception:
            pass
        return result

    def __getattr__(self, name):
        return getattr(self.stream, name)


@contextmanager
def producer_run_ledger(*, out_root: Path, cid: str, started_utc: str | None = None):
    """One main exit hook: observe stderr unchanged, persist on return/exception.

    SIGKILL/SIGTERM without Python unwinding cannot run a finally block; the
    offline CLI can report those runs without claiming a terminal timestamp.
    """
    started = timestamp(started_utc) if started_utc is not None else datetime.now(timezone.utc).timestamp()
    # PRODUCER_STAGE truncates UTC to milliseconds. Keep a sub-ms run's first
    # event inside its window rather than discarding it as an older event.
    if started is not None:
        started = math.floor(started * 1000) / 1000
    tee = _StageTee(sys.stderr)
    try:
        with redirect_stderr(tee):
            yield
    finally:
        ended = datetime.now(timezone.utc).timestamp()
        try:
            sources = collect_sources(Path(out_root), stage_lines=tee.lines)
            ledger = build_ledger(sources, start=started, end=ended, candidate_id=cid)
            write_ledger(Path(out_root) / f"{cid}.time-ledger.json", ledger)
        except Exception:
            pass  # Same contract as producer_stage_log: no output/exit-code changes.
