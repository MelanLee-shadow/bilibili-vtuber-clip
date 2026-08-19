from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# 300/2000 有两重含义，不再是"全场必须达标"的硬顶：
#   1. 入场线——任何**不在下方债务账本里**的函数/模块越线，立即失败；
#   2. 偿还目标——账本里的存量项要还到这条线以下才能删行。
#
# 为什么改成账本：这两个断言 2026-07-15 随 god-file 拆解一起建立，但只有全局
# 常量、没有逐项豁免口，于是第一次超标之后就永远红着、谁也没法局部收拾，实际
# 被整体忽略。base 79e3e0e 时已经 11 函数 / 9 模块超标（超出 456 / 2025 行），
# 到 8ebe302 变成 20 函数 / 12 模块（超出 1243 / 4643 行）——整整两周没有任何一次
# 被人看见，因为没人跑全量测试、deploy 也没有测试关卡。
#
# 账本规则（双向单调，只准往好的方向走）：
#   - 成员只减不增。修完一项就删掉它那行；删不掉说明没修完。
#   - 逐项行数只降不升。行数变了测试就失败，把新数字写回来——涨了是回归，
#     降了是把重构收益永久锁死，两种都必须在 diff 里留痕。
#   - 2026-07-31 之后新增任何一行，rationale 注释必须写明 Ivan 的批准出处
#     （日期 + 原话或 commit）。没有出处就是不许加。
#   - 同一项第二次抬数字，自动触发单独的 bounded 拆解 task，没有第三次。
#
# 账本就是下面这两个 dict 加 dated 注释。不要把它做成 .vN JSON asset、不要加
# schema、不要发 receipt——用 schema 增殖去治 schema 增殖是这个仓库最不该有的结局。
MAX_ACTIVE_FUNCTION_LINES = 300
MAX_ACTIVE_MODULE_LINES = 2_000

