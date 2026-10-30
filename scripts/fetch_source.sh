#!/usr/bin/env bash
# 利用権限のある動画URLを取得する。字幕とチャットは任意。
set -euo pipefail
BASE="$(cd "$(dirname "$0")/.." && pwd)"
URL="${1:?使い方: bash scripts/fetch_source.sh URL [保存ディレクトリ]}"
OUT="${2:-$BASE/data/source}"
command -v yt-dlp >/dev/null || { echo 'yt-dlpが必要です' >&2; exit 1; }
case "$URL" in http://*|https://*) ;; *) echo 'http(s)のURLを指定してください' >&2; exit 1;; esac
mkdir -p "$OUT"
yt-dlp --no-playlist --no-overwrites -f 'bv*[height<=1080]+ba/b[height<=1080]' \
  --merge-output-format mp4 --remux-video mp4 -o "$OUT/source_%(id)s.%(ext)s" -- "$URL"
yt-dlp --no-playlist --no-overwrites --skip-download --write-auto-subs --sub-langs ja --sub-format json3 \
  -o "$OUT/source_%(id)s.%(ext)s" -- "$URL" || echo '自動字幕の取得はスキップしました'
yt-dlp --no-playlist --no-overwrites --skip-download --write-subs --sub-langs live_chat \
  -o "$OUT/source_%(id)s.%(ext)s" -- "$URL" || echo 'チャットの取得はスキップしました'
echo "保存先: $OUT"
