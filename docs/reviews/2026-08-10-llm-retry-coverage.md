# LLM 退避重试覆盖 + 基础设施失败不杀候选

日期：2026-08-10 · 分支 `tmp-llm-backoff`（base `a2b07e8`）· 实现 commit `5b395ec` + `3a89478`

Ivan 2026-08-10 #9 逐字裁定：

> 「仅仅是一个CPA请求失败为什么会让整条候选判死…CPA请求失败的逻辑是积极重试，而不是判候选死」

`f6e8a2b` 做了传输层退避。本文档记录：剩下哪些腿没覆盖、候选是怎么被判死的、
改了什么、以及**改这些文件会波及多少在飞的包**。

---

## 0. 先纠正两个前提

### 0.1 「`BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmCallError` 那条腿不在已修的终审腿上」——**不成立**

边界语义复核就在终审腿上。链路逐跳：

```
producer_text_pipeline.py:2026   llm_call=_build_final_review_llm_call()
  → producer_final_review_transport.py:13  build_final_review_llm_call()
  → llm_client.build_llm_call(transport="command",
       command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file}
                         'gpt-5.6-sol gpt-5.5 gpt-5.4' medium 1")
```

也就是说 `f6e8a2b` 装在 `scripts/llm_via_cpa.sh` 里的 `CPA_TRANSIENT_ATTEMPTS_PER_MODEL`
transient floor **本来就覆盖了它**（floor 对服务类失败生效，`attempts_per_model=1`
也照常）。真正的缺口不在"退避没覆盖到"，而在两处别的地方（§1.2、§2）。

### 0.2 「8/7 剩余失败卡在这条腿」——orchestrator 已核实为不准，本文复核确认

从 free 只读拉的 `talk_superseded_attempts` 实测分布（每一条 = 一次 15–55 分钟的整条重产）：

| 日期 | failure_kind / stage | 次数 | retry_reason 分布 |
|---|---|---|---|
| 08-07 | `subtitle_authority` / `final_review_findings` | 10 | 7 fingerprint_changed + 3 sanctioned_revival |
| 08-07 | `provider_transient` / `final_review_correction_discovery` | 4 | 4 fingerprint_changed |
| 08-07 | `provider_transient` / `final_review_discovery` | 4 | 3 fingerprint_changed + **1 transient_infrastructure_failure** |
| 08-07 | `producer_error` / `unknown` | 3 | 3 fingerprint_changed |
| 08-07 | `subtitle_authority` / `final_review_carryover` | 3 | carryover |
| 08-08 | `producer_error` / `unknown` | 10 | 7 fingerprint_changed + 3 transient_produce_failure |
| 08-08 | `speaker_evidence` / `speaker_finalization` | 7 | sanctioned_revival |
| 08-08 | `content_boundary` / `boundary_semantic_review` | **1** | sanctioned_revival |

两天合计 50 次重产。**边界腿只占 1 次。**

同时暴露出一件比原任务书更重要的事——见 §3：

> **50 次重产里 25 次的 `retry_reason` 是 `pipeline_fingerprint_changed`。**
> 也就是说，产线一半的机时不是烧在 provider 故障上，是烧在**我们自己每次部署**上。

---

## 1. 任务 1：全仓 LLM / CPA / Gemini 调用点盘点

`src/autoslice/llm_client.py` **自身零重试**：两个 transport 各打一枪，任何异常
→ `LlmCallError`。所有重试都在它外面的五个地方。

### 1.1 重试机制总表

| # | 位置 | 机制 | 覆盖情况 |
|---|---|---|---|
| 1 | `scripts/llm_via_cpa.sh` | 模型失效备援（sol→5.5→5.4）+ `ATTEMPTS_PER_MODEL`(argv5，默认 3) + `CPA_TRANSIENT_ATTEMPTS_PER_MODEL`(默认 3) 服务类下限 + 指数退避带抖动（总睡眠 ≤30s）+ `CPA_DEADLINE_SECONDS=400` 硬墙 | 覆盖**所有走 command transport 的文本腿** |
| 2 | `scripts/cpa_semantic_qa_llm.py:190` | `--retries`（生产传 3）+ 429/5xx `min(60, 15·2ⁿ)` 退避 + `--fallback-model` | 已有 |
| 3 | `src/autoslice/agy_gemini_client.py:397` `run_gemini_key_ladder` | 免费 key×轮次（`MIN_FREE_CHAIN_STRIKES=3`）→ 政策门控付费 key | 已有 |
| 4 | `src/autoslice/acoustic_witness_adjudication.py:763` | `JUDGE_MAX_PROVIDER_RETRIES=1`，**无 sleep** | 薄，但底下压着 #1 |
| 5 | `src/autoslice/cover_generation.py:1069` `_call_cpa_image_edit` | 仅"模型不可用"时换模型，**同模型零重试** | ✗ 未覆盖 |

### 1.2 关键发现：`attempts_per_model=1` 下空补全绕过了 floor（**已修**）

