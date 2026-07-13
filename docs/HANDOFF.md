# vtuber-slice 交接（HANDOFF）

> 约定：每次实质进展或会话收尾更新本文件（五段：目标/已完成/进行中/阻塞/下一步）。
> 开工先读本文件 + AGENTS.md，别凭旧对话推断。

## 2026-07-12：失败自治恢复与 AGY -> Gemini API 故障转移（当前）

### 目标

消除 7/11、7/12 暴露的伪终态失败：运行环境缺件不能消耗候选重试，
provider 配额/网络故障必须自治恢复，明确下一话题只进入尾气时应缩短尾气，
不安全的内容候选要自动淘汰并由下一名补位。保持人工真值 withheld、
生产 `DISABLED` 和 no-upload。

### 已完成

- 取证确认两类主根因：隔离 `repo-34b9b26` 漏掉 tracked
  `voiceprint_profile.v1.json`，使 6 个 talk 在 speaker preflight 重复失败；
  两日歌曲均先遇到 `AGY_SOURCE_CONTEXT_RUNNER_FAILED`，随后整日
  `6 current + 12 superseded = 18` 的总上限压过单曲基础设施重试。
  AGY job 的稳定 stderr 进一步确认当时为 `Individual quota reached`，并
  自带 17–41 分钟 reset 时间。下游 LRC/边界/人声/hash 缺失主要是同一
  上游失败的级联结果，不是十二个独立内容错误。
- 新增 runtime preflight 和 commit-exact eval builder：候选执行前验证
  tracked speaker profile、talk producer、source-context executor；缺件时
  日期进入 `paused_runtime_invalid`，pending 与重试计数不动。builder 只从
  `git archive <commit> scripts src assets` 生成快照，写逐文件 SHA manifest，
  缺 profile 直接拒绝构建。
- 歌曲基础设施重试改为 per-candidate 优先：不再被整日 18 次 tombstone
  提前掐死；等待 retry-after 的日期保持 `retry_wait` /
  `review_ready_retry_wait` 非终态。AGY quota reset 被解析并加安全余量，
  429/5xx/timeout/quota 等可恢复故障不因历史计数变成内容失败。
- 按 Ivan 指示接入远端 Gemini API failover：正式顺序为
  **AGY -> GEMINI_API_KEY / _2 / _3 ->（仅歌切）draft 继续独立 LRC/边界/
  host-vocal 证明**。Gemini API 只能改文本；cue index 必须一一对应，最终
  时间轴强制恢复为 draft。远端现有 `gemini-3.5-flash` + key#1 实际音频
  smoke 成功，provider=`gemini_api`、输出非空、timing signature 完全一致；
  key 值未输出/落 manifest。
- 7/12 `auto_154845_1140_1254` 的真实事故已修：closure=123310ms、下一 cue
  `我要找一下`=123470ms、VAD=123592ms；旧 VAD-only clamp 仍让下一字幕进入
  22ms。现在 cue timeline 优先，把 final tail 缩到 123370ms，不再误报
  `next_sentence_enters_tail_pad / closure_not_final_subtitle`。
- AGY 保留全部 cue index 但擅改 timestamp（真实 48/48、96/96）时，文本会
  确定性重新挂回 draft 时间轴；漏 cue、重复或重排仍 fail closed。talk
  failure 现在持久化 stage/kind/message/fingerprint/recoverable，不再只有
  generic `failed`。speaker stale sidecar 在读取 profile 前清理。
- 结构化 talk reserve backlog 已加入：`boundary_unrepairable` 或
  `speaker_review_required` 不再让整日少一席，而会记为
  `candidate_rejected` 并按置信度补下一候选；基础设施等待不会用补位掩盖
  outage。全套回归 **879 passed**，代码提交
  `6f9da78606dc764cfedb549fe248bf7e85897727`。

### 进行中（含后台进程）

- 新隔离 BASE：`free:/opt/bilive/autoslice/evals/failure-selfheal-6f9da78`；
  repo 由新 builder 从上述 commit 生成，manifest 98 个 tracked 文件且 profile
  存在。只挂 7/11、7/12 录像，复用机器 ASR cache，
  `AUTOSLICE_HUMAN_TRUTH_MODE=withheld`，显式使用 machine timely snapshot
  与机器角色图。
- transient systemd timer `autoslice-failure-selfheal-6f9da78.timer` 每 5 分钟
  调正式 `free_session_autoslice.py --once`，flock 单飞；没有手工调用
  `process_date` 或候选阶段。首次只读状态为 7/11 `processing`、7/12 尚未建
  state，尚不能声称两日成片收敛。
- 生产 `/opt/bilive/autoslice/DISABLED` 仍存在；本轮未上传，也尚未把
  `6f9da78` 正式部署为生产 `DEPLOYED_COMMIT`。

### 阻塞

- 无需 Ivan 决策的代码 blocker。当前只等待隔离正式入口自治完成；在两日
  state/summary/媒体没有收敛前，不把单测与 Gemini smoke 冒充完整直播验收。

### 下一步

1. 只读检查两日 state、`AUTOSLICE_SUMMARY.md` 和媒体/SRT/封面；不读取进程、
   阶段日志或手工推进。要求等待 provider 的条目保持非终态，明确内容拒绝有
   reserve 补位，最终 package 全部 no-upload。
2. 收敛后停止并移除隔离 timer，更新本节最终结果。
3. 再用正式 `scripts/deploy_free_autoslice.sh free` 部署干净 HEAD，回读
   `DEPLOYED_COMMIT`、runtime assets 和 Gemini key 可见性；继续保留生产
   `DISABLED`，不得上传。

## 2026-07-12：多新番角色图广度调度与最新直播产物（当前）

### 目标

修复角色知识图只有 BanG Dream 的实际调度缺陷，使每日 crawler
从机器时效快照中广度优先生成「话题 -> 当前作品 -> 角色中文名/读音」
子图，并为未来 183 天新番保留固定配额。同时只读回答 7/11、7/12
最新直播是否已有切片；保持生产 `DISABLED` 和 no-upload。

### 已完成

- 根因不是“设计上只做 BangDream”：旧 crawler 按一个 term 连续最多
  6 次搜索，`max_queries=12` 可被前两个 term 吃完；且 committed
  fallback timely asset 本身只有一个人工审阅的「梦限大」term。生产机器
  timely snapshot 实为 202 terms，其中符合当前/未来动漫图条件的有
  96 个。
- crawler 改为 breadth-first：默认 16 topics / 40 searches / 80 HTTP requests，
  首轮每个动漫只查一次，未解析者才进入第二轮；当前 term 占
  12 个槽位，未来 183 天新番占 4 个槽位。漫画、新闻与漫展不进
  角色图；它们仍保留在 timely-term crawler 层。
- 当前作品优先使用 timely snapshot 里的 Bangumi subject 稳定 ID，
  否则依次用 canonical 和带季号/当前标题的 structured readings/aliases。
  Season/Cour/Part 查询不再默默退化为旧本篇，`Black Clover Season 2`
  也不会因为模糊前缀匹配被错接到 `BLACK LAGOON`。当前作 subject
  尚无角色时，才从同 franchise 已有季/本篇回退取候选角色。
- 社区昵称特例改为「梦限大 -> BanG Dream! YUME∞MITA -> MyGO!!!!! /
  Ave Mujica」：先解析当前作，再在广度发现结束后有界扩展兄弟
  作品，不再让父 franchise 提前占满 work 槽位。Bangumi 合并别名中
  的「、，,;/」也已分成独立可语音匹配的 alias。
- 用生产 202-term 机器快照做了无人工真值的临时实网重放：
  **12 topics / 14 works / 127 characters**，38 次搜索、52 次网络请求、
  diagnostics 为空。图包含梦限大当前作/MyGO/Ave、无职转生第三季、
  实教第四季、超市后门吸烟、幼女战记第二季、婚姻剧毒、死神千年血战、
  胆大党第三季、影之实力者残响篇、艾莉同学第二季等。例如
  `Takakura Ken` 可在胆大党子图中对应规范中文「高仓健」，
  `Ayanokouji Kiyotaka` 对应「绫小路清隆」。
- 新增了 breadth/current-work/source-ID/empty-cast fallback/跨 franchise 误匹配/
  current-before-sibling/CLI 默认值回归；完整测试为 **861 passed**，
  compileall 与 `git diff --check` 通过。
- 代码 commit `f5385e85f2635f0b3e02a232b517763ed347fa01` 已用正式
  `scripts/deploy_free_autoslice.sh free` 部署；远程 `DEPLOYED_COMMIT`、runner
  md5 和 crawler CLI 默认值回读一致。每日 06:37 graph cron 恰好一条，
  `/opt/bilive/autoslice/DISABLED` 仍存在，未上传。
- 最新直播产物只读核对：7/11 隔离自治验收为
  `review_ready_with_failures`，已有 3 个 talk MP4+SRT+cover，另有 2 talk failed，
  6 song 全部 blocked；7/12 仍是 `processing`，当时 2 song pending，交付目录
  只有已过时 summary，没有 MP4/SRT/cover。正式生产 state/out 仍只到
  7/10，7/11、7/12 都是隔离验收面。

### 进行中（含后台进程）

- 生产 runtime `state/topic_entity_graph.json` 在本次部署后尚未生成；它由
  已安装的每日 06:37 cron 自动从最新 machine timely snapshot 构建，不由
  agent 手工启动。生产在该文件出现前仍回退到 committed 1-topic graph。
- 7/12 隔离验收读取时仍在自治变化；本轮没有看进程/阶段日志，
  没有手工调用任何切片阶段。

### 阻塞

- 无需 Ivan 决策的代码 blocker。尚未有新 runtime graph 的自治 cron 产物，
  因此不能宣称 12-topic 临时重放已经在生产新直播中端到端命中。
- 7/11 的 6 个 song blocked 和 7/12 无成片仍是真实验收失败/等待状态，
  不能因为本次 crawler 修复而改写成成功。

### 下一步

1. 只读检查下一次 06:37 cron 之后的 `state/topic_entity_graph.json`：要求
   lineage 绑定当次 `timely_terms.json`，diagnostics 为空，且 topics/works/entities
   不再是 1/3/23。不手工触发 crawler 或切片候选阶段。
2. 后续新直播字幕验收时，选取一个非 BanG Dream 话题，核对话题路由、
   原始音频 forced-choice 与最终中文角色名三者；不能只用 crawler JSON
   存在代替端到端字幕真值。
3. 7/12 只在状态自治收敛后重读 state/summary/媒体产物；不恢复对话
   heartbeat，不手工促进 runner。

## 2026-07-12：失败归因与话题子专名图（历史基线）

### 目标

说明最新自治盲测为什么失败，并把字幕专名链改成「先识别话题/作品，
再进入该话题的角色子图，最后由原始音频确认角色身份并写回规范中文名」。
同时修复已确认的 99→98 AGY 漏 cue 和歌曲重试丢失画面歌名证据；保持
truth withheld、生产 `DISABLED` 和 no-upload。

### 已完成

- 已删除 Codex thread automation `vtuber-slice-latest-blind-artifact-check`；
  不再由本对话定时检查。远端隔离 systemd timer 是流水线自身的自治运行面，
  本次没有停止、轮询或手动驱动任何候选阶段。
- 最新 7/11 state 真实为 2 个 talk failed、0 个当前 song blocked、2 个 song
  pending。`auto_170019_305_355` 的旧边界失败已经自愈，当前失败是隔离 repo
  缺 `voiceprint_profile.v1.json`；`auto_170019_580_757` 当前先失败于 AGY
  `expected 99, got 98`，即使越过也会遇到同一缺资产问题。隔离 repo 的
  `assets/` 为空，而相同 commit 的生产 repo 有声纹资产，因此该 eval 副本
  不能视为生产等价部署。7/12 仍在 sealing、尚无最终产物，不能把等待态写成
  内容失败。
- 新增 `lidousha-topic-entity-graph.v1`、有界 Bangumi 结构化 enrichment 和
  `scripts/crawl_topic_entity_graph.py`。运行时从 BCUT 草稿、selection hook、
  结构化 SC/弹幕及可用 screen text 解析 topic/work；显式作品只加载该作品角色，
  仅命中家族话题时加载其有界子作品并集，无匹配/无关歧义则不注入。图只提供
  `canonical_zh`、别名和读音；动态 referent group 仍调用原始音频 forced-choice
  决定身份。
- 当前机器生成快照包含 1 个 BanG Dream 相关话题、3 个结构化作品、23 个角色，
  可提供高松灯、要乐奈、椎名立希、丰川祥子、三角初华等中文规范名及日文/
  假名/罗马字读音。`梦限大` 等 Bilibili 社区别名只负责把字幕路由进该话题，
  不能直接充当角色名真值。当前图 SHA-256 为
  `72ab92748f8defcfa8fc70a90de3bd0ce3b35534645fbf12d519c00cf740cc2b`；
  `MyGO` 可窄路由到其 11 个角色，只有家族话题时才使用 3 个子作品/23 角色并集。
- `withheld` 模式默认禁用 committed/reviewed 角色图，只接受显式
  `AUTOSLICE_BLIND_TOPIC_ENTITY_GRAPH` 机器快照，并要求图内 generator/
  `input_timely_terms_sha256` 与当次 blind timely snapshot 精确匹配；图文件另有
  严格 schema、双向 edge、来源 allowlist、大小/数量上限、过期和 SHA-256 绑定。
- 结构化弹幕只在没有 transcript/selection hook/screen 的明确作品命中时用于
  work routing；弹幕中的 sibling work 名不能覆盖主播明确说出的 MyGO/Ave。
  crawler 任一 enrichment diagnostics 都拒绝覆盖 last-good runtime graph。
- AGY 仅在输出时间戳全部精确属于草稿、且最多漏 2 条/3% 时，按 timestamp
  补回原 BCUT cue；多漏、重复或时间漂移继续 fail closed。歌曲 recoverable
  requeue 现保留 `lane/title_hint/visual_song_evidence`，不会在重试时丢掉画面歌名。
- 当前完整回归 `python3 -m pytest -q` 为 **835 passed**；部署脚本语法、
  `git diff --check` 和相关编译检查通过。
- Fresh 只读对抗审查的所有材料性反例均已接受并修复：包括 sibling chat 覆盖
  主播明确作品、静态/动态图 surface 重叠、非双向 edge、partial crawl 覆盖好图、
  blind lineage 不绑定、短 chunk 超过 3% 仍补 cue、异常 song wrapper 丢画面证据。
  最终审查未发现剩余材料性代码风险。
- 代码承重 commit `03ac31efea735eda82884a9d66b367ac7b2b5d1c` 已通过正式
  `scripts/deploy_free_autoslice.sh free` 部署。远端 runner md5、声纹 runtime
  assets、graph schema/lineage 均验证通过；`DEPLOYED_COMMIT` readback 一致，
  committed graph SHA 为 `72ab9274...`、1/3/23，06:37 graph cron 恰好一条，
  `/opt/bilive/autoslice/DISABLED` 仍存在。没有上传。

### 进行中（含后台进程）

- 代码位于 `/Users/ivan/Project/vtuber-slice-song-selfheal`、分支
  `codex/july10-song-selfheal`。本节代码与部署已完成；本次 HANDOFF 同步是后续
  docs-only 收尾，远端 exact authority 始终以 `DEPLOYED_COMMIT` 为准。
- 远端旧隔离 timer 仍可自治运行旧 `repo-34b9b26`；没有本对话 heartbeat 继续
  追踪它。生产 `/opt/bilive/autoslice/DISABLED` 仍须保留。

### 阻塞

- 旧 eval repo 的部署资产不完整，导致边界修好后仍在 speaker finalizer 确定性
  失败；这不是重试预算耗尽，也不能靠重复同指纹重试自救。
- 7/12 尚无终态产物。不能在没有新 immutable eval/deploy 证据时声称新图已在
  最新全部直播上端到端通过。

### 下一步

1. 后续盲测必须从完整 committed archive 创建等价 repo，并显式传入由机器
   timely snapshot 生成的 blind graph；不得再用空 `assets/` 的 repo 归因生产能力。
2. 若继续旧 7/11/12 自治验收，应新建 immutable 完整部署快照，让 runner 自己
   运行并只在终态读产物；不要恢复本对话 heartbeat 或手工逐阶段推进。

## 2026-07-12：社区专名、边界/声纹自愈与最新直播自治盲测（历史快照）

### 目标

不再由 agent 逐阶段驱动候选片：修复社区时效专名、下一话题边界误判和单个声纹离群点误杀后，由正式 `--once -> tick()` 入口在隔离目录自行处理 2026-07-11/12；主线只按里程碑检查 state/report/artifact。人工真值必须 withheld，不上传。

### 已完成

- 社区 crawler 集成为 `4044433 -> 2d53963`：在官方/ACG 时间窗口外加入有界 Bilibili 社区证据，修复假别名、跨实体桥接、非确定性、部分 HTTP 失败隐藏和 consumer 冲突。排除 reviewed seed 的 live 机器盲测得到唯一 `BanG Dream! YUME∞MITA`，自动别名含「梦限大」，证据来自 2026-07-10..12 的 8 个 Bilibili 社区视频，不依赖 Ivan 给的答案表。生产快照 `/opt/bilive/autoslice/state/timely_terms.json` 为 202 terms，SHA-256 `20245740472d1e5e9739908cdfc9749122822d4f9e207e363170f084258ef8e9`；隔离盲测固定在 `evals/community-latest-20260712/timely_terms.machine-blind.json`。
- 边界修复 `31bc17c`：默认 400ms tail 如果跨入独立的下一话题 VAD island，在证据空白后自适应截断；只有真正穿过语义切点的说话/收束 cue 才能延长。事故 fixture `auto_170019_305_355` 的预期切点为 60.420s，不再被后面的新话题拖延 19.7s。
- 单例声纹修复 `34b9b26`：单个笑声/语气词离群点只在强主播多数、两侧主播、非词汇内容和 whole-clip judge >=0.90 全部成立时自动判李豆沙；其余进入 hash-bound `SPEAKER_REVIEW_REQUIRED`，不烧字/不交付。`confidence:true`、marker-only、缺 media/text/cue-audio hash、stale manifest 均已负向验证为普通失败，不会永久卡死。独立复核无剩余 P0/P1；全量 `818 passed`、compileall 和 diff-check 通过。
- 正式部署脚本已将 `34b9b265d538484348df74a145bde63b60c37dbe` 部署到 `free:/opt/bilive/autoslice/repo`；远端 `DEPLOYED_COMMIT`、runner md5、speaker runtime assets 均验证通过。生产 `/opt/bilive/autoslice/DISABLED` 保留，无上传路径。

### 进行中（含后台进程）

