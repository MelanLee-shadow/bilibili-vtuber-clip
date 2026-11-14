#!/usr/bin/env python3
"""Choose among rendered, code-vetoed candidates using the existing CPA vision reader."""
import json
import os
import re
import sys

from cover_candidates import CALLS, vision


def main(job_path):
    with open(job_path, encoding="utf-8") as fh:
        job = json.load(fh)
    out = job["out"]
    rows = job["candidates"]
    question = ("这些是同一条切片的已渲染封面候选，最后一张是源画面。请亲自比较图像，选最适合的一张；"
                "代码的否决理由只是提示，不是最终决定。判断人物、身份、画面与文字是否符合源画面，"
                "说明选择理由与仍有的缺点。必须从候选 ID 中选择一个，不得发明图片。\n"
                + "标题：" + job["title"] + "\n封面字：" + " / ".join(job["lines"]) + "\n"
                + "\n".join(f"第{i}张 ID={row['id']}，代码理由={row['vetoes']}"
                            for i, row in enumerate(rows, 1))
                + '\n只输出 JSON：{"chosen":"候选ID","reason":"看图选择的理由及缺点"}')
    images = [(os.path.join(out, f"{row['id']}.png"), 1920) for row in rows]
    answer, _ = vision(job, "fallback-choose", question, [*images, (job["frame"], 1280)], out)
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    choice = json.loads(match.group(0)) if match else {}
    if choice.get("chosen") not in {row["id"] for row in rows} or not str(choice.get("reason") or "").strip():
        raise ValueError("CPA_COVER_CHOICE_INVALID")
    with open(os.path.join(out, "fallback.result.json"), "w", encoding="utf-8") as fh:
        json.dump({"choice": choice, "calls": CALLS}, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv[1])
