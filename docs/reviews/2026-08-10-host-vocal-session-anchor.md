# host-vocal 会话锚点:48 秒视野把真说话挡在外面

日期:2026-08-10 · 分支 `tmp-hostvocal`(base `beae25c`) · 只读实验在 free,未部署、未写 free 的 state/out/repo

## 结论

`song_210131_1210`《心型病毒 (Live)》(2026-08-08 3D live 生日舞台)被判
`SONG_NOT_LIDOUSHA_SINGING`,**不是**因为这条素材缺少主播演唱证据,而是因为会话锚点的搜索面被一个
固定 48 秒视野截断,搜索范围整个落在**联唱里的下一首歌**上,没扫到真正的歌后说话。

阈值一个没动:`MIN_SESSION_ENROLL_MEDIAN=0.50`、`MIN_CHECKPOINT_MEDIAN=0.31`、
`MIN_PASSED_CHECKPOINTS=5`、`MIN_SESSION_LYRIC_SCORE=0.22`、`SINGING_THIS_LYRIC` 角色要求,全部原样。

---

## 一、只读实验:全场到底有没有 ≥0.50 的说话锚点

### 打分路径可比性(先做这一步,否则结论无意义)

在 free 用部署树 `/opt/bilive/autoslice/repo` + `venv-diar`(modelscope 1.38.1)复算部署 proof 里
13 个候选中的 3 个。该部署树的 `host_vocal_proof.py`(md5 `464b5a46…`)与本工作树逐字节相同。

| start_ms | 部署记录 median | 复算(pairwise) | 逐 ref 最大绝对差 |
|---|---|---|---|
| 258000 | -0.06212 | -0.06212 | **0.0** |
| 278000 | 0.30084 | 0.30084 | **0.0** |
| 306000 | 0.28050 | 0.28050 | **0.0** |

逐位精确复现。`speaker_finalizer._campp_embedding` + `_campp_similarity_score`(仓内既有的
embed-once 生产快路)与 pairwise 差 ≤4.4e-6,故全长扫描用 embed-once。

### 全长扫描(602.136s,窗 8000ms,步 4000ms,150 窗)

| 区段 | 最高 `enroll_median_score` | 位置 | ≥0.50 窗数 |
|---|---|---|---|
| pre_song(<118.5s) | 0.27512 | 0–8000 | 0 |
| in_song(118.5–243.5s) | 0.31466 | 124000–132000 | 0 |
| post_song(>243.5s) | **0.78569** | **548000–556000** | **27** |
| —— 其中部署 48s 视野内 | 0.30084 | 278000–286000 | **0** |

≥0.50 的 27 个窗是连续一片:488000ms 一直到源尾 602136ms,全部 ≥0.5360,其中 12 个 ≥0.70。

原始数据:free `/tmp/hostvocal-scan/{summary.json,scan.jsonl,control.json}`。

### 为什么 48 秒视野打不到

对照 `song_210131_1210_full_source.srt`:

- **258s–314s(部署搜索的全部 13 个窗)根本不是说话。** 243.5s 歌曲结束后是器乐间隙,
  269.94s 起是**联唱里的下一首歌**(SRT 第 58–63 行,罗马音日语歌词,一直唱到 308.69s)。
  0.30 正是"主播在唱歌"对**说话** enrollment 的典型分。
- **真正的歌后说话从 486.04s 开始**(SRT 第 83 行「欢迎回来」),此后 486–602s 全是李豆沙讲话
  (「大家看的开心吗」「我去,紧张死我了」「lovely 选的歌选了三首」…)。
- `258000 + 48000 = 306000`。**视野尾端离真实说话起点还差 180 秒。**

高分窗与 SRT 完全对得上:548–556s 对应「…其实我当时给,呃 / lovely 选的歌选了三首 /
他选了一首最难的 / 然后最适合」,是纯说话。

### 顺带否掉一个替代假设

