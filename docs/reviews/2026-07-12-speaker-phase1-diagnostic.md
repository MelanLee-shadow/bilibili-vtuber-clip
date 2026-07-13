# 2026-07-12 人声二分离第一阶段诊断

## 目标

在不改变 production、不启用上传或 cron 的前提下，建立一份不含模型预测的李豆沙/非李豆沙人耳真值包；同时确认当前生产是否会在独播时浪费资源，并定义保守的整场独播快速放行边界。

完成判据：120 条历史联动 cue 可本地试听、自动保存、导出 hash-bound package ID 的 JSON；2026-07-10 独播不进入样本；生产 gate 设计对不确定场次默认升级而不是漏掉嘉宾；用当前生产与隔离 binary-v4 候选完成一次真实离线裁决。

## 已完成

- 用户确认 2026-07-10 是独播；此前 5 个 `single_host` 成品应视为正确独播证据，不再作为疑似失败样本。
- 从 2026-07-09 已知联动选择 4 个素材，各抽 30 条：
  - `promo_203027_314_479`：纯自动基线；
  - `promo_220021_125_232`：source-session anchor 路径；
  - `promo_210025_643_801`：困难换人/重叠历史样本；
  - `promo_193036_367_476`：边界拆分对照。
- 生成盲听包 `lidousha/2026-07-12/人声二分离第一阶段盲听/`：120 条，模型预测、颜色和 override 均未写入页面。
- Ivan 已完成 120/120 人耳标注：李豆沙 40、非李豆沙 59、mixed/overlap 20、unjudgeable 1，32 条带备注；labels 与 manifest 的 package ID、120 个 review ID 完整一一对应，无缺失、额外或重复。
- 找回两份此前遗漏的 Ivan 人工修订真值（位于 `stash@{0}^3`）：
  - `15-v6.review-edit.srt` SHA-256 `4092dfd4...a49a1`；与 phase1 的 15 是同一场景但不同分段，禁止按后续 cue 编号硬 join。最新 labels 对重叠样本优先，其中开头明确应为李豆沙说“弹幕说”。
  - `R1-v4.review-edit.srt` SHA-256 `27262866...f6b38`；与 phase1 四段无内容重合，额外提供 47 条清晰单人（李豆沙 27、非李豆沙 20）、8 条 mixed、2 条排除项。
- 新增可复用构建器 `scripts/build_speaker_blind_review.py` 和两个确定性测试。
- 浏览器实际验证：页面加载 120 条；AAC 音频返回 200；标注后进度变为 1/120；刷新后自动保存仍为 1/120；导出 JSON 含 120 个 review ID。测试导出文件已删除。
- 只读确认 production `0f31119f...` 当前没有 session gate：每个 talk 都以 `--speaker-mode required` 无条件运行完整 finalizer。

## 评分结论

### 当前生产路径

在 `free` 对 4 个历史联动片段做了无 speaker override 的当前生产精确重跑，4/4 `READY`、hash/媒体/文字/计划绑定全通过。与 120 条最新人耳真值按时间重叠对齐：

- 99 条清晰二分类 cue：cue accuracy `75.76%`，时长加权 `81.70%`，macro-F1 `0.7533`；
- 李豆沙 precision/recall/F1：`67.39% / 77.50% / 72.09%`；
- 非李豆沙 precision/recall/F1：`83.02% / 74.58% / 78.57%`；
- CAM++ 直接声学裁决为 `46/50 = 92%`，纯文本 whole-clip context 为 `29/49 = 59.18%`。

结论：当前生产成片颜色准确率不够；主要瓶颈不是 sapphire/white 渲染，而是短句被 text-only context 猜身份。

### binary-v4 v6 正式冻结候选

远端旧 scratch 实际比 handoff 更新：`free:/tmp/binary-v4-freeze-fullv4-v2/out/` 已存在 15/R1 双跑 byte-stable 的 `FROZEN` marker，CPU RTF 分别约 `2.06`、`1.75`，峰值内存低于 3 GiB。隔离 evaluator 首次打开旧人工 SRT 后，候选失败：

- 15 困难子集出现 2 条真实 mixed false READY；旧 SRT 未给开头 speaker 前缀导致 scorer 另计 1 条 UNKNOWN false READY，但最新 labels 已确认该条实际为李豆沙。
- R1 困难子集出现 12 条 false READY；其中连续 6 条清晰 guest 被 `BINARY_FUSION_SESSION_PROMOTION` 错升为 TARGET。

