#!/usr/bin/env python3
"""Exhaustive A/B replay for the closed-set / read-aloud CPA judge lane.

对指定的两个模型（默认 gpt-5.6-sol@medium vs gpt-5.6-luna@max）重放历史
chat-authority 里的真实裁决案例，比较三方：模型A、模型B、当年的生产判决。

保真标准：闭集法官案例只有当**重渲染 prompt 的 sha256 等于历史记录的
judge_prompt_sha256** 时才算合法重放（字节级）；无法达到该标准的案例计入
unreplayable 并披露原因，绝不悄悄用近似 prompt 冒充。念弹幕案例的模板无
历史 sha 可对（当年未记录），按模板确定性重放并在报告中如实分类。

输出：reports/luna-ab/ab-report-<ts>.json + 摘要行到 stdout。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.autoslice.read_aloud_llm_verifier import (  # noqa: E402
    _CLOSED_CHOICE_PROMPT,
    _prompt as _read_aloud_prompt,
    _witness_check_request,
)
from src.autoslice.acoustic_witness_adjudication import (  # noqa: E402
    build_witness_request,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _walk(obj, pred, path=""):
    hits = []
    if isinstance(obj, dict):
        if pred(obj):
            hits.append((path, obj))
        for key, value in obj.items():
            hits += _walk(value, pred, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            hits += _walk(value, pred, f"{path}[{index}]")
    return hits


def _render_closed_choice(arb: Mapping) -> tuple[str, str] | None:
    """(prompt, recorded_judge_sha) — 只在能拿到全部原始输入时返回。"""

    request = arb.get("request")
    verdict = arb.get("verdict")
    if not isinstance(request, Mapping) or not isinstance(verdict, Mapping):
        return None
    recorded = str(verdict.get("judge_prompt_sha256") or "")
    if not recorded:
        return None
    witness = verdict.get("witness")
    if not isinstance(witness, Mapping):
        return None
    candidates = [
        dict(candidate)
        for candidate in request.get("candidate_entities") or ()
        if isinstance(candidate, Mapping)
        and str(candidate.get("canonical") or "")
    ]
    canonicals = [str(c["canonical"]) for c in candidates]
    if len(canonicals) < 2:
        return None
    current = str(request.get("matched_audio_text") or canonicals[-1])
    proposed = str(
        request.get("exact_text")
        or request.get("structured_chat_canonical")
        or canonicals[0]
    )
    check_request = _witness_check_request(
        request, current=current, proposed=proposed
    )
    if check_request is None:
        return None
    context = {
        "current_transcript": current,
        "context_before": request.get("context_before"),
        "context_after": request.get("context_after"),
        "kind": request.get("kind"),
        "structured_chat_text": request.get("exact_text"),
        "structured_chat_canonical": request.get("structured_chat_canonical"),
        "structured_chat_surface": request.get("structured_chat_surface"),
        "candidate_provenance": check_request["candidate_provenance"],
        "whole_clip_context": request.get("whole_clip_context"),
        "adjacent_structured_event_chain": (
            request.get("whole_clip_context") or {}
        ).get("adjacent_structured_event_chain")
        if isinstance(request.get("whole_clip_context"), Mapping)
        else None,
    }
    prompt = _CLOSED_CHOICE_PROMPT.format(
        witness=json.dumps(dict(witness), ensure_ascii=False, sort_keys=True),
        candidates=json.dumps(
            candidates, ensure_ascii=False, indent=2, sort_keys=True
        ),
        context=json.dumps(
            context, ensure_ascii=False, indent=2, sort_keys=True
        ),
    )
    return prompt, recorded


def _call(prompt: str, models: str, effort: str, timeout: int) -> dict:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as handle:
        handle.write(prompt)
        prompt_file = handle.name
    completion_file = prompt_file + ".out"
    started = time.time()
    try:
        proc = subprocess.run(
            [
                "bash",
                str(REPO / "scripts/llm_via_cpa.sh"),
                prompt_file,
                completion_file,
                models,
                effort,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        latency = round(time.time() - started, 1)
        raw = (
            Path(completion_file).read_text(encoding="utf-8")
            if Path(completion_file).exists()
            else ""
        )
        return {
            "rc": proc.returncode,
            "latency_s": latency,
            "raw": raw[:4000],
        }
    except subprocess.TimeoutExpired:
        return {
            "rc": -9,
            "latency_s": round(time.time() - started, 1),
            "raw": "",
        }
    finally:
        for path in (prompt_file, completion_file):
            try:
                os.unlink(path)
            except OSError:
                pass


def _extract_json(raw: str):
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        try:
            start = raw.index("[")
            end = raw.rindex("]") + 1
            return json.loads(raw[start:end])
        except (ValueError, json.JSONDecodeError):
            return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--globs", nargs="*", default=[
        "/opt/bilive/autoslice/repo/lidousha/2026-07-*/*.chat-authority.json",
        "/opt/bilive/autoslice/out/2026-07-*/*/*.chat-authority.json",
    ])
    parser.add_argument("--model-a", default="gpt-5.6-sol")
    parser.add_argument("--effort-a", default="medium")
    parser.add_argument("--model-b", default="gpt-5.6-luna")
    parser.add_argument("--effort-b", default="max")
    parser.add_argument("--limit", type=int, default=0, help="0=全部")
    parser.add_argument("--out-dir", default=str(REPO / "reports/luna-ab"))
    args = parser.parse_args()

    files: list[str] = []
    for pattern in args.globs:
        files += glob.glob(pattern)
    files = sorted(set(files))

    closed_cases = []
    read_aloud_cases = []
    unreplayable = []
    seen_prompt_shas = set()
    for path in files:
        try:
            data = json.load(open(path, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for where, arb in _walk(
            data,
            lambda x: isinstance(x.get("verdict"), Mapping)
            and x["verdict"].get("schema_version") == "chat-entity-verdict.v1",
        ):
            verdict = arb["verdict"]
            if not verdict.get("judge_prompt_sha256"):
                continue  # AGY forced-choice 等非 CPA 法官行
            rendered = _render_closed_choice(arb)
            if rendered is None:
                unreplayable.append(
                    {"file": path, "where": where, "reason": "inputs_missing"}
                )
                continue
            prompt, recorded = rendered
            if _sha(prompt) != recorded:
                unreplayable.append(
                    {"file": path, "where": where, "reason": "sha_mismatch"}
                )
                continue
            if recorded in seen_prompt_shas:
                continue
            seen_prompt_shas.add(recorded)
            closed_cases.append(
                {
                    "file": path,
                    "where": where,
                    "prompt": prompt,
                    "historical_choice": str(
                        verdict.get("canonical_entity") or ""
                    ),
                }
            )
        for where, row in _walk(
            data,
            lambda x: isinstance(x.get("request"), Mapping)
            and x["request"].get("schema_version")
            == "chat-read-aloud-verification-request.v1",
        ):
            request = row["request"]
            verdict = row.get("verdict")
            historical = (
                verdict.get("is_read_aloud")
                if isinstance(verdict, Mapping)
                else None
            )
            prompt = _read_aloud_prompt(
                str(request.get("danmu_text") or ""),
                str(request.get("asr_text") or ""),
                str(request.get("context_before") or ""),
                str(request.get("context_after") or ""),
            )
            key = _sha(prompt)
            if key in seen_prompt_shas:
                continue
            seen_prompt_shas.add(key)
            read_aloud_cases.append(
                {
                    "file": path,
                    "where": where,
                    "prompt": prompt,
                    "historical_is_read_aloud": historical,
                }
            )

    cases = [("closed_set", c) for c in closed_cases] + [
        ("read_aloud", c) for c in read_aloud_cases
    ]
    if args.limit:
        cases = cases[: args.limit]
    print(
        f"replayable: closed_set={len(closed_cases)} "
        f"read_aloud={len(read_aloud_cases)} "
        f"unreplayable={len(unreplayable)} running={len(cases)}",
        flush=True,
    )

    results = []
    stats = {
        "agree": 0,
        "disagree": 0,
        "errors_a": 0,
        "errors_b": 0,
        "a_matches_history": 0,
        "b_matches_history": 0,
        "history_known": 0,
    }
    for index, (kind, case) in enumerate(cases, start=1):
        out_a = _call(case["prompt"], args.model_a, args.effort_a, 240)
        out_b = _call(case["prompt"], args.model_b, args.effort_b, 240)
        parsed_a = _extract_json(out_a["raw"])
        parsed_b = _extract_json(out_b["raw"])
        if kind == "closed_set":
            pick = lambda parsed: (  # noqa: E731
                str(parsed.get("canonical") or parsed.get("choice") or "")
                if isinstance(parsed, Mapping)
                else None
            )
            value_a, value_b = pick(parsed_a), pick(parsed_b)
            history = case.get("historical_choice") or None
        else:
            pick = lambda parsed: (  # noqa: E731
                parsed.get("is_read_aloud")
                if isinstance(parsed, Mapping)
                else None
            )
            value_a, value_b = pick(parsed_a), pick(parsed_b)
            history = case.get("historical_is_read_aloud")
        if out_a["rc"] != 0 or value_a is None:
            stats["errors_a"] += 1
        if out_b["rc"] != 0 or value_b is None:
            stats["errors_b"] += 1
        agreed = value_a is not None and value_a == value_b
        stats["agree" if agreed else "disagree"] += 1
        if history is not None:
            stats["history_known"] += 1
            if value_a == history:
                stats["a_matches_history"] += 1
            if value_b == history:
                stats["b_matches_history"] += 1
        results.append(
            {
                "kind": kind,
                "file": case["file"],
                "where": case["where"],
                "prompt_sha256": _sha(case["prompt"]),
                "history": history,
                "a": {
                    "value": value_a,
                    "latency_s": out_a["latency_s"],
                    "rc": out_a["rc"],
                },
                "b": {
                    "value": value_b,
                    "latency_s": out_b["latency_s"],
                    "rc": out_b["rc"],
                },
                "agreed": agreed,
            }
        )
        marker = "==" if agreed else "!!"
        print(
            f"[{index}/{len(cases)}] {kind} {marker} "
            f"a={value_a!r}({out_a['latency_s']}s) "
            f"b={value_b!r}({out_b['latency_s']}s) hist={history!r}",
            flush=True,
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9]", "", time.strftime("%Y%m%dT%H%M%S"))
    report_path = out_dir / f"ab-report-{stamp}.json"
    report_path.write_text(
        json.dumps(
            {
                "schema_version": "model-ab-replay-report.v1",
                "model_a": {"model": args.model_a, "effort": args.effort_a},
                "model_b": {"model": args.model_b, "effort": args.effort_b},
                "stats": stats,
                "unreplayable": unreplayable,
                "results": results,
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"stats: {json.dumps(stats)}")
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
