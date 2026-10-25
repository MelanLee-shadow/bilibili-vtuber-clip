# 中断的 normal OFF 字幕回归恢复

## 输入状态与质量状态

`subtitle_regression_eval.py --resume-existing` 继续只接受完整、合法终态的 evaluation。进程已经退出、工作目录仍有当前字幕，不能代替 evaluation，也不能把 FAILED 改为 COMPLETED 来通过该守卫。

`--worker --normal-off --resume-interrupted PARENT` 是同一普通 worker 的显式中断输入入口。它在加载凭据前验证被冻结的原件，随后从新建且仅创建一次的 sibling 输出身份继续。它仍调用普通 prepared reviewer、普通独立音频路线及 exact-final gate；不跳过任何选定包，也不继承旧代码的质量 PASS。一个物理包可包含多个选定 case。

输入准入的结果是 `ORDINARY_EXACT_FINAL_REVIEW_REQUIRED`。终态文件里的 COMPLETED 只表示这一尝试已返回并写出实际 evaluation；字幕可能仍被 BLOCK，进程返回码也可能为 1。评分快照、整片疑点、would_release 和上传许可必须分别判断。

## 可接受的真实证据

冻结清单 schema 为 `subtitle-interruption-evidence-freeze.v1`，必须先在原 owner 和 worker 均退出、目录字节稳定后创建。它记录原输出物理路径、全部文件的 bytes/SHA256、唯一共享付费账本链接、实际执行代码目录的完整文件清单、全部选定原始输入及真实启动/失败终态、日志、fixture/归档的物理路径和 SHA256。传入清单的精确 SHA256；清单不能补造缺失的 evaluation、run、provider 回包或评分。

入口再次核对：

- 真实原终态是 FAILED/INTERRUPTED，或是绑定了实际 evaluation 的 COMPLETED/returncode=1 且所有选定 case 都为 EVALUATION_ERROR。后者须逐项核 case、candidate、具体 error、summary 和实际 package_runs；没有包 run 是真实准备失败状态，不补造成功包。原 launcher/worker 均不存活，无同 output 的其他进程；原 worker 锁也必须可独占。
- 原命令的 normal OFF、四维身份（源/fixture/代码/输出）、选定 case、开始/结束时钟和至多 14400 秒限制一致。其余完整 evaluation 仍走原完整恢复合同；存在一个 JSON 文件不等于完整包输入。
- 全部原目录文件、原始输入、原源码/资源/现存字节码与冻结清单逐项相符，且验证前后原目录一致。路径不经符号链接，唯一例外是已存在且指向同一生产付费账本的链接。
- 当前字幕的实际 bytes、authority、自愈链和 pending 状态由已有原生 typed 校验器验证；原始记录和 context 由现有 source-clock/provenance 校验器验证。保留全部目标外疑点、span obligations 和原 carryover checkpoint。
- 已返回包的实际 run 绑定必须吻合；中断包没有 run 时，实际 review-input 或已持久化的原子阶段必须绑定当前文本。只有请求/回包齐全且原生身份校验成立的成功结果可被原缓存冷复用。

native 使用原 budget receipt/history 校验器；CPA 逐项核对 request/prompt/terminal/completion 的原始 hash；semantic 使用原 SemanticAttemptRecorder reader。DISPATCHED、STARTED、ERROR、OUTCOME_UNKNOWN 等未决历史保留，不退款，不写成成功。native TEXT_UNLOCATED 不变成带时间戳的观察。cost_usd 未知继续为 null，重叠口径不能相加。

## 普通公开入口

先运行无 provider 预检，OUTPUT 是一个尚不存在的私有 sibling 目录：

```sh
python -B scripts/subtitle_regression_eval.py \
  --worker --normal-off \
  --resume-interrupted PARENT \
  --interruption-evidence FREEZE_JSON \
  --interruption-evidence-sha256 FREEZE_SHA256 \
  --cases ORIGINAL_CASES_JSON \
  --case CASE_ID \
  --output NEW_PREFLIGHT_OUTPUT \
  --code-root EXACT_CODE_ROOT --code-identity EXACT_CODE_COMMIT \
  --runtime-root EXISTING_RUNTIME_ROOT \
  --recovery-preflight
```

每个 case 各传一次 `--case`。实际续跑使用同一入口和相同父链、fixture、清单、最终代码，改用一个全新的输出身份，移除 `--recovery-preflight`，并明确传入 `--maximum-wall-seconds 14400` 或更小值。

预检通过同一个 `stage_successor` 的无 claim 隔离分支复制原输入，再逐包调用真实 `run_package` 的 materialization→`build_prepared_exact_final_reviewer`，在调用 reviewer/observer 之前结束。网络和子进程派发由预检作用域的审计守卫拒绝，不加载凭据。包 run、来源/clock 回执与阶段正常落盘，quality_outcome 为 NOT_EVALUATED、would_release 为 null，不能当作目标或整片通过。预检不消费父尝试的续接 claim。成功的同身份预检只有显式增加 `--recovery-preflight-replay` 才能冷重放；检查既存 preflight、父链、输入与代码身份，保留首轮文件并另存 replay 回执。缺包、残缺副本或不同身份不能被静默接管。实际续接为父物理输出+真实失败终态 SHA 建立不可重置的 O_EXCL claim；换输出 ID 不能再从同一父尝试启动第二个 child，两个并行 starter 只有一个可以创建成功。准备失败也不自动删 claim/重试。新失败尝试需要新的真实冻结和显式授权，不能自动循环。

