# 2026-08-07 说话人二分策略跨场次评估：equal-quality 对谈是否验证过？

> 复核范围：`src/autoslice/speaker_host_evidence.py`（默认连线/HOST 需硬证据/语义只佐证不独立定案，
> Ivan 2026-08-07 裁定）在**游戏语音场次之外**——尤其是双人/多人对谈、音质相近、响度接近的
> 连线场次——是否有真实标注数据支持。read-mostly 评估，未改动生产行为，仅落盘本文档 +
> 交叉核对结论；未新增测试 fixture（现有 `tests/lidousha/fixtures/auto_203735_555_680_*` 已覆盖
> 唯一可回放数据集，见下）。

## 结论先行

**该策略目前只在一个游戏语音场次（压缩嘉宾音频）上有可回放的真值验证；对 equal-quality
对谈/连线场次是零标注、零证据、未评估（unproven，not evaluable offline）。** 现有唯一确认的
equal-quality 连线场次（2026-07-22 南町联动）在生产中是以 `speaker_mode=uniform_host` 整场跳过
声纹分离产出的，没有任何声纹分数、CAM++ margin 或人工说话人标签留存——无法离线回放，也无法
做 label-agreement 比较。这不是本次评估遗漏，是数据本身不存在。

生产暴露面：`free` 当前 crontab（`crontab -l`，2026-08-07 观察）对每次 runner 调用无条件设置
`AUTOSLICE_SPEAKER_MODE=auto`，与 `docs/pipeline/40-subtitle-text.md` 记载的 "talk 成品
speaker_mode=required" 不一致（后者应为较早文档状态，未在本次任务范围内核实谁是权威、也未改
动任何一方）。按 `auto` 模式代码路径，下一场真实连线会直接进入这条从未在 equal-quality 音频上
验证过的判定链。

## 1. 数据清单与场次分类

| 场次/候选 | 日期 | 类型 | 标注/证据现状 | 是否可回放新策略 |
|---|---|---|---|---|
| `auto_203735_555_680`（真善美） | 2026-08-07 | 游戏语音，单一嘉宾，声道压缩 | Ivan 61-cue 逐句裁决（`tests/lidousha/fixtures/ivan_truth_diff_20260807.json`）+ 真实 CAM++ margin/threshold 抽取（`..._machine_decisions_20260807.json`）+ 响度研究（`..._loudness_study_20260807.json`） | **是**——已有专用回放测试 `tests/lidousha/test_speaker_host_evidence.py`，本次重跑确认见下 |
| `promo_210025_643_801`、`promo_193036_367_476`、`promo_203027_314_479`、`auto_193036_487_758` 等 4 段（2026-07-09 联动，嘉宾礼墨/Sumi + 安晚/Awa） | 2026-07-09 | 游戏语音（哑人/聋人/盲人玩法），多嘉宾轮换 | Phase1 120-cue 人耳盲听 + 第二阶段 A/B 54-cue holdout（`docs/reviews/2026-07-12-speaker-phase1-diagnostic.md`、`2026-07-12-speaker-binary-v2-blocker.md`）；`assets/lidousha/speaker_overrides/promo_210025_643_801.speaker.v1.json`、`promo_193036_367_476.speaker.v1.json` 是 Ivan 逐轮人工核定的最终标签（已发布口径） | **否**——原始 CAM++/margin 分数未在仓库或 `free:/opt/bilive/autoslice/out/2026-07-09/` 下持久化（该场发布走的是文本/说话人 override 直接落地，不是 finalizer 打分产物存档）；只能引用 2026-07-12 诊断文档的聚合统计，不能对 `speaker_host_evidence.py` 的具体阈值/band 做逐 cue 机械回放 |
| `auto_162645_394_507` | 未知（override 记 2026-07-18 review） | **独播**（全部 23 cue 均为李豆沙，上游已剔除被观看视频原声段） | `speaker_overrides/auto_162645_394_507.speaker.v1.json` | 不适用——非对谈场次，不含嘉宾对比信号 |
| 2026-07-22 南町联动（`session_relation_ledger.v1.json` 唯一 `CONFIRMED LIVE_COLLABORATION`，5 条已发布切片，如「弹幕追问李豆沙为何请南町吃火锅，她从」100 cue） | 2026-07-22 | **equal-quality 对谈**（双人连线，同一直播流录制，非游戏语音压缩链路） | **无**——5 个 `.record.json` 全部 `speaker_mode: uniform_host`，`speaker_finalization: null`；没有跑声纹分离，没有 CAM++ margin，没有人工说话人标签 | **不可评估**——数据不存在 |

