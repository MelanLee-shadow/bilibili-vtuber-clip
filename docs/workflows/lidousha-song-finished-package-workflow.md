# 李豆沙歌切 finished-package 操作导航

> 本文件只提供操作顺序，不是规则权威，也不固定 schema、policy epoch、模型、封面路线或
> 标题正文。当前判据只从 [流水线索引](../pipeline/README.md) 及对应 step 读取。

## 按阶段读取当前权威

| 阶段 | 只读该步 |
|---|---|
| 从候选回到完整歌曲、证明演唱与歌词时间轴 | [50-song-lane.md](../pipeline/50-song-lane.md) |
| 冻结标题 | [60-title.md](../pipeline/60-title.md) |
| 选择并证明封面路线 | [70-cover.md](../pipeline/70-cover.md) |
| 生成最终媒体、字幕、证据闭包与审片包 | [80-package-delivery.md](../pipeline/80-package-delivery.md) |
| Ivan 授权后的上传与公开验收 | [90-publish.md](../pipeline/90-publish.md) |

## 操作顺序

1. 从准确 source、current state/record 与实际 artifact hashes 开始；不要把旧审片包或历史
   `passed` 当成当前输入。
2. 按 song step 完成歌曲身份、边界、演唱者与时间轴证明，再进入 title/cover 两步。
3. 按 package step 从同一组最终字节重建 portable review package，并运行当前 canonical
   auditor；任何版本号、policy fingerprint 与输入闭包都以当次 auditor 输出为准。
4. 无上传授权时停在审片交付；有授权时才按 publish step 生成 manifest、验证、上传并读回
   公开面。不要从日期化报告复制旧 uploader 或修复命令。

## 状态边界

- 本地源码存在、定向测试通过或审片包生成成功，只证明本地能力/产物；都不证明相同代码已
  部署到 `free`。
- `free` 上的文件或任务状态也不证明线上稿件已完成；发布完成只由 publish step 要求的
  live readback 与持久证据判定。
- 因此本流程不保存“当前已部署”“当前已上传”之类易漂移结论；每次操作都重新读取真实面。

## 历史样本

2026-06-29/07-03 的 sample、旧错误码、旧模型名和当时的 `passed` 只用于事故复盘，不是
gold command 或当前 acceptance。历史事实保留在 Git 与日期化 `docs/reviews/` /
`docs/spark/` 中。
