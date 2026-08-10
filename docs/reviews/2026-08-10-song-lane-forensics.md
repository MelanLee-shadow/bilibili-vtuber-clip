# 歌lane 全军覆没根因取证（2026-08-07 / 08 / 09）

工作树 `vtuber-slice-wt/song-lane-fix`，分支 `tmp-song-fix`，base `a2b07e8`。
证据来源：free 只读拉取的 `/opt/bilive/autoslice/state/2026-08-0{7,8,9}.json`
与 `/opt/bilive/autoslice/out/2026-08-07/**` 选择器产物。**本次未在 free 上写入
任何文件、未部署。**

---

## 0. 一句话结论

**8/7 六条歌切不是被 provider 门判死的**——判死它们的是**真实的 LRC 内容否决**
（ASR↔LRC 召回 0–23%，远低于门槛）；`JINGTING_PROVIDER_NOT_AGY` +
`JINGTING_MODEL_MISSING` 是歌lane **自己的设计内旁路**被下游 provenance 评估器
误判成违规而喷出的**噪声**，它污染了每一条歌切的 reason_codes、堵死了发布口，
并且因为这两个 code 同时是 `SONG_INFRA_TRANSIENT_REASON_CODES` 成员，把**真实的
内容否决伪装成基础设施故障无限重试**——这就是 8/8「26 次尝试 / 0 产出」的僵尸
循环，也是把 orchestrator 误导成「provider 门杀死候选」的那层烟雾。

---

## 1. 8/7 六条：完整 reason_codes 与判死路径

六条全部 `status=candidate_rejected` / `decision=REJECT` / `rc=0` /
`transient_failure_code=None` / `transient_retry_count=0`。

| candidate_id | lane | title_hint | 终态判据 |
|---|---|---|---|
| `songvis_203735_830_3d9fc4bd` | visual_song_inventory | `'na'` | `SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS` |
| `songvis_203735_10_d37287dc` | visual_song_inventory | `'Leee'` | `SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS` |
| `songvis_203735_200_fe053083` | visual_song_inventory | `'町'` | `SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS` |
| `songvis_203735_140_4d93a748` | visual_song_inventory | `'中生'` | `SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS` |
| `song_213743_1754` | semantic_recall | — | `SONG_AUDIO_LRC_ALIGNMENT_INVALID` |
| `song_230754_1118` | semantic_recall | — | `SONG_AUDIO_LRC_ALIGNMENT_INVALID` |

六条**共有**的 reason_codes（以 `song_213743_1754` 为例，state 逐字）：

```
JINGTING_PROVIDER_NOT_AGY, JINGTING_MODEL_MISSING, CPA_SEMANTIC_INCOMPLETE,
CPA_RELEASE_NOT_READY, SONG_AUDIO_LRC_ALIGNMENT_INVALID,
SONG_FULL_BOUNDARY_PROOF_MISSING, SONG_LYRICS_ALIGNMENT_PROOF_MISSING,
SONG_HOST_VOCAL_UNPROVEN, SONG_MATERIALIZED_RECUT_MISSING,
SONG_SUBTITLE_ARTIFACT_HASH_INVALID, SONG_BURNED_PREVIEW_MISSING,
SONG_RECUT_MANIFEST_HASH_INVALID, SONG_RECUT_MANIFEST_CONTENT_INVALID,
SONG_RECUT_SOURCE_BINDING_INVALID, SONG_RECUT_PROOF_BINDING_INVALID,
SONG_RECUT_INTERVAL_BINDING_INVALID, SONG_RECUT_STREAM_CONTRACT_INVALID,
SONG_RECUT_ARTIFACT_BINDING_INVALID
```

后 12 条 `SONG_RECUT_*` / `SONG_*_MISSING` 是**级联产物**：LRC 证明没过 → 没有
materialized recut → 所有 recut 绑定检查全空。它们不是独立故障。

### 判死的确切代码路径

`src/autoslice/batch_terminal_state.py:24` `project_terminal_song_disposition`：

- L18-20 `SONG_DETERMINISTIC_PROOF_REJECTION_CODES` = 仅 `{SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS, SONG_AUDIO_LRC_ALIGNMENT_INVALID}`；
- L47 `proof_rejections = reasons & SONG_DETERMINISTIC_PROOF_REJECTION_CODES` 命中；
- L57-59 `rc == 0` 通过（选择器确实干净退出）；
- L62-66 `explicit_transient` 为空（**8/7 那一代还没有 4af4a88 的分类修复**）→ 不否决；
- L69-80 铸造 `song_terminal_disposition{status=TERMINAL, disposition=DETERMINISTIC_CONTENT_REJECTION, retryable=false, revival_authority=EXPLICIT_OPERATOR_REVIVAL_REQUIRED}`，
  并 `pop` 掉 `transient_failure_code` / `next_retry_at*`。

