#!/usr/bin/env python3
"""Same six full-context inputs, GPT-6 low, sparse output format experiment.

No new ASR calls. The full-output baseline remains unchanged. This experiment
isolates a practical output-volume optimization rather than more reasoning.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import evaluate_astra_native_audio as study
from scripts import evaluate_agy_replacement_paired as prior
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.subtitle_draft_delta import parse_draft_delta

RULE = """\n只输出简洁JSON，不复制未变字幕，不输出逐条解释。你仍必须检查整片全部cue，不得只审改字处。
格式：{"schema_version":"cpa-draft-delta.v1","review_complete":true,"reviewed_cue_count":总条数,
"edits":[{"n":编号,"before":"输入该cue原文","after":"最小修改后完整该cue"}],
"needs_audio":[{"n":编号,"priority":1到3,"reason_code":"NAME|LANGUAGE|NUMBER_NEGATION|MISSING|OTHER"}]}。
priority=3最高，2一般，1低收益。只列真正需要原音的疑点。NAME为姓名/称呼，LANGUAGE为外语，
NUMBER_NEGATION为数字否定，MISSING为漏词，其余OTHER。edits=[]是完整审查后无可确定文字修改。
没有听音证据时不得删除整句或补写猜测词；before必须逐字复制原cue，条数和时间不变。
"""


def one(cid):
    out = study.OUT / cid / "first-low-delta"
    if (out / "result.json").exists():
        return
    original = (study.OUT / cid / "first-prompt.txt").read_text()
    marker = "\n只输出一个 JSON 对象,条数必须和草稿完全一致"
    if original.count(marker) != 1:
        raise ValueError("PRODUCTION_PROMPT_SUFFIX_NOT_FOUND")
    prompt = original.split(marker, 1)[0] + RULE
    cues = parse_srt_cues((study.OUT / cid / "prepared.srt").read_text())
    response = study.call_cpa(prompt, "low", out / "cpa")
    texts, doubts = parse_draft_delta(response, [c.text for c in cues])
    output = prior.render(cues, texts)
    (out / "output.srt").write_text(output)
    study.save(
        out / "result.json",
        {
            "status": "COMPLETE",
            "model": study.MODEL,
            "effort": "low",
            "output_protocol": "cpa-draft-delta.v1",
            "complete_review_is_provider_declaration": True,
            "requests": doubts,
            "full_production": False,
            "output_sha256": study.sha(output.encode()),
        },
    )


def score():
    m = study.scorer()
    rows = []
    for cid in study.load(study.OUT / "inputs.json"):
        out = study.OUT / cid / "first-low-delta"
        if not (out / "output.srt").exists():
            continue
        s = m.score(cid, out / "output.srt")
        study.save(out / "score.json", s)
        full = study.load(study.OUT / cid / "first-low/score.json")
        r = study.load(out / "cpa/receipt.json")
        baseline = study.load(study.OUT / cid / "first-low/cpa/receipt.json")
        rows.append(
            {
                "candidate_id": cid,
                "D_full": full["d_keep_distance"],
                "D_delta": s["d_keep_distance"],
                "reference_chars": s["reference_chars"],
                "full_seconds": baseline["wall_seconds"],
                "delta_seconds": r["wall_seconds"],
                "full_output_tokens": baseline["usage"]["output_tokens"],
                "delta_output_tokens": r["usage"]["output_tokens"],
                "delta_reasoning_tokens": r["usage"]["output_tokens_details"]["reasoning_tokens"],
                "explicit_exact": sum(e["exact_normalized_match"] for e in s["exact_human_edits"]),
            }
        )
    study.save(study.OUT / "delta-summary.json", rows)
    import json

    print(json.dumps(rows, ensure_ascii=False, indent=2))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "score":
        score()
        return
    study.parallel([(one, (cid,)) for cid in study.load(study.OUT / "inputs.json")])
    score()


if __name__ == "__main__":
    main()
