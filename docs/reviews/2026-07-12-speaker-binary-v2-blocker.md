# 2026-07-12 人声二分离第二阶段：当前阻塞与继续路径

> **历史诊断快照。** “当前阻塞”、测试数、模型阈值与候选状态只描述 2026-07-12；
> 现行 talk speaker gate 见
> [../pipeline/40-subtitle-text.md](../pipeline/40-subtitle-text.md)。

## 裁决

目标尚未完成，当前候选不得部署。

- 已经打通二分类结果写入字幕样式、整场路由、claim/hash 绑定和 `FAST_SOLO` 成片的主要代码骨架；集成 worktree 的完整测试曾达到 `798 passed`，相关测试 `149 passed`。
- 但声学核心在现有开发真值上的清晰二分类只有 `129/146 = 88.36%`；仍有 4 条超过 1 秒、分数较高的嘉宾语音被错认成李豆沙。错误会直接烧成 sapphire，不能用“测试全绿”替代产品验收。
- 当前 production `805a704...` 继承 `34b9b265...` 的 speaker fail-closed：证据不足会产出 hash-bound `SPEAKER_REVIEW_REQUIRED` 并阻止烧录；但它仍让 whole-clip 文本/LLM 参与身份裁决，而且需要人工 override，属于安全的临时 fail-closed，不是终极无人值守二分离。
- 2026-07-10 是已确认独播，只用于整场 `FAST_SOLO` 路由验收，不用于寻找“疑似嘉宾” cue。

## 已完成的可复用工作

### 真值与评分

- 第一阶段盲听 120 条已全部标注：李豆沙 40、非李豆沙 59、mixed/overlap 20、unjudgeable 1。
- 已纳入此前遗漏的人工修订：`15-v6.review-edit.srt` 与 `R1-v4.review-edit.srt`；后者额外提供 47 条清晰单人真值。
- 当前 production 在 99 条清晰 cue 上为 `75.76%`；CAM++ 直接声学子集 `46/50 = 92%`，text-only context 子集 `29/49 = 59.18%`。因此文本/LLM 不应拥有说话人身份决定权。
- direct-v2 开发候选：Phase1 `91/99`，R1 `38/47`，合计 `129/146`；混淆矩阵 `TP=59, FN=8, FP=9, TN=70`。

### 已证伪的声学补丁

以下路径都没有同时做到“修掉至少 3/4 条高危嘉宾错色，并且不引入同量级的新清晰错误”，不应继续围绕它们调阈值：

1. cue 内多窗 CAM++、锚点/complete-link 聚类；
2. 监督式 CAM++ backend；
3. ERes2NetV2、WeSpeaker ResNet34-LM 及监督式 WeSpeaker；
4. 2026-07-10 独播正样本 bank 的 centroid/median/top-k 聚合；
5. CAM++/ERes/WeSpeaker 三模型 fusion；
6. 中文 ECAPA `iic/speech_ecapa-tdnn_sv_zh-cn_cnceleb_16k` 及四模型 fusion；
7. HMM/Viterbi 时间平滑。

这些传统 speaker-verification 模型的错误高度相关；4 条高危嘉宾在多个模型中都排在最像李豆沙的一端。继续叠同类 embedding 或做时间平滑不是有效下一步。

### 整场独播路由

- 已实现 `speaker-routing.v1` 的正式 claim、媒体/字幕/模型/profile hash 绑定、缓存校验、篡改拒绝、分组 ffmpeg 抽取和 `FAST_SOLO` 全 sapphire renderer。
- 2026-07-09 联动会进入完整二分离；2026-07-10 v3 shadow 没有误判为独播，但被一个 620 ms 已知李豆沙短 cue veto，说明当前 gate 过严且资源收益尚不理想。
- 下一版应让极短 cue 不再单独否决整场独播，同时要求足够多、覆盖首中尾的长 cue 提供强正证据；任何嘉宾/第二声纹/证据不足仍升级完整二分离。