所有原文件留在父目录，并完整逐字节归档到新输出的 `predecessor/FREEZE_SHA/`。运行所需的 raw/cache/native/semantic 和当前输入另外物化为活动副本，后续活动状态变化不会抹掉父输入。旧 run/evaluation/attempt/status/包级结果与旧 `normal-off-source.*` 只归档。`copied_to` 指向完整不可变归档，`active_copied_to` 说明实际活动副本；原回执内部 path 不改写。准入递归重验真实父 freeze、terminal、已消耗 claim 和继承映射，不能改从旧父换 output 再开。付费账本链接继续指向同一账本。

普通消费者媒体由 `media_copy_provenance.materialize_media_copy` 创建独立 inode 的字节副本，使用明确的 `media-independent-copy-provenance.v1` 同 stem 侧车。它保存原 prepared 来源/clock、record/context/candidate 绑定、原 reader 实际读取的源证链文件 hash/stat，以及真实新路径和副本 hash/stat；冷读仍由原 `_range_media_source_binding` 重放。复制不会伪装为新 recut，旧 sidecar、原媒体和音频时钟不改动。既存无证副本不补签；缺证、错路径/字节/stat/candidate/endpoint 继续拒绝。native 源定位迁移继续消费已有严格的 relocation/原回执归档机制，不重置原预算。

## 持久化与再次中断

普通 `resume=True`（含同输出 comparison resume）在首次覆盖 `run.json` 前，稳定读取原始字节并由既有 immutable writer 保存至 `run-history/<原 SHA256>.json`。首阶段记录 `resume_parent_run` 的原路径、归档路径、SHA、大小和 PENDING 状态；通过原 mode/candidate/input/current text/authority 校验后才标为 VALIDATED，并将 `parent_run_sha256` 绑定到这些旧原始字节。constructor 失败或中断仍保留已验证父身份，校验失败仅保留拒绝的原输入证据。归档写入失败直接停止，不能由失败 checkpoint 覆盖唯一旧 run；同字节归档可在首 checkpoint 前中断后严格重用，冲突或链接拒绝。comparison 既有根目录三份 history 原件继续保留。不得通过摘要本轮 PREPARING 文件来替代真实父身份，也不倒改已结束旧尝试。

worker 在准备开始时先持久化包身份和非成功 run，在物化前后、prepared constructor 前后、每次审查开始/返回、普通路线完成和包返回/中断时保存阶段，以父阶段 SHA 连成链。v2 阶段显式记录 text/authority 是否已取得；尚无输入时不伪造空字幕或成功 authority blob，旧 v1 阶段仍严格逐项验证。准备异常记录具体 error、failed_phase、PACKAGE_PREPARATION_FAILED/INTERRUPTED；取得 prepared reviewer 也不表示质量通过。现有原子文件写入和 immutable store 负责落盘；阶段及 blob 先落盘，再推进当前指针。指针写入失败不能抹去已保存阶段，读取时逐项验证阶段链和 blob hash，不能据此制造成功结论。

准备输入的 authority 若不可读取、不是合法 JSON 或类型错误，检查点保留实际取得的原始字节及具体错误，不能在错误处理里再次把它当作成功 authority 解析。主失败发生后若推进当前指针再次失败，已保存的包身份、原始 error 和不可变阶段仍保留；终结 progress 同时披露二次写入异常，不能将任何一层错误补签为成功。

已有 producer-current/source text checkpoints 是生产文本阶段的原生合同，本回归入口消费已有 typed 自愈和 carryover 原件，不伪造那些生产 checkpoint 所需的源阶段成功。回归阶段记录补充的是实验身份、评审序号和包进度；业务字幕判断仍由同一原生普通路线完成。

每个已返回包立即写入 `package-results/NAME.json` 和 `experiment-progress.json`，无需等最后一个包结束。worker 终结时再原子写入真实 lifecycle/returncode、各包 error/phase、quality_outcome 和 would_release，避免把进行中快照当作仍在运行；旧父 progress 原字节保留。进行中的审查输入使用新的序号，不覆盖没有回包的旧 review-input。SIGTERM/墙钟上限通过异常退栈保存实际 `run.json`、PACKAGE_INTERRUPTED 和新 `attempt-terminal.json`；再次中断时保留所有父链、旧包结果和未决账。硬杀或介质故障若使实际 current/typed binding 不完整，恢复必须精确拒绝；已有原子阶段和 raw 请求仍保存供核查，不能靠补评分或猜成功解锁。

长跑应由带真实 PID、cwd、命令、日志、时间上限和不可变外层终态的持久 launcher 承载。Mac transport 返回/超时均不是远端业务终态。持续工作时对外报告 RUNNING 和实际终态路径，由行政既有 probe 核验，不创建新的业务 agent 或 watcher。

## 本合同的回归边界

新测试覆盖无完整 evaluation 的合法中断输入、双启动/在途拒绝、源/时钟/当前稿/authority/fixture/代码/原请求/回包漂移、缺回包不造成功、UNKNOWN/native DISPATCHED 不退款、已有成功 semantic 冷回放不重买、普通 gate 的实际中断退栈、失效指针下的不可变阶段，以及再次中断的父链和旧包结果保存。合成 fixture 只测试合同，不构成任何真实字幕质量证据。

原评分与输入函数（load_cases/body_ms/validate_original_cases/summarize_case/aggregate/rescore）保持原 AST；参考答案只进入既有 scorer。真实 case 是否改对、误改以及整片是否可发，只能读取最终代码实际产生的包级结果/evaluation 判断。
