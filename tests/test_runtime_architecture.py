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
    ("scripts/audit_lidousha_review_package.py", "_audit_item_story_contract"): 316,
    ("scripts/audit_lidousha_review_package.py", "audit_package"): 329,
    ("scripts/build_lidousha_recovery_review_manifest.py", "build_manifest"): 365,
    ("scripts/run_auto_review_shadow_pipeline.py", "_run_live_source"): 316,
    ("src/autoslice/cover_repair.py", "_roll_forward_prepared_cover_transactions"): 346,
    # 764 行，全场最重的一项，已单列 bounded 拆解 task。
    ("src/autoslice/final_review_auditor.py", "adjudicate_context_finding"): 764,
    ("src/autoslice/final_review_auditor.py", "audit_final_subtitles"): 407,
    ("src/autoslice/producer_boundary_resolution.py", "_repair_boundary"): 305,
    ("src/autoslice/producer_package_finalization.py", "_materialize_final_recut"): 320,
    ("src/autoslice/producer_package_finalization.py", "_run_exact_final_review_gate"): 340,
    ("src/autoslice/producer_package_finalization.py", "_stage_record"): 330,
    ("src/autoslice/producer_text_finalization.py", "verify_chat_authority_final_surfaces"): 335,
    ("src/autoslice/producer_text_pipeline.py", "_finalize_text_evidence"): 305,
    ("src/autoslice/producer_text_pipeline.py", "_run_final_review"): 304,
    ("src/autoslice/producer_text_pipeline.py", "run_text_pipeline"): 307,
    # 2026-07-31 +6：full-text contract 穿透形参与调用（Ivan 07-31 `/goal`
    # 「直接按照 fable 的 advise 继续，直至修复所有问题」授权；Fable 裁定 4
    # 点名「contract 不穿透 = 静默把唯一合法全文通道杀死」，必须补）。
    ("src/autoslice/publish_staging.py", "_stage_cpa_redraw_cover"): 415,
    # 2026-07-31 +2：同上，contract 穿透接线。
    ("src/autoslice/publish_staging.py", "_stage_lidousha_ai_cover"): 389,
    # 2026-07-31 +27：reuse 封面绑定（1013 jyl-r9 案——reuse 不绑 cover sha，
    # recovery manifest 必然 REFUSE；Ivan 常设修复授权链）。已连续吃增长，
    # 下次动这个函数必须先拆，不许再抬。
    ("src/autoslice/publish_staging.py", "_stage_publish_draft"): 455,
    # 2026-07-31 +3：同上，截图/polish 路径的 contract 穿透。
    ("src/autoslice/publish_staging.py", "_stage_screenshot_direct_cover"): 322,
    ("src/autoslice/song_lane.py", "produce_song"): 311,
}

# 2026-07-31 冻结基线：12 项。同上，全部是欠账。
MODULE_DEBT_LEDGER = {
    "scripts/audit_lidousha_review_package.py": 2_003,
    "scripts/authorized_upload.py": 2_929,
    "scripts/free_session_autoslice.py": 2_063,
    # 2026-07-31 +122：封面文案链修复（分行权威等级 + 锁定模式 + 缩略图合同背带
    # + max_lines 按合同封顶）。新增逻辑已抽成 _talk_locked_split /
    # _assert_talk_thumbnail_contract 两个模块级函数，_overlay_lidousha_cover_title
    # 因此回到 300 行以内、未进函数账本。Ivan 07-31 `/goal` 授权 + Fable 裁定链。
    # 2026-07-31 再 +52：_talk_font_floor_layout_override——无梗字单行文案的
    # 120px 下限版面自愈（Ivan 07-31 原话拍板「120px 是硬性要求，无所谓是什么
    # layout，接受版面切换」）。
    "src/autoslice/cover_generation.py": 2_338,
    "src/autoslice/cover_repair.py": 2_049,
    "src/autoslice/delivery_recovery.py": 2_078,
    "src/autoslice/final_review_auditor.py": 3_390,
    "src/autoslice/live_source_review.py": 2_035,
    "src/autoslice/producer_package_finalization.py": 2_765,
    "src/autoslice/producer_text_pipeline.py": 2_098,
    # 2026-07-31 +12：contract 穿透接线（形参 + 4 个调用点）。
    # 2026-07-31 再 +17：封面路由 P1——witness 从「路由法官」降回「置信输入」，
    # 删掉无条件放行、几何否决移到关系分支之后、置信改为 几何 OR witness bbox。
    # 净增主要是记录实测根因的注释（70% 几何假阴性、41% 超弥散帽、9.00 分被否）。
    # Ivan 07-31 `/goal` 授权 + Fable 路由链裁定 P1。
    # 2026-07-31 再 +27：reuse 封面 sha 绑定（同函数条目注释）。
    "src/autoslice/publish_staging.py": 2_703,
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
    Path("src/autoslice/speaker_finalizer.py"): 1_800,
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
    Path("src/autoslice/song_common.py"): 525,
    Path("src/autoslice/song_lrc_provider.py"): 550,
    Path("src/autoslice/song_alignment.py"): 1_175,
    Path("src/autoslice/song_performance.py"): 1_200,
    Path("src/autoslice/speaker_common.py"): 100,
    Path("src/autoslice/speaker_context.py"): 500,
    Path("src/autoslice/speaker_evidence.py"): 625,
    Path("src/autoslice/full_session_transcription.py"): 1_200,
    Path("src/autoslice/boundary_endpoint_binding.py"): 120,
    Path("src/autoslice/producer_boundary_review_stage.py"): 225,
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


def _ledger_problems(
    actual: dict, ledger: dict, *, label: str, cap: int, render
) -> list[str]:
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
        path.relative_to(ROOT).as_posix(): len(
            path.read_text(encoding="utf-8").splitlines()
        )
        for path in _active_runtime_files()
    }
    actual = {
        path: count
        for path, count in actual.items()
        if count > MAX_ACTIVE_MODULE_LINES
    }

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
    truth can still delete or renumber cues during finalization.
    """

    path = ROOT / "src/autoslice/producer_text_pipeline.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = {
        node.func.id: {keyword.arg for keyword in node.keywords}
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id
        in {
            "_apply_entity_authority",
            "_run_final_review",
            "review_final_boundary_semantics",
        }
    }

    assert "boundary_max_forward_ms" not in calls["_apply_entity_authority"]
    assert "boundary_max_forward_ms" not in calls["_run_final_review"]
    assert (
        "boundary_max_forward_ms"
        in calls["review_final_boundary_semantics"]
    )


def test_final_text_result_cues_and_receipt_reach_the_same_boundary_resolver() -> None:
    """The resolver must consume one post-authority result, not mixed grids."""

    path = ROOT / "scripts/produce_slice_package.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    assignments = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Assign) and len(node.targets) == 1
    ]

    def assignment_to(name: str) -> ast.Assign:
        matches = [
            node
            for node in assignments
            if isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
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
    resolver_keywords = {
        keyword.arg: keyword.value
        for keyword in resolver_calls[0].keywords
    }
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
                node.func.id
                if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", "")
            )
            if name != callee:
                continue
            if "full_text_cover_contract" not in {
                keyword.arg for keyword in node.keywords
            }:
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
                and "full_text_cover_contract"
                not in {keyword.arg for keyword in node.keywords}
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