- 隔离 BASE 为 `/opt/bilive/autoslice/evals/community-latest-20260712`，最终代码快照为 `repo-34b9b26`。systemd transient timer `autoslice-blind-20260711-12-34b9b26.timer` 处于 active/waiting，每次完整 tick 结束 10 分钟后再调用正式 `free_session_autoslice.py --once`；使用 `runner.lock` 单飞、`AUTOSLICE_HUMAN_TRUTH_MODE=withheld`、固定机器专名快照，仅可见 7/11 与 7/12 录像目录。
- 旧的一次性 7/11 driver PID 1696379 尚在自然收尾时，timer 只做 PID guard 后立即跳过；它退出后，timer 自动把旧隔离 repo 的已成功交付复制到新 repo，然后由 tick 自行重排旧指纹的边界/声纹失败并处理 7/12。agent 不再轮询子阶段或日志。
- Codex thread heartbeat `vtuber-slice-latest-blind-artifact-check` 已删除；其历史检查规则由上方当前节取代。

### 阻塞

- 无需 Ivan 决策的代码 blocker。当前只等待旧 driver 自然退出及自治 tick 生成最终产物；CPA/AGY 限流会按已持久化的 backoff 跨 tick 重试，不由 agent 手动促进。
- 生产 `DISABLED` 不在本次隔离验收范围内；它何时移除仍需 Ivan 另行授权。

### 下一步

1. 只定时读取 `state/2026-07-11.json` 和 `state/2026-07-12.json`，不看阶段进程。每日期必须达到 `review_ready | review_ready_with_failures | no_delivery`、pending talk/song 为空、segment snapshot 稳定、无同指纹可重试项，且生成对应 `AUTOSLICE_SUMMARY.md`，才称为收敛。
2. 收敛后停止隔离 timer，核对新专名、边界、说话人、歌切、每个媒体/SRT/封面和 no-upload 证据；对 fail-closed 项如实记录，不把 `review_ready_with_failures` 写成全成功。
3. 用最终运行结果更新本节并再跑正式部署脚本，使生产 `DEPLOYED_COMMIT` 与最终干净 HEAD 一致；仍不上传。

## 2026-07-12：7/10 歌切自愈、16 首视觉歌单与时效专名 crawler 集成

### 目标

修复 2026-07-10 歌曲只发现/交付极少数的问题：把主播画面右上角的 15 首歌单作为独立发现与歌名提示来源，并保留歌单结束后李豆沙演唱的《宝贝》为第 16 首；修复《怎么办》重试后复用旧媒体窗口导致完整边界生成失败、host-vocal 验证器误杀、CPA 429 不跨 tick 重试、人工真值污染盲测等通病。同时接入以运行日为中心回溯 9 个月、前瞻 6 个月的 ACG 时效专名候选 crawler。本轮始终 no-upload。

### 已完成

- 集成分支 `/Users/ivan/Project/vtuber-slice-song-selfheal` / `codex/july10-song-selfheal` 已形成可复现提交链：`4e04ffb`（bounded timely-term crawler）、`6078fdf`（歌切缓存/验证器/429/盲测隔离）、`26f8877`（画面歌单发现）、`8a2e0f8`（voiceprint profile/session-anchor 资产重绑定）、`e97b737`（歌曲 AGY 长窗口与跨 tick 重试）、`db6ec5c`（full-source 断点恢复）、`e8720d9`（严格结构校验后恢复 AGY 非零收尾输出）、`0a79c6b`（已验证 AUTO_RECUT 歌曲的 no-upload 包装）。最终全量回归为 **774 passed**，`git diff --check` 通过。
- **16 首事实与发现根因**：真实画面最终歌单为年轮、可愛くなりたい、园游会、猜不透、怎么办、你的微笑、下课铃声、龙卷风、想和你迎着台风去看海、晴る、快乐星猫、MORE! JUMP! MORE!、小城夏天、行星环、太阳系disco，共 15 首；尾声《宝贝》是李豆沙演唱的第 16 首。旧流水线误把 talk setup/payoff/closure selector 当作 song fallback、共享 4 个语义候选上限，并以 180 秒邻近规则吞并相邻歌曲，所以几乎每个 30 分钟文件只留下一个候选。
- **视觉歌单 lane 已经真实回填验收**：固定右上 ROI、每 10 秒抽帧、timestamp contact sheet、一次有界 AGY High 严格 JSON、内容+配置缓存和 fail-open；按日期累计编号歌单去重，与 ASR/语义候选取并集。远端六个有效录制段逐段 live AGY（非 mock）机器恢复编号 1–15 全部歌名和候选区间，随后六段 cache-hit 原子写入 7/10 state 的 `visual_song_inventory` / `visual_song_backfill`；`visual_song_count=15`。OCR 的 `可爱くなりたい`、`more jump more` 只作为 LRC 查询提示，不直接成为最终标题；《宝贝》由独立音频/ASR lane 保留为第 16 首。
- **《怎么办》已真实恢复交付**：根因是重试改变窗口后仍使用固定文件名，旧 tight/full source 分别比新 job 多 42.6 秒，AGY 报 source duration mismatch。修复后 tight `214.250s`、full `269.250s` 均与 job 精确一致；full-source 得到网易云 ID `1862114887`、63 行、匹配率 `1.0`、`FULL_SONG_READY`、AGY `LIVE_STREAMER_SINGING` 0.95、host-vocal READY 和烧字视频。期间继续修复 AGY 慢任务预算、跨 tick backoff/full 断点恢复、完整 SRT 写完后 AGY epilogue 非零、AUTO_RECUT 与 runner 包装契约冲突。最终用 hash-bound summary 零计算恢复为 `review_ready`，reason 只剩 `SONG_FULL_BOUNDARY_READY`，delivery manifest 为 `DELIVERED_NO_UPLOAD / upload_enabled=false`。
- **host-vocal 验证器修复**：不再要求每个演唱 checkpoint 都直接达到过高 enrollment 分数；仍要求 AGY 明确李豆沙现场演唱且无他人/和声/回放，并允许“直接 enrollment 或已验证同场说话桥”通过。远端隔离实证：《宝贝》READY（anchor median `0.56622`，6/7，头中尾齐）、《园游会》READY（实际 post-song speech 3450ms，median `0.50196`，7/7）。没有按歌名白名单放行。最终部署后又用当前代码、完整源片和既有 LRC 报告 fresh 重算《宝贝》，proof 位于 `/opt/bilive/autoslice/out/acceptance/baobei-host-vocal-current-20260712/seededsong_45000_168840.host-vocal-proof.json`，SHA-256 `29e1ac355027141bb1af251bc5ff20c06351e835eff5dac0fdc2d30d923bdd23`，仍为 READY 6/7；7/10 `songs[]` 中的旧 blocked/`SONG_NOT_LIDOUSHA_SINGING` 条目是修复前历史 selector 结果，尚未重跑完整边界/LRC/烧字包装，不得再作为当前演唱身份结论。
- **429 跨 tick 自愈**：从日志尾部区分 `CPA_RATE_LIMITED / CPA_MODEL_DOWN / CPA_UPSTREAM_5XX / CPA_UPSTREAM_TIMEOUT`，状态持久化 `next_retry_at`；15 分钟起指数退避、最长 6 小时、最多 6 次基础设施重试，同时保留总生命周期上限。《行星环》《年轮》不会再把一次 429 当永久内容失败。
- **人工真值隔离**：新增 `AUTOSLICE_HUMAN_TRUTH_MODE=delivery|withheld`。`withheld` 模式屏蔽候选文本 override、字幕回归 gate、人工 reviewed timely terms，并由生产器 fail-closed 防止真值字段泄漏；另有 `scripts/score_blind_subtitle.py` 在生成后单独对真值评分。crawler 支持 `--exclude-reviewed-seed` 生成机器盲测快照。
- **时效专名 crawler 已部署并 live smoke**：AniList/Bangumi/ANN/TV Tokyo RSS/受控活动源，默认 `2025-10-12..2027-01-12`，有界 HTTP/cache、失败隔离、严格 schema、原子写入。远端实网本轮 7 次请求、231 词、adapter error 为 0，已写 `/opt/bilive/autoslice/state/timely_terms.json`；每日 06:17 cron 已安装。排除人工 seed 的机器盲测快照仍为 230 词，SHA-256 `663efd2a0982fdeaef3127c7852b5365cbe817d43113a66102170d65fe826148`。

### 进行中（含后台进程）

- 无本轮遗留 AGY、selector、ffmpeg、CAM++ 或上传进程。生产承重代码为 `0a79c6b`，其后只有本节交接文档提交；最终部署版本以远端 `DEPLOYED_COMMIT` readback 为准。`/opt/bilive/autoslice/DISABLED` 仍在；cron 存在但 runner 保持暂停。7/10 当前歌曲交付为原有《想和你迎着台风去看海》+ 新恢复《怎么办》共 2 条，符合项目原定 `MAX_SONGS_PER_DATE=2`；15+《宝贝》的完整演唱库存与最多交付 2 条的策略已分离。

### 阻塞

- **机器盲测还不能声称自动得到中文“梦限大”**：完整快照里的“梦限大”目前来自有官方来源支撑的 reviewed seed；排除 seed 后能发现当季 `BanG Dream! YUME∞MITA / ゆめ∞みた` 相关实体，但当前结构化源不会自动推导中文粉丝简称“梦限大”。在增加可靠中文别名证据链前必须如实区分。
- 无视觉 backfill、《怎么办》边界或包装 blocker。`DISABLED` 是否在下一场前移除仍需 Ivan 明确决定；本轮没有恢复无人值守 runner，也没有上传授权。

### 下一步

1. Ivan 审听本地《宝贝》源片；当前流水线 fresh 隔离 proof 已给出 READY，但该候选尚未重跑完整边界/LRC/烧字包装，也未占用/突破 `MAX_SONGS_PER_DATE=2`。若后续要求把它形成第三个 review package，需要先明确是否临时突破现有每场最多 2 个歌切的产品策略。
2. 后续补一个有来源约束的中文别名/新闻实体解析层，使“梦限大”在 `--exclude-reviewed-seed` 盲测也能由当季新闻证据导出；在此之前不把 reviewed seed 命中冒充 crawler 自发现。
3. 若接受本生产基线，下一场前由 Ivan 明确授权移除 `DISABLED`，再观察一次自然直播的 15+1 视觉/音频并集和跨 tick 基础设施重试；仍保持任何发布必须另行授权。

## 2026-07-11（续六）：偏航 worktree 隔离 + 当前生产真相复核

### 目标

接管上一 agent 留下的多 worktree 现场，先恢复干净、可逆的 Git 状态，再从 `free` 真实运行面确认当前部署、7/10 批次终态和下一条主线；本轮不上传、不重新部署、不摘 `DISABLED`。

### 已完成

- **8 个 worktree 已全部恢复 clean**。已验证但被后续超集取代的 `codex/campp-perf-fix` 两文件 WIP 隔离在 stash `42e848c9a9e907f529b8738fffe086c7624e858c`；明显偏航的 `codex/speaker-review-corrections` 大型实验（124 文件、约 112k 新增行）及其生成残留分别隔离在 stashes `6419723eb3e3615ec7db38c0c83716c64c0a6be9`、`43ead04413139ba171d2f3aa72629f4ab66fd37f`。三份均可恢复，但不属于当前生产主线。
- **主 worktree 污染根因已修**：Mac launchd 每 30 分钟拉取的 `reports/slice_monitor/autoslice_free/` 是 disposable mirror，却被上传审计证据的全局反忽略规则重新暴露。已删除本地 146MB 镜像，并在 main commit `14cdaf0` 只对该 mirror 重新忽略；canonical 上传证据目录不受影响。
- **生产真相已更新**：`free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT` 当前为 `0f31119f3d1f1d56fc77639fceaac8969792084d`（2026-07-11T09:35:50Z），是 `2246f5c` CAM++ / speaker-final 超集之后的后续 authority、frozen resume、song self-heal 和 cover repair 集成，不再是下节记载的旧部署点。
- **当前代码确定性验绿**：在干净的 `codex/july10-song-selfheal @ 0f31119` 运行 `python3 -m pytest -q`，结果 **728 passed in 18.59s**；旧 `.pytest_cache` 中两个 `lastfailed` nodeid 在当前测试文件已不存在，属于陈旧缓存，不是当前失败。
- **7/10 真实交付终态**：远端 `review_ready_with_failures`；talk 为 5 条 `review_ready` + 1 条真实 `boundary_unrepairable`，song 为 1 条 `review_ready` + 3 条 blocked + 2 条 failed。6 条交付物均有 MP4、SRT 和 `REPAIRED_AI_COVER / VALID_BOUND` 封面，早先 CPA `gpt-image-2` blocker 已解除。无上传进程或本会话后台进程。

### 进行中（含后台进程）

- 无 agent 遗留进程。free cron 仍每 10 分钟触发，但 `/opt/bilive/autoslice/DISABLED` 自 09:35Z 在位，runner 每轮只记录 paused；上传面仍关闭。

### 阻塞

- **恢复无人值守前的唯一运行面 blocker 是 `DISABLED`**。本轮没有授权移除；应在接受当前生产基线、下一场直播前由验收流程明确摘除。
- **Git 集成仍未收口**：生产 `0f31119` 与 main 已明显分叉；不能把隔离的 112k 行实验 stash 当成待合并内容，也不能从旧 `2246f5c` 文档状态推断当前生产。
- 6 条 review package 是否接受、是否逐条上传仍由 Ivan 决定；本轮没有上传授权。

### 下一步

1. 以 **`0f31119` 为当前生产基线**审 7/10 的 5 条谈话 + 1 条歌切；发现具体字幕/说话人/封面问题时走窄修复，不恢复 binary-v4 大型实验 stash。
2. 新建干净集成面，审慎把 main 独有提交与 `0f31119` 汇合；先做 diff/冲突审查和全量测试，再决定是否形成下一部署 commit。
3. 集成基线被接受后、下一场直播前移除 `free:/opt/bilive/autoslice/DISABLED`，随后观察一次自然 cron end-to-end；这一步需要明确运行面授权。
4. 保持 no-upload；任何发布继续要求逐条授权和 hash-bound `AUTO_UPLOAD` manifest。
## 2026-07-12：自动新闻/专名 crawler（独立分支，未部署）

### 目标

把人工 `timely_terms.json` 扩展成可重复运行的候选先验生成器：以运行日期为中心默认回溯 9 个月、前瞻 6 个月，覆盖动画、漫画/轻小说 ACG 企划、新闻和已配置漫展名称；不允许新闻或专名先验覆盖原音与结构化 SC/弹幕。

### 已完成

- 独立工作树 `/Users/ivan/Project/vtuber-slice-crawler`、分支 `codex/timely-term-crawler`，基线为生产 `0f31119`。
- 新增 bounded HTTP/cache、AniList 动画与 manga 结构化范围查询、Bangumi 当前番剧中文名精确匹配、ANN/TV Tokyo RSS 新闻证据、受控漫展 watch、严格配置/schema、失败隔离、离线 cache replay、原子幂等写入和 CLI dry-run/write。
- 实网 pinned smoke（`2026-07-12T12:00:00-04:00`）覆盖 `2025-10-12..2027-01-12`：231 个有效词条（150 动画、50 manga/light-novel 输入、29 个 Bangumi 中文名匹配、11 个 ANN 新闻命中；合并去重后 231），严格 consumer 校验通过；快照 251,655 bytes，SHA-256 `9d253cb03c27c8ae554d5cdf9d66058e5666b08003dbbb93c5f12ff3e39b3a57`。
- 全量回归 `python3 -m pytest -q`：`740 passed in 13.53s`；`py_compile` 和 `git diff --check` 通过。

### 进行中（含后台进程）

- 无后台进程。代码仅在独立分支/工作树，尚未部署、未改生产快照、未上传。

### 阻塞

- 无代码 blocker。数据覆盖仍有诚实边界：历史 RSS 不是归档；Bangumi 中文规范名目前只补当前周表；新创漫展仍需将稳定名称加入受控 watch；自动生成同音 confusable 的误伤成本过高，因此继续由音频回归/人工真值增补。

### 下一步

1. root 集成该分支提交后，在生产候选基线做一次禁用人工 override 的字幕盲测；crawler 只供候选，不作为正确答案。
2. 经集成验收后再决定是否部署，并把 CLI 接入每日有界定时刷新；本分支没有执行部署。
3. 后续可增加有权威中文本地化和稳定发布日期的结构化源，逐步降低 seed 依赖。
## 2026-07-11（续五）：CAM++ speaker_finalizer O(N²) 挂死修复 + speaker-final 超集部署（2246f5c）+ 2026-07-10 批次全 5 条谈话恢复

### 目标

接手另一会话中途的 `3ad0f9b`（结构化字幕权威）工作面：诊断 2026-07-10 无人值守批次里 4 条谈话 rc=1 失败根因、修复、按 Ivan 决定把 speaker-final 超集 + 修复一次性部署上线、把该批次恢复成一致的 review_ready。git 并回 main（#3）本轮按 Ivan 指令押后；无上传授权。

### 已完成

- **根因定位（4 条失败）**：3 条（`auto_190017_1068_1217` 邦多利 / `auto_200009_524_545` 21s / `auto_212005_163_311`）= `speaker_finalizer._run_campplus_analysis` 每对 wav 重嵌入 + 每对重写增长的 `pair-cache.json` → O(句×锚) CAM++ 推理 + O(对²) 磁盘写，1800s 超时把已成片切片 rc=1 崩掉（21s/19 句片也中招=挂死非片长，机器 load 才 1.35/8核）。1 条（`auto_190017_902_950` BW见面会）= 真实 `BOUNDARY_UNREPAIRABLE`（切点后 23s 连续说话、新扩源逻辑已按设计跑过），非 bug。
- **CAM++ 修复（embed-once，保分）**：改为每句 embedding 只算一次（`verifier([wav], output_emb=True)['embs']`）再对缓存向量算 cosine。CAM++ 成对分数本就是这两向量的 cosine，按 pipeline 5 位小数取整 → 说话人裁决逐条不变，推理从 O(句×锚) 塌到 O(句)。真机实测（19 句片）：**8.2s vs 1800s 挂死**，10 对真实 wav old/new 分数 `max|diff|=0.0` 逐位相同，输出 `single_host` 合理。新增 embed-once 调用计数 / 保分 / 缓存持久化三条回归。
- **超集部署（`2246f5c` → free；Ivan 选“并入 speaker-final 超集”）**：新建 `codex/deploy-superset`（off `942f737` = speaker-final committed tip）= 超集 + 那 8 个 parked WIP（batch speaker review + 字幕文本 override + 部署期资产断言，从 speaker-final 工作树 patch 而来，单独一 commit）+ CAM++ fix（port 到其 1294 行 finalizer）。全量 **599 passed**；`deploy_free_autoslice.sh` 自带 staged-tree 校验（含 WIP 资产断言：profile/model hash、references、session anchors、entity_confusables、timely_terms、batch plan + hash-bound overrides）全过——先校验后原子切换。`DEPLOYED_COMMIT` = `2246f5c`（2026-07-11T02:33:38Z）。`DISABLED` 保留（cron 仍暂停）。
- **2026-07-10 批次恢复（全 5 条谈话统一到新流水线）**：复用 runner 自身 `requeue_recoverable_talks → produce_batch(produce_talk) → write_reports`（tick 的谈话路径），跳过 recall + 整个歌切 lane。先补 3 条 CAM++ 挂死片，再按 Ivan“整批统一”补 2 条早上 pre-authority 旧片（弹幕上下摇 / 3D线下见）。终态 `review_ready_with_failures` = **5 交付 / 1 真实边界(BW见面会) / 6 歌切按设计 fail-closed**。5 条全有 `speaker-final.json` + `chat-authority.json`；邦多利终字幕 恋青/梦限大/wakuwaku/立希 正确、零 小室/Mujica/Saki。5 条 mp4+封面已拉回本地 `lidousha/2026-07-10/`。

