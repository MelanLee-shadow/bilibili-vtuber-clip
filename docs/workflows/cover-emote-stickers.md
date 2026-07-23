# 封面表情包替换（emote stickers as cover subject）

2026-07-19 起，封面主体在「当场直播形象人物重绘」之外多了一条强理由通道：官方表情包。

> 本文件只决定 **CPA 重绘路线内部**使用人物参考还是官方表情包，不决定整条封面选择
> screenshot 还是 AI。总路由、最终像素、人物与字形 proof 以
> [70-cover.md](../pipeline/70-cover.md) 为准；下面“默认人物重绘”仅指已经选中
> `cpa_redraw` 后的主体选择。

## 政策（Ivan 拍板）

- **CPA 路线内默认人物重绘**（参考帧当场形象）。表情包只有强理由才可用：切片里李豆沙的反应与某个表情包高度贴合、用它明显比常规人物重绘更传神；拿不准一律人物重绘。
- **replace 与人物重绘互斥**：选了表情包，封面主体就是该表情包，不再画人物；反之亦然。
- **companion（同框）仅限两类强理由**：分身/复数小李梗，或用熊猫类表情包（25 只是熊猫）代画粉丝 kmx。
  - 术语（Ivan 2026-07-19 纠正）：**kmx 是李豆沙粉丝们的名字**，规范读音 kimo熊；「天不熊」永远是误听（term_lexicon 已收录该 ASR 别名→kmx）。kmx **不是吉祥物**——companion 的语义是"切片明显在互动/回应粉丝时，用贴纸代画观众"，不是画一只吉祥物宠物。
- **歌切封面永不使用表情包**（人声证明语境，保持唱歌人物形象）。
- **表情包重绘必须轻度**：保持原姿势/表情/服饰/配件，只做高清化、描边和背景融合，不改设计、不加配饰；成图中表情包与人物重绘同理，必须占据画面主体（沿用 left/right/banner 布局分区）。
- 表情包自带的字（别走好吗/抱抱/贡丸）在封面重绘中**一律省略**——标题叠字仍由本地 overlay 独占，CPA 出图保持零文字。

## 资产拓扑

- **清单（入库，行为指纹的一部分）**：`assets/lidousha/emote_library.v1.json`，注册为 channel profile 可选资产 `emote_library`。25 条：id/label/subject/描述/适用场景/自带文字/`hd_sha256`。
- **媒体（不入库，`assets/emote/` 已 gitignore）**：
  - 小图原件：`assets/emote/NN_李豆沙_<label>.png`（162px，B 站表情包本体）
  - 高清重绘：`assets/emote/hd/NN_李豆沙_<label>.png`（1254px+，AI 重绘，作 CPA 参考图）
- **运行时媒体根**（探测顺序，镜像 intro 的 runtime_media_paths 模式）：`AUTOSLICE_EMOTE_DIR` 环境变量（显式覆盖）→ 清单 `media_root.runtime_roots`（现声明 `/opt/bilive/autoslice/assets/emote`）→ repo 内 `assets/emote`。
- **free 部署**：只需 `rsync -av assets/emote/ free:/opt/bilive/autoslice/assets/emote/`；runtime root 已声明在清单里，**不需要注入任何服务环境变量**。媒体缺失/哈希不符**不会 BLOCK 封面**——自动降级回默认人物重绘并在 `cover_generation.emote` 证据里披露（detail 列出每个探测路径的失败原因）。

## 决策链

1. `load_emote_library`（fail-open：清单缺失/损坏 ⇒ 永远默认重绘）。
2. 艺术指导 judge（luna 通道）在谈话封面 prompt 里看到 25 条清单，输出 `"emote": null | {"id","mode","reason"}`；确定性 baseline 从不选表情包，judge 失败 fail-open 回 baseline。
3. `normalize_emote_choice` 硬校验：id 必须在库、mode ∈ {replace, companion}、reason 必须成句（≥6 字）、歌切一律剥离。
4. staging 解析参考图：replace ⇒ 参考图直接换成 hd 表情包（sha256 必须与清单一致）；companion ⇒ 高清表情包以右下角 inset 合成进参考帧。失败 ⇒ 降级默认重绘 + `FALLBACK_DEFAULT_REDRAW` 证据。
5. `_lidousha_cover_prompt` 按 mode 出分支 prompt；无表情包路径与旧契约**字节一致**（钉死 sha 测试仍有效）。

## 人工点名通道

```bash
python3 scripts/regenerate_lidousha_cover.py \
  --title "【李豆沙】全场都在打call，她当场看傻" \
  --emote 09 --emote-mode replace --emote-reason "…" \
  --out out/cover.png          # replace 模式不需要 --ref/--media
```

companion 模式需要 `--ref` 或 `--media`（要有人物参考帧可合成）。`--emote` 点名失败会硬报错（不静默降级），judge 自动选择才走降级。

## 测试

`tests/test_cover_emote.py`（36 用例）：清单 schema/25 条完整性、本机媒体 sha 抽查（媒体不在则 skip）、loader fail-open、强理由门控矩阵、prompt 三分支、sha 漂移降级、staging 参考图替换/合成。回归：`test_auto_review_shadow_pipeline.py`、`test_channel_profile.py`、`test_free_session_autoslice.py` 全绿。