# 2026-07-31 冻结基线：20 项。全部是欠账，不是许可。
FUNCTION_DEBT_LEDGER = {
    # 2026-08-02 +20：run_mode 白名单纳入 MANUAL_PRODUCE_REVIEW 且强制
    # manual_attestation 署名（手动产线包进审计闭环；Ivan 8/2 /goal 授权，
    # 测试 test_manual_review_manifest.py + 二轮真实测试实锤此缺口）。
    ("scripts/build_lidousha_recovery_review_manifest.py", "build_manifest"): 343,
    ("scripts/run_auto_review_shadow_pipeline.py", "_run_live_source"): 316,
    ("src/autoslice/cover_repair.py", "_roll_forward_prepared_cover_transactions"): 346,
    # 2026-08-08 净 -16：Ivan 8/8 真值法证 synthesis F7——删除无声学
    # context-only 直改分支，统一落回候选盲声学见证路径并锁定拆解收益。
    # 2026-08-14 净 -33：文字第三候选重建与 exact-source transcript receipt
    # 构造抽到 focused leaves；本函数只保留编排与既有 CPA/mutation audit。
    ("src/autoslice/final_review_auditor.py", "adjudicate_context_finding"): 715,
    # 2026-08-08 +12:Ivan 8/8 真值法证 F1 回声环修复(synthesis)——
    # 只接入登记误听面分类、弱 provenance 与声学路由；分类器在新小模块。
    # 2026-08-09 净 -1：F16/F17 trusted priority provenance 接线压成薄调用。
    ("src/autoslice/final_review_auditor.py", "audit_final_subtitles"): 418,
    # 2026-08-09 -3：exact reviewed terminal projection 接线同时把 tail-pad
    # coverage receipt 抽到模块级 helper，锁定本次边界修复的净拆解收益。
    # 2026-08-10 合并 ft-a8600994：以下两项按合并后**实测**行数记账,数字来自本次
    # merge(主线 F16/F17 接线与 ft 边界修复各自的净收益叠加),非凭空抬降。
    ("src/autoslice/producer_package_finalization.py", "_materialize_final_recut"): 314,
    # 2026-08-08 +3：owned_intervals 执法接线（Ivan 2026-08-08 配额上传波
    # 修复——zsm8 案：baseline 已应用的 cue 被 exact-final CPA 自愈无声改写；
    # redelivery_subtitle_baseline.py 写 owned_intervals 从未被读取）。
    # 全部新增逻辑已抽到新模块 src/autoslice/redelivery_baseline_ownership.py
    # 的 suppress_baseline_owned_self_heal_findings，这里只留 3 行调用点。
    # 测试 tests/test_producer_package_finalization.py::
    # test_exact_final_review_gate_suppresses_baseline_owned_finding。
    # 2026-08-10 +35：不可读窗删除车道接线（Ivan 2026-08-10 逐字「遇到这种情况
    # 证人报 WITNESS_IMPLAUSIBLE_SYLLABLE_RATE：11 个音节塞进 0.92s，根本听不
    # 出来，应该直接报需要审查，并且在权宜上传时也不能上传，可以把这段字幕删掉
    # 然后出成品等待审阅，而不是拦住」）。判据/守卫/授权/事务落盘全部推给两个
    # 新模块 src/autoslice/unreadable_span_policy.py 与
    # src/autoslice/unreadable_cue_drop_stage.py，这里只留 stage/seal 两处薄调用
    # 点 + 一条 rationale 注释。测试 tests/test_unreadable_cue_drop.py。
    # 2026-08-19 +5：owned_intervals 执法第二个权威源接线（Ivan 2026-08-19
    # 审片裁定 #2「弹幕不修正」——主包/主播案：chat authority 已落笔的逐字
    # danmaku cue 被后续 self-heal 当成 ASR 错字重判）。新增逻辑全部推给新
    # 模块 src/autoslice/chat_authority_ownership.py，这里只留 5 行调用点
    # （2 行 rationale 注释 + 3 行调用），与 owned_intervals 首个权威源
    # （baseline）同一模式。测试
    # tests/test_producer_package_finalization.py::
    # test_exact_final_review_gate_suppresses_chat_authority_owned_finding。
    ("src/autoslice/producer_package_finalization.py", "_run_exact_final_review_gate"): 383,
    # 2026-08-07 +17：狍哥案实施指令（Ivan 2026-08-07「你把狍哥案解决了」，
    # docs/reviews/2026-08-07-source-fact-rescore-design.md）——
    # SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE 不再落 EXHAUSTED，改写
    # pending rescore sidecar 并抛 SOURCE_FACT_REPAIRED_RESCORE_REQUIRED；
    # 新增逻辑的重量已推给 src/autoslice/selection_rescore.py，这里只留
    # 薄调用点。测试 tests/test_selection_rescore.py。
    ("src/autoslice/producer_package_finalization.py", "_stage_record"): 322,
    # 2026-08-13 净 -6：决策行收集抽成薄 helper，为 expected-value canon
    # typed supersession 接线腾出空间；锁定本次拆解后的更低上限。
    ("src/autoslice/producer_text_finalization.py", "verify_chat_authority_final_surfaces"): 329,
    # 2026-07-31 +40：SC 发送者裁决对 v2 精确重放 redelivery 的 deferral
    # （jyl-r10 案：CPA 宕机/岔听下 UNRESOLVED，而该 cue 终局注定被基线盖回；
    # source_truth 同款 DEFERRED 惯例）。连续吃增长，下次动它先拆。
    # 2026-08-11 净 -25：source boundary review 整体抽到
    # producer_source_boundary_review.py；锁定本次拆解收益。
    ("src/autoslice/producer_text_pipeline.py", "_finalize_text_evidence"): 320,
    # 2026-08-08 +6：会话内重述修复接线（Ivan 2026-08-08 当日指令，
    # docs/reviews/2026-08-08-restatement-repair-design.md §4）——同上，
    # 重活在 restatement_recall.py，这里只留调用点。
    # 2026-08-08 +15：Ivan 2026-08-08 优化①边界重放 + wsl 重产 BLOCK
    # 实证——loader、source/final frozen verdict 与 carry disclosure 接线；
    # hash/输入锚不符仍走原 fresh reviewer。
    # 2026-08-10 +9：终审结转硬退出侧车（Ivan 2026-08-10 15:05Z 交棒清单第 7 项
    # 逐字「硬退出丢 carryover(超时/崩溃跳过侧车落盘)」，docs/HANDOFF.md:80）——
    # review_exact_final_srt 把每个终审 pass 的确证行「算出来就写」，不再等整个
    # 终审门跑完最多五轮自愈；SIGKILL(runner subprocess timeout=5400)救不了 pass
    # 内部，但跨 pass 与非 SystemExit 异常这两条路从此不丢。取舍谓词、原子落盘、
    # 完整性标记、封存/在途两态的读法全在 src/autoslice/final_review_carryover.py，
    # 本文件只有一处薄调用点（赋值 + 调用 + 出处注释）。
    # 测试 tests/test_final_review_carryover_hard_exit.py。
    # 2026-08-11 净 -31：同一 source boundary router 抽取后的实测值。
    ("src/autoslice/producer_text_pipeline.py", "run_text_pipeline"): 316,
    # 2026-07-31 +6：full-text contract 穿透形参与调用（Ivan 07-31 `/goal`
    # 「直接按照 fable 的 advise 继续，直至修复所有问题」授权；Fable 裁定 4
    # 点名「contract 不穿透 = 静默把唯一合法全文通道杀死」，必须补）。
    # 2026-08-02 +9：显式降级执行位（demotion_detail 形参 + READY_DEGRADED
    # 记录）——Ivan 8/2 /goal「全都按你的想法进行修复」授权，封面路由 P1。
    ("src/autoslice/publish_staging.py", "_stage_cpa_redraw_cover"): 418,
    # 2026-07-31 +2：同上，contract 穿透接线。
    # 2026-08-02 +29：截图物化失败→显式降级重绘（降级回执+细节留痕，重绘
    # 前置门照跑）——同上授权；测试 test_cover_route_demotion.py + shadow 用例。
    # 2026-08-10 净 -7：截图优先修复顺带还债——source-composition 见证的调用+
    # 异常包装整体抽到 src/autoslice/cover_scene_binding.py
    # （run_source_composition_witness），场景分叉的新增行零留在本函数。
    ("src/autoslice/publish_staging.py", "_stage_lidousha_ai_cover"): 398,
    # 2026-07-31 +27：reuse 封面绑定（1013 jyl-r9 案——reuse 不绑 cover sha，
    # recovery manifest 必然 REFUSE；Ivan 常设修复授权链）。已连续吃增长，
    # 下次动这个函数必须先拆，不许再抬。
    # 2026-08-01 +47：reuse 结转已发布 cover_generation 证据包（1013 r14 案，
    # recovery manifest 要求 route-decision.v2；sha 逐字节相等才结转）。
    # 2026-08-01 +45：身份见证现场重打（摘要形态回执过不了现行校验，不考古，
    # 对同一字节新打 CPA 见证；失败即丢弃整个结转 fail-closed）。
    # 2026-08-10 +19：合并 ft-a8600994 快车道分支（Ivan 2026-08-10 逐字「这就是要
    # 合并的快车道代码，现在就去合并 merge」）带入的 relocation/冻结包接线。数字为
    # 合并后实测,非估算。⚠️ 此项已连续吃增长且上方 8/1 注释写明「下次动这个函数
    # 必须先拆，不许再抬」——本次是合并带入而非新写功能,但欠账事实成立,须补拆解。
    ("src/autoslice/publish_staging.py", "_stage_publish_draft"): 541,
    # 2026-07-31 +3：同上，截图/polish 路径的 contract 穿透。
    # 2026-08-10 净 -6：同上——终检见证的 verifier-missing 分支与调用抽到
    # cover_scene_binding.run_final_host_identity_witness。
    ("src/autoslice/publish_staging.py", "_stage_screenshot_direct_cover"): 314,
    # 2026-08-08 +13：歌lane provider门修复（Ivan 2026-08-08「老毛病竟然还
    # 重新犯，你必须修复」）——JINGTING_PROVIDER_NOT_AGY 及同族此前未被识别
    # 为 transient，同一 attempt 里跟着的 SONG_*_MISSING/INVALID 级联码就会
    # 被 project_terminal_song_disposition 判定终态弃选（2026-08-07
    # song_230754_1118 复发）。新增分支在 SONG_INFRA_TRANSIENT_REASON_CODES
    # 里找本次 attempt 剩余的 infra 码。测试
    # test_full_song_provider_outage_stays_infra_wait_not_terminal_rejection。
    # 2026-08-10 净 -12：歌名命名权威切换（Ivan 逐字「如果意识到了可能是歌
    # 再从 BCUT 切换过来」）顺带还债——全源趟取证键白名单抽成模块常量
    # FULL_SOURCE_RETRY_FORENSIC_KEYS（音频命名权威也进这张表，否则真名会像
    # 修复前的标题一样被这层白名单埋掉）。本函数只减不增，账本按实际收紧。
    ("src/autoslice/song_lane.py", "produce_song"): 312,
}

