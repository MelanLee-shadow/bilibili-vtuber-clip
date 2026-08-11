# 2026-08-10 Claude 连续对话接管审计

## 结论先行

截至 2026-08-10 21:29 EDT（2026-08-11 01:29Z），这几天真正阻止流水线恢复的只剩
一个 P0：**候选进入选席前，必须先得到 hash-bound 的主播归属证据，再用带
`[李豆沙] / [其他] / [存疑]` 标签的字幕重评 centrality，最后重排。**

Claude 的 `tmp-host-occupancy` worker 已经做出一个有价值的 CAM++ 窗口估计器，并在
真实四候选上识别出两条“主要发言人不是李豆沙”的片段；但 commit `047f63c` **没有接入
runner，也没有重算 centrality 或重排**，而且漏掉了 Pro 已明确要求的可信
`solo_source` 条件。因此它不能作为 P0 闭环部署，不能据此移除 `DISABLED`。

当前正确动作是：保持生产暂停；保留现有 23 件已部署修复；把 `047f63c` 降格为 shadow
scaffold；先裁定 centrality 从 v1 可补偿权重迁移成不可补偿前置条件的精确口径，然后完成
全链路接线、真实 challenge set、全量测试、部署，最后才恢复产出。

## 一、审计范围与证据边界

### 已覆盖

本审计逐条读取了本项目 Claude 原始 JSONL 中 2026-08-07 至 2026-08-11 的连续主会话、
中断续接会话和 host-occupancy worktree 子会话；主要 session id 为：

1. `5cbe14f2-2623-4f3f-8308-060346f7e8ab`
2. `d975757c-7f1f-430c-a71a-7db2e02fcb51`
3. `f95a556a-79e4-4756-92f8-4e0679d5966a`
4. `502e228c-6912-44a8-85be-cc8802b45241`
5. `a4ce1306-5b45-4820-a747-2d0f6f83dc38`
6. `769ba35d-3caf-4bd4-bbbd-f3693b04d8ab`
7. `7f7fdf3a-020f-440c-97a2-98f4b9e83594`
8. `83832107-68d3-40e3-bf7b-093f1e8d398b`

同时现场核对了主仓、host-occupancy worktree、free 部署位、cron、runner lock、state、
录播目录、出版登记、Bilibili 官方 API 和 7 个近期公开稿件。

### 未覆盖/不能声称

- Chronicle recorder 当时未运行，PID 文件不存在，因此本审计没有把屏幕历史缓存当证据。
- 本报告能证明的是 **vtuber-slice 项目目录下这组连续 Claude 会话**；不能证明 Ivan 在另一台
  机器、另一个 Claude 项目目录或未落盘会话里没有说过额外内容。
- 对话里的 `RC=0`、`deployed`、`review_ready`、worker 自述都没有直接当现状；现状均另做
  live readback。

## 二、Ivan 明确说出的任务与当前归宿

### A. 选片、边界与 centrality

| 任务/裁定 | 对话里的最终口径 | 当前状态 |
|---|---|---|
| 8/7 游戏场最多 10 条 | 第 6 席起分数门为 85；不是凑数目标 | 已入 quota authority；仍受真实分数/门禁约束 |
| 8/8 配额 | 事件场 15 条；同日杂谈另有 5 条 | 已实现为互斥 scope |
| 8/7 四条 tier-1 | Ivan 盲审：前三条均不应发；`578_654` 最值得发、应 90+ | 真值已归档；尚未成为完整 review package |
| 好片边界 | `578_654` 应延长到故事结束；Ivan 确认约 734.2s 收尾 | B1 已部署；新路径实算 734230ms |
| 主角规则 | “主角都不是李豆沙基本就是低分判定，不用看别的” | **尚未在选席链路落地** |
| centrality 判断 | 必须先有说话人归属；存疑直接人工 | **P0 未闭环** |
| N | 争席候选先取 10，不够再补 | 数据结构已写；生产接线未写 |
| metric v2 | 语义路径 OR；v1/v2 双口径；不能拿临时常量直接发版 | shadow 已部署，生产 caller 为 0 |

### B. 说话人、字幕与术语