桥接里原本有一条**先于** `transient_failure()` 的特例分支：

```bash
if [[ "$EMPTY_COMPLETION" == "1" ]]; then
  max_attempts="$ATTEMPTS_PER_MODEL"        # 终审面 = 1
elif transient_failure ...; then
  max_attempts=max(ATTEMPTS_PER_MODEL, TRANSIENT_ATTEMPTS_PER_MODEL)
```

`transient_failure()` 第一行就把 `empty_completion=1` 判成服务类，所以这条特例
分支唯一的作用就是**把 floor 抹掉**。而 `attempts_per_model=1` 在全仓只有两个
调用点，其中一个正是终审面传输层——**十条腿共用它**：

| 腿 | owner | 失败落什么 |
|---|---|---|
| 终审审片 | `final_review_auditor.py:535/686` | `AUDITOR_UNAVAILABLE` → BLOCK |
| 代词一致性 | `pronoun_consistency.py:143` | `AUDITOR_UNAVAILABLE` |
| **边界语义复核** | `boundary_semantic_review.py:794` | `BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:<Exc>` |
| 精确交付边界复核 | `producer_boundary_review_stage.py:~188` | 同上 |
| 闭集词choice裁决 | `acoustic_witness_adjudication.py:628` | `JUDGE_UNAVAILABLE` |
| 精确终审收敛 | `exact_final_convergence.py:619` | `CPA_CYCLE_ADJUDICATION_UNAVAILABLE` |
| 外文脚本裁决 | `foreign_span_witness.py:509` | `JUDGE_UNAVAILABLE` |
| 语言保全裁决 | `foreign_span_witness.py:894` | 同上 |
| 缺失提案 bootstrap | `missing_proposal_bootstrap.py:144` | `..._LLM_UNAVAILABLE` |
| 外文闭集重建 | `foreign_closed_set_rebuild.py:69` | `PROPOSAL_REBUILD_LLM_UNAVAILABLE` |

推理模型的空 `output_text` 是**已知怪癖**（桥接注释自己写着）。修复前这十条腿
碰到它就是"一枪换一模型"，sol/5.5/5.4 三枪打空 → 判死。**已删除该特例分支。**
600s 外层期限仍由 `CPA_DEADLINE_SECONDS=400` 独立守住（400s + 一枪在飞的
`curl --max-time 180` = 580s < 600s），不依赖 `attempts_per_model=1`。

连带影响的另一个 `argv5=1` 调用点：`scripts/crawl_community_names.py:40`
（`DEFAULT_LLM_COMMAND`，240s 超时）。空补全在那里现在最多花 9 次请求而不是 3 次，
但 bridge deadline(400s) > 它自己的 240s 超时，实际上限仍是调用方的 240s；且它的
失败形态是"丢掉这一批、爬取继续"（`community_name_crawler.py:1376/1527`），不判死
任何候选。无害，此处记明。

### 1.2b 同批加的埋点：`body_bytes`

桥接每条 attempt 的 stderr 现在带 `body_bytes=<请求体字节数>`。零行为改动，纯诊断——
理由与用法见 §5.1b：目前全仓没有任何一处记录过请求体大小，"大请求系统性失败"
只是推论，无法证伪。该 stderr 已被 `provider_failure_detail` 原样收进
`provider_detail` 落盘，所以下一轮失败即可从 state 直接读出"哪个字节数在什么状态码
上失败"。

### 1.3 仍未覆盖的腿（不杀候选，列为剩余风险）

| 位置 | 说明 | 为什么没修 |
|---|---|---|
| `src/autoslice/cpa_frame_witness.py:218` | 裸 `urllib` POST `/responses`，单枪，失败 → `VISION_CALL_FAILED` | `visual_witness.py:67` 有 AGY 兜底（key ladder），不判死候选 |
| `src/autoslice/cover_generation.py:1069` | 裸 `urllib` POST `/images/edits`，同模型零重试 | 失败落 `cover_route_regeneration`，本身就在有界重试车道 |
| `scripts/run_auto_review_shadow_pipeline.py:1089/1095/1098` | `LlmConfig` 漏传 `timeout_seconds` → 60s（同腿在生产路径是 180s） | shadow 管线，不进生产交付 |
| `scripts/regen_covers_latest_flow.py:38` | 不传模型链/effort，回落 env 或 sol 链（其余封面点都钉 luna） | 一次性回填脚本 |

---

## 2. 任务 2：候选是怎么被判死的，改成什么

### 2.1 调用链（修复前）——**两条出口，主路径不是原先以为的那条**

共同前半段：

```
boundary_semantic_review.py:809  _semantic_payload_for_request
      except Exception as exc:                    # llm_call() 或 extract_json() 抛的任何东西
          raise BoundarySemanticReviewError(
              f"BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:{type(exc).__name__}") from exc
 ↓
producer_boundary_review_stage.py:114   except BoundarySemanticReviewError → reason = str(exc)
      return {"status": "BLOCK", "reason_codes": [reason], ...}
 ↓
producer_text_pipeline.py:2012  写进 final_review_audit["boundary_semantic_review"]
```