调用点：`src/autoslice/song_lane.py:30` `_project_terminal_song_result`，由
`produce_song` 的每个返回路径（L768 / L845）经过。

### 内容否决是真的，不是门太严

`out/2026-08-07/**/song_repair/*.song-repair.json` 逐条 attempt 记录：

- `song_213743_1754` `_full`：8 个 LRC 候选，最好的 `【雷军】全世界死机2` 只匹配
  **23%**（门槛 55%）；进入音频复证后 `canonical LRC line 0 was not affirmatively heard`。
- `song_230754_1118` `_full`：最好 `一起长大 (live)` **3%**，runner-up 0% →
  `ambiguous low-ASR LRC identity: best=3%, runner-up=0%, need best>=20% and margin>=8%`。
- `songvis_203735_10_d37287dc` `_full`：best 9% / runner-up 7%，margin 不够。

**没有任何一条的失败是 provider 调用失败。**全程没有生成过一个
`*.jingting.review-required.json`（`find out/2026-08-07 -name '*.review-required.json'`
返回空），`song_review_retry_after_seconds` 那条 provider 退避链从未触发。

---

## 2. `JINGTING_PROVIDER_NOT_AGY` / `JINGTING_MODEL_MISSING` 的真正来源

free 上 8/7 落盘的 jingting manifest（逐字）：

```json
{ "provider": "source_draft_context", "model": null, "agy_rc": 0,
  "provider_fallback_used": false,
  "provider_request_id": "BYPASSED_NOT_AUTHORITATIVE_FOR_SONG_LRC",
  "refinement_required": false,
  "subtitle_authority_scope": "proof_context_only_external_lrc_required" }
```

同目录有 `source-context.jingting.done`，**没有** review-required —— 也就是说
`source_context_executor` 这一层**判定完全正常**：`run_auto_review_shadow_pipeline.py:290`
`source_refinement_required = not song_candidate or refined_srt is not None` 对歌切为
`False`，`source_context_executor.py:301-315` 遂**故意**跳过 AGY 文本润色（歌切的
字幕权威是独立抓取的 LRC + 音频对齐，AGY 润色既冗余又是多余的 provider 依赖），
写下上面这份自证 manifest，`_agy_reason_codes` 对它正确地返回空元组。

**缺陷在下游**：`auto_review.evaluate_jingting_provenance`（修复前 L408-410）只认
`provider == "agy"`，且 `JingtingProvenance.from_manifest` **根本没有读取**
`refinement_required` / `subtitle_authority_scope` 这两个自证字段。于是设计内旁路
和"擅自换 provider"在数据上无法区分，每一条歌切被无条件扣两顶帽子。

三处后果：

1. `review_candidate` 的 `hard_block_prefixes`（auto_review.py:218-243）直接 BLOCK；
2. `is_publish_gate_satisfied`（auto_review.py:176）**无条件重算** provenance，
   **不接受** `verified_song_lrc_authority` 参数 —— 即使内容全过，发布口再死一次；
3. `AutoReviewManifest.to_dict()`（L125-129）在落盘时**重新注入** provenance
   reason_codes，干净的 decision 写到磁盘上又脏了。

> 既有的 `verified_song_lrc_authority` 豁免（auto_review.py:197）为什么救不了？
> 因为它只在 **LRC 证明已经成功**时为 True（`run_auto_review_shadow_pipeline.py:474`
> 要求 `evidence.song_complete is True and evidence.lyrics_alignment_ready is True`）。
> 证明失败的候选拿不到豁免，于是额外背上 provider 帽子——**恰恰在最需要诚实
> 归因的时候，reason_codes 开始撒谎**。而且它是 `review_candidate` 的参数，
> 保护不了 `is_publish_gate_satisfied`。

---

## 3. 为什么 8/8 变成 `transient_infrastructure_failure`：4af4a88 修复不够，而且方向有害

`song_lane.py:636-648`（注释标「Ivan 2026-08-08 歌lane provider门修复」）新增：