| 任务/裁定 | 当前状态 |
|---|---|
| 非李豆沙说话使用白色字幕；身份必须以视觉/声纹为证，语义不能单独决定 | 已进入 producer 规则；历史包仍需逐条重产 |
| 不确定时默认不是李豆沙；要标李豆沙必须有正证据 | 规则已写，但 pre-selection centrality 仍在纯文本猜身份 |
| 说话人证据不足转人工，不得判死 | 已部署 |
| 自动梯子必须先尝试声纹/分离，再到统一色兜底 | 已部署；candidate-level preflight 仍缺 |
| Ivan 的括号动作（如 `(跃起)`）保留；机器生成的伪括号删除 | 已部署相关修复；1323 同 BV 已修 |
| `ラブコード` 等正字法、白色奶龙、Yuna/PSP/马有利等实体知识 | 已落库/修复相应路径 |
| 不可辨认 11 音节 | 删除该 cue，仍做成品给 Ivan 审，不得 provisional 上传 | 已部署 |
| truth 只用于评估和最终人工裁定，不得喂给修复算法作弊 | 仍是硬规则 |

### C. 标题、封面与发布

| 任务/裁定 | 当前状态 |
|---|---|
| 封面在缩略图尺寸必须一眼讲明故事 | 规则已收紧；历史失败需重产 |
| 游戏切片优先源截图，反应可放大；AI 重绘只是 fallback | 已进入封面路线 |
| 允许完整 hook 文案用于封面 | 有过候选级授权；不能扩成所有稿件的永久授权 |
| cover 临时失败不能永久判死候选 | A1/cover retry 修复已部署 |
| “受骗”上传 | 已发布并现场复核 `BV1JLuj6zEdM` 公开 |
| “贪生怕死” | 明确 hold，不上传；也不应继续烧封面预算 | registry hold 已生效 |
| 上传权限 | 只认仓内 `publication_registry.v1.json`；任何旧的临时授权不构成当前 blanket auth | 当前 40 published + 3 hold；本次没有新上传 |

### D. 歌切

| 任务/裁定 | 当前状态 |
|---|---|
| 日语歌不能靠 BCUT 中文乱码猜歌名；用听音频链，Gemini 是 fallback | 已部署歌名 authority 修复 |
| WSL 产物要能导回 free | 歌切跨主机 import lane 已部署 |
| `agy_rc<0` 后 Gemini 成功不能被旧 AGY 失败码抹掉 | 已修 |
| Gemini 视觉接触表超 20MB 不能 fail-open 成“没有歌” | 相关路径已修/受门约束 |
| 《心型病毒》 | 包已导入 free，但仍是 review/hold；禁止上传 | registry hold；还差 apply/supersede、Ivan 审阅、release |
| 联唱检测 | 表达合同已修，但实际联唱发现信号仍不存在 | P2 未完成 |

### E. 流水线可靠性与代码债

| 任务/裁定 | 当前状态 |
|---|---|
| A1 必须按真实 stage 分类重试/计预算，不是照抄人类举例 | 已部署 |
| hard exit 不得丢 final-review carryover | checkpoint + 原子落盘已部署 |
| CAM++ 对重叠候选必须 embed-once，不能 6N 重算 | 已部署 |
| `_stage_publish_draft` 597 行拆解 | P2；必须保护 fail-closed 异常语义 |
| `_run_exact_final_review_gate` 378 行拆解 | P2；同上 |
| shadow 脚本第二处弹幕 cap 8 | P2 未修 |

## 三、截至接管时已部署的修复

free 的 live authority 是：

- repo/deployed commit：`e00f40501c4183fcaea3912de966f230307150b5`
- 实际代码位：`828a845` 及以前的 23 件修复；`e00f405` 只补 handoff 文档
- 部署时间：2026-08-11T01:01:26Z
- 主线本机全量：`4123 passed in 112.12s`
- `DISABLED`：存在
- runner lock：空闲
- cron：每 10 分钟存在，但每次因 `DISABLED` 正常退出

已部署内容按功能归并如下：

1. **边界/选片**：payoff 后延、弹幕突发提示 cap 放宽、metric v2 shadow、source-fact
   重评分数陈旧状态修复。
2. **说话人/字幕**：证据不足人工停泊、先猜再兜底、正字法 disclosure 修复、不可辨 cue
   删除仍出审阅件、词表/实体补齐。
3. **歌切**：`is_song` 键、歌名听音频 authority、WSL→free import、心型病毒 hold、联唱
   post-song 字段解耦、host-vocal 锚点与 CAM++ embed-once。
