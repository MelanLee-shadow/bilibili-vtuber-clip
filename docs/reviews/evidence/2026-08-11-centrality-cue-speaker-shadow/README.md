# Centrality 口径、cue 级声纹与上线门：P0 / shadow 证据（2026-08-11）

本目录记录 Ivan 于 2026-08-11 批准的推荐口径、随后完成的 P0 安全修复、`free`
隔离实跑和最终 go/no-go。它是**开发证据与接线合同**，不是部署、发布或上传授权。

## 结论先行

1. **推荐口径已经落成确定性纯函数与哈希收据**，且 3/4 级绝不再进入排序或破同分：
   `null/[存疑]` 转人工，0–1 自动淘汰，2 只许人工选，3–4 同等进入自动候选池。
2. **声纹安全与记账缺陷已经修复**：静音会打断平滑连段，重叠窗只记一次，
   `SOLO_VERIFIED` 必须有可信 solo source，逐 prototype 分数和音频哈希被保留，
   cue 的 HOST / OTHER / UNKNOWN / NON_SPEECH / 未覆盖时长全部披露。
3. **当前声纹仍不准，不能接生产。** 8/8 人工真值上的指定策略有 1 个 false-host，
   主播召回只有 6.52%，清晰 cue 覆盖 44.15%；开发门失败，聚合 CLI 按设计退出 2。
4. **生产保持停用。** 2026-08-11 06:35:01Z 现场复核：部署标记仍是
   `f03e1bdaacf0f01ea2ec4165e38c590199dc5d57`；`/opt/bilive/autoslice/DISABLED`
   存在；timer/service 均 inactive；无 runner 进程。本轮没有 deploy、enable、push、
   upload，也没有写生产 `state/` / `out/` / `repo/`。

## 一、Ivan 批准的推荐口径

| 输入状态 | 处理 | 自动候选池 | 排序影响 |
|---|---|---:|---:|
| 尚未评估 `NOT_RUN` | 停在未评估态，不伪装成 0 或 null | 否 | 无 |
| 说话人 `[存疑]` / centrality `null` | 有界人工 speaker review | 否 | 无 |
| 已验证，centrality 0–1 | `AUTO_INELIGIBLE` | 否 | 无 |
| 已验证，centrality 2 | `MANUAL_SELECTION_ONLY` | 否 | 无 |
| 已验证，centrality 3–4 | `AUTO_ELIGIBLE` | 是 | **无** |

实现：

- `src/autoslice/centrality_policy.py`：纯函数、0–4 映射、证据结构校验；
  `centrality_rank_component()` 对 3/4 都返回空元组。
- `src/autoslice/centrality_receipt.py`：绑定 candidate interval、resolved boundary、source
  media、ASR、speaker receipt、cue labels 和 content-rank-v2 receipt；任一漂移均 stale。
- provisional speaker authority 只能生成 `SHADOW_ONLY_SPEAKER_CALIBRATION_REQUIRED`；
  automatic selection 还必须有独立的 content-rank-v2 receipt；upload 永远为 false。
- legacy v1 只嵌入审计字段，强制 `decision_influence=false`。改变 v1 分数会改变审计哈希，
  但不会改变 gate、rank、quota 或 backfill。

这解释了“推荐口径”的核心：centrality 是**身份依赖的资格门**，不是可被其他内容分
补偿的加分项；3 和 4 都已经足够以主播为叙事中心，后续只按非 centrality 的内容质量排。

## 二、P0 声纹修复与 shadow 工具

### 候选级安全修复

`src/autoslice/host_occupancy.py` 已升到 schema/estimator v2，但尚无生产 caller：

- 多 prototype 原始分数与每个分析 WAV 的 sha256 不再丢失；
- 2s/1s 重叠窗投影为互斥 attribution cells，跨标签不会重复记时；
- `NON_SPEECH` 或真实时间缺口会打断平滑连段；隔长静音的四个孤立 HOST 窗不再被
  拼成 false `SOLO_VERIFIED`；
- `SOLO_VERIFIED` 除声学门外还必须 `solo_source=true`；
- cue 只有已知说话人占自身时长至少 85%，且没有足量 UNKNOWN / 静音 / 未覆盖，才可硬标；
  否则是 `[存疑]`。