```python
remaining_infra = reason_set & _runner.SONG_INFRA_TRANSIENT_REASON_CODES
if remaining_infra:
    transient_code = min(remaining_infra)
```

而 `SONG_INFRA_TRANSIENT_REASON_CODES`（`free_session_autoslice.py:309-329`）**包含**
`JINGTING_PROVIDER_NOT_AGY` 和 `JINGTING_MODEL_MISSING`。由于旁路**对每一条歌切
无条件**喷出这两个 code：

- `remaining_infra` 永不为空 → `transient_code = min(...)` = `JINGTING_MODEL_MISSING`（字典序最小）；
- `project_terminal_song_disposition` 的 L62-66「有明确 typed transient 则不终态化」永远命中；
- 真实的 LRC 内容否决**永远无法终态化**，每次 tick 重新入队重跑。

state 实证：8/8 `songs=[]`（全部被搬走），`song_superseded_attempts` **26 条**，
覆盖 5 个候选，每个重试 4–5 次，每条的 reason_codes 都是同一副面孔（JINGTING 两条 +
`SONG_AUDIO_LRC_*`）；队列行 `retry_reason=transient_infrastructure_failure`、
`transient_retry_count=4/5`。**26 次昂贵的重跑，0 产出。**

**定性：4af4a88 不是"不够"，是方向有害。**它在正确地识别「provider 故障不该判候选
死」的同时，把一个**恒真的伪 provider 故障**接进了免死金牌通道。正确的修法是让
旁路**别再喷这两个 code**（本次所做），而不是让 transient 覆盖内容否决。

> 补充：另一位 worker 正在本工作树修复互补的另一半——`song_infra_transient_is_active`
> 给这道 veto 加上 `SONG_INFRA_RETRY_CAP` 上限，并发现
> `refill_songs` 让 `selected_repair` 项优先占用**每场 1 条**的
> `song_delivery_budget`，导致 8/8 的 `song_200130_1012` 在 retry_count=4 上
> 霸占唯一名额、同场另外 8 条候选**一次都没跑过**。这正是 memory 记载的
> 「尝试帽饿死真歌」的确切机制，两处修复方向一致、互不冲突。

---

## 4. `CPA_RELEASE_NOT_READY` 是什么门、为什么全中

`src/autoslice/cpa_semantic_qa.py:531` / `:572`：CPA 语义 QA 的**兜底汇总码**——
`release_ready` 为假且没有更具体的理由时补一条。它跟着
`CPA_SEMANTIC_INCOMPLETE` / `CONTEXT_DEPENDENCY_HIGH` / `VIEWER_CONTEXT_INCOMPLETE`
一起出现，因为**歌切的"文本"是一段割裂的歌词**：以谈话切片的语义完整性标准看，
它当然"开头结尾像残片、上下文依赖高"。

**这条门本身已经有正确的豁免**：`cpa_semantic_qa.py:24-33`
`COMPLETE_SONG_SEMANTIC_WAIVED_REASONS` 明确豁免
`CPA_SEMANTIC_INCOMPLETE` / `CONTEXT_DEPENDENCY_HIGH` / `NOT_INTERESTING` /
`CPA_RELEASE_NOT_READY` / `VIEWER_CONTEXT_INCOMPLETE`——**前提是这条已被证明是
完整歌**。8/7 六条正是因为 LRC 证明没过、拿不到"完整歌"资格，才连带没拿到语义
豁免。**所以 `CPA_RELEASE_NOT_READY` 是 LRC 失败的下游症状，不是独立拦路虎，
本次不动它。**

⚠️ **剩余风险**：`TERMINOLOGY_QA_FAILED`（8/7 有 4 条命中）**不在**豁免集合里，
而它是 hard block。歌词里出现「天不熊」这类词会触发。若某天一条歌切内容全过却
栽在这里，那才是需要单独裁定的过度限制——本次不动，仅登记。

---

## 5. `next_retry_at_epoch` 现状：**没有任何重试被钉到 AGY 恢复日**

逐条核实（free 只读）：

| | 顶层 `next_retry_at_epoch` | `songs[].next_retry_at_epoch` | 队列行 |
|---|---|---|---|
| 8/7 | `None` | 六条全 `None`（终态化时被 `pop`） | backlog 4 条无计时器 |
| 8/8 | `None` | `songs` 为空数组 | pending 1 + backlog 8，**全部无 `next_retry_at_epoch`** |
| 8/9 | `None` | `songs` 为空数组 | pending 1 + backlog 14，全新，无计时器 |