关键区分：2026-07-09 和 2026-08-07 两批唯一带证据的场次都是**游戏语音**（嘉宾经语音聊天/游戏
客户端压缩链路，与李豆沙直播麦克风的编码/响度特征天然不同）。2026-07-22 是本仓库里唯一一个
被独立确认的 equal-quality 连线场次，且恰好是零标注的那一个。`docs/reviews/2026-07-12-speaker-binary-v2-blocker.md`
的「跨 session 数据缺口」一节在一个月前就指出：当时全部标注只映射到 2026-07-09 单场、同一对
嘉宾（礼墨+安晚），可辩护发布线需要 6 场真实联动（另需 5 场，覆盖不同嘉宾/麦克风条件）。这个
缺口今天原样还在——新策略换了一次校准场次（8/7 真善美替代了 7/09 批次作为唯一活跃锚点），但
仍然是单场，而且仍然是游戏语音，equal-quality 场次的缺口没有被这次策略调整覆盖到。

## 2. 回放方法与结果

### 2a. `auto_203735_555_680`（唯一可机械回放场次）

本地重跑 `tests/lidousha/test_speaker_host_evidence.py`（Python 3.14，2026-08-07，工作树
`/Users/ivan/Project/vtuber-slice`，HEAD `84e3603`）：

```
$ python3 -m pytest tests/lidousha/test_speaker_host_evidence.py -q
9 passed, 2 warnings in 0.12s
```

该测试用真实抽取的 CAM++ margin/threshold（`auto_203735_555_680_machine_decisions_20260807.json`）
逐 cue 回放 `speaker_host_evidence.resolve_ambiguous_cue_speaker`：

| 指标 | 数值 |
|---|---|
| 非 mixed cue 总数 | 55（另 6 条 mixed 走 `mixed_cue_speaker` 单独路径） |
| false-host（判 HOST 但真值 GUEST） | **0**（硬性要求，测试断言） |
| false-guest（判 GUEST 但真值 HOST） | 1（cue 40，margin 落在语义佐证带内但唯一可用的历史语义投票是 GUEST，策略未在弱于该投票的依据上翻转） |
| correct | 54/55 |
| mixed cue（6 条）判定 | 全部 GUEST（v1 无真实子 cue 音频分窗，退化为保守默认） |
| 响度硬通过 lane | 全场零触发（host/guest 响度中位数几乎重叠：host −22.95dB vs guest −22.0dB，guest 中位数反而更响，`loudness_study` 数据） |

**新旧策略对比（同一场次，逐 cue 机械计算，非引用）**：对 `..._machine_decisions_20260807.json`
里持久化的**旧策略机器原始判定**（未经人工修正的 `speaker` 字段）与 `ivan_truth_diff_20260807.json`
真值逐 cue 比对（55 条非 mixed cue，脚本对比未落盘为测试，纯分析）。核实：这 10 条旧策略
false-host cue 全部落在 `speaker_overrides/auto_203735_555_680.speaker.v1.json` 的 20 条覆盖范围
内——即它们是 Ivan 8/7 那轮 61-cue 逐句裁决当场发现并手工纠正过的，这份 override 文档本身就是
`speaker_host_evidence.py` 的校准来源；换句话说，下表左列反映的是"如果没有这次人工修正，旧策略
原本会判成什么"，不是"公开发布的字幕曾经写错"：

| | 旧策略（机器原始判定，未经本轮人工修正） | 新策略（`speaker_host_evidence.py` 回放） |
|---|---|---|
| false-host（判 HOST，真值 GUEST） | **10**（cue 4/5/6/7/9/11/12/14/33/47） | **0** |
| false-guest（判 GUEST，真值 HOST） | 4（cue 40/46/48/49） | 1（cue 40） |
| 正确 | 41/55 = 74.5% | 54/55 = 98.2% |

新策略把 false-host 从 10 降到 0，正是 8/7 裁定要解决的问题（旧策略里 10 条 cue 被语义投票误判
成李豆沙，而这些 cue 的 CAM++ margin 实际上离阈值很远，属于纯粹的语义误决策）；这也印证新代码
策略的目的就是让这类修正从"靠 Ivan 手工逐条 override"变成"机器默认就对"。这是新策略唯一
一次端到端的真值验证，方向符合 Ivan 的不对称裁定（宁可漏判连线不误判本人）。**但这场是游戏
语音**：嘉宾音频经语音聊天压缩，与直播麦克风声道在编码特征上天然可分（按场次形态推断，未逐条
核实压缩链路）——这正是下一节要指出的、equal-quality 场次不具备的分离信号来源。这组"旧策略
10 false-host → 新策略 0"的改善，本身就是在游戏语音这一种音频条件下测得的，不能外推到
equal-quality 场次说明同等改善会发生。