从这里分成两条出口，**先撞上的是 (A)**：

#### (A) 边界解析面 —— 主路径（早于终审契约，8/8 十条 `producer_error/unknown` 的真身）

```
produce_slice_package.py:391-397   spec["boundary_semantic_review"] = final_review_audit[...]
 ↓
producer_boundary_resolution.py:581  if semantic_review.get("status") != "PASS":
      retry_scope = _bounded_boundary_retry_scope(semantic_review)   # 要求 needs_more_context=True
      # UNAVAILABLE 的 BLOCK 没有 needs_more_context → retry_scope is None
      raise SystemExit("BOUNDARY_SEMANTIC_REVIEW_REQUIRED: "
                       '["BOUNDARY_SEMANTIC_REVIEW_UNAVAILABLE:LlmCallError"]')
 ↓
talk_lane.classify_talk_failure
      # 全仓 grep：`BOUNDARY_SEMANTIC_REVIEW_REQUIRED` 在分类器里**一个分支都没有**
      else: kind, stage, recoverable = "producer_error", "unknown", True
 ↓
delivery_recovery._talk_retry_decision
      producer_error ∉ INFRASTRUCTURE_WAIT_FAILURE_KINDS → infrastructure_retry=False
      transient = (… and transient_count < 1) → **只吃一次重试**，之后卡死等 fingerprint
```

这解释了 8/8 的失败分布：`producer_error/unknown` 10 次，`content_boundary` 只有 1 次。
free 上 `auto_233123_315_412` / `auto_233123_782_859` 两条的 produce 日志尾，字面就是
`BOUNDARY_SEMANTIC_REVIEW_REQUIRED: [...]`。

#### (B) 终审契约面（文本 mutation 后的精确交付复核走这条）

```
producer_text_pipeline.py:1159  reason_codes.append("FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED")
final_review_contract.py:254    raise FinalReviewContractError(...)  → produce rc≠0
 ↓
talk_lane._classify_final_review_release
      if boundary_status == "BLOCK" or "FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED" in reason_codes:
          return ("content_boundary", "final_review_boundary_semantic", False, evidence)
                                                                        ^^^^^ recoverable=False
 ↓
delivery_recovery._talk_retry_decision  → 两项都 False → **完全不重排**
```

两条出口都把「provider 打不通」记成了别的东西：(A) 记成"未知 producer 缺陷"，
(B) 记成"内容缺陷"。**两条都已修**（§2.3 C/E）。

#### 第三条 marker：`BOUNDARY_CONTEXT_EXHAUSTED` —— 已核，**不受影响**

它只在 `_bounded_boundary_retry_scope` 返回非 None 时抛出，而那要求
`needs_more_context is True`——那是 LLM **真答了**才会有的字段（
`boundary_semantic_review.py:1115` 在解析出的裁决上追加）。transport 失败构造的
BLOCK dict 里没有它。所以这条 marker 天然只承载内容裁决。仍然加了同款 allowlist
判断作纵深防御（闭集，误报不可能），并用
`test_boundary_context_exhausted_keeps_its_content_verdict` 把这条不变量锁住。

### 2.2 `provider_transient` 那条链——**它一直在重试，问题是间隔**

orchestrator 首要问题的答案，逐条：

**重试了没有？** 重试了。`provider_transient` ∈ `INFRASTRUCTURE_WAIT_FAILURE_KINDS`
（`delivery_recovery.py:58`），`_talk_retry_decision` 里 `infrastructure_retry`
一旦 `next_retry_at_epoch` 到点就置真。

**重试几次？** 无上限。`transient = (… and transient_count < 1) or infrastructure_retry`
——`infrastructure_retry` 直接短路了那个 `<1` 计数上限。`talk_transient_retry_count`
每次 requeue 递增，但**没有任何地方读它做终止判断**（歌 lane 有
`SONG_INFRA_RETRY_CAP=6`，talk lane 没有对等物）。

**最终会不会判死？** 不会。`backfillable_talk_rejection` 只认
`{subtitle_authority, story_contract}` + `recoverable is False`，`provider_transient`
永远不会变成 `candidate_rejected`。

**那问题在哪？** 在**间隔**。修复前 `talk_lane` 无论第几次都写死：

```python
retry_epoch = int(time.time()) + _runner.SONG_INFRA_RETRY_BASE_SECONDS   # 15 分钟，永不增长
```

一次 produce 要 15–55 分钟。15 分钟的固定间隔意味着：**上一次产完，下一次立刻
又排上**。多小时的上游故障里，每个候选就在那儿反复烧整条重产，每次都必然以
同样的方式失败。歌 lane 早在 `song_infra_retry_delay_seconds` 里解决过同一个
问题（15m→30m→…→6h 上限），talk lane 从来没接。

