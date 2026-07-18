# 2026-07-14 当日上传全量对抗式审计(成片之后全步骤)

> 历史快照说明：本文的 4288ms/2026-07-12 片头观察只描述 7/14 当日稿件；当前固定谈话片头已于 2026-07-18 换为 Z1「李豆沙一直是零，不对，李豆沙一直是为爱做一」（5754ms）。不得用本文旧时长或旧片头判断未来交付。

> 执行:Claude 审计会话(Ivan 指令:一小时后开工,对成片之后所有步骤对抗式复核,默认前序 agent 可能出错;修正一律编辑原视频,不新传、不删稿)。
> 真值来源:B 站公开面 + 创作中心读回(证据 `reports/lidousha-uploads-20260714/`,每 BV 一份 `*.public_verify.json`),free 上传账本、staging manifest、成品 record.json。
> 审计基线 = 最新权威:publish skill(b318dc1 整合版)+ Ivan 2026-07-14 当日新规(cf09597 歌切无片头;1ac9606 歌切裸标题/专名不拆行/当场参考帧/侄女全语境)。

## 部署缺口(指令第 1 项)

- 开工时 `DEPLOYED_COMMIT=e784d238`(06:09:43Z,并行会话已部署当日全部功能提交,含 cf09597)。本会话补部署 docs+资产提交:**`1ac9606` 已部署**(runner md5 / branding intro / speaker assets 校验全过,DISABLED 未触碰),现 DEPLOYED_COMMIT == main HEAD。部署前本地全量 **1175 passed**。
- 原快照中的未提交 `chat_authority.py`/`entity_audio_verifier.py` 已由并行会话作为 e784d23 提交并部署,无遗留。
- 遗留(非本会话新增):生产 crontab 仍带 `GEMINI_PAID_BACKUP_DEV_EXCEPTION=1`,按 HANDOFF 约定回填收敛后应撤。

## 今日上传全景(北京 7/14,UTC 时刻)