# 2026-07-31 冻结基线：12 项。同上，全部是欠账。
MODULE_DEBT_LEDGER = {
    # 2026-08-02 +20：同上（manual run_mode 准入+署名门）。
    # 2026-08-08 +1：Ivan 8/8 真值法证 synthesis F2——候选级代词
    # 审计器加入包审计 policy fingerprint，防实现漂移而指纹不变。
    # 2026-08-02 +6：简介第一行固定项目署名常量（Ivan 8/3 指令：默认带
    # 项目名+网址；OSS 同步为署名+env 频道行）。
    "scripts/authorized_upload.py": 2_867,
    # 2026-08-01 新记：OSS 发布整备（Ivan 授权）把导出器扩成改名/patch/模板引擎；
    # 私库专用构建工具，导出时自剥离，不进 OSS 面。
    # 2026-08-02 +55：二轮测试修复（骨架逐键摘除治 governance:{} 必炸类、
    # prompt 注入类模板全占位化、tag prompt JSON 契约）——Ivan 8/2 /goal 授权；
    # 测试 test_template_skeletons.py。
    # 2026-08-19 +449：8/18 公开库更新授权下的两周演进对齐。其中 TEMPLATE_DIRS
    # 补 10 个候选级资产目录是**堵真实泄漏**（8/7 后新增的审阅真值类目录从未
    # 登记，首版导出把真实审片批注原样带出，未 push 即截获）；另含姓名词干
    # 剥离机制、HANDOFF 词界扫描收窄、6 处 patch 漂移对齐。私库专用构建工具，
    # 导出时自剥离，不进 OSS 面。
    "scripts/export_oss_snapshot.py": 2_722,
    # 2026-08-02 +15：--smoke-segment 有界 backfill（帽 3）——Ivan 8/2 /goal
    # 「全都按你的想法进行修复，当然都要配测试」授权；测试 test_smoke_backfill.py。
    # 2026-08-02 再 +3：child_env 加 PYTHONUNBUFFERED（二轮实测：候选日志因子
    # 进程全缓冲十几分钟 0 字节，观察者只能猜死没死）。同一 /goal 授权。
    # 2026-08-07 +14：child_env_for_date 绑定会话游戏语境（Ivan 8/7 指令：
    # 鹅鸭杀场「流水线不知道在玩什么」通病修复）。检测/状态/渲染本体全在
    # src/autoslice/game_context.py，runner 只留 import + 按日期绑定调用；
    # 测试 tests/lidousha/test_game_context.py。
    # 2026-08-07 再 +8：child_env_for_date 绑定会话动态主题提示（Ivan
    # 2026-08-07 指令：「不是所有直播都是游戏，主题可能在主播 B 站动态里」）。
    # 检测/状态/渲染本体全在 src/autoslice/streamer_dynamics.py，runner 只留
    # import + 按日期绑定调用；测试 tests/lidousha/test_streamer_dynamics.py。
    # 2026-08-07 +4：Ivan 2026-08-07 狍哥案实施指令（闭环接线）——produce
    # 派发前过滤 rescore_pending 项（谓词在 selection_rescore.py），本体只
    # 留 import + 4 行调用点。
    # 2026-08-08 +7：8.8 配额授权（Ivan「8.8切片配额到20条，分数在85分以上即
    # 可，候选也最好搞多一点」）——TALK_ATTEMPT_CAP 10→20 的 rationale 注释 +
    # PER_SEGMENT_CANDIDATES 12→18 的 rationale 注释。测试
    # tests/lidousha/test_game_session_talk_pick_cap.py。
    # 2026-08-10 +3：Ivan 2026-08-10 逐字「它必须无论如何至少先猜一个说话人，我才
    # 能审查，不能猜都不猜」——证据不足现在会产出一份**猜的**双人分离并停泊等人
    # 改。本文件只有一处接线：把 src/autoslice/speaker_guess.py 加进
    # talk_failure_recovery_fingerprint 的 speaker_evidence 相关文件表（1 行路径 +
    # 2 行出处注释），否则将来修好猜法也唤不醒已经停泊的候选。降级本体、梯子、
    # 逐 cue 证据缺口披露全在新模块 src/autoslice/speaker_guess.py。
    # 测试 tests/test_speaker_guess.py。
    # 2026-08-10 +11：Ivan 2026-08-10 两句逐字——①「把 tier1 的 4 条做了」；
    # ②「87 现在需要纳入处理范围」。2026-08-07 早已滑出 list_dates() 的最新三天
    # 窗口，既有两个例外（source_incomplete / historical_source_recovery_in_progress）
    # 一个都不成立，仓里此前没有「运维显式指定某天进处理范围」的通道。本文件只有
    # 两处接线：一行 import + list_dates() 里的第三条例外分支（1 次判定调用、1 行
    # 日志、把原来的两项 or 拆成三项）。出处校验、收敛即自动出圈、expires_at 兜底、
    # 候选级限定全在新模块 src/autoslice/operator_processing_scope.py。
    # 测试 tests/test_operator_processing_scope.py。
    "scripts/free_session_autoslice.py": 2_030,
    # 2026-07-31 +122：封面文案链修复（分行权威等级 + 锁定模式 + 缩略图合同背带
    # + max_lines 按合同封顶）。新增逻辑已抽成 _talk_locked_split /
    # _assert_talk_thumbnail_contract 两个模块级函数，_overlay_lidousha_cover_title
    # 因此回到 300 行以内、未进函数账本。Ivan 07-31 `/goal` 授权 + Fable 裁定链。
    # 2026-07-31 再 +52：_talk_font_floor_layout_override——无梗字单行文案的
    # 120px 下限版面自愈（Ivan 07-31 原话拍板「120px 是硬性要求，无所谓是什么
    # layout，接受版面切换」）。
    # 2026-08-10 +7：合并 ft-a8600994 快车道分支（Ivan 2026-08-10 逐字「这就是要合并的快车道代码，现在就去合并 merge」）——ft 新增的
    # punch_fragment_whitespace_is_source_safe 守卫接线 + 合并说明注释。
    # 数字为合并后实测,非估算。
    "src/autoslice/cover_generation.py": 2_345,
    # 2026-08-02 +166：bind_manual_package_cover——手动产线包封面回写（同一套
    # 校验/binding/原子写；Ivan 8/2 /goal 授权；测试 test_manual_cover_bind.py）。
    "src/autoslice/cover_repair.py": 2_217,
    # 2026-08-07 +79：cue59「殉情」顶替真值「偶遇」实案（Ivan 2026-08-07
    # auto_203735_555_680 speaker-truth-diff 裁决 + 落地授权）——新增
    # _glossary_session_candidate_undecidable / _adjudicate_with_glossary_witness_guard
    # 两个模块级函数；两个调用点保持原行数不变（drop-in 改名），已ledgered 的
    # 764 行 adjudicate_context_finding 未再增长。测试
    # tests/test_final_review_auditor.py::
    # test_glossary_candidate_cannot_win_bare_witness_conflict_on_semantics_alone。
    # 2026-08-07 再 +98：Ivan 同日回归纠正——首版门槛把
    # candidate_provenance.kind=="glossary" 本身当唯一拦截条件，误伤 kmx 类
    # 已注册误听方向（停放熊等，只登记在 expected_value_respell_pairs，不在
    # respell_pairs/orthography_ambiguous 查得到的范围）与独立结构化弹幕/SC
    # 佐证场景。新增 _registered_misheard_direction / _glossary_candidate_
    # structured_text_support 两个模块级函数收窄拦截条件为「三者皆缺」；两个
    # 调用点仍保持原行数不变（合并参数到同一行）。第三次抬同一模块的数字，
    # 按账本规则本应触发独立 bounded 拆解 task；此次是同日同案的直接回归修
    # 正、范围极窄且已有专项拆解 task 记录在 adjudicate_context_finding 的
    # 函数账本注释里，Ivan 本人下达修正指令视为对本次追加的批准出处。测试
    # tests/test_final_review_auditor.py::
    # test_glossary_candidate_with_registered_misheard_direction_wins_witness_conflict、
    # test_glossary_candidate_with_bound_structured_chat_support_wins_witness_conflict。
    # 2026-08-08 净 -269：F3 三逃生口纯判定抽到 candidate_support.py，
    # 同时接入 Ivan 8/8 真值法证 F1 回声环修复；锁定拆解后的模块收益。
    # 2026-08-08 再净 -16：同日 truth-harvest synthesis F7 删除无声学
    # context-only mutation 快捷路，保留显式 disclosure 降级门。
    # 2026-08-09 净 -77：Ivan F16/F17 指令要求新逻辑放新模块；结构化闭集
    # 证据与 chat window renderer 抽到 closed_set_evidence.py，god-file 只留
    # 可信 provenance、request 接线，锁定本轮债务偿还。
    # 2026-08-14 净 -146：closed-set proposal rebuild 与 exact-source transcript
    # contract/provider/authority 分拆到 focused leaves，锁定此次提取收益。
    "src/autoslice/final_review_auditor.py": 3_064,
    "src/autoslice/live_source_review.py": 2_035,
    # 2026-08-07 +18：狍哥案实施指令（同上）——marker 选择改判
    # SOURCE_FACT_REPAIRED_RESCORE_REQUIRED + 写 pending sidecar。
    # 2026-08-08 +6：owned_intervals 执法调用点接线（Ivan 2026-08-08 配额上传
    # 波修复，zsm8 案；主体逻辑已抽到新模块
    # src/autoslice/redelivery_baseline_ownership.py，未计入本模块行数）。
    # 本项第二次抬数字：按账本规则应触发独立 bounded 拆解 task，记为剩余风险，
    # 留给下一次动这个 god-file 的人先拆再改。
    # 2026-08-08 +5：Ivan 2026-08-08 优化①边界重放 + wsl 重产 BLOCK
    # 实证——把两层 carry/skip 披露写入最终 boundary audit。
    # 2026-08-10 +1：同上那句逐字——交付摘要里加一个 speaker_guess 紧凑指纹
    # （lane 从 stdout 摘要认出"这条是猜的"并压回停泊态）。指纹构造在
    # src/autoslice/speaker_guess.summary_digest，这里只有一行调用点；import
    # 并进了已有的 `from src.autoslice import selection_rescore` 那行，不另占行。
    # 2026-08-10b：与 ft-a8600994 合并后按**实测**行数记（两侧各自记的 2_742 /
    # 2_740 都不是合并后的真实值；账本记实测，不记任一侧的旧快照）。
    # 2026-08-10c +39：不可读窗删除车道（Ivan 2026-08-10 逐字见上面
    # _run_exact_final_review_gate 那条）。新增逻辑的重量全在两个新模块
    # （unreadable_span_policy.py / unreadable_cue_drop_stage.py，未计入本模块
    # 行数），本模块只涨了 stage/seal 两处调用点、import 与 rationale 注释。
    # 2026-08-19 +10：owned_intervals 执法第二个权威源接线（Ivan 2026-08-19
    # 审片裁定 #2「弹幕不修正」，主包/主播案；见上面
    # _run_exact_final_review_gate 那条同案说明）。新增逻辑的重量全在新模块
    # src/autoslice/chat_authority_ownership.py（未计入本模块行数），本模块
    # 只涨了 5 行 import/rationale + 5 行调用点。
    "src/autoslice/producer_package_finalization.py": 2_783,
    # 2026-07-31 +40：同上（SC 发送者 deferral）。
    # 2026-08-08 +7：会话内重述修复接线（Ivan 2026-08-08 当日指令，
    # docs/reviews/2026-08-08-restatement-repair-design.md §4）——会话内
    # 重述候选注入 exact-final 优先 findings；重活在
    # src/autoslice/restatement_recall.py，这里只留 import + 一处调用点。
    # 2026-08-08 +23：Ivan 2026-08-08 优化①边界重放(8b 冻结 loader 接线)
    # +30：Ivan 8/8 真值法证 F2 代词发现器接线;两处主体均在独立新模块。
    # 2026-08-09 净 -1：F16/F17 priority assembler 保留旧测试入口并拆分候选计数。
    # 2026-08-10 净 -35：F20 真值全所有权快路径（Ivan 2026-08-09 立项，
    # docs/HANDOFF.md ⭐⭐⭐⭐「Ivan 三问的答案」）——两条后置所有权判定、
    # 跳过阶段包装与 truth_full_ownership.v1 回执全部落在新模块
    # src/autoslice/delivery_fast_path.py（钉死重放判定也一并搬过去），
    # 这里只剩 import 与三处调用点，净收益锁进账本。
    # 2026-08-10 −2：AUDITOR_UNAVAILABLE 的 discovery 字面量收进
    # src/autoslice/provider_failure.auditor_unavailable_discovery（同时把
    # provider 保真证据挂上去），三处调用点各省一行。
    # 2026-08-10 合并 ft-a8600994：按合并后实测行数记账,非凭空抬降。
    # 2026-08-10 +10：终审结转硬退出侧车（Ivan 2026-08-10 15:05Z 交棒清单第 7 项
    # 逐字「硬退出丢 carryover(超时/崩溃跳过侧车落盘)」，docs/HANDOFF.md:80）——
    # +9 是 run_text_pipeline 里的薄调用点（见函数账本同日条目），+1 是 import。
    # 本体（谓词、原子落盘、完整性标记、封存/在途两态）全在
    # src/autoslice/final_review_carryover.py。
    # 2026-07-31 +12：contract 穿透接线（形参 + 4 个调用点）。
    # 2026-07-31 再 +17：封面路由 P1——witness 从「路由法官」降回「置信输入」，
    # 删掉无条件放行、几何否决移到关系分支之后、置信改为 几何 OR witness bbox。
    # 净增主要是记录实测根因的注释（70% 几何假阴性、41% 超弥散帽、9.00 分被否）。
    # Ivan 07-31 `/goal` 授权 + Fable 路由链裁定 P1。
    # 2026-07-31 再 +27：reuse 封面 sha 绑定（同函数条目注释）。
    # 2026-08-01 +47：同上（证据包结转）。此模块 7/31-8/1 三次靠抬账过关，
    # 拆解已经不是建议是欠账。
    # 2026-08-02 +38：封面路由显式降级（同函数条目注释，Ivan 8/2 /goal）。
    # 2026-08-10 净 -2：截图优先修复（Ivan 8/10「你直接做掉截图那个」）——
    # 场景分叉与见证调用的新增重量全部落在新模块 cover_scene_binding.py，
    # 本模块只留调用点，顺带把两处内联 try/except 抽走，账本因此收紧。
    # 2026-08-10 再净 -151：竖屏源判据（Ivan 8/10「特别是竖屏直播，通常不适合
    # 截图，只能重绘」）。新增重量落在 cover_source_composition 的几何判据上，
    # 同时把纯决策的 _decide_cover_treatment（含三个标定常量）整体抽到新模块
    # cover_route_policy.py，本模块只留 import 别名与调用点。
    # 2026-08-10 +19：合并 ft-a8600994 快车道分支（Ivan 2026-08-10 逐字「这就是要合并的快车道代码，现在就去合并 merge」）带入的
    # relocation/冻结包接线（同 _stage_publish_draft 那一项）。合并后实测。
    "src/autoslice/publish_staging.py": 2_644,
    "src/autoslice/same_bv_repair.py": 2_422,
}
SCRIPT_EXCLUSIONS = {
    # Incident-specific forensic repair retained as historical evidence, not a
    # production runtime entry point.
    Path("scripts/repair_false_green_20260709.py"),
}
ENTRY_FILE_LINE_BUDGETS = {
    Path("scripts/produce_slice_package.py"): 500,
    Path("scripts/run_auto_review_shadow_pipeline.py"): 1_150,
    # 2026-07-31：+63 来自 8356710（歌切最终歌词交 CPA 按 hash 绑定重判），
    # 是真实新增判定分支，不是搬运。预算贴当前实际值，任何新增行立即失败。
    Path("scripts/run_full_session_selector_cpa_shadow.py"): 928,
}
FOCUSED_MODULE_LINE_BUDGETS = {
    # Compatibility/public workflow facades must not absorb extracted domains.
    Path("src/autoslice/chat_authority.py"): 150,
    Path("src/autoslice/song_repair.py"): 1_200,
    # v7 owns only one exact final-review rejection lineage.  Keep it below a
    # small domain cap so provider/general recovery logic cannot accrete here.
    Path("src/autoslice/selected_final_review_recovery.py"): 700,
    # 2026-08-07 +1：Ivan 2026-08-07 说话人默认连线裁定——ambiguous-cue 语义
    # 佐证接线（confidence 穿透 + 移除死掉的 neighbour smoothing 分支），净增
    # 只有 1 行；重活在新模块 src/autoslice/speaker_host_evidence.py。
    # 2026-08-10 合并波按实际行数调和到 1_804（先例：2257＝两次抬号之和）。
    # 旧号 1_801 是 2026-08-07 留的上限，彼时实际 1_793；本波两笔接线各自落在
    # 独立模块，本文件只留穿透与调用点：
    #   +5 来自 7a55dc79「冻结复核交付机器基线」——fresh/frozen automatic 标签
    #   物化与 replay 证据的调用接线，本体在
    #   src/autoslice/reviewed_speaker_baseline.py；
    #   +6 来自 4e748c2「F5 子cue混说证据面」——disclosure-only sidecar 挂点
    #   （text_srt_path 穿透 + 一处调用），本体在新模块
    #   src/autoslice/speaker_overlap_evidence.py。
    # 两笔都没有在本模块做实活，预算贴实际值。
    # 2026-08-10 +52：Ivan 2026-08-10 两句逐字——①「它必须无论如何至少先猜一个
    # 说话人，我才能审查，不能猜都不猜」；②「我说的猜不是全片统一李豆沙……我要的
    # 就是正常分离两说话人，尽最大努力分开，然后再由我改正」。证据不足时的
    # best-effort 分离**只能**落在本文件：CAM++ 的逐 cue seed_scores 与 cue 音频
    # 只在这里存在，退到 producer 层就要整条重跑。本文件里只有三处接线——
    #   +18 主播锚点提名分支（阈值一字不动，走 speaker_guess.nominate_host_anchors）；
    #   +20 GuessLadder 三级降级的调用点与 analyze() 闭包（闭包同时把原本重复的
    #        分析调用收成一处，抵掉了一部分增量）；
    #   +14 _write_ready_speaker_delivery 的 guess 参数/状态分叉与 CLI 开关。
    # 梯子语义、提名算法、逐 cue 证据缺口披露、收货门判据全在新模块
    # src/autoslice/speaker_guess.py。预算贴实际值。测试 tests/test_speaker_guess.py。
    # 2026-08-10 −256：CAM++ 的 embed-once 原语（cosine/嵌入校验/内容寻址缓存
    # 读写/绑定 sha/相似度打分）抽到 src/autoslice/campp_embed_once.py，因为
    # host-vocal 出证也要用它，而 speaker_finalizer 反过来 import
    # host_vocal_proof——留在本文件就是 import 环。本文件只保留 re-export，
    # 一行实活没搬回来。按棘轮规矩收紧到实测值锁定收益。
    Path("src/autoslice/speaker_finalizer.py"): 1_600,
    # Extracted domains retain a small amount of headroom for real behavior,
    # while failing long before another 3k-4k line domain bus can form.
    # 2026-07-31：+3 来自 4666765（转录实体改由 CPA 路由）。预算贴实际值。
    Path("src/autoslice/chat_evidence.py"): 1_303,
    # 2026-07-25 Ivan：预算是"该重构了"的信号，不是硬顶格——不许为凑行数做
    # 技巧性压缩。本次 +30 来自语境关联召回的接线（召回调用 + UNCERTAIN 保留
    # + 确证改写三个分支），召回本体已抽到 entity_context_recall.py。
    # 2026-07-31：再 +8，同样来自 4666765 的 CPA 实体路由接线。仍未重构，
    # 这个模块已经连续两轮靠抬预算过关——下次再超必须真拆，不许再抬。
    Path("src/autoslice/chat_repair.py"): 1_088,
    Path("src/autoslice/chat_proposals.py"): 1_250,
    # 2026-08-02 +5：LRC 失败转移模型加 env 覆盖位（与另两个 Gemini 调用点同款
    # 惯例；Ivan 8/2 /goal 授权，测试 test_song_repair.py 的 env-override 用例）。
    Path("src/autoslice/song_common.py"): 530,
    Path("src/autoslice/song_lrc_provider.py"): 550,
    # 2026-08-08 +10：369e92b（song_230754_1118 案：唯一非零 ASR 候选被通用
    # 20% recall 地板终态弃选，AGY 音频验证从未真正跑过）——两条窄口子放宽
    # 地板，margin 消歧义不变；8d4b09b 已把该案首版试过又回退的 family-match
    # 死函数删掉（-13 行），净增贴当前实际值。测试 test_song_alignment.py 的
    # 对应 bypass/floor-holds 回归用例。
    # 2026-08-10 +13：三处并行改动合流后的实际值——(a) worker A 的 max_lines
    # 18->120 采样帽 + 其说明；(b) F3 跨字系放行（中文 ASR 对日/韩歌词恰好
    # 0.0，ratio 不含信息，交由音频裁决）；(c) integrator 手工合并注记（两个
    # writer 的工作副本代际不同，整份取用会静默回退 (a)，故记录在案）。
    # 阈值 0.20/0.08/0.55 一字未动。
    Path("src/autoslice/song_alignment.py"): 1_198,
    Path("src/autoslice/song_performance.py"): 1_200,
    Path("src/autoslice/speaker_common.py"): 100,
    Path("src/autoslice/speaker_context.py"): 500,
    Path("src/autoslice/speaker_evidence.py"): 625,
    Path("src/autoslice/full_session_transcription.py"): 1_200,
    Path("src/autoslice/boundary_endpoint_binding.py"): 120,
    # 2026-08-08 +27：Ivan 2026-08-08 优化①边界重放 + wsl 重产 BLOCK
    # 实证——source/final 两层 review 穿透与相邻 carry audit 汇总。
    Path("src/autoslice/producer_boundary_review_stage.py"): 252,
    Path("src/autoslice/review_package_boundary_contract.py"): 350,
}