## 第二阶段新 holdout（已完成并首次评分）

目录：`lidousha/2026-07-12/人声二分离第二阶段盲听/`

- `package_id`: `lidousha-speaker-holdout-20260712`
- 54 个唯一 review ID：联动片段 A 30 条、B 24 条；与第一阶段四段、15-v6、R1-v4 不重合。
- manifest SHA-256：`f746c9d5015cc7202f36983c381330a41518c2d88e0aa0bdb03c1b61cbf372b9`
- 页面、两段音频和 ±3 秒上下文均已实际验证；不含模型预测、颜色、历史标签或 2026-07-10 内容。
- Ivan 已完成 54/54：李豆沙 28、非李豆沙 24、mixed/overlap 2、unjudgeable 0；labels SHA-256 `5c1ab7d530778205814096563046637e74846266e97d19558cb75ad2cc2c176f`，review ID 无缺失、额外或重复。
- direct-v2 在揭示真值前先于 `free:/tmp/lidousha-speaker-holdout-eval-20260712/` 冻结 A/B 预测；代码 SHA `a8716b86...1655`、profile SHA `663eee98...ad1d`，两个 speaker manifest SHA 分别为 `f74fadb4...3962`、`45d93e12...61a`。
- 首次评分：52 条清晰 cue 仅 `42/52 = 80.77%`，duration-weighted `86.88%`，macro-F1 `0.8074`；李豆沙 `22/28`，非李豆沙 `20/24`，即仍有 4 条嘉宾被错烧 sapphire。
- 后验可分性诊断的 AUC 为 `0.9256`；对冻结到五位小数的分数，阈值必须高于最大 guest 分数 `0.54590`（例如 `0.54591`）才能把 holdout 嘉宾 false sapphire 降为 0，但李豆沙只剩 `19/28 = 67.86%` recall。它证明分数有排序信号，也证明单阈值无法同时满足两类字幕正确率；该后验阈值不得回用于本 holdout 冒充验证。
- 两条 human mixed/overlap 都被 direct-v2 强制二分类：`B:cue-22`→白色、`B:cue-11`→sapphire，但 B manifest 仍写 `production_ready=true`、`overlap_output_cue_count=0`。这单独就违反生产完成门。
- fresh reviewer 复算全部指标与 hash 一致；审计限制是 labels 已在本机先导出，缺少不可变的“导出前预测 freeze”，因此只能确认模型/代码在预测前已固定、远端预测目录没有 labels，不能把历史 truth 隔离说成密码学证明。

现有生产/录播证据中找不到第二个可确认的联动日期；不能为凑多场样本猜测 7/11 或把 7/10 独播混入 holdout。

## WavLM 009 隔离试验（已 INVALIDATED）

表征机制明显不同的官方候选 `microsoft/wavlm-base-plus-sv` 已完成最小试验：WavLM Transformer 表征加 XVector speaker head，官方模型要求 16 kHz；固定 revision 为 `feb593a6c23c1cc3d9510425c29b0a14d2b07b1e`。模型权重约 404.55 MB，最小三文件下载合计 404,605,907 bytes；许可为 CC BY-SA 3.0。

- 官方 model card：https://huggingface.co/microsoft/wavlm-base-plus-sv
- 官方 license：https://github.com/microsoft/UniSpeech/blob/main/LICENSE
- 隔离包已静态准备在 `free:/tmp/lidousha-wavlm-spike-009-20260712-prepared`：8 files / 35,237 bytes，package-manifest SHA-256 `9207dec6c38789946ab1be91ee5b3ce513cb25d894769e084c2fd9aed762583d`；7 个绑定 hash、15 WAV 的 SHA/PCM16 mono 16 kHz authority、sealed/truth-blind binding 均通过。
- 试验只允许写上述 `/tmp` 目录及其独立 Hugging Face cache；不得写 production、state、out 或上传账本。
- 固定 truth-blind 15 段小面板：4 条高危嘉宾、4 条李豆沙 control、4 条普通嘉宾 control、3 条 enrollment；真值单独密封。
- 先过小面板才允许扩到 149 条：4 条高危嘉宾全部低于 4 条李豆沙 control；用 Phase1 导出的阈值至少修复 3/4 高危嘉宾，且 4 条李豆沙 control 零错。
- 资源上限：15 分钟、6 GiB；禁用 `trust_remote_code`；失败后不继续下载其他大模型。

