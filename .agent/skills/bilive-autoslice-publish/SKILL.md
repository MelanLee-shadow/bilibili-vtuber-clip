---
name: bilive-autoslice-publish
description: "操作、修复、审查或发布李豆沙 autoslice 成品；从 live runtime authority 开始，按当前打包审计、最终感知复核与 authorized-upload 闭环执行。"
---

# Bilive Autoslice Publish

本 skill 只提供**操作入口和顺序**。所有准入、证据 schema、状态机与漂移判据均以
[`docs/pipeline/`](../../../docs/pipeline/README.md) 对应 step 为唯一权威；不要从本文件、
memory、日期化报告或本地旧审片包推导规则。

## 1. 从 live authority 开始

先按相关 step 现场读取部署 commit、运行 state/record、最终 artifact hashes、录制源和
B 站 public/Creator/section 状态。本地 checkout、旧包、某个状态字段或命令返回 0 都不能
替代 live readback。

## 2. 只读当前任务涉及的 step

- 源录像：[10-source-recording.md](../../../docs/pipeline/10-source-recording.md)
- 选片/exact 状态：[20-selection.md](../../../docs/pipeline/20-selection.md)
- 边界：[30-boundary.md](../../../docs/pipeline/30-boundary.md)
- 字幕与语义修复：[40-subtitle-text.md](../../../docs/pipeline/40-subtitle-text.md)、
  [41-semantic-repair.md](../../../docs/pipeline/41-semantic-repair.md)
- 歌切：[50-song-lane.md](../../../docs/pipeline/50-song-lane.md)
- 标题/封面：[60-title.md](../../../docs/pipeline/60-title.md)、
  [70-cover.md](../../../docs/pipeline/70-cover.md)
- 打包/audit：[80-package-delivery.md](../../../docs/pipeline/80-package-delivery.md)
- 授权上传/同 BV 修复：[90-publish.md](../../../docs/pipeline/90-publish.md)

## 3. 打包与最终复核

按 [80-package-delivery.md](../../../docs/pipeline/80-package-delivery.md) 构建当前审片包并运行
canonical auditor。机器 audit 不代替人对最终烧录字节的完整观看。

需要 exact same-BV repair 时，先用
`scripts/build_lidousha_final_human_review.py --prepare-evidence-template` 冻结当前 package、
manifest 与 canonical audit 的字节绑定；实际 reviewer 再按 90 step 逐点观看这些相同最终字节、
填写独立 observations；最后用同一 builder create-only 构建 receipt。具体输入、证据与绑定规则
只读 [90-publish.md](../../../docs/pipeline/90-publish.md) 和脚本当前 `--help`。无上传授权时
停在 `NO_UPLOAD`，仍可完成打包、审计与诊断。

## 4. 新投稿

用户明确授权后，只通过 `scripts/authorized_upload.py` 的 `make-manifest`、`verify`、
`upload`、必要时 `season-add` 操作。完整准入、配额、合集与公开验收顺序只读
[90-publish.md](../../../docs/pipeline/90-publish.md)；不得裸调 uploader 或 legacy 脚本。

## 5. 已发稿同 BV 修复

先确认当前修复代码已部署、最终包通过当前 80/90 step，并完成真实最终感知复核。然后：

1. 用 `make-manifest --final-human-review ...` 与 `verify` 冻结并重验当前包、receipt 和授权；
2. 运行 `repair-plan --dry-run`，确认后再 create-only 建立 plan/journal；
3. 用 `repair-status` 查看**本地** plan/journal 状态，再运行 `repair-run --dry-run`；
4. 只用 `repair-run` 顺序执行或幂等 resume；多稿不得并行修复；
5. journal 到达 `VERIFIED` 后，仍须运行
   `repair-verify-live --out <same-bv-repair-completed.json>` fresh readback；只有生成
   create-only completed sidecar 才完成公开闭环。

`repair-plan`、`repair-run`（包括 dry-run）与 `repair-verify-live` 的远端读取前都必须通过
当前 90 step 定义的 read-only login canary。`repair-status` 只重验本地闭包，不访问线上，
不能证明公开态。状态机、退出码、重试边界和 completed sidecar 契约只读
[90-publish.md](../../../docs/pipeline/90-publish.md)。

不得使用 `swap_video_p.py`、legacy replace、裸 API、手工 append/edit，也不得为修正另建
新 BV。

## 6. 安全

- 不打印 cookie、token、API key 或含密钥的完整命令。
- 不从历史文档冻结账号、合集 ID、部署 commit、运行状态或配额。
- 发布路径 fail closed；需要新权限、验证码或无法证明闭环时停止并报告 blocker。
- 封面事实与叙事的验收只读 [70-cover.md](../../../docs/pipeline/70-cover.md)。
