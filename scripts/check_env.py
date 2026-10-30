#!/usr/bin/env python3
"""読み取り専用の環境診断。APIキーの値は出力しない。"""
import _venv  # noqa: F401
import importlib.util
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

BASE = Path(__file__).resolve().parent.parent

def main():
    missing = []
    def check(label, okay, required=True):
        print(f"{'OK' if okay else 'NG' if required else '任意・未導入'}  {label}")
        if required and not okay:
            missing.append(label)
    check('Python 3.11以上', sys.version_info >= (3, 11))
    for executable in ['ffmpeg', 'ffprobe']:
        check(executable, bool(shutil.which(executable)))
    if shutil.which('ffmpeg'):
        filters = subprocess.run(['ffmpeg', '-hide_banner', '-filters'], capture_output=True, text=True)
        check('FFmpeg assフィルター（libass）', ' ass ' in filters.stdout)
    for module in ['PIL', 'numpy', 'requests']:
        check(module, importlib.util.find_spec(module) is not None)
    for filename in ['MPLUSRounded1c-Black.ttf', 'MochiyPopOne-Regular.ttf', 'ShipporiMinchoB1-ExtraBold.ttf']:
        check(filename, (BASE / 'assets/fonts' / filename).is_file())
    check('yt-dlp（URL取り込み用）', bool(shutil.which('yt-dlp')), False)
    local = platform.system() == 'Darwin' and platform.machine() == 'arm64' and importlib.util.find_spec('mlx_whisper') is not None
    check('ローカル文字起こし', local, False)
    check('API文字起こし（環境変数設定のみ確認）', bool(os.environ.get('OPENAI_API_KEY')), False)
    print('編集の必須項目はOK' if not missing else '不足: ' + ', '.join(missing))
    print('文字起こしはローカル/APIのどちらかを選択。既存の文字起こしがあればどちらも不要です。')
    return 1 if missing else 0

if __name__ == '__main__':
    sys.exit(main())