"3D live 音频链(混响/BGM 串音)系统性压低相似度、所以这场天生过不了"不成立:
本场最高 0.78569,**高于 7/25 成功件《海海海》的 0.57341**。这场的说话锚点质量比成功件更好。

---

## 二、修复

`src/autoslice/host_vocal_proof.py`:

1. **删除** `SESSION_HOST_ANCHOR_SEARCH_HORIZON_MS = 48_000`。`_session_host_anchor_positions`
   从 `post_song_talk_start_ms` 一路扫到 source 末尾,窗长/步进/最短窗/全部阈值不变。
   每个返回的窗仍要独立清过 0.50 才算锚点。
2. **显式排除歌曲区间**:与 `[first_lyric_start_ms, last_lyric_end_ms]`(hash-bound,取自
   alignment 报告本身,不另造边界来源)重叠的窗一律不进候选。
   用她的演唱去验证她的演唱等于把桥接掏空,并且会给"播放原唱/BGM 假冒"开口子——那正是这道门存在的理由。
   > 诚实标注:这一条在当前实现里是**纵深防御,不是行为变更**。`_session_host_anchor_position`
   > 已硬性要求 `post_song_talk_start_ms >= last_lyric_end_ms`,所以候选本来就全在歌后;
   > 加显式过滤只是把这个不变量变成**可测试、有守卫**的,而不是靠上游附带保证。
3. `policy` 块里 `session_host_anchor_search_horizon_ms: 48000` 换成
   `session_host_anchor_search_scope: "post_song_speech_to_source_end_excluding_lyric_span"`。
   `_legacy_reference_profile_policy()` 同步 pop 新键,**`assets/lidousha/voiceprint_profile.v1.json`
   的 policy 哈希面不受影响**(它本来就 pop 掉 search_* 两键)。

### 为什么不把 pre_song 也纳入搜索

主会话倾向"只排除本歌区间、pre/post 都允许"。我选择**维持只搜歌后**,理由三条:

1. **前缀兼容。** 保持起点在 `post_song_talk_start_ms`,新候选序列是旧序列的**严格超集且前缀相同**。
   已通过的 proof(7/25《海海海》选中的是第 4 号候选 442800–450800 / 0.57341)重新出证时
   会选到**完全同一个锚点**。若把 pre_song 窗插到前面,"第一个通过的窗"可能变成歌前窗,
   等于悄悄改掉一件**已发布**成品的证据面。
2. **策略串是 hash-bound 且逐字校验的。** `_validate_session_anchor_search` 硬比对
   `"first_verified_enrollment_window_after_song"`。允许歌前锚点就得改这个语义串,不再是最小修复。
3. **本例不需要。** 高分区(488s→602s)全在歌后;pre_song 实测最高 0.27512。扩到歌前对修好这条毫无贡献,
   只增加面。

如果以后确有"只有歌前有说话"的素材,再单独提,并同步改策略串。

---

## 三、另一个缺陷(**未修**,单列):`post_song_talk_start_ms` 在联唱场景会指错

**这次的修复是绕过它,不是修正它。**

- **它从哪来:** AGY / Gemini 的音频观察。prompt 定义在 `src/autoslice/agy_lrc_alignment.py:252`——
  "`post_song_talk_start_ms` is the first surrounding speech after the song"。
  本例 AGY 子进程 rc=-9(`AGY_FAILED_RC`)后回退到 `gemini_api / gemini-3.6-flash`,
  该模型断言 `post_song_transition_kind: HOST_TALK`、`post_song_transition_ms: 258000`。
- **为什么错:** 258000ms 处没有任何说话,是器乐间隙接下一首歌。在**联唱 / 3D live / 连续歌单**里,
  模型把"歌与歌之间的间隙"当成了"歌后说话"。真值约 486000ms。
- **自洽但同错:** `song_performance.py:292` 强制 `post_song_transition_ms == post_song_talk_start_ms`,
  两个字段一起错,交叉校验查不出来。