**结论：不需要重置任何计时器。**`song_review_retry_after_seconds`
（song_lane.py:210）只从**本次 attempt 自己的** `*.jingting.review-required.json`
sidecar 读取 retry-after，而 8/7 全程没有生成过任何 review-required 文件，这条
provider 退避链从未被触发过。今晚 runner 不存在"被钉到 8/15 所以不会再试"的问题。

8/8 队列行的 `transient_retry_count` 已到 4–5，`SONG_INFRA_RETRY_CAP=6` ——
配合另一位 worker 的 cap 修复，它们会在下一次真实失败后正常终态化，而不是继续
霸占名额。

---

## 6. 精听能力实测（orchestrator 于 free 执行，本报告转录并独立核对代码侧）

| 探针 | 结果 |
|---|---|
| `GEMINI_API_KEY` / `_2` / `_3` 直打 `generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent` | **三把全部 HTTP 200 并正常返回内容** |
| 付费 `GEMINI_KEY_BACKUP` | 已配置于 `/opt/bilive/.env`，runner 按 `gemini_backup_policy` 门控注入子进程 |
| CPA `gpt-5.6-sol` → `/responses` | HTTP 200 正常（该分组 `/v1/models` 不列 gemini，用 `gemini-2.5-flash` 打 `/chat/completions` 会 502——分组能力问题，非 CPA 故障） |
| AGY 二进制 `/root/.local/bin/agy` | 在位（199MB），但 OAuth 周配额 8/15-16 才恢复 |

**定性（供 Ivan 复核）**：做精听所需的**实际能力完整可用**，只是不能走 AGY 订阅
那一层，得走 Gemini API key 那一层，而**三层是同一个 Gemini 模型
（`gemini-3.6-flash`；`entity_audio_verifier.ENTITY_AUDIO_API_MODEL_DEFAULT`
与 `agy_gemini_client.GEMINI_VISION_MODEL_DEFAULT` 都是它）**，只差配额顺序。

所以 `JINGTING_PROVIDER_NOT_AGY` 拦掉的**不是"没有证据"、也不是"证据质量差"，
而是"证据来自同一模型的另一个配额层"**——这正是 Ivan 2026-07-19 拍板的
「按 provider 层拒证据的门 = 过度限制」的教科书实例。

### 实证：音频/LRC 的 Gemini 兜底**已经在生产里跑通了**

不是理论。8/8 free 上真实落盘（`out/2026-08-08/song_210131_1210/song_selector_full/
attempt-5w629ekw/.../song_repair/agy_audio_lrc/variant-01/.../run.manifest.json`）：

```json
{ "schema_version": "agy-audio-lrc-run.v3", "provider": "gemini_api",
  "model": "gemini-3.6-flash", "agy_rc": -9,
  "provider_fallback_used": true, "agy_failure_category": "AGY_FAILED_RC",
  "sandbox": false, "accepted_key_tier": "free",
  "accepted_key_ordinal": 3, "configured_key_count": 3 }
```

要点：

- **模型串确实被如实钉进了 provenance**（`gemini-3.6-flash`），连**用了第几把
  免费 key**（ordinal 3 / 共 3 把）、**AGY 是怎么死的**（`agy_rc=-9` = SIGKILL，
  即 8/9 那次 OOM）都记了。「按层钉模型串」在这条链上是**已实现**状态。
- 全仓 8/7–8/9 **零次** `AGY_AND_GEMINI_API_FAILED` —— Gemini 那条腿**一次都没
  失败过**。
- 该 manifest 出现在 4 个不同 attempt × 2 个 variant，说明兜底是常态路径。

**所以「AGY 配额要到 8/15 才恢复」对歌切的音频/LRC 证明链不构成阻塞** ——
兜底早就接管了。挡在产线和一条歌之间的只有本报告的两个根因：provenance 污染
（§2）和歌名识别被饿死（§7）。

### 关于「fallback 有没有写模型串」

- **歌lane 不需要**：歌切根本不调用 AGY 精听（`refinement_required=False`），
  manifest 的 `model: null` 是**正确**的——没有模型跑过，声称一个模型串反而是
  伪造。所以本次的修法是**按 lane 判定该不该要模型串**，不是删掉这道门；旁路
  若声称模型串，新增 `JINGTING_BYPASS_MODEL_UNEXPECTED` 硬阻断。