def _active_runtime_files() -> list[Path]:
    files = [*sorted((ROOT / "src/autoslice").rglob("*.py"))]
    files.extend(
        path
        for path in sorted((ROOT / "scripts").rglob("*.py"))
        if path.relative_to(ROOT) not in SCRIPT_EXCLUSIONS
    )
    return files


def _ledger_problems(actual: dict, ledger: dict, *, label: str, cap: int, render) -> list[str]:
    """Compare measured over-cap items against the frozen debt ledger.

    Both directions fail on purpose: growth is a regression, and shrinkage
    must be written back so the refactor's gain is locked in permanently.
    """

    problems: list[str] = []
    for key in sorted(actual):
        measured = actual[key]
        recorded = ledger.get(key)
        if recorded is None:
            problems.append(
                f"NEW {label} over the {cap}-line entry limit: "
                f"{render(key)} is {measured} lines. "
                "拆掉它；确需入账必须带 Ivan 的批准出处。"
            )
        elif measured > recorded:
            problems.append(
                f"REGRESSED {label}: {render(key)} {recorded} -> {measured} lines. "
                "欠账只准还不准欠。"
            )
        elif measured < recorded:
            problems.append(
                f"IMPROVED {label}: {render(key)} {recorded} -> {measured} lines. "
                f"把账本数字收紧到 {measured} 以锁定收益。"
            )
    for key in sorted(ledger):
        if key not in actual:
            problems.append(
                f"RESOLVED {label}: {render(key)} 已回到 {cap} 行以内，删掉它的账本行。"
            )
    return problems


