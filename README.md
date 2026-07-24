# vtuber-slice

李豆沙直播的 remote-first、fail-closed 自动切片流水线。产品目标是无人值守地完成召回、
选片、边界、字幕、标题、封面、打包与发布门。

## 当前权威入口

[docs/pipeline/README.md](docs/pipeline/README.md) 是当前流水线规则的唯一入口；进行到某一步
时，只读对应 step 文件及其指向的代码、schema、profile 或资产。本 README、skills、
workflows、日期化报告和 memory 都不能覆盖 step。

部署、运行、产物与公开状态必须按相关 step 从 live authority 现场读取。本地 checkout、
旧审片包、历史报告或状态字段不能单独证明当前结果。

## 分步索引

- 源录像：[10-source-recording.md](docs/pipeline/10-source-recording.md)
- 选片与量化：[20-selection.md](docs/pipeline/20-selection.md)
- 边界：[30-boundary.md](docs/pipeline/30-boundary.md)
- 字幕与语义修复：[40-subtitle-text.md](docs/pipeline/40-subtitle-text.md)、
  [41-semantic-repair.md](docs/pipeline/41-semantic-repair.md)
- 歌切：[50-song-lane.md](docs/pipeline/50-song-lane.md)
- 标题与封面：[60-title.md](docs/pipeline/60-title.md)、
  [70-cover.md](docs/pipeline/70-cover.md)
- 打包与发布：[80-package-delivery.md](docs/pipeline/80-package-delivery.md)、
  [90-publish.md](docs/pipeline/90-publish.md)

## 主要入口

- 生产 runner：`scripts/free_session_autoslice.py`
- 单片 producer：`scripts/produce_slice_package.py`
- package auditor：`scripts/audit_lidousha_review_package.py`
- 最终人工复核 receipt builder：`scripts/build_lidousha_final_human_review.py`
- 授权发布器：`scripts/authorized_upload.py`
- 部署器：`scripts/deploy_free_autoslice.sh`
- 活字乱刷：[docs/workflows/huozi-luanshua.md](docs/workflows/huozi-luanshua.md)
- 时效专名：[docs/workflows/timely-term-crawler.md](docs/workflows/timely-term-crawler.md)

## 本地验证

```bash
uvx --from ruff==0.15.21 ruff check src scripts
python3 -m compileall -q src scripts tests
PYTHONPATH=. uv run pytest -q
git diff --check
```

这些命令只证明本地源码；部署后仍须核对远端 `DEPLOYED_COMMIT`、运行树 hash、state、产物与公开面。

协作、工作树与交付边界见 [AGENTS.md](AGENTS.md)。