- **talk lane 的真相更糙**：`jingting_remote_runner.__call__`（L418-440）
  **只会**返回 `provider="agy"` / `api_fallback_chunk_count=0`——**这条精听链
  压根没有实现 Gemini 兜底**。也就是说，AGY 配额耗尽时，talk 的 source-context
  精听是**没有备胎**的，门放不放宽都一样。本次把 `gemini_api` 接进了
  provenance 契约（前瞻），**但真正缺的是兜底实现，不是门**——这是本报告要
  交给下一棒的最大缺口。

### 歌lane 里还有没有别的地方硬要 AGY？

审计 `song_repair.py` / `song_completion.py` / `song_common.py` / `live_source_review.py`：

- **音频/LRC 证明链早就接受 Gemini 兜底**：`song_common.validate_audio_lrc_execution_metadata`
  （L297-340）接受 `provider="gemini_api"` + `model=GEMINI_API_AUDIO_LRC_MODEL`
  + `provider_fallback_used=True` + 有界 `agy_failure_category` + `sandbox=False`；
  `agy_lrc_alignment.run_agy_audio_lrc_alignment`（L949-960）在 AGY 失败时确实
  切到 Gemini API 并如实钉模型串。**这条链是"按层钉模型串"的既有正确范本。**
- 以下 AGY 字样**不是** provider 身份门，**不许改名**：
  - `alignment_model` 必须以 `-agy-audio-lrc-global-shift-v1` 结尾
    （live_source_review.py:682、song_completion.py:680）——**证明 schema 标识**，
    Gemini 兜底写的是同一个串；memory 明确记载这是生产门。
  - `evidence_source == "agy_audio_lrc"`（song_completion.py:234）——证据**种类**标签。
  - cue_id 格式 `agy-audio:{sha}:line-{i}`（live_source_review.py:610）——证明行 id 格式。

**结论：除已修复的 jingting provenance 一处外，歌lane 没有别的 provider 身份门。**

---

## 7. 第二个根因：`_full` 权威窗的歌名识别被输入采样帽饿死

8/7 日志里一个刺眼的不对称：**同一条候选，短窗认得出歌名，权威的 `_full` 窗
认不出**。

```
song_selector       (60–170s)  → SUCCESS  guessed queries: ['小鸡小鸡 王蓉', '小鸡小鸡']
song_selector_full  (169–850s) → FAILED   LLM returned no usable song guesses   ← 6 条里有 4 条
```

根因在 `src/autoslice/song_alignment.py:153` `generate_llm_song_queries` 的
`max_lines: int = 18`（硬默认，**全仓无任何调用方覆盖**）：超过就**均匀抽稀**。

| 窗 | cue 数 | 抽样步长 | 演唱段存活 |
|---|---|---|---|
| 短窗 | ~30 | 1.7 | 密度够，认得出 |
| `_full` | **214** | **11.9** | **约 3 条**（实测，见下） |

实测（`songvis_203735_10_d37287dc` 同形状，40 条演唱 cue）：
**旧 cap=18 → 存活 3 条；新 default=120 → 存活 23 条。**

prompt 明写「完全无法判断就给空数组」，模型拿到 3 条散落在十几条聊天里的同音
错字，**正确地**返回空数组。也就是说：**窗开得越大、越权威，反而越认不出歌。**

同时注意这个失败分支的判别力：LLM 调用异常/解析失败会落到
`song_repair.py:692` 打印 `"{ExcType}: {msg}"`；观测到的是 L697 的
`"LLM returned no usable song guesses"`，**只可能**意味着调用成功、JSON 解析成功、
模型自己返回了空数组。**排除了 provider 故障。**

> 与 memory 里「>5min 窗 0 行 = 没分块」是**不同 lane**：那条讲的是 AGY 音视频
> 转写按 ~5min 分块（`docs/spark/2026-06-30-future-live-e2e-runbook.md:79`），
> 修那条不会让歌名识别恢复。

---

## 8. 第三个发现（不修，登记）：视觉歌名抽取产出垃圾

8/7 四条 `songvis_*` 的 `title_hint` 实际值：**`'na'` / `'Leee'` / `'町'` / `'中生'`**。
这些 OCR 碎片被当作歌名喂进 `--song-lrc-query`，直接解释了 LRC 候选池里的荒谬结果
（`'Leee'` → `The Lost Chord (feat. Leee John)`）。

