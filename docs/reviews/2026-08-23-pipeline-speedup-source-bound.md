# 流水线提速并行契约（source-bound review，2026-08-23）

本页是历史设计证据、当前分支映射和后续实现入口，不是新的流水线规则。分步规则仍以
`docs/pipeline/20-selection.md`、`40-subtitle-text.md`、`80-package-delivery.md`、
`90-publish.md` 及其代码/资产强制层为准；本页不得被执行者当作放宽任何字幕、源事实、封面、
审计、manifest 或上传 gate 的授权。部署、真实运行态和公开发布必须由 root 现场验收。

## 证据边界

以下两段是 Ivan 在原始 source thread 中明确批准/要求的提速方向；“历史批准”只表示设计意图
有原文证据，不表示当前部署或 runtime 已实现。

| 证据 | 原始位置 | 可引用结论 |
|---|---|---|
| Historical approved design | `/Users/ivan/.codex/sessions/2026/08/20/rollout-2026-08-20T01-03-41-01a01d8d-b49e-7eb1-bed7-1d2f70d013c7.jsonl:21890-21891`，`2026-08-21T17:04:36.455Z`，source thread `01a0253b-68d8-7671-b5be-f508815dc1e1` | 现有生产已有 `MAX_PARALLEL_PRODUCE=5`；瓶颈是串行的“微修→全测→部署→浅 dry-run→apply 首个深 gate→诊断→再部署”循环。要求 full no-target-write after-image preflight、typed predicate matrix、sanitized receipt、readiness graph，以及 prepare/validate 并行、provider semaphore、短 CAS lease、单一 uploader。 |
| Historical approved priority | 同一 JSONL `:29331-29332`，`2026-08-22T02:18:13.265Z`，source thread `01a02737-09ae-7b11-8987-81e29006e148` | 让既有任务自然到达合法 stop；停止下一轮 candidate-specific test/deploy/upload 循环，先批量收敛通用 P0/P1，再一次 full suite + deploy；不跳过 safety gates，之后才恢复 Qixi same-BV/快车道发布。 |
| Historical design context | `/Users/ivan/.codex/archived_sessions/rollout-2026-07-23T04-20-23-019f8e0f-b967-7221-aaf4-8ea6fd056256.jsonl`、`/Users/ivan/.codex/archived_sessions/rollout-2026-07-23T23-48-33-019f923d-39af-7730-9ada-a23385b3b55a.jsonl` | 早期讨论形成 evidence lanes、immutable digests 和 serial gates；这些是来源与设计背景，不覆盖当前 step 文件。 |

历史瓶颈的可操作解释是：昂贵的 provider/素材准备被 runner 全局锁包住，浅层 dry-run 没有
重放完整 after-image，导致错误按首个失败逐轮暴露；候选还被整场 raw state hash 互相牵连。提速
目标是提前暴露、独立分类、批量准备，最后仍以严格的串行提交和上传收口。

## 当前 live-branch implementation mapping（仅观察，不是部署证明）

基线为 `codex/fastlane-historical-run` 的 `6bc8ab927cb8a49a00feee867d5a7bd7a786133b`。
在该分支的项目文档中，以下能力已有文字/入口映射；本页没有验证 free host、运行进程、部署
commit、真实 provider 或 B 站公开面，因此状态只能写为“文档映射，runtime 未验证”：

| 方向 | 当前入口映射 | 仍需 root 验收 |
|---|---|---|
| P0 full no-target-write / typed matrix / sanitized receipt / readiness graph | `docs/pipeline/80-package-delivery.md:7-17,32-35`；`docs/pipeline/90-publish.md:7-15,19-26` | live code、负向写入 canary、receipt 内容不泄露、graph 与实际 state 一致 |
| P1 prepare outside lock / provider semaphore / short CAS lease | `docs/pipeline/80-package-delivery.md:19-30`；`docs/pipeline/90-publish.md:19-26` | 并行边界、跨进程 slot、lease/CAS 漂移拒绝、恢复不重复调用 provider |
| Candidate authority | `docs/pipeline/20-selection.md:9-20` | candidate-local identity 是否真正替代不必要的整场耦合 |
| Exact text/review gates | `docs/pipeline/40-subtitle-text.md:14-25` | final raw-byte review、source/full-window 与 delivery-local witness 的真实回执 |

## 契约：允许并行的部分与必须串行的 gate

### P0：先做完整、无目标写入的 after-image preflight

P0 的目标是同一 candidate 一次性得到可复现的全量结论，而不是逐个修复后再次部署：

1. 在 private stage 构造 canonical after-image，执行所有确定性 validator 和
   `_validate_after_image`；不得写 record/state/journal/target/upload。