4. **稳定性**：A1 stage budget、CPA transient 分类、carryover checkpoint/atomic write、运维日期
   scope、cover hold 提前阻断。

## 四、host-occupancy 真实验收

### worker 产物

- worktree：`/Users/ivan/Project/vtuber-slice-wt/host-occupancy`
- branch：`tmp-host-occupancy`
- commit：`047f63c`
- 改动：新模块 1208 行、v2 枚举 15 行、测试 690 行
- worker 全量：`4173 passed`

### free 真实四候选测量

固定的 3 个 prototype 共 167.69 秒；模型只加载一次。四候选总候选音频 386.1 秒，
wall 125.41 秒，CAM++ 783 次相似度调用。

| 候选 | Ivan 真值 | 检测结果 | host share | 解释 |
|---|---|---:|---:|---|
| `auto_210739_727_840` | 非主讲，不发 | `MULTI_VERIFIED / HOST_MINOR` | 4.26% | 正确抓到非主讲 |
| `auto_210739_1695_1804` | 主讲但没看点 | `UNKNOWN` | 39.68% | 安全停泊，不能证明内容价值 |
| `auto_223750_578_654` | 唯一好片 | `UNKNOWN` | 73.08% | 主播占比高，但未知窗 42.22% |
| `auto_223750_734_822` | 非主讲，不发 | `MULTI_VERIFIED / HOST_MINOR` | 0% | 正确抓到非主讲 |

对真正修复后的完整边界 `578030–734230ms` 又单独实测：`UNKNOWN`、host share 21.35%、
classified coverage 71.2%、unknown 28.8%。这条结果再次证明 **speech share 不是
centrality**：李豆沙可以因游戏行为、事件因果与别人围绕她的反应成为故事主角，而不必占最多
语音时长。

### 为什么不能部署成 P0 闭环

1. `host_occupancy.py` 没有任何 production caller；runner 仍是
   `prioritize()` 后才 `prepare_speaker_routing()`。
2. 语义召回 prompt 和两处 cap 前排序仍让模型在纯文本上猜 centrality。
3. 没有把窗级 HOST/OTHER/UNKNOWN 投影到 ASR cue。
4. 没有 centrality v2 重评、scorecard 重建或 post-occupancy final rank。
5. `VERIFIED_HOST_MINOR` 只是新枚举；v2 对所有 verified 状态一视同仁，且 v2 caller 为 0。
6. `SOLO_VERIFIED` 实现漏了 Pro criterion A：`solo_source=True`；默认 false 仍可自动 SOLO。
7. UNKNOWN helper 虽会在单测里 park，但生产从未调用，所以当前不会改变 state。

**接管裁定：不 cherry-pick、不部署、不解除 `DISABLED`。** 该 commit 可作为下一版 shadow
scaffold 的素材，但 commit message 所写“定 centrality → 重排”不是实际代码事实。

## 五、当前产品与队列真值

### 已公开且仍可见

2026-08-10 21:26 EDT 通过 Bilibili 官方 API 逐条复核，以下 7 稿均 `code=0/state=0`：

- 8/7：`BV1JLuj6zEdM`、`BV1hfuS6EENb`、`BV1houS6SEF3`
- 8/8：`BV18Gu16NEcX`、`BV1Bau16nEyq`、`BV1hquD6pE7X`、`BV13zuX6fEwh`

房间 22966160 当时 `live_status=0`。公开稿件、review package 和 registry 权限是三个不同
验收面，不能互相替代。

### 未完成队列

| 日期 | 当前 state | Talk | Song | 下一产品含义 |
|---|---|---|---|---|
| 8/7 | `ready_unpublished_with_failures` | 10 picks，20 backlog | 6 rows，4 backlog | 人工确认的 `578→734.2s` 应为第一优先 review package |
| 8/8 | `published_with_failures` | 18 picks，无 pending/backlog | 6 rows，3 backlog | 12 个历史失败仍需按新代码重新审计/重产 |
| 8/9 | `no_delivery` | 5 picks，5 pending，4 backlog | 1 pending，14 backlog | 尚无公开成品，是恢复生产后的第一批 backlog |
| 8/10 | 无 state | 已封存多段录播 | 未发现 | 首次转写/召回尚未开始 |

出版登记共 43 条：40 `published` + 3 `hold_pending_review`。三条 hold 是：

