#!/usr/bin/env python3
"""文字起こしの誤変換を 3 ファイル（whisper_verbose.json / whisper_segments.txt / whisper_words.txt）同時に直す。

使い方: python3 scripts/fix_transcript.py <文字起こしディレクトリ> 誤=正 [誤=正 ...] [--dry-run]
例:     python3 scripts/fix_transcript.py data/projects/demo/transcript 公正=校正

- 単語列は「対」「岸」のように 1 文字ずつ分かれていることがあるので、語を連結した文字列で探し、該当する語に
  文字を割り当て直す（同じ長さなら 1 文字ずつ置換、長さが違えば該当語をまとめて 1 語にする）
- segments の text と全体 text は単純置換
- 直した箇所（時刻・前後の文脈）を corrections.log に追記する。一次データを書き換えるので、根拠を必ず残すこと
- 誤変換は文脈依存（「対岸」が本当に川の対岸のこともある）。必ず該当箇所の文脈を確認してから実行する
  （このスクリプトは置換前に各箇所の前後を表示する。--dry-run で表示だけ）
"""

import _venv  # noqa: F401

import datetime as dt
import json
import sys
from pathlib import Path


def find_in_words(words, wrong):
    """語列を連結した文字列で wrong を探し、[(先頭語 idx, 末尾語 idx, 先頭語内オフセット, 末尾語内オフセット), ...]"""
    text, owner, offs = '', [], []
    for i, w in enumerate(words):
        for k in range(len(w['word'])):
            owner.append(i); offs.append(k)
        text += w['word']
    hits, pos = [], text.find(wrong)
    while pos >= 0:
        j = pos + len(wrong) - 1
        hits.append((owner[pos], owner[j], offs[pos], offs[j]))
        pos = text.find(wrong, pos + 1)
    return hits


def apply(words, hit, wrong, right):
    i0, i1, o0, o1 = hit
    if i0 == i1:
        w = words[i0]['word']
        words[i0]['word'] = w[:o0] + right + w[o1 + 1:]
        return
    if len(wrong) == len(right) and all(len(words[i]['word']) == 1 for i in range(i0, i1 + 1)) and o0 == 0:
        for k, i in enumerate(range(i0, i1 + 1)):
            words[i]['word'] = right[k]
        return
    # 長さが違う／語の境界が文字と一致しない → まとめて 1 語に
    head = words[i0]['word'][:o0]
    tail = words[i1]['word'][o1 + 1:]
    words[i0]['word'] = head + right + tail
    words[i0]['end'] = words[i1]['end']
    del words[i0 + 1:i1 + 1]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry = '--dry-run' in sys.argv
    if len(args) < 2:
        print(__doc__); sys.exit(1)
    d = Path(args[0])
    rules = [a.split('=', 1) for a in args[1:]]
    jp = d / 'whisper_verbose.json'
    data = json.loads(jp.read_text(encoding='utf-8'))
    words, segments = data['words'], data['segments']
    log = []
    for wrong, right in rules:
        hits = find_in_words(words, wrong)
        if not hits:
            print(f'「{wrong}」: 語列に見つからず'); continue
        # 後ろから適用（index がずれないように）
        for hit in reversed(hits):
            i0, i1 = hit[0], hit[1]
            ctx = ''.join(w['word'] for w in words[max(0, i0 - 6):i1 + 7])
            t = words[i0]['start']
            print(f'  {t:8.2f}s 「{wrong}」→「{right}」 …{ctx}…')
            log.append(f'{t:.2f}s {wrong}→{right} …{ctx}…')
            if not dry:
                apply(words, hit, wrong, right)
        n_seg = sum(s['text'].count(wrong) for s in segments)
        if not dry:
            for s in segments:
                s['text'] = s['text'].replace(wrong, right)
            data['text'] = data.get('text', '').replace(wrong, right)
        print(f'「{wrong}」→「{right}」: 語 {len(hits)} 箇所・segments {n_seg} 箇所')
    if dry:
        print('（--dry-run: 書き換えなし）'); return
    jp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    (d / 'whisper_segments.txt').write_text(
        ''.join(f"{s['start']:8.2f}-{s['end']:8.2f} {s['text'].strip()}\n" for s in segments), encoding='utf-8')
    (d / 'whisper_words.txt').write_text(
        ''.join(f"{w['start']:8.2f} {w['end']:8.2f} {w['word']}\n" for w in words), encoding='utf-8')
    with open(d / 'corrections.log', 'a', encoding='utf-8') as f:
        f.write(f"# {dt.date.today().isoformat()} fix_transcript.py {' '.join(args[1:])}\n")
        f.write(''.join(f'{ln}\n' for ln in log))
    print(f'書き換え: {jp} と txt 2 本。記録: {d / "corrections.log"}')


if __name__ == '__main__':
    main()
