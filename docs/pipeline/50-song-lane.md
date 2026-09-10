# 50 歌切专线

本文件是歌切步骤的**分步权威**。`.agent/skills/song-lyrics-timeline-aligner/SKILL.md`
只提供操作方法，不能覆盖本文件或当前 schema。

`operator-processing-scope-grant.v1`、v2 `RECOVER_NAMED_FAILED_PICKS`、v3 held-current
重出、v4 `RECOVER_NAMED_SPEAKER_MANUAL_REVIEW_HOLD` 与 v5
`RECOVER_NAMED_RESOLVED_TOPIC_DEDUP_HOLD`、v6
`RECOVER_NAMED_SELECTED_SOURCE_FACT_REJECTION` 与 v7
`RECOVER_NAMED_SELECTED_FINAL_REVIEW_REJECTION` 都只授权点名 Talk，不授权 Song。
scope 激活时，`pending_song`、`song_backlog`、`song_selection_backlog`、`songs`、
`song_superseded_attempts` 必须逐项深等值保留；session annotation、终态投影与持久化都不得
让 Song 漂移，runner 也不得恢复、发现、补位或生产任何 Song。未来恢复历史 Song 必须使用
独立、显式的 typed Song authority。v7 receipt 已存在而 grant 缺失、不可识别或不匹配时，
Talk scope 也必须冻结为空；不能借“grant 不生效”恢复 broad Talk/Song 工作。

## 要点（指针表）

- 识别/去重：`song_lane.py`（视觉歌名 hint 优先于演唱 ASR；已发布歌按 normalized 标题+别名去重 `published_song_history.py`）。
- 歌名命名权威：`song_name_authority.py`。窗口一旦进歌 lane，命名权就归**听音频那条链**
  （`agy_audio_lrc` 观察 × canonical LRC 全局位移证明，判别＝alignment model 带
  `-agy-audio-lrc-global-shift-v1` 后缀，与 `song_completion` 的 `evidence_source` 交叉校验同源）。
  画面 OCR 歌名与 BCUT 中文 ASR / hook 引号标题一律只是**候选提示**（`song_title_candidates`，
  带来源标签），任何环节都不得把它们升格成名字。音频证成即写 `song_name_authority`（含
  `source_ref`/provider/model/`matched_line_ratio`/报告 sha），**与交付授权解耦**——host-vocal
  判否只说明可能不是本人在唱，不影响「这是哪首歌」已经被证过；音频未证成时**没有权威名**，
  维持既有保守处置，不回落到提示名。重试跨 tick 带走已证权威并用它领队 LRC 检索，错名不再
  占 `preferred_title_hints`；无权威时第一趟召回顺序与阈值一字不变。
- 边界：`live_source_review.py` song_boundary（首尾演唱、≥7 行且 ≥80% 演唱、戏剧对白块四重限制）。
- 本人演唱证明：`agy-audio-lrc-observation.v5` 与 `host-vocal-proof.v3` 在同一
  source/LRC evidence 上做联合门；CAM++ 只从明确演唱行取样。memory/日期化 review
  只作历史案例，不是 schema authority。生产音频输入优先交 AGY；只有 AGY 出现机器可判定的
  quota、timeout、不可用或无效输出时，才允许把完整、hash 绑定的音轨与 canonical LRC
  交给直接 Gemini API 作有 provenance 的降级观察。降级结果仍须通过同一 v5 结构、时间证据、
  本人演唱与完整编曲门；free key 轮换及 paid backup 受独立门控。CPA 不接收音频，只保留
  文本/语义终审权；普通文字模型不得冒充音频证人。
- 源视频尚未到盘、切窗失败、BCUT 窗口字幕暂空，以及 Jingting 未取得 AGY/model
  provenance，都属于带指数退避的 infrastructure failure：可以越过普通内容尝试上限自愈，
  但仍受当前已授权的每日歌切名额和剩余 delivery slot 约束。历史只有 free-form error 的同类失败由
  recovery 迁移成 typed reason；不得因为同场其他候选耗尽尝试额度而永久冻结。
- 同一 song pipeline fingerprint 的到期基础设施重试，若上次已确认窄窗属于歌切、
  且全源扩窗仅因 typed provider failure 未完成，沿既有 `resume_full_source` 直接重做
  全源证明，不再次调用窄窗 selector。已进入直接全源重试的结果保留该阶段标记，后续
  同版本重试仍从全源继续；新指纹、缺失/冲突的阶段证据回到普通入口。原 backoff、
  重试资格、每日名额及 Talk-only scope 不变，阶段选择不授予 Song 或发布权限。
  `full_source_retry` 同时保留本次全源 selector 的路径、SHA 与内部 candidate ID；
  顶层窄窗绑定不覆盖，历史缺失引用不从目录新旧猜补。这些失败定位不构成交付恢复权威，
  每次全源重试仍要重新通过音频/LRC、本人演唱和成片全部门。
- 完成原始全源音频/LRC 证明后得到 `SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS` 或
  `SONG_AUDIO_LRC_ALIGNMENT_INVALID`，属于有证据的确定性弃选，不是 provider outage。batch
  必须写入 `song-terminal-disposition.v1`，把它与可恢复 `blocked` 分开；普通 song pipeline
  fingerprint 变化不得重开。只有显式 operator revival（例如新增、边界绑定的歌名权威）才可
  重新进入候选。若同一 attempt 有明确 typed AGY/Gemini/CPA transient，则 transient 优先，
  保留指数退避；窄窗遗留的 Jingting/CPA advisory 不得覆盖后来完成的全源负面证明。
- 歌词字幕：external LRC 全局位移（`song_alignment.py`），零点锚 BCUT ASR 中位（`offset_basis`）；精确重编码；ASS 取整。**歌词正文不过词表修复链**（结构性事实，见 41 重构议题）。
- 歌切不带片头；最终结构由 [80-package-delivery.md](80-package-delivery.md) 强制。
- 标题：铁律目录式，见 [60-title.md](60-title.md)。
- 默认每天最多 1 条歌切（维护者 2026-08-18 21:42 UTC：“默认是每天5条限额和一条歌切”）；
  这取代 7/10 的默认至多 2 首。没有完整合适歌曲不强行凑数。单日多场不能自动叠出多首，
  点名批次/日期的额外授权只在该范围生效，不改全局默认。
- 执行入口是 `candidate_selection.song_delivery_budget`、跨场 refill 与
  `delivery_recovery.recover_bound_song_deliveries`：已交付和已验证公开/待提交预留应共享
  日名额，各场内容尝试上限仍独立。是否已部署须核对实际运行入口与版本；
  不能凭文档更新宣布部署已生效。