### 2.3 改了什么

| 改动 | 文件 | 效果 |
|---|---|---|
| A | `scripts/llm_via_cpa.sh` | 删掉空补全特例分支 → 空补全恢复 transient floor（§1.2） |
| B | `src/autoslice/provider_failure.py` | 新增闭集 `TRANSPORT_EXCEPTION_NAMES` + `transport_unavailable_reason()` + `marker_transport_unavailable()` |
| C | `src/autoslice/talk_lane.py` | 出口 (B)：`_classify_final_review_release` 边界 BLOCK 若 reason 是 transport 类异常 → `provider_transient` / recoverable=True |
| **E** | `src/autoslice/talk_lane.py` | **出口 (A)（主路径）**：`classify_talk_failure` 新增 `BOUNDARY_SEMANTIC_REVIEW_REQUIRED` 分支，仅当该 marker 自带的 reason_codes 落在 transport 闭集时 → `provider_transient` / recoverable=True。内容裁决的同名 marker 归类**一个字不动**（仍走原来的 fallthrough）|
| D | `src/autoslice/infra_retry_policy.py`（新） | `talk_infra_retry_schedule()`：重排时刻改指数退避，复用歌 lane 同一条曲线；配额类从下一档起步 |

**基础设施 vs 内容的界线**（代码与本文口径一致）：

- **基础设施 = transient**：provider 从头到尾**没有给出裁决**。判据是异常类型名
  落在闭集 `TRANSPORT_EXCEPTION_NAMES` 里（`LlmCallError`、`TimeoutExpired`、
  `ConnectionError`、`OSError`、`HTTPError`/`URLError` 等）。`LlmCallError` 覆盖
  `llm_client` 的全部四种不可用结局：桥接非零 rc、子进程超时、补全文件缺失/为空、
  200 但 JSON 不可解析——四种都是"再问一次"，没有一种是对内容的判断。
- **内容 = terminal**：LLM 真的看了、真的给了裁决，裁决是不合格
  （`BOUNDARY_SEMANTIC_REVIEW_REQUIRED` 等）。照旧 `content_boundary` +
  `recoverable=False`。
- **代码缺陷 = terminal**：`TypeError` / `KeyError` / `AssertionError` 之流**不在**
  闭集里，仍旧终态。这条是硬要求：`INFRASTRUCTURE_WAIT_FAILURE_KINDS` 是无上限
  定时重试，把确定性 bug 放进去会让同一 fingerprint 每 tick 空转到天荒地老——
  `delivery_recovery.py:52-57` 明文警告过。allowlist 是**白名单**，未知名字一律终态。

**没有放松任何内容门。** BLOCK 依旧拒绝交付；变的只是失败**归类**（内容缺陷 →
基础设施等待）和由此得到的重排资格。findings 门（`FINAL_REVIEW_UNRESOLVED_FINDINGS`）
一个字没动。

### 2.4 断点续产：**没做**，列为剩余风险

评估结论：成本过高，不在本次范围。理由——produce 是一个 `subprocess.run` 的
整体调用（`talk_lane` 侧只拿 rc + stdout tail），中间态（转录/边界/终审/封面）
散落在 `out/<date>/<cid>/` 的十几个 sidecar 里，没有统一的阶段完成回执；要做
断点续产等于给 producer 引入一套阶段状态机 + 每阶段的产物哈希绑定。本次只做
"不杀候选 + 退避"，这已经把重复烧机时压下去（见 §5.1 量化）。

---

## 3. 任务 3（必答）：哪些文件进入 pipeline fingerprint

### 3.1 三个 fingerprint 函数

全在 `scripts/free_session_autoslice.py`：

| 函数 | 行 | 谁读它 |
|---|---|---|
| `pipeline_fingerprint()` | 421 | 全局基底 |
| `talk_pipeline_fingerprint(cid)` | 684 | = 基底 + 该候选的可选真值资产；落在每条 pick 的 `pipeline_fingerprint` 字段 |
| `song_pipeline_fingerprint()` | 493 | 歌 lane BLOCK 恢复 |
| `talk_failure_recovery_fingerprint(kind, cid)` | 985 | **按 failure_kind 收窄**的恢复面（见 §3.3） |

### 3.2 `pipeline_fingerprint()` 的输入清单（共 **284** 个文件 + 4 个环境变量 + provider authority）

**(a) 11 个显式脚本**（`scripts/free_session_autoslice.py:429-441`）
```
scripts/free_session_autoslice.py            scripts/produce_slice_package.py
scripts/free_asr_client.py                   scripts/repair_reviewed_covers.py
scripts/apply_subtitle_text_overrides.py     scripts/resume_frozen_talk_package.py
scripts/cpa_semantic_qa_llm.py               scripts/run_auto_review_shadow_pipeline.py
scripts/gemini_slice_jingting.py             scripts/run_full_session_selector_cpa_shadow.py
scripts/llm_via_cpa.sh
```

