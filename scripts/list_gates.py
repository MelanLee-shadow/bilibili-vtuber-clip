#!/usr/bin/env python3
"""列出流水线里所有 gate / reason code 及其定义与消费位置。

存在理由：一天之内三次出现「门建好了但没人知道它在」——救援组件被
接成否决、生成前阻断门被重复建、segments 1:1 被当成待建项。这个仓库有 280+ 个
schema 字面量和上千个 SCREAMING_CASE 常量，维护者不知道门在哪已经是被证实的
成本来源（见 docs/pipeline/README.md 规则 5b）。

刻意做成**确定性脚本而不是人肉维护的文档**：跑一次生成一页，不新增 schema、
不发 receipt、不需要有人记得更新。

    python3 scripts/list_gates.py                 # 全部
    python3 scripts/list_gates.py --grep COVER    # 只看名字含 COVER 的
    python3 scripts/list_gates.py --orphans       # 只列定义了却没人消费的
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEARCH_DIRS = ("src/autoslice", "scripts")
# reason code / 阻断码惯例：全大写下划线、≥2 段、≥8 字符。
# 必须匹配**嵌在长消息里**的码（`raise ValueError("CODE: 详细说明…")`），不能只认
# 独占整个字符串的形式——第一版就因此漏掉了 COVER_PUNCH_REVIEW_REQUIRED 这类，
# 而漏掉的恰恰是最需要被查到的那种。
CODE_RX = re.compile(r'\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,})\b')
MIN_LEN = 8
# 全大写但不是 reason code 的噪声（模块级配置常量、类型名等）
NOISE_SUFFIXES = ("_RX", "_DIRS", "_LEN", "_API", "_URL", "_PATH", "_SCHEMA_VERSION")


def _python_files() -> list[Path]:
    files: list[Path] = []
    for rel in SEARCH_DIRS:
        files.extend(sorted((ROOT / rel).rglob("*.py")))
    return files


def collect() -> dict[str, list[tuple[str, int]]]:
    hits: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if '"' not in line and "'" not in line:
                continue  # 码只在字符串字面量里才是 reason code
            for code in CODE_RX.findall(line):
                if len(code) < MIN_LEN or code.endswith(NOISE_SUFFIXES):
                    continue
                hits[code].append((rel, lineno))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grep", default="", help="只显示名字包含该子串的码")
    parser.add_argument(
        "--orphans",
        action="store_true",
        help="只显示单点出现的码（定义了却没有第二处消费，通常是接线漏了）",
    )
    args = parser.parse_args(argv)

    hits = collect()
    needle = args.grep.upper()
    rows = sorted(
        (code, sites)
        for code, sites in hits.items()
        if needle in code and (not args.orphans or len(sites) == 1)
    )
    for code, sites in rows:
        print(f"{code}  ({len(sites)} 处)")
        for rel, lineno in sites[:6]:
            print(f"    {rel}:{lineno}")
        if len(sites) > 6:
            print(f"    … 另 {len(sites) - 6} 处")
    print(f"\n合计 {len(rows)} 个码" + ("（仅单点出现）" if args.orphans else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
