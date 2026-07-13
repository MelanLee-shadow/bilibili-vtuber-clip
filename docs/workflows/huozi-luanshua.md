# 活字乱刷：历史语音重组工作流

`scripts/huozi_luanshua.py` 是李豆沙历史直播语音重组的 fail-closed 入口。它接受一句话，优先用尽可能长的自然连续语段覆盖目标；找不到完整覆盖时才退到短语或单字。它也能比较小幅改句后的拼接质量，但不会替用户决定语义是否仍合适。

当前能力是 **no-upload review workflow**，不接发布器，也不由 `free_session_autoslice.py` 的定时任务自动触发。

## 安全边界

- 历史粗字幕只用于发现。`history-scan` 的结果固定为 `DISCOVERY_ONLY_NOT_RENDERABLE`。
- 真正进入语料库的每个语句必须有逐字毫秒时间戳，且规划时就能在两个独立 ASR 结果的相同时间位置找到同一语音。`零 / 〇 / 0` 只在听音证据层视为同音，目标文字仍精确匹配。
- 专名同音异写不能暗改 ASR 原文。若两个逐字 ASR 的读音和时间对齐、其中一份写出正确专名，另一份写成同音字（例如 `豆沙 / 杜莎`），必须另存 hash-bound 人工专名校正；plan 中保留原始两份观察和校正依据。
- 独播场次必须有 Ivan 的独播确认或等价人工权威。联动/混合场次不能整场晋升；只允许完全落入 `trusted_ranges_ms` 的已核验李豆沙语句。
- 人工确认和声纹结论都是**毫秒范围权威**，不是上下文权威。证据文件中的 `confirmed_range_ms`、`verified_range_ms`、`applies_to.previous_source_fragment_ms` 或模型 decision range 必须完整覆盖所选原 utterance；只确认了“小李是”就绝不能把相邻的“零”一起晋升。
- 声纹自动权威至少绑定两个独立证据文件。高置信策略分不是校准概率；短句、重叠、模型分歧和身份不清一律留在 `REVIEW`，不能为凑字强行使用。
- `verify` 会重新打开两份 ASR 证据并重算媒体 SHA-256；`render` 再复核说话人证据 hash。任何漂移都停止。
- 每个交付候选必须能从本地 MP4 hash 反查 render manifest、中间 piece、原录播绝对路径、日期、原句和 core/cut 毫秒。ASR 文本相邻不能证明音频来自同一场直播或同一句话。
- 输出 manifest 固定为 `REVIEW_READY_NO_UPLOAD`、`upload_enabled=false`。

2026-07-12 首个片头的两个负例必须长期保留：

- 两条完整的“我要为爱做零”分别位于 17:18:39 分段的 `1029900–1032560ms`、`1429160–1431020ms`，它们属于同一个非李豆沙/礼墨声纹簇，不能因文字完美匹配而进入李豆沙语料库。
- Ivan 对旧候选的确认只覆盖 `441250–441850ms` 的“小李是”。旧流程错误地把更宽的上下文/ASR 范围晋升为“小李是零”整句。以后证据覆盖门必须让这种 plan 在 corpus 阶段失败，而不是等 Ivan 试听后才发现。

## 阶段

1. **全历史发现**：把 CloudDrive 上已有完整 SRT 镜像到本地磁盘后运行 `history-scan`。扫描可跨相邻 cue，但不跨默认 1500ms 的长停顿，并按最长精确连续片段排序。
2. **候选晋升**：从原录播重新抽取上下文，不复用来源不明的旧中间片段；只对更好的发现候选补跑 BCUT + 剪映逐字转写和说话人复核。失败候选仍留在发现报告，不进入 corpus。
3. **语料构建**：`corpus` 读取显式 source manifest，执行说话人、逐字时间、内容类型和 hash 门。
4. **原句规划**：`plan` 使用动态规划，先最小化片段数，再最小化单字片段，并偏好更长的自然片段。若句尾仍只能使用单字，会在同样通过双 ASR 与说话人门的候选中优先选择源句尾发音或更饱满的自然音节；异常长 token 的奖励封顶，不能靠错误时间轴胜出。
5. **建议句**：`suggest` 只接受编辑距离不超过 4 且确实降低碎片度的候选。报告同时保留原句 plan 和建议句 plan，必须两版都渲染供 Ivan 选择。
6. **证据与渲染**：`evidence` 生成双 ASR + 媒体 hash 观察，`verify` 产生 `READY_TO_RENDER` plan，`render` 会做逐片段响度对齐、120ms 片头留白和 260ms 分句停顿，再输出 MP4、SRT、ASS 和 no-upload manifest。

## Source manifest

独播来源示例：

