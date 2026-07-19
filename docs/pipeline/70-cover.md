# 70 封面

本文件是封面步骤的**分步权威**。工艺细节强权威：
`docs/workflows/lidousha-song-finished-package-workflow.md` §5（CPA 路线）+
memory `lidousha-cover-redesign-halfbody` / `cpa-real-ai-cover-always` / `lidousha-cover-no-extra-accessories`。

- 永远 CPA 真实出图（gpt-image-2 `images.edit`，含测试）；CPA 在 Cloudflare 后必须带浏览器 UA。失败 fail-closed `BLOCKED_AI_COVER_REQUIRED`。
- 形象铁律：以当场直播形象为原型，只改动作/表情/Q版；禁加饰品服装；多人场景主体锁定李豆沙；表情永不吐舌头。
- 版式：talk 轮换 left-split/right-split/banner；歌切恒 song-clean 且标题字要大（banner 级）；art direction 由 `_lidousha_cover_art_direction` 决定（`cover_generation.py`）。
- 封面文字 = 标题去前缀（歌切即 `《歌名》`）；无冒号，分句换行；Ivan 定稿标题成分一个不许丢。
- 字体：全链验字形 + Noto CJK 兜底 + `glyph_risk` 披露（memory `cover-font-zi-renders-as-bai`，a74520b）。
