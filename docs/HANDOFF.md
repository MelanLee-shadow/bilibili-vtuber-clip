# Current handoff

Updated: 2026-07-27 ~22:15Z（北京 07-28 06:15）by Claude Fable root session（context limit 收尾；Ivan 指定另一 agent 接手）。
旧 handoff 内容在 git 历史（本文件此前版本），当天仍相关的事实已折入本版。

## 0. 一句话现状

李豆沙**正在直播**（runner 22:10Z 起 live-wait，下播后自动收录+跑当天批次）。今晚发生并已修复 BV1ec3A6bEWF 事故（争议搁置件被当新切片上传 + 主播名两处误听照发）；三件修复级重产（850_940 拉拉版 / 909 / 1209_1410）已入队，**等下播后的 tick 自动跑**。生产树 HEAD=**19176a2** 已部署 free（runner md5 verified）。全套测试 2463 passed。

## 1. 目标（不变）

无人值守切片流水线：修复优先、fail-closed 兜底；发布走授权链；「发出去我检查有问题再修，而不是一直不发」。今天新增的硬序（Ivan 原话）：「先把流水线应用上，再开始后面的切片生产」——已执行完毕，生产已恢复。

## 2. 今晚已完成（全部已 commit + 部署）

按 commit 顺序（main 分支，全部已到 free /opt/bilive/autoslice/repo）：

- **a730fdf** 909 近音字真值「谢谢AC风的比心」（Ivan 授权：无任何可考来源——录播记录缺失、回放弹幕无名、画面无 toast、人耳亦无真值）。
- **a1adb5a** BV1ec3A6bEWF 事故三修复：
  1. **出版登记闸口**：`assets/lidousha/publication_registry.v1.json` + `src/autoslice/publication_registry.py`。authorized_upload 在 make-manifest 与 upload 前强制查询：published 候选拒新投稿（只许 edit-replace 原 BV）、hold_pending_review 拒任何上传、registry 不可读 fail-closed。**1209_1410 已登记 hold**；850_940 已登记 published→BV1ec3A6bEWF。
  2. **witness 否决权修复**：听写 `self_count_mismatch=true`（自称音节数与拼音串对不上）时不得经拼音门否决 judge 排序选择；branch=`WITNESS_SELF_INCONSISTENT_JUDGE_APPLIED`；删除类维持严门。
  3. **1573 冻结恢复豁免**：repair-run 路径 `load_and_verify(frozen_plan_resume=True)` 跳过 live 政策重算（部署演进曾把冻结置换卡死），哈希不可变性照常。
  另含 850_940 两条真值（cue15 截断口号「支持李豆……」——她下一句自述「这句话都还没说完」，忠实截断不补全；cue16「我连"支持李豆沙"这句话都还没说完呢」）。
- **3f230c4** 850_940 上传证据入库（reports/authorized_uploads/2026-07-24-incident-850940/）+ cue1 真值初版。
- **5e23ab7** cue1 真值 **Ivan 人耳定版「李姐拉拉，你新来的」**（机器听写 lai-la，我过度合理化成「来啦」被纠正——人耳高于机器听写）。
- **3e977c2** **CPA judge 缓存**（键=prompt_sha：同听写+同候选+同语境零重复请求；JUDGED 终态才入；根=env `AUTOSLICE_BASE`，无根旁路）+ runner env 注入 AUTOSLICE_BASE + Ivan 要求台账第六节。声学缓存（clip_sha 键）此前已在。
- **19176a2** **deploy-yield 尾巴保全**：produce_batch_windowed 只返回已开工前缀，合并层原先把未派发队尾清空（**909 今晚因此从 7/25 picks 蒸发**）；现尾巴留队 + defer 时提前收官；talk/song 双 lane 同修。

事故语境（给接手者）：850_940/1209_1410 是 7/24 当晚批次（上传许可:否）交付后搁进 `lidousha/2026-07-22-v15-preview/争议片段待Ivan裁定/` 的搁置件；今日重产出 review_ready 后上传链把 review_ready 当放行传了（BV1ec3A6bEWF）。Ivan 认定字幕大量专名错（支持留守/刘若莎=李豆沙、拉拉=李姐拉拉、都带抽我=就带宠物）。裁定链其实全部检出、judge 都投了正确候选，是拼音硬门+自不一致听写否决了 judge——已修。画面探针实锤：被查看角色 角色ID=李豆沙（粉丝同名号入会），kmx 粉丝原话「我只留了一个召唤宠物的技能」。

## 3. 进行中（后台状态，接手必读）

- **直播中**：runner（我手动踢的 tick，free pid 3372359，flock 持锁）进入 live-wait；下播后收录、跑 2026-07-28 新批次；cron `*/10` flock -n 与之互斥，无需干预。
- **三件重产已入队**（state 行均 failed(recoverable)，7/24 退避计时器已按 Ivan 指令清零）：
  - `auto_193129_850_940`（7/24）：重产将应用「李姐拉拉」等 4 处修复；**出包后不是上传**（registry 会拒），走 edit-replace，见下一步①。
  - `auto_192000_909_1014`（7/25）：行是我按审计格式重建的（deferred-tail 蒸发事故，revival 块注明来源）；真值 `20260725-ac-bixin-thanks-r1` 重产时应用；出包后走标准上传（从未发布、无 hold）。
  - `auto_183122_1209_1410`（7/24）：上轮 producer_error，重试引擎自跑；**registry hold，Ivan 放行前绝不上传**。