### 进行中（含后台进程）

- 无遗留后台进程（三个 detached 恢复/验证 driver 均已退出；free 上 scratch 目录与 `recover_*.py/.sh` 已清）。cron `*/10` runner 因 `DISABLED` 暂停不 tick；上传面独立关闭。

### 阻塞

- **封面被 CPA 拦（外部，非本次部署）**：5 条谈话 AI 封面全 `BLOCKED_AI_COVER_REQUIRED`——CPA 当前分组 `Codex-Plus` 不支持 `gpt-image-2`（HTTP 400 `client_model_unavailable`，02:47Z 三条实证一致，redacted 证据在各 clip 的 `evidence/*.cover-cpa-response.redacted.json`）。封面代码未动、23:57 pre-authority run 出图正常 → 是 CPA 账号分组权限变化，不是部署引入。需 Ivan 把 CPA 账号切到支持 gpt-image-2 的分组（我不动 CPA 服务），再重跑 `repair_covers`。盘上：2 条早片留着 12:36 旧封面(标题可能已变)，3 条恢复片无封面。**mp4 内容（说话人标签 + 字幕权威修正）已全部正确**。
- 无代码/部署/恢复 blocker。批次内容 review_ready，等 Ivan 逐条审片 + 逐条上传授权（本轮无授权）。

### 下一步

1. **封面补齐**：Ivan 修好 CPA 分组（放开 gpt-image-2）后，重跑 `repair_covers(2026-07-10)`（顺序补 5 条封面）即可；无需改代码。
2. **#3 git 并回 main（本轮押后，Ivan 明确“先别管”）**：生产在 `codex/deploy-superset @2246f5c`，**不在 main**；main 另有 3 个独有提交（换源 / luna-handoff / speaker-v10-overrides）。**分叉注意**：那 8 个 WIP 现已 commit 在 `codex/deploy-superset` 并上线，但在 parked 的 `codex/speaker-final-pipeline` 工作树仍是未提交改动（需去重/对齐，勿重复落地）。唯一真代码冲突面 = `scripts/apply_speaker_turn_overrides.py`（main v10 overrides vs codex finalizer 依赖）。`codex/campp-perf-fix`（off 3ad0f9b 的最小 fix）已被超集部署取代，可删。
3. 下一场直播前摘 `free:/opt/bilive/autoslice/DISABLED`（cron 恢复无人值守）——归属验收流程 / Ivan 定。
4. 遗留 follow-up（承 3ad0f9b Pro 复核）：边界语义收束、封面行首标点禁则。

## 2026-07-10（续四）：歌切“必须是李豆沙现场演唱”联合门上线 +《芽吹くとき》背景原曲阻断 + cron 恢复

### 目标

纠正“找到同步 LRC/整首歌 = 可以歌切”的产品漏门：只有李豆沙本人在直播现场以演唱为主体的歌曲才允许切；原唱/背景音乐、下播卡音乐、静态或离屏回放、其他歌手、和声/合唱、李豆沙只在音乐上说话均必须 fail closed。同时保留日语/稀疏乱码 ASR 的自动 LRC 恢复能力，fresh 重跑用户指出的 yonige《芽吹くとき》，更新全部当前文档；本轮没有上传授权。

### 已完成

- **生产联合门已部署**：代码承重 commit `f64cd29494fdc0d2b37d249e659897514fd701dc` 已从干净工作树通过 `scripts/deploy_free_autoslice.sh free` 部署，远端 `DEPLOYED_COMMIT`、runner 字节、CAM++ 模型树和三份私有 enrollment 均读回一致。生产 AGY 契约为 `agy-audio-lrc-observation.v4`；`host-vocal-proof.v2` 的七个 CAM++ checkpoint 只从 `SINGING_THIS_LYRIC` 行抽样并重新绑定逐行断言。
- **v4 的窄歌曲对白例外已收口**：首尾必须演唱，至少 7 行且至少 80% canonical 行演唱；最多一个 exact canonical 戏剧对白块，且同时受 6 行、12 秒 voiced、20% lyric-vocal duration、15 秒 wall-clock span 限制；三段 live evidence 必须落演唱行。普通说话/ad-lib/BGM、其他歌手/和声、录制/回放人声仍硬 BLOCK。历史 AGY v3 只由 7/9 事故修复适配器读取，不能进入生产正例。
- **测试与对抗复核**：`python3 -m pytest tests -q` 为 `506 passed`，`compileall`、capability JSON、`git diff --check` 全通过；两名独立 reviewer 对代码和输出绑定均无剩余 P0/P1。可见 ChatGPT Pro challenge 会话为 `https://chatgpt.com/c/6a508d95-7634-83ea-b65a-32033082f810`，复核文本 SHA-256 `41082ecfe2d18bbf6049f049634e86a97639122fa49ef110cecf32cbb5aa5df6`。
- **事故状态已修复**：事务 `/opt/bilive/autoslice/forensics/false-green-20260709-20260710T090025Z-3ad69d0263` 为 `COMMITTED`；7/9 六个 state/report/summary/delivery 权威面均已清除假绿，旧《芽吹くとき》交付保持 superseded/quarantined。
- **真唱正例通过**：fresh《屑屑》v5 在 `/opt/bilive/autoslice/out/acceptance/host-vocal-positive-20260710T101624Z` 完成 52/52 heard、48 行演唱 + 一个 4 行受限戏剧对白块；host-vocal proof 6/7 且头/中/尾覆盖，联合门 READY。仅物料化 241.000 秒、1V+1A 的 no-upload 验收切片，MP4 SHA-256 `fa130beb209bbc8cafa3d7c6556baa39f7a42930d35e1f2b3ed61c7e9da5fbc8`。
- **同音轨静态回放负例通过**：`/opt/bilive/autoslice/out/acceptance/host-vocal-static-replay-20260710T103213Z` 的音轨与正例 decoded PCM 相同，但画面为静态回放；AGY raw 给出 `ORIGINAL_OR_BACKGROUND_PLAYBACK` / `recorded_or_playback_vocal_present=true`，最终 BLOCK，无 host proof、recut、cover、delivery 或 upload。
- **用户指出的《芽吹くとき》fresh 重跑已正确不切**：`/opt/bilive/autoslice/out/acceptance/host-vocal-negative-20260710T103814Z` 自动从 NetEase/LRCLIB/Kugou 路径找出 LRCLIB `33542202`，25/25 日文 canonical 行、一个 global shift 和完整歌曲边界均 READY；因此日语/乱码 ASR 没有让 LRC 阶段失败。本轮 AGY v4 把背景原曲误报成 live，但独立 `host-vocal-proof.v2` 七点 **0/7**，最终 `BLOCK / SONG_NOT_LIDOUSHA_SINGING`、`materialized_recut=null`。这份反例保留了单模型方差，而联合 AND 门成功阻止假绿；wrapper summary SHA-256 `57d139cf584cefc7788856978419385c64fd7d06784d0303dd6ad1e3d7ba31d9`。
- **no-upload 与 cron 恢复均验真**：三次 fresh run 前均冻结 upload-ledger prefix；运行后 ledger 仍为 SHA-256 `c95ee0690a5755b1971bd3dd5165b4bbccefdf4326ddd3736294f43dcc1adfb5`、32,015 bytes，未出现 publish/delivery/uploader。`2026-07-10 10:50:29Z` 在 runner lock 下移除 `DISABLED` 并执行 crontab 的同一 `--once` 入口，rc=0、`live=False`；active state digest `062234c7166df9b8e5724efc9030c0aab0fadba6fe005fd620e8ca7f19b7c23b`、review summary digest `f2e4a34c035435e3586f1e9db2f1c653264111f6730b1d2cf6acbcbbbb670425`、ledger 均前后不变，lock 已释放。随后 `11:00:02Z` 的真实 `*/10` cron tick 自然执行并记录 `tick done: live=False ... 2026-07-09:review_ready`；scheduled runner 现已启用。
- **文档已同步**：README、项目歌词 skill、song-finished workflow、capability MD/JSON、host-vocal 设计审查、7/9 事故审查、remote-first route 与 architecture banner 均改为 AGY v4 / proof v2 / 已部署验收态；下方 LRC-only acceptance 已明确标为 historical/superseded。

### 进行中（含后台进程）

- 无本会话遗留 selector、AGY、CAM++、上传或监控进程。既有 cron `*/10` + flock 已恢复；上传路径仍独立关闭并要求逐条授权。

### 阻塞

- 无代码、部署、状态修复或 no-upload 验收 blocker。
- 边界声明：不能承诺“任意日语歌必成功”；无唯一可靠同步 LRC、版本不符、当前现场改编无法用单一位移解释、缺 post-song 主播锚点或任一联合门/渲染证明失败时仍会 BLOCK。fresh《芽吹くとき》也实证 AGY 单次分类会有方差，所以禁止移除 CAM++ AND 或把 AGY 单层写成充分条件。

### 下一步

1. 下一场真实直播后观察一次自然 cron run 的新 session 结果，确认同一 v4/v2 门在无人值守入口继续保持 fail closed；这不是当前上线 blocker。
2. 若要进一步校准，可对 live/background/static/说话+BGM 小集做重复 AGY 方差统计；不得以此降低现有门槛。
3. 任何具体成片发布仍须 Ivan 另行逐条授权，再冻结 video/cover/title hash 并生成 `AUTO_UPLOAD` manifest；本轮完成本身不构成上传授权。

## 2026-07-10（续三）：封面全文排版修复 + 10 条全部发布 + 何意味/人称字幕修正重传 + judge /responses 落地

### 目标

Ivan 四连指令：①封面字太小要调大，且**必须保留完整原标题**（多换行放大；词/hook/专名不可拆，其余任意断行——我先用了"缩短文案"是错杠杆，已纠正）；②10 条直接上传；③上传后把语义 QA judge 迁到 /responses 并跑通；④新专名"何意味"+第三人称规则（联动主播→她；名人查证，小室=女；查不到→TA），修受影响字幕并重传。

### 已完成

- **排版器工作流修复（commit `1303fd4`，391 tests，已部署）**：行帽提升（侧分 5→8、banner 3→4、song 5→7）+ art direction 新增 `words` 词组切分（无损校验）喂给 `_wrap_even` 作不可拆原子——全文保留、多行放大、换行永不拆词。10 张全部按完整 Ivan 标题重排：传话员 105→145px(7行)、聋哑盲 73→116px(7行)、猜0 186px；**06 温情(49字/banner) 105px 为全文约束下的物理上限**。逐张目视验收过。
- **10 条全部发布闭环（Ivan 授权原话入 manifest）**：全部 state=0、入小李切片(section 现查=9320779)、公开验证 ✓。BV 清单见 `lidousha/2026-07-09/AUTOSLICE_SUMMARY.md` 发布记录节。审计链（manifest/幂等账本/uploaded/public_verify/批次结果）全部 commit（`7c3a4eb`+`5433464`，gitignore 白名单扩到完整审计链）。踩坑记录：新版 biliup 的 ResponseData 带引号导致 do_upload.sh bvid 抓取失效（已修 `4ace7b0`，本批 bvid 从创作中心 archives API 按标题回捞——绝不为取 bvid 重跑上传）。
- **语义 QA judge → /responses（commits `4ace7b0`+`67580e3`+`590cd18`，396 tests，已部署）**：`llm_client` direct transport 加 `api_mode=responses`（gpt-5.x chat 误路由）；judge lane `gpt-5.4-mini/chat` → **`gpt-5.6-luna/responses`**（max-tokens 16000、retries 3 带 429/5xx 指数退避、**fallback 链 luna→gpt-5.5**）。**live 实测**：真实 request artifact 上 luna 429（当晚 5.6 全家共享 provider 用量窗）→ 退避 → fallback gpt-5.5 → rc=0，response 契约完整、provider 如实记 `llm:gpt-5.5`——failover 全链验证通过。
- **何意味 + 第三人称规则（已入权威+同步 free）**：glossary 加 何意味（nani-imi 梗=什么意思；02 切片 Gemini 音频实锤她连说两遍，字幕曾错写"什么意思啊"）；principles 第六条改为：联动主播默认女→她/她们、名人查证（小室=女）、查不到→TA。**受影响字幕已修**（apply_subtitle_correction 重烧）：01 他们→她们(旗袍主播)、02 什么意思啊→何意味啊+TA说是二→她说是二×2、04 TA想问→她想问；音频裁决 R2「啥意思」为真中文不改、15 歌中小学生 TA 正确保留。
- 记忆/摘要/INDEX 已更新；修正版 mp4 已拉回本地。

### 进行中（含后台进程）

- 无本会话后台进程（换源收尾器已跑完退出）。free `DISABLED` 杀开关仍在位（另一会话的保护）；cron runner 不 tick。

### 阻塞

- 无。~~旧稿手删~~ 作废：Ivan 纠正"**编辑视频，不是新上传**"后，3 条已同 BV 就地换源。

### 已完成（补：换源终局，2026-07-10 13:1x-13:3xZ）

- 新投稿重试环（撞日投稿墙 3 轮）废弃杀掉；改走 **`biliup append --vid` 传修正版为新P（网页编辑接口，无投稿频率墙）→ `x/vu/web/edit` 只保留新P**。三条 **BV 不变** 就地换源：BV1tXNE67EHs(168s)/BV1tXNE6EE5g(170s)/BV1uDNE6SEjh(166s) 重审全回 state=0，时长指纹吻合修正版，**合集 episode 按 aid 存活（episodes/add 返 20080）无需重绑**，season 公开可见。manifest v2 逐条 verify 后才动手。工具收进 repo `scripts/swap_video_p.py`；publish SKILL 发布流程新增"换源（编辑视频）"小节；v2 证据（mode=video_replace_in_place）已拉回 commit。

### 下一步

1. ~~luna judge 主路径探针~~ **已完成（2026-07-10 Ivan 点跑）**：用量窗恢复后，生产同款命令（luna 主 + 5.5 fallback）在真实 request artifact 上 rc=0，provider 记 `llm:gpt-5.6-luna`（主路径亲自接住，未走 fallback），契约字段齐全（release_ready/reason_codes/scores/viewer_context/request_sha256 绑定），且与此前 5.5 fallback 对同一 artifact 的裁决 reason codes 一致（跨模型一致性佐证）。judge lane 主备双路径均实证。
2. 下一场直播前摘 `DISABLED`（归属另一会话验收流程）。
3. 遗留 follow-up：边界自修复的语义收束档（嗯类收尾）、封面行首标点禁则。

## 2026-07-10（续二）：quarantine 裁决落地（边界自修复）+ luna 上线矩阵定稿 + 歌切淘汰 + 部署 0d7150a

### 目标

Ivan 三连指令：①luna 已可用，独立重判模型分配矩阵；②歌切未被点名 → 按"没提到=淘汰"补执行 superseded；③**否决 quarantine 状态**——无人值守流水线检测到问题要自己修，修不好 fail-closed，不许"贴标签等人看"。另纠正执行面：重产应在 free 做完拉回本地，不是本地跑。

### 已完成

- **边界自修复替代 quarantine（commit `0d7150a`，387 tests，已正式部署 free）**：`produce_slice_package` 在切媒体**之前**跑纯计算修复环——红旗触发时向后找下一个"可验证干净收束点"（该 cue 尾垫内无新句起头、无 VAD 岛续讲≥1.5s，cap +25s，最多 3 步），开头被句子横跨则回退到该句自身起点；audit 记录完整修复轨迹（`boundary_repairs`）；修不到 → `BOUNDARY_UNREPAIRABLE` fail-closed 不交付（终态不重试）。runner 移除 quarantine 状态词（旧 state 只作 legacy 渲染），摘要改说"自修复×N / 不可修复未交付"。已知边界：修复只保证**声学/句边界卫生**，不判语义收束强度（见下一步①）。
- **02/04/07 在 free 上用部署版流水线重产成功**（Ivan 纠正后从本地跑切回 free：正式部署 `0d7150a` md5 验证 → 复用原 spec/pieces + `--reuse-cover` → 拉回）：02 +2.9s 收「嗯」、04 +6.9s 收「嗯」、07（原 8.4s 续讲）+9.7s 收「所以先是第一个模块」，三条红旗全部清零、时长/末字幕核验过；publish.json 封面字段已从 canonical 合并回（AI_COVER_READY）。本地半途重产已停止并清理（504MB）。
- **模型矩阵重判（luna 实测可用后定稿，随 0d7150a 部署）**：sol/medium=语义召回+字幕校正裁决（开放式、承重、量小）；sol/high=标题；terra/medium=歌名提示（模糊世界知识推断，非 luna 形，且有钉歌兜底不承重）；**luna/medium=封面 art direction**（结构化选择+已知好结果形状+确定性兜底+judge 护栏 = luna 教科书位）。QA judge 仍 gpt-5.4-mini（direct transport 是 chat 形，5.6 Responses-only；迁 luna 需先改 transport）。cpa.env 的临时 `CPA_CHAT_MODELS` 行已按计划删除（每站点 argv 参数接管）。
- **歌切淘汰补执行**：旧《ただ》错词版 + 重产《芽吹くとき》的交付副本全部移入 `_superseded/`（free+本地一致，交付面现恰好 10 条 = Ivan 点名集合）；另一会话的 LRC 修复工程证据（out/、reports/、known_songs）不受影响。摘要/INDEX 处置说明已同步（另一会话曾重写摘要顶掉我 append 的节，已重新追加并保留他们的歌切验收行）。
- 记忆更新：`cpa-gpt5-responses-api`（5.6 矩阵终稿+luna）、`vtuber-slice-goal-repair-first`（quarantine 裁决=修复或拒绝，适用于一切审计门）。

### 进行中（含后台进程）

- 无本会话后台进程。**free `/opt/bilive/autoslice/DISABLED` 杀开关在位**（另一会话 04:30Z 部署后放置的保护，我未动）——cron runner 目前不 tick，下一场直播前需要该会话/Ivan 决定摘除。

### 阻塞

- 无。上传逐条授权（本批明示暂不上传）。

### 下一步

1. **语义收束 follow-up（待 Ivan 定）**：确定性修复会落在「嗯」这类声学干净但文本弱的收束句；廉价改进=修复候选跳过纯语气词 cue（嗯/哦/啊）取下一个干净点，或加一档 CPA 收束判据。07 的「所以先是第一个模块」同理。
2. Ivan 审 `lidousha/2026-07-09/`：10 条 review_ready（其中 02/04/07 带修复轨迹，审片留意结尾语义）。
3. 下一场直播前摘 `DISABLED`（归属另一会话的验收流程）。
4. 封面 fitter 行首标点禁则（03 那张的行首逗号）仍待做。

