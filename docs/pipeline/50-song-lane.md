# 50 歌切专线

本文件是歌切步骤的**分步权威**。`docs/workflows/lidousha-song-finished-package-workflow.md`
与 `.agent/skills/song-lyrics-timeline-aligner/SKILL.md` 只提供操作方法，不能覆盖本文件或
当前 schema。

## 要点（指针表）

- 识别/去重：`song_lane.py`（视觉歌名 hint 优先于演唱 ASR；已发布歌按 normalized 标题+别名去重 `published_song_history.py`）。
- 边界：`live_source_review.py` song_boundary（首尾演唱、≥7 行且 ≥80% 演唱、戏剧对白块四重限制）。
- 本人演唱证明：`agy-audio-lrc-observation.v5` 与 `host-vocal-proof.v3` 在同一
  source/LRC evidence 上做联合门；CAM++ 只从明确演唱行取样。memory/日期化 review
  只作历史案例，不是 schema authority。生产音频输入只交 AGY；AGY quota、timeout 或
  输出失败必须写 typed provider failure 并由 runner 重试，禁止转交 Gemini API、CPA 或
  其他文字模型。旧 Gemini 音频回执不能通过现行 execution provenance 门。
- 歌词字幕：external LRC 全局位移（`song_alignment.py`），零点锚 BCUT ASR 中位（`offset_basis`）；精确重编码；ASS 取整。**歌词正文不过词表修复链**（结构性事实，见 41 重构议题）。
- 歌切不带片头；最终结构由 [80-package-delivery.md](80-package-delivery.md) 强制。
- 标题：铁律目录式，见 [60-title.md](60-title.md)。
- 每场歌配额 1（多场日歧义待 Ivan 拍板）。
