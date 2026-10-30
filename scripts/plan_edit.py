#!/usr/bin/env python3
"""実音声の無音と単語の空白が一致する場所だけを短縮する編集計画案を作る。

言い間違い・重複テイクは意味判断が必要なので自動削除しない。生成計画はreviewed:false。
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re

from edit_video import BASE, number, probe, read_json, run, validate_intervals


def merge_intervals(intervals):
    out = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(end, out[-1][1]))
        else:
            out.append((start, end))
    return out


def complement(intervals, duration):
    result, cursor = [], 0.0
    for start, end in merge_intervals(intervals):
        start, end = max(0.0, start), min(duration, end)
        if start > cursor:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        result.append((cursor, duration))
    return result


def silence_intervals(source, duration, noise_db, min_silence):
    stderr = run(['ffmpeg', '-hide_banner', '-nostdin', '-i', source, '-vn', '-af',
                  f'silencedetect=noise={noise_db}dB:d={min_silence}', '-f', 'null', '-']).stderr
    events = re.findall(r'silence_(start|end):\s*([-\d.]+)', stderr)
    result, start = [], None
    for kind, value in events:
        value = max(0.0, min(duration, float(value)))
        if kind == 'start':
            start = value
        elif start is not None:
            result.append((start, value))
            start = None
    if start is not None:
        result.append((start, duration))
    return merge_intervals(result)


def propose_removals(words, silences, duration, min_silence=0.8, keep_silence=0.2, guard_before=0.12, guard_after=0.18):
    """語頭/語尾に余裕を残した非発話区間と、音声無音の共通部分だけを候補にする。"""
    protected = [(max(0, w['start'] - guard_before), min(duration, w['end'] + guard_after)) for w in words]
    gaps = complement(protected, duration)
    removals = []
    for low, high in silences:
        for start, end in gaps:
            a, b = max(low, start), min(high, end)
            if b - a < max(min_silence, keep_silence + 0.1):
                continue
            # 語時刻誤差のガードに加え、無音を少なくともkeep_silence秒保持する。
            cut_start, cut_end = a + keep_silence / 2, b - keep_silence / 2
            removals.append((cut_start, cut_end))
    return [{'start': round(a, 6), 'end': round(b, 6),
             'reason': '実音声の無音と単語空白が一致。語頭/語尾の保護余裕を残して短縮',
             'kind': 'silence_candidate'} for a, b in merge_intervals(removals)]


def relative(path, directory):
    return os.path.relpath(Path(path).resolve(), directory)


def build_plan(source, transcript, plan_path, *, min_silence=0.8, keep_silence=0.2, noise_db=-35, output=None, no_silence=False):
    source, transcript, plan_path = Path(source).resolve(), Path(transcript).resolve(), Path(plan_path).resolve()
    if not source.is_file() or not transcript.is_file():
        raise ValueError('素材と文字起こしJSONが必要です')
    if plan_path in (source, transcript):
        raise ValueError('計画ファイルで入力を上書きできません')
    number(min_silence, 'min-silence', 0.2, 60)
    number(keep_silence, 'keep-silence', 0.05, 10)
    number(noise_db, 'noise-db', -80, -10)
    if min_silence <= keep_silence:
        raise ValueError('min-silence は keep-silence より大きくしてください')
    info = probe(source)
    words = validate_intervals(read_json(transcript).get('words', []), info['duration'], 'words', allow_zero=True)
    if not words:
        raise ValueError('単語時刻wordsがありません。無音と語の交差検証にはwordsが必要です')
    words.sort(key=lambda word: (word['start'], word['end']))
    if not info['audio'] and not no_silence:
        raise ValueError('音声がないため無音検出できません。--no-silenceで全編採用案を作れます')
    silences = [] if no_silence else silence_intervals(source, info['duration'], noise_db, min_silence)
    removed = propose_removals(words, silences, info['duration'], min_silence, keep_silence)
    kept = complement([(r['start'], r['end']) for r in removed], info['duration'])
    output = Path(output).resolve() if output else BASE / 'output' / plan_path.parent.name / 'edited.mp4'
    if output in (source, transcript, plan_path):
        raise ValueError('出力先が入力ファイルと衝突しています')
    return {
        'schema_version': 1, 'reviewed': False,
        'source': relative(source, plan_path.parent), 'transcript': relative(transcript, plan_path.parent),
        'output': relative(output, plan_path.parent),
        'brief': {'mode': 'clean', 'goal': '話の意味を保って間を整え、1素材から字幕付きの1本を作る',
                  'review_notes': '無音候補の前後を音で確認。言い間違い/重複テイクは全文を読んで採否を判断し、segmentsへ反映する'},
        'segments': [{'start': round(a, 6), 'end': round(b, 6)} for a, b in kept],
        'removed': removed,
        'video': {'width': info['width'] // 2 * 2, 'height': info['height'] // 2 * 2, 'fit': 'contain', 'fps': 30},
        'subtitle': {'enabled': True, 'max_chars': 12 if info['height'] > info['width'] else 26},
        'audio': {'channel': 'auto', 'loudness': -14},
        'analysis': {'silence_threshold_db': noise_db, 'min_silence': min_silence, 'keep_silence': keep_silence,
                     'guard_before': 0.12, 'guard_after': 0.18, 'detected_silences': len(silences),
                     'note': '自動案。BGM・小声・未文字起こし発話を含む素材は特に確認が必要。意味による削除は未実施。'}
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('transcript')
    parser.add_argument('--out', required=True)
    parser.add_argument('--output', help='完成mp4のパス（この引数は実行ディレクトリ基準）')
    parser.add_argument('--min-silence', type=float, default=0.8)
    parser.add_argument('--keep-silence', type=float, default=0.2)
    parser.add_argument('--noise-db', type=float, default=-35)
    parser.add_argument('--no-silence', action='store_true', help='無音検出を行わず全編採用の案を作る')
    args = parser.parse_args()
    try:
        plan = build_plan(args.source, args.transcript, args.out, min_silence=args.min_silence,
                          keep_silence=args.keep_silence, noise_db=args.noise_db, output=args.output, no_silence=args.no_silence)
        target = Path(args.out).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        # 入力の調査中に同名ファイルが作られても上書きしない。
        with target.open('x', encoding='utf-8') as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        duration = sum(s['end'] - s['start'] for s in plan['segments'])
        print(f"計画案: {target}\n{len(plan['removed'])}箇所の無音短縮候補 / 完成予定 {duration:.2f}秒")
        print('全文とカット境界を確認し、必要な言い間違い・重複テイクを編集してからreviewed:trueにしてください。')
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(1, f'エラー: {exc}\n')


if __name__ == '__main__':
    import _venv  # noqa: F401
    main()
