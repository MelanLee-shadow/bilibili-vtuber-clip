# 李豆沙标题与封面文案：skill 接入资产

非歌切执行 [60 标题](../../docs/pipeline/60-title.md) 指向的 lidousha-title-cover skill 第四、五节。
本文件只提供 prompt 内的简短接入说明与历史研究 provenance，不另立相反规则。
具体范围以公开流水线的标题与封面步骤为准。

## 标题

完整最终 SRT 是唯一事实来源；已有对齐并核实的 speaker SRT 时按它核对引语归属。
不要重听音频、调用 ASR，也不要从旧标题、选片摘要或历史研究补事实。
候选数量见 `title_policy.py` 的 `_TITLE_MIN_CANDIDATES` / `_TITLE_MAX_CANDIDATES`；每项写具体取舍理由，再选择一个标题。
正文建议范围见 `title_policy.py` 的 `_TITLE_BODY_SUGGESTED_MIN_LEN` / `_TITLE_BODY_SUGGESTED_MAX_LEN`；
加频道前缀后的共享发布长度边界见 `title_policy.json` 的 `min_length` / `max_length`。
只讲一个中心梗，写出真实情绪和粉丝视角，不强制三拍叙事，不拼成长摘要。
至少保留一句最终字幕里的逐字原话；允许去掉结巴和无意义重复，不许杜撰引语。
人物用 B 站正名，kmx 是粉丝；区分现实、游戏、过去转述与假设，不升级关系、程度或结果。
长度、前缀、括号栈、语法、事实与可读性仍各自验收；字幕有来源不等于标题自动好看。
人工正文及歌切的独立权威保持原范围，不自动重写已冻结标题。

## 封面文案

1–2 个物理行，每行最多 9 个字符；第一行情境，第二行梗字更大。
允许“名字：原话”，冒号不再自动改成换行；不写逗号长句，不带【李豆沙】前缀。
可与投稿标题重复，也可补充原因或反差；具体事实只能由最终字幕支持。
引语逐字核对，非引语改写由 CPA 做语义与防编造审查并保存原始回包。
先定好文字，在 2–4 个封面候选之间保持逐字相同，不能为字号分改字。
情绪贴合时优先考虑官方表情包；人物、源图、图层与最终像素仍要真实核验。
渲染与评分按 [70 封面](../../docs/pipeline/70-cover.md)，本资产不规定固定背景或永远重绘。

## 歌切

歌切仍走 [50](../../docs/pipeline/50-song-lane.md) 和 [60](../../docs/pipeline/60-title.md) 的独立目录式标题，
封面用原规则的《歌名》，不套非歌切候选文案、表情包和新字体规则。

## 编辑方法与 tag consumer 的绑定

这套方法适合比较少量有来源的标题/封面触发点、检查陌生读者能否理解，以及区分主要内容、
必要背景和偶然提及。标题与封面的唯一事实 authority 是完整最终字幕；研究词面
不能机械搬入标题或 tags。只有 source-specific candidate application 同时绑定当前最终
字幕/标题、明确的 `CONFIRMED`/`VERIFIED` entity、来源路径或 URL、digest 与逐字证据时，
作者/作品/人物才进入普通 tag consumer 的候选池。缺少这组绑定时，receipt 将
`candidate_projection.status` 记为 `INAPPLICABLE`，继续使用通用方法并披露缺口；不读取
未部署 reports，不从 identity/spelling-only context、ASR 音近字或自由文本报告猜专名。