- **影响面:** 任何连续歌单场次都可能同病。除了锚点搜索,它还喂给
  `recut_materialization.py:1127`(`post_song_anchor_start_ms`)和
  `live_source_review._tighten_song_boundary_to_verified_host_anchor`(用锚点起点当 `clip_end_ms`),
  所以一个错值会同时污染边界判断。
- **建议:** 归 Ivan 裁,不在本次动。它是另一处判据(模型观察面),改法多半在 prompt
  或"用 ASR 实际语音起止交叉验证 HOST_TALK 断言",与本次搜索面的修复正交。

---

## 四、旧 proof 的可复验性(必须交代)

| 类别 | 变更后是否仍可复验 | 说明 |
|---|---|---|
| 全部现存 proof(21 份) | **否** | `policy` 块是 hash-bound 证据面,`session_host_anchor_search_horizon_ms` → `session_host_anchor_search_scope` 后 `_canonical_policy()` 不再相等,报 "host vocal proof policy does not match the compiled fail-closed policy"。 |
| 其中 READY 件(7/25《海海海》) | 重新出证后**结论与锚点都不变** | 它选中的是第 4 号候选,落在不变的前缀里;重新出证会选到同一个 442800–450800 / 0.57341。 |
| 其中 BLOCKED 件 | **应当**失效 | 只搜了 13 个窗的"未通过"搜索记录不再构成穷尽证明。`_validate_session_anchor_search` 在选中窗未通过时要求 `len(candidates) == len(positions)`,旧的 13 条会被拒。这是 fail-closed 的正确方向。 |
| `assets/lidousha/voiceprint_profile.v1.json` | **是,不受影响** | 走 `_legacy_reference_profile_policy()`,该函数本来就 pop 掉 search_* 键。已实测:改动后 `_validate_profile()` 仍通过。该资产在本工作树与 free 部署树逐字节相同,sha256 `663eee9820d7b0df9021c148ebe21c39f10d782f73cba22bfeeee0ff1a85ad1d`,与今天这份 proof 里绑定的 `reference_profile.sha256` 一致。 |

结论:policy 面变了就该重新出证——这正是把 policy 绑进 proof 的设计目的(弱策略下出的证不能在强策略下复用)。
本次是**把搜索变穷尽**,属于收紧,不是放宽。

---

## 五、测试

新增 4 条(`tests/test_host_vocal_proof.py`),修复前红/绿实测:

| 测试 | 修复前 | 修复后 |
|---|---|---|
| `test_session_host_anchor_search_reaches_speech_beyond_a_fixed_post_song_horizon` | **红** | 绿 |
| `test_anchor_search_truncated_near_the_song_no_longer_proves_exhaustion` | **红** | 绿 |
| `test_exhausted_search_without_a_qualifying_anchor_still_refuses`(反向门) | **红** | 绿 |
| `test_anchor_may_never_be_drawn_from_the_song_it_is_proving`(循环论证守卫) | 绿 | 绿 |

最后一条**修复前后都绿**,如实说明:因为按上面 §二.2 的分析,循环论证的口子在本设计里从未打开
(候选起点被硬性约束在 `last_lyric_end_ms` 之后)。它验的是伪造 proof 把锚点填进歌唱区间会被拒
(`session host anchor timing mismatch`),以及 `_session_host_anchor_positions` 的返回值永不与歌曲区间重叠——
是守卫,不是回归。

反向门 `test_exhausted_search_without_a_qualifying_anchor_still_refuses` 覆盖的正是
"搜索变宽不等于变松":50 个窗全扫、全部低于 0.50,即使每个检查点的 bridge 分都到 0.40(远超 0.22),
桥接仍然关闭,7/7 全不通过,结论 `BLOCKED / NO_LIDOUSHA_VOCAL_DETECTED`。

**全量:`python3 -m pytest tests/ -q` → 3773 passed, 2 warnings in 151.69s**(修复前 3769,新增 4)。

---

## 六、修复后这条素材会怎样(free 上只读预测,**未重新出证**)

