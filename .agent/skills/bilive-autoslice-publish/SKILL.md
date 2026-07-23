---
name: bilive-autoslice-publish
description: "操作、修复、审查或发布李豆沙 autoslice 成品；从 free runtime authority 开始，按当前 package audit 与 authorized-upload 闭环执行。"
---

# Bilive Autoslice Publish

本 skill 是**操作配方**。规则 authority 在 `../../../docs/pipeline/`；不要从本文件、全局
`~/.codex` 镜像、memory 或日期化报告复制一套平行规则。

## 1. 先读真实面

```text
free:/opt/bilive/autoslice/repo
free:/opt/bilive/autoslice/{state,out,reports}
free:/opt/bilive/bilive-recorder
container bililive_recorder:/rec
```

先读 `DEPLOYED_COMMIT`、目标 state/record、实际 artifact hashes 与 B 站公开/Creator 面。
本地 checkout、旧审片包、某个 `review_ready` 字段或命令返回 0 都不能代替 live readback。
`/opt/bilive/app` / `bilive_record:/app` 是 tooling/legacy app，不是 recorder 或 autoslice authority。

## 2. 按步骤操作

只读当前任务涉及的 step：

- 选片/精确恢复：[20-selection.md](../../../docs/pipeline/20-selection.md)
- 边界：[30-boundary.md](../../../docs/pipeline/30-boundary.md)
- 字幕/专名/最终 owner：[40-subtitle-text.md](../../../docs/pipeline/40-subtitle-text.md)
- 歌切：[50-song-lane.md](../../../docs/pipeline/50-song-lane.md)
- 标题：[60-title.md](../../../docs/pipeline/60-title.md)
- 封面：[70-cover.md](../../../docs/pipeline/70-cover.md)
- 打包/audit：[80-package-delivery.md](../../../docs/pipeline/80-package-delivery.md)
- 授权上传/同 BV 修复：[90-publish.md](../../../docs/pipeline/90-publish.md)

## 3. 成片与审计

1. 从 current state/record 构建扁平审片包；exact recovery 只有
   `exact-talk-contract-closure.v1.status=COMPLETE` 才可覆盖旧包。
2. 验证最终 MP4、SRT、speaker SRT/ASS、封面、title、record、StoryContract、
   clip-context、truth/baseline owner attestations 和 route evidence 都来自同一最终字节。
3. talk 片头与 song 无片头只按当前 `branding_intro` manifest 验，不硬编码某个 Z1/Z2
   文件、时长或 hash。
4. 运行 `scripts/audit_lidousha_review_package.py --json`。新包必须得到当前 audit v2、
   当前 epoch/fingerprint 和完整 audited-input closure；旧 `passed:true` 不可复用。
5. 无 Ivan 上传授权时停在 `NO_UPLOAD`，但仍可完成审片包与失败诊断。

## 4. 授权发布

用户明确授权某条后：

1. 用 `scripts/authorized_upload.py make-manifest` 绑定最终包、冻结标题/tags 与授权原话；
2. 用 `verify` 让 uploader 重跑当前 auditor、strict SRT、共享标题门与 hashes；
3. 用 `upload` 一次投稿；不得裸调 `biliup`/`do_upload.sh`；
4. 已拿到 BVID 但合集/公开验证未完成时，只用幂等 `season-add`，不得重传；
5. 只有 public view、public tags、Creator archive 与精确 section singleton/title 全部一致，
   才能写 completed ledger；
6. 授权、manifest、uploaded/public/season evidence 必须 commit。

## 5. 已发稿修复

先确认部署版本含当前 `same_bv_repair.py`，且最终包通过 current manifest/audit；源码存在或
单测通过不等于 production 已部署。只用 `authorized_upload.py repair-plan` 冻结同 BVID 的
单 P live truth，再用 `repair-status` / `repair-run --dry-run` 检查，最后以 `repair-run`
跨进程幂等推进到 `VERIFIED`。它与投稿共用 `upload.lock`，append intent 落盘后绝不再次
append，21540/timeout 只可对同一 frozen CID/payload 重试。

不得用 `swap_video_p.py`、legacy replace、裸 API 或手工 append/edit，也不得为修正新投稿。
只有同 BVID/aid、唯一新 CID/P、public/Creator/section 与最终 metadata 全部读回一致才算完成；
完整状态机、退出码与判据见 [90-publish.md](../../../docs/pipeline/90-publish.md)。

## 6. 安全

- 不打印 cookie、token、API key 或含密钥的完整命令。
- 不凭历史文档冻结账号、合集 ID、运行 commit 或配额状态；每次 live inspect。
- 发布路径 fail closed。需要新权限、验证码或无法证明同 BV 闭环时，停止并报告 blocker。
