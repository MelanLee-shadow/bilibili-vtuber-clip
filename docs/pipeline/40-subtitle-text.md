# 40 字幕文本链

本文件是字幕文本步骤的**分步权威**。入口：`src/autoslice/producer_text_pipeline.py::run_text_pipeline`
（生产唯一调用方 `scripts/produce_slice_package.py`）。

## 阶段顺序（真实调用序）

| # | 子阶段 | 模块 | 作用 |
|---|---|---|---|
| 1 | `_collect_timeline_chat` | `producer_chat_input.py` | 弹幕/SC/礼物/上舰证据装载（XML 与显式绑定 JSONL 各守其权威） |
| 2 | `_transcribe_draft` | ASR 适配器 + `session_topic_authority.py` + `term_boundary.py` | 转写草稿 + 场级话题实体吸收 + 词边界统一 |
| 3 | `_build_entity_verification_context` | `read_aloud_llm_verifier.py`、`entity_audio_verifier.py` | 实体仲裁闭包（人工 override → 朗读 LLM → 音频仲裁） |
| 4 | `_apply_entity_authority` | `self_reference_absorption.py`、`chat_proposals.py`、`subtitle_fidelity.py`、`chat_repair.py` | 自称吸收、弹幕权威修复、数字事实门、音频实体落地 |
| 5 | `_run_final_review` | `final_review_auditor.py` | LLM 终审 + 逐条声学复核路由 |
| 6 | `_finalize_text_evidence` | `subtitle_fidelity.py` 各 guard、`surface_canon.py`、`song_name_pin.py`、`source_subtitle_truth.py` | 语言保持/书名号/标点门、梗词定形、歌名钉、源真值投影（FAILED 即 SystemExit） |

语义修复引擎（专名/方言/语境不合适度）的设计与规则见
[41-semantic-repair.md](41-semantic-repair.md)——那是本步的核心子权威。

## 硬约束

- 上传语义修复永不放行未见证改写：改动必须有 glossary/拼音同音/弹幕/音频见证其一（`subtitle_fidelity.py` verdict 逻辑），否则 revert。
- 实体上下文构建完成后必须生成同一份 hash-bound `.clip-context.json`：绑定 candidate/date、
  官方源 SHA、整片 draft、selection hook、relation/topic、结构化弹幕/SC 与 scoped speech
  memory。终审、声学请求、StoryContract、record 和交付包只能引用验证过的同一 digest；
  payload、candidate、日期、源 hash 或 ledger hash 漂移立即阻断。
- speech memory 只生成候选闭集，`mutation_authorized=false`；必须按 candidate/relation/date
  scope 检索并携带 `candidate_memory_id`。它不能冒充 source_surface，不能进入 glossary，
  即使与误听同音也必须走声学仲裁。片内另一个由同一 ASR 派生的 cue 同样只是相关候选，
  不得作为独立文字证人直接改字。上下文展示用的 `id=` 不是 ID 本体；终审只可在去掉
  **一个**该固定展示前缀后精确命中哈希绑定 ledger 时受控规范化，未知 ID 禁止模糊匹配。
- 音频二听只证明读音，不证明同音人名/称呼的汉字写法：带「小/老/阿」前缀或「神/老师/姐/哥/酱/桑/君/总/宝」后缀的同音换字，没有词表/源真值等文字权威就只回退该换字跨度，同 cue 其余有见证修复仍保留。守卫同时检查 draft 改写跨度本身的人名形态，并只额外容忍 `-n/-ng` 鼻音尾漂移来识别近同音（如 `毁神→绘声`）；不得因改写把「神」一起吃掉就逃过相邻后缀检查。`什么/怎么/为什么/谁/哪里/多少` 等疑问意图族发生变化则整 cue 回退，禁止把逐字字幕改成解释性提问。
- 字母昵称的规范词面与口播读音必须分层：已有 source-backed entity provenance、建议包含
  字母、且**整条 current/proposed 的去标点拼音在折叠相邻口语重启后完全相同**时，声学层
  听到字母名（如 `N→恩`）不得以 grapheme 不同否决 `大N`。该窄门不提供 provenance，
  不适用于普通语义改写、未知专名或发音不等价候选。
- 源真值支持 `replace_cue` / `replace_substring` / `drop_cue`；`drop_cue` 只允许删除被 source-timeline 真值半开区间完整包含的 cue（仅容忍 120ms 编码/SRT 边界漂移）。任何实质性跨界均记 `DROP_CUE_STRADDLES_TRUTH_INTERVAL` 并 fail closed，禁止按“有重叠”整条删除。
- hash-bound 源真值拥有的 cue 在 `_run_final_review` 前即进入保护集，终审不得先把中文音译改成
  假名、也不得用声学对同音专名重新选字后再指望末尾钉子挽救。保护只容忍总计 `<=250ms`
  且不超过 cue 10% 的**外边界**漂移；区间内部有空洞仍 fail closed。源真值随后照常重放并复验，
  因此保护不是“跳过修复”，而是禁止低权威阶段抢写最高权威辖区。