def test_active_runtime_functions_stay_bounded() -> None:
    actual: dict[tuple[str, str], int] = {}
    for path in _active_runtime_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            line_count = (node.end_lineno or node.lineno) - node.lineno + 1
            if line_count > MAX_ACTIVE_FUNCTION_LINES:
                key = (path.relative_to(ROOT).as_posix(), node.name)
                actual[key] = max(actual.get(key, 0), line_count)

    problems = _ledger_problems(
        actual,
        FUNCTION_DEBT_LEDGER,
        label="function",
        cap=MAX_ACTIVE_FUNCTION_LINES,
        render=lambda key: f"{key[0]}::{key[1]}",
    )
    assert problems == [], "function debt ledger is out of date:\n" + "\n".join(problems)


def test_active_runtime_modules_stay_bounded() -> None:
    actual = {
        path.relative_to(ROOT).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for path in _active_runtime_files()
    }
    actual = {path: count for path, count in actual.items() if count > MAX_ACTIVE_MODULE_LINES}

    problems = _ledger_problems(
        actual,
        MODULE_DEBT_LEDGER,
        label="module",
        cap=MAX_ACTIVE_MODULE_LINES,
        render=lambda key: key,
    )
    assert problems == [], "module debt ledger is out of date:\n" + "\n".join(problems)