**(b) `profile_tool("cover_regenerator")`** = `scripts/regenerate_lidousha_cover.py`

**(c) `CHANNEL_PROFILE.fingerprint_paths()` = 35 个**：`profiles/lidousha/profile.json`
+ 32 个 `fingerprint_asset_keys` 资产 + `fonts/` 目录下 2 个 ttf。完整清单：
```
profiles/lidousha/profile.json
assets/lidousha/{entity_confusables,community_name_sources,community_names,
  game_glossary,cover_reference_overrides,emote_library,bilibili_gift_names,
  known_songs,manual_archive_metadata,manual_title_overrides,psplive_roster_sources,
  streamer_registry,streamer_registry_sources,session_relation_ledger,
  selection_score_calibration,speech_memory_ledger,subtitle_truth_ledger,
  timely_term_seeds,timely_term_sources,timely_terms,topic_entity_graph,
  title_policy,upload_tag_policy,voiceprint_profile}.{json,v1.json}
assets/lidousha/{glossary.txt,cover_identity_prompt.txt}
assets/lidousha/{persona.md,psplive_roster.v1.md,slice_selection_metric.md,
  subtitle_correction_principles.md,title_style.md}
assets/lidousha/intro/branding_intro.v1.json
assets/lidousha/fonts/{SmileySans-Oblique.ttf,ZCOOLKuaiLe-Regular.ttf}
```

**(d) `src/autoslice/**/*.py` 全部 —— 目前 237 个**，唯一排除项：
```python
PIPELINE_FINGERPRINT_EXCLUSIONS = {"src/autoslice/reporting.py"}
```

> **结论（orchestrator 要的那句）：改 `src/autoslice/` 下任何一个 `.py`
> （除 `reporting.py`）都会改全局 fingerprint。**新增文件同样算——它走的是
> `rglob("*.py")`，不是白名单。`tests/**` 和 `docs/**` 不进。

**(e) 4 个环境变量**（值参与哈希）：`AUTOSLICE_SPEAKER_ROUTING_PROVIDER_{COMMAND_JSON,NAME,ALGORITHM_ID,ARTIFACTS_JSON}`

**(f) speaker routing provider authority** 的 JSON 内容（不可读时哈希一个显式的
`provider-authority-unavailable:<ExcType>`）

`talk_pipeline_fingerprint(cid)` 在此之上再加该候选的 text override /
subtitle regression / speaker override / reviewed baseline（**绝大多数候选没有，
此时逐字节等于基底**）；`human_truth_mode()=="withheld"` 时另走一层封装。

### 3.3 恢复面是分层的：不是所有失败都被全局 fingerprint 唤醒

`talk_failure_recovery_fingerprint(kind, cid)` 只对 4 个 kind 收窄：

| failure_kind | 恢复面 |
|---|---|
| `content_boundary` | `CONTENT_BOUNDARY_RECOVERY_RELATIVES`（15 个文件，`delivery_recovery.py:71`）|
| `subtitle_authority` | `subtitle_authority_recovery_relatives()`（19 文件 + 真值账本，`talk_failure_recovery_policy.py:8`）|
| `speaker_evidence` / `runtime_prerequisite` | 5 个文件 + 声纹 profile + 该候选的 speaker override |
| **其余全部**（`provider_transient` / `producer_error` / `story_contract` / `selection_rescore` / `final_review_contract` / `content_duration` / `source_media` / `cover_route_regeneration` / …） | **完整 `talk_pipeline_fingerprint`** |

### 3.4 本次改动的实测波及面

方法：`git archive` 出 `a2b07e8` 与本分支交付位 `3a89478` 两份纯净树，分别跑
fingerprint 函数对比。两边都只含 git 内容，所以绝对值与生产不同——有意义的是「变 / 不变」。

| fingerprint | a2b07e8 | 本分支 | 变？ |
|---|---|---|---|
| `pipeline_fingerprint` | `98d920be…` | `dc94df11…` | **变** |
| `song_pipeline_fingerprint` | `30287957…` | `95499f8b…` | **变**（`llm_via_cpa.sh` 在歌 lane 显式清单里）|
| recovery `content_boundary` | `69e34714…` | `4a4ed609…` | **变**（`talk_lane.py` 在 15 文件里）|
| recovery `subtitle_authority` | `15eeb730…` | `598dc2fd…` | **变**（`talk_lane.py` 在 19 文件里）|
| recovery `speaker_evidence` | `9e4c4157…` | `9e4c4157…` | 不变 |
| recovery `runtime_prerequisite` | `387335ae…` | `387335ae…` | 不变 |

