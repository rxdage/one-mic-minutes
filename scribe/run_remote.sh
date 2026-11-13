#!/usr/bin/env bash
# 可选：本机没装环境时，把录音传到一台你自己的 Linux 服务器上跑，跑完取回文字结果，并删掉服务器上的全部文件。
#
#   SCRIBE_HOST=myserver SCRIBE_PY='~/miniforge3/envs/scribe/bin/python' \   （单引号：让 ~ 在服务器上展开）
#     bash scribe/run_remote.sh 录音.m4a 输出目录 [座位.json | 名单.txt | K]
#
# 环境变量：
#   SCRIBE_HOST        必填，ssh 能直接连上的主机名（建议写进 ~/.ssh/config，配好免密登录）
#   SCRIBE_PY          服务器上装了依赖的 python，默认 python3
#   SCRIBE_WORKDIR     服务器上的临时目录，默认 one-mic-minutes-runs（相对 home）
#   SCRIBE_VOICEPRINTS 本机声纹库 npz 路径（可选；存在就带上）
#   SCRIBE_HOTWORDS    本机热词文件（可选）
set -euo pipefail
AUDIO="$1"; OUT="$2"; ARG3="${3:-}"
HOST="${SCRIBE_HOST:?请设置 SCRIBE_HOST}"
PY="${SCRIBE_PY:-python3}"
WD="${SCRIBE_WORKDIR:-one-mic-minutes-runs}"
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="$WD/$(date +%Y%m%d_%H%M%S)"
EXT="${AUDIO##*.}"
SSH="ssh -o BatchMode=yes -o ServerAliveInterval=30"

[ -f "$AUDIO" ] || { echo "找不到录音: $AUDIO"; exit 1; }
mkdir -p "$OUT/_asr"
cleanup_remote() { $SSH "$HOST" "case '$RUN' in '$WD'/2*) rm -rf ~/'$RUN';; esac"; }

echo "== 上传到 $HOST:~/$RUN"
$SSH "$HOST" "mkdir -p ~/'$RUN'"
scp -q "$HERE/pipeline.py" "$HERE/voiceprint.py" "$HOST:$RUN/"
EXTRA=""
if [ -n "${SCRIBE_HOTWORDS:-}" ] && [ -f "$SCRIBE_HOTWORDS" ]; then
  scp -q "$SCRIBE_HOTWORDS" "$HOST:$RUN/hotwords.txt"; EXTRA="$EXTRA --hotwords hotwords.txt"; fi
if [ -n "${SCRIBE_VOICEPRINTS:-}" ] && [ -f "$SCRIBE_VOICEPRINTS" ]; then
  scp -q "$SCRIBE_VOICEPRINTS" "$HOST:$RUN/voiceprints.npz"; EXTRA="$EXTRA --voiceprints voiceprints.npz"; echo "   带上声纹库"; fi
if [ -n "$ARG3" ]; then
  if [ -f "$ARG3" ]; then
    case "$ARG3" in *.json) AT=attendees.json;; *) AT=attendees.txt;; esac
    scp -q "$ARG3" "$HOST:$RUN/$AT"; EXTRA="$EXTRA --attendees $AT"; echo "   带上参会名单"
  else EXTRA="$EXTRA --k $ARG3"; fi
fi
scp -q "$AUDIO" "$HOST:$RUN/input.$EXT"

echo "== 开跑（服务器后台运行，断线不影响）"
$SSH "$HOST" "cd ~/'$RUN' && { setsid nohup $PY pipeline.py input.$EXT out $EXTRA > run.log 2>&1 < /dev/null & echo \$! > pid; }"
$SSH "$HOST" "cd ~/'$RUN'; while kill -0 \$(cat pid) 2>/dev/null; do sleep 20; done; grep -E '^\[' run.log | tail -8"

if $SSH "$HOST" "test -f ~/'$RUN'/out/summary.json"; then
  scp -q "$HOST:$RUN/out/*" "$HOST:$RUN/run.log" "$OUT/_asr/"
  cleanup_remote
  echo "== 完成：结果在 $OUT/_asr/ ，服务器上的运行目录已删除"
else
  echo "== 失败，日志末尾："
  $SSH "$HOST" "tr '\r' '\n' < ~/'$RUN'/run.log | grep -v 'it/s' | tail -30"
  $SSH "$HOST" "rm -f ~/'$RUN'/input.$EXT ~/'$RUN'/out/audio*.wav"
  echo "（服务器上的录音已删，日志留在 $HOST:~/$RUN 供排查）"
  exit 1
fi