Ivan 已授权模型下载；三份模型文件总计 404,605,907 bytes，固定权重 SHA 与官方 authority 匹配。初次加载被 Transformers 的 torch >=2.6 安全门阻止，在生成 embedding 前退出；随后只在 `/tmp` 增加官方 PyTorch/torchaudio 2.6 CPU overlay，未修改 production venv。修复后 15×512 embedding 在 18.48s 完成，峰值 RSS 1,156,024 KiB。

预注册门明确失败并停止：

- 4 条高危嘉宾只修掉 2 条，要求至少 3；
- 4 条李豆沙 control 错 2 条，要求 0；
- `max(high-target guest)=0.899747` 高于 `min(Li control)=0.704268`，rank gate 失败；
- `overall_pass=false`、`full_149_allowed=false`、`full_149_started=false`。

fresh reviewer 用真 cosine 独立复算得到同一结论。运行时 overlay、wheel/tree、embedding、日志与 verdict 的 additive authority 已写入 `docs/reviews/2026-07-12-wavlm-spike-009-postrun-authority.json`，并以同 SHA `c1324c7f...ca4` 同步到远端 spike。WavLM direct enrollment 路线到此淘汰，不得违反 stop rule 扩 149。

## grouped one-class 010（已 INVALIDATED）

不再下载模型；仅复用已经冻结的 CAM++、ERes2NetV2、WeSpeaker embedding。每个 encoder 只取相对三份李豆沙 enrollment 与 fold 内 Li centroid 的低维特征，禁止 guest prototype、文本、source ID 或时长特征。

先在旧 146 条清晰开发集做五素材外层 LOGO、内层 LOGO 调参。开发门要求 accuracy ≥90%、guest→sapphire ≤3/79、Li recall ≥85%、每个 source guest→sapphire ≤1；任一失败即 INVALIDATED，不能生成 holdout 缺失 embedding，更不能查看 holdout 结果。只有开发门通过，才允许一次性评分 A/B。

development-only 结果为 `INVALIDATED`：nested outer LOGO `110/146 = 75.34%`，`TP=32, FN=35, FP=1, TN=78`。Guest→sapphire `1/79` 虽通过安全门，但 Li recall 仅 `32/67 = 47.76%`，五个 outer fold 都找不到同时满足 guest FPR≤5% 和 Li recall≥85% 的 inner 参数；没有生成 candidate model，也没有读取 holdout 标签或生成 holdout embedding。产物保存在 `/private/tmp/lidousha-speaker-oneclass-spike-010-20260712` 与同名 `free:/tmp`。

## recovery v7 holdout（安全但覆盖为零）

`codex/binary-v4-recovery-v7 @ ef37e46` 已在 A/B 的真实 recut 与 timing-only SRT 上做 truth-blind 运行。两段 decoded PCM 与盲听包 M4A 对齐，但都在产出 candidate JSON 前因 cluster target 分数歧义 fail-closed：

- A：`cluster-001 ... AMBIGUOUS_TARGET_SCORE`；
- B：`cluster-002 ... AMBIGUOUS_TARGET_SCORE`；
- 最终 `TARGET=0, OTHER=0, REVIEW=54`，清晰 READY coverage `0%`，false READY `0`，两条 mixed 都 REVIEW；
- prediction freeze SHA `bc097eca...a045`，post-unseal score SHA `8ac52ba0...4407`。

这证明 v7 的安全门有效，也证明它不能交付罕见联动：整场拒绝不等于完成二分离。