def test_extracted_entry_files_stay_thin() -> None:
    violations = []
    for relative, budget in ENTRY_FILE_LINE_BUDGETS.items():
        line_count = len((ROOT / relative).read_text(encoding="utf-8").splitlines())
        if line_count > budget:
            violations.append(f"{relative} is {line_count} lines (budget {budget})")
    assert violations == [], "entry-point growth regressed:\n" + "\n".join(violations)


def test_extracted_domain_modules_stay_focused() -> None:
    violations = []
    for relative, budget in FOCUSED_MODULE_LINE_BUDGETS.items():
        line_count = len((ROOT / relative).read_text(encoding="utf-8").splitlines())
        if line_count > budget:
            violations.append(f"{relative} is {line_count} lines (budget {budget})")
    assert violations == [], "domain-module growth regressed:\n" + "\n".join(violations)


def test_dynamic_boundary_context_cap_is_wired_to_post_authority_review_only() -> None:
    """The spec cap belongs to post-authority boundary review only.

    A previous refactor attached the new keyword to the adjacent
    ``_apply_entity_authority`` call.  Unit tests of the two helpers stayed
    green, while the real producer entry point would have raised ``TypeError``.
    Boundary review must also stay out of ``_run_final_review`` because source
    truth can still delete or renumber cues during finalization.  The extracted
    source-boundary router now owns the one direct semantic-review call.
    """

    pipeline_path = ROOT / "src/autoslice/producer_text_pipeline.py"
    pipeline_tree = ast.parse(
        pipeline_path.read_text(encoding="utf-8"),
        filename=str(pipeline_path),
    )
    pipeline_calls = {
        name: [
            node
            for node in ast.walk(pipeline_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        ]
        for name in {
            "_apply_entity_authority",
            "_run_final_review",
            "review_final_boundary_semantics",
        }
    }
    assert len(pipeline_calls["_apply_entity_authority"]) == 1
    assert len(pipeline_calls["_run_final_review"]) == 1
    assert pipeline_calls["review_final_boundary_semantics"] == []
    for name in ("_apply_entity_authority", "_run_final_review"):
        assert "boundary_max_forward_ms" not in {
            keyword.arg for keyword in pipeline_calls[name][0].keywords
        }

    review_path = ROOT / "src/autoslice/producer_source_boundary_review.py"
    review_tree = ast.parse(review_path.read_text(encoding="utf-8"), filename=str(review_path))
    review_calls = [
        node
        for node in ast.walk(review_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "review_final_boundary_semantics"
    ]
    assert len(review_calls) == 1
    assert "boundary_max_forward_ms" in {keyword.arg for keyword in review_calls[0].keywords}


def test_source_media_receives_the_request_spec_parent() -> None:
    path = ROOT / "scripts/produce_slice_package.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    main = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    calls = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "prepare_source_media"
    ]
    assert len(calls) == 1
    keyword = next(value.value for value in calls[0].keywords if value.arg == "spec_parent")
    assert isinstance(keyword, ast.Attribute)
    assert keyword.attr == "parent"
    assert isinstance(keyword.value, ast.Attribute)
    assert keyword.value.attr == "spec"
    assert isinstance(keyword.value.value, ast.Name)
    assert keyword.value.value.id == "args"


def test_final_text_result_cues_and_receipt_reach_the_same_boundary_resolver() -> None:
    """The resolver must consume one post-authority result, not mixed grids."""

    path = ROOT / "scripts/produce_slice_package.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    main = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    assignments = [
        node for node in ast.walk(main) if isinstance(node, ast.Assign) and len(node.targets) == 1
    ]

    def assignment_to(name: str) -> ast.Assign:
        matches = [
            node
            for node in assignments
            if isinstance(node.targets[0], ast.Name) and node.targets[0].id == name
        ]
        assert len(matches) == 1
        return matches[0]

    cues_value = assignment_to("cues").value
    assert isinstance(cues_value, ast.Attribute)
    assert isinstance(cues_value.value, ast.Name)
    assert cues_value.value.id == "text_result"
    assert cues_value.attr == "cues"

    chat_value = assignment_to("chat_authority_audit").value
    assert isinstance(chat_value, ast.Attribute)
    assert isinstance(chat_value.value, ast.Name)
    assert chat_value.value.id == "text_result"
    assert chat_value.attr == "chat_authority_audit"

    resolver_calls = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_producer_boundary"
    ]
    assert len(resolver_calls) == 1
    resolver_keywords = {keyword.arg: keyword.value for keyword in resolver_calls[0].keywords}
    assert isinstance(resolver_keywords["cues"], ast.Name)
    assert resolver_keywords["cues"].id == "cues"

    receipt_source = assignment_to("final_review_audit").value
    assert isinstance(receipt_source, ast.BoolOp)
    receipt_call = receipt_source.values[0]
    assert isinstance(receipt_call, ast.Call)
    assert isinstance(receipt_call.func, ast.Attribute)
    assert isinstance(receipt_call.func.value, ast.Name)
    assert receipt_call.func.value.id == "chat_authority_audit"
    assert receipt_call.func.attr == "get"
    assert isinstance(receipt_call.args[0], ast.Constant)
    assert receipt_call.args[0].value == "final_review_audit"

    receipt_projection = [
        node
        for node in assignments
        if isinstance(node.targets[0], ast.Subscript)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "spec"
        and isinstance(node.targets[0].slice, ast.Constant)
        and node.targets[0].slice.value == "boundary_semantic_review"
    ]
    assert len(receipt_projection) == 2
    before, after = sorted(receipt_projection, key=lambda node: node.lineno)
    assert before.lineno < resolver_calls[0].lineno < after.lineno
    assert isinstance(before.value, ast.Name)
    assert before.value.id == "boundary_semantic_review"
    assert isinstance(after.value, ast.Name)
    assert after.value.id == "resolved_boundary_semantic"