### cue-aligned challenge

- `scripts/run_cue_aligned_speaker_shadow.py`：严格校验媒体、SRT、profile、reference、
  CAM++ model、runtime、decoded PCM、runner/config 哈希；所有可解码 cue 都保留分数。
- 小于 300ms 才禁止硬判；300–1500ms 短句仍可用 2-of-3 / 3-of-3 保守 consensus，
  不再整层丢弃。长句按最少数量、每窗不超过 4s 的 deterministic 子窗做一致性判定。
- mixed / 子窗冲突只允许降级 UNKNOWN，不能抬高置信；energy activity 明确不是 speech
  或 BGM 分类，报告固定 `bgm_assessed=false`。
- `scripts/evaluate_host_occupancy_challenge.py` 独立 replay persisted prediction，核对 exact
  media/SRT/cue grid/text；`scripts/aggregate_cue_speaker_challenge.py` 要求至少两个候选、
  每候选和 pooled 双门，并硬要求两个锁定跨场 holdout。

## 三、reviewed enrollment 实料

### 7/22 跨场开发 bank（实际进入本次 diagnostic）

精确来源：

- media sha256 `f5662ff2c919101e90c1e5028f68d5c5f59aa4e68d5fd0eb00d11121f805cbc7`
- text-final SRT sha256 `81d326339fa84f3971ea96c364f190cdcc829794c901e544a001d003de6dfed4`
- Ivan-reviewed override sha256
  `1e2fe85ac75c89c89a9e3842ef42d9763ce7a0de410a4a5e7fbb09bc464d97d4`

只抽取 full-cue、single-speaker、HOST、interval 与 reviewed expect 完全相同的 cue
4 / 5 / 27 / 32，时长 780 / 1100 / 2140 / 840ms，总计 **4.860s**。七条 mixed cue
的子段边界在真值里明确是按字符近似，因此全部排除。cue 5 的 reviewed text 修正过，
manifest 分别绑定 worksheet text hash 与 reviewed text hash，不把文字差异误作声学边界漂移。

最终 manifest：

- remote `/tmp/hostocc-v2-20260811/results/reviewed-enrollment-2026-07-22.v3.json`
- file sha256 `d3eb98e8d6c52662d5d0e946d5e9a92867252c9a4966a309d4877fbb975da5d2`
- deterministic payload sha256
  `3a7ccfa1acac21746da6a5b3f51d28254f67f2798f03944f1aec3cbe689f0ed3`
- builder sha256 `00cbb8207072525491ef62a71f5a742415bcc83b870fa47d9b51866a3fa8bf7e`

它只有 4 条/4.86s，不满足建议的 6–12 条、60–120s；只作为额外 raw-score bank，
从未改 production profile，也不进入 v1 prediction。

### 8/8 同日开发样本（已抽取，但不进入本次评测特征）

| candidate | exact reviewed HOST 整 cue | 时长 | 排除 |
|---|---:|---:|---|
| `auto_200130_1323_1603` | 33 | 69.029s | 6 mixed、116 OTHER、2 条 <700ms |
| `auto_200130_1722_1792` | 11 | 17.960s | 26 OTHER |

合计 44 条、86.989s。两份 manifest 均绑定 exact media、text-final SRT、override、
decoded PCM 和 clip hash；但它们与 8/8 challenge 是同一场、同一批 cue，**只能作为
样本库或未来 prototype-selection 的开发输入，绝不能拿来做本次 holdout 或报准确率。**
而且现算法对多个 prototype 取 max，盲目把 44 条全塞进去会机械抬高 impostor 分；
下一步必须先做代表样本/consensus 设计，再去两个新场次盲测。

- 1323 manifest：remote `results/reviewed-enrollment-2026-08-08-1323.v1.json`，
  file sha256 `07165d845a99a9932743dc4afabe7c0b56f5cc9583d3228ca9fc66907a2ea4c9`
- 1722 manifest：remote `results/reviewed-enrollment-2026-08-08-1722.v1.json`，
  file sha256 `f72e99951ee4a128245b73b392466a47fa20b72f2292d7184a90b600d07a1298`

### 8/10 + 8/11 新 holdout source freeze（尚无 cue/预测/真值）