生产网格(从 258000 起步进 4000)共 86 个候选窗,第一个通过的是 **index 58 = 490000–498000ms**,
`enroll_median_score = 0.55836`(pairwise 逐 ref:0.55836 / 0.56857 / 0.52192)。
SRT 对照 490–498s = 「欢」「欢迎回来」「大家看的开心吗」,确是说话。

用这个锚点对**原 proof 里 sha256 已绑定的那 7 个检查点 WAV**(逐一校验过未被改动)重算:

| # | bucket | 歌词起点 | enroll median | 旧 anchor 分 | **新 anchor 分** | 旧 | 新 |
|---|---|---|---|---|---|---|---|
| 0 | head | 125300 | 0.32956 | 0.66176 | 0.35307 | 过 | 过 |
| 1 | head | 137200 | 0.25286 | 0.57629 | 0.34756 | 拒 | 过 |
| 2 | middle | 154500 | 0.25079 | 0.45022 | 0.28263 | 拒 | 过 |
| 3 | middle | 166500 | 0.00254 | 0.15390 | 0.27650 | 拒 | 过 |
| 4 | middle | 181300 | 0.08495 | 0.33385 | 0.35219 | 拒 | 过 |
| 5 | tail | 216800 | 0.02113 | 0.29003 | 0.33026 | 拒 | 过 |
| 6 | tail | 231500 | 0.08774 | 0.38427 | 0.35978 | 拒 | 过 |

预测:**7/7 通过,head/middle/tail 全覆盖 → READY / `LIDOUSHA_VOCAL_PRESENT_ON_LYRIC_CHECKPOINTS`**。
最低 bridge 分 0.27650,比 0.22 门高 26%。

注意一个反直觉但正确的现象:**旧锚点(278000,是她在唱下一首歌)的 bridge 分反而更高**
(0.66 / 0.58 / 0.45),因为唱对唱比说对唱容易。它只是清不过 0.50 的 enrollment 门。
这恰好说明门的结构是对的:锚点必须先对 enrollment 证明身份,才有资格去桥接。

**这只是预测。** 真正的 proof 必须由 free 的 runner 重新出证(需要部署),本次没有部署、也没有写
free 的 state/out/repo。

---

## 七、剩余风险

1. **生产出证会变慢。** 实测 free 上 3 次 pairwise 调用 = 3.94s(每次 ~1.31s,因为每次都要重新
   embed ~60s 的 enrollment wav)。本例扫到第 59 个窗才通过 → 锚点搜索约 **3.9 分钟**;
   最坏情况(86 窗全不通过)约 **5.7 分钟**。prover 子进程超时是 900s
   (`scripts/run_full_session_selector_cpa_shadow.py:131`),本例安全。
   但成本随歌后时长线性增长:约 `(歌后毫秒/4000) × 3.94s`,**歌后尾巴超过约 15 分钟就会撞到 900s**。
   若要治本,应让 `generate_host_vocal_proof` 也改用 `speaker_finalizer` 的 embed-once
   (实测与 pairwise 差 ≤4.4e-6),那是独立的一次改动,本次未做。
2. **边界收紧可能少发生。** `_tighten_song_boundary_to_verified_host_anchor` 用锚点起点当
   `clip_end_ms`,且只在 `anchor_start_ms < clip_end_ms` 时生效。锚点变晚后该条件更容易不成立,
   收紧就不发生(只会"少收紧",不会"错误延长")。本例 clip_end=242040 < 490000,两种情况都不收紧,
   行为不变。但对那些原先靠 48s 内锚点收紧过边界的候选,重新出证后尾巴可能变长,需要过一遍。
3. **`post_song_talk_start_ms` 仍然可能指错**(§三)。本次修复让它指错时不再致命,
   但它仍在喂 recut 与边界收紧。
4. **21 份现存 proof 需要重新出证**(§四)。已发布件不受影响(出版登记门管上传),
   但待发件在部署后第一次 tick 会重跑 prover,注意成本与耗时。
5. **本例仍未真正出证。** §六是预测,不是交付。