对照 8/9 的 `songvis_200615_1170_5dda2f5b`：`title_hint='告白气球'` —— 完整、真实、
高辨识度的歌名。**这是今晚最有希望的一条。**

`title_hint` 不绕过歌名 LLM（`song_repair.py:685` 无条件调用），只是
`_round_robin_unique_queries`（L700-703）的第一路。它唯一的特权在**身份选择**：
精确标题命中时豁免 20% 召回地板（`song_alignment.py:863-874`，2026-08-08 修复），
但同名歧义的 margin 门仍在，下游 55% 对齐门也仍在。

---

## 9. 本次改动与定性

| 文件 | 改动 | 定性 |
|---|---|---|
| `src/autoslice/auto_review.py` | `JingtingProvenance` 读取 `refinement_required`/`subtitle_authority_scope`，新增 `lane()`；`evaluate_jingting_provenance` 按 lane 判定（AGY / Gemini 兜底 / 歌切旁路 / 未知） | **基础设施门修正** |
| `src/autoslice/auto_review.py` | 新增 `JINGTING_BYPASS_MODEL_UNEXPECTED` 进 `hard_block_prefixes` | **新增 fail-closed 门**（收紧） |
| `src/autoslice/source_context_executor.py` | `_agy_reason_codes` 接受**完整自证**的 `gemini_api` 兜底 | **基础设施门修正**（前瞻，当前无发射方） |
| `src/autoslice/song_alignment.py` | `generate_llm_song_queries` `max_lines` 18 → 120 | **基础设施门修正**（输入采样帽，非内容门） |
| `scripts/revive_rejected_candidates.py` | 新增 `--lane song`，复活 `state["songs"]` 的 `candidate_rejected` 行 | **运维通道**（原脚本只处理 `picks`，对歌切完全无效） |
| `tests/test_auto_review.py` | 翻转 `test_gemini_api_fallback_provenance_is_rejected` | **删除过度限制**（详见下） |
| `tests/test_source_context_executor.py` | 翻转 `test_gemini_api_fallback_is_rejected_as_source_context_provider` | **删除过度限制** |
| `tests/test_runtime_architecture.py` | `song_alignment.py` 预算 1185 → 1194 + 理由注释 | 账本 |
| `tests/test_jingting_provenance_lanes.py` | 新增 38 条 | 测试 |

**一律没动的内容门**：LRC 召回 0.20 / margin 0.08 / 对齐 0.55、完整歌校验、
歌词对齐门、host-vocal 声纹、AGY 回声防御、`alignment_model` 字符串、
`song_lane.py:173` 的「重试不得复用切自另一区间的字节」、
`COMPLETE_SONG_SEMANTIC_WAIVED_REASONS`。

### 放宽后仍然 fail-closed 的边界（新增 20 条参数化用例锁住）

歌切旁路要被接受，必须**完整自证**四件事：`provider="source_draft_context"`
∧ `refinement_required=False` ∧ `subtitle_authority_scope=
"proof_context_only_external_lrc_required"` ∧ `agy_rc=0` ∧
`provider_fallback_used=False` ∧ **没有**模型串。缺一项、或声称模型串，照旧阻断。

Gemini 兜底要被接受，必须：`provider="gemini_api"` ∧ 有模型串 ∧
`provider_fallback_used=True` ∧ **记录了 AGY 那条腿的退出码**。缺一项照旧阻断。

### 关于翻转的两条测试（请 Ivan 重点复核）

`test_gemini_api_fallback_provenance_is_rejected` 断言的是「provider 不是 agy →
拒」。这是一道**按 provider 层拒证据的门**，与 Ivan 2026-07-19 拍板直接冲突。
翻转后这条 manifest **仍然被 BLOCK**（因为 `agy_rc=None` 意味着没说清 AGY 那条腿
怎么退出的），只是理由从"provider 身份不对"变成"provenance 不完整"。

**但必须诚实说明**：本仓 jingting 精听链**当前没有任何代码会发射
`provider="gemini_api"`**，所以这一改动**不修复任何已观测到的生产故障**，属于
前瞻契约。若 Ivan 认为该门是有意为之而非伪裁定，回退这两处**不影响今晚出歌**
（歌lane 走的是旁路 lane，与 gemini lane 无关）。

---

## 10. 8/7 那 6 条 terminal 到底怎样才能回产线

### 10.1 歌 lane 确实有 requeue 通道，但 `candidate_rejected` **不算** recoverable BLOCK

