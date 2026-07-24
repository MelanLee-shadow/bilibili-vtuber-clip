# 活字乱刷：操作导航

> 这是李豆沙历史语音重组的显式触发、no-upload review workflow，不是普通 autoslice 的
> 定时步骤。本文件只提供入口与操作顺序；证据/schema 以当前脚本和项目 skill 为准，片头、
> 打包与发布边界只读对应 pipeline step。

## Authority 与入口

- Agent 操作入口：`.agent/skills/huozi-luanshua/SKILL.md`
- 唯一实现入口：`scripts/huozi_luanshua.py`
- 原始录播与录制完整性：[10-source-recording.md](../pipeline/10-source-recording.md)
- talk 最终烧录、片头与 package：[80-package-delivery.md](../pipeline/80-package-delivery.md)
- 任何后续发布授权：[90-publish.md](../pipeline/90-publish.md)

运行从 `free:/opt/bilive/autoslice/repo` 的 committed runtime、live cache 和原始录播开始；macOS
工作树只作代码、测试与试听 staging。实际路径、子命令、schema、阈值、ASR adapter 与 runtime
media 均从当前部署、脚本 `--help` 和代码读取，不从历史示例推导。

## 高层阶段

1. `history-scan`：在已索引历史字幕中找长连续语段；结果只用于发现。
2. 候选晋升：回到 immutable 原录播，生成当前逐字转写与说话人证据；混合场次只晋升证据
   精确覆盖的李豆沙毫秒范围。
3. `corpus`：从显式 source manifest 构建可规划语料，并绑定媒体、转写与说话人证据。
4. `plan` / `suggest`：先生成原句方案，再评估小幅改句是否得到更自然、较少碎片的方案。
5. `evidence` → `verify` → `render`：只渲染当前验证通过的 plan，并保留逐 piece 来源闭包。
6. `bundle`：把原句与建议句的最终 manifest 绑定为 no-upload 对比包，供 Ivan 试听选择。

完整字段、允许的 authority、失败状态和 subcommand 参数只读
`.agent/skills/huozi-luanshua/SKILL.md`、脚本当前 `--help` 与实现；本导航不复制一套平行合同。

## 操作边界

- 历史粗字幕只召回候选；真正使用的语音必须回到原媒体和当前证据。
- 人工确认、声纹和 trusted range 都只授权其明确覆盖的毫秒范围，不把邻 cue 或上下文自动
  扩权。来源争议先沿 manifest/hash 回溯并替换来源，不靠调 fade 掩盖。
- 每个最终 piece 保留原录播 identity、绝对时间、媒体/evidence hash、转写 authority 与说话人
  authority；验证和渲染阶段重新检查绑定。
- 本 workflow 的 review bundle 保持 no-upload。若用户之后单独要求发布，先按 80 step 用当前
  talk intro roster 生成最终字节，再进入 90 step；本文不固化某个 intro ID、数量、时长或 hash。

## 命令导航

先读取当前帮助：

```bash
python3 scripts/huozi_luanshua.py --help
python3 scripts/huozi_luanshua.py <subcommand> --help
```

典型顺序：

```text
history-scan
→ corpus
→ plan
→ suggest
→ evidence
→ verify
→ render
→ bundle
```

为原句和建议句分别执行 evidence/verify/render；不要从本文复制旧模型列表、固定路径或历史
source range。

## 验证与交付

运行该 workflow 的定向测试和本次改动触及的媒体/package 测试；最终报告提供可点击本地成片
与紧凑来源表。源码/测试 PASS 不证明同一代码已部署，review bundle 也不证明已取得上传权限。