## 2026-07-10（续）：《芽吹くとき》生产重跑验收 + 日语稀疏 ASR/LRC 路线上线

> **SUPERSEDED / HISTORICAL LRC-ONLY ACCEPTANCE**：本节只证明日语稀疏 ASR 下的 LRC/边界恢复，遗漏“李豆沙本人现场演唱”前提，已被本文件最上方“续四”联合门验收取代。旧 hash、probe 时间、成片与当时 runner 状态仅保留为事故证据，不得作为当前歌切正例。

### 目标

按 Ivan 授权把日语歌不应因乱码/稀疏 ASR 丢失的修复正式部署到 `free:/opt/bilive/autoslice/repo`，从同一条 7/9 原始录播重跑真实歌切，校验 LRC、当前音频、完整边界、字幕、渲染、封面、state 和定时 runner；本轮没有 B 站上传授权。

### 已完成

- **真实重跑已验收**：v1–v3 均因缺证据安全阻断；v4 `song_223019_166_mebukutoki_rerun_v4` 在原始段 `22966160_2026-07-09-22-30-19-.mp4` 上确认 yonige《芽吹くとき》，最终 `final_acceptance.status=ACCEPTED_NO_UPLOAD`。原始 selector 结果仍诚实保留 `status=review_ready`、`decision=AUTO_RECUT`、`reason_codes=[SONG_FULL_BOUNDARY_READY]`、`cover_release_gate_satisfied=false`，不是 `AUTO_UPLOAD`。
- **LRC + 当前音频正证据**：canonical timed LRC 为 LRCLIB `https://lrclib.net/api/get/33542202`；sandboxed AGY `Gemini 3.5 Flash (High)` 无 fallback，25/25 行均 heard、最低 confidence `0.95`、matched ratio `1.0`、唯一 global shift `+17000ms`、无需 stretch。v4 在原始录播绝对时间轴上的权威点是：LRC zero `138.220s`、首句 `145.920s`、末句结束 `343.220s`、post-song talk/切点 `348.220s`。
- **重复段审计缺口已闭环**：原 raw 的 `repeated_section=53790ms` 误指首次出现，但 25/25 observations 已独立覆盖真正复现。manual report 已补 index `17` / full-window `152910ms` / source absolute `274130ms`，当前 report SHA-256 `bcf0500829d6802bc4d6397ea7b48c8ab79edc2d00656d766ecf314a63241e85`；原 raw 与其 SHA 不改。后续 validator/prompt 已要求 exact repeated lyric 必须绑定 later recurrence，错误首现点 fail closed；alignment report 也会直接携带 spot checks 与 post-song talk。
- **成片与字幕通过**：210.000s、1920x1080 H.264/AAC，sample-accurate cut error `16ms <= 100ms`，烧录前后 decoded PCM MD5 相同；25 条 SRT 与 canonical LRC 一致、单调且不重叠，首句/间奏/尾句抽帧无 tofu。MP4 SHA `759bb74fa3b31f663b40547c7b45a96bec606d1470764d8eea9c3b2c97a04bee`，SRT SHA `e89a1b6d1dda509bbc44ff95ffa7d40d17761076dbc49b217e8ae91b11dcb789`，alignment report SHA `d2daa6f4ea007e587c0a99c34bce2d117bdce16f606e4db0df2b9cf8854a6c2e`，recut manifest SHA `0c17e3ad9622c2d9f99a63104bfc9ef84370574812d0180e0f6e4effc5900b7a`。
- **标题/封面通过 no-upload 审片门**：标题为 `【李豆沙】豆沙歌，《芽吹くとき》｜下播前的温柔哄睡小歌`；selector gate 后单独用项目正规 `gpt-image-2 images.edit` / `song-clean` 流程生成封面，fallback=false，1920x1080，文案 `《芽吹くとき》｜下播前的温柔哄睡小歌`，实看通过，SHA `7ee06854118b08f573111000f766f0b22ee2d078a9f9b8a576b77ee058f8ba17`。这不把 package 提升成 publish-ready。
- **控制面已修复**：验收后才备份并原子替换 7/9 state 中旧假绿记录，保留原逻辑 candidate id、另记 rerun id；旧错误 MP4/cover 已移入远端 `_quarantine/false-green-song_223019_166-20260710T040416Z`，本地镜像移入 `_superseded/`。当前 state SHA `0115c631d81d9f0bc7b3b990d9d0a20ed99e8aa57090935dea6a799b4a3f628c`，summary SHA `d5f051804a61a8e67005aa16f6ed1703504f52820ef0f1ec93b1ed512e310b89`，摘要当前显示《芽吹くとき》。
- **流水线已正式部署并验证**：v4 本身运行在 `1e8818c`；hardening commit `c847325ce43e7e3914e8921c3f27c1254a6beffa` 先经 `scripts/deploy_free_autoslice.sh` 正式部署，本节所在的最终 clean HEAD 随后由同一脚本同步，远端 `DEPLOYED_COMMIT` 指向该收尾提交；runner/song-repair/AGY 等运行文件 SHA-256 与对应干净 commit 快照一致。聚焦回归 `70 passed`，全套 `382 passed`；独立 acceptance reviewer 与 docs challenger 均无剩余 P0/P1。
- **不是“所有日语歌强行过”**：tight/core/full 均保留 upstream song seed 并使用 `--lrc-provider auto`（NetEase + LRCLIB）；只有 expanded full retry 可额外启用当前音频证明。日语/kana 不再因 ASR 乱码直接丢歌；但无唯一可靠同步 LRC、身份/版本歧义、音频或 hash 证明失败、非单一位移、渲染失败时仍 fail closed。
- **文档已同步更新**：项目歌词 skill、song finished workflow、canonical E2E runbook、capability MD/JSON、事故/live acceptance review、remote-first/architecture/Bcut 边界说明、README 与 AGENTS source-of-truth 均已修正；旧 dated plan/handoff 仅加 `SUPERSEDED` banner，避免重写历史。
- **runner 已恢复**：本轮临时 `DISABLED` 于 `2026-07-10 04:19Z` 在 runner lock 下移除；04:20 UTC 的真实 cron tick 成功。最终部署后又按同一 cron 命令手动跑了一次 04:25 smoke tick，两次均输出 `live=False ... 2026-07-09:review_ready`，state/summary SHA 未漂移，锁已释放。没有 `.publish.json` / `AUTO_UPLOAD` / uploader 进程，`upload_enabled=false`。

### 进行中（含后台进程）

- 无会话遗留进程。只有既有 cron `*/10` + flock 定时 runner 正常启用；04:20 tick 后没有常驻 runner，也没有上传器。

### 阻塞

- 无代码、部署或 no-upload review blocker。公开视频仍缺 Ivan 对这条具体成片的逐条上传授权；在此之前不得生成/执行 `AUTO_UPLOAD` manifest。
- “所有日语歌必成功”不是目标：同步 LRC 缺失、身份歧义或现场改编与 canonical LRC 不符会按设计阻断，需要新增可靠来源或人工复核，不能降门伪绿。

### 下一步

1. Ivan 可直接审本地 `lidousha/2026-07-09/歌切_【李豆沙】豆沙歌，《芽吹くとき.mp4`、同前缀 `.cover.png` / `.srt` / `.manual-rerun-report.json`；远端 package 与本地关键 SHA 一致。
2. 若 Ivan 明确授权发布，再单独冻结视频/封面/标题 hash，生成 `AUTO_UPLOAD` manifest 并走幂等 uploader；本次完成本身不构成上传授权。
3. 后续普通 post-stream session 继续由 cron 无人值守运行；若遇无可靠 LRC/歧义/音频证据失败，应保留明确 blocker，不回退为 ASR 歌词假绿。

## 2026-07-10：Ivan 审片点名执行（7 补产＋3 改标题＋淘汰）+ CPA 默认模型切 gpt-5.6

### 目标

Ivan 点名 7/9 落选预览 **01/02/03/04/06/07/15** 按其手定标题出成品、已产 3 条改标题（传话员→沙豆李 / 聋哑盲→黑白小猪 / 猜0→数字零）、未点名一律淘汰、**暂不上传**；出成品前把 CPA 默认模型按任务复杂度切到 **gpt-5.6 家族**（sol/terra/luna 分级、配 effort、留 5.5 fallback）。

### 已完成

- **CPA 模型切换（commit `beb4f13`，381 tests 全绿）**：`llm_via_cpa.sh` 增 argv 3/4（每站点模型链+effort，arg>env>默认，默认链 `gpt-5.6-sol→5.5→5.4`）。分级（Ivan challenge 后定稿）：sol/medium=语义召回+字幕校正裁决（prompt 大，high 会顶 curl 180s 单次上限）、sol/high=标题（短 prompt 单次）、**terra**/medium=歌名提示（known_songs 钉歌兜底，不承重）+封面 art direction（结构化 fail-open）。**gpt-5.6-luna 在 CPA 上 auth_unavailable（providers=codex 无权限）**，放开后 art direction 是首个切换位。语义 QA judge 仍 `gpt-5.4-mini`（direct transport 是 chat 形，5.6 Responses-only 会误路由，动它需先改 transport）。健康探针改走生产链（sol→5.5→5.4）。**免部署生效**：free `cpa.env` 已加 `CPA_CHAT_MODELS`（部署 `beb4f13` 后应删掉此 env 行，改吃 per-stage 参数）；部署版桥接 sol 实测通过，本批全程零 failover。记忆 `cpa-gpt5-responses-api` 已更新。
- **7 条点名成品全部交付**（`lidousha/2026-07-09/`，driver=free:/opt/bilive/autoslice/produce_ivan_20260710.py，正规 produce_slice_package 链，`given_title` 手动标题直通+【李豆沙】前缀）：01 拜早年(2:47✓)、02 哑巴尖叫(2:47 ⚠quarantine：切点后语音续1.8s等3旗)、03 厨房着火(1:47✓，封面502批末修复)、04 哑人最难(2:38 ⚠quarantine：2旗)、06 下播肺腑(1:46✓)、07 无视灯(2:36 ⚠quarantine：**语音续8.4s 本批最大**+2旗，封面502已修复)、15 炸学校歌词(1:49✓)。**验收**：10/10 标题与 Ivan 原文逐字符一致；抽帧验字幕（01/06）正常；封面抽验 6 张（06 含"自"整张得意黑✓、15 背景自动画蒙眼/捂耳/封嘴三猴无爆炸元素✓）；upload_enabled 全 False。已知小瑕疵：03 封面第二行行首逗号（fitter 回流），待字排 follow-up。
- **3 条改标题**：封面复用原 AI 背景原版式重排（零出图），publish.json title/title_source=`ivan_manual_20260710` 已更新，肉眼验收通过。
- **淘汰**：「聊三人游戏聊到《胡闹厨房》」「李豆沙卡在动画里害全员罚站」→ `_superseded/`（free+本地）；预览 05/08-14/16-22 维持落选。处置全记录在 `lidousha/2026-07-09/落选预览/INDEX.md` 与 `AUTOSLICE_SUMMARY.md`（均已同步 free）。

### 进行中（含后台进程）

- 本会话无遗留后台进程（driver 已跑完，结果在 free:/opt/bilive/autoslice/reports/ivan_20260710_promote_results.json）。**另一 agent 的歌切《芽吹くとき》重产/声纹实验在同仓活跃（commits 6b35849/d43a310/60beb22），本会话未触碰其交付物。**

### 阻塞

- 无。上传永远逐条授权（本批 Ivan 明示暂不上传）。

### 下一步

1. Ivan 审 `lidousha/2026-07-09/`：4 条 review_ready + 3 条 ⚠quarantine（02/04/07 边界红旗，07 的 8.4s 续讲最值得看结尾；Ivan 预览时看过裸切端点，红旗属保守审计）。
2. 下次授权部署带上 `beb4f13`（per-stage 模型参数生效），同时**删除 free cpa.env 里的 `CPA_CHAT_MODELS`/`CPA_REASONING_EFFORT` 两行**（否则它只对无参调用者生效，无害但易混淆）。
3. gpt-5.6-luna 在 CPA 放开后：art direction 链 `terra→luna`（或直接 luna→5.5），顺带评估 QA judge 迁移（需 direct transport 支持 /responses）。
4. 封面 fitter follow-up：行首标点（03 那种）在 `_fit_cover_lines`/`_wrap_even` 回流时应禁则（避头点）。

## 2026-07-10（续）：7/9 歌切假绿纠正——实际是 yonige《芽吹くとき》

### 目标

纠正本文件下方把 7/9 歌切写成《ただそばにいて》且“字幕是正确日语歌词”的错误结论；把“搜到 LRC”变成可验证、可追溯、缺证据就不交付的自动流程。仍然只做 review package，**没有 B 站上传授权**。

### 已完成

- **旧结论已证伪**：远端真实 selector 记录里 `lyrics_alignment={}`、`song_boundary={}`、`subtitle_source=asr_cues`；旧 MP4 是把 ASR 烧进去的，不是 LRC 字幕。《ただそばにいて》只是歌词句，不是歌名。下方跨-agent 契约第 7 条末句仅保留为事故记录，不再是权威状态。
- **公开歌词源很容易拿到**：按 Ivan 提醒走公开检索，确认歌曲是 yonige《芽吹くとき》；LRCLIB `33542202` 有 25 条同步歌词，代码保留直接来源 `https://lrclib.net/api/get/33542202`，并在 `known_songs.json` 固定歌名、作者、来源与可容错指纹。yonige 官方 discography 只作身份佐证。
- **边界重新核实**：旧源窗 `151.220–341.760s` 物理上漏了前奏和结尾。原始段上下文的 Gemini 3.5 Flash (High) 音频/LRC 审核判定单一全局位移、无需 stretch：LRC 零点约 `148.5s`、首句约 `156.2s`、尾部约 `349.0s`，下一段说话约 `348.820s`。证据留在本机忽略目录 `reports/2026-07-09-mebukutoki-lrc-repair/agy_probe/`。
- **工作流修复已在本地完成**：新增 LRCLIB 同步歌词 provider 与 NetEase→LRCLIB 隔离 fallback；日文假名可参与搜索；钉歌即使通用搜索暂时失效也能跑；clip 从 LRC 零点前 1.5s 起而不是从首句前 1.5s 起；紧窗口识别为歌但无完整证据时，同一原始段只扩大一次到 anchor±45s 重试，并把歌曲精听固定为 High。
- **交付门改成正证据门**：必须同时有 `FULL_SONG_READY`、`lyrics_alignment READY`、结构完整且匹配率≥55%的 alignment report、报告 SHA、边界/nominal LRC zero/offset/候选/歌名/来源互相一致、`external_lrc_global_shift` SRT、SRT SHA、精确重渲染成功、render QA 通过、materialized recut、burned MP4 SHA。旧的 MP4/cover glob 兜底已删除；outer selector summary 也已补齐向 runner 透传 materialized recut 等字段。
- **最后 challenger 的三条 P1 已关闭**：①每次 selector 改用独占空 `attempt-*` 目录，且当前 rc 必须为 0，失败进程不能重用旧 summary；②nominal LRC zero 在源窗外直接失败，runner 再绑定 boundary/alignment/report 三处零点；③外部 LRC 的 sample-accurate 重渲染或 fresh render QA 任一失败，producer 标 `RETRY_INFRA`，runner 也独立拒绝。原 reviewer 复跑后结论为无剩余 P0/P1。
- **验证**：`python3 -m pytest -q` 为 `367 passed`；`python3 -m py_compile ...` 与 `git diff --check` 通过；实时 LRCLIB 直取返回 `lrclib / 芽吹くとき / yonige / 25 lines / 7700–199890ms`。假绿门、source 两端丢失、stale glob/summary、outer-summary 丢字段、空报告自哈希、负 LRC 零点、准确重渲染失败均有对应回归测试。
- **生产只读核对**：`free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT` 当前仍是 `cab9a151ccd67605d087ee0aae48c1ecf1f88830`；7/9 state 仍把 `song_223019_166` 记为 `song_complete=true` 并指向旧《ただそばにいて》文件，证明本轮修复尚未上线，旧 state 也必须视为假绿证据而不是验收结果。

### 进行中（含后台进程）

- 无项目后台进程。代码与本地 challenge/defense 已收口；不会触碰 `free` 正式部署或上传面。

### 阻塞

- **本轮修复尚未部署到 `free`，也尚未生成新的 7/9 review package**；旧的《ただそばにいて》本地 MP4/cover 必须视为 quarantine，不可上传、不可作为正确歌词证据。
- 生产部署属于外部运行面变更，Ivan 本条消息没有明确授权，所以不擅自执行。若获授权，只能走 `scripts/deploy_free_autoslice.sh`，然后用真实 7/9 原始段跑一次 no-upload acceptance。
- ChatGPT Pro 终审的 Hermes CDP 首次尝试因可见模型标签未确认而在提交前停止，记录状态是 `mode_not_confirmed`；没有重复提交。手工向 ChatGPT 发送 prompt 也需要明确授权。独立本地 challenger 不受此阻塞影响。

### 下一步

1. 以本节所在提交作为唯一部署输入；不要从未提交工作树或单文件同步。
2. Ivan 若授权部署：用正式部署脚本更新 `free`，核对 `DEPLOYED_COMMIT` 与 md5；随后只重跑 7/9 这首歌，验收 report/SRT/MP4 三份 hash、首句/副歌/重复段/最长间奏/尾部五点以及新标题/封面；**不上传**。
3. 新 package 验收成功后，把旧错误文件移入 quarantine，并把本节更新成真实远端路径、commit 与 hash；若新精听对齐低于门槛或 sample-accurate 重渲染/QA 失败，保持 blocked，不借用这次人工审核结果伪造运行时绿灯。

## 2026-07-10 跨 agent 协调契约（歌切 gate/封面 与 runner v4 会话）【给正在修 local_prepare+歌切封面顺序的 agent】

**背景**：Ivan 2026-07-10 给 runner-v4 会话的指令：「歌切不需要语义，只要是弹幕最高两个歌就可以」。你（另一 agent）正在做：local_prepare 先过 gate 再出封面、整段录播默认不做 AI 封面、autoslice 歌切先过 release gate 再做封面。两边在 `produce_song` 相撞，契约如下：

