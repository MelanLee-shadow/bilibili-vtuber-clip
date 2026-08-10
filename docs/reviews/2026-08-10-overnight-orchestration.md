# 2026-08-10 夜间自主调度记录(Ivan 就寝令)

**Ivan 逐字令(本夜唯一授权源)**:
> 我要睡觉了，你自己调度，我希望明天早上能看到878889的切片和歌切都上传成功

orchestrator = 单会话 integrator;执行 worker 全部 Opus(Codex 额度断供至 8/15,Sonnet 永久禁用)。

---

## 一、最重要的结论:今晚的天花板是**上传配额**,不是产能

`scripts/authorized_upload.py:143-145`:

```
QUOTA_FREQUENCY_CODE = 21566
ROLLING_UPLOAD_LIMIT = 10
ROLLING_UPLOAD_WINDOW_SECONDS = 24 * 60 * 60
```

滚动 24h 上限 **10 条**,B 站 code 21566 为最终权威。按 `reports/upload_ledger.jsonl`,截至 2026-08-10T06:24Z 窗口内已用 **5** 条:

| 时间 (UTC) | BV |
|---|---|
| 2026-08-09T07:24 | BV1Bau16nEyq |
| 2026-08-09T19:26 | BV1hquD6pE7X |
| 2026-08-09T22:02 | BV1hfuS6EENb |
| 2026-08-09T22:07 | BV1houS6SEF3 |
| 2026-08-09T22:52 | BV13zuX6fEwh |

→ **当前可用 5 席**;07:24Z 起 6 席(BV1Bau16nEyq 滚出窗口)。

**推论(必须让 Ivan 知道)**:8/7 待产 20+、8/8 待产 13、8/9 待产 5,合计远超 10。
free 单机产能已经超过上传配额,**"三天全部上传"在今晚算术上不可能**。
因此本夜目标改为:**把配额花在价值最高的片上 + 把系统性缺陷修掉**,而不是催产量。
配额分配倾向:**优先保歌切**(Ivan 明确点名,且这三天歌切产出为 0),其次 8/9 谈话切(该日已发 0 条)。

---

## 二、基础设施实测(2026-08-10 06:30Z,free 上实打)

| 能力 | 状态 | 证据 |
|---|---|---|
| CPA `gpt-5.6-sol` via `/responses` | **健康** | HTTP 200,正常返回 |
| CPA `/v1/models` 分组 | 只列 gpt-5.x + gpt-image,**无 gemini** | 拿 gemini 打 `/chat/completions` → 502 |
| Gemini 免费 key ×3 | **三把全部存活有配额** | `gemini-3.6-flash:generateContent` 三把均 HTTP 200 |
| Gemini 付费 backup | 已配置 | `/opt/bilive/.env`,受 `gemini_backup_policy` 门控 |
| AGY 二进制 | 在位(199MB) | `/root/.local/bin/agy` |
| AGY OAuth 周配额 | **耗尽,8/15-16 才恢复** | 交棒记录 |

**关键推论**:做 精听 的**实际能力是完整的**——只是不能走 AGY 订阅层,得走 Gemini API key 层,
而两者是**同一个模型 `gemini-3.6-flash`**(`entity_audio_verifier.ENTITY_AUDIO_API_MODEL_DEFAULT`
与 `agy_gemini_client.GEMINI_VISION_MODEL_DEFAULT` 都是它)。

---

## 三、歌切全军覆没的根因(已定位,修复在飞)

- **8/7**:6 条歌切候选**全部** `candidate_rejected`,每条 reason_codes 都含
  `JINGTING_PROVIDER_NOT_AGY` + `JINGTING_MODEL_MISSING` + `CPA_RELEASE_NOT_READY`。
- **8/8**:0 产出,`transient_infrastructure_failure`(1 pending + 8 backlog)。
- **8/9**:1 pending + 14 backlog。

`src/autoslice/auto_review.py:218-243` 的 `hard_block_prefixes` 把
`JINGTING_PROVIDER_NOT_AGY` / `JINGTING_MODEL_MISSING` / `JINGTING_PROVIDER_FALLBACK_USED` /
`JINGTING_PROVIDER_FALLBACK_UNKNOWN` 全部列为**硬拦**。也就是说:**即使 Gemini fallback 成功跑完,
也会在 review 层被判死**。

