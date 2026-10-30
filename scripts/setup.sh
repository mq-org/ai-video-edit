#!/usr/bin/env bash
# Mac向けセットアップ。モデルの取得は初回の文字起こし時に行う。
set -euo pipefail
BASE="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL=0
case "${1:-}" in
  --with-local) LOCAL=1 ;;
  --help|-h) echo '使い方: bash scripts/setup.sh [--with-local]'; exit 0 ;;
  '') ;;
  *) echo '不明な引数です。--help を参照してください' >&2; exit 1 ;;
esac
if [ "$(uname -s)" != Darwin ]; then
  echo '自動セットアップはmacOS用です。READMEの手動セットアップを参照してください。' >&2; exit 1
fi
command -v brew >/dev/null || { echo 'Homebrewを導入してから再実行してください: https://brew.sh/' >&2; exit 1; }
for pkg in ffmpeg yt-dlp python@3.13; do
  brew list --versions "$pkg" >/dev/null 2>&1 || brew install "$pkg"
done
PY="$(brew --prefix python@3.13)/bin/python3.13"
[ -x "$BASE/.venv/bin/python" ] || "$PY" -m venv "$BASE/.venv"
REQ="$BASE/requirements.txt"
if [ "$LOCAL" = 1 ]; then
  [ "$(uname -m)" = arm64 ] || { echo '--with-localはApple Silicon専用です' >&2; exit 1; }
  REQ="$BASE/requirements-local.txt"
fi
"$BASE/.venv/bin/python" -m pip install -r "$REQ"
exec "$BASE/.venv/bin/python" "$BASE/scripts/check_env.py"