2. 输出 typed predicate matrix：`source_fact`、record/publish mirror、title authority、
   cover route/pixel/host/participant/punch、state diff 等独立谓词。全部谓词都 fail-closed，
   不因展示更多错误而放宽门。
3. 失败时仅 create-only 写 hash-bound、sanitized receipt，含 commit/authority/candidate、
   谓词矩阵、stage manifest/provider-attempt 的 hash；不保存 cookie、media、prompt、原始
   provider response、completion 或 secret。private stage 必须清理。
4. 只读 readiness graph 将候选分为 `READY_TO_PREPARE`、`NEEDS_PROVIDER`、
   `NEEDS_IVAN_TRUTH`、`STATE_DRIFT`、`CODE_DEFECT`、`READY_FOR_SERIAL_UPLOAD`。
   blocked candidate 不得阻塞 ready candidate；graph 不是上传授权。

P0 的串行 gate 仍是 authority/truth 决策、最终字节审查、canonical audit、manifest verify
和显式授权；它们不能因“全量 preflight”而被跳过。

### P1：准备并行，提交与上传串行

| 可并行（candidate-local） | 必须串行（全局 side effect） |
|---|---|
| package/ASS 审计、标题/封面 QC、manifest replay、source-fact/cover prepare 与 deterministic validate | runner commit lease 内的最新 preimage/CAS、journal/state-last commit |
| 不同 candidate 的私有 stage；provider 调用受跨进程 provider semaphore 限流 | `upload.lock` 下的 Bilibili transport、状态 reconciliation、合集/公开 readback |
| 纯测试可在已完成审计后 xdist；固定路径、锁和远程测试保持串行 | 上传失败/歧义不得自动重复上传；单一 uploader 是唯一上传副作用入口 |

prepare 必须在 runner lock 外完成；只在短 lease 内重读 runtime/deployed authority、比较完整
preimage、确认 intended store 后提交。任一漂移、busy、store inventory/hash/symlink 异常都在
任何 formal journal/target/state 写入前拒绝。

### P2：row hash 延后

candidate-row hash/state generation 与通用 sealed transition 可作为后续局部重构，但不属于
本轮快车道必需项。先保持 candidate-local identity 和现有 state/manifest authority 可读、可审计；
不要为追求抽象而改写当前 publication hot path。

## 更早的 evidence lanes 与 serial gates

这是历史设计上下文的 source-bound 摘要，不是新的 step 规则。可并行收集 candidate-local 的
source media digest、ASR/字幕、弹幕/SC、视觉/角色、专名/已知词、日期/上下文和标题/封面资产
证据；所有 lane 共享 immutable `candidate_id`、source media SHA-256、clip-context digest、
policy epoch、publication target 与 artifact hashes。合并后仍必须按顺序通过：

`authority merge → truth decision → boundary resolve → exact final bytes → canonical audit →
human final review → manifest verify → same-BV/live verify → serial upload/public readback`。

任何 lane 的“已完成”、review-ready 或本地 graph 分类都不等于上传许可；真实公开状态必须重新
读取。此前历史报告中的测试数量、部署状态和发布状态均不在本页复用为当前事实。

## 缓存边界（NEW SUGGESTION，未找到原始批准）

指定原始证据只批准了并行、限流、短 lease、全量 preflight 和批量收敛；没有批准把 provider
结果或 after-image 作为通用缓存。因此 candidate/provider/result cache、跨 run 复用或 content
addressed cache 目前只能是新建议，不能写入 runtime contract。若未来提出，必须另行定义：
输入 digest、policy/provider/version key、TTL、失效条件、敏感数据擦除、跨 candidate 隔离、
receipt provenance，以及 provider 重新验证 gate；在 step 文件和代码强制层落地前不得假定命中
缓存即可跳过任何 serial gate。

## 实施职责与停止条件

root 只负责目标、优先级、authority、调度、智力判断、review、acceptance 和 deployment approval，
不直接执行 implementation 或 production mutation。Terra/Luna 只作为 root 的直接 worker，
本战役默认使用 medium；确有复杂性时才可升至 high，禁止 xhigh/exhigh，且禁止递归开 agent。
实现时应按不重叠文件/阶段拆分，由 root 读取 exact diff、测试、runtime、部署、rendered/public
surface 后验收。最快安全收口是：
先批量完成 P0/P1 → 一次完整 suite → 一次 canonical deploy → live/readback 验证 → 再恢复
Qixi-first 队列；任何发布动作仍须另有授权，不由本页自动触发。