1. **歌切交付规则（Ivan 拍板，权威）**：交付条件 = **是歌（window_classified_song）+ burned 物料齐**（+保留你的 sha256 验证，hash 卫生是好的）。`decision_action`/`reason_codes`（AUTO_UPLOAD/BLOCK/ADVISORY_*）**只作摘要参考，不再拦交付**。你当前 working tree 里 `gated_song_delivery_artifacts` 要求 `decision==AUTO_UPLOAD 且 reason_codes 为空` —— 这与 Ivan 指令相反（7/9 的《ただそばにいて》x18 该交付而不交付），请按本条改。
2. **封面顺序**：先过（上面新定义的）gate 再做封面 ✓ 不冲突；但 `cover_repair_needed` 的歌切守卫别锁在语义门上——按新交付规则交付的歌必须能补封面，守卫条件同第 1 条。
3. **已部署事实**：你管线侧的 `cover_release_gate`（run_auto_review_shadow_pipeline.py:2288）已被 runner-v4 会话在 commit `bc14ee1` 一并 commit 并部署到 free（当时以为是 7/7 遗留）。别重复提交；线上已有。
4. **部署纪律**：free 部署只走 `scripts/deploy_free_autoslice.sh`（dirty tree 拒绝、DEPLOYED_COMMIT 指纹、md5 校验）。**不要 scp 单文件上 free**——会被下次 rsync --delete 冲掉。
5. **旧控制面已退役（2026-07-09 20:26Z 执行完毕）**：live 实测（虚拟区房间 22499290 实录 3.5min，scan/local_prepare 全程死透）证明**仅 blrec 就产出 runner 全部输入**——compact 名 `.mp4`（remux_to_mp4，ffprobe 201s ✓）+ `.xml` 弹幕 + `.jsonl`(SC)，date 目录布局不变；runner 的 `{ROOM}_*.mp4` glob 与 `find_danmaku_xml`（查 parent+sources/）天然兼容 compact 名，**零代码搬运**。已做：compose.yml 删掉 scan+local_prepare 两行（备份 `/opt/bilive/compose.yml.bak-20260710`）+ 容器重建（两 blrec 实例/监控/录制开关验证 OK，runner tick 绿）；shadow daemon systemd unit `lidousha-auto-review-shadow-22966160` 已 `disable --now`（它每 5min 全树重扫老日期 slice_candidates.json 是 FUSE 挂载不稳的主嫌——clouddrive 死前最后一条日志就是在读 6/17 的这个文件）。**注意**：⑴ 20:26Z 容器重建杀掉了你 docker exec 起的 scan/local_prepare 测试进程——要继续测直接 `docker exec bilive_record python -m src.upload.local_prepare`，不依赖容器 Cmd；⑵ 你 local_prepare 侧修复对生产已 moot（进程不再常驻），autoslice 歌切侧（第 1/2 条）仍有效；⑶ 旧管线的全段 AI 封面/whisper ASR/hybrid 切片/publish 草稿随退役全部停止（"整段录播默认不做 AI 封面"的根除版）。
6. **弹幕 top2 选择**已在 runner v4 的 `prioritize/refill_songs`（配额=交付+回填），你不用做。
7. ~~歌切交付语义的实现权~~ **已由 runner-v4 会话接手落地（你的改动 4.5h 无更新，Ivan 直令收尾；commits 7209e03+cab9a15，已部署 free）**：Ivan 最终规则（较第 1 条细化）＝**至多 2 个、按弹幕量排序、没唱完整（SONG_PARTIAL）不切**；语义判定（AUTO_UPLOAD/BLOCK/closure/viewer-context）仅作参考。实现：`song_delivery_ok`（交付判定）+ `song_delivery_artifacts`（你的 hash 卫生保留：sha256 漂移拒用，`gated_*` 去语义门重写）+ `record_is_song`（semanticsong_* 召回分类也算歌——日语歌 LRC 钉不上时不再漏，7/9 当时按《ただそばにいて》召回）+ cover_repair 守卫跟随交付。你的两个门测试已按当时规则改写，349 tests 全绿。**本条原称“含正确日语歌词”的 7/9 回填已于上方 2026-07-10 续节证伪：实际是 yonige《芽吹くとき》，旧物料为 ASR 字幕，必须 quarantine。**

## 2026-07-10（续）：切片流水线在 blrec 原生输入上端到端验证 + monitor 复活逻辑根除 + GitHub 预检

**目标**：Ivan 指出退役旧控制面只证明了"能录播"没证明"能自动切片"（流程的核心）；且之后要发布 GitHub。

**已完成**：
- **切片流水线 e2e 实证（compact 命名，旧面已死）**：拿 7/9 真实 blrec 原生段 `sources/22966160_20260709-20-00-28.mp4`（1.3GB，弹幕 xml/jsonl 同目录）跑 runner `--smoke-segment` 全链：BCUT 转写→CPA 语义召回（5 候选）→边界 snap+audit（red_flags=[]）→精确重切→AGY+CPA 字幕→sapphire72 烧录→真标题（「【李豆沙】0是什么手势？小李终于懂了：原来是豆沙」）→gpt-image 真封面（AI_COVER_READY）→交付 `review_ready`。**结论：新链路对 blrec 原生输入完全可用，不依赖旧面任何产物**。另补 `tests/test_compact_segment_names.py` 钉住两种命名兼容（compact/dashed、xml 在 parent/sources 都能配）。smoke 产物已清理。
- **monitor 是旧面复活的真凶（根除）**：`lidousha_slice_monitor.py` 有 `slice_blessed` crash-recovery——旧面进程不在就 `docker exec` 拉回来（实锤：我 20:2xZ 跑了一次监控，scan+local_prepare 20:29 就复活了）。已重写：①删除全部 start_scan/start_local_prepare/_start_daemon/FORCE_START_SCAN/AUTOSLICE_ENABLED 复活逻辑——旧面进程在跑只 WARN 绝不重启；②新增 `run_autoslice_probe()`：监控新面健康（heartbeat 新鲜度>30min 告警、SOURCE_UNAVAILABLE=DOWN、近 6h ALERT_* 文件上报、下播后日期状态卡 new/sealing/processing 超 90min 告警）；③jingting 旧遗留 backlog 降为 note 不再永久 WARN；④upload 进程 kill 守卫保留。实跑验证：verdict=无问题、心跳/日期状态入报告、跑完旧面仍为 0（不复活）。
- **GitHub 预检 + 秘密修复**：`lidousha_slice_monitor.py` 硬编码 blrec RECORD_KEY 两处已改为运行时读 env/.env（工作区秘密 0 命中）；预检清单见上节"GitHub 发布预检"。

**进行中/阻塞**：~~歌切去语义门~~ 已落地（见契约第 7 条，commits 7209e03+cab9a15）。

**2026-07-10 Gemini 免费 key 落位（AGY 到期预案，已完成）**：3 把免费 key（尾号 fvPo/w7AA/vtXA，各 20 RPD=共60次/天）就位——本机 `~/.config/vtuber-slice/gemini_keys.env`(600) + free `/opt/bilive/.env`(600)；逐 key 实测 gemini-3.5-flash 全部 HTTP 200。旧付费 key(...lCdc) 已从 .env+备份删净，**Google 控制台吊销待 Ivan 手动**。HF token(...pDQL) 也已落位 free `/opt/bilive/autoslice/hf.env`。待办：`gemini_slice_jingting.py` 加 429 轮换+每 key 当日记账（现只有 KEY→KEY_2 简单 fallback），AGY 到期日切 `--provider gemini`。

**2026-07-10 声纹分离 v10 定点真值版（已完成；禁止上传）**：

- **目标**：修复 v8/v9 把一个 ASR cue 强制当成一个说话人的结构缺陷，并把 Ivan 定点复听给出的说话人、文本、删除和抢话真值落成可复现的全片二分色验收版。
- **已完成**：Ivan 已审完 6 处无标签盲听包；连同此前两处，权威修正包括：#3「这中间」=李豆沙；#17 礼墨「因为李豆沙会一直说话」→ `32.250s` 换李豆沙「啥意思啊」；#27 多人无实意哼哈整 cue 删除；#33 多人笑（无李豆沙）及 #34 李豆沙「你当时要不就看这个吧」；#36 李豆沙「你看她啊」→多人笑；#38 礼墨笑→李豆沙「我真的分不清」，声学稳定空隙把换色点收在 `78.000s`，并把下一 cue 的 ASR 重复前缀删掉、只留「左右」；#49 主层李豆沙「但是对我来说」，安晚重叠「暂时」单独放第二层；#69 只记录 Ivan 实际听到的末尾「传信息」=礼墨，不外推整 cue 的具体来宾身份；#70 安晚「是的，只是单方面的」→ `148.300s` 换李豆沙「盲人跟哑人说话」。重叠规则按 Ivan 最终口径固化为：主说话人字幕必保，副说话人仅在字词和时段都可靠时 best effort 双层，否则不猜。
- **实现与验证**：`scripts/apply_speaker_turn_overrides.py` 现支持 fail-closed source hash/预期文本/时间、整 cue 删除、连续换人拆分和显式 overlap layer；SRT/ASS/manifest 同目录原子替换。`tests/test_speaker_turn_overrides.py` 为 `10 passed`，覆盖 source drift、边界缝隙、删除、重叠层、清单计数与 hash。真值文件是 `docs/reviews/2026-07-10-speaker-color-v10-overrides.json`。
- **交付**：本地 ignored 目录 `lidousha/2026-07-09/说话人分离实验/v10_最终定点验收/` 内有全片 MP4、SRT、ASS、manifest 和 `qa_v10_contact.png`。源 74 cues → 输出 78；本轮审定输出 15、继承 v9 输出 63、可靠重叠 1、删除源 cue 1，因此 manifest 诚实保留 `fully_reviewed=false`。成片先用黑底字幕带遮掉原视频硬字幕再烧 v10，1920×1080/60fps/AAC、158.000s；全片 ffmpeg 解码零错误，重点帧视觉检查通过。MP4 SHA-256=`0728f8a4968c05e057ca2df9237608234171e4c501d23d4819ed45d7c948ef32`。
- **进行中/阻塞**：无。63 条非定点 cue 仍是 v9 继承标签，不冒充全片人工真值；本轮已知 tricky 点均闭合，不要求 Ivan 再全片听一遍。若以后发现新错句，继续向同一 override 真值文件追加人工裁决即可。
- **下一步**：保持 no-upload，不部署到 `free:/opt/bilive/autoslice/repo`；只有 Ivan 明确要求发布或产线集成时再做相应 gate。

**2026-07-10 声纹分离 v8 二分色+已校正专名（已被 v10 接手；v9 亦不合格）**：Ivan 批 v6 两点：demo 专名没校正（问了两轮才做，教训）、「为什么」明明语境+声纹都该是李豆沙却错。修复：①**专名校正进 demo**——生产同款 AGY 精听（首跑掉 1 条 cue 被时间轴校验拦下 fail-closed，重试成功），李德森/李乐山→李豆沙 清零，语境票也吃校正后文本；②**v6 真凶=连线声纹被毒化**：bootstrap 用"vs 独播声纹低分"挑连线印，但她**激动短句**(哇/为什么/嘿嘿)对平静独播印也低分→她的声音被注进连线印（cue#15「为什么」本身就在连线B组印里！）。修复=连线印三重净化：低 seed+**时长≥2s**+不像她的会话声纹(≤0.42)；cue 窗口钳制到邻句边界防串音。③净化后「为什么」仍错（margin −0.26，音频自信地错——1.1s 激动短问句的声纹 embedding 本身不可靠）→ **最终规则：<1.5s 短句永远进语境投票，无论 margin 多强**（短句恰是语境最能判的："为什么"就是对"当聋人最难"这条评价的回应）。v8 四靶点全对：#6 音频强判(+0.33)、#15/#19/#20 语境票判回。代价：42/74 句进语境层（音频只对长句强判）——验收重点=语境票的错误率。demo `lidousha/2026-07-09/说话人分离实验/说话人分色v8_二分色_已校正.mp4`（srt 带 margin）。脚本 free:/tmp/diar_v8.py。环境坑：ssh+nohup 后台跑 venv 推理会无声消失（无 OOM 无 traceback），**前台 timeout 跑最稳**。

**2026-07-10 声纹分离 v6 二分色（已被 v8 取代）**：v4 后 Ivan 再报两句她本人的话被判连线（cue#19「哇，李豆沙哪里分不清上下」0.49 / cue#20「而且这不是对李豆沙来说」0.51——都贴着阈值的浑水区），并问：专名怎么还没校正？先校正再借语境分离会不会更好？v6 三层架构（采纳其语境思路但放在正确位置）：**①会话自适应双侧声纹**——用本片高置信句(≥0.72)建"她的联动声纹"、低分句(≤0.35)自动分成两组连线声纹（关键发现：两位连线音色不同，单组 guest 印全采到一人，另一位把中间带搅浑=v5 guest 中心 0.015 的根因），margin=vs她−max(vs连线A,vs连线B)，同信道对比锐利得多；**②强 margin 音频直判**（57/74 句）；**③浑水区（|margin−阈|<0.1，17 句）交 CPA 语境投票**（gpt-5.6 链，17/17 成功；坑：cpa.env 是 export 格式要剥前缀）。结果三靶点全对：#6 音频强判(+0.41)、#19/#20 语境票判回李豆沙。全片 33/41、切换16。demo `lidousha/2026-07-09/说话人分离实验/说话人分色v6_二分色.mp4`（srt 带 margin 分）。**专名答复**：实验烧的是裸 BCUT（李德森/李乐山=李豆沙错听），产线集成顺序=BCUT→AGY/CPA校正→逐句声纹→浑水区语境票（吃校正后文本）→分色烧录，正是 Ivan 建议的"先校正"。脚本 free:/tmp/diar_v6_binary.py。

**2026-07-10 声纹分离 v4 二分色（已被 v6 取代）**：Ivan 判 v3 连李豆沙本人的句子都错（实锤 cue#6「就这三个里面哪个是最复杂的」被标连线A）、拍板**只上二分色但要修流程**。v4 架构变更：**彻底绕开 diarization 聚类**——每条字幕音频（pad±0.2s、短句扩到≥1.2s）直接 vs 3 个李豆沙声纹打分（CAM++ SV 均分），2-means 自动定阈（本片 guest≈0.38 / 李豆沙≈0.67 → 阈 0.53）+ 滞回填充 + **带置信度守卫的平滑**。关键教训（首跑抓到）：**盲平滑会把"两句连线中间她插一句"的正确标签抹掉**（cue#6 声纹 0.805 判对了却被平滑翻错）——平滑只许动 |score−阈|<0.10 的低置信孤立句，强证据永不覆盖。修后 cue#6 正确=李豆沙，全片 李豆沙32/连线42、切换16次。demo：`lidousha/2026-07-09/说话人分离实验/说话人分色v4_二分色.mp4`（.srt 带每句得分，供核对）。已知风险：0.42-0.61 中间带 ~20 句是浑水区（短句/抢话/游戏音效），错误会集中在那里；若仍不过关，下一杆=demucs 人声分离预处理后再打分。产线集成待验收后做（接进 produce 链默认关、联动场景开）。

**2026-07-10 声纹分离实验 v3（多声纹+平滑+pyannote对比，已被 v4 取代）**：Ivan 判 v2 仍差，问多声纹是否有用。v3 改动：①**多声纹 enroll**（3 个不同 7/6 独播片段取平均分，李豆沙 0.892 vs 连线 0.51/0.63，区分度更净）；②**时间平滑**（孤立单句换色吸附邻居，切换 26→14 次）；③**pyannote-3.1 对比**（独立 venv-pyannote 解依赖打架：funasr 要新 huggingface_hub、pyannote3 要旧；403 真因=pyannote4 内部重定向到未授权的 community-1 仓库，非 token 问题——token 是 aierlma 的 read token，3.1/segmentation 权限都在）。**结果**：pyannote 在此素材**退化**（65/74 全归一簇——三相近女声+游戏音效聚类塌缩，num_speakers=3 也没救）；CAM++ v3 结构合理（30/33/11）；两法仅 34/74 一致=素材本身难。另做**二分色变体**（只分李豆沙 vs 其他——0.89 锚点是唯一强信号，回避连线互认错误）。三个 demo 在 `lidousha/2026-07-09/说话人分离实验/说话人分色v3_{campp,pyannote,二分色}.mp4`。若 v3 全部不过关，剩余路径：人声/伴奏分离(demucs)预处理、或只上二分色、或放弃。venv 留 free（venv-diar/venv-pyannote），脚本 /tmp/diar_compare_v3.py + /tmp/pyannote_segs.py。

**2026-07-10 声纹分离实验 v2（CAM++，已被 v3 取代）**：free 建 `venv-diar`（uv+py3.11+funasr/pyannote，CPU）。**CAM++ 结果**：李豆沙声纹 enroll（7/6 独播成品 60s）匹配 **0.90**（另两簇 0.51/0.67）；`oracle_num=3` 钉簇（不钉会 3人拆6簇）；73 条字幕分布 李豆沙31/连线A30/连线B12/?1；158s 片段 CPU 59s（nice -10，配合 free 能力）。**demo：本地 `lidousha/2026-07-09/说话人分离实验/说话人分色v2_campp.mp4`**。**pyannote 403**：HF token 有效但账号未在 `hf.co/pyannote/speaker-diarization-3.1`+`hf.co/pyannote/segmentation-3.0` 接受条款——Ivan 点完可重跑对比。验收留意：0.67 那簇声纹分偏高，可能混入少量李豆沙语音。脚本 free:/tmp/diar_compare.py。

**2026-07-10 说话人分离实验（3人联动）——Ivan 验收：不合格，LLM 路径否决**：demo（`lidousha/2026-07-09/说话人分离实验/`）被打回："一塌糊涂，Sumi 一个人说话中途换色"。**根因（Ivan 指出的坑）：画面没有说话人线索，只能靠音色**——AGY 声称"从画面锚定、有充分把握"是幻觉式自信，实际靠内容/轮转在猜。结论入记忆 `lidousha-speaker-diarization`：LLM 路径死刑，prompt 调不回来；若再做走专职声纹模型（pyannote/FunASR CAM++ 聚类 + 李豆沙声纹 enroll，她声音跨场恒定可一次建库），需再实验验证，待 Ivan 决定是否投入。烧录侧分色（ASS 三样式）技术已验证可复用。此失败不否定 AGY 精听纠错本职（纠错=听力+上下文，非音色分辨）。**AGY 到期预案**：`gemini_slice_jingting.py --provider gemini` API 直连路径代码现成（GEMINI_API_KEY 即换）；按 2026-07 现价测算月成本（每场5talk+2song≈30min媒体×25场/月）：同档 3.5 Flash≈$22-25、3 Flash Preview≈$8-9、2.5 Flash≈$6、音频优先≈$2-3、Flash-Lite纯音频≈$0.6；免费档可能覆盖（~10次/天，配额需验证）。备选：Groq whisper-turbo 第二耳朵（已接线，$0.04/音频小时≈$0.5/月，无视觉/无diarization）。

**2026-07-10 补充（Ivan 审片支援）**：①7/9 全部 22 条落选候选已裸切预览拉回本地 `lidousha/2026-07-09/落选预览/`（按 conf 降序编号+INDEX.md；stream-copy 裸切开头±2s 关键帧对齐；未做成品，Ivan 过目选中哪条再产）。②conf=0.94 落选根因=7/9 批次跑在 v4 修复前：晚段（22-00）因云盘上传延迟晚进候选池+旧跨段轮转不看分数，早段五条先占满配额——正是审计第 5 条，修复（封场+全局排序）已部署，下场生效。③**说话人分离/分色字幕（3 人联动场景）**：现链路无 diarization（BCUT 不支持）；可行路径=AGY 精听时标注说话人（听音色+看画面）+ 烧录侧 ASS 按说话人换色（技术现成），准确率需拿真实联动片段实验一轮，待 Ivan 点头再开工。