| 批次 | 动作 | 稿件 |
|---|---|---|
| 00:29Z | 新稿尝试(配额 21566 全拒) | 临去彩排(后成为陈旧 manifest 事故源) |
| 00:4x–01:0x | **8 条原地换源**(#2-#9,零配额) | BV1JXNk6vECx/16QNk6KE9W/1JQNk6KEGp/1zQNk6KEUS/1JQNk6KEkh/16QNk6KEZE/1JQNk6KENJ/1rQNk6KELM |
| 05:40Z | 5 条新稿 | 临去彩排(dup,已删)/女性朋友 BV1SGNR6nEGF/养熊猫 BV1uGNR6HEyf/0.6侄女 BV1RPNR6dET9/幸福一家 BV1RPNR6dE7G |
| 05:59–06:00Z | 3 条补发(压轴漏传修正 + Ivan 点名两歌,前提「不要片头,封面标题放大」) | 压轴 BV1MeN967EbE/《暗恋是一个人的事》BV1TeN967EUp/《小幸运》BV18eN967ENy |
| 06:13Z | xinyi 新稿(配额拒 rc=1) | 展示新衣服 → retry timer 09:15Z |
| 07:36Z | 本审计:两歌换源(无片头 canonical) | BV1TeN967EUp/BV18eN967ENy(均已回 state=0) |
| 07:53Z | 本审计:nvxing 封面原子安全重叠编辑 | BV1SGNR6nEGF(-6 修改待审,元数据读回正确) |

## 逐条结论(检查项→证据→结论;checklist 10 项全过=✓)

**8 条换源批(#2-#9)**:全部 state=0、单P(旧P已移除)、tid=21、小李歌切以外全在「小李切片」合集、`is_season_display=true`、tag=4 基础位+内容位(≤12,#3 的 安晚→南町/伊索尔 已按人工裁定重算 ✓)、标题=Ivan 手定(含 #6 直女→侄女 定版)、封面=无头套新版(Ivan 7/14 晨已逐张目检)。→ **全部 ✓,无动作**。

**5+3 新稿**:
- 养熊猫 BV1uGNR6HEyf ✓(片头 PREPENDED、无机器味标题、tag 合口径、萤火虫漫展有字幕依据)。
- 0.6 BV1RPNR6dET9 ✓:标题已按侄女全语境铁律改「我是侄女啊」;封面同步「我是侄女啊」、礼墨/0.6 未拆行、ZCOOL「自」字形正常(两横+上撇)、feed 带内。tag 专名全为正主名(礼墨Sumi/南町/伊索尔/星汐Seki)✓。
- 幸福一家 BV1RPNR6dE7G ✓(Ivan 手定标题一字不改含句号;封面拆行仅普通词「组/成」,不违专名铁律,观察项)。
- 压轴 BV1MeN967EbE ✓(BW+萤火虫双 tag 均有字幕依据 grep 证实;封面帽子=新装自带可脱帽,非凭空加饰品)。
- 女性朋友 BV1SGNR6nEGF:内容/标题/tag(安晚awa 有字幕依据)✓;**封面违规:「大N老师」「李豆沙」拆行**(违 1ac9606 专名不拆行)→ **已修**:v2 无字背景(带完整 CPA 出图证据链)+ 显式 6 行原子安全重叠(122px,hook 单方面,无标点),cover-only 编辑 code 0,读回新封面生效。
- 《暗恋是一个人的事》/《小幸运》:标题已是裸格式 ✓、小李歌唱合集 ✓、封面=裸《歌名》大字 banner(feed 裁剪验证通过,「暗恋」贴边但完整)✓;片头问题见下。

**临去彩排 dup(BV1SGNR6nEbQ)**:state=-100 已删除(删稿有验证码墙,应为 Ivan 手删)→ 已闭环;原 BV1JXNk6vECx 换源+新标题健在 ✓。

## 事故复盘与本审计修正动作

1. **歌切「带片头」疑云(本审计最初红旗,已澄清并规范化)**:两歌交付目录 `record.json` 写着 `PREPENDED 4288ms`,但实测上传件时长 = `main_sha256_before` 内容时长(266.75/260.42s)——**上传件本就无片头**(trio 会话在 cf09597 后重出过),**record sidecar 是陈旧的**(描述 19:58Z 的带片头旧烧录,重出后未再生)。本审计据 record 误判违规,用原 recut+原 ass+原编码参数重烧,产物与 `main_sha256_before` **逐字节一致**(107654db/a9bd4b86),已换源(append→swap,BV/合集/tag 不变),两稿均回 state=0。净效果:线上=管线 QA 过的 canonical 字节、换源 manifest 补齐授权链;内容零变化。**机制缺口(归 produce 线):重出成品必须同步再生 record.json/sidecar,否则证据错位误导审计。**
2. **陈旧 manifest 重复投稿(临去彩排)**:00:29Z 新稿 manifest 因配额搁浅→内容改走换源(00:4x)→05:40Z 配额放开后旧 manifest 被重跑,同内容再投一稿。**账本盲区:换源 append 不写 upload_ledger,按 artifact sha 的幂等查不到"已在线上"。建议:swap/append 也记 ledger 事件,do_upload 按 sha 拒绝任何 ledger 中已成功(含换源)的 artifact。**
3. **05:40 批结果镜像文件 title 与 artifact 错位**(`ivan_20260714_replace_upload_results.json` n=1 标题写压轴、实际投的是临去彩排)——结果文件必须从 manifest 回读标题,不允许独立维护标题列表。
4. **台风/恋爱告急 staged 件滞留带片头旧烧录**(`upload_staging/20260713/11` 等,manifest 冻结的是 PREPENDED 字节)。歌切 timer 已撤但 manifest 还在——**任何未来补传前必须无片头重烧+重做 manifest**(部署后管线自动无片头)。《怎么办》仍缺封面。
5. **desc 系统性多一行 source URL**(biliup APP 接口投稿层拼接;7/13 起所有稿一致,7/4 老稿无)。两行规范内容逐字在、链接正确。**待 Ivan 裁定**:接受为新常态(改 skill 措辞)或修 do_upload;不建议为 17 稿逐条刷编辑重审。
6. **xinyi(09:15Z timer 待发)预检+补齐**:talk 片头 PREPENDED ✓、封面合规(当场新装形象、标题成分全保留,仅行尾一处逗号小瑕疵,观察项)、字幕「眼镜也可以脱」与标题「眼罩」并存均属实(当场两样都展示)。**修正:manifest 原缺 tags(会裸 4 基础位上传)→ 已按管线 `generate_upload_tags`(CPA)+人工过目补 `新衣装,换装,可爱,兽耳,反差萌,搞笑`;/tmp/upload_xinyi.py 已扩展:上传成功后自动等 state=0→挂「小李切片」合集(现查 season id,fallback 9320779)→公开验证→写 `xinyi_postpublish.json` 证据(fail-soft,不影响上传)。**
7. **前瞻(不回改已目检封面)**:c01-c06b 换源批封面共用同一张 7/11 参考帧(含 7/10 条目)——按 1ac9606「参考帧必须取自当场」新规,后续封面线应逐场取帧;Ivan 已目检接受本批,不动。

## 与「歌切暂停」的关系

00:2x/05:59Z 的 manifest 授权原话显示:Ivan 在「歌切全线暂停」后**点名放行**了《暗恋是一个人的事》《小幸运》两首(带前提),压轴为 05:40 批漏传的补发——三条均有明确授权链;02:00Z 的旧歌切补传 timer 未触发且已消失 ✓。

## 收口终态(09:4xZ 复核)

- nvxing BV1SGNR6nEGF:封面编辑过审,**state=0**,线上封面=原子安全新版(d0dd7ee3…)✓。
- 两歌切 BV1TeN967EUp/BV18eN967ENy:换源后稳定 **state=0** ✓。
- xinyi 09:15Z timer 触发但再吃 **21566 配额拒**(rc=1,未消耗配额)。按当日实测滚动 ~24h 窗(10 帽,且今日 8 新稿+1 删稿+2 换源 append 疑似均占窗),已把 timer 重挂 **2026-07-15 05:55Z**(05:40 批出窗后),脚本挪至持久路径 `upload_staging/20260714/xinyi/upload_xinyi.py`(含 tags+postpublish 自动化)。发布成功后 postpublish 证据自动落 staging,下一个会话拉回补 commit 即可。
- **配额计数疑点(记录待证)**:09:15Z 时按"仅新稿"口径 trailing 窗应只有 9 条仍被拒——换源 append 的分P上传可能也计入投稿频率窗。若 7/15 05:55Z 成功而中途无其他动作,即为佐证;编辑元数据(swap 提交/封面/tag)已证不占。

## 证据

`reports/lidousha-uploads-20260714/`:18×`*.public_verify.json`(含换源后终态)、9×staging `*.upload_manifest.json`、2×swap manifest、nvxing 封面编辑结果、当日 ledger 切片、重烧/换源日志。xinyi 发布后其 postpublish 证据由 timer 脚本落 staging,下轮会话拉回补 commit。
