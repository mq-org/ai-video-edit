#!/usr/bin/env python3
"""個人素材なしで動作確認できるテスト映像・合成文字起こしを生成する。

音声は周期的なテスト音であり、人の発話ではない。文字起こし精度の評価には使えない。
python scripts/make_demo.py [--seconds 60] [--width 1280] [--height 720]
"""
import argparse
import json
from pathlib import Path
import subprocess

BASE = Path(__file__).resolve().parent.parent

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seconds', type=float, default=60)
    p.add_argument('--width', type=int, default=1280)
    p.add_argument('--height', type=int, default=720)
    p.add_argument('--name', default='demo')
    a = p.parse_args()
    if not 1 <= a.seconds <= 7200 or a.width < 160 or a.height < 90 or a.width % 2 or a.height % 2:
        p.error('秒数は1〜7200、サイズは160x90以上の偶数で指定してください')
    import re
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', a.name):
        p.error('nameは英数字・ハイフン・アンダースコアで指定してください')
    src = BASE / 'data/source' / f'{a.name}.mp4'
    out = BASE / 'data/projects' / a.name / 'transcript'
    if src.exists() or (out / 'whisper_verbose.json').exists():
        p.error('同名のデモが存在します。別の --name を指定してください')
    src.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-n', '-f', 'lavfi',
                    '-i', f'color=c=0x334155:s={a.width}x{a.height}:r=30:d={a.seconds}',
                    '-f', 'lavfi', '-i', f'aevalsrc=if(lt(mod(t\\,10)\\,4)\\,0.05*sin(2*PI*440*t)\\,0):s=48000:d={a.seconds}',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                    '-shortest', str(src)], check=True)
    words, segments = [], []
    for i, start in enumerate(range(0, int(a.seconds), 10)):
        end = min(start + 3.8, a.seconds)
        if end - start < 0.5:
            continue
        text = f'これはテスト用の映像です（{i + 1}）'
        step = (end - start - 0.2) / len(text)
        words.extend({'start': round(start + 0.1 + j * step, 4),
                      'end': round(start + 0.1 + (j + 1) * step, 4), 'word': ch} for j, ch in enumerate(text))
        segments.append({'id': i, 'start': start + 0.1, 'end': end, 'text': text})
    doc = {'duration': a.seconds, 'language': 'japanese', 'engine': 'synthetic',
           'text': ''.join(s['text'] for s in segments), 'words': words, 'segments': segments}
    (out / 'whisper_verbose.json').write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'whisper_segments.txt').write_text(''.join(f"{s['start']:.2f}-{s['end']:.2f} {s['text']}\n" for s in segments), encoding='utf-8')
    (out / 'whisper_words.txt').write_text(''.join(f"{w['start']:.2f} {w['end']:.2f} {w['word']}\n" for w in words), encoding='utf-8')
    print(f'素材: {src}\n合成文字起こし: {out}/whisper_verbose.json')
    print('テスト音と架空字幕です。音声認識や意味編集の品質を示すサンプルではありません。')

if __name__ == '__main__':
    main()
