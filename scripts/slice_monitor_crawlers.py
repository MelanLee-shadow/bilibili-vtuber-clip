"""Last-run health of the read-only crawlers, for slice_monitor.py.

The crawlers run as systemd oneshot units on the recording host
(config/scheduled_crawlers.v1.json -> ``oci3-crawl-<id>.service``).  A crawler's
stderr goes to ``<base>/logs/<id>.log``, never the journal, so a failing crawler
is invisible unless the unit's own result is read.  The unit result is the
authority: ``Result`` must be ``success`` and ``ExecMainStatus`` must be ``0``.
"""
from __future__ import annotations

UNIT_PREFIX = "oci3-crawl-"
UNIT_SUFFIX = ".service"

# Remote shell fragment: one ``unit|Result|ExecMainStatus`` line per crawler unit.
CRAWLER_RESULT_CMD = (
    "for u in $(systemctl list-unit-files 'oci3-crawl-*.service' --no-legend 2>/dev/null "
    "| awk '{print $1}'); do "
    "echo \"$u|$(systemctl show \"$u\" -p Result --value 2>/dev/null)"
    "|$(systemctl show \"$u\" -p ExecMainStatus --value 2>/dev/null)\"; done"
)


def parse_crawler_results(text):
    """Parse ``unit|Result|ExecMainStatus`` lines into ``{unit: (result, exit_status)}``."""
    results = {}
    for line in (text or "").strip().splitlines():
        unit, sep, rest = line.strip().partition("|")
        if not (sep and unit.startswith(UNIT_PREFIX) and unit.endswith(UNIT_SUFFIX)):
            continue
        result, _, status = rest.partition("|")
        results[unit] = (result.strip(), status.strip())
    return results


def crawler_problems(results):
    """One WARN per crawler unit whose last run did not end ``success`` with exit 0."""
    problems = []
    for unit, (result, status) in sorted((results or {}).items()):
        if result == "success" and status == "0":
            continue
        job = unit[len(UNIT_PREFIX):-len(UNIT_SUFFIX)]
        shown = f"Result={result or '未知'} ExitStatus={status or '未知'}"
        problems.append({
            "id": f"crawler_{job}_failed", "sev": "WARN",
            "msg": f"只读爬虫 {unit} 上次运行 {shown}（成功应为 Result=success 且 ExitStatus=0）；"
                   "对应的快照可能已过期。",
            "fix": f"看 <宿主>:/opt/bilive/autoslice/logs/{job}.log 末行（失败行在日志文件，不在 journal）。",
        })
    return problems


def crawler_note(results):
    """One report line listing every crawler unit and its last result."""
    if not results:
        return "爬虫 unit: 宿主上未读到任何 oci3-crawl-*.service"
    parts = [
        f"{unit[len(UNIT_PREFIX):-len(UNIT_SUFFIX)]}={result or '?'}/{status or '?'}"
        for unit, (result, status) in sorted(results.items())
    ]
    return "爬虫 unit(Result/ExitStatus): " + " ".join(parts)
