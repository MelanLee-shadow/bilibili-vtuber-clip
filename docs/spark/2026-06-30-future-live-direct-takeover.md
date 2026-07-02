# 2026-06-30 future-live E2E direct takeover plan

## 目标

为下一场“将来发生的真实直播”准备一条**无人值守、no-upload、从录制开始到最终切片结束**的完整 live E2E 路线，且所有关键节点都 fail-closed：

```text
录制/换源落盘
→ 录制完成检测
→ source SRT 就绪
→ full-session candidate selector / 既有切片候选接入
→ source-context 精听
→ 术语库约束
→ CPA/边界/内容 QA
→ preview render + render QA
→ AUTO_UPLOAD shadow decision
→ upload gate 停住（无显式授权不得真传）
```

本文件只做本地规划与接线准备；**不启动真实录制，不启动真实上传，不修改远端 runtime**。

## 当前代码面已确认的能力

### 1) shadow daemon 已有安全外壳，但还不是“完整 future-live 接管器”

`scripts/lidousha_auto_review_shadow_daemon.py`

- 已有单实例 daemon lock。
- 已能在 `run_once()` 内先做 `source_integrity`，再决定是否进入 shadow。
- 已能在 `.jingting.done` 缺失时，若 `publish/evidence` 带有：
  - `source_video`
  - `source_srt`
  - `anchor_start_ms`
  - `anchor_end_ms`
  则走 live-source route，而不是直接卡死在 `jingting_incomplete`。
- 目前若当天还没有 prepared slice，仍会直接返回 `no_prepared_slices`；这意味着它**还不会自己从完整 live source 里主动选 full-session candidate**。

### 2) live-source shadow pipeline 已能跑 source-context → boundary → render QA → shadow gate

`scripts/run_auto_review_shadow_pipeline.py`

- `_run_live_source(...)` 已接上 `execute_source_context_job(...)`。
- 成功路径会：
  - 从 full-source SRT 裁出 context draft SRT。
  - 产出 refined SRT / jingting manifest / done marker。
  - 解析 refined cues。
  - 跑 boundary resolver。
  - materialize `replacement_recuts/*.recut.mp4 + .srt + .manifest.json`。
  - 落 `*.render_qa.json`。
  - 对 `AUTO_UPLOAD` 候选，如 stream-copy 切点误差 >100ms，会自动触发 accurate rerender。
- shadow decision 最终仍受 `is_publish_gate_satisfied()` 保护；没有完整 artifact hash + clean AUTO_UPLOAD manifest，不会通过 publish gate。

### 3) full-session selector 已存在，但没有接进 daemon 自动路径

`src/autoslice/full_session_candidate_selector.py`

- 已可从完整 source timeline 中选择 setup→payoff→closure 候选。
- 会先用 `resolve_talk_boundary(...)` 做一次召回期边界筛选。
- 当前更多是“离线/一次性 runner 能用”，**不是 daemon 的默认入口**。

### 4) source-context planner 存在，但 daemon 目前没有真正调用 planner

`src/autoslice/source_context_planner.py`

- 已有稳定 job manifest 结构与 provenance。
- 当前 live-source route 主要还是从 `publish/evidence` 直接拼 `source_context_job`，而不是通过 planner 统一出 job。

### 5) 术语库仅在 jingting runner 侧存在全局入口

`scripts/gemini_slice_jingting.py`

- 术语库来源：
  - `LIDOUSHA_GLOSSARY`
  - `/opt/bilive/app/lidousha_glossary.txt`
  - `/app/lidousha_glossary.txt`
- 这说明**已有全局 glossary 注入能力**，但 source-context job 本身尚未记录：
  - glossary path
  - glossary sha256
  - 本场是否使用特定术语覆盖

## 当前对 future-live E2E 的真实缺口

### A. daemon 入口仍依赖“已有 prepared slice”

现状：

- `discover_slices()` 只看 `*-.publish.json`。
- `run_once()` 在 `not slices` 时直接 `no_prepared_slices`。

结论：

- 对“下一场未来直播”的真正无人值守 full-live test 来说，**还缺一条当天直播结束后，直接从 full source / official_source / replacement_source 里做 candidate recall 的入口**。

### B. 缺少“录制完成”判定 hook

当前已有：

- `source_integrity`
- `replacement_source` 覆盖检查
- `local_prepare` / `scan` / `upload` 监控逻辑（`scripts/lidousha_slice_monitor.py`）

当前缺少：

- 一个明确的 `RECORDING_COMPLETE` / `SOURCE_READY` 判定器，来告诉 shadow daemon：
  - 还在直播中，不要动；
  - 直播已结束但本地录制仍在 flush，不要动；
  - 本地录制有洞，等 official/replacement source；
  - 完整源已 ready，可以做 full-live selector lane。

### C. source SRT 的“整场真源”入口不统一

已知历史 proof 有两类：

- 由完整官方/换源视频 + 拼接 full-session SRT 做 acceptance。
- 由既有 prepared slice / publish / evidence 反推 source metadata。

对未来 live test 来说，需要统一成：

