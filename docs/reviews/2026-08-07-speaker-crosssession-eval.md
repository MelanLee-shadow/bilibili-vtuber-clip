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

## 附录（2026-08-08）：第 5.1 节验证——2026-07-22 南町联动离线 CAM++ 打分首跑

按第 5 节建议 1，对 2026-07-22 南町联动唯一已发布 equal-quality 候选跑了一次离线 CAM++ 二分打分
（`src.autoslice.producer_speaker.run_speaker_finalizer`，`host="localhost"`，`free` 上部署 commit
`84e3603`，只写 `/opt/bilive/autoslice/tmp/`，未碰生产 `out/`/`lidousha/` 交付物、未改 crontab、未上传）。

**候选**：`auto_200511_61_138`（发布标题「李豆沙解释为什么请南町吃火锅，从"付出劳动"一路改口，最后
承认她是最最最最喜欢的人」，77.2s，40 cue，`replacement_recuts/*.recut.mp4`+`.recut.srt`）——即第 5 节
建议 1 点名的候选。`source_media_sha256=f5662ff2…5cbc7`，`text_final_srt_sha256=81d32633…6dfed4`，
`profile_sha256=663eee98…85ad1d`（`voiceprint_profile.v1.json`，未变更）。

**finalizer 状态**：`READY`，`production_ready=true`——**未触发 `SPEAKER_REVIEW_REQUIRED`**。这是
equal-quality 场次第一次跑通整条判定链并产出可读的 margin/decision 逐 cue 记录。

### 标签分布与判定来源

| 指标 | 数值 |
|---|---|
| 李豆沙(HOST) | 26/40 = 65% |
| 连线(GUEST) | 14/40 = 35% |
| `campp_audio`（非临界带，声学直接判定） | 23/40 = 57.5% |
| `campp_semantic_corroborated`（临界带内，语义佐证判 HOST） | 8/40 = 20% |
| `whole_clip_context_guest_confirmed`（临界带内，语义投票判 GUEST） | 5/40 = 12.5% |
| `guest_default_ambiguity`（临界带内，语义未能佐证 HOST，按默认判 GUEST） | 4/40 = 10% |
| `loudness_hard`（响度硬通过） | **0**——响度参数未接入 `speaker_finalizer.py` 的 `resolve_ambiguous_labels` 调用点（未传 `cue_loudness_db`），这条 lane 在当前 finalizer 生产路径里根本没有被调用，不只是"零触发"，是**未接线**。与第 3 节风险点 3 的怀疑方向一致，但结论更强。 |

### 临界带（ambiguity band）占比——关键新发现

区分两种"进入临界带"的原因：margin 落在 `threshold±band` 内（声学本身不确定），vs cue 时长
`<short_cue_ms(1500ms)` 被无条件强制视为临界带（不看 margin 多确定）。

| | 2026-07-22（equal-quality，本次） | 2026-08-07（游戏语音，`auto_203735_555_680`） |
|---|---|---|
| 纯 margin 落在 `threshold±band` 内 | 7/40 = **17.5%** | 11/61 = **18.0%** |
| 含短句强制（`<1.5s`）后的临界带总量 | 17/40 = **42.5%**（其中 10 条纯因短句被强制，margin 本身已在带外） | 未知——8/7 fixture 未持久化 cue 时长，无法同口径复算 |

**纯 margin 意义上的临界带占比几乎相同**（17.5% vs 18.0%），不支持"equal-quality margin 分布更连续、
`_two_means` 更不稳定"这一假设——至少这一条候选上没有观察到。但**短句强制这一项让实际需要语义
兜底的 cue 几乎翻倍到 42.5%**，且短句强制的 10 条里语义投票只佐证成功 0 条（全部落在
`guest_default_ambiguity`/`whole_clip_context_guest_confirmed`，即全部按默认判 GUEST）——equal-quality
对谈里"哎""对对对""啊怎么了"这类 <1.5s 的应和/插话密度明显高于游戏语音场次，第 3 节风险点 4
（对谈抢话/重叠频率更高）在这里以"短句强制进临界带"的形式先一步体现出来，即使还没出现真正的
重叠 cue。