**下一步**：①另一 agent 落地后：按契约改 produce_song 交付语义 + 补交付 7/9《ただそばにいて》(x18) + commit/deploy；②GitHub 发布前：轮换 RECORD_KEY（git 历史含旧值）+ LICENSE/README + 定发布范围（建议只发流水线骨架）；③7/9 旧面遗留的 12 个 hybrid 切片/.jingting backlog 是死数据，可择机归档。

## 2026-07-09/10：外部审计修复轮（"控制面在撒谎"）—— runner v4 + 生产现场急救

**目标**：外部三轮审计（+ChatGPT Pro 终审）判定系统"关键处假绿"：①挂载死了心跳报绿；②BLOCK 记成 ok、0 交付叫 done；③上传授权不绑定最终文件；④talk 边界门是假门；⑤top5 先到先占坑；⑥state 覆盖写+损坏静默清零；⑦dirty tree 部署。**全部意见经逐条代码/现场复核认可**，本轮修复。

**已完成（生产现场急救，2026-07-09 19:0x–19:2xZ）**：
- **P0 live 事故**：CloudDrive FUSE 宿主挂载点 `Transport endpoint is not connected`（clouddrive 16:41Z 自杀重启后挂载传播断裂），`bilive_record` 绑死挂载进 `/app/Videos` → **录制链路当时是断的**，下场直播会全丢。修复序列：lazy umount → 重启 clouddrive2 → 宿主恢复 → 重启 bilive_record → 两侧 create/fsync/rename/read/delete 探针全过 + blrec API 正常。**故障窗口 ~16:54Z–19:15Z，期间每个 tick 均报未开播 → 无直播错过、无需回填**。
- cookie 全部 `chmod 600`（cookie.json / biliup_cookies.json / bak / yt txt）。
- **mount 看门狗**（`scripts/free_mount_watchdog.sh`，free cron */5）：探测 stale/hung FUSE（timeout ls，防挂死）→ 自动修复序列 + 恢复后重启 recorder + `ALERT_MOUNT_WATCHDOG.txt` 报告，30min 冷却防重启风暴。此故障 7/9 一天发生≥2 次，会复发。

**已完成（runner v4，commit 见 git log，342 tests）**：
- **源健康门**：tick 首步探挂载（subprocess ls + timeout，防 FUSE 挂死堵死 tick）；失败 → `ALERT_SOURCE_UNAVAILABLE.txt` + 心跳写 `SOURCE_UNAVAILABLE(...)` + 什么都不跑（fail-closed）。`list_dates/list_segments` 不再静默吞 OSError。
- **状态词诚实化**：歌 rc=0 无交付 = `blocked`（门在工作≠歌交付了）；talk 交付 = `review_ready`，带确定性红旗 = `quarantine`（交付但要人看）；批次终态 `review_ready / review_ready_with_failures / no_delivery`——0 交付永远不叫 done，摘要头新增"交付实况"行 + no_delivery 刺眼提示。旧 state 的 `ok` 兼容按已交付处理。
- **配额=交付**：`song_delivery_budget` 只数已交付；BLOCK 不占坑，按弹幕从结构化 backlog 自动回填（`SONG_ATTEMPT_CAP=6` 硬上限防无限产歌）。
- **全局选片 + 封场**：`prioritize` 改全局按 confidence 降序（metric v3 已嵌在召回 prompt 里），段内软上限 2（不够填满时让位）；**session sealing**——段清单（名+大小）跨 tick 稳定才做选片（7/9 实锤：0.94 晚段候选输给 5 条更早的 0.85-0.90）。晚段/多场同日进入竞争而非白来。
- **确定性边界红旗**（`produce_slice_package.boundary_red_flags`，不用 LLM）：切点后 speech island 续讲 ≥1.5s / 开头截进句中 / 下一句进尾部 pad / 收束句≠最终 SRT 末句 → 写进 boundary_audit + 交付记录，runner 标 `quarantine`。旧测试"允许切点后island续讲5s还叫绿"已改为断言出红旗。
- **state 原子写**：tmp+os.replace + `.bak` 上一版；损坏 → 隔离留证 `.corrupt-<ts>`、有 bak 恢复并持久化、无 bak 写 `state_corrupt_blocked` 标记文件（幂等，绝不当"全新日期"重产重交付）+ ALERT。
- **上传绑定**（`scripts/authorized_upload.py`）：`make-manifest`（审查时冻结 video+cover sha256+标题原文+Ivan 授权原话）→ `upload --manifest`（上传时重算 hash，漂移拒传；幂等账本 `upload_ledger.jsonl`，同视频重传硬拒=防充电器重复投稿复发；uploader 只接 manifest 参数）。free 的 `do_upload.sh` 已换为带门版本（裸调 exit 4），源收进 repo `scripts/free_do_upload.sh`。publish SKILL 已钉上此规则。
- **部署纪律**（`scripts/deploy_free_autoslice.sh`）：dirty tree 拒部署、远端打 `DEPLOYED_COMMIT` 指纹、部署后 md5 校验 runner；本轮起 free 上跑的就是 commit 的字节。

**进行中**：无后台进程。7/9 批次 5 条 talk 已交付待 Ivan 审（历史摘要头的 done 字样属修复前产物，歌切表格本身诚实）。

**阻塞**：无。上传永远逐条授权。

**GitHub 发布预检（Ivan 2026-07-10 提出"之后要发布 github"，本轮先扫了一遍）**：
- ✅ 已修：`lidousha_slice_monitor.py` 硬编码 blrec RECORD_KEY（两处）→ 改为容器环境变量 / free 侧 `.env` 运行时读取，工作区秘密扫描现为 0 命中（SESSDATA/bili_jct 匹配均为字段名非值；GROQ/CPA key 全部只活在 free:/opt/bilive/.env）。
- ⚠ **发布前必做**：①**轮换 RECORD_KEY**——git 历史里仍有旧值（比洗历史便宜：改 /opt/bilive/.env + 容器重建，monitor 已改为读 env 不用再动）；②加 LICENSE + README（现在都没有）；③决定发布范围——整仓含大量运营数据（HANDOFF 运营细节、uploaded.json 证据、李豆沙 persona/词表/选题 metric 等编辑私产），建议只发布流水线骨架（scripts/src/tests + 脱敏文档）或新开 public 仓抽取；④字体资产 SmileySans/ZCOOLKuaiLe 均 OFL 许可，可随仓发布。
- 主机名/IP 未泄露（committed 文件 0 命中）；媒体/大文件未入库（最大是字体 2.5MB）。

**下一步（审计"一周内"项，待排期/待 Ivan）**：①候选池 candidate_pool.jsonl + 封场后全局去重/多样性（本轮全局排序+封场已覆盖大半）；②talk/song 统一生命周期、golden replay（用 7/6、7/9 建回归集）；③审查界面（标题/hook/首尾上下文/红旗/Accept-Reject-Needs trim，落选默认折叠）；④根盘 88% 用量的水位/保留策略（审计正确指出：无背压设计前别上 local-first spool）。核心指标改口径：Ivan 接受并发布的片数 ÷ Ivan 审核分钟。

## 2026-07-07（续二）：《屑屑》歌切误报根因（歌名识别失败）+ 已上传 + 5 条收尾闭环

**目标**：查清《屑屑》为什么老被判"不完整"并**修工作流**（Ivan：不是放宽门那么简单——"歌曲要完整=后奏也要留、按 LRC 走、是屑屑"）；完成 5 条已授权上传收尾。

**根因（研究结论，非"门太严"）＝歌名根本没识别出来**：这次重跑 `song_repair` 的 LLM 猜歌名猜歪（快乐崇拜/穷开心…），歌词行查询也没搜到屑屑，最高才对上 10%（<55%）→ `song_boundary=null` → `full_song_evidence_ready=False` → `run_auto_review_shadow_pipeline` 的完整性豁免段（~1293）跳过 → `content_evidence` 退回**ASR 词重叠 / 歌时长**估完整度 → 而屑屑正是"末句+纯器乐后奏+念白"结构，词重叠必然不足 → `SONG_PARTIAL` 级联 `CPA_SEMANTIC_INCOMPLETE`（豁免需 song_complete=true）。且没走 LRC 边界→后奏（无词）被切。**三个诉求同源：钉死歌→识别成功→证据链成立（误报消失）+ LRC 边界（含后奏）+ 自动 LRC 字幕。**

**工作流修复（commit 5c634df，315 tests，已部署 free，端到端验证 decision=SONG_FULL_BOUNDARY_READY）**：
- **歌名去猜化·指纹钉歌**：`assets/lidousha/known_songs.json`（屑屑→netease 2615403834+指纹句）；`song_repair.fetch_netease_lrc` 按 id 直取；`_pinned_lrc_for_song` 拿窗口 ASR 指纹匹配（≥2 命中就钉，实测屑屑 4/5）塞进对齐池，靠真实对齐率裁定（钉错会被排掉，安全）。
- **后奏保留**：歌末不再切在最后一条 ASR cue，延到下一句念白前（`max_outro_ms` 封顶）。屑屑 3:31→**3:50**，多出 ~16s 无字幕器乐后奏。
- **LRC credit 出字幕的 bug**：`parse_lrc_text` 前缀过滤漏了中段关键词（音乐制作/贝斯演奏/混音、母带），netease 把它们打了后奏时间戳→烧成字幕。改为短冒号头内任意位置匹配 credit 词。屑屑 55→52 行，末行是末唱句。
- 歌一识别成功，烧录自动切 LRC 时间轴（`_write_lyric_timeline_srt`）→ LRC 成字幕权威（没洗≠没醒、high five≠beat）。

**已上传并闭环（Ivan 2026-07-07 授权"标题取为《屑屑》你就可以上传了"+"不用到本地直接传"）**：`【李豆沙】豆沙歌，《屑屑》你`（屑屑=谢谢谐音=谢谢你；歌切标题带"豆沙歌，"系列标记，封面不带）**BV1MgMt6UEGp**（aid 116877569297821/cid 39727268871），biliup 单次 rc=0，封面 right-split 持麦唱歌无吐舌。审核通过后后台 `xiexie_finalize.py` 自动补挂**小李歌唱**(8410735/9364628,code=0)+公开验证 **state=0**。uploaded.json commit 9d5989e、public_verify.json commit 2abaa7a。**发布链完整闭环**。

**5 条已授权上传收尾闭环（commit 3d969c1）**：礼墨CP BV1PHMt63Ez7 / 拉拉学历 BV1PHMt63Epi / 棒棒糖 BV1iBMt68EwS / 15坏女人(奖励) BV1RBMt6bEET / 电脑更新 BV1jBMt68EqL——全部 state=0、入小李切片(8383206/9320779；15坏女人+电脑首触 20111 限速重试成功；现 25 集)。10 份证据已 commit。

**进行中/阻塞**：无。本轮全部闭环（屑屑已发布入集验证、5 条切片已闭环、工作流修复已 commit+部署+测试）。上传逐条授权。可选后续：known_songs 随复现歌逐步补表（现仅屑屑）；歌切边界检测对"唱后念白/间奏"的 ADVISORY 提示仍保守（fail-closed 安全，非缺陷）。

## 2026-07-07（三）：SC 匹配工作流大修（首句谢错人的根因）+ 核听工具 + 提速

**目标**：Ivan 反复纠正——切片首句"谢谢X的SC"老是谢错人，要修**工作流**不是单个切片。

**根因（终于查清）**：她**批量清 SC 积压**，一条 SC 可能过好几分钟才轮到念。拉拉首句她谢的是 **十麻乃orient 在切片前 ~4 分钟(video 353s vs 切片 592s)** 发的 SC「想要成为真正的拉拉，还要经历一番风吹日晒」(切片 2-3 句逐字复读它)。旧逻辑只取窗内最近一条→抓成夏色星川(错，那条是"妈妈晚上好晚安")。CP 首句"跟李豆沙说句恭喜"(读 BlueArxiv 弹幕)被强礼墨Sumi语境改成"礼墨"(自称被带偏)。

**已修（工作流级，313 tests，已部署）**：
- **SC 回溯窗 120s→900s(15min)**(`produce_slice_package.SC_PRE_CONTEXT_MS`)：她拖很久批量补谢，"最近几条"不够；标 `【SC此前·名】`，靠内容匹配从积压挑对的。
- **agy_prompt SC 规则重写**：按发送者/内容匹配、时间只软提示、±10s 对SC不适用、对不上保留音频别硬套。
- **glossary 反向铁律**：自称(李豆沙/小李)绝不改成搭档名(礼墨Sumi/kmx)。
- **熊姐是对的**(她玩熊猫→熊姐梗)，收回误判。
- **核听工具** `scripts/apply_subtitle_correction.py`：BCUT层自动改不了的口播错字，给正确文本→改 srt+重烧 ~20s(不重产/不重封面)。`--set-line N='文本'`/`--replace 旧=新`(surgical)。
- **提速**：`produce_batch` 并行(MAX_PARALLEL_PRODUCE=3)、`--reuse-cover` 只改字幕跳 gpt-image-2。
- **自→白封面通病治本**：`_COVER_ZCOOL_WRONG_GLYPHS={"自"}`，含"自"整张换 SmileySans。

**验证 ✓**：重跑拉拉，工作流自己出对「谢谢十麻乃orient的SC」；CP 用核听工具改对李豆沙。两条 md5 同步到本地无误。

**进行中/待 Ivan**：①《屑屑》歌切 song-ID 已修(对上82%)，但完整性门卡"最后3句LRC无ASR匹配"(她可能没唱完/ASR漏尾)——放宽门 or 按现状交付 or 弃，Ivan 定。②5条上传候选(礼墨CP/拉拉/15个坏女人/棒棒糖/电脑)字幕现已可用；15个坏女人的"点上菜了/姐身子好"等仍是BCUT错听，要不要核听逐条改。

## 2026-07-07（续）：封面字卡放大（工作流级）+ 字幕结合SC + 5条上传候选字幕重审

- **封面字卡太小（Ivan 重复多次的通病，工作流级修复，已部署+310 tests）**：根因=标题带冒号→固定2行→侧分文字区窄(~700px)字被行宽卡小、竖直空间大量留白。修 `_fit_cover_lines`：不再锁死冒号2行，**评估所有行数取填满文字区的最大字号**，并**优先词安全分行**(LLM分行/冒号分句的相邻合并 `_regroup_lines`，在其≥最优90%时选它→放大不拆词)，词安全过小才退均衡器。艺术指导 prompt 加"侧分布局必须多分几行每行更短填满竖区"。实测礼墨CP封面从2行小字→5行大字铺满(被催找/礼墨学/谢礼物/你只是想/磕CP罢了)，礼墨/磕CP 整词。已就地换上 CP 交付封面；流水线修好后**后续所有封面自动变大**。
- **字幕结合画面SC（Ivan 2026-07-07，已部署）**：SC(醒目留言)不在弹幕xml，在 blrec 的 `.jsonl`(SUPER_CHAT_MESSAGE.data.message)。`produce_slice_package._load_superchats` 从 xml 同名 `.jsonl` 取 SC 文本(send_time-最早事件=视频相对时间)，标 `【SC】` 并入弹幕纠错上下文→AGY/CPA 结合她在读的 SC 卡片纠错。实证 SC 提供关键上下文(如"想看李漏右手唱地球大爆炸"=CP里"念到地球大爆炸"的由来；"想看豆沙多线程:被15个小女友拷打…扎针…蜘蛛"=15个坏女人由来)。
- **流水线提速（Ivan 2026-07-07："为什么这么慢"+"并行、只改字幕不重出封面"）**：诊断——单条 5-10min，瓶颈是**网络型 AI 步骤串行**(AGY gemini 看视频 1-3min + 几趟 CPA gpt-5.5 推理 + gpt-image-2 出封面 ~90s)+**源在 123云盘 FUSE 网盘慢读**+**5条串行**。修：① `produce_batch` 用 ThreadPoolExecutor **并行产出**(MAX_PARALLEL_PRODUCE=3，process_date 的 talk/song 循环改并行，title_failed 留 pending 下 tick 重试而非整批暂停)；② `produce_slice_package --reuse-cover` + `_stage_publish_draft(skip_cover=)` **只改字幕时复用已有封面**、跳过 art-direction+gpt-image-2(每条省~90-120s)，`produce_talk(reuse_cover=)` 接线；③ AGY prompt 说明**弹幕/SC已作为文本喂给你、别再去 OCR 糊字，把精力放音频+其他画面**(呼应 Ivan"agy工作降低了")。312 tests，已部署 free。**注**：SEND_GIFT 发送者名脱敏(小***)不可恢复；礼物感谢名认命。
- **SC 权威纠错（通病·工作流级，Ivan 2026-07-07 要求"严谨定义问题再动手"，已部署+311 tests）**：**问题**——她感谢/朗读 SC 的 cue，纯听 ASR 对**发送者名字+朗读内容**错得极多（名字/外语是 ASR 死穴；"谢谢十麻乃orient的SC"听成"谢谢142的SC"，日语おめでとう听成"没得到"）。之前我的 SC 集成只喂了 SC 正文当**软提示**、没带发送者名、AGY minimal-edit 不会替换。**修复方向**（严谨定过）：SUPER_CHAT 的 `user_info.uname` **不脱敏**(全名可取，SEND_GIFT 的 uname 脱敏成"小***"不可取→礼物名认命)，把 **uname+正文** 标 `【SC·<name>】<text>` 喂进纠错，并给 `agy_prompt` 加 **SUPER_CHAT AUTHORITY** 指令：与SC时间对齐的"谢谢…SC/朗读SC"cue，用SC精确名字+原文**覆盖音频**(屏上真值非猜测)；日语插话入 glossary(おめでとう等)。改 `produce_slice_package._load_superchats`(+uname)、`gemini_slice_jingting.agy_prompt`、glossary。**正用改好的流水线重跑5条验证**(十麻乃orient/おめでとう 两个靶点)。
- **5 条上传候选字幕重审（进行中，subagent）**：礼墨CP/拉拉学历/15个坏女人/抢棒棒糖/电脑更新——重跑 produce_talk(默认 bcut_agy_cpa=**新鲜 AGY 精听听音频**修错听 如"熊姐/念到地球大爆炸")+SC上下文+glossary(加 李漏/李露→小李)+大字封面，同 hook 覆盖原交付。**拉拉切片确认已产出**(promo_210019，标题「主播探究拉拉:先五年高考十年模拟」)，在上传集里。歌切不动。subagent 验字幕准确率后回报。

## 2026-07-07：选片 metric v3 + 歌切流水线双修 + 监控修复 + 专名纠错

**目标**：Ivan 反馈①选片 metric 要把百合/CP 提到最高优先级、把两条被落选的百合/CP切片做成成品；②歌切应由 free 流水线自动出（不该我手动）——修流水线；③监控 `com.ivan.lidousha-slice-monitor` exit=1。

