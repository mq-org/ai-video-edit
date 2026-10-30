#!/usr/bin/env python3
"""ライブチャット（yt-dlp の live_chat.json）を整形し、反応が集中する時刻＝盛り上がりピークを集計する。

クリップ選定（references/editing-guide.md Phase 4）の客観的証拠に使う。
使い方:
  python3 scripts/chat_peaks.py data/source/live_XXXX.live_chat.json data/transcripts/XXXX [--bin 60] [--top 15]
出力:
  <出力dir>/live_chat.txt   [mm:ss.s] @名前: 本文（全件・時系列）
  標準出力               分ごとの件数ヒストグラム＋上位ピーク（時刻・件数・メッセージ抜粋）
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('chat_json'); ap.add_argument('out_dir')
    ap.add_argument('--bin', type=int, default=60, help='集計幅（秒）')
    ap.add_argument('--top', type=int, default=15)
    args = ap.parse_args()
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)

    msgs = []
    with open(args.chat_json, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            r = d.get('replayChatItemAction', {})
            off = int(r.get('videoOffsetTimeMsec', 0)) / 1000
            for a in r.get('actions', []):
                item = a.get('addChatItemAction', {}).get('item', {})
                v = item.get('liveChatTextMessageRenderer') or item.get('liveChatPaidMessageRenderer')
                if not v:
                    continue
                text = ''.join(x.get('text', '') or x.get('emoji', {}).get('shortcuts', [''])[0]
                               for x in v.get('message', {}).get('runs', []))
                name = v.get('authorName', {}).get('simpleText', '')
                msgs.append((off, name, text))
    msgs.sort()
    (out_dir / 'live_chat.txt').write_text(
        ''.join(f'[{int(t // 60):02d}:{t % 60:04.1f}] {n}: {x}\n' for t, n, x in msgs), encoding='utf-8')

    bins = Counter(int(t // args.bin) for t, _, _ in msgs)
    by_bin = defaultdict(list)
    for t, n, x in msgs:
        by_bin[int(t // args.bin)].append((t, x))
    print(f'メッセージ {len(msgs)} 件 → {out_dir}/live_chat.txt')
    print(f'\n== {args.bin}秒ごとの件数 ==')
    if bins:
        for b in range(max(bins) + 1):
            c = bins.get(b, 0)
            t = b * args.bin
            print(f'{int(t // 60):3d}:{int(t % 60):02d} {"#" * c} {c}')
    print(f'\n== 上位ピーク（件数順・{args.top}件） ==')
    for b, c in bins.most_common(args.top):
        t = b * args.bin
        sample = ' / '.join(x for _, x in by_bin[b][:6])
        print(f'{int(t // 60):3d}:{int(t % 60):02d}〜 {c:3d}件: {sample[:120]}')


if __name__ == '__main__':
    main()
