# 手动切片机械流水线：操作说明

判断由 CPA 判，直接采用；只在工具失败或输入缺失时停。唯一不放行的是「定不下且影响意思」（`doubt_policy.HOLD_RULE`）：流水线照常做到成片包，只是不封存、不上传。CPA 按全片证据和选定文字判断剩余不确定性是否仍改变句意。

## 步骤与顺序

执行顺序为 1（边界）、0（外片查重）、2–9，10 为上传（需显式授权，默认不跑）。`run --until N` 跑到步骤 N。

| 步骤 | 内容 | 判断者 |
|---|---|---|
| 1 边界 | CPA 判定起止，采用其切点（源段内、时长 30 s–10 min 两项常识检查） | CPA |
| 0 外片查重 | 只有 FULL_DUPLICATE（H62）拦截 | 工具 |
| 2 剪辑 | OCI3 ffmpeg，时长校验 | 工具 |
| 3 字幕 | 疑点检测：BCUT 与剪映不一致、CPA 整片审读、对照表、ASR 漏句与句内另起一段（剪映、整片 MAI/MOSS）、相接处重复、念弹幕。整片 MAI 与 MOSS（MOSS 用 evidence 接口，分人重叠段照样收）每个切片各调用一次、按音频复用，也给第 4 步提供说话人簇。疑点逐级加证人、定下即停（`suspicion.LADDERS`，默认 asr_first）：L1 只对疑点窗口跑 MAI/MOSS 重识别（与整片同家，不重复计票），L2 Gemini 3.8 Low，L3 argon；仍定不下由 CPA 选最可能的写法、标低把握。公开规则亲听为 L1 旁路；导入的手推（`import-subtitles`）就是本步的已定字幕。每个读法只含本句时间里的话：剪映、整片 MAI/MOSS、窗口 MAI/MOSS 的一段跨几句时，按各句现稿文字与时间切开，每句只拿到自己那一截（`evidence_attach.segment_slices`，一处规则）；听写和窗口 ASR 读法开头或结尾与邻句相同的部分，按整片读段（BCUT、剪映、整片 MAI/MOSS）判断是同一段话录了两次才删，真实的重复保留，邻句两侧都没有整片读段就不删（`overlap_join.trim_reading`，U159）；定字提示词要求不把前后句的话并进本句 | CPA |
| 4 说话人 | 联动（两位以上参与者）：标签用 8c1——整片 MOSS 每个簇的 CAM++ 嵌入均值与李豆沙登记声纹算余弦，最高的一簇是李豆沙、其余是嘉宾，每句取重叠最长的簇；MOSS 缺失、无说话人或只有一簇时退回 v8；MAI 簇只作辅助信号。8c1 与 v8 或 MAI 不一致、或引擎显示句内两人的句子交 CPA 按语境复核（调用失败本步停）。声学标签是默认值（`speaker_v8.adopt_review`）：各路声音打平时按语境选一方，打平又拿不准按嘉宾色；其余只有本句里的明确内容证据（自称、自报身份，或只能出自一方、说话人说自己的话，逐字摘出）才改，称呼、提问与回答、前后句、评论口吻、第三人称提到某人都不算。CPA 给出的两段切分只在给出标签的那一路（MOSS）换人时刻切开，两段说话人按同一路声音证据，与语境写的不一致就不切（`speaker_v8.split_at_switch`）。导入的手推用它自己的说话人；单人场默认 uniform host，画面有被看视频（看动画）时读其画面字幕，与 v8 声纹交 CPA 定李豆沙或非李豆沙（`step4_other_voices`）。标出说话人后，发布校验判为孤立单字的句子并进同一说话人、间隔不超过 0.15 s 的相邻句；并不进去的不出字幕（U138，记入 subs/lone_dropped.json）。定义在 `normalize.join_lone_chars`，第 3 步、导入、复审都经过它；说话人未知时不删，等本步 | 工具 |
| 5 烧录 | 片头前置（缺失即停，公开规则规则） | 工具 |
| 6 标题与封面 | CPA 给标题，采用推荐者；违反 公开规则标题禁词（直接、当场、秒+汉字）则顺延。封面从便宜的做起（U138）：先只看图评截图（关键词选中官方表情包时一并评），有合格的就用、不出图；都不合格才出 1 张 CPA 生图抠图；它也不合格才再出 1 张（Q 版贴纸，故事需要场景时为整幅场景）。同一组封面字，CPA 逐张看图给理由，按仓库 `score_candidate` 的分数选；主脸按它所在的源图定位一次（同一张源图同一个框），再按图层几何换算到封面。生图模型与回退按 [70-cover](70-cover.md) 及仓库统一 image-edit 选择器执行，生图与看图调用均记入本步记录 | CPA |
| 7 标签 | 工具内 CPA 判定，直接采用 | CPA |
| 8 成片包 | 组装视频、字幕、封面、标题、简介、标签。成片审查的看点优先使用原始选片 `clip.hook`；缺失时只逐字使用 CPA 已选标题候选的 `chosen.punchline`，并在 PROCESS 和复审上下文标明来源，不回写选片记录；两者均缺时 FMR 仍拒绝 | 工具 |
| 9 复审 | CPA 读一次成片材料，修改直接采用（字幕与说话人同改，只重建烧录、标签与成片包，不再复审；改说话人与第 4 步同一条规则，只认本句明确内容证据，`speaker_v8.content_evidence`）。同一答复可按疑点稳定 id 明确给出文字修改后的 confidence、affects_meaning 和依据；文字不变也可判断，缺失或无效不默认解除 hold。只有成员等级改变时重建包及复审产物，不重烧或重算标签。材料含封面字和选中图片的既有 CPA 判读，文字复审未独立看图。已有旧主终态缺少该字段且仍有 hold 时，只补一次针对指定遗留疑点的处置；主答复已含字段（包括空列表）不补问。已有主或 focused 终态从原 cache 复用，缺失或损坏则停下，不重发；遗留处置不冒称全片重新复审。仍「定不下且影响意思」的句子不封存，其余 attest 并封存 | CPA |
| 10 上传 | `--upload --quote <公开规则原话>` 才执行；按片事实选首发/换视频/换封面 | 授权 |

