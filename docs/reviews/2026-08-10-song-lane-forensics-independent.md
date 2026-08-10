# 歌切 lane 成建制失效根因诊断（2026-08-07 / 08-08）

- 取证时间：2026-08-10（只读）
- 取证面：`ssh free`（`/opt/bilive/autoslice` state / out / logs / dmesg）+ Mac 仓代码与 git 历史
- 部署位：`free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT` = `a2b07e86e40efebe1e7fa702431e84d932168222`（deployed 2026-08-10T05:50:17Z）；已核对 `src/autoslice/song_lane.py` md5 `fc6de7896fe10be65c0c765780832e19` 与 Mac HEAD `a2b07e8` 逐字节一致，故本报告引用的 Mac 行号即 free 现行行号
- 纪律：未写 free 的任何 state/out/repo，未 deploy，未上传；本仓只新增本文件

---

## 0. 一句话结论

**歌 lane 不是被内容门挡住的，是被四个代码缺陷串成的死路挡住的。**
其中一条（`song_common.py:335` 的 `agy_rc < 0`）在 2026-08-08 把一份**已经完整成立的正向证据**（心型病毒 (Live)，28/28 行全部 `heard`、28/28 `LIDOUSHA SINGING_THIS_LYRIC`、`FULL_STUDIO_SEQUENCE`、有头有尾）判成 `SONG_AUDIO_LRC_ALIGNMENT_INVALID` 丢弃；另外三条把失败包装成"基础设施瞬时故障"，让它**无上限重排队**并**永久占住每场 1 个交付名额**，把同场另外 8 个候选饿死在 backlog。

按 Ivan 逐字令「歌配额每场 1」计，这两晚的理论上限就是 2 条。实际证据支持能救回的是 **1 条（8/8 心型病毒）**；8/7 六条**没有任何一条持有可交付的正向证据**。

---

## 1. 版本真相（8/7 与 8/8 表现不同的原因）

| 事件 | 时间（UTC） | 证据 |
|---|---|---|
| 8/7 歌 lane 六次尝试 | 2026-08-07 19:02–20:45（+8/8 06:40 一次 revival） | `logs/2026-08-07_song*.log` mtime |
| `4af4a88` "recognize Jingting provider outage as infra-wait, not terminal reject" | 2026-08-08 20:06 | `git log -1 --format=%ad --date=iso 4af4a88` |
| 8/8 歌 lane 全部尝试 | 2026-08-09 02:53–06:46 | `logs/2026-08-08_song*.log` mtime、`logs/runner.log:17257-17366` |

即：**8/7 跑在 `4af4a88` 之前**（失败→终态 `candidate_rejected`），**8/8 跑在其之后**（失败→被判 transient→无限重排）。同一批根因，两种外观。

---

## 2. 逐条拒因

### 2.1 2026-08-07：`songs` 六条，全部 `status=candidate_rejected` / `decision=REJECT`

`song_terminal_disposition` 一律 `DETERMINISTIC_CONTENT_REJECTION` / `retryable:false` / `revival_authority: EXPLICIT_OPERATOR_REVIVAL_REQUIRED`。

| # | candidate_id | 发现 lane | 窗口 | 终态 reason_code | 真正卡死的门（逐字） |
|---|---|---|---|---|---|
| 1 | `songvis_203735_830_3d9fc4bd`（"na"） | visual_song_inventory | 830–1560s | `SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS` | `agy_audio_lrc_identity`: `ambiguous low-ASR LRC identity: best=18%, runner-up=11%, need best>=20% and margin>=8%` |
| 2 | `songvis_203735_10_d37287dc`（"Leee"） | visual_song_inventory | 10–140s | 同上 | `best=9%, runner-up=7%` |
| 3 | `songvis_203735_200_fe053083`（"町"） | visual_song_inventory | 200–370s | 同上 | `best=19%, runner-up=17%` |
| 4 | `songvis_203735_140_4d93a748`（"中生"） | visual_song_inventory | 140–200s | 同上 | `best=8%, runner-up=8%` |
| 5 | `song_213743_1754` | semantic_recall | 1754–1803s | `SONG_AUDIO_LRC_ALIGNMENT_INVALID` | AGY(rc=0) 实听后拒：`variant 1/1 title='【雷军】全世界死机2'; ASR_support=2/62 (23% row recall); validator rejected: canonical LRC line 0 was not affirmatively heard` |
| 6 | `song_230754_1118` | semantic_recall | 1118–1274s | `SONG_AUDIO_LRC_ALIGNMENT_INVALID` | AGY(rc=0) 实听后拒：`variant 1/1 title='一起长大 (live)'; ASR_support=1/35 (3% row recall); validator rejected: canonical LRC line 0 was not affirmatively heard`（该条 8/8 被 operator 以 `expected_fix_commit=8d4b09b` revive 过一次，第二次仍败） |