- 通道是 `requeue_recoverable_songs`（`src/autoslice/delivery_recovery.py:1161`），
  runner.log 里那行 `... and N recoverable song BLOCK(s) for song pipeline sha256:...`
  就是它。
- 但 L1194-1201 的准入条件写死：

```python
if (not isinstance(record, dict)
    or record.get("delivered")
    or record.get("verified_delivery_pending_commit") is True
    or record.get("status") not in {"blocked", "failed"}):
    kept.append(record); continue
```

  **`candidate_rejected` ∉ {blocked, failed} → 直接 `kept`，永不入队。**

**明确回答 orchestrator 的关键问题：不算。我的门修复会改变 song pipeline
fingerprint，但这 6 条仍然不会被自动拾起。** 必须有 sanctioned 复活。

（这与 talk 的 `--force-redo` 注释「主车道从不 re-supersede `review_ready`」
是同一条设计纪律：终态即冻结，只认显式操作者授权。歌 lane 的
`song_terminal_disposition.revival_authority` 也明写
`EXPLICIT_OPERATOR_REVIVAL_REQUIRED`。）

### 10.2 `revive_rejected_candidates.py` 原样对歌切完全无效 —— 已补 `--lane song`

orchestrator 说得对：该脚本 `:157` 只读 `state["picks"]`，全程不碰
`songs`/`pending_song`/`song_backlog`，而 8/7 六条全在 `state["songs"]`。
本次已新增 `--lane song` 分支（`_revive_songs`），沿用同一纪律：只接受终态、
自持 `runner.lock`、`_atomic_write` 原子写、写审计块、**默认 dry-run**。

### 10.3 复活时必须一并撤回 reason_codes —— 否则当场被打回（实测）

`requeue_recoverable_songs` 在检查 status **之前**先跑
`project_terminal_song_disposition`（L1180-1192）。只把 `status` 翻成 `blocked`
并删掉 `song_terminal_disposition` **是不够的**：行上还留着
`SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS`，加上 `rc=0`、无 transient，终态会被**立刻
重新铸造**。

实测（8/7 state 本地副本）：第一版实现复活后第一次 tick 即被打回
`candidate_rejected`。修正后：`re-minted=False, status=blocked`，且**未被复活的
同批行仍然保持 `candidate_rejected`**（不误伤）。

旧判据不是被抹掉而是**被撤回**：18–19 条原文完整保存在 `song_revivals[].
previous_reason_codes` 审计块里，等这次新尝试自己重新给出判据。

### 10.4 复活命令（orchestrator 照抄；**我未在 free 上执行任何写操作**）

前提：先部署本分支（`song_pipeline_fingerprint` 会随之改变）。8/7 该场
lifetime attempts = 6 < `SONG_LIFETIME_ATTEMPT_CAP=18`，预算充足。
另需确认 8/7 的三个源录像仍在 `REC_ROOT/2026-08-07/` —— `requeue_recoverable_songs`
L1207-1210 找不到 segment 文件会静默 `kept`。

```bash
# 1) dry-run 先看
python3 /opt/bilive/autoslice/repo/scripts/revive_rejected_candidates.py \
  --state /opt/bilive/autoslice/state/2026-08-07.json \
  --lane song \
  --candidate songvis_203735_830_3d9fc4bd \
  --candidate songvis_203735_10_d37287dc \
  --candidate songvis_203735_200_fe053083 \
  --candidate songvis_203735_140_4d93a748 \
  --candidate song_213743_1754 \
  --candidate song_230754_1118 \
  --reason "jingting provenance 旁路误判修复 + _full 窗歌名识别采样帽 18->120；8/7 六条的 LRC 身份/对齐否决是在被饿死的输入上做出的" \
  --fix-commit <本次 commit sha>

# 2) 确认无误后加 --apply（脚本自持 runner.lock，state 原子写）
```

脚本行为：`candidate_rejected` → `blocked`，撤销 `song_terminal_disposition`，
**撤回 `reason_codes`（原文存进审计块）**，把 `song_pipeline_fingerprint` 置为
哨兵 `sanctioned-revival:<fix-commit>`（保证 `requeue_recoverable_songs` 的
`changed` 为真），追加 `song_revivals` 审计块。已在 8/7 state 的**本地副本**上
冒烟通过，并验证过复活行不会被 `project_terminal_song_disposition` 打回、
未复活行不受影响。

### 8/8 的 transient 是另一类，不要混谈