**已完成**：
- **选片 metric v3**（`assets/lidousha/slice_selection_metric.md` + 记忆 lidousha-slice-selection-metric）：新增"优先级分层"硬序压过六维——第一层 百合/GL+与具体人物(kmx/礼墨Sumi/露蒂丝)的CP/磕CP/关系("看不腻")；第二层 围绕本人的自证/玩梗("看多会腻")；第三层 hook讲不清趣点的泛泛感慨(降级)。加"hook可读性准入门槛"。**通病**：metric v2 已列百合/关系维度但没定层级，召回把自证(0.86)/泛泛(0.86)排在礼墨Sumi-CP(0.84)之上。已 sync free。
- **专名纠错（Ivan 抓的错，已owning）**：CP切片人名不是"李墨素"——那是 ASR 听错版，真名 **礼墨Sumi**（glossary 早有）。我看到了礼墨Sumi 还另加了个错词条，是我疏忽。已把所有听错版(李默送/李救下/李墨素/李墨素描/刘李慕思维)并入 glossary 礼墨词条→修正为礼墨Sumi，metric/memory/驱动脚本全改，并 SendMessage 纠正在产的 subagent。
- **歌切流水线双修（都已部署 free + 测试 309 pass）**：诊断第2首歌切被门拦的根因=**两层 bug**，非"完整歌不够"。查明该歌是 **《屑屑》(ChiliChill乐团)**，实际可自动交付(ASR 对 LRC 67-71% 行匹配 > 55% 门)。Bug①**anchor 渗入前置谈话**：召回给的歌 anchor(1073s)起点扎进"猪鼻子"谈话，窗口以谈话开头→窗内召回改判 talk→AUTO_RECUT。修：`_song_core_span` 用 ASR 音乐前奏静默 gap 把窗口**前缘**裁到真正开唱处(只裁前缘不裁尾——裁尾会切掉歌末尾间奏后的副歌→SONG_PARTIAL)，`window_classified_song=False` 时自动重试一次(自愈，嘉宾那种干净窗口不受影响)。Bug②**歌名识别失败**：`_build_lyric_queries` 取"3条最长行"当 netease 查询，但最长的恰是 BCUT 糊掉的英文/rap段("chewe now baby")→查不到；改为**取干净的 CJK 密集行做单条查询**(实测干净行"谁说圆满的人生才能算圆满"→netease 首位就是屑屑)。
- **监控修复**（`scripts/lidousha_slice_monitor.py`，Ivan 问"修好了吗"——诚实：之前没动，现已修）：三个 bug——(a)`sys.exit(verdict码)` 让 DEGRADED→exit 1 污染 `launchctl list`(读着像监控自己挂了)，违背"告警只走报告文件"约定→改成**成功运行一律 exit 0**，健康走报告/邮件，旧行为留 `--health-exit` 开关；(b)`audio_missing` 误报根因=音频检查探的是 1GB 裸 `.m4s` 分片，ffprobe 扫它**超时**(rc=124)→异常→has_audio=False→假告警；改为**探已合成的可读 `.mp4`**(ORIG_RX 只配 .m4s 紧凑日期名，加 FINISHED_MP4_RX 配带横线的 .mp4)+**探测失败按"未知"不按"缺失"**。实测现 verdict=OK、audio=aac/LC/2ch、**exit=0**，launchctl 已清。

**进行中（两 subagent）**：①产 礼墨Sumi-CP + 拉拉GL 两成品(已发纠正、用礼墨Sumi重跑)；②用**原始 anchor** 跑 produce_song 复产《屑屑》证明流水线自愈端到端(不再手动)。都验字幕/封面后回报。

**阻塞**：无。上传永远逐条授权。**Codex/mhb 自动化**（6:10am ghostty检查、8am-mhb审计）确认**不在 launchd**(只有 Codex/Ghostty 桌面 app 本身)，属其他项目 Codex 托管，非 vtuber-slice——我没动；另注意到 `com.ivan.prediction.wsl-tunnel` exit=255(prediction 项目，也在挂)。要不要把这些转成 launchd 需 Ivan 定(跨项目)。

**下一步**：两 subagent 完成后验收；屑屑 若自愈交付成功=流水线双修验证闭环；metric v3 下批直播自动生效。

## 2026-07-06 晚：7/6 直播无人值守首个真批次（runner v3 实战）+ 封面自愈

**目标**：CPA 恢复后全新重跑 7/6 批次（产物已清空），验证 runner v3 五项修复真实生效。

**已完成**：
- **批次自动跑通中**：发现 5 talk（信心 0.86–0.92，hook 与选片 metric 对齐）+ 恰好 2 歌切（《嘉宾》x298 沙壁反转 /《今天也过得很愉快》x234）+ 10 落选 + 2 歌备份 + 死段账本捕获 2.9KB 残桩。标题全部真标题（如「【李豆沙】电脑要造反？小皇帝拒绝更新」）。
- **字幕豆腐框根除实拍验证**：pick1 交付 mp4 抽帧肉眼核对 sapphire72 白字宝蓝描边、CJK 字形完整（不再只看退出码）。
- **CPA 图像网关两层新故障，当场修**：① new_api 加了"宽高必须 16 倍数"校验，老请求 1920x1080 整批 400 → 请求改 `1920x1088` + 收图 `_normalize_cover_canvas` 裁回 1920x1080 叠字画布（`run_auto_review_shadow_pipeline.py`，探针实测 400 消失）；② 修完暴露图像渠道断供（500 渠道不存在 / 503 auth_unavailable providers=codex，token 无 gpt-image-1.5 权限）→ **不擅自换模型**（7/4 先例：换模型需 Ivan 拍板），runner 加批末 `repair_covers` 封面专项自愈：交付成品缺封面→用产出侧干净参考帧（`cover_refs/`，不用烧了字幕的 mp4 抽帧）重出，真实失败限 3 次，**通道断供签名不烧次数、跨 tick 无限等恢复**。已部署 free，298 tests。
- 记忆已更新 `cpa-real-ai-cover-always`（16 倍数 + 渠道断供两层故障与应对）。

**批次终态（20:31 free 时间，0 失败）**：5/5 谈话交付（标题全真全过禁词门、字幕抽帧 2/2 验过、边界全 ok_sentence_boundary_cut、hook+信心齐全）；歌切《嘉宾》x298 过完整性门交付（4:41 完整曲、LRC 字幕实拍正常）；《今天也过得很愉快》x234 被门拦（窗口内容被改判 talk，AUTO_RECUT 不交付，fail-closed 正确）；AUTOSLICE_SUMMARY.md 质量达标（含 10 落选+2 歌备份+死段账本）。Mac launchd 每 30 分钟拉回本地 `lidousha/2026-07-06/`。

**封面自愈闭环（21:22–21:29，Ivan 通报渠道恢复后手动踢 tick）**：6/6 全部 attempt-1 修复成功（~70s/张，art direction→gpt-image-2→裁画布→叠字全链），断供期正确走"不烧次数"分支（20:40 tick 实证 10s 内识别 503 并 defer）。抽验 2 张：构图/表情/安全区/hook 高亮/《嘉宾》原子性全过；歌切封面外观随切片（猪鼻+脸颊熊猫都在）。

**进行中**：无。批次全闭环（5 talk + 1 song + 6 covers），Mac launchd 半小时内拉回本地。

**阻塞**：无。

**封面词内断行已修（2026-07-06，Ivan 确认后做）**：均衡分行器 `_wrap_even` 词盲，会把「小皇帝拒/绝更新」「吵闹熊/猫头的」拦腰断词。修法：艺术指导 LLM 新增 `lines` 字段产出**词感知分行**，经 `_validated_cover_lines` 严校（拼接后与文案一字不差+顺序不变+《歌名》和 hook_word 整词同行+行数≤布局上限+单行≤12字）才采纳，否则回退均衡器（永不劣化）；分行优先级 冒号显式 > LLM词感知 > 均衡器。`_fit_cover_lines` 加 `forced_lines` 参，overlay 元数据加 `line_split`/`rendered_lines` 存证。307 tests。**6 张已交付封面就地重排**（复用已存的 `.cover.ai-bg.png` 无字底，按各自 cover_generation.json 的原始 layout/hook/color 重叠字，零图像 API 调用）：电脑「小皇帝/拒绝更新」、嘉宾「吵闹熊猫头的/《嘉宾》」、不想下播「又在幻想全职主播」、黑白小猪均已整词；两条冒号标题（棒棒糖/15个）本就显式分行未动。抽验 3 张：断词消除、构图与人物对位保持、字号更大。

**下一步（待 Ivan）**：①产品语义拍板：top-2 歌切被门拦时要不要顺位补备份歌（本场 x208《地球大爆炸》/x198《宝贝》按字面"弹幕最高的两个为准"未补位）；②歌切封面文案按权威规则不带"豆沙歌"系列标记（记忆 cpa-real-ai-cover-always），非缺陷。

## 2026-07-05：李豆沙 7/5 直播 → 5 条上传-ready 候选切片（本地 no-upload）

**目标**：Ivan 要 7/5 直播上传-ready 候选切片到本地审；主控读全部候选池挑题，subagent 做出片体力活。

**已完成**：
- **5 条成品在 `lidousha/2026-07-05/`**（+ `OPEN_ME.md` + `index.html` 可视化过片）：A 数学讲成兄弟情虐恋(4:20，全场语义分最高)、B 以为小猪结果熊猫(0:31)、C 有没有李豆沙/缩成小点(0:54)、D 联动游戏名/聋子(1:07)、E 一本正经讲丧尸偶像/血鬼舞台(0:54)。每条 video+准字幕(cos/sin/OBS/VIVINOS等专名全准)+围绕李豆沙自动标题+CPA真图 gpt-image-2 封面。**5/5 verify PASS**（无词表泄漏、字幕行合规、含音视频轨、topic-closure 落完整句）。**未上传**。
- 选题：主控读 6 段生产候选池(98候选/57 talk)+jingting字幕+弹幕，观众视角+围绕李豆沙+题材多样挑 5；出片 subagent 各跑一条 `produce_slice_package.py --substrate aggregate_asr --correct cpa`（Ivan 2026-07-04 定的字幕架构）。
- **两个坑修好并入记忆**：(1) spec `remote_media` 必须 free **宿主** 123云盘 路径(`/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/...`)，**不是容器 `/app/Videos`**（脚本宿主侧 ffmpeg 无 docker exec）；FUSE 挂载~36s/小文件极慢；semantic_start/end 对准真实语音起止读 `padded.fresh.srt`，候选 start 含前置铺垫会 fail-close。记忆 `produce-slice-package-host-path-and-slow-mount`。(2) 封面字体 `ZCOOLKuaiLe` 把「自」渲成「白」形（自私→白私），A/D 封面钩子临时改写绕开(自私→还小气、自认→成了)，B站标题保留原字。记忆 `cover-font-zi-renders-as-bai`。
- B/E 初版边界飘（B 拖进无关banter、E overshoot climax），已收紧 semantic_end 重跑（缓存切复用，快）。

**进行中**：无（本节收尾已由 Fable 会话完成 2026-07-06 05:3xZ，Opus 会话勿重复执行）：
- **A(cos/sin) 已发布** BV1QzTS65Enf：单次 biliup rc=0、入小李切片(code 0)、封面换「自私」修正版（**ZCOOL 版实为白私字形**——放大核验过；修正版=得意黑整张，`_cover_evidence/*.zisi-smiley.png`；换封面触发复审后已回 state=0，公开 pic=b73d7446 新版 ✓）。证据 cossin.uploaded.json + cossin.public_verify.json，已 commit。
- **D 重做 ✓** 收束「这就是本周直播直播安排」（试了 3 个目标位：92k→"然后就这样"、95k→蹿到下话题"露总"且自动标题带双自字，最终 92k+钉标题「还没联动，小李先把盲聋哑念乱了」命中理想句）。
- **H 重做 ✓** 收束「也没有阻止这个展开啊」（完整句收题，避开谢SC杂段；比塞到 02:08 更干净）。
- **F2 真由来 ✓** 新增 `小丑鼻子的真正由来_猪鼻不见了.mp4`：19:30 段 06:35–09:13 = 猪猪侠盲盒→VTS 猪鼻不见了→借陆医生小丑鼻代替→"先用这个代替一下吧"收。原 F(生日/活动小丑)保留为续集。全段 BCUT 存 `pull/seg1930.full.bcut.srt`。
- **标题新规**：「直接」全局禁用（Ivan：直呼打咩>>直接打咩）——代码硬门+词库+skill+记忆全链固化，287 tests。
- B/C/E/G 未动。OPEN_ME.md 已补 2026-07-06 节。

**阻塞**：无。上传永远需 Ivan 逐条授权。

**下一步**：
1. **Ivan 审 `lidousha/2026-07-05/`**（浏览器开 `index.html` 过片）。
2. **封面字体 `自→白` 治本**（通病应入代码，勿单例补丁）：给 `scripts/run_auto_review_shadow_pipeline.py` 的 `_overlay_lidousha_cover_title` 逐字渲染加「自」→回退字体映射（现有"缺字回退"对字形错的「自」不触发），或整体换 `自` 正确的快乐体系字体（仍 fail-closed）。
3. 可选第二批：日语歌《男も女も恋してるべき》(20:30 段，走完整曲 LRC 全局位移 lane) + 备选 talk（米老鼠版权/被乌鸦俯冲/百合是工作/回不了家/木马/人设太帅，见 OPEN_ME）。
4. 未提交改动：本轮 `reports/lidousha-autoslice-20260705/`(specs+verify脚本)、`lidousha/2026-07-05/`(成品+OPEN_ME+index.html) + 之前所有未提交改动（Ivan 未让 commit）。

## 2026-07-06 runner v3（无人值守首跑复盘修复，Ivan 验收打回后重造）

首跑五宗罪→根因→修复（commit c8fbd31，292 tests）：标题全是cid（CPA gpt-5.5/5.4 provider 整段断供，静默回退）→ **CPA 健康门**：断供即 paused_cpa_down 推迟批次、恢复自动从 pending 断点续产，标题失败的成品不交付（清理重试≤3次），llm_via_cpa.sh 加 5.5→5.4 failover；字幕豆腐框（free 零 CJK 字体，ASS 指名微软雅黑）→ free 已装 fonts-noto-cjk（烧录实测字形正常），runner 启动自检；摘要不知所云→重写（每条：标题/选片理由hook/信心分/收束句/边界/封面；歌：弹幕数/门判定/原因码；含落选清单+死段清单）；唱4+首只出1首→召回帽3→4+确定性演唱检测补充歌队列，最终按弹幕 top2；2.9KB 录制残桩每10分钟无限重试→桩过滤+BCUT失败2次入死段账本。交付文件名改用 hook 可读名。**7/6 产物已全域清空**（free 工作区/交付/缓存/state+本地拉回件），CPA 恢复后 cron 自动全新重跑（门控已实战验证："CPA chat lane down — batch deferred"）。

## 2026-07-06 第二轮收尾（Fable 接管续）

- **已发布 +2**（均单次 biliup+入小李切片+公开验证+证据 commit）：A cos/sin **BV1QzTS65Enf**（自私修正版封面）；D 联动 **BV1uCTS6kEuf**（Ivan 定稿标题「过期（？）女大终于迎来再次联动」，礼墨/安晚awa 词表闭环重出）。
- **staged 不上传**：百合长片 `yuri_full_arc_2000`（10:56 三段拼接，标题带《我在意的人不是男生》，百乃工/百破图已修，封面大字钩子）；F2 小丑由来（「猪鼻不见了，小李只好借小丑鼻顶上」）。旧 G/H/生日F/Opus重复由来片 → `_superseded/`。
- **规则升级**：标题禁词扩到 直接/当场/秒X（代码硬门+词库按 Ivan 真实标题重建+示例清洗）；作品讨论类标题必须带《作品名》；对话提炼的工作流偏好固化进 publish SKILL「Ivan 工作流偏好」节 + 记忆 ivan-implicit-workflow-preferences。
- **修的 bug**：llm_client 命令超时未包成 LlmCallError 会炸 produce（11 分钟长片踩到）→ 已修+测试；校正 CPA 超时 180→600s。288 tests。
- **待 Ivan**：①「牡丹原作者/百牡丹」正确写法（未确证未入词表）；②已上线 A 标题含「当场共鸣」（禁词规则前上传的），要不要热改。

## 目标（2026-07-04）

无人值守/半自动的李豆沙直播切片流水线。本轮核心：Ivan 审 7/3 竖屏直播切片后给出多类字幕/标题/构图反馈，
要求**把每类反馈当成流水线通病在代码里修掉、并固化进单一权威**（绝不做单例修补）。
关键洞察（Ivan）：**每个 prompt 都只带了本轮反馈的几条规则，没带项目历史积累的全部原则——prompt 不够强，且这个病可能在多处**。
7/3 六条成品在 `lidousha/2026-07-03/` 待审。

## 已完成（2026-07-04 本轮）

1. **字幕校正原则单一权威化（治本）**：新建 `assets/lidousha/subtitle_correction_principles.md` = 字幕文本校正唯一权威，
   收敛了此前**只活在代码提示词里**的操作规则（幻听置空删除、SC价格忽略、花体字会误读别盖词表、口误保留、咳嗽删语气词留、
   时间配对±10s、歌切文本以LRC为权威、人工订正即真值、自称误听/kmx 元规则）。
   改 `gemini_slice_jingting.glossary()` → 返回 `术语表+全量原则`，**所有**校正提示词一处改动全局同步；
   CPA 校正/裁决删掉重复硬编码换指针；`glossary.txt` 瘦身为纯术语表；`sync_lidousha_assets.sh` 增推 principles。
2. **BCUT×AGY×CPA 裁决框架修正**（Ivan：BCUT 很准只专名弱，AGY 只补专名/同音字，不采纳 AGY 对普通措辞的改写）。
3. **P0 标题（subagent T）**：`title_style.md` 回灌全量权威；LLM 自动标题强制 12–30字/【李豆沙】前缀/7违禁词命中有界重试；
   **人工标题铁律直通**。**离谱**改两用词（只禁"X到离谱"后缀，留"越看越离谱"）。**P2 封面**注入 persona 身份。
4. **P1 术语门（subagent Q）**：新建 `scripts/lidousha_glossary_terms.py` 动态解析 30 canon+34 误听黑名单注入 judge，
   `terminology_ok` 不再空判（此前只喂 kmx 一词）。
5. **竖屏→横屏 pillarbox**：`_burn_preview_subtitles` 检测竖屏，模糊侧填充到 1920×1080。
6. **词表学习闭环验证（§十三）**：哄睡妈妈 kmx"给我总弄上妈妈了"声学不可恢复 → 加 glossary 种子 → 重出得"kmx就叫妈妈了"。
7. **通病·篇章级代词消解**：Ivan flag"他→TA（同学性别未知）"通用校正做不到（规则被长prompt淹没）→ 加专职 `_cpa_pronoun_ta_pass`
   （模型只判 cue 编号、代码确定性替换 他/她→TA、带 其他/他们 守卫、区分匿名未知 vs 具名已知如司马懿、门控+重试+fail-open）。