- `auto_210739_1142_1436`
- `auto_223750_913_1322`
- `song_210131_1210`

## 六、接下来要做什么（按优先级）

### P0：完成 centrality 的真实两段式闭环

1. **产品政策裁定**：centrality 不再是可被笑点/反差补偿的 25% 权重；需要明确 0–4 中哪些
   level fail gate、哪些 pass，以及 pass 后是否还参与同级排序。
2. **初召回改六维**：prompt 中 centrality 必须是 `null/未观测`；分片内与跨分片 cap 前都按
   `B_i + 25` 上界排序，不能先被旧 centrality 截掉。
3. **N=10 session contention**：按 quota scope 建池；按 source segment 合并扩窗；持久化模型、
   enrollment、源 hash、窗口证据、检测预算和 backfill。
4. **修 estimator 合同**：SOLO 必须同时满足可信 solo source、无多人反证、严格声学门；阈值继续
   标 provisional。
5. **ASR 标签投影与 centrality v2**：关键 cue 有 UNKNOWN 即人工；verified cue 交给新 rubric，
   不能把 host share 线性映射成 centrality。
6. **统一 final-rank choke point**：普通、exact、pinned、revival、拒绝后 backfill 都必须验证 fresh
   receipt，不能有第二条旧 `prioritize()` 绕路。
7. **真实 challenge set**：至少包含这四条 Ivan 盲审真值、完整 578–734.2s，以及不同语气/直播
   条件；校准 false-host、false-other、abstention。
8. 全量测试、free deploy、部署 bytes/fingerprint readback；全部通过后才能移除 `DISABLED`。

### P1：恢复产出（仍不等于上传）

1. exact/operator-scope 只做 `auto_223750_578_734`，使用 Ivan 90+ 盲审 authority，先生成完整
   review package；不要先做旧分数前三条。
2. 8/9 五 pending + 四 backlog；优先拿第一条过全门的 review package。
3. 建 8/10 state，跑首次转写/召回；房间已下播、录播已封存。
4. 8/8 历史失败按 failure stage 分类重跑，不做整候选无差别重烧。
5. 歌切独立处理：心型病毒保持 hold，先完成 apply/supersede 后交 Ivan 审。

### P2：不阻塞首批产品的债务

1. `_stage_publish_draft` 597 行拆解。
2. `_run_exact_final_review_gate` 378 行拆解。
3. 修 shadow selector 第二处弹幕 cap 8。
4. 增加真正的联唱发现信号。
5. v2 OR metric 的证据 hash 绑定、标定和 caller；在此之前只做 shadow。

## 七、产品时间窗（条件式，不做假承诺）

| 交付面 | 最早可信窗口 | 前提 |
|---|---|---|
| P0 生产安全修复 | 1 个完整工程日（约 6–12 小时有效工作） | centrality gate 口径先定；完成接线、challenge set、4123+ 全量 |
| 8/7 `578→734.2s` 完整 review package | P0 部署后约 1–3 小时 | CPA/ASR/cover provider 正常；不上传 |
| 8/9 第一条 review package | 恢复生产后约 2–6 小时 | 前一候选不被 provider/authority 阻塞 |
| 8/10 首轮候选清单 | 恢复生产后约 2–6 小时 | 首次转写与语义召回正常 |
| 第一批跨日 review packages | 恢复生产后约 6–18 小时 | runner 稳定、无新直播中断 |
| 公开 Bilibili 成品 | **无自动 ETA** | 每条必须 Ivan 审阅并显式授权；registry hold 必须先翻转 |

这些是基于现有每候选约 15–90 分钟 producer、真实 host preflight 四条 125 秒和当前 backlog
规模给出的工程窗口，不是发布日期承诺。任何 provider outage、人工审阅或新直播都会顺延。

## 八、本次接管已经执行的动作

1. 读取并交叉核对了上述连续 Claude 会话，而不是只信 HANDOFF。
2. 现场核对 main/free/publication/room/recording/state。
3. 跑完主线 `4123 passed`。
4. 收取并审查 `047f63c`，确认 production caller 为 0。
5. 在 free 用真实音频跑四候选和完整 578–734.2s 测量。
6. 因发现两个合同 blocker 和完整链路缺失，**主动停止错误部署**。
7. 保持 `DISABLED`、没有上传、没有更改出版登记、没有手术 production state。