补充事实：
- 六条的 `reason_codes` 全部含 `JINGTING_PROVIDER_NOT_AGY` + `JINGTING_MODEL_MISSING`（见 §4.1，这是 100% 假阳性）。
- 六条恰好用满 `SONG_ATTEMPT_CAP = 6`（`scripts/free_session_autoslice.py:289`），导致 `song_backlog` 里 4 条（`songvis_203735_1030/1100/1140/1560`）**当天一次都没被尝试**。
- 8/7 直播场景上下文 = `EVENT_CONTEXT_NOT_PROVEN_FAIL_CLOSED`（无 3D/歌回关键词），四条 visual 候选窗口为 30–170s 的"歌单行可见区间"，物理上装不下一首完整歌（详见 §4.4）。
- `runner.log`（2026-08-10 05:46:26）：`2026-08-07 batch finished [ready_unpublished_with_failures]: … song 0 delivered / 0 retry-blocked / 6 rejected / 6 attempted`。

### 2.2 2026-08-08：`songs=[]`，`pending_song=1`，`song_backlog=8`，`song_superseded_attempts=26`

- **pending 那条 = `song_200130_1012`（《花之塔》，弹幕 622）**，`retry_reason: "transient_infrastructure_failure"`，`transient_retry_count: 4`。
- 26 条 superseded attempts **全部** `retry_reason: transient_infrastructure_failure` / `status: blocked`，覆盖 6 个 cid，每个 4–5 轮。
- 没有任何一条进入终态；`songs` 列表被清空，全部回到队列。
- `runner.log`（2026-08-10 04:40:00 / 05:47:02）：`2026-08-08: 1 song item(s) deferred for deploy — resuming next tick` → `song 0 delivered / 0 retry-blocked / 0 rejected / 0 attempted`。

**pending 到底卡在哪（直答）**：卡在一个**没有上限的 transient 重排环**里。
`src/autoslice/delivery_recovery.py:1224-1303` 用 `transient_retry_count` 只做**指数退避**（`SONG_INFRA_RETRY_BASE_SECONDS=15min` → `SONG_INFRA_RETRY_MAX_SECONDS=6h`，`free_session_autoslice.py:293-294`），**全仓没有任何 `transient_retry_count` 上限**（grep 已确认）。只要 `transient_failure_code` 一直被设上（§4.1 保证它一定被设上），这条 candidate 就永远 `blocked → requeue → blocked`，并且因为 `refill_songs`（`candidate_selection.py:918-969`）把 `selected_repair` 项**优先塞进** `song_delivery_budget`（= `MAX_SONGS_PER_SESSION - 已交付` = 1），它**永久霸占那唯一的名额**，另外 8 条只能进 `song_backlog`。

各 cid 的真实底层失败（从 `song_repair` 回执逐字取）：

| cid | 歌（真实） | LRC 发现是否命中正主 | 底层失败 | 类别 |
|---|---|---|---|---|
| `song_200130_1012` | 花の塔 | ✅ `'花の塔' (netease://song/1956534872)` | `0% of 36 lines matched` → `ambiguous low-ASR LRC identity: best=19%, runner-up=16%`（19% 来自噪声候选《乌鲁木齐九月》） | ④ 跨字系死路 |
| `song_203132_1342` | 花の塔 | ✅ 命中两版，均 `0%` | `best=14%（小鸡小鸡）, runner-up=7%` | ④ |
| `song_210131_1210` | **心型病毒 (Live)** | ✅ 排到 primary，`96% row recall` | **`validator rejected this variant: ValueError: audio aligner Gemini API failover metadata is invalid`**（两个 variant 同因） | **④ 代码缺陷 D1** |
| `song_213135_1073` | 夜に駆ける | ✅ `'夜に駆ける' 0% of 56 lines` | 身份门选中噪声候选《无照青春》16%；failover 对齐结果 72 行 **0 heard / 0 LIDOUSHA** → 确属选错歌 | ① 内容（但选错源自 ④） |
| `song_210131_1481` | 初恋サイダー / 勇气? | ✅ `'初恋サイダー' 0%`、`'勇气' 0%` | `best=12%（特别的爱给特别的你）, runner-up=5%` | ④ |
| `song_213135_1541` | Maria (华莎) | ✅ `'마리아 (Maria)' 0%`、`'Maria (LIVE)' 0%` | 身份门选中《踏浪》4% → AGY(rc=0) 实听拒 | ① 内容（选错源自 ④） |

8/8 直播场景 = `LANDSCAPE_EVENT_CONTEXT_CONFIRMED` / `3DLive`（生日 3D 舞台），候选全是真歌、弹幕 133–1103。

---

## 3. 归类（按"根因门"计，不按 reason_code 条数）