### Margin 分离度（cluster separation）

`_two_means` 得到 `low_center(guest)=-0.157`，`high_center(host)=+0.265`，`threshold=0.0538`——
双峰缺口依然存在，**不是退化成单峰连续分布**。按机器自身标签分组（非人工真值，仅自洽性）：

| | HOST median margin | GUEST median margin | 分离度(HOST−GUEST) |
|---|---|---|---|
| 2026-07-22（机器标签自洽，本次） | 0.2947 | −0.0961 | 0.3908 |
| 2026-08-07（Ivan 真值分组，`auto_203735_555_680`，55 条非 mixed cue） | 0.3704 | −0.0777 | 0.4481 |

分离度量级接近（0.39 vs 0.45），但**7/22 一列是机器标签自洽统计，不是真值验证**——不能说明
准确率，只能说明"如果 threshold 判对了，两组声学分数确实拉得开"。这正是第 4 节列出的缺口：
false-host/false-guest 仍然是 0/0（未知），要等 Ivan 用下方标注文件逐句核对之后才能算。

### 已交付的人工核对材料

- 标注底稿：`/Users/ivan/Project/vtuber-slice/lidousha/2026-07-22/auto_200511_61_138.speaker-eval.srt`
  （`lidousha/` 已 gitignore，未入库）——40 条 cue，每条附机器标签、`decision_source`、`margin`，
  文件头注明 Ivan 标记语法（A=李豆沙，B=非李豆沙，句内多标记=每个标记管辖到上一个标记为止，
  不标=认可机器当前标注）。
- 30s 双样式烧录对比（`free:/opt/bilive/autoslice/tmp/speaker-eval-20260722-auto_200511_61_138/`，
  未落生产/未上传）：`baseline_30s.mp4`（无说话人区分的当前 `uniform_host` 风格）+
  `speaker_30s.mp4`（`lidousha-speaker-sapphire-host-white-guest-v2`，HOST=细橙棕描边、GUEST=粗体
  深藏青描边）；两张同一时间点（cue4「哎，现在几点了？」，机器判 GUEST）截帧对比已本地核实
  （`baseline_frame.png` vs `speaker_frame.png`，裁剪对比确认描边粗细/颜色确有可辨差异）。

### 结论更新

equal-quality 场次的**纯声学可分性**（margin-only 临界带占比、双峰缺口）在这一条候选上看起来
和游戏语音场次相当，没有观察到第 3 节风险点 2 担心的分布退化；**但真正需要语义兜底的 cue 比例
因短句强制机制而显著更高（42.5% vs margin-only 的 17.5%），且这部分短句里语义投票目前 0 命中
HOST**——equal-quality 对谈的高频短插话是这条判定链目前最大的未验证暴露面，比原文第 3 节笼统
描述的"抢话/重叠"更具体、更早出现（不需要真正重叠才触发，只需要说话人快速换人接话）。**是否
"验证可行"取决于 Ivan 核对标注底稿后 false-host 是否为 0**——如果短句强制之后判成 GUEST 的 10 条
在真值里真的都是南町/连线，说明短句默认 GUEST 这条保守规则在 equal-quality 场次下仍然安全；如果
其中有本人的短接话被误判成连线，则短句强制阈值本身（1500ms）在 equal-quality 场次可能需要重新
校准，而不是像 8/7 校准时那样只调语义佐证 floor。本文档到此为止不预判，留给标注结果。

## 附录（2026-08-08）：equal-quality 场次第一次真值核对结果——false-host 不是 0

Ivan 已逐句核对标注底稿（`lidousha/2026-07-22/auto_200511_61_138.speaker-eval.srt`，40 cue，A/B
标记语法）。诊断脚本与完整逐 cue 记录见
`/private/tmp/claude-501/-Users-ivan-Project-vtuber-slice/5cbe14f2-2623-4f3f-8308-060346f7e8ab/scratchpad/ivan_722_truth_diff.json`
（schema `ivan-speaker-truth-diff.v1`，未入库；落地产物见"已交付真值"一节）。

