# 李豆沙歌切 finished-package 操作流程

> 当前操作配方，2026-07-23 收敛。硬规则不在本文件复制：
> 歌切证明见 [50-song-lane.md](../pipeline/50-song-lane.md)，标题见
> [60-title.md](../pipeline/60-title.md)，封面见
> [70-cover.md](../pipeline/70-cover.md)，打包/audit 见
> [80-package-delivery.md](../pipeline/80-package-delivery.md)，发布见
> [90-publish.md](../pipeline/90-publish.md)。

## 操作顺序

1. 从 source-bound song anchor 回到完整源，证明歌曲身份、完整演唱边界、canonical timed LRC
   与当前音频单一位移；anchor 不能直接当最终片段。
2. 只有当前 song step 要求的 live-performance 与 host-vocal 两个独立子门共同通过，才能
   materialize 歌切。原唱/回放/BGM/其他歌手、证据或 hash 漂移都保持 BLOCK。
3. 使用 external-LRC timeline 生成最终 SRT/ASS，精确 re-encode；不得用 ASR 歌词时间或
   `-c copy` 作为最终字节。
4. burn 前运行共享 strict SRT validator；每个非空 block 都必须被消费，连续编号、合法正向
   时间、至少 300ms、单调无 overlap、文本与媒体边界合法。视觉行宽另按 manifest 绑定的
   Sapphire72 契约验证。
5. 最终标题只能是 `【李豆沙】豆沙歌，《canonical歌名》`；封面文字只能是 `《歌名》`。
6. song 当前选择 CPA `cpa_redraw` 的 clean aesthetic；必须有真实调用/资产 hash、无字背景、
   本地 glyph overlay、最终像素/字形证据。生成失败保持 cover pending/BLOCK，不能拿 raw
   frame 伪装发布封面。
7. 生成 portable review package，至少包含最终 media、SRT、ASS、cover/reference、
   record、review manifest、StoryContract、alignment/performance evidence 与 audit。
8. 运行当前 canonical auditor。完成包必须得到
   `lidousha-review-package-audit.v2`、
   `2026-07-23.final-artifact-gates.v3`、当前 policy fingerprint、完整 audited-input closure
   与 zero blockers。旧 `passed:true` JSON 或 dated sample 状态不能复用。
9. 无 Ivan 明确授权时保持 no-upload。授权后只走 `authorized_upload.py`，上传器会重跑
   current auditor、strict SRT 与共享 title gate。

## 交付布局

审片包使用 manifest 明示的 portable paths，不依赖远端绝对路径或相似文件名猜测。面向 Ivan
的浏览面可以扁平，但 evidence closure 必须保留，并让每个最终文件的 SHA-256 可从 record、
manifest 与 audit 三方读回。

## 完成判据

- full song identity/boundary/performance proof PASS；
- 最终 SRT/ASS 与 burn 的 hashes 一致，strict/visual gates PASS；
- 精确目录标题、匹配封面文字与当前 cover proof PASS；
- package audit v2 当前且输入无漂移；
- review package 明确 no-upload，或存在 Ivan 授权的 v3 manifest；
- 若已发布，公开/Creator/section 四面闭环完成。

## 历史样本

2026-06-29/07-03 的 sample、旧错误码、旧模型名和当时的“passed”只用于事故复盘，已不作为
gold command 或当前 acceptance。历史事实保留在 Git 与日期化 `docs/reviews/` /
`docs/spark/` 中；执行者不得从那些文件复制旧命令覆盖本流程。