## relative-cluster 011/012

011 预注册了候选内相对 cluster 映射：每场只用 pyannote session cluster；用三份 Li enrollment 的 median cosine 给 cluster 排序，最高 cluster=Li，只有 top-second gap≥`0.05` 才映射；provider overlap、secondary cluster share>20% 或 dominant coverage<80% 的 cue 进入 REVIEW。它禁止文本、guest prototype、per-cue threshold tuning。

只读 replay 精确 `BLOCKED`：冻结 v7 cache 对 A 的 26 个身份采样 cue 只有 5 个 exact embedding，对 B 的 14 个只有 3 个；whole-session cluster embedding 会混入 cue 外语音，不能替代。011 没有预测、没有评分、没有读 labels。authority v2 SHA `5be3e8ef...684`，provenance audit SHA `9a26b634...92b5`，blocked SHA `27cb136e...95a`。

012 只补这个已钉死的缺口：无 labels 上下文的 worker 对每个 cue∩dominant-cluster 重建 exclusive WAV，用同一冻结 CAM++ runtime 补 embedding，然后先写 prediction freeze。它不得访问 `/Users/ivan/Downloads`、任何 labels/evaluation 文件，也不得评分；冻结后才交给独立 scorer 一次性开封。规则与门不随结果变化。

运行共生成 31 个 exclusive WAV：27 个新 CAM++ embedding、4 个 exact v7 cache hit。A 的 cluster score 为 `0.65837/0.308725/0.21784`，gap `0.349645`，输出 12 READY + 18 REVIEW；B 的 cue 13 与所有 provider cluster 都是零交集，未偷换 full-cue 音频，因此整场 BLOCK、24 REVIEW。prediction freeze SHA `4fc718c269acf237bc8b64228c4425d2d823483bfc9be8b769eb13127e7b8a36`，replay SHA `f83d9453...00b0`，重复运行没有新增 inference 且预测 byte-identical。

独立开封评分精确匹配 54/54 ID/时间：

- clear READY precision `11/12 = 91.67%`（FAIL）；
- clear READY coverage `12/52 = 23.08%`（FAIL）；
- guest false-sapphire `0`（PASS）；
- Li recall `8/28 = 28.57%`（FAIL）；
- mixed/overlap REVIEW `2/2`（PASS）；
- A strict `11/30 = 36.67%`、B strict `2/24 = 8.33%`（均 FAIL）。

evaluation SHA `eed006fccc327238c311ed0b3b5e2ea55a13a8cf2be8935bf6136a4f5f4ca127`。012 为 `FAIL_PRIMARY_GATES`，正式淘汰；不得用 READY-only 91.67% 隐去覆盖失败。

## 与 production 最新基线的集成边界

现有旧 integration worktree 不能直接 rebase/merge：其 `direct_target_binary_v2` 提前返回会让 production 的 singleton/review fail-closed 逻辑不可达。2026-07-12T23:30:59Z 另一工作线又把 production 从 `34b9b265...` 推进到 `42d0a345...`；后者增加 topic entity graph/huozi 等功能，但未加入本任务的 session router。随后 production 又在 23:56:33Z 直接推进到 `805a704...`，只改四个 huozi 路径。任何 speaker 集成都必须覆盖实时 production HEAD，不能把 `5d1310c` 直接部署成对最新生产的覆盖。

最小安全路线：

1. 从 `34b9b265` 新建干净 worktree；
2. 只移植 router schema/verifier 与独立 claim/hash 测试；
3. 移植 additive `FAST_SOLO` renderer；
4. 手工合并 runner/package，保留 production override fingerprint、结构化复核校验、状态分类与 requeue 语义；
5. 删除或重写所有依赖无条件 direct-target early-return 的测试；
6. 只有新声学模型通过开发集与新 holdout，才接入正式二分类；随后全套测试、真实 7/9 联动 + 7/10 独播 shadow、fresh adversarial review，再考虑部署。

