# 70 封面

本文件是封面步骤的**分步权威**。工艺细节强权威：
`docs/workflows/lidousha-song-finished-package-workflow.md` §5（CPA 路线）+
memory `lidousha-cover-redesign-halfbody` / `cpa-real-ai-cover-always` / `lidousha-cover-no-extra-accessories`。

- 默认 `auto` 路由：只有同时具备强表情/动作证据、可信主播主体几何且全局运动不发散时才保留真实直播帧；游戏运动高分但 `subject_confident=false`，或累计动作热区超过半屏，即使局部运动块误判为主体，也不能冒充主播名场面，必须走 CPA `gpt-image-2 images.edit` 大脸重绘。真实帧不得把整张同场截图直接当背景，必须装入当前 `cover_diversity_slot` 对应的图形海报底板（不同配色、纹理、卡片角度）后再叠梗字；中等且主体可信的帧可先轻修再进入同一底板。任何所选路线失败都 fail-closed，不得用低质随手截帧冒充成品。
- 形象铁律：以当场直播形象为原型，只改动作/表情/Q版；禁加饰品服装；多人场景主体锁定李豆沙；表情永不吐舌头。
- 同场批内创新硬门：selection 为 talk 入选项持久化 `cover_diversity_slot`；前 5 张不得碰撞背景家族。0–5 依次为蓝色漫画爆炸、暖色手账拼贴、紫色霓虹舞台、薄荷贴纸涂鸦、黑白漫画分镜、珊瑚棋盘杂志。返修必须继承该槽位，不能退回独立随机抽色。
- 版式：talk 轮换 left-split/right-split/banner；歌切恒 song-clean 且标题字要大（banner 级）；art direction 由 `_lidousha_cover_art_direction` 决定（`cover_generation.py`）。短梗字会为可读性强制 banner，但背景家族仍必须批内不同。
- 自动 talk 封面按 2026-07-20 生态调研采用 2–12 字的原话/质问/反差梗字，配真实表情帧和更大的脸；完整长标题不是默认封面文案。Ivan 定稿标题仍按人工权威保留其要求的全部成分；歌切恒为 `《歌名》`。
- talk 封面强调字号必须 `>=120px`；渲染低于该线直接报 `COVER_TITLE_TOO_SMALL`，交付包审计也必须阻断。不得用“文件完整/没有裁字”代替缩略图可读性验收；应缩短封面梗字或换更宽版式，禁止继续缩字（2026-07-22 当面对质封面 91px 回归案）。
- 经审阅的封面返修可用 `regenerate_lidousha_cover.py --cover-text` 锁定短梗字；该文案必须由 hash-bound repair plan 提供并逐字验收，不得让返修入口擅自改写。
- 字体：全链验字形 + Noto CJK 兜底 + `glyph_risk` 披露（memory `cover-font-zi-renders-as-bai`，a74520b）。

## 路由证据与审计

- screenshot 与 AI 都是一等路线；人工标题不等于禁用截图，截图 route 也不得因没有短梗字
  静默回退 AI。`auto` 必须落盘 `route + reason_codes + considered evidence`，从最终包可以回答
  “为何选截图/为何选 AI”。
- `screenshot_direct` / `screenshot_polish` 必须有官方源 SHA 绑定的 reference、实际 final cover
  文件与 SHA、逐字 rendered text；指定双人帧还必须匹配 reference override 的 candidate、
  source time、participant IDs 与 required treatment。截图路线不要求、也不得伪造 AI model 证据。
- CPA 路线必须有真实 on-disk AI background/final cover hash、attempted/selected model 与调用证据；
  `model`/`method` 默认字符串或 `ai_cover_generated=true` 不能冒充生图成功。
- 任一路线在最终像素、文字、安全区、人物关系或 route evidence 上失败都 fail closed，不得跨路线
  静默降级。双人联动要求双方在 hash-bound source reference 中真实可见；没有 counterpart
  reference 时禁止凭描述画第二位。
