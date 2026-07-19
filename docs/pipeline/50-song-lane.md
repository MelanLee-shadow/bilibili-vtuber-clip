# 50 歌切专线

本文件是歌切步骤的**分步权威**。完整包工艺强权威：
`docs/workflows/lidousha-song-finished-package-workflow.md`；歌词时轴 skill：
`.agent/skills/song-lyrics-timeline-aligner/SKILL.md`。

## 要点（指针表）

- 识别/去重：`song_lane.py`（视觉歌名 hint 优先于演唱 ASR；已发布歌按 normalized 标题+别名去重 `published_song_history.py`）。
- 边界：`live_source_review.py` song_boundary（首尾演唱、≥7 行且 ≥80% 演唱、戏剧对白块四重限制）。
- 本人演唱证明：`host_vocal_proof.py`（CAM++ 只从演唱行取样）；AGY 回声防御与日语门见 memory `lidousha-japanese-song-gates`。
- 歌词字幕：external LRC 全局位移（`song_alignment.py`），零点锚 BCUT ASR 中位（`offset_basis`）；精确重编码；ASS 取整。**歌词正文不过词表修复链**（结构性事实，见 41 重构议题）。
- 歌切不带片头（Ivan 2026-07-14，`cf09597`）。
- 标题：铁律目录式，见 [60-title.md](60-title.md)。
- 每场歌配额 1（多场日歧义待 Ivan 拍板）。
