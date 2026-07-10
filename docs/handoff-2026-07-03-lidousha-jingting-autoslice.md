# Handoff 2026-07-03：李豆沙 7/2 录播自动切片（含二次精听）——已跑完，结果在下

> **SUPERSEDED / 历史证据，勿照抄命令**：本文保留 2026-07-03 当时的作业状态和失败复盘。尤其下方 NetEase-only、手工清目录/pkill、长段 AGY 与 no-upload acceptance 命令不再是当前运行手册。当前 song/LRC 权威请读 `.agent/skills/song-lyrics-timeline-aligner/SKILL.md`、`docs/spark/2026-06-30-future-live-e2e-runbook.md` 与 `docs/reviews/2026-07-09-mebukutoki-lrc-repair.md`。

## 2026-07-03 下午续篇：误诊纠正 + 工作流改造（语义召回 / 观众视角审查 / 分块精听）

**重要纠正：歌类候选的 BLOCK 不是精听超时。** 本文下面"超过 50m/65min 时限"的说法是误诊。实际证据：free 上作业目录 `ssh-source-context.context-20260703-144249` 显示 agy 只跑了 ~14 分钟就 rc=0 退出，stdout/stderr 均 0 字节、没写 output.srt；错误文案是 `did not produce valid SRT`（超时路径的文案是 `did not finish within 65min`）。同目录原样重跑复现了同样的空输出——**30 分钟/1.2GB 全段输入对 agy (gemini-3.5-flash) 是确定性的静默空转**。这是已知 bug 家族：antigravity-cli#76（非 TTY 下 print 模式丢 stdout、exit 0）、gemini-cli#24290（只吐 thought token 时空响应+exit 0）、官方支持帖确认 >30min 视频不可靠。

当天按 Ivan 指示完成的工作流改造（全部有回归测试，231 passed）：

1. **分块精听**（`src/autoslice/jingting_chunker.py` + `_build_ssh_agy_runner` 重写）：context 按 cue 间隙切 ~5 分钟块（社区转写甜点 5-10min），本地重编码 1280p/crf28（对齐已验证成功的 ~50MB 输入档），逐块 agy 精修（`script -qec` 伪 TTY 规避 #76），时间轴逐块强校验后拼回原 draft 时间轴。每块最多重试 1 次（防重试风暴灌爆磁盘）。失败按 `AGY_EMPTY_OUTPUT` / `AGY_TIMEOUT` / `AGY_FAILED_RC` 区分（`AgyRunnerError`），不再误诊。
2. **语义召回 lane**（`src/autoslice/semantic_candidate_selector.py`，`--semantic-recall-llm-command`）：观众视角 LLM 扫全场字幕选candidates（故事/弹幕互动/玩梗/歌），召回时就要求把上下文触发点包进窗口。关键词 primary/fallback 降为退路。实测 7/2 素材：语义 lane 一次性找到 `一眼AI破防`(604-688s，含看图触发点)、`反沙绕口令`(1266-1436s，旧 lane 完全漏掉)、`熊袭新闻叙事`(152-258s，旧 lane 误判为歌)。
3. **观众视角上下文审查**（CPA judge 扩展）：请求携带切片前后各 90s 原文（`surrounding_context`），judge 必答 `viewer_context_ok` + 扩窗建议毫秒数；观众看不懂且不可推测 → `VIEWER_CONTEXT_INCOMPLETE` 强制 block；选择器拿到扩窗建议后自动扩窗重审一次（`_ctxexp` 候选）。完整歌豁免此码（产品规则不变）。
4. **语义权威**：语义 lane 候选（含扩窗候选）打 `boundary_authority=semantic`——机械关键词边界只出 `ADVISORY_*` 参考码不再 DROP/改窗；content_evidence 的关键词 payoff/standalone/editorial 分数被 CPA 真实判决覆盖（`_apply_semantic_authority_evidence`）。机器可验证的 gate（时间轴、切割误差、查重、歌证明、精听 provenance、上传风险）权威不变。

验证跑输出目录：`reports/lidousha-autoslice-20260702/full_selector_jingting_v2/`（`selector_stage=semantic_recall`）。

---

## 以下为原始交接（其中歌类"超时"结论已被上面纠正）

**更新：管线在会话关闭前跑完了。** 结果：

