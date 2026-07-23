# Autoslice capability map

Updated: 2026-07-23

> 本文件只列出源码中存在的能力与当前权威入口，不声称某个 commit 已部署、某日期已
> `review_ready`、cron 正在运行或公开稿件已生效。实时状态必须从
> `free:/opt/bilive/autoslice/{repo,state,out,reports}` 与 B 站读回。

| 能力 | 当前机器门 | 权威 |
|---|---|---|
| 源录制 | BililiveRecorder 唯一录制器、FileClosed/source completeness、mount/container 证明 | [10-source-recording.md](pipeline/10-source-recording.md) |
| 选片 | Tier 硬准入、七维固定算术、可执行绝对分校准、exact lifecycle closure | [20-selection.md](pipeline/20-selection.md) |
| 边界 | 人工下界 + 必需 semantic review + cue/syntax/四命题 | [30-boundary.md](pipeline/30-boundary.md) |
| talk 字幕 | hash-bound clip context、专名/聊天/声学裁决、baseline→truth、final owner、strict SRT | [40-subtitle-text.md](pipeline/40-subtitle-text.md) |
| 语义修复 | per-mention alias、整片 callback、幻听删除专线、无见证不改 | [41-semantic-repair.md](pipeline/41-semantic-repair.md) |
| song lane | full-song identity/LRC/current-audio/live-performer/host-vocal joint gate | [50-song-lane.md](pipeline/50-song-lane.md) |
| 标题 | manual body/auto generation 共用 archive envelope 与 package/upload validator | [60-title.md](pipeline/60-title.md) |
| 封面 | screenshot/AI evidence route、关系型 final participant、真实 glyph bbox | [70-cover.md](pipeline/70-cover.md) |
| 打包 | intro roster、StoryContract、audit v2 epoch/fingerprint/input closure | [80-package-delivery.md](pipeline/80-package-delivery.md) |
| 发布 | manifest v3、auditor rerun、same-BV edit、public/Creator/section closure | [90-publish.md](pipeline/90-publish.md) |

## 当前已知能力边界

- speech memory、话题图与整场上下文只扩大候选，不直接授权改字。
- 自动发现并不能保证抓住每一个语义通顺的幻听插入词；已发现项可以声学删除并 fail closed。
- final-pixel schema 证明可复验的变换/人物/字形契约，不等同于通用计算机视觉“绝不会看错人”；
  修改像素的关系图仍需独立 verifier。
- `swap_video_p.py` 仍不是发布入口；完整同 BV 修复只由
  `authorized_upload.py repair-plan / repair-run / repair-status` 的 hash-bound plan、
  durable journal 与四面 readback 持久化。代码存在不等于已部署或某次线上修复已完成。
- 模型/provider、配额、cookie、合集 ID、部署 commit 与 runtime health 都是易变状态，不在文档
  固化。