### 授权依据(orchestrator 亲做的逐字考据)
1. Ivan 2026-08-10:「…会让整条候选判死？这完全是不正确的逻辑，CPA请求失败的逻辑是积极重试，
   而不是判候选死，**毕竟这跟候选没有关系啊**。」
2. Ivan 2026-08-10:「需要调用AGY->gemini 这条链的，全都复用一种接口才好」(已实现 `4a9a6b2`)。
3. Ivan 2026-07-19 拍板:三层是同一个 Gemini 模型、只是配额顺序;
   「**按 provider 层拒证据的门 = 过度限制**」。
4. **全量 transcript 搜索:没有任何 Ivan 的 user turn 要求过"精听 provider 必须是 AGY"**;
   2026-07-20 的记录反而明确把 `..._NOT_AGY` 硬拦 API 证据列为过度限制。

→ 因此放宽 **provider 身份门**属于「按已记录裁定修复缺陷」,**不是绕过 fail-closed 门**。
内容质量门(歌词对齐、回声防御、完整歌校验、`alignment_model` 字符串)一律不动。

---

## 四、谈话切失败的真实分布(修正交棒文档的错误前提)

交棒文档说「8/7 剩余失败卡在 `BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmCallError`」。
**实测 state 的 `talk_superseded_attempts`,该腿两天合计只有 1 次,不是主要矛盾:**

**2026-08-07**(failure_kind / failure_stage):
- `subtitle_authority` / `final_review_findings` × **10**
- `provider_transient` / `final_review_discovery` × 4
- `provider_transient` / `final_review_correction_discovery` × 4
- `producer_error` / `unknown` × 3
- `subtitle_authority` / `final_review_carryover` × 3
- `story_contract` / `source_fact_repair` × 1

**2026-08-08**:
- `producer_error` / `unknown` × **10**
- `speaker_evidence` / `speaker_finalization` × 7
- `cover_route_regeneration` × 3
- `content_boundary` / `boundary_semantic_review` × **1**

注意:F21(声学证人接线 + Gemini fallback,`cfcc929`)**已合入并部署**(是 `a2b07e8` 的祖先),
所以上表相当一部分是 **F21 之前的代际**。当前这一轮 tick 跑在含 F21 的最新代码上,其结果才是现状证据。

---

## 五、1323 合集分P标题:-400 根因已排除交棒文档的假设

交棒文档猜「-400 极可能是用了置换前的旧 cid」。**已证伪**:

- 稿件 `BV1hquD6pE7X` archive 侧全部正确:标题已是新标题,`cid=40765099962`(置换后)。
- 它**在**合集里:`seasonId=8383206`、`sectionId=9320779`(103 集)、episode `id=215001342`、`order=100`;
  该行的 `cid` 就是 40765099962(当前值),`archiveTitle` 也是新标题,**只有 `title` 字段是旧标题**。
- 用仓内 `sync_exact_section_episode_title` 调用(其全部 fail-closed 前置校验**均通过**),
  `season_episode_edit` 仍返回 `{'code': -400, 'message': '请求错误'}`。**线上无任何变化(纯拒绝)**。

→ 身份全对仍 -400,说明**是 payload 契约本身不对**,首要怀疑 `sorts` 字段
(`{"id": <page_cid>, "sort": n}` 用 cid 当 id)。已交 worker 离线查证,**严禁在活稿件上试错**。

**顺带发现一个真 bug**:section 列表的正确解析路径是 `data.episodes`(list),
而 `data.section.episodes` 字段恒为 `None`。仓内若有按后者解析的代码即为缺陷。

---

## 六、本夜处置

- **8/9 解挂**:Ivan 就寝令明确含 89,取代此前「8.9先不着急做」。已持 `runner.lock` 排队改
  `manual_preclaim` → `no_delivery`,回执写进 `manual_preclaim_note.released`(附逐字引语)。
- **产线停拍(DISABLED)**:避免 13 条 8/8 在即将变更的代码上白产 ~1.5h 后被
  `pipeline_fingerprint_changed` 整批 requeue。当前 tick 的 4 条 8/7 不受影响(mid-tick 仅 defer 后续日期)。