8/8 那 5 条**不是 terminal**，它们在 `pending_song`(1) / `song_backlog`(8) 队列里，
`retry_reason=transient_infrastructure_failure`、`transient_retry_count=4~5`、
**`next_retry_at_epoch` 不存在**（§5）。也就是说它们**已经到期、不需要复活、
也不需要重置计时器**，下一个 tick 的正常 produce 路径就会取。

它们今晚真正的风险不是"不会重试"，而是**每场 1 条的 `song_delivery_budget`
被 `selected_repair` 项优先占用**（另一位 worker 已定位：8/8 的
`song_200130_1012` 在 retry_count=4 上霸占唯一名额，同场另外 8 条一次都没跑过）。
配合其 `SONG_INFRA_RETRY_CAP` 上限修复，本次门修复会让它们在下一次真实失败后
正常终态化并让出名额。

**8/9 从未产出过任何东西**（free 上 `out/` 只到 `2026-08-08`，没有 `2026-08-09`
目录），15 条候选全新、零消耗预算 —— 这也是它最有希望的原因。

### 逐条预后（诚实版）

- **四条 `songvis_*`（title_hint = `na`/`Leee`/`町`/`中生`）**：预后**差**。
  采样帽修复会让 `_full` 窗重新拿到歌名猜测，这是真实的增量；但视觉 title_hint
  仍是垃圾，会继续往候选池里灌噪声。值得跑，别指望。
- **`song_213743_1754` / `song_230754_1118`**：预后**中等**。这两条的短窗本来就
  猜出了 `小鸡小鸡` / `天亮了` / `有没有人告诉你` / `富士山下`，只是 `_full` 窗
  丢了猜测且实测匹配率极低（0–5%）。采样帽修复直接命中它们的失败模式。

### 今晚出歌的最优下注不是复活，是 8/9

**`songvis_200615_1170_5dda2f5b`（8/9，`title_hint='告白气球'`）**——完整真实歌名，
LRC 必然存在且高辨识度，精确标题命中还能豁免 20% 召回地板。8/9 另有 14 条 backlog
+ 1 条 pending 从未跑过，且**歌配额是每场 1 条**，所以目标本就不是把 29 条全produce
出来，而是让最好的 1 条过门。**建议 orchestrator 优先解挂 8/9。**

---

## 11. 剩余风险

1. **jingting 精听链没有 Gemini 兜底实现**（`jingting_remote_runner` 只返回
   `provider="agy"`）。AGY 配额 8/15-16 前，**talk lane 的 source-context 精听
   没有备胎**。本次只把门修好了，兜底本体没做。**这是最大的未闭合缺口。**
2. `TERMINOLOGY_QA_FAILED` 不在完整歌语义豁免集合里，且是 hard block。未观测到
   它单独致死，但存在这个可能。
3. 视觉歌名抽取产出 OCR 碎片（`na`/`Leee`/`町`/`中生`），持续污染 LRC 候选池。
4. `max_lines` 18→120 对**短窗**也生效（短窗现在整段送出而非抽到 18 行）。方向上
   是更多信号，但 8/7 短窗原本能给出猜测，行为会变——属预期内但未经生产验证。
5. `song_alignment.py` 的 `min_recall_ratio=0.20` / `min_margin=0.08` 是
   `song_repair.py:272-273` 的**硬编码字面量**，不是具名常量，没法配置调参。
5b. `JingtingProvenance.to_metadata()` 新增 `lane` 键，会改变同样输入下的
   `auto_review_manifest_sha256`。**已核实不构成重部署风险**：该哈希经
   `shadow_review.py:335-337` 每次运行**现算**并作为产出
   `decision_json_sha256` 写出，且在 `_required_artifact_hash_checks`
   （`shadow_review.py:518`）里被显式跳过，全仓没有"部署前落盘、部署后重算比对"
   的用法。
6. **本工作树在我作业期间被另一位 writer 修改**（`song_lane.py`、
   `batch_terminal_state.py`、`delivery_recovery.py`、`candidate_selection.py`、
   `song_common.py`、`agy_lrc_alignment.py`、`free_session_autoslice.py`、
   `tests/test_song_repair.py` + 两个新 fixture）。我**只 commit 了自己的文件**，
   其余留在工作区未提交，等其所有者处理。`test_runtime_architecture.py` 目前仍有
   一条红：`REGRESSED module: scripts/free_session_autoslice.py 2079 -> 2081`，
   来自**对方**的改动，需其所有者更新账本。