**部署这份改动会唤醒：** 全部 `provider_transient` / `producer_error` / 其它未收窄
kind 的失败件、全部 `content_boundary` 失败件、全部 `subtitle_authority` 失败件、
全部歌 lane BLOCK、以及 exact-recovery 流程里所有 `pipeline_fingerprint` 已漂移的
**已产未发**包（`delivery_recovery.py:2059-2067`，`retry_reason=
"current_delivery_pipeline_fingerprint_changed"`）。
**不唤醒：** `speaker_evidence` / `runtime_prerequisite` 失败件。

其中 `content_boundary` 的唤醒是**想要的**：被本 bug 错杀成内容缺陷的边界件，
正需要重跑一次才能拿到新的 `provider_transient` 归类。

> 8/7–8/8 实测 50 次重产里 **25 次** 的 `retry_reason` 就是
> `pipeline_fingerprint_changed`。**产线一半的机时烧在我们自己部署上，不是烧在
> provider 故障上。**「全部 `src/autoslice/*.py` 进 fingerprint」是个极粗的
> 恢复面——建议单独立项收窄（§5.2），但不该混进这次改动。

---

## 4. 验证

全部实跑，输出如实记录。

| 命令 | 结果 |
|---|---|
| `python3 -m pytest tests/ -q -p no:randomly` | **3643 passed**, 0 failed, 65.5s |
| `python3 -m pytest tests/lidousha/test_provider_failure_evidence.py -q` | 29 passed |
| `python3 -m pytest tests/test_llm_via_cpa.py -q` | 16 passed |
| `python3 -m pytest tests/test_runtime_architecture.py -q` | 8 passed |
| `bash -n scripts/llm_via_cpa.sh` | SYNTAX OK |

### 4.1 新增测试的"修复前确实失败"验证（本仓惯例，防恒绿假测试）

`git stash` 掉实现、只留测试，实跑：

```
$ git stash push -- src/autoslice/talk_lane.py src/autoslice/provider_failure.py
$ python3 -m pytest tests/lidousha/test_provider_failure_evidence.py -q -k "..."
FAILED  test_boundary_review_transport_failure_waits_instead_of_dying
FAILED  test_transport_unavailable_reason_allowlist_is_closed
FAILED  test_infra_retry_delay_escalates_instead_of_reburning_every_15_minutes
FAILED  test_quota_class_starts_one_step_further_out
4 failed, 2 passed
```

那 2 个 passed 是**反向门**（`test_boundary_review_code_defect_stays_terminal`、
`test_boundary_review_content_verdict_stays_terminal`）——它们断言的是**不该变的
行为**，修复前后都该绿，绿是正确结果。

```
$ git stash push -- scripts/llm_via_cpa.sh
$ python3 -m pytest tests/test_llm_via_cpa.py -q -k empty_completion
FAILED  test_empty_completion_also_honors_the_transient_floor
        AssertionError: assert ['gpt-first', 'gpt-second'] == ['gpt-first', 'gpt-first']
1 failed, 1 passed
```

同样，passed 的那条是 floor 关掉后仍立刻失效备援的反向门。

### 4.2 跳过了什么

- **没有跑真实 CPA/AGY 冒烟。** 套件有密闭守卫（`conftest.py:44-58` 把任何含
  `llm_via_cpa.sh` 的模板打成 `TEST_HERMETIC_LLM_BLOCKED`），本地也无生产凭据。
  桥接的退避/失效备援/deadline 全部靠 `tests/test_llm_via_cpa.py` 的 fake curl 覆盖。
- **没有部署，没有碰 free 上任何文件。** 对 free 只做了只读 ssh（读 state JSON 与
  produce 日志）。部署权归 orchestrator。
- **没有跑 lint / build**：本仓无配置的 linter 入口，pytest 即门。

---

## 5. 剩余风险与未做的部分

### 5.0 现场活样本（2026-08-10 06:51Z，跑在含 F21 的当前部署位上）

```
[2026-08-10 06:51:22] auto_203735_388_526: provider failure class=service status=[503, 524]
```

06:01 起跑，50 分钟后死。produce 日志（`2026-08-07_auto_203735_388_526.log`，
append-only 跨 attempt）本次尝试的**最后一条**致命 marker 是：

```
FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_DISCOVERY_INCOMPLETE: …chat-authority.json
```

**问：503/524 之后发生了什么？** → 落 `provider_transient` / `final_review_discovery`
/ `recoverable=True`（`talk_lane.py:857` 那条既有分支）。**没有判死**，进 `pending_talk`
等重排。也就是说这一条本身归类是对的——`f6e8a2b` 的 provider 归类层正常工作，
`class=service status=[503,524]` 就是它打的标签。真正的浪费在**间隔**：修复前
15 分钟定时到点就整条重产（50 分钟的活），这正是 §2.2 说的病，本次 §2.3-D 修的
就是它。

**一个必须说清的局限**：该候选当前 state 是
`talk_transient_retry_count=0, talk_repair_retry_count=4, retry_reason=pipeline_fingerprint_changed`。
`talk_transient_retry_count` 只在 `transient and not changed` 时递增
（`delivery_recovery.py:1813`）——**每次部署引发的 requeue 都不计数**。所以在
部署密集期（正是现在），指数退避的档位一直停在第 0 档，等于没生效。
退避治的是"故障期间的定时空转"，治不了"部署引发的全量重产"，后者要靠收窄
fingerprint 恢复面（§5.2b）。

