"""プロジェクト同梱の仮想環境（.venv）へ自動で切り替えるブートストラップ。

`python3 scripts/xxx.py` と素の python3 で起動されても、リポジトリ直下に .venv があれば
その python で自分自身を実行し直す（PIL・mlx-whisper 等の依存はすべて .venv に入っている）。
各スクリプトの先頭で `import _venv  # noqa` するだけでよい。.venv が無ければ何もしない。
環境変数 NO_VENV=1 で無効化できる（デバッグ用）。
"""

import os
import sys
from pathlib import Path

_VENV = Path(__file__).resolve().parent.parent / '.venv'
_PY = _VENV / 'bin' / 'python'

if _PY.exists() and not os.environ.get('NO_VENV') and Path(sys.prefix).resolve() != _VENV.resolve():
    os.execv(str(_PY), [str(_PY)] + sys.argv)