Pro consultation 之后，两个未进入既有生产处理或 speaker truth 的新场次被登记为
H1/H2。`free` 现场确认录制 idle、CloudFS 正常、全部 segment 有 FileClosed 与 adapter
finalization。隔离 source freezer 对 8/10 的 10 段和 8/11 的 5 段做了两次完整重读；15 个
当前 MP4 SHA 均匹配 adapter target hash。两次 deterministic payload 同为
`c4e27632a0c279747697e3168d2a91cab535967a5d186fcd3e0c5cb0ff284184`。

它们的状态严格只是 `SOURCE_FROZEN`：ASR、cue table、candidate prediction 和 human truth
全部未冻结/未开放。精确路径、FUSE duplicate-listing 诊断、两次文件 hash 与下一状态见
`locked-holdout-source-inventory.md`。不能把 source freeze 写成已通过 holdout。

### Pro 选择的下一代 score-only 候选

Pro 选择 session-stratified、duration-matched centroid--medoid consensus 加 OTHER veto；
每个时长层至少 3 个独立 HOST session、每场至少 3 条 audited HOST，OTHER 每层至少
12 条并确定性选 8 个 veto medoid。跨场使用严格多数 order statistic，而不是所有场次
minimum。OTHER 只能把 prospective HOST 降为 UNKNOWN，不能提升；真实 threshold 当前
固定为 `NULL`，所以该模块不会产生硬标签。

机器可执行 spec 在 `assets/lidousha/speaker_scmc_v0_spec.json`，纯 shadow 数学/泄漏合同在
`src/autoslice/speaker_scmc_shadow.py`。它没有 production caller，也没有修改现有 designated
strategy/config/predictions。完整咨询 binding 与 canary 见
`pro-consult-overnight-decision.md`。

## 四、8/8 人工真值与最终 v6 结果

reviewed clear truth 共 **188** 条：46 HOST + 142 OTHER；另有 6 mixed，3 条
machine-only cue 排除。短句 `<1500ms` 有 95/188（50.5%）：18 HOST + 77 OTHER。
因此旧的 blanket short abstention 理论最高覆盖也只有 91/188（48.4%）。

现有自动 speaker baseline 在 188 clear 上是：H→H 33、H→O 13、O→H 18、O→O 124；
正确 157/188（83.5%），但有 **18 次 false-host**，不能作为安全生产 fallback。

最终指定策略 `two_prototype_consensus_shadow`：

| 指标 | 最终值 | 开发门 |
|---|---:|---:|
| H→H / H→O / H→UNKNOWN | 3 / 6 / 37 | — |
| O→H / O→O / O→UNKNOWN | 1 / 73 / 68 | false-host 必须 0 |
| host precision | 0.7500 | 1.0000 |
| host recall | **0.0652** | ≥0.85 |
| classified coverage | **0.4415** | ≥0.85 |
| clear UNKNOWN share | **0.5585** | ≤0.15 |
| verified accuracy | 0.9157 | ≥0.95 |
| mixed hard-label | 0 | 0 |

结论：`development_gate_passes=false`，`production_promotion_authorized=false`；聚合 CLI
退出码 2 是预期 fail-closed，不是运行错误。

### 7/22 reviewed bank 的可分性诊断

以下 zero-FP recall 是“看过真值后把 threshold 放在最高 OTHER 分之上”的 oracle 上界，
只说明信息量，**没有阈值或部署权威**：

| target | max AUC / zero-FP recall | median | second-highest | dual-bank min-median |
|---|---:|---:|---:|---:|
| 1323–1603（35H/116O） | .588 / .000 | .701 / .086 | .667 / .057 | .719 / .086 |
| 1722–1792（11H/26O） | .720 / .182 | .822 / .545 | .769 / .364 | **.850 / .545** |

跨场 bank 确实增加了信号，尤其第二条的 AUC 到 .85；但长候选仍只有最高 8.6% 的
零误认召回，短候选也只有 54.5%，远低于 85%。固定 0.50 threshold 下 median 两条的
host recall 都是 0。数据证实不能靠单纯抬/降阈值解决。

## 五、最终收据与复现边界

