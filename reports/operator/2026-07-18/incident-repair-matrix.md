# 2026-07-16 李豆沙全场 talk 字幕事故：定义、全量修复与验收

状态：`NO_UPLOAD_FULL_STREAM_READY`

时间口径：用户看到的成品含 4288 ms 活字乱刷片头；本表 cue 时间均为内容侧
SRT 时间，因此 `成品时间 - 4.288s = cue 时间`。

## 范围结论

- 当天共生成 11 个 talk candidate。
- 上传账本和当前 BVID 反查得到 8 个当前线上稿；8 个全部进入本次审计，
  全部重新通过 current speaker/burn contract。
- 另外 3 个也已检查：`auto_155648_152_178` 从未发布，仍有未解决的
  `TA` 且是旧 `uniform_host`，因此明确标为非交付就绪；
  `auto_162645_712_787` 与 `auto_162645_938_967` 均被长版
  `auto_162645_712_967` 覆盖。三者旧 timing QA 的 destructive action 均为 0，
  因此记录结果但不重复烧录过时稿。
- 之前只修用户点名的 4 篇属于范围错误；点名时间点只能作为 regression
  canary，不能定义 incident scope。

## 问题分类

| 类别 | 严格定义 | 全场证据 | 根因 | 系统处理 |
|---|---|---|---|---|
| S0 样本冒充范围 | 只修用户举例，没有从真实交付集合反推全量稿件 | 实际当前线上 8 篇，之前只交付 4 篇 | 完成门没有绑定上传账本/current BVID | 项目权威 skill 新增强制范围门：当前线上集合、审计集合、review index 必须相等 |
| T1 时间轴 authority escape | 普通 BCUT cue 被下游向内裁剪，导致 cue 短于语音或整段无字幕 | 8 篇中 6 篇存在，共 20 个 destructive snap；此前漏掉奈叶片 7 个、难绷片 3 个 | 低召回 VAD 获得了破坏 BCUT 时间的权限 | v2 normal cue 不可缩；6 篇从 authoritative repaired timing 重烧并逐 cue 相等验证 |
| T2 精确文本 authority escape | 已确认弹幕/人工文本被语义阶段同义改写 | `soyo就是妈→soyo是真妈`、`并非不行→绝对不行` | exact/read-aloud span 未保护 | hash-bound cue decision；文本层只改字不改时间 |
| T3 专名不平等 | 小李、李豆沙、豆沙因候选顺序或流行度获得不同先验 | 抽卡片多处李豆沙漂成小李；反向 canary 同样可能发生 | 原始音频 witness 丢失、resolver 不对称 | 三者同门槛同 margin；证据明确才互相恢复，模糊时不改 |
| T4 专名槽吞字 | 专名前的普通介词被正则吞进 name slot | `可以由李豆沙自己` 可被解析为专名 `由李豆沙` | 可选介词放在捕获组内 | `由` 移出专名捕获组并加回归 |
| T5 外部视频泄漏 | 所看视频台词被当作李豆沙字幕；不能按语言粗删 | 妈感片 1 个视频日语 cue；奈叶片完整 9-cue 日中混合视频轮次；难绷片 9 个中文视频台词/歌词 | 过去只盯外语，且 binary speaker 标签被误当 source 类型 | 按完整 source turn 区分主播、视频、系统/游戏、连线；主播日语保留，视频中夹中文仍删除，系统公告保留 |
| T6 speaker 标签不稳定 | 删除外部视频 cue 前后，CAM++ 对剩余主播反应的二分标签发生翻转 | 难绷片删掉歌词后，`何意味啊` 等主播反应被重新判成 `[连线]` | clip-local 聚类输入集合改变 | 不把二分标签当删除 authority；对完整 reviewed turn 做 hash-bound speaker override |
| T7 专名召回上限 | 同系列 unit 无法进入 resolver 候选 | MyGO / Mujica 语境下需要 `sumimi` | topic graph 只支持 character | `sumimi` 作为 reviewed `unit` 通过 retrieval activation 接入，不伪造角色 membership |
| T8 cue identity 漂移 | 相邻人工文本存在但落在错误时间槽 | 3:18/3:22 两句互换 | override 只靠 ordinal/邻近 | schema v3 绑定 start/end/text witness |
| T9 artifact identity 失效 | 同 BVID 换过源，本地 flat review 仍指向旧字节 | 旧 flat MP4 hash 与当前 replacement 不同 | BVID 被误当内容版本 | 8 篇 flat mirror 全量刷新并与 free hash 对齐；长期仍需内容寻址 current pointer |
| T10 唯一字符规范绕过 | `直女` 未在最终边界无条件映射为 `侄女` | 抽卡片旧成品仍出现 `直女` | 旧 artifact 绕过 hard canon | 末端 deterministic hard canon；不调用 LLM |