```json
{
  "schema_version": "huozi-source-manifest.v1",
  "sources": [
    {
      "source_id": "20260710-190017",
      "source_date": "2026-07-10",
      "media_path": "/absolute/source.mp4",
      "asr_json_path": "/absolute/bcut.json",
      "speaker": "lidousha",
      "speaker_authority": "ivan_confirmed_solo_session",
      "speaker_confidence": 1.0,
      "speaker_evidence": [
        {"authority": "ivan", "path": "/absolute/solo-session-authority.md"}
      ],
      "transcript_confidence": 0.99,
      "transcript_authorities": ["bcut", "jianying"],
      "transcript_evidence": [
        {"authority": "bcut", "path": "/absolute/bcut.json", "confidence": 0.99},
        {"authority": "jianying", "path": "/absolute/jianying.json", "confidence": 0.99}
      ],
      "content_kind": "talk"
    }
  ]
}
```

联动来源应写成 `speaker=mixed`，并只提供逐条核验的 `trusted_ranges_ms`；范围外语句自动丢弃。不要在联动场次顶层伪造 `speaker=lidousha`。每个 trusted range 的 speaker evidence 文件还必须声明精确覆盖范围；`trusted_ranges_ms` 写得比证据范围更宽不会扩大权限。

## 命令形状

```bash
# 1. 粗字幕全历史发现；结果不能直接剪
python3 scripts/huozi_luanshua.py history-scan \
  --text '我本来就是零，不对，我要为爱做零' \
  --transcript-root /local/history-srt-mirror \
  --output /tmp/huozi/history-discovery.json

# 2. 从已晋升来源构建高置信逐字语料
python3 scripts/huozi_luanshua.py corpus \
  --sources /tmp/huozi/sources.json \
  --output /tmp/huozi/corpus.json

# 3. 原句 + 小改句比较
python3 scripts/huozi_luanshua.py plan \
  --corpus /tmp/huozi/corpus.json \
  --text '我本来就是零，不对，我要为爱做零' \
  --output /tmp/huozi/original.plan.json
python3 scripts/huozi_luanshua.py suggest \
  --corpus /tmp/huozi/corpus.json \
  --text '我本来就是零，不对，我要为爱做零' \
  --suggestions /tmp/huozi/suggestions.json \
  --output /tmp/huozi/suggestion-report.json

# 4. 每个 plan 分别执行 evidence -> verify -> render
python3 scripts/huozi_luanshua.py evidence \
  --plan /tmp/huozi/original.plan.json \
  --output /tmp/huozi/original.verification.json
python3 scripts/huozi_luanshua.py verify \
  --plan /tmp/huozi/original.plan.json \
  --verification /tmp/huozi/original.verification.json \
  --output /tmp/huozi/original.verified-plan.json
python3 scripts/huozi_luanshua.py render \
  --plan /tmp/huozi/original.verified-plan.json \
  --output /tmp/huozi/original.mp4 \
  --work-dir /tmp/huozi/.render-original

# 两版完成后用 hash 绑定为一个待选择交付包；bundle 仍然不会上传
python3 scripts/huozi_luanshua.py bundle \
  --original-manifest /tmp/huozi/original.manifest.json \
  --suggested-manifest /tmp/huozi/suggested.manifest.json \
  --suggestion-report /tmp/huozi/suggestion-report.json \
  --output /tmp/huozi/comparison.manifest.json

# Ivan 选定后重建同一份 hash-bound manifest；仍然不会上传
python3 scripts/huozi_luanshua.py bundle \
  --original-manifest /tmp/huozi/original.manifest.json \
  --suggested-manifest /tmp/huozi/suggested.manifest.json \
  --suggestion-report /tmp/huozi/suggestion-report.json \
  --select original \
  --output /tmp/huozi/comparison.manifest.json
```

## 首个片头验收目标

- 必交短版：`小李是零，不对，我是为爱做零`。
- 必交长版：`小李本来就是零，不对，我是为爱做零`。
- 必交连续原句版：`豆沙是零，不对，我是为爱做零`；其中 `豆沙是零` 必须整段取自同一个源 utterance，不允许再拆。
- 三版的后半句都必须是“我是为爱做零”，不能擅自回退成“我要”或“我就”。第一句优先用连续“是零”，不允许再把孤立的“零”接到“小李是”后面。
- 可额外给更自然的同义候选，但不能替代上述三个指定版本。每版都要交付 MP4、SRT、ASS、plan、verification 和 render manifest。
- 媒体 QA 至少检查：存在视频/音频流、时长为正、1920×1080、固定 30fps、无解码/DTS 警告、字幕可解析、目标文字按 plan 完整重建、片段与源 hash 一致。成片再跑 BCUT + 剪映作为可懂度 QA；它不能替代每个源片段的双 ASR 证据门。
