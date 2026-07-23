# vtuber-slice Agent Notes

## 流水线权威

- 当前规则只在 [docs/pipeline/README.md](docs/pipeline/README.md) 及其分步文件中维护。
  进行到哪一步只读哪一步；改规则时只改对应 step 与它指向的代码/资产强制层。
- 本文件、skills、HANDOFF、日期化报告和 memory 只提供入口、操作方法或历史证据，不复制
  step 正文。runtime 状态也不写死在文档中，按相关 step 现场读取。

## 分步入口

- 源录像/录制：[`docs/pipeline/10-source-recording.md`](docs/pipeline/10-source-recording.md)
- 选片/量化/exact 状态：[`docs/pipeline/20-selection.md`](docs/pipeline/20-selection.md)
- 边界：[`docs/pipeline/30-boundary.md`](docs/pipeline/30-boundary.md)
- 字幕与语义修复：[`docs/pipeline/40-subtitle-text.md`](docs/pipeline/40-subtitle-text.md)、
  [`docs/pipeline/41-semantic-repair.md`](docs/pipeline/41-semantic-repair.md)
- 歌切：[`docs/pipeline/50-song-lane.md`](docs/pipeline/50-song-lane.md)
- 标题/封面：[`docs/pipeline/60-title.md`](docs/pipeline/60-title.md)、
  [`docs/pipeline/70-cover.md`](docs/pipeline/70-cover.md)
- 打包/发布：[`docs/pipeline/80-package-delivery.md`](docs/pipeline/80-package-delivery.md)、
  [`docs/pipeline/90-publish.md`](docs/pipeline/90-publish.md)

## 专项操作入口

- 活字乱刷：`.agent/skills/huozi-luanshua/SKILL.md`
- 歌词时间轴：`.agent/skills/song-lyrics-timeline-aligner/SKILL.md`
- autoslice 生产/发布操作：`.agent/skills/bilive-autoslice-publish/SKILL.md`