结论：v6 的 anonymous track 传递闭包/session promotion 会把局部关联漂移成身份，不可部署。

### binary-v4 v7 既有安全候选

本地被隔离的旧 stash 在打开 oracle 前已经包含两个关键安全改动：禁止 track 连通分量传递身份，并要求 session promotion 与片段自身声学区间相容。补齐其下游 `source_acoustic_projection` 的 fail-closed 消费契约后，在 `free:/tmp/binary-v4-v7-diagnostic/` 做真实 CPU 重跑：

- 15：全片 `13 TARGET / 6 OTHER / 29 REVIEW`；旧困难子集 6 条 READY 全对；最新 phase1 的 30 条采样中，25 条清晰二分类里 11 条 READY、`11/11` 全对，5 条 mixed 全部 REVIEW。
- R1：全片 `36 TARGET / 24 OTHER / 56 REVIEW`；困难子集清晰单说话人 false READY 为 0，19/48 条 eligible 正确 READY；仍有 2 条 mixed cue 被错误 READY（一个漏检 overlap，一个保留 local TARGET lock）。
- 完整隔离测试：`1271 passed, 18 skipped, 0 failed`；18 个 skip 是 Mac Python 3.14 缺 NumPy，真实 CAM++/pyannote 已由远端重跑覆盖。
- recovery 分支：`codex/binary-v4-recovery-v7 @ ef37e46`。这是 stash/recovery ancestry，不是 merge-ready 生产分支。

结论：v7 已证明“清晰 READY 高精度、困难处 fail closed”，值得继续；但困难子集自动正确覆盖仅约 29%–44%，仍是安全实验，不是无人值守联动成品线。

整场 gate 的建议合同为 `speaker-routing.v1`：

1. 在候选选出、三个 producer 并发前，每场只运行一次。
2. 文本/标题/弹幕中的“联动、连线、队友、嘉宾名”只能触发完整二分离，不能单独证明独播。
3. 从候选和场次首/中/尾抽 8–16 个不少于 1.8 秒的语音样本；CAM++ 模型和李豆沙参考只加载一次。
4. 任何文本联动信号、非李豆沙样本、第二声纹簇、样本不足、阈值边缘或解码失败，都返回 `RUN_BINARY_FINALIZER`。
5. 只有覆盖完整且全部样本稳定属于李豆沙时返回 `FAST_SOLO`；快速路径仍生成正式全 Sapphire SRT/ASS/manifest，并绑定媒体、文字、模型和 profile 哈希。
6. gate 先 shadow：仍运行完整 finalizer，只比较路由结论。已审联动场次出现一次错误 `FAST_SOLO` 都不得启用跳过。

## 进行中（含后台进程）

- 没有遗留本地/远端模型进程；远端仅保留 `/tmp` 诊断与 post-freeze evaluator 证据。
- production cron 仍由 `/opt/bilive/autoslice/DISABLED` 暂停；本轮未改、未部署、未上传。

## 阻塞

- v7 的 overlap 检测仍有 2 条 false READY；在接入正式成品链前，mixed/overlap 应统一 fail closed 或取得可验证 split。
- v7 在联动困难 cue 上覆盖不足；若直接接生产，很多联动切片会停在 REVIEW_REQUIRED，尚不满足终极无人值守。
- session gate 尚未用 2026-07-10 独播与 2026-07-09 联动做 shadow 混淆矩阵，不能直接启用资源跳过。

## 下一步

1. 从 deployed superset/current production 的干净基线新建 integration 分支，只移植 v7 已验证的 acoustic/fail-closed 核心；禁止把 recovery stash ancestry 直接 merge/rsync 到生产。
2. 删除 text-only context 的身份决定权；文本最多触发 `RUN_BINARY_FINALIZER` 或 veto，不能把 UNKNOWN 升为 TARGET/OTHER。
3. 对任何 pyannote overlap、双声道/模型冲突、同 cue 换人或 local/session 不一致统一 REVIEW；再用现有 120+R1 回归集验证清晰 false READY 不回升。该集合从现在起是开发回归，不再冒充 holdout。
4. 建 `speaker-routing.v1` shadow：2026-07-10 独播必须 FAST_SOLO，2026-07-09 四段联动必须 RUN_BINARY；任何不确定默认 RUN_BINARY。shadow 通过前不省略二分离资源。
5. 覆盖与 overlap 规则稳定后，再给 Ivan 一个新的、未参与调试的小型联动 holdout 盲听包；当前阶段不需要额外人耳复核。
