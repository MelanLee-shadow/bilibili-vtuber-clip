# 切片流水线：从零搭起来

给第一次搭这条流水线的人（或代理），只写代码实际要求的东西。日常操作见 [操作说明](../docs/pipeline/manual-lane-supervision.md)。
照着做卡住了——某一步要求一个这里没提到的文件、目录或服务，或仍假定是示例频道——是这份说明或代码的缺口，请提 issue 并附上那一步写下的原因。

## 1. 两个角色：控制端和媒体主机

- **控制端**：运行 `scripts/manual_lane.py` 的仓库检出。
- **媒体主机**：做剪辑、识别、烧录、看图的机器，控制端用 ssh 别名连它（`manual_lane.json` 的 `remote_host`）；
  可以是同一台机器。加 `--on-media-host` 时两者都在媒体主机上跑。读取位置：`layout.py`、`adapters.py`。

## 2. 媒体主机上要有什么

`manual_lane.json` 只写几个根，其他路径由它们推出：

| 位置 | 内容 |
|---|---|
| `<autoslice_base>/current/repo` | **和控制端内容相同的一份仓库**（主机端工具从这里读）。你的 profile 和资产是未提交的文件，所以用 `rsync -a --exclude=.venv --exclude=.git ./ <主机>:<autoslice_base>/current/repo/` 同步；每次改 profile、资产或代码后重新同步 |
| `<autoslice_base>/venv-main` | 装好 `requirements.txt` 的虚拟环境，主机端工具用它的 `bin/python` |
| `<autoslice_base>/cpa.env` | 模型网关的地址和密钥：`CPA_BASE_URL`、`CPA_API_KEY`。文字、看图、复听用的音频模型都走这个网关（用哪些模型见 `assets/manual_lane/lane_rules.json`）；流水线只读 `<autoslice_base>/` 下的这几个 `*.env`，不读别处的 `.env`。整片识别用的 `mai.env`、`moss.env` 可选。[凭据说明](../docs/credentials.md) 写于早先版本，其中的 `preflight.py`、发布工具、别的 `.env` 路径和模型名以本文为准 |
| `<work_root>` | 工作根：每个录像日一个目录，共用资产在 `<work_root>/assets` |
| `<work_root>/assets/fonts`、`<work_root>/assets/intro` | 封面字体和片头。默认字体文件名 `RuiZiZhenYan.ttf` 不随仓库分发：放一个同名字体，或在 `manual_lane.json` 的 `cover_font` 写另一个字体文件在**媒体主机上的完整路径**（`assets/<id>/fonts/` 里有开源字体，同步后在 `<autoslice_base>/current/repo/assets/<id>/fonts/` 下）。没有片头就在片头资产里关掉 |

最小布置（`venv-main` 和各 `*.env` 可以是指向已有环境和凭据文件的软链）：

```bash
ssh <主机> 'mkdir -p <autoslice_base>/current/repo <work_root>/assets/fonts <work_root>/assets/intro <archive_root>'
rsync -a --exclude=.venv --exclude=.git ./ <主机>:<autoslice_base>/current/repo/      # 先同步，cover_font 指向的字体才在主机上
```

`mai.env`、`moss.env` 缺失时这两家识别记为不可用，流程照常往下走。

另外需要：

- `ffmpeg`、`ffprobe`，CJK 字体。
- **`remote_user` 必须能免密 `sudo -n`。** 需要密钥的命令在 root 子 shell 里读 `*.env` 再运行，控制端从不读取或打印密钥；
  没有免密 sudo 时第一次主机调用就会失败。先在主机上试 `sudo -n true`。
- 声纹工作区（`manual_lane.json` 的 `speaker_v8`）按频道登记。**还没有就四项都留空**：只有主播一个人说话的片不用它；
  有嘉宾或在看视频的片会停在第 4 步并写明缺它。

## 3. 建频道

```bash
python3 scripts/init_channel_profile.py <id> --display-name <主播名> --room <直播间号>
```

它从模板建好 `assets/<id>/` 和 `profiles/<id>/` 下的三个文件，做完所有机械替换，并清掉模板里的示例词条。
`assets/<id>/` 里写着「（待填」的字段是还没填的说明，不是值。然后逐项填写：

1. `profiles/<id>/profile.json`：主播的名字和自称、说话人标签、标题前缀、封面身份描述、资产清单。
   `short_name` 是主播的简称（没有就写全名）；`self_reference_aliases` 只列她真的用来称呼自己的词。
   **每次改完都要登记**：`python3 scripts/archive_channel_profile.py profiles/<id>/profile.json`（不登记，校验会报
   `manifest history not updated`；每次登记在 `profiles/manifest_history/` 多一条记录，这是预期的）。
   `tools.cover_regenerator` 指向早先流水线的一个脚本，不在这一版里：校验输出的 `missing_runtime_paths` 列出它是预期的，做片不需要它。