### 标签准确率（回答第 259-263 行悬而未决的问题）

| 指标 | 数值 |
|---|---|
| 总 cue 数 | 40 |
| 未标注（机器判定确认正确） | 29/40 = 72.5% |
| 整句重判（machine GUEST → truth HOST，false-guest） | **4**（cue 4/5/27/32），**0** 整句 false-host |
| 混合 cue（句内换人，仅局部重判） | **7**（cue 9/12/20/23/25/36/40） |
| 混合 cue 内 false-host 子段 | **4**（cue 9/12/20/40 各一处，均为句首或句尾的短接话/语气词，从未整句误判） |
| 混合 cue 内 false-guest 子段 | **3**（cue 23/25/36） |
| **false-host 合计（含子段）** | **4** —— **不是 0** |
| **false-guest 合计（含子段）** | **7**（4 整句 + 3 子段） |

**结论：整句层面 false-host 仍是 0**（与 8/7 游戏语音场次、以及 Ivan 不对称裁定的方向一致：机器
从未把一整句连线误判成本人），**但子段层面 false-host 不是 0**——4 处本人的极短接话/语气词
（"哦"cue9、"啊哈哈"cue12、"行"cue20、"行"cue40）被机器判成连线。这 4 处全部落在混合 cue 内部，
不是被"短句强制进临界带"的独立整句（对照 §2c 表格：cue4/5/27/32 才是被短句强制/margin判定误判
成连线的整句，且全部方向是 false-guest 不是 false-host）——即真正验证成立的是"整句层面机器保守
偏 GUEST"，但"句内快速换人接话"这个第 3 节风险点 4 预判的暴露面在这条候选上确实兑现了：机器
把说话人切换点判晚了半句（把李豆沙刚说完让给对方的最后一个字/词仍算给自己），而不是把整句
张冠李戴。第 4 节"我无法评估的部分"里"equal-quality 对谈的抢话/重叠频率"这一条，现在有了第一个
真实样本：7/40=17.5% 的 cue 存在句内换人，其中 4 处（10%）产生了子段级 false-host。

### 文本纠错（proper-noun 类 vs 一般听误）

3 处文本与机器原文不同（详见下表），仅 1 处是专名类：

| cue | 机器原文 | 真值 | 类别 | 登记状态 | 今天的管线会不会修对 |
|---|---|---|---|---|---|
| 5 | 我还没认识 | 我还没看时间 | (d) 非专名听误 | 不适用 | 无对应门；纯语义/语音误听，无词表可挂，仅作听误案例记录，不入词表 |
| 23 | 不是，主要是火锅了 | 不是，主要是想吃火锅了（漏字补全） | (d) 非专名听误 | 不适用 | 无对应门；ASR 吞字类，无专名可挂，仅作听误案例记录 |
| 31 | 终于和大人见面了 | 终于和大N见面了 | (c) 专名类，误听方向未登记 | 实体已登记（`assets/lidousha/glossary.txt` 南町/大N 词条 + `assets/lidousha/psplive_roster_sources.v1.json` `南町Nightin`→别名`大N老师`），但"大人"这个具体误听方向此前不在任一登记表 | **本次已修**：`glossary.txt` 南町词条误听高发列表新增"大人"，方向单向注明来源（本轮裁决）。修之前：即使实体已注册，逐 mention 音频仲裁（词表原文"每个 mention 分别凭本句音频与局部上下文判断"）不会自动纠正未登记的具体误听面，所以"大N"registered 不等于这一句会被修对——同一切片其余 5 处"大N"都听对了、仅这一处听岔，说明这是逐句声学问题不是实体未知问题，属于登记 confusables 列表能提高候选优先级、但最终仍需逐句音频裁决的情形。修之后：下次同类"大人"误听至少会进入候选提示。 |

