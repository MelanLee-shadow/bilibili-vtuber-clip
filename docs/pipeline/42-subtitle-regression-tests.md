# 用户字幕纠错回归集

新的用户字幕纠错进入 `tests/fixtures/subtitle_regressions/cases.json`；**放行逻辑改动必须通过回归集**。
保存原话及来源、候选/包路径与原稿 SHA、错误时间窗和人工正确文字或判定。
成片时间减去包内 record/provenance 的 `intro_offset_ms` 才是正片时间，不能用两种时长相减。
没有精确错句/说话人真值的报告保留 `pending_localization`，不借模型听写补造 公开规则的原话。
媒体不入库；脚本在 OCI3 私有临时目录只读原包、原输入和媒体，输出另存，绝不发布。

```bash
python scripts/subtitle_regression_eval.py \
  --commit <修复提交SHA> --output reports/subtitle-regressions/2026-10-15/after-fix
# --deployed 改用现场部署代码；--case <id> 可定点复跑（不能替代全量门）。
```

本机 Python 驱动；OCI3 使用原生产 `venv-main/bin/python` 在指定 commit 的独立源码快照上执行
`build_prepared_exact_final_reviewer` → `_run_exact_final_review_gate`，包括普通发现、typed 修复、自愈重审及原 release validator。
起点是错误包的原交付 SRT、authority/context 和无片头媒体；不重做 ASR、渲染或上传。
答案只进入评分器，不注入终审 prompt，不另造严重度/放行算法；同一候选的错误共用一次完整审查。
当前稿的终审、发现、最终文字和 provider 原生用量回执保存在输出目录；金额未提供时记 unknown，不伪记零。
每次从原错误包开始的试验使用输出目录下独立的缓存与重试历史，不继承或清空生产目录的
旧未决计数。CPA/native runtime 仍绑定生产配置，Gemini 付费阶梯账本仍指向原生产账本；
这不是生产续跑或预算重置，`replay-state.json` 记录实际绑定。
远端 worker 在审查前复用生产 `load_gemini_credentials()`，只把 `/opt/bilive/.env` 中四个 Gemini 变量载入进程环境；缺失时停止，不能把缺凭据的运行当生产基线。
`gemini-environment.json` 记录 `configured_key_count`（去重免费 key 数）、付费备用是否配置及变量名；`gemini-http-attempts.json` 记录实际 HTTP 的 key 层级/序号和状态，均不保存密钥值。AGY 原生执行回执另计，同候选多个样例共享一次运行账目。

通过条件逐条为 `PASS_REPAIRED`：人工答案、任务明确接纳的参考文字，或注明理由与评估者的等价文字命中。
`PASS_BLOCKED_CRITICAL` 保留历史计数名，仅表示“已拦未修”，不计入总通过；不能把阻断当修复完成。
标点空白可忽略，代词、术语、数字与原语言不得模糊匹配；其他改写标待审。F 听写仍不是人工逐字真值；
T 任务已明确接纳的目标单列 `accepted_reference_text` 及依据，不改写原参考来源。
`--rescore <已有结果目录>` 可零调用重算；补充明确等价字符串时加 `--allow-equivalence-update`，保存旧标签及理由，不能改原输入、错误类别或 公开规则的原始正确文字。
遗漏、非关键放行、provider/输入异常、无关阻断、待补证均不产生全量 PASS；非零退出码及 `evaluation.json` 供整合验收。
字幕门通过不等于包审计/发布通过，其他生产验收继续适用。

离线：`python -m pytest -q tests/test_subtitle_regression_eval.py`。全量按项目要求 `python -m pytest --assert=plain -n 8 -rfE tests`，
失败 ID 与派单基线比较，只报告新增失败。首次指定基线为 `044453f15d494f2552fc6ac2990fe974faed0a56`；其已含句意等价规则，实际结果不以“多数应放行”为预设。


## 新的普通 OFF 回归

`--normal-off` 从所选原错误包开启新的普通回归，不读取旧 Argon plan/grant 或终态实验。
同一 `run_package` / prepared reviewer / final gate 先通过现有 independent seam 对目标做真实
盲听探针；普通消费者只复用通过同源、同目标、同宽窗与原生回执验证的观察，不同窗仍须
按实际路线取证。目标只由原媒体 SHA、原交付时间窗和当前 cue 构造，评分答案不进入
探针或 spec。S2 与 Argon 均显式关闭，Gemini 免费/付费阶梯、MAI/MOSS
和 CPA 继续读取既有生产配置与原付费账本；配置就绪不是调用成功。

使用 `--commit <exact-sha> --normal-off --case <id> --output <new-directory>`，或在已冻结
同一源码的隔离 OCI3 目录用普通 `--worker` 入口。每例保存 `ordinary_route_validity`；
空路线、没有匹配源/目标的观察不得计为有效评测。原失败/UNKNOWN、原语言、窄/宽窗与
各 provider 原生回执仍按现有合同保存，失败不算路线已用尽。普通换版续跑仍保留原账和
原 typed 稿，并重验 OFF/普通模式及目标集合摘要，不能换模式或改目标来清零。
目标修好与整片 `would_release`、剩余 findings 分列；本入口无渲染、发布、部署或
Argon 费用授权。