- `fallbacksong_0_590646`（歌类，全段 1.2GB context）→ BLOCK `AGY_SOURCE_CONTEXT_RUNNER_FAILED`：agy Flash (Low) 精听 30 分钟全段超过 50m/65min 时限。诚实 fail-closed。**后续项：歌类候选的 context 需要分块精听或提模型档，见文末。**
- `fallbacktalk_616646_705486`、`fallbacktalk_789826_860626` → **二次精听真实完成**（`source-context.context.refined.srt` 已生成，生产同款 jingting prompt + 李豆沙词表），随后被真 CPA judge 以 NOT_INTERESTING / CPA_SEMANTIC_INCOMPLETE 等 DROP——两段内容是普通闲聊，gate 判定诚实（对比：昨天洁绯杂谈测试同一 gate 给出了 AUTO_UPLOAD）。
- 二次精听效果样例（fallbacktalk_616646_705486，11 处修正）：`阿董/阿斗→阿朵`、`容→融`（"这真的不是融了阿朵吗"）、`超贼→超级AI`、`他胸口→它胸口`（动物用"它"）、断句语病修正。draft vs refined 对比：
  `diff .../source-context.context.draft.srt .../source-context.context.refined.srt`

给 Ivan 看二次精听效果直接用上面两个文件的 diff。若还要一个"有趣的李豆沙成品切片"，换一段素材重跑（这 30 分钟段的 talk 候选被 CPA 判平淡；生产切片标题显示 `反沙绕口令` `跳闸犯傻` 等梗其实在这段里，但 fallback 选择器窗口没套准它们——值得排查 primary 选择器为何没出候选）。

---

以下为原交接内容（背景、续跑命令、约束）。

写给下一个 agent/会话。本会话关闭时后台管线进程会被杀掉，但 free 上的 agy 精听作业是 nohup 的、会继续跑完。按下面"如何续跑"操作即可。

## 当前正在做什么（被中断的任务）

Ivan 的指令：用李豆沙 2026-07-02 场次录播做自动切片，**必须走真实二次精听**（管线内 AGY jingting 精修，不用 `--copy-draft-context`），产出后展示"二次精听前后对比"（draft SRT vs refined SRT）——这是 Ivan 明确要看的交付物。

状态：管线已在跑第 1/3 个候选的精听。
- 素材已拉到本地：`reports/lidousha-autoslice-20260702/source_1100.mp4`（7/2 的 11:00-11:30 段，30min，1.2GB）+ `source_1100.srt`（生产 ASR，558 cues）。挑这段是因为生产切片标题显示它对话梗最密（反沙绕口令破防、跳闸犯傻、一眼AI 阿董/阿斗）。
- 输出目录：`reports/lidousha-autoslice-20260702/full_selector_jingting/`
- 3 个候选：`fallbacksong_0_590646`（歌类，context=全段 1.2GB，最重）、`fallbacktalk_616646_705486`、`fallbacktalk_789826_860626`（小 context，快）。
- free 上的当前 agy 作业目录：`/opt/bilive/jingting_jobs/ssh-source-context.context-20260703-144249`（nohup 拉起，跑完会写 `agy.rc`；本地管线死了它也会继续跑完，但结果没人收——续跑时直接重跑管线即可，job 目录可删）。

## 如何续跑（本会话关闭后）

本地管线进程已死的话，直接重跑（幂等，会重建输出目录）：

```bash
cd /Users/ivan/Project/vtuber-slice
D=reports/lidousha-autoslice-20260702
rm -rf $D/full_selector_jingting
ssh free 'pkill -f "agy --sandbox"; rm -rf /opt/bilive/jingting_jobs/ssh-source-context.*'
python3 scripts/run_full_session_selector_cpa_shadow.py \
  --source-video $D/source_1100.mp4 \
  --source-srt $D/source_1100.srt \
  --output-dir $D/full_selector_jingting \
  --room-id 22966160 \
  --agy-ssh-host free \
  --max-candidates 3 \
  --cpa-command "python3 scripts/cpa_semantic_qa_llm.py --request {request_json} --response {response_json} --transport direct --model gpt-5.4-mini --api-base \$CPA_BASE_URL --api-key-env CPA_API_KEY" \
  --lrc-provider netease \
  --burn-preview \
  --publish-staging \
  --song-hint-llm-command "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file}" \
  --title-llm-command "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file}"
```

注意时间预期：歌类候选（全段 context）的 agy Flash (Low) 精听实测 ~50-60 分钟；两个 talk 候选各几分钟到几十分钟。总时长可能 1-2.5 小时。runner 是"远端 nohup + 每 20s 轮询 agy.rc，总时限 65min，agy 自身 --print-timeout 50m"（今天刚修的，之前 40min ssh 长连接超时导致假失败）。若歌类候选超时 BLOCK，那是诚实 fail-closed，看另外两个 talk 候选的产出即可。