`entity_confusables.json`（repo）逐字核对不含南町/大N 任何条目；free 运行时状态三个文件逐字核对
（`psplive_roster.json`、`community_names.json`、`streamer_registry.json`，均 `grep`/Python 正则实测，
非推断）：后两者各命中若干"南町"/"大N老师"条目（登记的是别名，见下），但**没有任何一个文件出现过
"大人"这个具体误听面**——`glossary.txt` 是该场唯一登记来源，本轮才补上。

### 已交付真值（不触发任何上传/edit-replace）

- 复核字幕基线：`assets/lidousha/reviewed_subtitle_baselines/auto_200511_61_138.reviewed.srt` +
  `auto_200511_61_138.subtitle-baseline.v1.json`（与 `auto_203735_555_680` 同一 `subtitle-redelivery-baseline.v2`
  格式；source binding 公式与该 precedent 一致：`source_pieces.start_ms(51770) + boundary_audit.final_start_ms(9770)/final_end_ms(86970)`
  = `absolute_source_start_ms=61540` / `absolute_source_end_ms=138740`，跨度 77200ms 与 `duration_ms` 精确相等；
  `recording_basename=22966160_20260722-20-05-11.mp4`）。
- 说话人 override：`assets/lidousha/speaker_overrides/auto_200511_61_138.speaker.v1.json`（与
  `auto_203735_555_680.speaker.v1.json` 同一 schema 与语义，11 条 override：4 整句重判 + 7 混合 cue 双段；
  `expect.text` 是绑定 SRT 的机器原文逐字，真值文本只落在 `segments[].text`，与 precedent 一致，不与 truth 混写）。
- `assets/lidousha/glossary.txt` 南町/大N 词条新增"大人"误听方向（方向单向，非强制替换；已用
  `scripts.lidousha_glossary_terms.load_glossary_terms` 实跑验证"大人"未进入 `mishear_blacklist`
  或 expected-value 机械替换车道——只作为该条目既有的、`canon` 列表里已长期存在的"误听高发"提示词一部分，
  这是该条目的既有 pre-existing 解析特征，安晚/大安/打完老师/大恩老师/大卫老师/大黄老师同样如此，
  不是本次改动引入的新行为）。

**`candidate_id=auto_200511_61_138` 在 `assets/lidousha/publication_registry.v1.json` 中未找到精确匹配
条目，free 2026-07-22 runner state 里其 `status=review_ready`（非 `published`）**——但内容大概率已经
发布，只是换了一个 candidate_id：同日另一候选 `auto_193450_1863_2056`（同一录制会话被拆成两段录制文件，
分别以 `22966160_20260722-19-34-50.mp4` 与 `22966160_20260722-20-05-11.mp4` 命名）已发布为
**BV1xgg462Env**（`assets/lidousha/publication_registry.v1.json` 记录），其发布标题「弹幕追问李豆沙为何请
南町吃火锅，从"付出劳动"嘴硬到"最最喜欢"，刚认识就互相霸凌」与本候选主题完全一致；把两条候选各自的
`recording_basename` 时间戳与 `absolute_source_start/end_ms` 换算成墙钟时间：`auto_193450_1863_2056`
覆盖 20:05:53.55–20:09:06.08，`auto_200511_61_138` 落在 20:06:12.54–20:07:29.74，后者完全落在前者窗口
内。**这是强证据但不是确认**——是否真的是同一段内容被两条不同 pipeline 跑法（本候选走
`speaker_mode=uniform_host` 的常规产线，`auto_193450_1863_2056` 走的是另一条 `full-rerun-v8` 恢复线）
各自切出、`auto_193450_1863_2056` 是否完整覆盖了本候选的南町吃火锅片段、以及是否需要把本文档的 40-cue
真值套用到 BV1xgg462Env 做 edit-replace，**都是 Ivan 的裁定，本次不预判、不触发任何上传或换源操作**。