预期的 remote 开发写入都在 `/tmp/hostocc-v2-20260811`。另有一次早期 CLI `--help`
检查意外创建了 `/tmp/host-occupancy-help.txt`；它不含生产状态、未被本次结果读取，也没有
在未经授权的情况下擅自删除。最终 runner sha256：
`61ade36485ffaeca6f573bfa68db25855a877b5d9f44a08dd2a1e479321854a6`。

| artifact | remote / local 临时路径 | file sha256 |
|---|---|---|
| cue shadow 1323 | remote `results/auto_200130_1323_1603.cue-shadow.v6.json` | `416f56bd0b6c40088b293551d7771b081683ed9c2b397972f4166c8ea864f6d4` |
| cue shadow 1722 | remote `results/auto_200130_1722_1792.cue-shadow.v6.json` | `109c6e7091bd855df0a6c8f5ef3648d5c15ed47ebda12e42a318415839b3fc03` |
| challenge 1323 | local `/tmp/auto_200130_1323_1603.cue-challenge.v6.json` | `8ea8abef51b324a23079e52a6483b46baef7da2015ca952110301e4a1e9d3a2a` |
| challenge 1722 | local `/tmp/auto_200130_1722_1792.cue-challenge.v6.json` | `7befb11cdc7c0853d0c7843e84c6954bc107792ca9e891d3b43842a45d8f75ef` |
| aggregate | local `/tmp/auto_200130_8-8.aggregate.v6.json` | `44caac19cd6a39fd68bdf41f060125bddf493967b6c3750af9fa0508114c6ab9` |

v6 的所有 strategy metrics 与重构前 v4 用 `diff -u` 逐项一致；重构只把 439 行
`run_shadow()` 拆成有界的 cue scoring 与 session-anchor 函数，以通过 architecture gate。

验收：

- 定向测试：145 passed（随后新增 8/8 样本合同并重构后再次通过相关 26 tests）；
- overnight 新增 source-freeze + SCMC 合同：24 passed；
- 本轮 changed-file `ruff check`、`py_compile`、`git diff --check`：通过；全仓
  `ruff check .` 仍报告 42 条既存 lint finding（均不在本轮文件），未借本任务扩 scope 修理；
- 整库最终：**4294 passed，2 个第三方 DeprecationWarning，0 failed，75.53s**。

## 六、生产接线、优先级与条件 ETA

目前 `host_occupancy`、`centrality_policy`、`centrality_receipt` 都没有生产 caller；
runner 仍先跑 legacy `prioritize(v1)`，再做 speaker routing。现在接线会让 provisional 声纹
影响生产，违反本收据自己的门，因此本轮刻意不接。

后续唯一允许的顺序：

1. **P0 准确率（最高）**：8/10 + 8/11 的 raw source 已冻结，但还需先构建 hash-bound
   pre-label ASR/cue/prediction 包，再由人工逐 cue/window/segment 标真值；同时从至少三个
   不同直播/语气收集干净 reviewed HOST bank。先修代表样本/consensus、
   overlap/mixed abstention，再要求每场 false-host=0、host recall/clear coverage ≥85%、
   clear UNKNOWN≤15%、verified accuracy≥95%。
2. **P0 production choke point**：通过后才在 session seal 之后、每一个 `produce_batch`
   之前生成并验证 hash-bound receipt；`talk_lane.produce_talk()` 再独立验证 freshness。
   exact/pinned/backfill 不能绕过。UNKNOWN 先升级已有 speaker-finalizer 的分析核心，仍存疑
   才进入人工队列。
3. **P1 shadow canary / deployment**：部署代码但保持 selection influence=false，按新场次
   对比 receipt；验收后才切换 gate。任何切换仍需 Ivan 明确 deploy/enable 授权；upload
   是另一条独立授权链。

时间口径：本 P0 **代码/证据成品现在已 review-ready**。可部署的声纹成品没有诚实的固定
日期，因为两个新场次只有 source freeze，pre-label 包与人工真值仍不存在；这些材料一旦齐全，算法校准 + 盲测预计
需要 1 个工作日，production 接线 + shadow canary 再需约 0.5–1 个工作日。若任一 holdout
没过门，日期自动顺延，不能用调低门槛换“按时上线”。