**跑完后要交付的东西**：
1. 每个候选 `source_context/<id>/source-context.context.draft.srt`（精听前）vs `source-context.context.refined.srt`（精听后）的差异对比——这是 Ivan 点名要看的"二次精听效果"。
2. 成品：`replacement_recuts/*.recut.burned-final-sapphire72.mp4`、`covers/*.ai-title.cover.png`、publish.json（检查 title/cover_status/reason_codes）。
3. 字幕排版抽查：任意 Dialogue 不得超过 2 行、每行 ≤28 字（今天刚实装的规范，见下）。
4. 写 OPEN_ME.md（参照 `reports/live-talk-test/20260703-room27481656/OPEN_ME.md` 的格式）。
5. 李豆沙包还要查词表泄漏（`天不熊`/`kimo熊` 必须归一为 `kmx`）。

## 今天已完成并验证的事（都有回归测试，195 passed）

1. **字幕早 ~20ms 根因修复**（不是加偏移）：歌切字幕改为 LRC 全局位移时间轴（`subtitle_source=external_lrc_global_shift`）、歌切强制精确重编码（禁 copy-cut 出成品）、ASS 时间 round 不 floor、sapphire72 头修正为 1080p 规格。详见记忆 `lidousha-song-lyrics-strict-timing` 和 `docs/workflows/lidousha-song-finished-package-workflow.md` §6。
2. **假完整歌修复**：`song_repair.py` 换成单调 DP + 两遍全局位移一致对齐（副歌重复、假开头都处理），span/中段覆盖强校验。完整歌 e2e 已验证（`reports/live-song-test/20260703-010424-room362064/OPEN_ME.md`，整首《雨天》）。
3. **CPA 全真实**（含测试）：Cloudflare 403 error 1010 需浏览器 UA（已加进 `llm_client.py`、`_call_cpa_image_edit`、`scripts/llm_via_cpa.sh`）。语义 QA judge=`scripts/cpa_semantic_qa_llm.py --transport direct --model gpt-5.4-mini`。标准命令固化在 `docs/spark/2026-06-30-future-live-e2e-runbook.md` § "Canonical validated song e2e command"。
4. **对话切片 e2e 成功**：房 27481656 洁绯杂谈，AUTO_UPLOAD 零 reason codes，真 CPA hook 0.72，见 `reports/live-talk-test/20260703-room27481656/OPEN_ME.md`。
5. **字幕排版规范**（Ivan 今天新要求 + 参考 `LLM Multimodal ASR/scripts/polish_srt_for_viewing.py --max-chars 28`）：≤28 字/行、≤2 行/条、优先 1 行，超长句按文本占比拆成顺序子 cue（`_layout_cue_for_display`），绝不堆 3-4 行。已实装+测试+写进 SKILL/工作流文档。
6. **ssh 二次精听 runner**：`run_full_session_selector_cpa_shadow.py --agy-ssh-host free`（生产同款 jingting prompt/词表/时间轴校验，执行搬到 free）。
7. **free 磁盘清理**：`/opt/bilive/jingting_jobs/22966160/2026-06-29` 有 7617 个重试风暴 scratch 目录占 94G，磁盘 100% 满已清（耐久产物确认安全后删除），manifest 在 `cleanup_manifests/free_jingting_jobs_20260629_retry_storm_cleanup_20260703.json`。**后续项：jingting daemon 需要重试上限/去重护栏**，否则一个卡住的切片又会灌爆磁盘。

## 未提交的工作树

全部改动都在工作树未 commit（Ivan 没让 commit）。`git status` 会显示：pipeline/song_repair/selector 脚本、测试、SKILL/docs、新脚本（`llm_via_cpa.sh`、`transcribe_live_song_via_agy.sh`、`transcribe_live_talk_via_agy.sh`）、cleanup manifest。`python3 -m pytest -q` 195 passed，`git diff --check` 干净。

## 硬性约束（勿回退）

- 封面永远真 CPA `images/edits` gpt-image-2 出图（含工作流测试），失败 fail-closed，禁抽帧冒充（记忆 `cpa-real-ai-cover-always`）。
- 歌词字幕严格流程 + 字幕排版规范见上；旧验收包已标 SUPERSEDED（`reports/live-song-test/20260702-234414-room362064/`），别从那里抄流程。
- 一切 no-upload：`upload_enabled=false`，不许真上传。