8. **CPA 用对 gpt-5.5（Ivan 纠正）**：根因——gpt-5.x 是原生 **Responses-API 推理模型**，我一直错用 `/chat/completions`（gpt-5.5 因此 503、gpt-5.4 空 content）。
   改 `scripts/llm_via_cpa.sh` 走 `POST /responses` + `reasoning.effort=medium` + max_output_tokens 16000 + 5 次重试（上游间歇返回空 reasoning-only 响应）。
   实测长prompt 8/8 稳定。一处改动，所有 CPA 任务（字幕裁决/代词/标题/艺术指导）升 gpt-5.5(medium)。硬约束：只走 CPA、不直连 sudocode、不动 CPA 服务。见记忆 `cpa-gpt5-responses-api`。
9. **测试**：全量 `pytest tests/` → 282 passed（含下条封面改版 +5 用例）。
10. **封面改版（通病·点击率，另一会话主线，真 gpt-image-2 验证）**：旧"居中人物+单色标题横条+大留白"千篇一律 → 改成
   **大头胸像怼一侧 + 半屏多彩艺术字 + 波普/场景背景填满**，按切片轮换布局、表情贴角色、外观随切片真实皮肤。全落
   `scripts/run_auto_review_shadow_pipeline.py`：新 `_lidousha_cover_art_direction`+`LidoushaCoverArtDirection`（确定性基线
   `sha256(candidate_id)` 轮换 layout/hook + 关键词角色→表情映射，可选 CPA judge 精修 **fail-open+护栏**，护栏丢弃吐舌/性感/挑衅）；
   改写 `_lidousha_cover_prompt`（四布局 left/right-split/banner/song-clean + 表情 + 背景池谈话忙歌净 + **外观随参考帧不锁服装** +
   **永不吐舌**，保留 panda/小李/熊猫/16:9）；改写 `_overlay_lidousha_cover_title`（**只靠粗描边**托白字=参考图不加盒子(深色卡已停用=难看方框)、**按段选描边**修白字糊、
   多色钩子词高亮色池轮换不含cream、分区自适应字号，ZCOOLKuaiLe fail-closed 不动）。新 CLI `--cover-art-direction-llm-command`，
   `produce_slice_package.py`/`run_full_session_selector_cpa_shadow.py` 已接线（art-direction LLM 复用 `llm_via_cpa.sh`，即走 gpt-5.5/responses）。
   契约守住：真 gpt-image-2 无字背景 fail-closed 禁抽帧，`cover_generation` 的 `model/method/fallback_used/ai_background`+`cover_status` 键不动。
   同步 `persona.md`(加"封面表情/角色映射"节)+`bilive-autoslice-publish/SKILL.md` 封面节+两 docs。验证 `lidousha/2026-07-04/_封面改版评审/round3/`
   4 张真图：**3 套不同皮肤**(0702蓝棉衣/0703 JK/0629熊猫兜帽演出裙)各随其参考帧=外观随切片证据，表情贴角色无吐舌，四布局+繁简背景对比。
   记忆见 `lidousha-cover-redesign-halfbody`、`cpa-real-ai-cover-always`（含 CPA 图像模型 gpt-image-2↔gpt-image-1.5 会掉线的实测）。

**7/3 交付（`lidousha/2026-07-03/`，全部 1920×1080）**：哄睡妈妈(kmx✅/贡丸删✅/虫儿飞留✅ Ivan确认真实回应/再睡✅)、
称呼大战(倒反天罡✅/kmx✅/SC谢✅ + 定稿标题"叛逆小李一定要喊kmx妈妈，kmx只好喊宝宝")、熊猫伪装、奶龙斗虫、直播腔(写非说✅/TA)、
虫儿飞歌(切片不动，标题→"温柔《虫儿飞》清唱甜美哄睡"，封面已换字)。

## 已发布（2026-07-05，Ivan 逐条授权，biliup 各只跑一次，全部 state=0 + 入合集 + season_display=True）

- 称呼大战 **BV1tdMT64E47** → 小李切片
- 虫儿飞 **BV1CWMT6EE6c** → 小李歌唱（豆沙歌，round3 温柔清唱封面 + pillarbox 横屏）
- 哄睡妈妈 **BV1yWMT6EEHF** → 小李切片
- 直播腔 **BV1yWMT6EEWD** → 小李切片
封面均用最新流程 `scripts/regen_covers_latest_flow.py`（只重出封面不动字幕/标题）。证据存各 `<cid>.uploaded.json`。
上传经 `free:/opt/bilive/app/tmp_manual_upload/`（**不是** `upload.sh` 那个会投稿删文件的守护进程）。
合集 API 偶发 20111「编辑过于频繁」→ 隔 15s 重试；新稿 state=-30 审核中时不能入集，等 state=0。

## 已完成（2026-07-05 Fable 复审轮：审阅 7/4–7/5 全部 Opus 会话的指令与改动）

逐条对照 5 个会话（B站字幕研究/BCUT接入/通病大修/封面改版/7/5切片）的用户指令与落地代码。
**结论：架构与决策基本合格予以保留**（单一权威原则文件、glossary() 组合加载器、三段式字幕、代词专职 pass、
llm_via_cpa.sh /responses+UA+重试、标题强制+人工直通、封面新系统+feed安全区、上传纪律），282 tests 复跑全绿。
**复审修正的问题**：① free 资产漂移（词表旧版+principles 缺失）→ 已 sync+md5 核对；② `free_asr_client.py`
AUTO_CHAIN 仍含已下线的 kuaishou（与文档/对用户报告不符）→ 移出 auto 链（实现保留可显式调用）；③ skill 安全区数字
漂移（写 320/1600，代码实为 260/1660）→ 修正并注明教训；④ title skill「离谱」缺两用词标注 → 补齐；⑤ 删死代码
`_wrap_cover_title_lines`（旧冒号时代包装器）+ 修 `_overlay_lidousha_cover_title` 过期 docstring（还写着深色字卡）；
⑥ `bili_replace_covers.py` 从会话临时目录入库到 `scripts/`（换封面是复发需求，工具不能只活在 /tmp）。
**留档不改的历史事实**：充电器曾重复投稿（BV1WEMA6TES6，Ivan 已删）；诊断期一次直连 sudocode 上游+key 进过命令行
（已被 Ivan 立规禁止，repo 无残留，llm_via_cpa.sh 有 NEVER bypass 注释）；7/5 旗舰用了 `--correct cpa` 而非默认三段式（续跑时纠正）。

## 进行中

- **[Claude 封面会话·已完成 2026-07-05]** 全账号(mid 55006782)**24 条李豆沙切片封面全部按新流程重做并已替换上线**(只换封面,标题/标签/合集未动;24/24 `COVER_UPDATED=True`)。含大字自适应(多换行+均衡分行)、feed 4:3 安全区(文字 x260–1660)、缺字整张换字体(镚→得意黑)、去方框/去 baked-text/永不吐舌/外观随切片(老切片无本地源→用其当前线上封面当参考帧)。换封面脚本已入库 `scripts/bili_replace_covers.py`(在 free 上跑,inspect→apply 两段,只改封面) + `scripts/regenerate_lidousha_cover.py`(出图);账号里少前2游戏视频非切片未动;规则固化进 `bilive-autoslice-publish/SKILL.md` 封面节 + 记忆 `lidousha-cover-redesign-halfbody`。**遗留**:含长英文串(shadowlee)/超长标题的封面字号有物理下限,要更大需把封面文案缩成钩子短句(待 Ivan 定)。
- ~~7/5 直播切片轮·被会话切换中断~~ **已被 Opus 会话续跑完成**（见顶部「2026-07-05」节：5/5 交付 verify PASS）。遗留提醒仍有效：该批字幕用的 `--correct cpa` 非默认三段式 `bcut_agy_cpa`（Ivan 定的架构是三段式；上传前如对专名有疑虑可按默认重出）；封面「自→白」字形坑待治本（见该节下一步2）。
- **[无人值守自动切片 runner·已部署 2026-07-06 (Fable)]** Ivan 目标落地：**直播结束后 free 自动跑完整产出链，无需人开口**（上传仍关）。`scripts/free_session_autoslice.py` 部署在 `free:/opt/bilive/autoslice/`（repo 副本+cpa.env(600)+state/cache/logs/reports），**cron 每 10 分钟** flock 单飞 tick：blrec API(带RECORD_KEY) 判在播→在播/API失联一律跳过（fail-safe）；下播→逐新段 BCUT 转写→CPA 语义召回(带弹幕突发hints，LLM挂了退确定性兜底不静默)→**谈话 top5**（跨段轮转）逐条 `produce_slice_package --ssh-host localhost`（默认三段式 bcut_agy_cpa+真图封面）→**歌切每场至多2个、按窗口弹幕量最高排序**（Ivan 2026-07-05），走 `run_full_session_selector_cpa_shadow` LRC lane（anchor±180/150s 窗，完整性门 fail-closed，只交付 AUTO_UPLOAD 判定的）→交付 `repo/lidousha/<date>/`+`AUTOSLICE_SUMMARY.md`+报告文件。**杀开关** `touch /opt/bilive/autoslice/DISABLED`；7/1–7/5 已预认领（manual）不会被重跑；失败不自动重试（防 retry storm）。Mac 端 launchd `com.ivan.lidousha-autoslice-pull` 每 30 分钟 rsync 交付+报告回本地 `lidousha/`+`reports/slice_monitor/autoslice_free/`。冒烟：talk 全链 rc=0（cut→BCUT→AGY(ssh localhost)→CPA→烧录→真图封面→交付）✅、语义召回 lane ✅（选出的候选与 Opus 人工选题重合）、**歌切全链 ✅**（虫儿飞靶：窗口判 song、netease LRC 匹配 92.3%、门判 AUTO_UPLOAD、标题"温柔《虫儿飞》哄你睡觉"、成品+封面自动交付；Mac 睡眠期间 free cron 独立跳 17 tick=自主性证明）。歌切两坑已修：①窗口余量必须小（anchor−15s/+20s，宽了窗内召回会改判 talk）；②**QA 判官曾以"翻唱版权"BLOCK 合格歌切（0.74 分）——已校准口径**：翻唱是频道常规内容（24 条上传里 7 条豆沙歌），unsafe 维度只管隐私/引战/平台违规/第三方画面（`cpa_semantic_qa_llm.py` + 测试）。**选片 metric 已资产化**：`assets/lidousha/slice_selection_metric.md`（Opus 会话 v2 校准 + 24 条已上传标题提取的偏好 + 熊猫伪装/奶龙拒发反例）注入语义召回 prompt（回退链），sync 推 free，286 tests。为此 free 配置了 root 自我 ssh(ed25519)。记忆 `free-unattended-autoslice-runner`。
- 无本会话后台进程（free 上只有常驻 cron runner + 既有 jingting daemon）。

## 阻塞

- 无硬阻塞。上传永远需要 Ivan 逐条授权。
- 重复投稿 BV1WEMA6TES6（充电器）需 Ivan 在创作中心手动删（API 删档要验证码，无法 headless）。

## 下一步

1. **熊猫伪装 / 奶龙斗虫：Ivan 决定不发**（2026-07-05）。7/3 已发 4 条（称呼大战/虫儿飞/哄睡妈妈/直播腔），本场收工。
2. ~~sync 推 free~~ **已完成（2026-07-05 Fable 复审时执行）**：复审发现 free 上词表停在 7/4 19:43 旧版（缺 kmx 主语误听种子）、`lidousha_subtitle_principles.md` 从未推上去——已跑 `sync_lidousha_assets.sh`，三个文件 md5 与本地一致。教训：以后跑 sync 别 `>/dev/null 2>&1` 吞错，跑完 md5 核对。
3. **7/5 直播切片续跑**（见「进行中」，另一 Opus 会话在跑，别撞车）：A 重出封面+按默认 `bcut_agy_cpa` 复核字幕，B/C/D/E 按 spec 产出，全部拉到 `lidousha/2026-07-05/` 给 Ivan 审。上传永远逐条授权。**"仙童数学"已核实**（2026-07-05 Fable）：BCUT 声学层与 free 侧 ASR 两路独立听到 xiāntóng shùxué，上下文=她模仿数学UP收尾口播的自称，Ivan 确认写法"仙童数学"→已入 glossary（黑名单：先童/神童/线童数学）并 sync free，成品字幕现状即正确。
4. **封面改版验收**：Ivan 看 `lidousha/2026-07-04/_封面改版评审/round3/` 4 张 → 定稿后决定 commit。persona.md 目前只在 repo（sync 脚本不推它，free 端封面不消费它，无需推）。
   可选小调：`produce_slice_package.py` 人工标题时不跑 LLM 艺术指导（走确定性基线，仍贴角色）；要人工标题也精修就把 `art_direction_llm` 提出 `if not given_title`（一行）。
5. **commit 规矩（Ivan 2026-07-05）：授权上传的内容必须 commit**——已执行，commit `7dcfdfa`（84 files：全部管线代码/skill/资产 + 9 份 `*.uploaded.json` 上传证据含追溯补记的充电器 + 24 张换封面状态证据；`.gitignore` 已加 `!reports/**/*.uploaded.json` 例外；媒体不入库，hash 在证据里）。以后每次授权上传后：写 uploaded.json → commit。见记忆 `authorized-upload-must-commit`。

## 2026-07-09 封面 release gate 与歌切交付绑定

### 目标

`local_prepare` 必须先过 release gate 再生成切片封面；整段录播默认不生成 AI 封面；autoslice 歌切先过最终 release gate，再做封面并交付。

### 已完成

- `scripts/run_auto_review_shadow_pipeline.py` 已把 publish/title/cover staging 移到所有 review decision merge 之后；先写 `slice-cover-release-gate.v1`，仅最终 `AUTO_UPLOAD`、无 reason code、recut 已 materialized 才调用封面路径。
- free 的 `/opt/bilive/app/src/upload/local_prepare.py` 已部署：非 `.flv` 整段录播 cover policy=`none`；切片必须同时满足 Jingting done、最终 AUTO_UPLOAD、无 reason code、AGY provenance、would-upload marker 与 pre-cover artifact hashes 才调用 cover。
- `scripts/free_session_autoslice.py` 不再 glob 目录取旧 video/cover；只读取当前 summary record 的 gate-bound 路径，校验 video/cover SHA-256，拒绝 symlink/改写文件。歌切自动 cover-repair 也要求当前记录 `AUTO_UPLOAD`、无 reason code、`release_gate_satisfied=true`。
- 已部署到 `free:/opt/bilive/autoslice/repo/scripts/`，本地/远端 SHA-256 一致。`local_prepare` 容器进程加载的文件 hash 与部署文件一致。
- 验证：仓库全量 `345 passed`；production local-prepare 关键分支 `6 passed`，包括整段录播 no-cover、仅 `.flv` 扩展不能授权 cover、blocked slice no-cover、合格 gate 只调用一次 cover。

### 进行中（含后台进程）

- free 的 `python -m src.upload.local_prepare` 在最终核验时发现原启动子进程已退出（日志无 traceback，主机同时有另一套 blrec watchdog 操作）；本轮于 20:23Z 恢复为 PID 717409，并跨过完整 2 分钟空队列轮询周期仍正常，运行中源码 hash 与受测文件一致。autoslice 仍由既有 cron 每 10 分钟触发；未改 compose 或并行 blrec watchdog。

### 阻塞

- 无硬阻塞。手动 operator cover 工具仍可显式重做已交付封面，不属于无人值守自动 gate policy；上传仍保持关闭并需 Ivan 明确授权。

### 下一步

- 下一次真实直播歌切时检查对应 `*.cover-release-gate.json`、summary 中 artifact hashes 与交付文件 hash；如 gate BLOCK，应看到 `SKIPPED_RELEASE_GATE` 且没有新的 AI cover 调用。

## 2026-07-10 成品二分离流水线与逐人分离实验

> **更正／已被后续全量任务取代**：`shadow` 是李豆沙的自称，不是第四位说话人。7 月 9 日实际参与者只有李豆沙、礼墨 Sumi、安晚 Awa。oracle=4 实验的参与人数前提错误，全部逐人身份与颜色结论已撤回，不再继续该实验。

### 目标

把 `promo_210025_643_801` 的人工说话人真值修进正式流水线，严格按“文本/专名/代词/人工终稿 → 说话人 → 分色 ASS → 烧录”生产成品；随后曾尝试逐人实验，但其把 `shadow` 错当第四位说话人，实验结论已撤回。全程 no-upload。

### 已完成

- 工作分支 `codex/speaker-final-pipeline` 已到 `f8f7a55ce4855b0c3177bbd326125384cd7b0c13`，事务化部署在 `free:/opt/bilive/autoslice/repo`；live frozen tree/stamp/cron/locks/external scripts 均经独立核验。全量 `535 passed`。
- `speaker_finalizer.py` 现支持 CAM++ 二分类、hash-bound 文本/媒体/自动标签、accepted context baseline、显式 split/drop/overlap override、分色 ASS 与 fail-closed manifest。已验收片的 43 个模糊上下文判断被冻结为 pre-override baseline；自动标签任一漂移都会在应用人工 override 前阻断。新片无 override 时仍走原自动上下文流程。
- 生产重跑 `free:/opt/bilive/autoslice/out/acceptance/speaker-final-20260710-f8f7a55`：context call `0`，43/43 baseline 命中，unresolved `[]`；text `63438b34…`、automatic `c13e178f…`、final SRT `41b2ea5f…`、ASS `cd2bfce…`。显式人工输出 25、accepted-context 输出 31、overlap 1。
- 最终烧录 MP4 `f9c02879…`，1920×1080/60fps/165.066667s，完整解码通过，烧录前后 decoded PCM MD5 同为 `eb2e38b…`。12 个关键点视觉 QA 通过，包括“她”、礼墨→李豆沙换色、礼墨笑→李豆沙、安晚“暂时”上层抢话、礼墨“最难的还是聋人啊”和结尾安晚→李豆沙。
- 本地成品包：`/Users/ivan/Project/vtuber-slice/lidousha/2026-07-09/说话人分离实验/v11_成品说话人分离/`；`final-package.record.json` SHA `79935f5b…`，本地/远端逐文件哈希一致。没有 AUTO_UPLOAD/publish marker、没有 uploader 进程、没有上传。
- 逐人实验包：`/Users/ivan/Project/vtuber-slice/lidousha/2026-07-09/说话人分离实验/v12_逐人分离实验/`。包含：只使用 Ivan direct identity 的“已确认版”；oracle=4 的“四簇候选版（非成品）”；以及 10 个无身份提示的短盲听样本。两视频完整解码、音频 PCM 与源一致。

### 进行中（含后台进程）

- 无本轮后台进程。生产 autoslice 只保留原 cron；逐人模型及其下载缓存没有接入生产代码、profile 或 runner。

### 阻塞

- 逐人身份不生产化：除样本不足与错误分簇外，更根本的问题是 oracle=4 把李豆沙自称 `shadow` 误当第四人。四簇对照会把李豆沙“啥意思啊”的开头归入礼墨簇，并拆错“我真的分不清”。production 固定为“李豆沙 vs 其他”。

### 下一步

- 按 Ivan 2026-07-10 的决定停止逐人分离实验；`v12_逐人分离实验/` 已标记作废，盲听样本、四簇身份和颜色均不得进入生产或成品判断。
- 任何上传仍需 Ivan 对具体成品单独明确授权；本轮产物全部保持 no-upload。
