#!/bin/bash
# 仅新稿上传；同 BV 修复不调用此 wrapper。单条手动上传（部署为 recording-host:/runtime/app/tmp_manual_upload/do_upload.sh）。
# biliup 只跑一次并保留进程 rc；公开完成由 authorized_upload 核验；绝不为取 bvid 重跑。
#
# 禁止裸调。必须经 scripts/authorized_upload.py upload
# （manifest 绑定审过的文件 hash + 幂等账本），它会带 AUTHORIZED_UPLOAD=1。
set -uo pipefail
if [ "${AUTHORIZED_UPLOAD:-}" != "1" ]; then
  echo "REFUSE: direct invocation is disabled." >&2
  echo 'From a source checkout on the deployment host: repo=$(python3 -m src.autoslice.deployed_code_root)' >&2
  echo 'python3 "$repo/scripts/authorized_upload.py" upload --manifest <manifest.json>' >&2
  echo "(make the manifest at review time with: authorized_upload.py make-manifest ...)" >&2
  exit 4
fi
not_dispatched() {
  local rc="$1"
  echo "$2" >&2
  printf 'AUTHORIZED_UPLOAD_RESULT={"schema_version":"authorized-upload-dispatch.v1","dispatch_state":"NOT_DISPATCHED","rc":%s}\n' "$rc"
  exit "$rc"
}
[ "$#" -eq 5 ] || not_dispatched 64 "REFUSE: expected video, cover, title, tags and description"
# Environment seams permit isolated wrapper tests; production defaults remain
# the deployed paths. No fallback or second invocation is allowed.
upload_workdir="${AUTOSLICE_UPLOAD_WORKDIR:-/opt/bilive/app/tmp_manual_upload}"
biliup_binary="${AUTOSLICE_BILIUP_BIN:-/opt/bilive/bin/biliup}"
upload_log="${AUTOSLICE_UPLOAD_LOG:-/tmp/last_upload.log}"
cd "$upload_workdir" || not_dispatched 72 "REFUSE: uploader work directory unavailable"
VIDEO="$1"; COVER="$2"; TITLE="$3"
[ -f "$VIDEO" ] && [ -r "$VIDEO" ] || not_dispatched 66 "REFUSE: video unavailable"
[ -f "$COVER" ] && [ -r "$COVER" ] || not_dispatched 66 "REFUSE: cover unavailable"
[ -f biliup_cookies.json ] && [ -r biliup_cookies.json ] || not_dispatched 66 "REFUSE: uploader cookie file unavailable"
[ -f "$biliup_binary" ] && [ -x "$biliup_binary" ] || not_dispatched 69 "REFUSE: biliup executable unavailable"
: > "$upload_log" || not_dispatched 73 "REFUSE: upload log unavailable"
# $4 = manifest 冻结的完整 tag 行（authorized_upload.py 传入, 含基础位; 上限12
# 已实测）。无 tags 的旧 manifest 回退 AUTOSLICE_FALLBACK_TAGS。
# 频道 tag 兜底是频道身份数据：来自环境变量（见 .env.example），不硬编码。
# 简介不在本脚本：authorized_upload.py 渲染后作为 $5 传入。
TAGS="${4:-${AUTOSLICE_FALLBACK_TAGS:-虚拟主播,直播切片}}"
# $5 = manifest 绑定的简介：authorized_upload.py 由 channel_envelope 渲染（频道行
# + 相关主播行）后传入；本脚本不持有简介的第二份定义。
DESC="$5"
[ -n "$DESC" ] || not_dispatched 64 "REFUSE: description unavailable"
"$biliup_binary" -u biliup_cookies.json upload "$VIDEO" \
  --cover "$COVER" --copyright 2 --source "https://live.bilibili.com/" --tid 27 \
  --title "$TITLE" --tag "$TAGS" \
  --desc "$DESC" --submit app 2>&1 | tee "$upload_log"
rc=${PIPESTATUS[0]}
echo "rc=$rc"
# biliup's ResponseData debug print varies by version: `bvid: String("BV..")`
# vs `"bvid": String("BV..")` (APP submit) — match both.
grep -oE "\"?bvid\"?: String\(\"BV[A-Za-z0-9]+\"\)" "$upload_log" | grep -oE "BV[A-Za-z0-9]+" | head -1 | sed "s/^/BVID=/"
printf 'AUTHORIZED_UPLOAD_RESULT={"schema_version":"authorized-upload-dispatch.v1","dispatch_state":"DISPATCHED","rc":%s}\n' "$rc"
exit $rc