## 当前线上 8 篇逐篇结果

| Candidate / BVID | 旧 timing action | 其中向内破坏 | source turn drop | 处理 | 最终状态 |
|---|---:|---:|---:|---|---|
| `auto_162645_264_391` / `BV1AbNR66ExW` | 8 | 7 | 9 | authoritative timing + 删除完整奈叶视频日中混合轮次 + required speaker 重烧 | READY |
| `auto_162645_394_507` / `BV1cbNR66Eyt` | 6 | 3 | 9 | authoritative timing + 删除狗视频中文台词/歌词 + reviewed host turns + 重烧 | READY |
| `auto_203003_1644_1737` / `BV1cbNR66Eky` | 5 | 0 | 0 | 无 inward shrink；仍重跑 required speaker/burn | READY |
| `auto_203003_272_377` / `BV1rbNR6zEWJ` | 7 | 0 | 0 | 无 inward shrink；小李证据不足时不强改李豆沙；重跑 speaker/burn | READY |
| `auto_210002_297_513` / `BV1cbNR66ECP` | 10 | 3 | 0 | authoritative timing + 专名/句序/侄女修复 + 重烧 | READY |
| `auto_213000_116_205` / `BV1pbNR66E2g` | 4 | 2 | 0 | authoritative timing；保留叙事本体的游戏/系统公告；重烧 | READY |
| `auto_213000_1218_1328` / `BV1pbNR66E2r` | 5 | 3 | 0 | authoritative timing + `并非不行` 等文本修复 + 重烧 | READY |
| `auto_162645_712_967` / `BV1AtNR63ERp` | 6 | 2 | 1 | authoritative timing + 删除明确视频日语 + `sumimi/soyo` 等修复 + 重烧 | READY |

## 非当前 3 个候选

| Candidate | 状态 | destructive timing action | 处置 |
|---|---|---:|---|
| `auto_155648_152_178` | 从未发布；仍有 `TA`，旧 `uniform_host`，非交付就绪 | 0 | 已检查，不制造新的 current 交付 |
| `auto_162645_712_787` | 被 `auto_162645_712_967` 覆盖；旧 `uniform_host` | 0 | 记录检查结果，不重复烧录短版 |
| `auto_162645_938_967` | 被 `auto_162645_712_967` 覆盖；旧 `uniform_host` | 0 | 记录检查结果，不重复烧录短版 |

## 验收

- 8/8 当前线上成品均完整音视频解码通过。
- 6 篇有 destructive v1 timing 的成品均与 authoritative repaired timing
  逐 cue 相等；仅扣除明确、hash-bound 的外部视频 source turn。
- 8/8 `speaker_mode=required`、speaker manifest `READY`、片头
  `PREPENDED` 且 `intro_offset_ms=4288`。
- 当前 SRT 无残留假名；这不是“删日文”门，而是本场经 source-turn 审计后，
  保留下来的主播话语恰好无日文。
- flat MP4/SRT 与 free 当前交付 hash 全部一致。
- 98 项 text/timing/entity/speaker/reburn 聚焦测试通过；全部 8 篇
  `upload_enabled=false`。
- 机器可读逐篇证据见 `full-stream-audit.json`；审片入口见
  `current-review-index.json`，现含 8 篇而不是 4 篇。

## Pro 独立审阅

- 可见模式：`Pro`
- 状态：`read_complete`
- 线程：https://chatgpt.com/c/6a5b0142-d7e0-83ea-b8ae-dc1812744462
- durable record：
  `/Users/ivan/.codex/chatgpt-pro-consult-runs/2026-07-18T04-28-27-796Z-6d2d99875904347f.json`
- 已采纳：把低召回 VAD 定义为 trigger、destructive authority 定义为根因；
  专名等先验；unit 走 reviewed overlay + retrieval activation；same-BVID
  需要内容版本身份。

## 边界

本次只生成 no-upload 修复交付。没有获得 Ivan 明确授权，因此没有执行 B 站
换源、编辑、重新投稿或标签修改。
