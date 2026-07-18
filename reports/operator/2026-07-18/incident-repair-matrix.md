# 2026-07-16 李豆沙四片字幕事故：定义、修复与验收

状态：`NO_UPLOAD_REPAIR_IN_PROGRESS`

时间口径：用户看到的成品含 4288 ms 活字乱刷片头；本表的 cue 时间为内容侧
SRT 时间，因此 `成品时间 - 4.288s = cue 时间`。

## 问题分类

| 类别 | 严格定义 | 本次证据 | 系统根因 | 处理 |
|---|---|---|---|---|
| T1 时间轴 authority escape | 普通 BCUT cue 的 start/end 被下游向内裁剪，导致 cue 短于语音或整段无字幕 | 旧 `subtitle_timing_qa.v1` 将 57.110–60.030 裁成 57.110–58.226、63.210–68.090 裁成 63.210–65.106；公会片 27.000–30.840 被裁成 28.530–29.530 | 低召回 VAD 曾有权破坏 BCUT 时间，而不是 VAD 低召回本身 | 现行 v2 已令 normal cue 时间不可缩；四片全部从 current BCUT 时间重烧 |
| T2 精确文本 authority escape | 已证明为逐字念弹幕或人工确认的 source text 被语义阶段同义改写 | `soyo就是妈` 漂成 `soyo是真妈`；`并非不行` 漂成 `绝对不行` | exact/read-aloud span 未被保护，语义阶段权限过大 | 弹幕近似句增加有界音频仲裁；本次用 hash-bound override 固定逐字文本 |
| T3 专名不平等 | 同一专名槽中，某个别名因流行度、终稿现状或候选顺序获得先验 | `李豆沙` 多次漂成 `小李`；旧修复又只实现“小李→李豆沙”的单向恢复 | 原始 BCUT witness 丢失，且 resolver 不对称 | `李豆沙 / 小李 / 豆沙` 同门槛、同 margin；支持双向恢复；模糊时无默认名 |
| T4 说话人/来源泄漏 | watched-video、嘉宾或系统音频被当作李豆沙字幕；同时不能一刀切删除李豆沙本人日语 | `裏表すごいし` 被用户确认为所看视频音频 | 孤立外语曾被豁免，且缺少候选帧级 speaker/source 证明 | 本片明确 drop；新引入外语无 speaker 证明即阻塞。完整 host-JA 正向 gate 仍需帧级 voiceprint，列为剩余 P1 |
| T5 专名召回上限 | topic crawler 的 schema 无法表示同系列 group/unit，导致音频 resolver 根本拿不到候选 | MyGO / Mujica 语境下没有 `sumimi` 候选 | 图谱只支持 character | 增加 reviewed `unit` overlay 和独立 retrieval activation edge；不伪造角色/作品 membership，不进入全局 glossary |
| T6 cue identity 漂移 | 相邻 cue 的人工文本虽都存在，却被放到错误时间槽 | 3:18/3:22 两句互换 | override 决定只靠 ordinal/时间邻近，没有强 cue instance CAS | 本次逐 cue 修正；schema v3 已绑定 start/end/text witness。后续应加入 source revision + cue instance ID |
| T7 artifact identity 失效 | 同 BVID 已换源，但旧 flat review file 仍被当作当前成品 | 在线 replacement hash 与 `lidousha/2026-07-16/*.mp4` hash 不同；本地仍是 v1 时间 | BVID 被当成版本身份，replacement 未原子刷新/撤销本地 canonical mirror | 本次重烧并刷新四个 flat mirror；增加 `--refresh-only` 入口。内容寻址 current pointer 是剩余 P0 |
| T8 频道唯一字符规范未封口 | `直女` 在最终边界没有无条件映射为 `侄女` | 卡片片旧本地成品仍显示 `直女` | 旧 flat artifact 绕过/早于 hard meme canon | 末端 hard canon 已存在；本次 cue 55 再做 hash-bound 决定并刷新成品 |

## 四片逐项修复

| 片 / 用户时间 | 内容 cue | 期望结果 | 修复状态 |
|---|---:|---|---|
| 公会片 0:25 | 20.712s 附近 | `kmx已经成为了李豆沙的帕鲁` cue 覆盖完整语音 | 待重烧；v2 时间已恢复 |
| 打灰片 0:17 | 09.970–12.250 | `不行不行，并非不行` | override 已验证 |
| 打灰片 0:54 / 1:01 | 46.280–47.396、52.380s 后 | `与你打灰到生命尽头` 及后续语音不被 VAD 短裁 | 待重烧；v2 时间已恢复 |
| 妈感片 约 1:02 | 57.530–60.450 | 删除 watched-video 日语 `裏表すごいし` | override drop 已验证 |
| 妈感片 2:09 | 125.090–127.650 | `soyo就是妈`，逐字复制已确认读出的弹幕 | override 已验证 |
| 妈感片 3:03 | 177.190–179.070 | `哦，sumimi的。` | override 已验证；unit overlay 已接入 |
| 抽卡片 0:34 | 28.040–29.780 | `李豆沙，说个台词` | override 已验证 |
| 抽卡片 1:02–1:16 | cue 24/25/27/30 | 所有经音频确认的自指槽恢复 `李豆沙` | override 已验证；平等 resolver 回归通过 |
| 抽卡片 3:05 / 3:13 | 181.670s、189.030s 后 | cue 使用 BCUT 正常时长，覆盖语音 | 待重烧；v2 时间已恢复 |
| 抽卡片 3:18 | 191.890–193.542 | `李豆沙一直是为爱做1` | override 已验证 |
| 抽卡片 3:22 | 195.450–197.650 | `李豆沙一直是……` | override 已验证 |
| 抽卡片 3:24 | 199.090–200.090 | `侄女` | override 与 hard canon 均已验证 |

## 已通过的自动验收

- 175 项 text/timing/entity/override/reburn 聚焦测试通过。
- 专名平等回归包括反向 canary：BCUT 明确说 `小李` 时可把错误终稿
  `李豆沙` 恢复为 `小李`；`豆沙` 同理；`刘翔` 不被候选顺序吸附。
- topic graph 的 35 项测试通过；`sumimi` 是 `unit`，通过
  `retrieval_entity_ids / activation_work_ids` 激活，不进入作品 ontology edge。
- 三份人工 override 的 source witness 和 decision witness SHA-256 均重新计算并验证，
  逐 cue 应用成功。

## Pro 独立审阅

- 可见模式：`Pro`
- 状态：`read_complete`
- 线程：https://chatgpt.com/c/6a5b0142-d7e0-83ea-b8ae-dc1812744462
- durable record：
  `/Users/ivan/.codex/chatgpt-pro-consult-runs/2026-07-18T04-28-27-796Z-6d2d99875904347f.json`
- 已采纳：把低召回 VAD 重新定义为触发器、把 destructive authority 定义为根因；
  专名等先验；unit 使用 reviewed overlay + retrieval activation；same-BVID 需要内容版本身份。

## 剩余停止条件

1. 四个重烧成品必须全解码通过、含 4288 ms 片头、SRT 不改 BCUT normal 边界。
2. 本地 flat mirror 的 MP4/SRT/record 必须与本次重烧 hash 一致；旧 hash 不得继续暴露。
3. 未获 Ivan 明确授权，本任务不得执行 B 站换源或任何上传。
4. 完整 host-Japanese 自动放行仍缺候选帧级 voiceprint 正向证明；在实现前，
   无证明的新日语只能 `REVIEW_REQUIRED`，不能自动进入 accepted subtitle。