### clean 安全基础设施 v5（commit-as-WIP，禁止部署）

已从 `34b9b265` 建立独立分支 `codex/speaker-binary-clean-20260712`，commit `5d1310c46b2fc9fc2a7687cc0db61cfaa72ab7ab`。它实现的是模型无关的安全壳，不是已通过的分类器：

- production allowlist 为空；未审计外部 provider 在执行前被拒绝，`FAST_SOLO` 当前硬禁用；
- provider bundle、实际 argv/脚本、config/model/profile、算法和 current runtime 字节绑定；
- final recut 必须由当前 invocation 从 claim source/绝对区间 fresh 生成；任何 post-install 校验失败先事务恢复原媒体，再进入 binary；
- mixed/overlap 进入 hash-bound terminal review，无 SRT/ASS；review WAV 路径、symlink、字节 hash 双重验证；
- sealed full-session inventory/decision 有独立 generation authority，title retry/requeue/状态回滚不能把 `[solo, collab]` 缩成 `[solo]` 重发 FAST；
- no provider 时不做额外 segment hash/model lane，保留原 binary/singleton/review/requeue 语义。

五轮 fresh review 的最终 V5 冻结指纹为 `76e1bd64e12c8f76c52dd3ecfc799971f70a5fd2524dd892f286f567f91e88c6`；最终 reviewer 独立 real-ffmpeg 验证正常 FAST、post-install tamper rollback、original-absent rollback 与 rollback-failure fail-closed，无材料性 finding。全套 `862 passed`，affected `207 passed`。结论仅为 `COMMIT_AS_WIP`；没有真实 allowlisted provider/model，也未做跨 session 验收，所以 deployment 仍为 NO。

### 最新 production 基线集成与未来证据采集（已提交，禁止部署）

以当时 production `42d0a345c4abb0590f7f02934c8baad48ac67258` 建立独立分支 `codex/speaker-binary-latest-20260712`，无冲突移植 V5 并保留 topic graph/huozi，形成 commit `21ed85e0311872c8e29e278a0e2ae00d4fce2849`。该提交仍保持 `AUDITED_PROVIDER_BUNDLES` 为空、`FAST_SOLO` 不可达；它只证明该基线可以容纳模型无关安全壳，不代表已有可靠分类器。

同分支第二个 commit `b0adfe24d4f957af53979e3863d0fb845e9a86d6` 实现 `collab-evidence-capture.v1`：

- solo/no-trigger 在文件系统、媒体 hash、模型和子进程之前纯返回；绝大多数独播不承担额外媒体工作；
- 只接受已验证 provider 的 opaque `PROVIDER_CAPTURE_TRIGGER`，或至少两类明确文本信号 `{EXPLICIT_COLLAB, LIVE_CONNECTION, GUEST_ROLE}`；文本只触发未标注采集，不参与 speaker 身份或路由；
- 生产 state/report 落盘后才 `Popen` 独立 worker，runner 不等待媒体处理；worker 总墙钟 900 秒，ffprobe/ffmpeg 各自有界；
- worker request 不含 provider verdict、candidate ID、原始 hook/preview 或 speaker 预测；trigger class 有 allowlist、去重排序和 reason-code 一致性门，已有 request 复用也重新验证；
- 每场从 sealed full-session 的真实 SRT cue 最多抽 120 个 PCM16 mono 16 kHz WAV，跨短/中/长与全时间轴，排除 song interval；source/SRT 前后 stat+hash、源时长、WAV 格式/帧数/时长、cue interval、manifest/queue integrity 均 fail closed；
- song interval 必须是候选 source 的非负严格整数区间；字符串/bool/倒置/外部 source 都在 worker boundary 拒绝，不能静默绕过歌曲排除；
- manifest 和 queue 明示 `labels_present=false`、`predictions_present=false`、`training_ready=false`、`upload_authorized=false`、`confirmed_collab_session=false`；只计 2026-07-13 起的候选场，达到 5 场仅生成一次性开发告警，不自动训练或发布。