### 2b. 2026-07-09 四段联动（label-agreement only，非逐 cue 机械回放）

原始 CAM++ margin 未持久化，无法对 `speaker_host_evidence.py` 的具体 `threshold`/`band`/
`host_semantic_corroboration_margin_below_threshold` 做逐 cue 回放。只能引用 2026-07-12 诊断
文档记录的、同一批 174 cue（120 Phase1 + 54 第二阶段 holdout）人耳真值上、**当时的生产管线**
（anchor-based CAM++ 声学核心 + whole-clip context，是今天 `speaker_finalizer.py` 的直接前身，
决策层后来才演进为本次 8/7 的非对称策略）的聚合表现：

| 子集 | 声学（CAM++ 直接）准确率 | 纯文本 whole-clip context 准确率 |
|---|---|---|
| Phase1 99 条清晰 cue（当时生产管线） | 46/50 = 92% | 29/49 = 59.18% |

另有同批数据上一个隔离开发候选 `direct-v2`（未部署，仅评估用）的数字，不与上表混用：Phase1+R1
合计 129/146 = 88.36%；第二阶段 holdout `42/52 = 80.77%`，后验可分性 AUC `0.9256`（该 AUC 来自
一次事后诊断，不是训练用/生产用阈值，文档明确禁止回用）。

上表（当时生产管线的数字）是 Ivan「声纹 + 语义是承重技术」判断在游戏语音场次上的独立支持证据，
且早于本次 8/7 裁定就已经确立了「文本/LLM 不应拥有说话人身份决定权」的结论——今天的
`speaker_host_evidence.py` 非对称策略正是沿着这个结论往前走的下一步。但它仍然是游戏语音数据，
不构成 equal-quality 场次的证据。

### 2c. 2026-07-22 南町联动（不可评估）

5 个已发布产物的 `.record.json` 均写 `"speaker_mode": "uniform_host"`、
`"speaker_finalization": null`，即整场未经过声纹判定，全部 cue 无条件标注为李豆沙。没有
CAM++ margin、没有人工说话人真值、没有语义投票记录可供比较。**唯一诚实的结论是"未评估"，不是
"通过"或"失败"。**

## 3. 架构风险点（equal-quality 场次特有）

读 `src/autoslice/speaker_finalizer.py`（`_prepare_campplus_anchor_state` / 打分主循环）确认
以下判定链完全依赖游戏语音场次才成立的分离前提，equal-quality 场次未验证：

1. **host anchor 种子选取（`host_session_seed_min = 0.68`，`voiceprint_profile.v1.json`
   `talk_speaker_policy`）**：当没有 `source_session_anchor_path` 时（clip-scope，多数场次的
   默认路径），种子锚点是从**当前 clip 自身**挑选——对每个 cue 计算与已登记参考声纹
   (`references`) 的中位相似度，取 ≥0.68 且排名最高的最多 4 条作为「host 声纹样本」。这一步在
   游戏语音场次里几乎不会误选嘉宾（嘉宾音频经压缩链路，与登记参考声纹的相似度天然被压低）；
   但在 equal-quality 场次里，如果嘉宾恰好音色接近或录制链路一致（同一直播混音流），嘉宾 cue
   有可能意外越过 0.68 门槛混入种子——一旦发生，是**系统性误判 HOST 方向**（Ivan 明确禁止的
   方向），因为后续每条 cue 的 margin 都相对这个被污染的 host bank 计算。这条路径在任何
   equal-quality 数据上都没有被测试过。
2. **guest bank 与阈值都是同 clip 自举的**：guest 候选（未入选 host 锚点的 cue）按互相相似度聚
   成最多 2 类 guest cluster；每条 cue 的 `margin = host_score − guest_score`；`threshold` 由
   `_two_means(margins)` 对**本场次自身**margin 分布做二均值分割得出——不是跨场次固定阈值。
   游戏语音场次天然双峰（host 高分紧凑簇 + guest 低分紧凑簇），两均值分割稳定；equal-quality
   场次若 margin 分布更连续（无天然双峰间隙），`_two_means` 落点可能不稳定，直接牵动
   `acoustic_hard_pass` 的 `band` 判定和下游语义佐证窗口的宽窄。没有 equal-quality margin 分布
   样本可验证这个假设成不成立。