本页维护 source-bound provenance 与设计入口；若与 step 文件或代码强制层冲突，以后者为准，
并应通过 README/step 的陈旧规则扫描发现冲突，而不是在本页另立例外。

## 2026-08-24 current code verification appendix

本附录是对本轮 current-baseline audit 的源码与 focused-test 核对；它 supersede 了本页上方“runtime 未验证”的历史表述，但不冒充 free host、部署或 B 站 live readback 证明。逐项核对到的当前 code symbols/path 如下：

| 设计项 | 当前源码证据 |
|---|---|
| full no-target-write canonical after-image preflight | `src/autoslice/qixi_post_correction_public_surface.py:1346` `prepare_qixi_after_image()` 在 private stage 构造/校验 after-image，docstring 明确不写 runner/state/journal；`src/autoslice/qixi_post_correction_modes.py:880-925` 的 `run(Mode.FULL_DRY_RUN)` 调 `_build_after_image()`、`_assert_prepare_preimages()`、`_validate_after_image()`，只输出 full-dry-run outcome。 |
| aggregate typed predicate matrix | `src/autoslice/qixi_post_correction_modes.py:153` `collect_matrix()`；`src/autoslice/qixi_post_correction_diagnostics.py:55` `collect_predicates()` 与 `:107` `matrix_document()` 固定 schema/predicate 集合，formal/stage/cleanup 结果在 `qixi_post_correction_modes.py:925-1008` 汇总。 |
| create-only sanitized diagnostic receipt | `src/autoslice/qixi_post_correction_modes.py:511` `_failure_receipt()` → `src/autoslice/qixi_post_correction_diagnostics.py:170` `write_failure_receipt()`；receipt body/hash-bound、create-only，且诊断路径只接受 sanitized predicate/provider evidence。 |
| all-candidate read-only readiness graph | `scripts/publication_readiness_graph.py:2-23` 是固定 no-arg read-only entry；`src/autoslice/publication_readiness.py:562` `build_readiness_graph()` 读取 registry/state/manifest/ledger，返回 `observational_only` graph，不授予 release/upload。 |
| candidate-private prepare/validate 在 `runner.lock` 外 | `src/autoslice/qixi_post_correction_public_surface.py:1346-1413` 的 `prepare_qixi_after_image()` 先建 private after-image；`src/autoslice/qixi_post_correction_public_surface.py:1413` `commit_qixi_after_image()` 才进入提交路径。实现 docstring 与 `scripts/finalize_qixi_post_correction_public_surface.py:1-11` 均明确 provider/private validation 不持 hot mutation lock。 |
| bounded cross-process provider slots | `src/autoslice/provider_slots.py:260` `provider_slot()` 使用 runtime-local `provider-slots/slot-N.lock`、跨进程 `flock`、进程内 thread mutex 与 capacity/timeout bound；`src/autoslice/provider_slots.py:118` `runtime_provider_slot()` 是 provider adapter 入口。 |
| short CAS/commit lease | `src/autoslice/qixi_transaction_core.py:417` `exclusive_runner_commit()` 提供 inode-validated、non-reentrant runner commit lease；`src/autoslice/runner_state_writeback.py:546` `write_exact_state_under_lease()` 只安装已验证 after-image，并要求 lease。 |
| single serial Bilibili uploader/reconciliation | `scripts/authorized_upload.py:2138` `exclusive_upload_lock()` 串行 verified upload/ledger writes；`scripts/authorized_upload.py:2166-2175` `upload()` 在同一 lock 内 verify、ledger guard 与 transport；publication reconciliation 继续由该 serialized upload/repair lane 收口。 |

### Focused verification

实际运行命令：

```text
uv run --with pytest --with pillow pytest -q \
  tests/test_qixi_post_correction_public_surface.py \
  tests/test_qixi_post_correction_diagnostics.py \
  tests/test_provider_slots.py \
  tests/test_provider_adapter_slots.py \
  tests/test_qixi_transaction_core_lease.py \
  tests/test_publication_readiness.py \
  tests/test_replay_reviewed_subtitle_baseline_cli.py
```

结果：`212 passed in 35.47s`。这些 focused tests 覆盖 private after-image/no-write 与 cleanup、typed matrix/sanitized receipt、provider slot 的跨进程/并发边界、runner lease/CAS、readiness graph 的 observational-only 行为，以及 replay prepare/readiness 的串行提交约束。

在上述源码和测试证据范围内，当前 baseline 没有已确认的 material speed gap，因此本轮没有代码改动。C1/C2/C12 等 candidate-specific packaging bugs 不属于流水线并行设计缺口，不能用来否定或夸大本 appendix 的结论；部署/runtime/public surface 仍须由 root 另行现场验收。