普通 OFF 在准备任何 provider 前，将原媒体逐字节复制为包内的独立文件并核对原 SHA；
保留原路径/哈希与实际输入路径，绝不使用共享 inode 的 hardlink。原生 observer 的
严格 stat/源漂移检查保持开启；共享原件的元数据变化不污染这一隔离输入，本地输入
篡改仍拒绝。已存在但 SHA 不符或为 symlink 的输入不覆盖、不改签。

正常换版续跑除既有 runtime 外布局外，也可进入真正的
`runtime_root/private-runs/<task>/<run>` 叶目录；runtime、current/state/out/versions、
父目录、额外子层与 symlink 别名均不由此例外放行。旧 evaluation/每包 run 必须真实终态，
同 fixture/目标、当前稿与源哈希、代码换版、原隔离状态和累计账本校验照旧；
归档旧证据后续接，不复制在途实验来制造终态或清零记录。普通 OFF 换版时所有选定包
仍经过新代码入口；旧版已修好的目标也要重新验证，不能以旧目标 PASS 冒充新代码实测。

## Argon 增量比较准备与零授权门（2026-10-16）

原 `scripts/subtitle_regression_eval.py` 增加 `--prepare-argon-comparison <原results目录>`：
只冻结选定case原source/SRT/context/authority、实际目标/上下文及完整WAV，读取已完成且
前后SHA稳定的baseline包；其它包记WAITING，不重启或读取在途输出。评分原话/参考来源单独
保存在 `scoring-only.json`，不加入producer spec或盲听提示，原评分器和四例真值不变。
原请求预览是几何预算资料，不是实际finding已经触发或发送的证明。

`--comparison-plan` 与 `--comparison-grant` 走同一 `run_package` / prepared reviewer /
normal final gate；新费用授权必须先于loader和任何provider。缺省授权0，不能从叫free的key
变量名、生产无cap或旧研究余额推导免费。Google官方Argon Batch无免费层，2026-11-30前
输入$1/M、输出含思考$5/M；本轮不进行认证探测或真实生成。报价与预算由
`argon_regression_comparison.py` 的有界授权合同保存，URL及当日依据进入任务报告。

获准后每个冻结target至多一个Batch POST；第一次smoke只开放指定case，剩余case可在另一
隔离输出目录执行，但共享原plan锚定的reservation账，不能换目录重置。UNKNOWN仍占预留，
实时/paid-backup/自动续写/工具调用均关闭。每次普通调用只推进一次，pending后用同一输出
`--resume-comparison`；不完整CLI invocation保留UNKNOWN、先核进程与日志，不盲重启。
`--attach-comparison-baseline <plan>`可追加后来完成的原baseline，原plan/输入/评分/授权SHA
保持不变；它不重跑baseline或更改原账。

历史只读比较仅复用已有普通听音cache；不得把空缓存导致未尝试路线解释为模型效果。
有效普通链与Argon增量对照可通过明确范围修订启用下节的正常普通路线。CPA仍使用原订阅
运行入口与provider slot。Argon原生HTTP账单独列出，不能用旧urllib计数0冒充Argon0；费用
估算与实际账单分开。输出按原评分器列修对/未修/改坏，另核新证据确实进入实际CPA裁决；
没有Argon材料却修对的结果不归因于Argon收益。四例不等于四整片。

本节是隔离工程接口，不是生产开启或收费授权。完整普通runtime代码须绑定Root审过的本轮
提交；准备用最小源码包不冒充已完成全套真实运行。既有provider、人工原话及发布阻断不变。


### 整合后的恢复身份与原件搬迁

比较 CLI 的 `invocation.json` 同时绑定 plan、case 范围、grant、代码标签、实际加载的
code_root、provider runtime_root、Python 路径和明确列出的当前比较/消费者文件 SHA。
这份有限文件摘要不是全仓源码证明；完整 OCI3 分发另需核实。变更这些绑定不能沿旧输出
继续，也不能覆盖已有完成/UNKNOWN 回执。普通 `run_package` 冷恢复在归档或写入前重验
自己的同类身份，防止绕过 CLI 时静默改绑。S2 在效果比较中显式 OFF，不改变四例或预算。

合法冻结/搬迁的普通 cache 保留旧 manifest 原字节/原路径；原 `probe_witness_cache`
已验证新目录下 input.mp4/prompt/raw 的完整 SHA 和几何后，Argon 使用该实际已验音频，
不要求把 manifest 的历史路径伪改成新目录。没有有效 probe 或新目录字节/时钟漂移仍拒收。


## 本次私有实验的修复后接续