3. **响度 lane 已在游戏语音场次上证伪，equal-quality 场次没有更好的备选**：`speaker_host_evidence.py`
   的响度硬通过（`host_loudness_required_margin_db = 9.0`）在唯一一次实测（`auto_203735_555_680`）
   上零触发——「近麦更响」假设不成立，guest 中位数反而更响。equal-quality 场次通常混音更均一，
   这条 lane 大概率同样不可用，即声学证据缺失时**没有第二道硬证据来源**，全靠语义窄带佐证——
   而语义佐证在游戏语音场次上独立准确率只有 59%（2c 节数据），且规则明确只允许它在临界带内
   佐证、不能独立定案。
4. **Mixed/overlap 判定（`mixed_cue_speaker`）v1 无真实子 cue 音频分窗**，任何声学证据不一致的
   cue 一律退化为连线——equal-quality 对谈的抢话/重叠频率通常高于游戏语音（后者受限于连麦延迟，
   打断天然少），该退化路径在对谈场次里触发频率未知，也未评估。

## 4. 我无法评估的部分（明确列出）

- 无法对 equal-quality 场次做任何逐 cue 机械回放或 label-agreement 比较——没有一条 equal-quality
  cue 留有声纹分数或人工说话人标签。
- 无法确认 `_two_means` 阈值分割在 equal-quality margin 分布下是否仍然稳定——需要真实
  CAM++ 打分才能观察，本次评估未运行 CAM++（超出 read-mostly 范围，也涉及生产主机算力）。
- 无法核实 `AUTOSLICE_SPEAKER_MODE=auto`（当前 crontab 观察值）与 `docs/pipeline/40-subtitle-text.md`
  记载的 `speaker_mode=required` 之间哪个是当前权威口径、二者何时出现分歧——只如实记录观察到
  的状态，未做进一步核实或改动。
- 2026-07-09 场次原始 CAM++ margin 未持久化，只能用 2026-07-12 诊断文档的聚合统计佐证，不能
  重构出该场次在新策略确切阈值/band 下逐 cue 会怎么判。

## 5. 建议的下一步验证（不改生产阈值，仅建议）

1. **对一个真实 equal-quality 场次做一次离线 CAM++ 打分 + Ivan 逐句人工裁决**，规模比照
   8/7 61-cue 练习。建议候选：2026-07-22 南町联动已发布的
   「弹幕追问李豆沙为何请南町吃火锅，她从」（100 cue）。`session_relation_ledger.v1.json` 锚定
   的 `sha256:0eb2778d...8989a` 是**该场直播录制源**（官方回放）的 sha，不是这条切片本身的媒体
   哈希；切片级别的 `video_sha256`/`ass_sha256`/`subtitle_sha256` 等在本地
   `lidousha/2026-07-22-full-rerun-review/*.record.json` 的 `artifact_hashes` 里逐项绑定。只需
   离线跑 CAM++ 打分（不触发上传、不改当前已发布产物），产出 margin/threshold，然后按
   `tests/lidousha/test_speaker_host_evidence.py` 的同一套回放方法验证 false-host 是否仍为 0。
2. 打分时同时记录 `_two_means` 的 `low_center`/`high_center`/`threshold` 与 margin 分布形态，
   直接检验第 3 节风险点 1、2 是否成立（是否有嘉宾 cue 混入种子锚点、双峰间隙是否消失）。
3. 若该场次 false-host 非零，下一步是给 host 种子锚点加一层跨 equal-quality 场次的稳定性门槛
   （例如要求种子分数显著高于 0.68 而非贴线通过），而不是继续依赖单场校准。这是留给数据出来
   之后的决策，本文档不预判。
4. 在拿到至少一场 equal-quality 真值前，`docs/pipeline/40-subtitle-text.md` 里"新策略"的适用
   范围声明应明确标注"已验证：游戏语音单场；equal-quality 对谈：未验证"，避免读者把 8/7 校准
   误读成覆盖所有 talk 场次。

## 复核信息

- 分支/工作树：`claude/session-live-context`，`/Users/ivan/Project/vtuber-slice`，base SHA `84e3603ff29bb08cc9128cd4506705031f7b4779`（评估期间未变更）。
- 本次运行的唯一命令：`python3 -m pytest tests/lidousha/test_speaker_host_evidence.py -q` → `9 passed`（本地 Mac，Python 3.14；未在 `free` 上跑，遵循 free 无 pytest 的既有记录）。
- 未修改任何生产代码、阈值、`voiceprint_profile.v1.json`、crontab 或已发布产物；未新增测试 fixture（现有 fixture 已完整覆盖唯一可回放场次，无需重复造数据）。
