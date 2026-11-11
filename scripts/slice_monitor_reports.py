"""Report/state persistence for slice_monitor.py.

Every file is replaced atomically (temp file in the same directory, then
``os.replace``) so a crash, full disk or memory pressure can never leave a
truncated ``state.json`` / ``latest.*`` for the next run or a reader to find.
History is a forensic trail only (nothing reads it back); files older than
``HISTORY_KEEP_DAYS`` are pruned by the process that writes them.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import timedelta

# Incident forensics only need the recent past: latest.json holds the current
# verdict and state.json the transition memory.  7 days is ~2000 files (~17 MB).
HISTORY_KEEP_DAYS = 7
_HISTORY_NAME = re.compile(r"^(\d{8}-\d{6})-[A-Z]+\.json$")
ICONS = {"OK": "✅", "WARN": "🟡", "DEGRADED": "🟠", "DOWN": "🔴"}


def atomic_write_text(path, text):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path, value):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def render_markdown(verdict, problems, actions, notes, ts):
    md = [f"# 李豆沙 自动切片监控 — {ICONS[verdict]} {verdict}",
          "", f"生成: {ts.strftime('%Y-%m-%d %H:%M:%S %Z')}", "", "## 状态"]
    md += [f"- {n}" for n in notes]
    if actions:
        md += ["", "## 监控自动处理"] + [f"- ✅ {m}" for _, m in actions]
    if not problems:
        return "\n".join(md + ["", "_无问题。_"])
    md += ["", "## 问题 & 建议"]
    for p in problems:
        md += [f"- **[{p['sev']}] {p['msg']}**", f"  - 建议: {p['fix']}"]
        md += [f"    - `{d}`" for d in p.get("detail", [])[:6]]
    return "\n".join(md)


def write_reports(report_dir, probe, verdict, problems, actions, notes, ts):
    payload = {"ts": ts.isoformat(), "verdict": verdict, "problems": problems,
               "actions": [{"kind": a[0], "msg": a[1]} for a in actions],
               "notes": notes, "probe": probe}
    history = os.path.join(report_dir, "history")
    atomic_write_json(os.path.join(report_dir, "latest.json"), payload)
    atomic_write_json(os.path.join(history, ts.strftime("%Y%m%d-%H%M%S") + f"-{verdict}.json"), payload)
    md = render_markdown(verdict, problems, actions, notes, ts)
    atomic_write_text(os.path.join(report_dir, "latest.md"), md + "\n")
    return md


def prune_history(report_dir, now, keep_days=HISTORY_KEEP_DAYS):
    """Delete history files whose name timestamp is older than ``keep_days``.

    Only files matching the writer's own naming are touched.  Returns the count.
    """
    history = os.path.join(report_dir, "history")
    cutoff = (now - timedelta(days=keep_days)).strftime("%Y%m%d-%H%M%S")
    removed = 0
    for name in os.listdir(history):
        match = _HISTORY_NAME.match(name)
        if match and match.group(1) < cutoff:
            os.unlink(os.path.join(history, name))
            removed += 1
    return removed