- **`review_ready` 包不会被 requeue**:`build_lidousha_daily_review_manifest.py:509-511` 明确
  「review_ready pick 的产物已冻结——主车道从不 re-supersede review_ready」。故部署不会毁掉已就绪的包。

## 一之二、席位账(按 `assets/lidousha/talk_quota_policy_authority.v1.json` 实查)

| 日期 | scope | cap | 额外席位门 | 已用 | 可再进 |
|---|---|---|---|---|---|
| 2026-08-07 | game(鹅鸭杀) | 10 | ≥85 | 10(满) | 0 |
| 2026-08-08 | event(3D live) | 15 | ≥85 | 5(4 published + 1 rejected) | ~10 |
| 2026-08-09 | **无条目 → fail-closed 回落 `default_policy`** | **5** | 无额外席位 | 0 | 5 |

8/9 恰好有 5 条 pending_talk,与默认 5 席**正好吻合**,且该日**已发 0 条**——
所以 8/9 是「878889 都上传」里唯一真正为零的缺口,配额应优先给它。

**8/9 回落到 5 席是否正确——已核实,正确,无需加条目**:
另一个会话提醒「回落到 default 可能是结构性少产,应按同类场景先例暂定 10 或 15」。
我按它给的正确判据(**只认 `state/session_game_context/<date>.json` 是否 RESOLVED,不看 `scene_kind`**)实查:
- `session_game_context/2026-08-09.json` **不存在** → 游戏 lane 未触发,不是游戏场
- `segment_scene_contexts` 9 段**全部 `talk`**,无 `event` → 不是事件场
→ 8/9 是**普通杂谈日**,`default_policy` 的 5 席正是 Ivan 逐字「**日常还是5，并没有分数限制**」。
**故不加条目**:在 Ivan 没裁过 8/9 的情况下按"先例"替他写一个 10 或 15,正是这份资产被设立时要防的
"实现者自造配额数字"(`4af4a88` 事故)。且今晚上传席只有 5–6 个,5 席**根本不是**约束点。

**再次印证第一节的结论**:可产席位(8/8 十席 + 8/9 五席)= 15,远大于今晚可用的 ~6 个上传席。
**瓶颈在上传配额,不在产能**;`import_external_package.py`(增外部产能)今晚不解决瓶颈。

## 六之二、**punch schema v2 部署使所有 05:50Z 之前的未发布包无法过审计**(本夜新发现)

`3301e4e`(梗字去抽取式 + 新增 `no_fabricated_fact`)把封面梗字回执升到
`lidousha-cover-punch-semantic-review.v2`,并随 `a2b07e8` 于 05:50Z 部署。

- `src/autoslice/cover_punch_semantics.py:312` 与 `:416` 都是
  `schema_version != SCHEMA_VERSION`(v2)**直接判否**,注释自己写明
  「fail-closed:缺字段(例如 **v1 老回执被塞进 v2 校验**)一律不通过」。
- 该校验由 **`scripts/audit_lidousha_review_package.py:472`** 调用 → **审计时生效**。
- 实测 `auto_210739_1142_1436`(8/7 20:22 产,唯一 `review_ready` 的 8/7 件)的回执是
  `/cover_generation/art_direction/cover_punch_semantic_review/schema_version =
  lidousha-cover-punch-semantic-review.v1` → **必被审计拦**。

**处置**:必须走**封面重生成**拿到新的 v2 回执,**不许**在同一份字节上重摇 QC 换绿
(同字节重考=彩票,本仓明令禁止)。好消息是 `cover_route_regeneration` 正是为此设计的
fingerprint 绑定一次性预算(`delivery_recovery.py:196-206`),且该候选
`cover_repair_attempts=0`(预算未用)。部署后放开 DISABLED,runner 应自行重生成封面。

**顺带发现一个死代码缺陷**:`cover_punch_semantics.py:21` 的 `LEGACY_SCHEMA_VERSIONS`
(声明用来兼容 v1)**全仓无任何引用**——向后兼容路径根本没接线。要么接上,要么删掉,
不要留着让人误以为 v1 能过。

## 三之二、歌切根因的**最终版**(两份独立法证互证;推翻我自己的假设)

