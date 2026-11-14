# 切片流水线：从零搭起来

给第一次搭这条流水线的人（或代理），只写代码实际要求的东西。日常操作见 [操作说明](../docs/pipeline/manual-lane-supervision.md)。

## 1. 两个角色：控制端和媒体主机

- **控制端**：运行 `scripts/manual_lane.py` 的仓库检出。
- **媒体主机**：做剪辑、识别、烧录、看图的机器，控制端用 ssh 别名连它（`manual_lane.json` 的 `remote_host`）；
  可以是同一台机器。加 `--on-media-host` 时两者都在媒体主机上跑。读取位置：`layout.py`、`adapters.py`。

## 2. 媒体主机上要有什么

`manual_lane.json` 只写几个根，其他路径由它们推出：

| 位置 | 内容 |
|---|---|
| `<autoslice_base>/current/repo` | 本仓库的检出（主机端工具从这里读；与控制端同一版本） |
| `<autoslice_base>/venv-main` | 装好 `requirements.txt` 的虚拟环境，主机端工具用它的 `bin/python` |
| `<autoslice_base>/cpa.env` | 模型网关的地址和密钥（见 [凭据说明](../docs/credentials.md)）；`mai.env`、`moss.env` 可选 |
| `<work_root>` | 工作根：每个录像日一个目录，共用资产在 `<work_root>/assets` |
| `<work_root>/assets/intro`、`<work_root>/assets/fonts` | 片头和封面字体（没有片头就在 profile 里关掉片头） |

另外需要：

- `ffmpeg`、`ffprobe`，CJK 字体。
- `remote_user` 能免密 `sudo -n`：需要密钥的命令在 root 子 shell 里读 `*.env` 再运行，控制端从不读取或打印密钥。
- 说话人步骤的声纹工作区（`speaker_v8` 四个路径），按频道登记：新频道要先登记主播的声纹。

## 3. 频道的三个文件

从 `profiles/_template/` 复制到 `profiles/<id>/`，逐项填写（说明见 [profiles/README.md](README.md)）：

1. `profile.json`：主播的名字和自称、说话人标签、直播间号、标题前缀、封面身份描述、资产清单。
2. `manual_lane.json`：第 1、2 节的那些根。
3. `manual_lane_channel.json`：流水线提示词里关于这个频道的几句话，每个字段在模板里有说明。
   拿不准的字段留空，不要编。

再建 `assets/<id>/`：词表、人设、标题风格等（`profile.json` 的 `assets.files` 列出需要哪些）。
用 `AUTOSLICE_PROFILE=<id>` 选频道；它在进程启动时读取。

检查：

```bash
AUTOSLICE_PROFILE=<id> .venv/bin/python scripts/validate_channel_profile.py --profile <id> --config-only
AUTOSLICE_PROFILE=<id> .venv/bin/python -m pytest -q tests/manual_lane/test_channel_neutral.py
```

第二条确认流水线的提示词、规则和代码里没有写死别的频道的名字。

## 4. 录像放在哪

流水线按 `<danmaku_root>/<直播间号>/<日期>/` 找一天的录像（`.flv` / `.mp4`）和同名的弹幕 `.xml`
（`danmaku_host` 是这台存录像的机器的 ssh 别名，可以就是媒体主机）。日期目录名是 `YYYY-MM-DD`。
没有弹幕文件也能做，只是少了弹幕这类证据。

## 5. 做第一条片

```bash
export AUTOSLICE_PROFILE=<id>
.venv/bin/python scripts/manual_lane.py discover --help     # 一天的录像 → 候选 → 建任务
.venv/bin/python scripts/manual_lane.py run --help          # 把一条任务一步步做到成片包
.venv/bin/python scripts/manual_lane.py status --help       # 每一步的状态
```

每条片的目录里有 `PROCESS.md`：每一步做了什么、模型怎么判、采用了什么；某一步停下时，原因写在那里和状态文件里。
停下就读原因，不要重复运行去碰运气。

第 10 步（上传）不在公开版里；`run` 做到第 9 步（复审）为止。

## 6. 搭的过程中卡住了

照这份说明搭不起来——某一步要求一个这里没提到的文件、目录或服务，或仍假定是示例频道——是这份说明或代码的
缺口，请提 issue 并附上那一步写下的原因。