- 宽 `replace_substring` 窗若同一专名出现多次，必须用 `mention_postconditions` 为每一次
  绑定绝对 source interval、required text 与 forbidden tokens；窗口内“某一次写对”不能
  掩盖另一 mention 仍错误。fresh ASR 把两个 mention 合进同一 cue 而无法分别归因时，
  `MENTION_POSTCONDITION_TARGET_NOT_ISOLATED` fail closed，禁止假阳性通过。
- `replace_cue` 可附带经人工/黑屏纯音频听证确认的绝对源时间轴 `spoken_start_ms`：用于删除幻听前缀后把保留口播的字幕起点同步收紧。目标必须唯一；真值宽窗擦到的前句仅在其结束早于审定起点时排除，fresh ASR 的目标 cue 起点最多可比审定起点晚 500ms（随后回钉到绝对起点），若仍有后续重叠 cue、前句跨过起点、越界或非整型则 fail closed。VAD 未检出本身仍不得推导这个起点。
- 已审字幕是独立于封面的文本权威。候选级资产放在 profile 的 `reviewed_subtitle_baselines`
  目录，由 runner 自动发现并写入候选指纹/spec；不得再以 `--reuse-cover` 作为是否保留人工字幕的
  条件。`subtitle-redelivery-baseline.v2` 同时绑定 SRT 哈希、源录像 basename/SHA-256 与绝对
  source coverage：本轮 BCUT 时间保留，文本按绝对源时间逐 cue 恢复旧版，再统一重放更高权威
  的全部源真值；新切点可在干净 cue 边界裁短或扩展，未审扩展区明确记账。哈希/源 identity
  漂移、覆盖边界切半 cue、漏 cue、合并/拆分、歧义映射或二次真值失败一律拒发，禁止靠重掷
  模型碰运气。只有已由 `drop_cue` 删除、无法与旧稿一一配对的静音窗会从两边同时排除；不能
  因宽真值窗内“任一 cue 已出现 required_text”就掩盖同窗其他新误听。
- v2 manifest 只有显式声明 `exact_interval_replay=true`，且源文件 basename、SHA-256、绝对
  起止区间全部逐字相同时，整份人工审定 SRT（含 cue 时间）才可直接重放。这防止同源重跑因
  ASR 随机漏 cue 而删除已审字幕；随后仍必须重放 source-truth。只要区间发生裁切或扩展，就
  回到上面的逐 cue 绝对时间映射，缺失、合并、拆分或漂移继续 fail closed，不能把审定时间轴
  宽松套用到另一段素材。
- 已登记 source alias 的结构化聊天必须显式绑定：官方源 basename/SHA-256、canonical sidecar
  path/SHA-256、JSONL 自身 origin epoch、alias timeline offset 与 `source_alias_id` 缺一不可；
  JSONL 的事件时钟不得从另一份官方媒体 basename 猜。已知 alias 但 sidecar 缺失、哈希漂移、
  无可解析事件时必须阻断，不能退化成误导性的 `evidence_considered=0 / NO_MATCH`。仅没有 alias
  authority 的旧录播可显式 `structured_chat_required=false`。`GUARD_BUY` 是独立 `guard`
  证据，按 username/uid/guard level 装载，并使用 300 秒上舰答谢因果窗；多事件无法唯一对应时
  保留原字幕而非猜名。
- 书名号结构门在所有文本 authority（含源真值）之后再跑一次；合法跨 cue 配对单独记账，真正的 `UNRESOLVED_COMPLEX_IMBALANCE` 必须阻断 `review_ready`。
- 幻听删除是一等声学动作：局部无声前缀用 `acoustic_delete`，只有“保留后的完整 cue =
  SUPPORTED 且原 cue = INCOMPATIBLE”才应用；整 cue 只有 `target_audible=false` 才可
  `acoustic_drop_cue`。局部静音绝不授权删除后半段真实口播；不确定时保留/留空并阻断，
  不为语句顺滑补词。
- source-language 整 cue 回退只适用于无中文的 Latin-language cue；中文口播里的 NN/L、NNLL、LLNNHHB 等 CP 顺序公式以及大写 `TA` 代词是标签/中文代词，不是外语段落，不得触发 mixed-language 拒发，也不得因 token 数下降把已删除的跨 cue 回声整句恢复。
- 交付 `.srt`/`.ass` 走内容时间轴；片头偏移只记录在 `burned_preview.branding_intro.intro_offset_ms`（见 [80-package-delivery.md](80-package-delivery.md)）。
- 说话人统一李豆沙色（数据积累期，Ivan 2026-07-13），说话人不确定绝不拒发。