- **优先 official_source / replacement_source 完整源**。
- 若只有本地录制且 `source_integrity.can_use_local_source=true`，允许走本地整场源。
- 若两者都不成立，必须 fail-closed，不允许把破碎录播伪装成完整 live source。

### D. glossary/术语库没有进入 machine-auditable provenance

现在 glossary 只在 jingting prompt 侧生效；future-live E2E 若想证明“source SRT/术语库”链路真的接上，需要在 job/result manifest 中留下：

- `glossary_path`
- `glossary_sha256`
- `glossary_source`（env / host path / container path）

### E. upload gate 虽已存在，但未来 live lane 还需要一个更前置的 hard stop

虽然 `is_publish_gate_satisfied()` 已是最终 gate，但 future-live 无人值守测试还应额外要求：

- shadow lane 固定 `no_upload=true`
- 任何 `would_upload` 只写 marker，不启动 uploader
- 测试前/后验证不存在 `src.upload.upload` / `biliup` / `UploadController`

## direct takeover 实施路线（下一场直播）

下面不是 Kanban，而是**按时间顺序的接管脚本设计**。

### Phase 0：开播前预置

只做安全预置，不触发上传：

1. 保持已有安全前提：
   - jingting daemon 运行
   - auto-review shadow daemon 运行
   - upload daemon 关闭
2. 监控层确认：
   - `scan` 可运行
   - `local_prepare` 可运行
   - `upload=0`
3. 预创建未来 live state 根：
   - `reports/future_live/<room>/<date>/`（建议在远端，不在本地长期保存）
4. 记录本次预期 source preference：
   - `official_source > replacement_source > clean local source`

### Phase 1：直播进行中

目标：只录，不切 full-live。

规则：

- 当 `live=YES` 时：
  - 允许 `scan/local_prepare/jingting` 继续各自已有安全路径。
  - **禁止 full-live selector lane 提前启动**。
- 若只存在增量 `.m4s`/`.flv` 且文件还在增长：
  - future-live state 标为 `RECORDING_ACTIVE`。

### Phase 2：录制完成检测

需要新增一个判定 hook，建议名字：

- `detect_recording_completion(date_dir) -> {status, reason_codes, preferred_source, ...}`

建议判定顺序：

1. 若监控仍显示 live=YES → `RECORDING_ACTIVE`
2. 若最新录制媒体仍在增长 / 最近修改时间过新 → `RECORDING_FLUSHING`
3. 跑 `build_date_source_integrity(...)`
4. 若本地完整 → `LOCAL_SOURCE_READY`
5. 若本地不完整，但 `official_source/replacement_source` 覆盖 expected range → `REPLACEMENT_SOURCE_READY`
6. 否则：
   - `WAIT_OFFICIAL_SOURCE`
   - 或 `BLOCKED_SOURCE_GAP`

这个 hook 应成为 future-live lane 的总开关；没有 READY，不允许进入 full-live selector。

### Phase 3：full-source SRT 就绪

对 READY source，统一产出：

- `full_source_video`
- `full_source_srt`
- `acceptance_source_manifest`

优先级：

1. official_source 现成 full video + full SRT
2. replacement_source 拼接 full video + 聚合 full-session SRT
3. 本地 clean source + 全量 source SRT

必须输出的 machine evidence：

- `source_video_sha256`
- `source_srt_sha256`
- `source_duration_ms`
- `source_kind` = `official_source|replacement_source|local_source`
- `source_integrity_snapshot`

### Phase 4：candidate recall 接管

这里是 future-live lane 最关键的新增接线。

建议新增一个 daemon hook：

- `run_full_live_selector_once(date_dir, source_video, source_srt, output_dir, ...)`

行为：

1. 解析 full-source SRT。
2. 调 `select_full_session_candidates(...)`。
3. 对每个 candidate：
   - 生成稳定 manifest。
   - 补 `duplicate_corpus`。
   - 走 source-context live-source pipeline。
4. 汇总 `selector_summary.json`。

注意：

- 这条 lane 应该在 `no_prepared_slices` 时补位，而不是替代既有 prepared-slice shadow。
- 即：未来可同时存在：
  - prepared slice review-package route
  - full-live selector route

### Phase 5：source-context / glossary / CPA QA

每个 full-live candidate 进入 live-source pipeline 时，应把以下信息并入 `source_context_job` 或其 provenance：

- `candidate_id`
- `title`
- `anchor_start_ms`
- `anchor_end_ms`
- `duplicate_corpus`
- `provenance.source_sha256`
- `provenance.recording_id`
- `glossary_path`
- `glossary_sha256`
- `selector_version`

关于 CPA QA：

- 当前仓内的“CPA”主要仍停留在 anchor/candidate 来源语义上，并未形成新的独立 future-live QA daemon。
- 对本次接管，建议把“CPA QA”明确收敛为：
  1. selector 候选文本必须保留 `text_preview`
  2. boundary resolution 必须落盘
  3. content evidence / payoff / open loops / duplicate / editorial 分必须落盘
  4. 任何缺失字段 fail-closed

换言之，本次不再新增一个模糊的“CPA 人审环节”，而是把 CPA 相关价值收敛进 machine evidence。