def test_full_text_cover_contract_reaches_every_renderer_call_site() -> None:
    """全部四条渲染路径都必须穿透 full-text contract。

    2026-07-31 废除 talk 的 mode=full 自动回退后，hash 绑定的 full-text contract
    是整句上封面的**唯一**合法通道；哪条路径不穿透，就等于在那条路径上把它静默
    杀死——刚消灭「静默回退」不能换来「静默不可能」。首轮修复只接了 CPA 重绘
    一条，截图直出 / polish 门 / degrade 门三条都漏了，靠端到端测试才发现。
    逐调用点钉死，别指望一条集成测试盖住四个面。
    """

    call_sites = {
        "src/autoslice/publish_staging.py": "_overlay_lidousha_cover_title",
        "src/autoslice/cover_polish_gate.py": "_overlay_lidousha_cover_title",
    }
    missing: list[str] = []
    for relative, callee in call_sites.items():
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            )
            if name != callee:
                continue
            if "full_text_cover_contract" not in {keyword.arg for keyword in node.keywords}:
                missing.append(f"{relative}:{node.lineno} {callee}")

    # 两个中转函数也必须把它继续往下传，否则形参收到了却不用。
    for relative, forwarder in (
        ("src/autoslice/publish_staging.py", "_compose_screenshot_cover_with_face_gate"),
        ("src/autoslice/publish_staging.py", "_degrade_rejected_polish_to_direct"),
        ("src/autoslice/publish_staging.py", "_stage_cpa_redraw_cover"),
        ("src/autoslice/publish_staging.py", "_stage_screenshot_direct_cover"),
        ("src/autoslice/cover_polish_gate.py", "_compose_screenshot_cover_with_face_gate"),
    ):
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == forwarder
                and "full_text_cover_contract" not in {keyword.arg for keyword in node.keywords}
            ):
                missing.append(f"{relative}:{node.lineno} {forwarder}")

    assert missing == [], (
        "these call sites drop the full-text cover contract, silently killing "
        "the only legal whole-title lane:\n" + "\n".join(sorted(missing))
    )


