#!/usr/bin/env python3
"""元動画のレイアウト調査用フレーム抽出＋コンタクトシート。

配信画面は時間帯でレイアウト（装飾枠・テロップ帯・ロゴ位置・待機画面・2画面）が変わるため、
一定間隔で全編をサンプリングして目視し、使用可能な時間帯とクロップ値を決める。

使い方:
  python3 scripts/probe_layout.py <動画> [--interval 180] [--times 100,200,...] [--crop W:H:X:Y] [--name NAME]
  --interval  秒間隔で全編サンプリング（既定180秒=3分）
  --times     特定時刻だけ（カンマ区切り秒）。クリップ候補区間の確認用
  --crop      ffmpeg crop 指定で切り出した結果を並べる（ロゴ帯の位置揺れ・クロップ値の検証用）
出力: data/frames/probe_<NAME>/sheet_NN.png（24枚/シート・時刻ラベル付き）。Read ツールで目視する
"""

import _venv  # noqa: F401  .venv があればその python で再実行する

import argparse
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent.parent
FONT = BASE / 'assets' / 'fonts' / 'MPLUSRounded1c-Black.ttf'


def duration(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('video'); ap.add_argument('--interval', type=float, default=180)
    ap.add_argument('--times', default=None); ap.add_argument('--crop', default=None)
    ap.add_argument('--name', default=None); ap.add_argument('--width', type=int, default=320)
    args = ap.parse_args()
    video = Path(args.video)
    name = args.name or video.stem
    out_dir = BASE / 'data' / 'frames' / f'probe_{name}'
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.times:
        times = [float(t) for t in args.times.split(',')]
    else:
        dur = duration(video)
        times = [t for t in [i * args.interval for i in range(int(dur // args.interval) + 1)] if t < dur]
    vf = (f'crop={args.crop},' if args.crop else '') + f'scale={args.width}:-1'
    frames = []
    for t in times:
        png = out_dir / f'f_{int(t):06d}.png'
        r = subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', f'{t:.2f}', '-i', str(video),
                            '-frames:v', '1', '-vf', vf, str(png)], capture_output=True, text=True)
        if r.returncode == 0 and png.exists():
            frames.append((png, t))
    if not frames:
        sys.exit('フレームを抽出できませんでした')
    font = ImageFont.truetype(str(FONT), 16) if FONT.exists() else ImageFont.load_default()
    cols, per_sheet = 6, 24
    sheets = []
    for si in range(0, len(frames), per_sheet):
        chunk = frames[si:si + per_sheet]
        tw, th = Image.open(chunk[0][0]).size
        rows = (len(chunk) + cols - 1) // cols
        sheet = Image.new('RGB', (cols * tw, rows * (th + 24)), (30, 30, 30))
        draw = ImageDraw.Draw(sheet)
        for i, (png, t) in enumerate(chunk):
            im = Image.open(png).convert('RGB')
            x, y = (i % cols) * tw, (i // cols) * (th + 24)
            sheet.paste(im, (x, y))
            draw.text((x + 4, y + th + 3), f'{int(t // 60):3d}:{int(t % 60):02d} ({t:.0f}s)', fill=(255, 255, 255), font=font)
        p = out_dir / f'sheet_{si // per_sheet:02d}.png'
        sheet.save(p); sheets.append(p)
    print(f'{len(frames)} フレーム → {len(sheets)} シート')
    for p in sheets:
        print(f'  {p}')


if __name__ == '__main__':
    main()
