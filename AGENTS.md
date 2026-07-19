# vtuber-slice Agent Notes

## 流水线分步权威（Ivan 2026-07-19）

- 流水线按步拆分在 `docs/pipeline/`（`README.md` 是指针索引）。**进行到某一步只读该步文件；改某步规则只改该步文件及其指向的代码强制层。** 其他文档（含本文件）对步骤规则只放指针，不复制正文——复制即债。

## Project direction

- The autoslice production source of truth is the committed deployment at `free:/opt/bilive/autoslice/repo` plus its live `state/`, `out/`, and `reports/` directories. The recorder source remains `free:/opt/bilive/app` and the `bilive_record` container path `/app`. The local macOS workspace is source staging/docs/tests plus review mirrors, not the final runtime or artifact store.
- The product goal is unattended automatic slicing. Early-phase manual audit is allowed for validation/forensics, but do not introduce workflows that require Ivan to routinely trim timelines, pick release clips one by one, or babysit uploads.
- Treat generated local media/reports (`lidousha/YYYY-MM-DD/**`, most `reports/**`, `.hermes/**`, `*.mp4`, `*.flv`, `*.m4s`, `*.bak-*`) as disposable unless a task explicitly names them as evidence.
- Upload/publish paths must fail closed: no `AUTO_UPLOAD` manifest + artifact hash gate means no publish.
- Candidates are content anchors, not final clip boundaries; source-context evidence and boundary resolver must decide final start/end.

## Project-local skills

- For 李豆沙 “活字乱刷” historical-speech reconstruction, source repair, guest-speaker exclusion, suggestion variants, and no-upload试听交付, use `.agent/skills/huozi-luanshua/SKILL.md`.
- For song lyric subtitle timing in this repository, use the project-local skill at `.agent/skills/song-lyrics-timeline-aligner/SKILL.md`.
- Do not rely on a personal/global copy of that skill. The intended workflow is project-specific: external timed lyric source, clip-local first/last lyric anchors, global shift first, tail verification, and only explicit evidence-based stretch.
- 歌切标题铁律（Ivan 2026-07-14 定、2026-07-19 重申并全局落地）：固定目录式 `【李豆沙】豆沙歌，《歌名》`，《歌名》前后不加任何字——禁止 `｜副标题`、hook 尾巴、「直播间唱」类衬词。schema 校验（`channel_profile.py`）、choke-point 规范化（`title_policy.canonicalize_song_catalog_title`）与 song lane canonical override 三层强制；不要在任何 prompt/资产/文档里再引入 hook 式歌切标题指导。
- When a song upload title changes, update the matching cover text before considering the edit complete. Song cover text is the title minus the `【李豆沙】豆沙歌，` prefix (i.e. `《歌名》`), rendered big (banner) per the cover skill.

## Unattended runner (2026-07-06)

- Post-stream automation lives in `scripts/free_session_autoslice.py`, deployed at `free:/opt/bilive/autoslice/` (cron */10, flock). It auto-produces top-5 talk clips + up to 1 song clip (ranked by danmaku volume) after each stream ends; songs already published on the channel are skipped by exact normalized title/explicit-alias match against the committed historical snapshot plus successful production upload ledger. Kill switch: `touch /opt/bilive/autoslice/DISABLED`. It has no upload path; publishing stays a separate Ivan-authorized step (and anything published must be committed — see memory `authorized-upload-must-commit`).
- Mandatory branding intro (Ivan 2026-07-18; song exemption Ivan 2026-07-14 `cf09597`): every delivered **talk-lane** clip — talk, 活字乱刷, frozen-resume and subtitle-correction redeliveries — must start with Z1「李豆沙一直是零，不对，李豆沙一直是为爱做一」; **song deliveries ship WITHOUT the intro**（歌切一律不加片头直接进歌，selector 唯一入口按政策忽略 intro manifest）. In the intro, “不对” keeps the original full frame; the first and third clauses use the enlarged Li Dousha region at 1080p with the ad cropped out. The committed switch/binding is `assets/lidousha/intro/branding_intro.v1.json` (`intro_id=huozi-lidousha-shiling-budui-weiaizuoyi-z1-v2`); the exact media is `free:/opt/bilive/autoslice/assets/intro/lidousha-branding-intro.z1-budui-20260718.mp4` (SHA-256 `bbd0c7e3b34d3d5af543bb8444861ab1e18f835c9252629480b2ec2fd34e7dc5`, outside the repo tree, deploys must not delete it). `src/autoslice/branding_intro.py` splices it inside the final burn so every downstream sha256 binding freezes the with-intro bytes, and fails closed when the intro is missing/drifted. `AUTOSLICE_BRANDING_INTRO=off` is a test/emergency escape only — never set it in production. Delivered `.srt`/`.ass` sidecars stay on the content timeline; the intro offset is recorded in `burned_preview.branding_intro.intro_offset_ms`.