2. `profiles/<id>/manual_lane.json`：第 1、2 节的那些根，以及录像位置（第 4 节）。`_` 开头的键是说明。
3. `profiles/<id>/manual_lane_channel.json`：流水线提示词里关于这个频道的几句话，每个字段在文件里有说明，
   写法可对照 `profiles/lidousha/manual_lane_channel.json`。拿不准的留空，不要编。
4. `assets/<id>/`：词表、人设、标题风格、封面身份等。模板文件开头是“问频道主人”的清单；其中的示例频道例子换成自己的或删掉。
   - `title_style.md` 里的「## 标题」「## 封面文案」两节会原样进提示词，节标题必须保留。还没有自己手定的标题时，
     这个文件保持模板原样就能跑；【主播名】是写法说明里的占位，不用逐个替换。
   - 外貌（`persona.md`、`cover_identity_prompt.txt`、`profile.json` 的 `cover_identity`）要以真实直播画面为准。
     还没有录像时先按公开资料写并标“待核实”，有了录像再对着画面改。
   - 标题的编辑方法：原文在 `assets/<id>/title_method/`（模板带一份通用写法），流水线读由它生成的
     `assets/<id>/title_editorial_method.json`。改了原文就重新生成：
     `AUTOSLICE_PROFILE=<id> python3 scripts/build_title_editorial_method.py`。
   - `slice_selection_metric.md`：必须换成自己频道的，是「## 优先级分层」里以「第一层」开头的那一条（这个频道什么题材最要紧）。
     文中的 `lidousha_centrality` 是代码里的评分字段名，意思是“和主播本人有多相关”，不要改名。

填 `profile.json` 时：`host_speaker_label` / `guest_speaker_label` 是字幕里主播和其他人的说话人标签（主播的通常就是主播名）；
`speaker_identity_aliases` 是固定的名单：人工标注或导入的说话人记录里，写着这些名字的句子算主播说的（通常是主播名和 profile id）；`cover_identity` 的每一项都不能是空串，没有内容就写“（暂无）”。
模板里标着“真例”“示例”的条目是示例频道的，换成自己的或删掉。校验输出里 `census_clues_status: ABSENT` 是正常的（可选的选片线索文件）。

用 `AUTOSLICE_PROFILE=<id>` 选频道（进程启动时读取）。检查：

```bash
python3 scripts/archive_channel_profile.py profiles/<id>/profile.json
AUTOSLICE_PROFILE=<id> python3 scripts/validate_channel_profile.py --profile <id> --config-only
AUTOSLICE_PROFILE=<id> python3 -m pytest -q tests/manual_lane/test_channel_neutral.py
```

`--config-only` 只查配置能否读通；不带它会连同声纹、名额授权等生产素材一起查，新频道在备齐之前是 `BLOCKED`，这不妨碍做片。
输出里的 `lane_config_unfilled` 列出 `manual_lane.json` 里还是模板占位的键；`notes` 说明哪些缺项是预期的。
整套测试不要设 `AUTOSLICE_PROFILE`；只有上面这一条是带着自己的 profile 跑的。

## 4. 录像放在哪

`manual_lane.json` 的 `archive_root` 是媒体主机上的录像根：`<archive_root>/<直播间号>/<YYYY-MM-DD>/` 下放当天的录像
（`.flv` / `.mp4`）和同名弹幕 `.xml`。`discover` 就看这里：这个房间还没有目录时，输出的 `deferred` 里有一条
`NO_RECORDINGS_YET`，不算失败；`archive_root` 本身不存在则报 `ARCHIVE_ROOT_MISSING` 并停下——路径打错，或者录像器还没录过、
还没把这个目录建出来（先 `mkdir -p` 建好它）。没有录像时整个流程就停在 `discover`，后面的步骤都要等第一场录像。

`danmaku_host` / `danmaku_root` 是**控制端**取弹幕文件用的：一个从控制端能 ssh 到的别名，和那台机器上的同一个录像根。
录像就在媒体主机上时，`danmaku_host` 填媒体主机的别名，`danmaku_root` 填和 `archive_root` 相同的路径。
没有弹幕文件也能做，只是少了弹幕这类证据。

## 5. 做第一条片

```bash
export AUTOSLICE_PROFILE=<id>
python3 scripts/manual_lane.py discover --task-dir <任务目录>            # 一天的录像 → 候选 → 建任务
python3 scripts/manual_lane.py status --task-dir <任务目录>              # 看选出了哪些
python3 scripts/manual_lane.py run --task-dir <任务目录> --cid <片号> --until 9
```

每条片的目录里有 `PROCESS.md`：每一步做了什么、模型怎么判、采用了什么；某一步停下时，原因写在那里和状态文件里。
停下就读原因，不要重复运行去碰运气。第 10 步（上传）不在公开版里。

同仓库的爬虫等工具（`scripts/crawl_*.py`）目前仍按示例频道的资产格式名校验，还没有按频道参数化；切片流水线不依赖它们。