### Phase 6：preview render QA

当前 pipeline 已有较强基础，可直接纳入 future-live lane 的验收条件：

- 每个 materialized recut 必须同时落：
  - `.recut.mp4`
  - `.recut.srt`
  - `.recut.manifest.json`
  - `.recut.render_qa.json`
- 需要检查：
  - `actual_cut_error_ms`
  - `accurate_rerender_used`
  - `render_qa.pass`
- 若 stream-copy 漂移过大：
  - 自动 accurate rerender
  - rerender 后仍不通过则 BLOCK/AUTO_RECUT，不准穿透到 upload gate

### Phase 7：upload gate（必须停住）

未来 live E2E 的验收只到 shadow/no-upload：

- 允许看到 `AUTO_UPLOAD` decision
- 允许写出 `.auto_review.would_upload`
- 不允许：
  - 启 uploader
  - 改成真实 publish
  - 真上传

必须验证：

- `publish_gate_shadow.decision_manifest_gate_satisfied == true` 只代表“理论可发”
- **不代表本次测试要真发**

## 建议补的脚本 hooks

### Hook 1：recording completion detector

建议落点：`scripts/lidousha_auto_review_shadow_daemon.py`

新增函数：

```python
future_live_state_for_date(...)
detect_recording_completion(...)
```

最少输出：

- `status`
- `reason_codes`
- `preferred_source_kind`
- `source_integrity`
- `live_still_active`
- `latest_media_mtime`
- `latest_media_size_stable`

### Hook 2：no-prepared-slices fallback to full-live selector

建议落点：`scripts/lidousha_auto_review_shadow_daemon.py`

当前：

- `if not slices: return no_prepared_slices`

建议改为：

- `if not slices and future_live_source_ready: run selector lane`
- `if not slices and source not ready: return WAIT/BLOCK summary`

### Hook 3：selector runner adapter

建议落点：新脚本或 daemon 内 helper。

职责：

- full-source SRT → `select_full_session_candidates(...)`
- candidate → `source_context_job`
- source_context_job → `run_shadow_pipeline(source_video=..., source_srt=...)`

### Hook 4：glossary provenance backfill

建议落点：

- `scripts/gemini_slice_jingting.py`
- `src/autoslice/source_context_executor.py`
- 或 planner/job manifest

要求：

- 在 manifest 中留下 glossary path/source/sha256
- 让 future-live E2E 能证明“术语库确实参与了本次 source-context 精听”

### Hook 5：future-live summary artifact

建议输出一个总报告：

- `future_live_summary.json`

最少字段：

- `recording_completion`
- `selected_source`
- `source_integrity`
- `selector_counts`
- `shadow_counts`
- `auto_upload_candidates`
- `auto_recut_candidates`
- `block_candidates`
- `drop_candidates`
- `retry_candidates`
- `render_qa_failures`
- `would_upload_markers`
- `upload_processes_before`
- `upload_processes_after`
- `no_upload_confirmed`

## 下一场真实执行时的推荐命令顺序（只列 runbook，不在本地执行）

### 1. 赛前只读确认

```bash
ssh free 'cd /opt/bilive/app && git status --short --branch'
ssh free 'docker exec bilive_record sh -lc "ps -ef | grep -E \"src\\.upload\\.upload|UploadController|biliup|lidousha_auto_review_shadow_daemon|gemini_slice_jingting\" | grep -v grep || true"'
```

### 2. 开播期间

```bash
# 只监控，不启动 uploader
python3 scripts/lidousha_slice_monitor.py
```

### 3. 收播后 future-live lane

目标动作，不是本地现在执行：

```text
detect_recording_completion
→ build/resolve full source
→ build full source SRT
→ select_full_session_candidates
→ run_shadow_pipeline(no_upload=true) for each candidate
→ write future_live_summary.json
```

### 4. 验收通过条件

至少满足：

1. 录制完成检测进入 READY，而不是误把 in-progress 录制当成完整源。
2. source_integrity 显示本地完整或 official/replacement 覆盖完整。
3. selector 真正跑了，`candidates_evaluated > 0`。
4. source-context refined SRT / jingting manifest / done marker 真实产出。
5. boundary resolution 真实落盘。
6. preview render 真实物化，且 render QA 落盘。
7. 若出现 `AUTO_UPLOAD`，只写 `would_upload` marker，不启动 uploader。
8. 测试前后上传进程都为 0。

## 对下一步实现的结论

如果只看本地现状，**下一场未来直播的完整无人值守 live test 已具备 70% 左右骨架**：

- 有 shadow daemon
- 有 live-source route
- 有 source-context executor
- 有 boundary resolver
- 有 render QA / accurate rerender
- 有 publish gate

但要真正接住“下一场未来直播”，还差 3 个必须补齐的接线点：

1. **录制完成检测**
2. **no-prepared-slices → full-live selector fallback**
3. **glossary/source provenance 的 machine-auditable 落盘**

在不碰远端 runtime 的前提下，这三项就是最直接、最安全、最接近真实 future-live takeover 的准备方向。