| 类别 | 8/7 | 8/8 | 说明 |
|---|---|---|---|
| ① 内容/质量真门 | **2**（`song_213743_1754`、`song_230754_1118`） | **2**（`song_213135_1073`、`song_213135_1541`） | AGY 以 rc=0 实听后拒（`canonical LRC line 0 was not affirmatively heard` / 0 heard）。这 4 条的"内容不成立"是真的，但**它们被判的是错误的歌**（身份门从噪声底噪里挑出来的），所以只是"用错答案得出的正确否定" |
| ② 基础设施/provider | 0 | **AGY 被 OOM-kill 9 次**（2 个候选） | `dmesg`：`Aug 9 06:10:43` / `06:21:28` `Out of memory: Killed process (agy) anon-rss:15165328kB`（AGY 单进程吃 ~14.5GB / 全机 31GB）。**但 Gemini API failover 已经正常顶上并产出了有效结果**，所以纯基础设施本身没有阻断交付 |
| ③ 配置/环境缺失 | **0** | **0** | 见 §4.2、§4.5：venv-diar 完好、AGY 常驻进程在、3 把免费 key 在（`accepted_key_ordinal=3, tier=free`）、CPA 无 429 |
| ④ 代码缺陷/死路 | **6/6 都被它污染**（4 条直接由它致命） | **6/6 全部**（2 条直接致命，6 条全部被无限 transient 环困住） | 四个具体缺陷见 §4 |

**每类条数（可机读汇总，12 个候选 = 8/7 六 + 8/8 六）**：① 内容真门 **4**；② 基础设施 **1 起事件 / 9 次 run**（AGY OOM-kill，但 failover 已顶上，未独立阻断交付）；③ 配置环境缺失 **0**；④ 代码缺陷 **12/12 全覆盖**（其中 **8 条**以它为唯一致命门：8/7 四条 visual + 8/8 `song_210131_1210`（D1）、`song_200130_1012`/`song_203132_1342`/`song_210131_1481`（D4 身份门死路）；另 4 条归 ①）。①②④ 有重叠是因为同一候选可能先被 ④ 选错歌、再被 ① 正确否定；表内以"根因门"归属为准。

---

## 4. 四个代码缺陷（按杀伤力排序）

### D1 — `agy_rc < 0` 把成功的 Gemini failover 判成"元数据非法"（**最高杀伤**）

`src/autoslice/song_common.py:329-338`：

```python
if provider == GEMINI_API_AUDIO_LRC_PROVIDER:
    if (
        model != GEMINI_API_AUDIO_LRC_MODEL
        or provider_fallback_used is not True
        or agy_failure_category not in AGY_AUDIO_LRC_FALLBACK_FAILURE_CATEGORIES
        or sandbox is not False
        or (
            agy_rc is not None
            and (isinstance(agy_rc, bool) or not isinstance(agy_rc, int) or agy_rc < 0)   # ← 335
        )
    ):
        return "audio aligner Gemini API failover metadata is invalid"
```

实测 run manifest（`out/2026-08-08/song_210131_1210/song_selector_full/attempt-5w629ekw/.../variant-01/.../run.manifest.json`）：

```
provider='gemini_api'  model='gemini-3.6-flash'  agy_rc=-9
provider_fallback_used=True  agy_failure_category='AGY_FAILED_RC'  sandbox=False
configured_key_count=3  accepted_key_ordinal=3  accepted_key_tier='free'
```

前四项全部合法，**唯独 `agy_rc = -9` 触发 `agy_rc < 0`**。`-9` 正是 Python `subprocess` 对被 `SIGKILL` 杀死的子进程的返回值——也就是 §3 那两次 OOM。**AGY 被系统杀掉本身就是 failover 的触发条件，却被同一个校验器当成"元数据非法"的理由。**

被丢弃的东西（`alignment.canonical.json`，同目录）：

```
rows=28  heard=28/28  lidousha_role=SINGING_THIS_LYRIC 28/28
live_performance: continuous_live_song_performance=true, confidence=0.95,
                  background_recording_likelihood=0.05
live_arrangement:  FULL_STUDIO_SEQUENCE, observed_live_song_opening=true,
                   observed_live_song_ending=true
第 0 行文本 "初次见面明明没有感觉" —— 与 state 里该候选 preview 逐字吻合
```

且这不是 AGY 顺从性回声：独立的 ASR 行召回是 `23/28 = 96%`，两路互证。

范围：8/8 共 **11** 次 aligner 调用（两晚合计 13，8/7 另有 2 次 `agy rc=0`），其中 **9 次是 `gemini_api rc=-9`，100% 被这条判死**——落在 `song_210131_1210`（8 次）与 `song_213135_1073`（1 次）；剩余 2 次是 `agy rc=0`（`song_210131_1481`、`song_213135_1541`）。