我一开始的假设是「`JINGTING_PROVIDER_NOT_AGY` = Gemini 配额层被 provider 门歧视」。**这个机制是错的**,
两份独立法证(worker A 的 `2026-08-10-song-lane-forensics.md` + 前会话 agent 的
`2026-08-10-song-lane-forensics-independent.md`)都给出了更准确的版本:

**歌切按设计就跳过 AGY 精听**(外部 LRC 才是字幕权威),`source_context_executor` 故意写自证三元组
`provider="source_draft_context"` / `model=null` /
`subtitle_authority_scope="proof_context_only_external_lrc_required"`;
而 `auto_review.evaluate_jingting_provenance` 只认 `provider=="agy"`,**根本没读那两个自证字段**,
于是把**设计内旁路**当成未授权的 provider 替换判死。

**更坏的二阶后果**:这两个 code 同属 `SONG_INFRA_TRANSIENT_REASON_CODES`,导致
`remaining_infra` 恒不为空 → transient「免死金牌」恒生效 → **真实的内容否决被伪装成基础设施故障而无限重试**
(8/8 单场 26 次尝试 / 0 产出的僵尸循环)。

### 今晚真正能救回的只有 1 条
- **8/8 `song_210131_1210` = 心型病毒 (Live)**:`96% row recall`、28/28 行全部 `heard`、
  28/28 `LIDOUSHA SINGING_THIS_LYRIC`、`FULL_STUDIO_SEQUENCE`、有头有尾——**一份已经完整成立的正向证据**,
  却被 `song_common.py:335` 的 `agy_rc < 0` 判据误杀成 `SONG_AUDIO_LRC_ALIGNMENT_INVALID`。
  该判据本身是**循环论证**:`agy_rc=-9` 意为 AGY 被 SIGKILL(8/9 那次 OOM),
  **"AGY 被杀"正是 failover 的触发条件,不能反过来成为否定 failover 产物的理由**。
- **8/7 六条:无一持有可交付的正向证据**(LRC 召回 3–23%,门槛 20%/55%),属**真实内容否决**。
  四条 `songvis_*` 的 `title_hint` 还是 OCR 垃圾(`'na'`/`'Leee'`/`'町'`/`'中生'`)。**复活它们没有意义**。
- **8/9 是最优下注**:`songvis_200615_1170_5dda2f5b` 的 `title_hint='告白气球'`(完整真实歌名),
  且该日 15 条候选**零预算消耗**、从未产出过任何东西。→ 印证了解挂 8/9 是对的。

### 已合入(`f2b78d6`,全量 **3675 passed**)
worker A 的 `8d1b13d`:typed provenance lane(AGY / Gemini API 兜底 / 歌切旁路各自校验完整自证,
未知 provider 照旧阻断)+ 新增 `JINGTING_BYPASS_MODEL_UNEXPECTED` **硬阻断(是收紧不是放松)**
+ `song_alignment.generate_llm_song_queries` 的 `max_lines` 18→120
(旧帽把 200+ cue 的权威复证窗抽成 1/12,演唱段只剩 3 条 → 模型正确返回空数组;实测 3→23 条)
+ `revive_rejected_candidates.py --lane song`(原脚本只读 `state["picks"]`,对歌切完全无效)。
**内容门一律未动**:LRC 0.20/0.08/0.55、完整歌校验、歌词对齐、host-vocal 声纹、AGY 回声防御、
`alignment_model` 字符串、`song_lane.py:173` 区间字节铁律。

### 未合入(在飞,writer 仍在写)
`song_common.py` 的 D1 修复(`agy_rc<0` → 类别一致性判据)、`SONG_INFRA_RETRY_CAP=6`
(全仓此前**没有任何** `transient_retry_count` 上限)、`refill_songs` 让耗尽重试的 repair
**降级而非丢弃**(2026-08-08 一条 repair 跨 26 次尝试霸占每场唯一交付名额,把另外 8 条饿死在 backlog),
以及新模块 `song_script_family.py`(跨字系:日文歌对中文 ASR 恒 0%)。
我按「不整合移动靶」的纪律**暂不合入**,等其收敛。

## 六之三、本夜合入清单(全部经全量测试门,最终 **3743 passed**)