**问：`TITLE_AUTHORITY_UNRESOLVED: title_length_out_of_bounds:50` 会不会造成整条重产？
→ 会，最多 3 次。** 链路：`talk_lane.py:1921` 见到该 marker 且
`title_authority_status` 以 `UNRESOLVED` 开头 → 删掉已交付文件与 recuts →
`result["status"] = "title_failed"` 提前 return（**覆盖掉刚算出来的 failure_kind**）
→ runner `free_session_autoslice.py:1821` `title_attempts += 1`，
`< TITLE_MAX_ATTEMPTS(3)` 就 `retry.append(item)` 重新排队 → **整条重产**，
并把 `state["status"]` 置 `paused_cpa_down`。

50 字比 48 字门只超 2 个字符，代价是最多三次 15–55 分钟的完整重产。这确实是
"小问题引发大浪费"的同一类病，但**它是内容门，不在本次范围**：正确的修法是让
标题重生成成为一个**局部重试**（只重跑标题那一段，其余产物按哈希复用），而不是
放宽 48 字门，也不是把它改成 transient。建议单独立项。

### 5.1 退避改动的正反面

一次 6 小时的上游故障，单个候选的整条重产次数（**下表是按重试间隔的推算，不是
观测值**；观测数据是 §0.2 那张 8/7–8/8 分布表）：

| | 修复前（固定 15 分钟） | 修复后（15m→30m→1h→2h→4h→6h 封顶） |
|---|---|---|
| 6h 内可发起的重产次数（推算） | 受 produce 时长限制，约 6–8 次 | 4 次 |
| 上限 | 无 | 无（只是间隔变长）|

代价：故障恢复后最坏要多等一档（配额类最长 6h）才回到线上。这是刻意的取舍——
"多等 6 小时" 换 "不再每小时烧掉 2–3 次 15–55 分钟的整条重产"。
**没有加终止上限**：`INFRASTRUCTURE_WAIT_FAILURE_KINDS` 按设计就是无界等待，
本次只改间隔不改这一点。

### 5.1b 退避够不够？——`503/524` + "小请求通、大请求挂"

orchestrator 实测：`gpt-5.6-sol` 走 `/responses` 发**小**请求当前 HTTP 200 正常；
交棒记录写明**大**请求（≥3.6 万字）408 / 400 `group_capability_unavailable`；
现在又实测到 503 / 524（524 = Cloudflare 源站超时）。

**我的判断：退避是必要的，但很可能不充分。** 依据分列：

*退避仍然正确的依据*
- 503 与 524 语义不同：503 是"服务此刻不可用"（容量抖动），重试是标准解法；
  524 是"源站没在 CF 的窗口内回话"。两者同批出现说明至少有一部分是容量抖动。
- 该 leg **是活的**（小请求 200），不是全挂。8/10 事故记录的形态也是同一分钟里
  200/400/408/503 混着来、sol 成功率 ~80%——在这种 regime 下重试把多数失败转成成功。
- 我的改动**不增加**单次 payload，且总时长仍被 `CPA_DEADLINE_SECONDS=400` 封顶，
  所以"退避不够"的情形下它也不会把事情变更糟。

*退避不充分的依据*
- 如果失败与 payload 大小**相关**，重试同一个大请求只会同样超时——
  524 尤其指向"源站耗时"，而耗时随 prompt / reasoning 长度增长。
- 模型失效备援（sol→5.5→5.4）只在各 leg 容量不同时才帮得上忙。

*但这里有个硬伤：这个推论目前无法证伪。* **全仓没有任何一处记录过请求体大小**——
`llm_client` 不记，桥接不记，provider_detail 里也没有。所以"大请求系统性失败"
至今只是推论。本次因此加了一行成本极低的埋点：桥接的每条 attempt stderr 现在带
`body_bytes=<字节数>`，而该 stderr 已经被 `provider_failure_detail` 原样收进
`provider_detail` 落盘。**下一轮失败就能直接从 state 读到"哪个字节数在什么状态码
上失败"**，届时判据是：

- 同一 `body_bytes` 量级反复 524 / 408 → 退避治不好，得**降上下文 / 分块 /
  换腿**（终审审片 prompt 是最大的那个，`producer_text_pipeline` 侧可控）。
- `body_bytes` 与状态码不相关 → 就是瞬时抖动，退避是对的解法。

**在拿到这份数据之前，我不建议动分块/降级**——那是对终审审片证据面的结构性改动
（prompt 一分块，"对最终字节的一次独立重扫"这个性质就要重新论证），凭推论去做
风险远大于收益。

### 5.2 未做：`producer_error` / `unknown` 的 typed 归类（8/8 最大头，10 次）