## 状态与续跑

每条切片一个状态文件 `state/<cid>.json`（步骤状态、产物路径、CPA 调用、短说明），以及 `clips/<cid>/PROCESS.md`（每步做了什么、CPA 怎么判、采用什么）。续跑只看产物：步骤记为完成且产物都在，就跳过。要重做某一步，删掉它的产物。

## 命令

- `init --task-dir T --cid C --selection S --session E --mac-bcut-srt B [--bcut-source-zero-ms M]`：建状态文件。`M` 是 BCUT 原文件零点对应的源录像毫秒；缺省 0，原文件字节不变。
- `compile --task-dir T --cid C --session E --theme 主题 --recording 录像名=原始BCUT.srt[@ZERO_MS] [--recording …] [--source-dir D] [--remote-root R] [--on-media-host]`：同场主题合集（规则见 [20-selection](20-selection.md)）。`--recording` 按录像先后重复给出，原始 BCUT 为该录像整场字幕，`@ZERO_MS` 同 `init --bcut-source-zero-ms`（该 BCUT 零点对应的录像毫秒，按场次累计时钟的为负数，缺省 0）；`--source-dir` 是录像在媒体主机的目录（缺省 `<会话工作目录>/sources`），`--remote-root` 是工作目录（拼接的虚拟来源写到其 `sources/`）。一次 CPA 选段后，成合集则裁切、拼接、对拼接后媒体跑 BCUT、重映射弹幕，并建好 `state/<cid>.json`（同 `init`），再 `run`。退出码：0 已建好候选；3 CPA 回答这一场不成合集（不是错误，未剪）；2 机械检查或工具失败（如 `COMPILATION_CUE_UNKNOWN`、`COMPILATION_OVERLAP`）。重跑同一 `--cid` 不会再问 CPA（读 `clips/<cid>/compilation/selection.json` 和缓存）。
- `run --task-dir T --cid C [--until N] [--on-media-host] [--upload --quote "<公开规则原话>"]`：默认使用 SSH；controller 在媒体主机运行时显式选 `--on-media-host`。
- `status --task-dir T --cid C`：步骤表。
- `score --task-dir T --truth J [--cid C] [--tsv F --label L]`：按真值打分（仅评估）。采纳改动的首要指标是 `cer_whole`：整片拼接字错率，真值相邻句首尾重复按整片读段判断是否算一遍（同第 3 步 `overlap_join.join_items`，U159；快照无读段时取 `T/clips`）、所有字幕句都计入（多出、重复算错）；逐句 exact、exact_flat 只看真值句是否出现，看不到插入。`--tsv` 每片一行加合计行。
- `listen-queue --task-dir T`：生成听音页（UNRESOLVED 且影响意思的疑点，音频静音、不自动播放）。
- `listen-import --task-dir T --export <json>`：导入 公开规则的标注（整批校验，锚点不符即拒绝）。
- `import-subtitles --task-dir T --cid C --source-dir D --source 名字 --authority U130 [--decided-by owner]`：手推字幕说话人成第 3 步输入，第 3 步起重跑；`owner`=频道主亲定，机器不改他改的句（据 `subs/baseline.srt`，无则全算），异议默认保留 `resolve-dissent` 可改；同文件只补标记仅重做复审。
- `reselect --task-dir T --cid C --from-counter --authority U103 --quote "..."`：以 CPA 最后一次切点开新候选（不调用 CPA）。
- `supersede --task-dir T --cid C --by N --authority ... --quote ... --reason ...`：仅标记 SUPERSEDED，文件保留。

## 运行位置与提交前必跑

- 主机、部署目录、工作根目录、冻结模型、免费 ASR 限速与检索地址（`search_mcp_url`）只在 profile 的 `profiles/<id>/manual_lane.json` 里写一次（`layout.py` 读；每项可用 `MANUAL_LANE_<KEY>` 覆盖）。代码不写主机路径；会话目录由 `work_root` 和录制日期推出，不设某天的默认值。
- 提交 manual_lane 子树（`src/autoslice/manual_lane`、`assets/manual_lane`、`scripts/manual_lane.py`）的改动前必跑：`PYTHON=python3.14 tools/quality/run_quality.sh --fast`（ruff、jscpd、deptry，对照 `tools/quality/baseline.json`，只拦新增，非零退出不提交），以及 `python3.14 -m pytest tests/manual_lane tests/test_cover_style_rotation.py`、`ruff check`。推送前跑不带 `--fast` 的全量（加 vulture、semgrep、gitleaks）。
