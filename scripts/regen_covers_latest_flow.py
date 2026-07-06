#!/usr/bin/env python3
"""Regenerate ONLY the cover (latest cover flow) for finished 7/3 clips —
subtitles/clip/title are left untouched (title reused verbatim).

Ivan 2026-07-04: subtitles are locked-good; the cover flow was refined by the
parallel cover-redesign session, so redo covers with the latest flow, keep
everything else.  Usage: python3 scripts/regen_covers_latest_flow.py <cid> ...
"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_auto_review_shadow_pipeline import (  # noqa: E402
    _lidousha_cover_text,
    _stage_lidousha_ai_cover,
)
from src.autoslice.llm_client import LlmConfig, build_llm_call  # noqa: E402

FINALS = ROOT / "reports" / "lidousha-autoslice-20260703" / "finals"
DELIVERY = ROOT / "lidousha" / "2026-07-03"

# cid -> delivery basename (Chinese)
DELIVERY_NAME = {
    "hongshui": "候选_哄睡妈妈哄女儿",
    "zhiboqiang": "候选_直播腔带到三次元",
    "chenghudazhan": "候选_称呼大战",
    "xiongmaoweizhuang": "候选_熊猫伪装成人类",
    "nailongdouchong": "候选_奶龙斗虫宇宙",
}

art_llm = build_llm_call(
    LlmConfig(
        transport="command",
        command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file}",
        timeout_seconds=180.0,
    )
)

for cid in sys.argv[1:]:
    recut = FINALS / cid / "replacement_recuts"
    record_path = next(recut.glob("*.record.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    title = (record.get("publish_staging") or {}).get("title") or record.get("title") or cid
    media_path = Path(record["media_path"])
    cover_text = _lidousha_cover_text(title)
    print(f"\n########## {cid} ##########\n  title: {title}\n  cover_text: {cover_text!r}")
    result = _stage_lidousha_ai_cover(
        record,
        media_path=media_path,
        candidate_id=cid,
        title=title,
        cover_text=cover_text,
        run_ffmpeg=True,
        art_direction_llm_call=art_llm,
    )
    status = result.get("status")
    cover_path = result.get("cover_path")
    print(f"  status: {status}  cover: {cover_path}")
    if status == "AI_COVER_READY" and cover_path and Path(cover_path).is_file():
        dest = DELIVERY / f"{DELIVERY_NAME[cid]}.cover.png"
        shutil.copy(cover_path, dest)
        print(f"  → delivered {dest.name}")
    else:
        print(f"  !! cover NOT ready ({status}) — delivery cover unchanged")