fresh adversarial review 独立复验任意 trigger class 注入、reason/class 不一致、坏 request 复用、假 WAV、字符串 song interval、合法重叠/非重叠 song interval 等 canary；最终相关回归 `176 passed`，无剩余材料性 finding。冻结文件 SHA：runner `06e7c2b608cbd7174e381cc384a5471ff59901653581bc14a3fa5749292e06c8`、capture module `ca59494aa713562e506331bf63560ac9e14bd38bd2fb5b35c78f8f756f88824b`、tests `0d8414cb7f3dea253b1ebc4a3fa4920be48c0402ca8220eecfc3737ae9aa51ee`。

production 漂移到 `805a704198ca4d7f2843c30fc93938ec84810e5e` 后，又将其四个 huozi 路径无冲突 cherry-pick 为 branch tip `5bbaebb66641f4347faaaa71303d0caa8542792b`；stable patch-id 相同，四个文件逐字节一致。对齐后的全仓为 `933 passed`，`py_compile`、`git diff --check`、`git show --check HEAD` 均通过，worktree clean。

上述任务 commits 均未部署。远端当前是 `805a704...` 且 `DISABLED` 存在；没有上传或 production state/out/ledger 写入。隔离 `/evals/failure-selfheal-6f9da78` 有另一工作线的 runner/speaker 评估进程，但没有 production BASE runner/speaker/uploader。部署 capture 只能开始积累候选证据，不能解锁 `FAST_SOLO`、联动二分离或发布。

## 跨 session 数据缺口

现有 Phase1 四段、15/R1 与第二阶段 A/B 全部只能确认或强映射到同一场 2026-07-09 联动，guest roster 也是礼墨Sumi + 安晚Awa。它们从现在起全部是 development-only；7/10 独播只能作 Li/router control，不能补 guest session。

最低可辩护发布线：共 6 场真实联动，即还需 5 场新联动；按整场分为 3 train、1 dev、2 locked holdout。每场首轮约 120 cue：96 clear（48 Li + 48 guest）、16 mixed/overlap、8 nuisance/unjudgeable；必须跨首中尾与至少四个非重叠窗口。H1/H2 各含训练未见 guest 或未见 mic/codec 条件，且任何单场失败即整体失败。

真正需要 Ivan 人耳的是未来 5 场约 600 条开发/holdout 盲听；其中 locked H1/H2 只能在模型、规则与预测 hash 冻结后开放。当前没有新的音频包需要继续审。

## 完成门

必须同时满足：

1. 清晰单说话人开发集不再出现高分、长时嘉宾 false sapphire；
2. 新 54 条 holdout 达到足以无人值守的精度，mixed/overlap 不被强制二分类烧错色；
3. 7/10 独播稳定 `FAST_SOLO`，7/9 联动稳定 `RUN_BINARY`；
4. 所有 speaker 身份来自声学证据，文本/弹幕/LLM 只能 veto 或触发完整二分离；
5. 从 production 最新干净基线集成且全套回归、真实 shadow、hash/claim/tamper 验收通过；
6. 保持无上传授权即不上传，部署前不得移除 `DISABLED`。

## 当前运行态

- `free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT`：`805a704198ca4d7f2843c30fc93938ec84810e5e`（2026-07-12T23:56:33Z）；受控清单 111 项与该 commit 完整匹配，`src/autoslice/speaker_session_router.py` 与 `src/autoslice/collab_evidence_capture.py` 均不存在，因此这不是本任务部署。
- `free:/opt/bilive/autoslice/DISABLED`：存在。
- 精确进程表只发现 `/opt/bilive/autoslice/evals/failure-selfheal-6f9da78` 隔离评估进程；未发现 production BASE runner/speaker/uploader。
- 本阶段没有部署、上传或写 production state/out/ledger。