只读拉了 free 上的实际 tail，样本如下：

- `auto_230125_960_1072`：`failure_message = "producer exited without diagnostic"`，
  但同一候选的 produce 日志尾部是一份**成功的交付 JSON**（`cover_status:
  AI_COVER_READY`、title、delivery 路径俱全）。
- `auto_213135_469_710`：`failure_message` 是一行 `[agy] bounded sparse-cue self-heal:
  {...}` —— 分类器只取本次 attempt 输出的最后一行非空文本，producer 非零退出但
  stderr 尾部没有任何可识别 marker。
- `auto_233123_315_412` / `_782_859`：日志尾部反复是
  `BOUNDARY_SEMANTIC_REVIEW_REQUIRED: [... "SELECTOR_STORY_WITNESS_INSUFFICIENT" ...]`。

**没有动手，是因为证据不足以支撑 typed 分支**：日志是 append-only 跨 attempt 的，
而分类器只看本次子进程写的字节；要把这些 tail 归到具体原因，得先拿到"本次 attempt
的完整 stderr"而不是共享日志文件的尾巴。凭现有样本硬加分支就是在猜。
**建议下一步**：让 producer 在任何非零退出前必落一行 typed marker（哪怕是
`PRODUCER_EXIT_UNCLASSIFIED:<最后一个已完成阶段>`），把"没有诊断"从沉默变成证据。
注意 `producer_error` 当前是 `recoverable=True` 但不在 `INFRASTRUCTURE_WAIT_FAILURE_KINDS`
里，所以它只吃**一次** transient 重试（`transient_count < 1`），不会无限空转。

### 5.3 未做：`subtitle_authority` / `final_review_findings`（8/7 最大头，10 次）

orchestrator 的假设是「AGY 周配额耗尽 → 声学证人无法给出确定裁决 → findings 恒停在
UNCERTAIN → 恒拦」。**本次既未证实也未证伪**，理由：

1. orchestrator 后续核实 **F21（声学证人接线修复，`cfcc929`）已合入并部署**，是当前
   部署位的祖先。8/7 那批样本属于 F21 **之前**的代际，不能当现状证据。
2. 现有 state 里那批记录的 `failure_evidence` 只留 `_compact_final_review_finding`
   压缩过的字段，**不含**每条 finding 的裁决状态（`JUDGE_UNAVAILABLE_KEEP_CURRENT` /
   `WITNESS_UNAVAILABLE_KEEP_CURRENT`），无法从盘上直接判定。

若要继续：仓里已有对的那扇门——`producer_text_pipeline.py:2005` 把
`infra_unresolved` 行走 `SystemExit(FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED)`
→ `provider_transient`。它目前只覆盖**被 provider 拦掉的修复提案**，不覆盖
**证人不可用导致无裁决**的 findings。要修就是把 infra 标注扩到后者，让
provider 不可用的裁决在 `FINAL_REVIEW_RELEASE_BLOCKED` **之前**走 transient 门。
**前提是先用 F21 之后的样本证实这个形状还在。**
⚠️ findings 门本身不许放宽（2026-07-26 记录：它校验的是对最终字节的独立重扫）。

### 5.4 未做：断点续产

见 §2.4。成本是给 producer 引入阶段状态机 + 每阶段产物哈希绑定，超出本次范围。

### 5.5 未覆盖的调用点

见 §1.3 四条。都不判死候选，故未修。

### 5.6 一个需要 Ivan 知情的连带效应（不修，只披露）

把这些死亡改判成 `provider_transient` 之后，`free_session_autoslice.py:1864` 的
`if recoverable_failure: break` 会生效——**一次长故障期间 talk lane 会原地停下
等待，而不是换下一个候选顶上**。这是既有设计意图（注释写着"不要花掉第二个候选
去掩盖故障"），本次没有改动，但改判会让它触发得更频繁。如果 Ivan 更想要
"故障期间继续出别的片"，那是另一条独立裁定。

### 5.7 部署前必读

1. **波及面**：见 §3.4。本改动同时改了全局 / 歌 / `content_boundary` /
   `subtitle_authority` 四个 fingerprint。部署即触发这四类失败件 + 已产未发包的
   全量 requeue。`speaker_evidence` / `runtime_prerequisite` 不受影响。
2. **合并基**：本分支基于 `a2b07e8`；期间 main 已推进到 `0ca0caf`（另有 worker
   落地）。本改动碰了 `talk_lane.py` 与 `llm_via_cpa.sh`，都是可能的冲突面；
   且 `test_runtime_architecture` 的债务账本**钉的是精确行数**，两边各自绿的
   文本合并也可能把行数推过账本。**合并后务必重跑
   `python3 -m pytest tests/test_runtime_architecture.py -q`。**
   本分支交付时 `talk_lane.py` = 1999 行 / `produce_talk` = 296 行，
   **均低于修改前**（2016 / 302），没有向账本新增任何条目。