原始三包执行完成且 `evaluation.json` 已落盘后，修复版可用同一脚本的
`--worker --resume-existing` 继续该私有实验；它不是生产恢复或重新发起原始包回归。
先核原 worker 已退出，使用新提交的私有 snapshot、相同冻结 cases 和原 results 路径。
脚本核验原评测/当前 SRT/原包文件哈希，先逐字节归档本次实验已有文本回执和状态，再进入原普通终审链。
当前字幕和 authority、音频缓存、重试 UNKNOWN 以及预算账仍留在原路径，不通过新目录清零。
生产 paid ledger 只保留原软链，不复制或修改它；历史原评测的生产账本不在本接续输入中。

已经由未改评分器认定 `PASS_REPAIRED` 的目标可以精确复用其实际结果，必须记录原代码身份、run/SRT
哈希和零新增调用，不称作新代码重新听音或整片通过。未修目标继续普通代码，人工答案仍只供评分。
新报告费用/调用同时区分累计与本次增量，不能再次累加已包含的父运行。`resume-status.json` 为
RUNNING 时，旧 `evaluation.json` 仅是父结果，不得视为本次终态；中断或未知必须先核实，不能盲重派。
同结果目录的 worker 通过进程锁避免并发写入；评分、人工原文、等价表和发布门不因此改变。

私有 worker 显式启用 `AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES=1`，在正常 LLM transport 实际派发时，
将原 prompt、completion、模型身份及各自 SHA 写入该候选 `cpa-text-exchanges/`，不保存环境、API key
或可能含凭据的异常消息。STARTED 原件不改写，失败终态为 OUTCOME_UNKNOWN，缺终态也不等于完成；
这只是原文观察，不创造重试、改字或放行权。普通非评测入口默认关闭该原文归档，原调用/返回与准入不变。
旧调用缺失的原文只能如实报告，不能由模板补造或为补日志重复派发。

### 两类接续不得混用

`--worker --resume-existing` 是已完成普通私有评测的显式换版续接：沿原cases/current SRT/
原始回执与预算账，记录父/新代码身份。`--resume-comparison` 是同一Argon比较任务的冷恢复：
plan/case/grant/实际源码根/runtime/Python/源码摘要必须仍相同。两类输出目录与CLI模式互斥，
不能借普通换版入口绕过Argon身份，也不能给没有原invocation的普通目录补签比较身份。
已有完成、STARTED/UNKNOWN、原模型回包与费用预留不覆盖、不退款重派。

私有Argon存储逐级以0700创建缺失父目录，不依赖调用者umask；既有目录不会chmod修复，
原不安全终点和任何父/终点symlink继续拒收。完整代码的私有运行验证与生成/计费授权分开。


### 真实运行暴露的代码缺陷：提交前原目录显式续接

当普通comparison已完整返回、该范围尚无任何Argon task handle或费用reservation时，允许
在修复代码并验证后，用 `--resume-comparison --comparison-code-successor <精确前版本>`
在原输出继续。新代码根及源码摘要通过append-only `code-successions/`链记录；原invocation/
每包identity/原结果历史/当前字幕/UNKNOWN/费用账不覆盖，不等于新建实验或新的付费机会。
plan/case/grant、provider runtime、Python与scope必须相同，前版本实际源码及当前SRT重验。
已有原生task、未知提交/未完成invocation、reservation或身份漂移均拒收此换版；普通同版本
`--resume-comparison`仍按当前有效身份重放。代码版本变化本身不是SemanticAttemptRecorder新材料。

comparison的只读普通缓存同时传递明确冻结目录的MAI/MOSS native-context路径；仍逐原请求/
物理来源/时钟/实际native合同验证，不扫描其它实验或改写原回执。补齐已存在的独立材料会
进入正常CPA；其实际后续判决也进入同疑点普通听证历史，不能漏记该分支，也不能把别家回执
改标签为3.8或把PROPOSED需要补听擅自改成NEITHER。重复原判决去重，未产生新材料不能强行续发。

### 正常普通路线的同代码 OFF/ON 比较

`--comparison-scope-revision` 指向独立授权修订，绑定原plan/grant字节而不改写原件。
该修订显式允许普通AGY/Gemini/MAI/MOSS路线与现有三轮付费政策；Argon独立campaign
总账、预留、样本和token范围不变。`--argon-arm off|on`仅切换升级；两臂S2和Argon实时后备关闭。
普通provider配置通过原loader，native配置通过原runtime读取，配置就绪不充当调用证据。

两臂都先通过同一prepared reviewer的既有independent seam对目标作实际盲听探针，
`independent_asr.routes_attempted`及原观察/失败原件与source/target绑定后才进入CPA包门。
生成的native缓存由该同一普通链消费，不手填route记录、不把新模型输出当原话真值。
空路线当场记`INVALID_EVALUATION_CONFIGURATION`并停止该臂；实际失败/UNKNOWN与未调用分开报告。

ON使用`--paired-off-output`精确验证同代码/解释器/源/目标/范围修订的已完成OFF结果，
在任何ON调用前冻结其结果hash；不以旧代码baseline冒充Argon单独因果对照。
两臂输出可区分、执行身份不可混用；原共享Argon预留路径不随输出变化而重置。