历史：这段是 `d772269`（2026-07-30 "restore bounded Gemini audio failover"）加回来的，`agy_rc < 0` 是当时新写的收紧项。它**直接违反 Ivan 2026-07-19 逐字令**（§6）。

### D2 — JINGTING 出处门对歌 lane 没有豁免，产出 100% 假阳性

- 歌 lane **故意不跑 AGY 文本精炼**：`src/autoslice/source_context_executor.py:301-315` 在 `refinement_required=False` 时写入 `provider="source_draft_context"`、`model=None`、`provider_request_id="BYPASSED_NOT_AUTHORITATIVE_FOR_SONG_LRC"`。
- `source_context_executor.py:550` 的 `_agy_reason_codes` **正确豁免**了这种情形。
- 但 `src/autoslice/auto_review.py:404-445` 的 `evaluate_jingting_provenance()` **完全不知道 `refinement_required`**，硬要 `provenance.provider == "agy"`（:420）和 `provenance.model` 非空（:435）。
- 唯一的绕过口是 `review_candidate()`（`auto_review.py:197`）的 `verified_song_lrc_authority`，而它（`scripts/run_auto_review_shadow_pipeline.py:474-479`）要求 `evidence.song_complete is True and evidence.lyrics_alignment_ready is True` ——**只有歌已经证成时才免检**。

结果：**任何一首没证成的歌，必然多出 `JINGTING_PROVIDER_NOT_AGY` + `JINGTING_MODEL_MISSING`**。8/7 六条、8/8 二十六条 attempt，无一例外。且这两个码在 `auto_review.py:218-224` 的 `hard_block_prefixes` 里，把 decision 钉成 BLOCK。

### D3 — D2 的假阳性 + `4af4a88` = 不可终结、不可让位的僵尸候选

`scripts/free_session_autoslice.py:309-328` 把 `JINGTING_PROVIDER_NOT_AGY` / `JINGTING_MODEL_MISSING` 列入 `SONG_INFRA_TRANSIENT_REASON_CODES`。
`src/autoslice/song_lane.py:637-648`（`4af4a88`）：任何残留 infra 码 → `transient_code = min(remaining_infra)` → 设 `transient_failure_code`。
`src/autoslice/batch_terminal_state.py:63-68`：显式 transient **压过** 内容否定 → 拒绝终态化。
`src/autoslice/delivery_recovery.py:1227-1231`：`explicit_transient` 存在时**跳过**"内容否定优先"的回退判断。
`delivery_recovery.py:1224/1303`：只累加 `transient_retry_count`，**没有任何上限**。
`candidate_selection.py:934-940`：`selected_repair` 项**优先占用** `song_delivery_budget`（每场 1）。

⇒ 一条内容失败的歌，被贴上假的 infra 标签，就变成**永不终结、永远重排、永远占着唯一交付名额**的僵尸。这就是 8/8 "0 产出"的直接机制。

`4af4a88` 的注释自陈是为修 "2026-08-07 song_230754_1118 recurrence"，但它修的是**症状的方向**（terminal→wait），根因（D2 的假阳性）没动，于是把"错误地终态拒绝"换成了"永远不出结果"。

### D4 — 跨字系身份门：精确标题命中要求 `ratio > 0.0`，而中文 ASR 对日/韩歌词恰好是 0.0

`src/autoslice/song_alignment.py:857-871`（`369e92b`/`8d4b09b` 的"精确标题只要求非零 ASR 关联"修复）：

```python
if max(item[0] for item in group) > 0.0
and any(_lrc_title_is_exact_hint(item[1], preferred_title_hints) for item in group)
```