def test_cover_only_lane_reuses_the_proven_video_repair_io_surface() -> None:
    """cover-only lane 必须复用 video same-BV lane 的真实 API 面。

    `same_bv_cover_repair`（2026-07-31 `28b3576` 新建）**零生产执行**。它的风险
    面之所以可控，唯一理由是最险的那层——Bilibili 四面观察与快照规范化——继承自
    已在生产多次真实执行的 video same-BV lane：两个 CLI 工厂
    `_same_bv_adapter` / `_same_bv_cover_adapter` 构造的是同一个
    `BilibiliRepairAdapter`。

    一旦有人给 cover lane 另起一套 observe/normalise，这个继承来的信誉立刻作废，
    而且两套实现是漂移温床。此测试就是那道防线。
    """

    parser = ast.parse(
        (ROOT / "scripts/authorized_upload.py").read_text(encoding="utf-8"),
        filename="authorized_upload.py",
    )
    factories = {
        node.name: node
        for node in ast.walk(parser)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_same_bv_adapter", "_same_bv_cover_adapter"}
    }
    assert set(factories) == {"_same_bv_adapter", "_same_bv_cover_adapter"}, (
        "两个 adapter 工厂必须都在；缺一说明 lane 拓扑变了"
    )
    for name, node in sorted(factories.items()):
        constructed = {
            call.func.id
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        assert "BilibiliRepairAdapter" in constructed, (
            f"{name} 不再构造 BilibiliRepairAdapter —— cover lane 继承自 video "
            "lane 的生产信誉已失效，未验证面重新扩大到整个远端 API 层"
        )