| 合入 | 内容 | 定性 |
|---|---|---|
| `8d1b13d` worker A | 歌切 typed provenance lane;新增 `JINGTING_BYPASS_MODEL_UNEXPECTED` 硬阻断;`max_lines` 18→120;`revive --lane song` | 基础设施门修正 + **一处收紧** |
| `bc4e853` import worker | `scripts/import_external_package.py`(外部包导入至 review_ready,六步做五步,不碰上传授权面) | 新工具(Ivan #1) |
| `ab13939` worker B | 边界腿 transport 失败改 typed `provider_transient`(不再判死);空补全恢复 transient floor;可恢复失败改指数退避;`body_bytes=` 埋点 | 基础设施门修正 |
| `016d889` 收编 | D1 `agy_rc<0` 循环论证;`SONG_INFRA_RETRY_CAP=6`;名额降级;F3 跨字系 | 基础设施门修正 + **两处收紧(新增上限)** |

**worker B 推翻了我给它的修正**:边界腿的 transport 死法在生产上根本没落进 `content_boundary`——
provider 打不通时 producer 先死在**边界解析面**(`producer_boundary_resolution.py:581`),抛的 marker
在 `classify_talk_failure` 里**一个分支都没有**,整条落进 `producer_error/unknown`(只吃一次重试)。
这才是 8/8 `unknown ×10` 的真身。我基于 state 统计给的"boundary 腿只有 1 次、不是主要矛盾"是**错的**:
它一直是主要矛盾,只是被错误归类藏起来了。

**worker B 对我另一个提议的正确拒绝**:我根据 503/524 推测「大 prompt 系统性超时,应该分块」。
它指出**全仓从来没有一处记录过请求体大小**,该推论目前**无法证伪**,因此加了 `body_bytes=` 埋点
(零行为改动)而**没有**动分块——「那是对终审证据面的结构性改动,凭推论做风险远大于收益」。
这个判断是对的,采纳。**下一轮失败即可从 state 直接判定**。

### 三个 worker 一致指出、但我未擅自处理的浪费源
1. **部署本身是最大的重产来源**:8/7–8/8 五十次重产里 **25 次** 的 `retry_reason` 就是
   `pipeline_fingerprint_changed`;且部署引发的 requeue **不递增** `talk_transient_retry_count`,
   所以指数退避在密集部署期一直停在第 0 档。**修法是收窄 fingerprint 恢复面**,不是加退避。
   (改 `src/autoslice/` 下**任何** `.py`——含新增文件,走 `rglob` 非白名单——都会改全局 fingerprint,共 284 文件。)
2. **标题超 2 个字符 → 最多 3 次整条重产**:`title_length_out_of_bounds:50`(门 48)会把 status 盖成
   `title_failed`,runner 按 `title_attempts<3` 整条 requeue 并把 state 置 `paused_cpa_down`。
   正确修法是**标题重生成局部重试、其余产物按哈希复用**,不是放宽 48 字门。建议单独立项。
3. **断点续产仍未做**——这是 Ivan #9「不要整条重产」的最后一块,本夜未闭合。

## 六之四、**今晚谈话切的头号拦路虎:终审正字法门**(实测,未擅自放宽)

部署 `d7956e7` 后仍存在。今晚跑完的两条 8/7(`auto_213743_1635_1717` / `auto_203735_388_526`)
都只产出 `.recut.mp4`、**没有 `burned-final-speaker` 产物**,日志反复:
`FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_UNRESOLVED_FINDINGS`。

`auto_213743_1635_1717.review-flags.json` 实测:
- `status=FLAGGED`、`release_gate=BLOCK`、`reason_codes=["FINAL_REVIEW_UNRESOLVED_FINDINGS"]`
- `discovery={status: COMPLETE, raw_validated_finding_count: 7, resolved_finding_count: 2}` → **5 条未解决**
- `unresolved_findings_disclosed = []` ← **disclosed 出口一条都没走**
- 5 条未解决的形状(第 0 条逐字):
  - `exact_release_acoustic_closure_blocked_reason = ORTHOGRAPHY_NOT_DECIDABLE_FROM_AUDIO`
  - 判官已裁:`mutation_authority={basis: SEMANTIC_JUDGE_ORTHOGRAPHY_TIEBREAK, status: PASS}`、`repaired=true`
  - **但** `orthography_authority={status: BLOCK, reason_code: ORTHOGRAPHY_TEXT_AUTHORITY_REQUIRED, provenance_kind: null}`
  - 候选对:`CURRENT="我觉得她还行吧"` vs `PROPOSED="我觉得TA还行吧"`

即:**音频判不了(同音)→ 判官提议改字 → 改字需"文本权威"而没有 → 整条候选 BLOCK。**

**我的疑问(已交 worker 量化,未擅自动门)**:PROPOSED 是一个**改进提议**(她→TA,更中性)。
保留 CURRENT 是**零编造风险**的安全默认。把「无法授权一个改进」升级成「原文不可发布」,逻辑上可能是反的。
但这是**内容门**,且 Ivan 在睡觉,我**没有碰它**。worker 在量化影响面
(今天已成功发布 4 条,所以它不是 100% 拦死),拿到真实比例再决定。

## 六之五、`auto_210739_1142_1436` 审计实测(punch v2 预判被证实)

`build_lidousha_daily_review_manifest` **通过**(rc=0),`audit` **不过**,且只有 1 条 blocking:
```
COVER_PUNCH_SEMANTIC_REVIEW_INVALID
punch mode requires hash-bound CPA text proof that a stranger can infer the concrete event and click motive
```
正是 v1 回执撞 v2 校验。

**处置纪律(重要)**:修法是**只重生成封面**(`cover_route_regeneration`,该候选
`cover_repair_attempts=0` 预算未用),**不是** `revive --force-redo` 的整条重产——
因为整条重产会重新走文本流水线,可能把这个**已经 `review_ready`(冻结、安全)**的包
送进上面那个正字法门而彻底失去。**同字节重摇 QC 换绿更是明令禁止**。

## 六之六、正字法门量化结论(worker 实测;**需要 Ivan 拍板**)

报告全文 `docs/reviews/2026-08-10-final-review-orthography-block.md`(分支 `tmp-orthography`)。

**先更正两处我自己的说法**:
1. 「今天已发 4 条」**错了**——那 4 条的 `uploaded.json` 时间戳是 **8/9**(19:26–22:52Z)。
   **8/10 至今零交付、零上传**。(滚动 24h 配额计算不受影响:那 4 条仍在窗口内,5 已用 / 约 5 席可用不变。)
2. 我推测的「无法授权的改字提议 → 原文被判不可发布」**对我采样那条不成立**:
   她→TA 那条**改字是被授权的**(`mutation_authority={basis: SEMANTIC_JUDGE_ORTHOGRAPHY_TIEBREAK, status: PASS}`)。
   而 `ORTHOGRAPHY_TEXT_AUTHORITY_REQUIRED` **根本不是判据**——它在 **PASS 的候选里最多出现过 40 次**,
   约等于"这条 cue 没文本出处"的默认态。我把背景噪声当成了病因。

**真正的判据**是 `final_review_contract.py:350` 的 `_DECIDED_KEEP_CURRENT_BRANCHES`:
一张**写死三个分支名**的白名单,而引擎实际吐 **7 种以上**分支名 → **白名单落后于引擎**。
更深一层:`final_review_auditor.py:2644` 把判官改好的 SRT 直接丢弃(`_unused_output`),
终审**没有 apply 通道**;而 `exact_final_convergence.py:1540` 只认"收敛到保留原文"才算解决
→ **凡被判定"该改"的 finding 永远回不到 resolved**。

### 通过率(按代际分层,已去重备份件、剔除 v1 老 schema)
| 代际 | 样本 | PASS | BLOCK | PASS 率 |
|---|---|---|---|---|
| F2 前(≤08-09 04:40) | 16 | 15 | 1 | **93.8%** |
| F2 后(08-09 05:38 起) | 8 | 3 | 5 | 37.5% |
| **8/10 当前代际** | 4 | **0** | **4** | **0%** |

**成因是发现量放大,不是门变严**:判据一个字没改(`a2b07e8..d7956e7` 在这些文件上 diff 为空);
变的是 `9b88e9c`(8/9 F2)引入 `candidate_pronoun_consistency_audit` —— 一条候选只要 **1 条** finding
落在白名单外就整条 BLOCK,**分母一涨,通过概率乘性坍缩**。
路径本身 7 月就在(`106a52b`/`0a97deb`/`f5daf4b`),与 8/9 的 F12/F16/F17 无关(worker 明确标为未归因,不硬套)。

### 四个选项(worker 用**本仓真函数**跑真实回执测出来的,不是推演)
| 选项 | 解锁 5 条 BLOCK | 解锁今天 8/7 三条 |
|---|---|---|
| A 现状 | 0/5 | 0/3 |
| **B** 白名单补 2 个 keep-current 分支名 | **1/5** | **0/3** |
| C = B + 放行 UNCERTAIN 那支 | 1/5 | 0/3 |
| **D** = C + 所有 `repaired=True` 一律保留原文 | **5/5** | **3/3** |

- **B 不削弱纪律**:那 8 条 finding **根本没改字**(`NOT_APPLIED`/`timing_immutable`),
  纪律管的是"改字要有授权";B 只是让白名单追上引擎。反向门仍拦死 `repaired=True` 与未走完的 infra 分支。
- **D 的风险必须明说**:会把审片员**已判定为错**的字幕原样发出去(`我去，张死`/`烧哦`/`刚刚香香烧烤说`/`超超级紧张`)。
  编造风险为零,但**已知错字上线**风险是满的——同 memory 里 delivery-divergence 把已修件退回 garble 那一类。

**伪裁定核查**:披露规则署名的「Ivan 2026-07-26/27 无人值守裁定:发出去检查有问题再修」,
4 个检索式查 transcript **没有任何 Ivan 本人 user turn**,只命中该注释被写下的那一刻 → **依据存疑**。
若那三个分支名不是 Ivan 划的线,**补白名单(B)就只是修 bug 而非改政策**。

### 我今晚的处置:**不动这个门**
理由:①**D 会发出已知错字**,这是 Ivan 本人被坑过的那类事故,我不越权替他降标准;
②**B 救不了今晚**(今天 8/7 三条 0/3);③**8/8 的 13 条才是今晚主力**,worker 估 **6–10 条**能过,
已经超过我仅有的 ~5 席上传配额;④被拦的 4 条**此刻正带 carryover 自动重跑**(07:40:57 起,约 08:20–08:30Z 出回执),
这是**免费的自愈实验**,先看它。**B 与 D 都留给 Ivan 拍板。**

## 六之七、`visual_song_discovery` 的 Gemini 兜底是**假兜底**(fail-open,今晚部署的新腿)

另一会话的 worker 在 7 份真实录播上实测:**5/7 报 `GEMINI_VISUAL_SONG_DISCOVERY_FAILED`**,
只有 27MB 的残桩成功。根因:整张 contact sheet 内联提交,而
`agy_gemini_client.GEMINI_REQUEST_MAX_BYTES = 20_000_000`,30 分钟录播的接触表几乎必然超限。

**最危险的是它 fail-open**:日志写 `visual song inventory failed open`、召回继续,
于是在没有可用 AGY 的机器上**静默读成"这场没有歌"**。free 的历史日志里这条路径**真的走过**
(7/25–7/26 因 AGY 配额耗尽多次 `failed open`)。

**今晚不构成阻塞**(已核实):8/8 与 8/9 的歌候选**都已在 backlog 里**(8/9 有 14 条),
发现阶段早已跑过,不依赖这条腿。**但它是个真缺陷**:AGY 8/9 一天被 OOM 杀 8 次(约 15GB/31GB),
一旦 free 落到这条腿上,**歌切会静默归零而不是报错**。
修法建议:接触表分批/降采样后提交;超限时**fail-closed 报错**,绝不 fail-open。

## 六之八、**谈话切真正的头号杀手:CPA 分组抽签 400 没有被重试**(实测,今晚最重要的发现)

worker B 埋的 `body_bytes=` 埋点当晚就还本了。8/7 那两条失败件的失败请求 **`body_bytes ≈ 118,586`**,
按"大请求系统性超时"的推论,应该是尺寸问题。**我直接做实验证伪了它。**

free 上直打 CPA(`/responses`,`gpt-5.6-sol`):

**(1) 同一个极小请求重复打 6 次**
| 模型 | 成功 |
|---|---|
| `gpt-5.6-sol` | 5/6(一次 408) |
| `gpt-5.5` | 5/6(一次 **400**) |
| `gpt-5.4` | 6/6 |

**(2) 按 payload 大小阶梯打(同一模型)**
`0B → 400` / `200B → 200` / `500B → 200` / `1000B → 200` / `2000B → 200` / `4000B → 400`
→ **非单调**:0B 失败而 2000B 成功。**与大小无关。**

400 的响应体逐字:
```
{"error":{"message":"当前分组不支持本次请求所需能力，请调整请求或切换分组后重试 (request id: ...)"}}
```

**结论**:不是 payload 大小,是 **CPA 上游 sudocode 的分组路由抽签**——请求轮询落到"没有该模型能力"
的那个分组就 400。**每请求失败率约 15–17%,与大小无关、与模型无关。**
这正是 **Ivan 裁定 #8** 描述的那两个分组(一个有 gpt-image、一个有 gpt-5.6-sol)。

**致命之处**:`scripts/llm_via_cpa.sh` 的 `transient_failure()` 里 **`4??) return 1`**
——400 被判为**不可重试**,于是这个抽签失败**直接把整条候选打死**(一条候选要打很多次 LLM,
按 16%/次,几乎必然踩中)。而**重试 3 次的失败率 ≈ 0.17³ ≈ 0.5%**。

**判据不是我发明的,是上一位 worker 自己写在代码注释里的**(`llm_via_cpa.sh` 约 176-181 行):
> 「同一 body_bytes 反复 524 = 退避治不好,得降上下文/分块;**body_bytes 不相关 = 就是瞬时抖动,退避正确**」

我实测到的正是"body_bytes 不相关"→ **按它自己的判据,退避重试就是正确修法**。
这也正是 Ivan #9 逐字要的:「CPA请求失败的逻辑是积极重试，而不是判候选死，毕竟这跟候选没有关系啊」。

**已派 worker 做最小修复**(只放行 `group_capability_unavailable` 那一类 400,
401/403/404/422 与普通 400 **仍不重试**,配反向门测试),赶在 8/8 的 13 条起跑前部署。
**根治仍是 Ivan #8 的 oracle 分组修复,今晚不动他的 oracle。**

## 六之九、⚠️ **`--preclaim` CLI 是个会毁 state 的地雷**(本夜发现,尚未修)

`scripts/free_session_autoslice.py:2014` 的 `--preclaim` 分支:
```python
write_state(date, {"status": "manual_preclaim", "segments_done": [...]})
```
而 `runner_state_writeback.write_state` 对**普通 dict**(非 `TrackedState`)走的是
`_atomic_write(path, state)` —— **整体替换,不合并**。

→ 对一个**已有内容**的日期跑 `--preclaim`,会把 `picks` / `publication_closure` /
`pending_talk` / `song_backlog` / 已发布记录**全部抹掉**,只剩两个字段。
在 8/7 或 8/8 上跑一次 = 直接丢掉当天全部已发布台账。

**我因此没有用这个 CLI**,而是用逐字段安全改法(只改 `status`、保留其余、备份 + 原子写 + 回执,
与 8/9 解挂同一套路)。**建议 Ivan 要么修成合并写、要么给它加一道"目标 state 非空即拒"的门。**

## 六之十、把 2026-08-07 暂停,把产能让给 8/8 / 8/9

8/7 席位已满(cap=10、已发 3 条),它的失败件**今晚没有上传价值**,却每个 tick 占满 5 个
produce 槽约 **50 分钟**,把 8/8(今晚主力 13 条)和 8/9(已发 **0** 条)挤到后面。
按"配额是瓶颈、产能不是"的原则,已把 8/7 置 `manual_preclaim`(**安全改法**,回执写明解除方式
并警告不要用 `--preclaim` CLI 解除)。预计每轮省下约 50 分钟。

## 七、留给 Ivan 的待裁项(未擅自决定)

1. 多嘉宾场「可交付但依赖审阅」政策 —— 未落地。
2. sudocode 两个分组(gpt-image / gpt-5.6-sol)的路由修复 —— 需在 oracle 上做,未动。
3. 8/7 配额追认争议(4 条 89.0/87.25/86.75/86.0 在 90 门下不合格)—— 仍待裁。
4. `scripts/import_external_package.py` 已立项在做,但**今晚不是瓶颈**(上传配额才是);
   其真实价值是省掉 integrator 的人工六步,建议明天按产能需要评估。