实测：`花の塔` `0% of 36 lines`、`夜に駆ける` `0% of 56`、`アイドル(Idol)` `0% of 74`、`마리아 (Maria)` `0% of 58`、`初恋サイダー` `0% of 32`。**全部恰好 0.0**，绕过口一次也没打开。
于是身份门退回按 ASR ratio 排序，从**中文同音噪声底噪**里选出《乌鲁木齐九月》19%、《无照青春》16%、《特别的爱给特别的你》12%、《踏浪》4%、《【雷军】全世界死机2》23% 作为"最佳候选"，再被后续门（`min_recall_ratio=0.20` / `min_margin=0.08`，`song_repair.py:272-273`；`min_matched_ratio=0.55`，`song_repair.py:994）判 AMBIGUOUS 或送 AGY 实听后判 INVALID。

**LRC 发现从来没错**——正主几乎每次都被抓到；错的是"用中文 ASR 的字面重合度当身份证据"这一步。

### D5（伴生）— 视觉歌单 lane 的窗口不是演唱区间

8/7 四条 `songvis_*` 的 `visual_song_evidence.reason` 逐字：
`"Yellow panda cursor box highlights row Leee starting at 10000 ms, continuing until row 中生 is visible at 140000 ms."`
`"Active song row state transitions to 町 at 200000 ms, continuing until list cycle transition to Leee at 370000 ms."`
这是**歌单面板某一行的可见/高亮区间**，不是演唱区间。产出的窗口是 30s / 60s / 170s / 730s；标题是被截断的行 OCR 片段（`na` / `Leee` / `町` / `中生`）。这些候选**在物理上不可能通过完整歌门**，却各吃掉一格 `SONG_ATTEMPT_CAP`。

---

## 5. 任务点名的已知线索：逐条证实/排除

### 5.1 `visual_song_discovery` 在无 AGY 机器上 fail-open 成"没有歌" → **在 free 上不适用（排除）**
free 上该 lane 确实产出过候选：8/7 `visual_song_seen_entries = 8`（4 条进 `songs`、4 条进 `song_backlog`）；8/10 `runner.log:17516` `22966160_20260809-20-06-15.mp4: visual song inventory 1 new / 1 visible (AGY High)`。8/8 该 lane 产出 0 条，但那天 `semantic_recall` 独立产出了 9 个真候选，**不是发现缺失**。
**但要另记一条**：free 上它没有 fail-open，却有 **fail-noisy**（D5），实际危害是吃光尝试帽。

### 5.2 `--host-vocal-python` / 声纹 diar 依赖 → **free 上完好（排除）**
`/opt/bilive/autoslice/venv-diar/bin/python` 存在，`import torch, modelscope` 成功（`torch 2.5.1+cpu`）。
`HOST_VOCAL_PYTHON` 默认指向该路径（`free_session_autoslice.py:257`），`song_lane.py:340` 正常传参。
两晚的 reason_codes 里只有 `SONG_HOST_VOCAL_UNPROVEN`（"没跑到"），**没有任何 host-vocal 执行错误码** → 是前置门先挂，不是声纹环节坏。wsl 车道的 venv-diar 缺失问题**不适用于 free**。

### 5.3 `song_lane.py:173`"重试复用切自另一区间字节" → **已修，未复发（排除）**
`song_window_media_path()`（`song_lane.py:166-183`）把 `{start_ms}_{end_ms}` 绑进文件名；`fresh_song_selector_dir()`（:143-159）给每次 invocation 开独立 `attempt-*` 子目录。
free 上实测：`song_200130_1012_tight_997310_1314400_source.mp4`（区间入名）、`song_selector/attempt-u2ecv1id|d4tmayil|ttqfif8q|noqobgbz/`（4 次尝试 4 个目录，互不覆盖）。未发现跨区间复用字节的迹象。

### 5.4 F19 歌名歌词语义门 → **两晚都没有参与（排除）**
`d2b6c1c` 落地时间 2026-08-09 13:34 EDT，晚于两晚的全部 run。
`state/2026-08-07.json` 与 `state/2026-08-08.json` 中 `song_name_semantic` / `SONG_NAME` / `LYRICS_UNAVAILABLE` / `DISPUTED` 出现次数**均为 0**。
且 `song_name_semantic_verification.py:80` 的 purpose 是 `talk_song_name_semantic_verification`——它是 **talk 侧**歌名 pin 的门，不在歌 lane 交付链上。

### 5.5 identity 门（说话人/演唱者） → **一次都没触发（排除）**
`HOST_NOT_SINGING` / `SONG_BACKGROUND_PLAYBACK_ONLY` 在两天 state 中出现 **0 次**；`SONG_PARTIAL` 也是 **0 次**。
全部候选停在 `SONG_HOST_VOCAL_UNPROVEN`（未证），不是"证否"。

### 5.6 零 recall 门 → **是真凶之一，但形态与旧描述不同（证实，需改述）**
不是"零 recall 直接拒"，而是 §4 D4：**精确标题旁路要求 `ratio > 0.0`，跨字系恰好等于 0.0**，旁路失效后回落到 `best>=20% / margin>=8%` 通用地板，再被噪声候选顶掉。8/7 的 4 条 + 8/8 的 3 条都死在这里。

---

## 6. 配额与 provider 链

**歌 lane 的 provider 链（按环节）**：
1. 源 ASR 草稿 = BCUT ASR（`cache/<date>/*.bcut.srt`）。
2. 精听精炼 = **歌 lane 主动跳过**（`provider="source_draft_context"`，见 D2）。
3. LRC 发现 = 网易云 + `llm_song_hint`（CPA LLM 猜歌名）。
4. **音频对齐/演唱证据 = AGY 订阅 → Gemini API 免费 key → 付费 key**（`song_common.py:66-73`，`AGY_AUDIO_LRC_MODEL="Gemini 3.6 Flash (High)"` / `GEMINI_API_AUDIO_LRC_MODEL="gemini-3.6-flash"`）。
5. 语义/发布门 = CPA。
6. 主唱声纹 = 本地 CAM++（`venv-diar`），无外部配额。

**今夜 CPA OAuth 周配额耗尽对这两晚有无影响：无。**
- 8/8 六个 song 日志中 `CPA_RATE_LIMITED|429|quota` 命中数 **全为 0**。
- 两天 state 中 `CPA_RATE_LIMITED` / `AGY_QUOTA_EXHAUSTED` 出现 **0 次**。
- 8/8 实际用掉的是 **免费 Gemini key 第 3 把**（`accepted_key_tier='free'`, `accepted_key_ordinal=3`），完全在 Ivan 规定的顺序内。
- **但对"现在做复现/复活"有影响**：第 3、5 步要 CPA，复活跑之前得确认 CPA 可用，否则会引入新的 `CPA_*` 噪声码，再次触发 D3 的僵尸环。

**Ivan 逐字裁定（已用 `search_session_transcripts` + 原始 jsonl user turn 核验）**

> 2026-07-19T23:28:37Z（`~/.claude/projects/-Users-ivan-Project-vtuber-slice/4e3c1232-5df6-4944-bdbf-5e290ae53492.jsonl`，user turn）
> 「什么叫只差AGY，AGY不是还有gemini API 吗，gemini和AGY是一样的啊。codex已经解除。你不要动disable。**歌配额每场1.** API不是代打，API就是gemini 模型，AGY也是gemini模型，只是有调用顺序的区别而已，你把这个内容记住。先调用AGY订阅，然后用免费key，最后用付费key。」

> 2026-08-08T19:44:00Z（`5cbe14f2-2623-4f3f-8308-060346f7e8ab.jsonl`，user turn）
> 「…还有，精听provider不是AGY和Gemini API吗，怎么会死在这里？付费API可以用啊」

> 2026-08-08T19:48:40Z（同上，user turn）
> 「老毛病竟然还重新犯，你必须修复？…」

D1 与 D2 都**直接违反** 7/19 那条逐字令；Ivan 8/8 本人已经点过同一现象。

---

## 7. 7/19 记忆里的三缺口：现状逐条核对

| 缺口 | 现状 | 证据 |
|---|---|---|
| **尝试帽饿死真歌** | **仍在，且演化出更狠的第二形态** | ①旧形态：8/7 `SONG_ATTEMPT_CAP=6` 被 4 条 visual OCR 片段吃掉，`song_backlog` 4 条**零尝试**（`candidate_selection.py:958-963`）。②新形态：8/8 一条僵尸候选（D3）无上限占住 `song_delivery_budget=1`，另外 8 条全部饿死（`candidate_selection.py:934-940`） |
| **fingerprint 重排重烧** | **已修，未复发** | 见 §5.3。另 `delivery_recovery.py:1265` 的 `content_change_retry` 还受 `SONG_LIFETIME_ATTEMPT_CAP=18` 约束；8/8 已有 26 次 superseded，该路已死（复活必须走 operator revival） |
| **provider 层门过度限制（>5min 窗 0 行=没分块）** | **门仍在（D2），但"0 行"症状未复发** | 门：`auto_review.py:420,435` 依旧硬要 `provider=="agy"`。分块：8/8 `performance_window` 步骤 `SUCCESS 46 cues 0..318820ms`（318s 窗有 46 行），说明分块正常。**该门现在的危害从"终态误拒"变成了"无限等待"（D3），比 7/19 更糟** |

---

## 8. 修复设计（只出设计，不实现）

> 排序原则：最小改动 × 最大产出。三条按依赖顺序执行；F1 单独就能解锁 8/8 的配额。

### F1（P0）— 放行被 SIGKILL 触发的 Gemini failover

**落点**：`src/autoslice/song_common.py:329-338`，具体是 **:335** 的 `or agy_rc < 0`。

**改法**：`agy_rc` 的语义是"AGY 为什么没能用"，**任何非零（含负数信号退出）都应是合法的 failover 触发条件**。删掉 `agy_rc < 0`，改为"`agy_rc` 必须是 int 且 ≠ 0（`None` 仍允许，表示 AGY 未启动）"，并把 `-N` 显式归一到 `AGY_FAILED_RC`/新增 `AGY_KILLED_BY_SIGNAL` 类别（`song_common.py:74-84` 的 `AGY_AUDIO_LRC_FALLBACK_FAILURE_CATEGORIES`）。**不要**放宽 `model`/`provider_fallback_used`/`sandbox` 三项——那三项是真正的防伪面。
同步改：`src/autoslice/agy_lrc_alignment.py:406-412` 的 `_classify_agy_nonzero`，把 `returncode < 0` 显式分类，别和超时混在一起。

**金丝雀**：
1. 单测（离线，进 `tests/test_song_repair.py`）：构造 `provider=gemini_api, model=gemini-3.6-flash, provider_fallback_used=True, agy_failure_category=AGY_FAILED_RC, sandbox=False, agy_rc=-9` → 断言 `validate_audio_lrc_execution_metadata(...) is None`；同时保留 4 条负向 canary：`sandbox=True` 拒、`provider_fallback_used=False` 拒、`model` 漂移拒、`agy_failure_category=None` 拒。
2. **重放金丝雀（关键，零新 API 调用）**：拿 free 上现存的 `out/2026-08-08/song_210131_1210/song_selector_full/attempt-5w629ekw/seededsong_120000_242040/song_repair/agy_audio_lrc/variant-01/seededsong_120000_242040-u2eehj25/`（`run.manifest.json` + `alignment.canonical.json` 已在盘）做离线重放，断言：`validate` 通过 → `live_performance.status=READY` → `lyrics_alignment_ready=True`。这是唯一能在不重新烧钱、不重跑 AGY 的前提下证明修复真的救回这一条的方式。
3. 生产金丝雀：仅对 `song_210131_1210` 单条 operator revival 重跑，看它能否走到 host-vocal（CAM++）环节。

**预计救回**：**8/8 恰好 1 条**（`song_210131_1210` 心型病毒 (Live)）——正好等于 Ivan 的每场 1 配额，即 8/8 从 0 变 1。
`song_213135_1073` 那次 failover 虽然也被 D1 判死，但其 canonical 结果是 **72 行 0 heard / 0 LIDOUSHA**（选错歌），修了也救不回来，不计入。
8/7 无 `gemini_api` 调用（两次都是 AGY rc=0），**F1 对 8/7 = 0 条**。
残余风险：`song_210131_1210` 后续还要过 CAM++ 主唱声纹与 CPA 发布门，这两环两晚都没跑到过，不能承诺一定落地。

### F2（P0）— 掐断"假 infra 码 → 僵尸候选"这条链

**落点（两处二选一或都做，建议都做）**：
- **主改**：`src/autoslice/auto_review.py:404-445` `evaluate_jingting_provenance()` 增加 `refinement_required: bool = True` 形参（与 `source_context_executor.py:548-556` 的既有豁免逻辑对齐）：当 `refinement_required=False` 且 `provenance.provider == "source_draft_context"` 且 `provider_request_id == "BYPASSED_NOT_AUTHORITATIVE_FOR_SONG_LRC"` 时，`JINGTING_PROVIDER_AGY` / `JINGTING_MODEL_RECORDED` 两项 `passed=True`。调用点 `auto_review.py:197-199` 与 `run_auto_review_shadow_pipeline.py:480-487` 透传。这样歌候选不再无中生有两个 infra 码。
  **`evaluate_jingting_provenance` 有两个调用点，必须一起处理**：除 `review_candidate`（:197）外，`is_publish_gate_satisfied`（`auto_review.py:177`）也**无条件**调 `_provenance_reason_codes(evaluate_jingting_provenance(...))`，没有任何 `verified_song_lrc_authority` 旁路。只改 :197 的话，同一假阳性会在发布门原地复活。落地前必须二选一并写进 commit：把豁免透传到 :177，或**举证** :177 对歌 lane 是死码（其 docstring 自称 "future uploader code should call"，而现行歌交付走的是 `song_delivery_ok` + `_commit_verified_song_package`——本次取证**未验证**这一点，属于 F2 实施者的必答项）。
- **保险**：`src/autoslice/delivery_recovery.py:1227-1231` + `src/autoslice/song_lane.py:637-648`，给 transient 加**上限**：`transient_retry_count >= SONG_TRANSIENT_RETRY_CAP`（建议 5，与现有 6h 退避封顶配套）时，即便 `transient_failure_code` 存在，也必须落到 `project_terminal_song_disposition` 的内容判定；并在 `candidate_selection.py:934-940` 让**超过上限的 selected_repair 不再优先占用 delivery slot**，把名额让给未尝试过的候选。

**金丝雀**：
1. 单测：一条只带 `JINGTING_PROVIDER_NOT_AGY + JINGTING_MODEL_MISSING + SONG_AUDIO_LRC_IDENTITY_AMBIGUOUS` 的 song record，`refill_songs` + `delivery_recovery` 跑 6 轮 → 断言第 6 轮变 `candidate_rejected`，且 `song_delivery_budget` 释放、backlog 中下一条被提升到 `pending_song`。
2. 负向 canary（必须留）：一条带**真** infra 码（`AGY_QUOTA_EXHAUSTED`）的 record，前 5 轮仍必须是 transient wait，不能被这次改动误终态化。
3. 生产金丝雀：`--once` 干跑一 tick，看 `runner.log` 里 8/8 的 `pending_song` 是否从 `song_200130_1012` 轮换到 backlog 中下一条（`song_203132_1342`）。

**预计救回**：**直接产出 0 条，但它是 8/8 其余 8 条唯一的出场机会**。没有 F2，即使 F1 修好，`song_200130_1012`（花之塔，永远过不了 D4）仍会霸着那唯一名额，`song_210131_1210` 永远排不上。**F1 的 1 条产出实际依赖 F2 或一次 operator 手工 revival。**

### F3（P1）— 跨字系歌不能用"中文 ASR 字面重合度"当身份证据

**落点**：`src/autoslice/song_alignment.py:857-871`（精确标题旁路的 `> 0.0` 条件）与 `src/autoslice/song_repair.py:269-275`（`min_recall_ratio=0.20` / `min_margin=0.08`）。

**改法（最小）**：当 LRC 候选的歌词主体是**非中文字系**（日文假名/韩文谚文/拉丁）而 ASR 是中文时，`ratio` 无意义——把精确标题旁路的门槛从 `ratio > 0.0` 改为「`_lrc_title_is_exact_hint` 命中 **且** 该命中来自 `llm_song_hint` 或 `visual_song_evidence.song_title` 这类**独立于演唱 ASR** 的证据源」，允许 `ratio == 0.0` 直接进入 AGY 音频验证（音频验证本来就是真正的裁判，`canonical LRC line 0 was not affirmatively heard` 这类否定已经证明它有能力把错歌挡下来）。同时给这条旁路加**跨字系判定**做闸门，避免中文歌被顺带放宽。

**金丝雀**：
1. 重放金丝雀：用 free 现存的 `song_200130_1012` / `song_213135_1073` / `song_213135_1541` 的 `song-repair.json`（LRC 候选集与 ratio 全在盘）离线重跑身份门，断言选中的 primary 从《乌鲁木齐九月》/《无照青春》/《踏浪》变成 `花の塔` / `夜に駆ける` / `마리아 (Maria)`。
2. 负向 canary：7/19 群青 evidence-mass 排序测试（`8d4b09b` 提到的那条回归）必须仍绿；再加一条"中文歌 + 0% 命中"必须仍被拒，防止把闸门开成"任何 0% 都放行"。
3. 生产金丝雀：只对 `song_200130_1012`（花之塔，弹幕 622）单条 revival，观察是否首次进到 AGY 音频验证（无论最终 PASS/FAIL，"进到"本身就是这条修复的成功判据）。

**预计救回**：**直接产出 0–1 条**。它不新增交付名额（8/8 已被 F1 的那条占满），真正价值在**后续每一场**：8/8 九个候选里至少 4 个是日/韩曲目，8/7 也有 1 条日语；不修这条，凡是唱外语歌的场次仍然是结构性 0 产出。
**不建议**用它去救 8/7：8/7 六条 + backlog 四条里，四条 `songvis_*` 是 30–170s 的歌单行区间（D5），两条 semantic 已被 AGY 实听否定，**没有任何一条持有可交付的正向证据**；要救 8/7 需要先修 D5 的窗口来源，那不是"最小改动"范畴。

### 汇总预计

| 修复 | 8/7 救回 | 8/8 救回 | 备注 |
|---|---|---|---|
| F1 单独 | 0 | 0（证据在，但名额被僵尸占着）→ 配一次手工 revival 则 **1** | |
| F1 + F2 | 0 | **1** | 达到 Ivan 每场 1 配额上限 |
| F1 + F2 + F3 | 0 | **1** | 8/8 不增量；对后续外语歌场次是从 0 到 1 的差别 |

**8/7 需要单独立项**（visual 歌单 lane 的窗口来源 D5 + 尝试帽被垃圾候选吃光），本报告不给设计。

---

## 9. 未找到 / 未验证（明说）

- **没找到**任何 `transient_retry_count` 的上限实现（全仓 grep 只有累加与退避）。结论"无上限"基于 grep 全覆盖，非反证。
- **没验证** `song_210131_1210` 过 CAM++ 主唱声纹与 CPA 发布门的结果——两晚都没跑到，只能说"前置门一旦放开就会首次跑到"。
- **没验证** AGY OOM（~14.5GB RSS）是回归还是长期特性；只确认 2026-08-09 06:10 与 06:21 两次 `Out of memory: Killed process (agy)`。若 AGY 常态 OOM，F1 之后 failover 会变成常规路径（按 Ivan 7/19 令这是合法的，但值得单独盯内存）。
- **没验证** 8/7 那场是否真的有 6 首以上完整歌可切（场景上下文 `EVENT_CONTEXT_NOT_PROVEN_FAIL_CLOSED`，无 3D/歌回关键词）。因此"8/7 救回 0 条"是对**现有候选**的判断，不是"那晚没有歌"的判断。