- **1573 置换（BV1DAg46HEXE）**：本地链路全通（审计豁免+biliup cookie 已 renew，canary rc=0）。纯等 B 站 Creator 把新 cid 40356020489 从 -30 翻正；翻正后 repair-run 幂等续跑完成置换。plan/journal 全套在 free `/opt/bilive/autoslice/recovery/2026-07-22/full-rerun-v15-screenshot-cover/release-1573/`。若 canary 再报 rc=1：`cd /opt/bilive/app/tmp_manual_upload && /opt/bilive/bin/biliup -u biliup_cookies.json renew`（bilitool 的 cookie 是另一份，今晚一直健康）。
- **CloudFS 挂载**：22:00Z 死过一次，watchdog 22:05Z 自动重挂+消费者门 COMPLETE。老病，watchdog 管。
- **CPA 链路已实弹验证**（22:16:39Z 探针 rc=0，Ivan 可在网关对时间戳）：judge/终审/标题只走 `scripts/llm_via_cpa.sh`（CPA env 缺失=exit 2 硬失败，无任何 AGY/Gemini 回落）；AGY 仅精听源上下文 lane。Ivan 曾疑「verdict 全用了 AGY」——结论：否，安静时段=产线真闲着（停产改造+live-wait）。
- **费用**：今日付费 1016 笔 ≈$6.01（Ivan 已知；「每 $2 报一次」只是本会话临时协议，**不进生产**）。cap=GEMINI_PAID_BACKUP_DAILY_CAP=2000（cpa.env）。账本 `state/gemini-paid-backup/usage-<date>.jsonl`（purpose 分桶：entity_audio_verdict=听写层）。免费池天花板=3key×2model×20RPD=120 笔/日。
- **⚠️ 我的会话级监控随本会话死亡**，接手者需自建（脚本在 free /tmp，重启会丢，均可轻易重写）：
  - `python3 /tmp/lane_poll.py`：各日 rr/fail/rej + tick 摘要 + 主 lane 静默哨兵（>25min 无日志且 pgrep==0 才 STALL）。
  - `python3 /tmp/repro_poll.py`：三重产候选状态 + 850_940 烧录件 sha（旧包 sha 前缀 b6a61ab8，换血=重产完成）。
  - 1573 续跑：每 ~10min 一次 repair-run（幂等，BLOCKED_DRIFT=正常等待）；或等翻正后手动一次。

## 4. 阻塞 / 等待外部

- 850_940/909/1209 重产：等**下播**（直播中不产）。
- 1573 完成：等 B 站审核翻正（外部）。
- 1209_1410 上传：等 **Ivan 放行**（hold）。
- 7/24 state 里 5 条 review_ready（fourpack+44_293）是已发布常态（发布不改 state 行）；它们都在 registry published 名单，上传链会拒重传——**设计如此，不是 bug**。

## 5. 下一步（按序）

1. **850_940 出包后**：验证新 SRT 四处修复在位（`grep '李姐拉拉\|支持李豆\|李豆沙\|就带宠物' …/replacement_recuts/auto_193129_850_940.recut.srt`；注意 `.recut.burned-final-sapphire72.srt` 是 19:42 的陈旧孤儿边车——record.json 的 subtitle_sha256 指向 `.recut.srt` 才是真身；孤儿正占着上传链同名位，顺手删除或等重产覆盖）→ **edit-replace**：`cd /opt/bilive/autoslice/repo && python3 scripts/bili_archive_tool.py replace BV1ec3A6bEWF --media <新烧录mp4>`（编辑不占配额；封面/标题不动）→ 证据追加进 `reports/authorized_uploads/2026-07-24-incident-850940/` + registry 该行 note 更新 + commit（authorized-upload-must-commit）。
2. **909 出包后**：标准上传链（build_lidousha_daily_review_manifest → audit_lidousha_review_package --json → authorized_upload make-manifest（--season talk，quote=无人值守常令）→ verify → upload）→ 证据入 `reports/authorized_uploads/2026-07-25-revived/` + **registry 追加 published 行** + commit。
3. **1209_1410 出包后**：停在 review_ready，报 Ivan 等放行。
4. **1573 翻正后**：repair-run 续跑完成 → repair-verify-live → completed sidecar → 报 Ivan。
5. **07-28 新批次**：下播后自动跑；无人值守授权有效；**新发布必须同步写 registry 行**（纪律尚未自动化——把 registry append 挂进 upload 成功路径是个好的下一步）。
6. Backlog（不急）：truth-refresh 真·单句修复模式（R-成本-02 残余，台账明确「未实现」）；1863「行」320ms timing dice；672/V15 provider transient（V15 fail:4）；1493 沙豆李字幕置换 BV1zzgd6JEHe（registry+edit-replace 流程已具备，照①做）；3573/1475 结案文书；7/18 遗留 kmx称呼串+七星仍 hold。

## 6. 权威与红线（防跑偏）

- 要求总账：`docs/reviews/ivan-requirements-ledger-2026-07-26.md`（**第六节=07-27 新增 R-成本-01/02、R-架构-06、R-发布-06、R-裁定-06/07，含 Ivan 原话**）。
- Memory：`lidousha-publication-registry-hold-gate`、`lidousha-adjudication-cost-caching`（含「truth-refresh 未实现勿当已完成」警示）。
- 红线速记：registry 是上传唯一授权（review_ready≠可上传）；闭集裁决只归 CPA、音频模型只当耳朵（候选盲听写）；自不一致听写无否决权；人耳定版＞机器听写；发布即快照（证据+registry 行必须 commit）；报 Ivan 的时间用北京时间。
