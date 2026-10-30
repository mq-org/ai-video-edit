#!/usr/bin/env python3
"""Whisper 文字起こし（単語タイムスタンプ）。ローカル（mlx-whisper・既定）と OpenAI API の2エンジン。

使い方:
  python3 scripts/transcribe.py <動画または音声> <出力ディレクトリ> [--prompt "固有名詞 …"]
                                [--engine local|api] [--model NAME] [--tmp DIR]
例:
  python3 scripts/transcribe.py data/source/live_XXXX.mp4 data/transcripts/XXXX \
      --prompt "講師名、番組名、専門用語。"

出力（エンジンによらず同じ形）:
  <出力ディレクトリ>/whisper_verbose.json（一次データ: duration/text/words/segments）
  whisper_segments.txt（start-end テキスト）/ whisper_words.txt（単語タイムスタンプ）

エンジン:
  local（既定）: mlx-whisper を Apple Silicon の GPU で実行。モデル既定 mlx-community/whisper-large-v3-turbo
                （初回のみ Hugging Face から約1.6GB を取得）。API キー不要・ファイルサイズ制限なし。
                前提: .venv に mlx-whisper（scripts/setup.sh が入れる）。Intel Mac では使えない → api
                【ギャップ修復】長尺では前の窓の文脈に引きずられて 30 秒窓ぶんの発話が丸ごと落ちることがある
                （窓単体では正しく起こせる場合がある）。そこで本パスの後、
                単語の空白が 3 秒以上で話声レベルの音がある区間（90 秒以下）を切り出して個別に再文字起こしし、
                単語・セグメントを時刻順に差し込む。--repair-json で既存の whisper_verbose.json だけ修復もできる
  api          : OpenAI whisper-1（verbose_json, word+segment）。環境変数 OPENAI_API_KEY が必要。
                 25MB 超は無音点で自動分割してオフセット加算マージ（references/editing-guide.md）
  既定は環境変数 WHISPER_ENGINE / WHISPER_MODEL でも変えられる。

共通処理:
  1. 16kHz モノラル WAV を作る（両エンジンの入力。分割の基準音声にもなる）
  2. エンジンで文字起こし
  3. duration が ffprobe の音声長と一致（±1秒）することを検証し、3ファイルを書く
"""

import _venv  # noqa: F401  .venv があればその python で再実行する

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time

import requests
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LIMIT = 24.5 * 1024 * 1024
DEFAULT_MODEL = 'mlx-community/whisper-large-v3-turbo'


def sh(cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(r.stderr[-3000:]); sys.exit(1)
    return r


def duration(path):
    r = sh(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)])
    return float(r.stdout.strip())


# ================= ローカル（mlx-whisper） =================
GAP_MIN = 3.0        # 修復候補にする単語間の空白（秒）
GAP_MAX = 90.0       # これより長い空白は待機画面等の非発話区間とみなし、修復せず報告だけする
SPEECH_DB = -35.0    # 話声とみなす 50ms フレームの RMS（dBFS）
SPEECH_MIN_SEC = 1.0 # 空白内にこの秒数以上の話声フレームがあれば修復する


def _import_mlx():
    try:
        import mlx_whisper
        return mlx_whisper
    except ImportError:
        sys.exit('mlx-whisper が見つかりません。`scripts/setup.sh` を実行するか、'
                 'OPENAI_API_KEY を設定して `--engine api` を使ってください')


def _norm(txt):
    return re.sub(r'[\s、。,.!?！？…・「」]', '', txt)


def is_garbage(seg, strict, prompt_norm=''):
    """幻覚セグメントの判定。strict=True（修復パス）は低確度・温度1.0・プロンプト復唱も弾く。
    幻覚の典型例: compression_ratio>2.4（「ササササ…」）、キリル/英字混じり（「線工程falaены…」）、
    temperature=1.0 かつ avg_logprob<-1（「あれば簡単ですのでどうぞ…spoilers…」）、initial_prompt をそのまま出力、の4型"""
    txt = seg.get('text', '').strip()
    if not txt:
        return True
    if seg.get('compression_ratio', 0) > 2.4:
        return True
    if re.search(r'[\u0400-\u04FF]', txt):
        return True
    if len(re.findall(r'[A-Za-z]', txt)) / len(txt) > 0.4:
        return True
    if strict:
        if seg.get('avg_logprob', 0) < -1.0 or seg.get('temperature', 0) >= 1.0:
            return True
        if prompt_norm and len(_norm(txt)) >= 8 and _norm(txt) in prompt_norm:
            return True
    return False


def _collect(res, offset=0.0, strict=False, prompt=''):
    """mlx-whisper の結果 → (words, segments, 除去数)。offset 秒を加算し、API 版と同じ形に整える。幻覚は除く"""
    words, segments, dropped = [], [], []
    prompt_norm = _norm(prompt or '')
    for s in res.get('segments', []):
        if is_garbage(s, strict, prompt_norm):
            dropped.append(s); continue
        for w in s.get('words', []):
            txt = w['word'].strip()
            if txt:
                words.append({'word': txt, 'start': round(float(w['start']) + offset, 2),
                              'end': round(float(w['end']) + offset, 2)})
        seg = {k: s[k] for k in ('text', 'temperature', 'avg_logprob', 'compression_ratio', 'no_speech_prob') if k in s}
        seg['start'] = round(float(s['start']) + offset, 2); seg['end'] = round(float(s['end']) + offset, 2)
        segments.append(seg)
    return words, segments, dropped


def speech_seconds(audio, sr, a, b):
    """audio[a:b] のうち話声レベル（SPEECH_DB 超）の 50ms フレームの合計秒数"""
    import numpy as np
    x = audio[int(a * sr):int(b * sr)]
    fl = int(sr * 0.05); m = len(x) // fl
    if m == 0:
        return 0.0
    rms = np.sqrt((x[:m * fl].reshape(m, fl).astype('float32') ** 2).mean(1))
    db = 20 * np.log10(rms + 1e-9)
    return float((db > SPEECH_DB).sum() * 0.05)


def find_gaps(words, dur):
    """単語の空白（GAP_MIN 秒以上）を (start, end) で列挙。先頭・末尾も含む"""
    ws = sorted(words, key=lambda w: w['start'])
    gaps, prev_end = [], 0.0
    for w in ws:
        if w['start'] - prev_end >= GAP_MIN:
            gaps.append((prev_end, w['start']))
        prev_end = max(prev_end, w['end'])
    if dur - prev_end >= GAP_MIN:
        gaps.append((prev_end, dur))
    return gaps


def repair_gaps(mlx_whisper, audio, sr, words, segments, dur, prompt, model, pad=0.3):
    """話声のある空白を個別に再文字起こしして差し込む。戻り値: (words, segments, 修復リスト, 長い空白リスト, 除去リスト)"""
    repaired, long_gaps, dropped_all = [], [], []
    for a, b in find_gaps(words, dur):
        sp = speech_seconds(audio, sr, a, b)
        if sp < SPEECH_MIN_SEC:
            continue
        if b - a > GAP_MAX:
            long_gaps.append((a, b, sp)); continue
        ca, cb = max(0.0, a - pad), min(dur, b + pad)
        res = mlx_whisper.transcribe(audio[int(ca * sr):int(cb * sr)], path_or_hf_repo=model, language='ja',
                                     word_timestamps=True, initial_prompt=prompt or None, verbose=False)
        nw, ns, dropped = _collect(res, offset=ca, strict=True, prompt=prompt)
        dropped_all += [(a, b, d) for d in dropped]
        nw = [w for w in nw if a + 0.05 < w['start'] < b - 0.05]
        ns = [x for x in ns if a - pad <= x['start'] < b]
        if nw:
            words += nw; segments += ns
            repaired.append((a, b, len(nw), ''.join(w['word'] for w in nw)))
    words.sort(key=lambda w: w['start']); segments.sort(key=lambda x: x['start'])
    return words, segments, repaired, long_gaps, dropped_all


def report_gaps(repaired, long_gaps, dropped=()):
    print(f'    ギャップ修復: {len(repaired)} 箇所（{sum(b - a for a, b, _, _ in repaired):.1f}秒）')
    for a, b, n, txt in repaired:
        print(f'      {a:8.2f}-{b:8.2f} +{n}語: {txt[:60]}')
    if dropped:
        print(f'    修復候補のうち幻覚として除去: {len(dropped)} セグメント')
        for a, b, d in dropped:
            print(f"      {a:8.2f}-{b:8.2f} lp={d.get('avg_logprob', 0):.2f} cr={d.get('compression_ratio', 0):.2f} "
                  f"T={d.get('temperature', 0):.1f}: {d.get('text', '').strip()[:40]}")
    if long_gaps:
        print(f'    未修復の長い空白（{GAP_MAX:.0f}秒超・待機画面や BGM 区間の可能性。Phase 3 で使用不可時間帯として確認）:')
        for a, b, sp in long_gaps:
            print(f'      {a:8.2f}-{b:8.2f}（{(b - a) / 60:.1f}分・話声レベル {sp:.0f}秒）')


def transcribe_local(wav, dur, prompt, model, condition_prev=True):
    mlx_whisper = _import_mlx()
    import numpy as np
    from mlx_whisper.audio import load_audio, SAMPLE_RATE
    print(f'2/3 mlx-whisper で文字起こし（{model}）… 進捗バーは標準エラーに出ます')
    t0 = time.time()
    audio = np.asarray(load_audio(str(wav)))   # mlx 配列 → numpy（スライスと RMS 計算のため）
    res = mlx_whisper.transcribe(
        audio, path_or_hf_repo=model, language='ja', word_timestamps=True,
        initial_prompt=prompt or None, verbose=False, condition_on_previous_text=condition_prev,
        hallucination_silence_threshold=2.0,   # 長い無音（待機画面など）での幻覚を抑える
    )
    words, segments, dropped_main = _collect(res, strict=False)
    if dropped_main:
        print(f'    本パスの幻覚除去: {len(dropped_main)} セグメント（圧縮率>2.4 または非日本語文字）')
    print(f'    本パス {(time.time() - t0) / 60:.1f}分（音声 {dur / 60:.1f}分・{dur / max(time.time() - t0, 1e-6):.1f}倍速）')
    t1 = time.time()
    words, segments, repaired, long_gaps, dropped = repair_gaps(mlx_whisper, audio, SAMPLE_RATE, words, segments, dur, prompt, model)
    report_gaps(repaired, long_gaps, dropped)
    for i, s in enumerate(segments):
        s['id'] = i
    elapsed = time.time() - t0
    print(f'    修復パス {(time.time() - t1) / 60:.1f}分 → 合計 {elapsed / 60:.1f}分')
    return {'duration': dur, 'language': 'japanese', 'text': ''.join(s['text'] for s in segments),
            'words': words, 'segments': segments,
            'engine': 'local', 'model': model, 'elapsed_sec': round(elapsed, 1),
            'repaired_gaps': [[a, b, n] for a, b, n, _ in repaired]}


def repair_existing(json_path, wav, dur, prompt, model):
    """既存の whisper_verbose.json に対してギャップ修復だけを行う（--repair-json）"""
    mlx_whisper = _import_mlx()
    import numpy as np
    from mlx_whisper.audio import load_audio, SAMPLE_RATE
    d = json.loads(Path(json_path).read_text(encoding='utf-8'))
    print(f'2/3 既存の文字起こしを修復（{json_path}・{model}）…')
    audio = np.asarray(load_audio(str(wav)))   # mlx 配列 → numpy（スライスと RMS 計算のため）
    words = [{'word': w['word'], 'start': float(w['start']), 'end': float(w['end'])} for w in d['words']]
    segments = [dict(s) for s in d['segments']]
    words, segments, repaired, long_gaps, dropped = repair_gaps(mlx_whisper, audio, SAMPLE_RATE, words, segments, dur, prompt, model)
    report_gaps(repaired, long_gaps, dropped)
    for i, s in enumerate(segments):
        s['id'] = i
    d.update({'words': words, 'segments': segments, 'text': ''.join(s['text'] for s in segments),
              'repaired_gaps': d.get('repaired_gaps', []) + [[a, b, n] for a, b, n, _ in repaired]})
    return d


# ================= OpenAI API（whisper-1） =================
def detect_silences(wav, noise='-35dB', min_d=0.8):
    r = sh(['ffmpeg', '-v', 'info', '-i', str(wav), '-af', f'silencedetect=noise={noise}:d={min_d}', '-f', 'null', '-'],
           check=False)
    out = r.stderr
    starts = [float(x) for x in re.findall(r'silence_start: ([\d.]+)', out)]
    ends = [float(x) for x in re.findall(r'silence_end: ([\d.]+)', out)]
    return list(zip(starts, ends))


def pick_cut(target, silences, window=600.0):
    """target 秒に最も近い 1秒以上の無音の中央（±window 内）。無ければ任意の無音、それも無ければ target"""
    long_ = [(abs((a + b) / 2 - target), (a + b) / 2) for a, b in silences if b - a >= 1.0 and abs((a + b) / 2 - target) <= window]
    if long_:
        return min(long_)[1]
    any_ = [(abs((a + b) / 2 - target), (a + b) / 2) for a, b in silences if abs((a + b) / 2 - target) <= window]
    if any_:
        return min(any_)[1]
    return target


def whisper_api(m4a, key, prompt, json_path):
    fields = [('model', 'whisper-1'), ('response_format', 'verbose_json'),
              ('timestamp_granularities[]', 'word'), ('timestamp_granularities[]', 'segment'),
              ('language', 'ja')]
    if prompt:
        fields.append(('prompt', prompt))
    with open(m4a, 'rb') as stream:
        response = requests.post('https://api.openai.com/v1/audio/transcriptions',
                                 headers={'Authorization': f'Bearer {key}'},
                                 data=fields, files={'file': (m4a.name, stream, 'audio/mp4')}, timeout=600)
    if not response.ok:
        sys.exit(f'Whisper API エラー (HTTP {response.status_code})。通信・認証・容量を確認してください')
    d = response.json()
    json_path.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
    return d


def transcribe_api(wav, dur, prompt, tmp):
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        sys.exit('環境変数 OPENAI_API_KEY が未設定です（--engine api には必要）')
    full = tmp / 'audio.m4a'
    print('2/3 48kbps m4a 化 …')
    sh(['ffmpeg', '-y', '-v', 'error', '-i', str(wav), '-c:a', 'aac', '-b:a', '48k', str(full)])
    size = full.stat().st_size
    n = max(1, math.ceil(size / LIMIT))
    print(f'    {size / 1024 / 1024:.1f}MB → {n} パート')

    if n == 1:
        parts = [(0.0, dur)]
    else:
        print('    無音点で分割 …')
        silences = detect_silences(wav)
        cuts = [pick_cut(dur * i / n, silences) for i in range(1, n)]
        bounds = [0.0] + cuts + [dur]
        parts = list(zip(bounds[:-1], bounds[1:]))
        for i, (a, b) in enumerate(parts):
            print(f'    part{i}: {a:.2f}-{b:.2f}s')

    results = []
    for i, (a, b) in enumerate(parts):
        m4a = full if n == 1 else tmp / f'part{i}.m4a'
        if n > 1:
            sh(['ffmpeg', '-y', '-v', 'error', '-ss', f'{a:.3f}', '-t', f'{b - a:.3f}', '-i', str(wav),
                '-c:a', 'aac', '-b:a', '48k', str(m4a)])
            if m4a.stat().st_size > LIMIT:
                sys.exit(f'part{i} が25MB超（{m4a.stat().st_size / 1024 / 1024:.1f}MB）。--tmp を変えて再実行するか分割数を増やす改修が必要')
        print(f'    Whisper API 送信 part{i} ({(b - a) / 60:.1f}分) …')
        d = whisper_api(m4a, key, prompt, tmp / f'part{i}.json')
        results.append((a, d))
        print(f"    受信: duration {d.get('duration', 0):.2f}s words {len(d.get('words', []))}")

    words, segments, texts, total = [], [], [], 0.0
    for a, d in results:
        for w in d.get('words', []):
            words.append({'word': w['word'], 'start': w['start'] + a, 'end': w['end'] + a})
        for s in d.get('segments', []):
            s2 = dict(s); s2['start'] = s['start'] + a; s2['end'] = s['end'] + a
            s2.pop('tokens', None)
            segments.append(s2)
        texts.append(d.get('text', ''))
        total += float(d.get('duration', 0))
    for i, s in enumerate(segments):
        s['id'] = i
    return {'duration': total, 'language': 'japanese', 'text': ''.join(texts),
            'words': words, 'segments': segments,
            'parts': [{'start': a, 'end': b} for a, b in parts],
            'engine': 'api', 'model': 'whisper-1'}


# ================= メイン =================
def glossary_prompt(path):
    """data/glossary.tsv の 1 列目（正しい表記）を「、」で並べて initial_prompt にする。
    Whisper は同音異義語を頻度で選ぶ（例: たいがん→対岸）。固有名詞・用語をプロンプトに出しておくと、その表記に寄る"""
    p = Path(path)
    if not p.exists():
        return ''
    terms = []
    for ln in p.read_text(encoding='utf-8').splitlines():
        if ln.strip() and not ln.startswith('#'):
            terms.append(ln.split('\t')[0].strip())
    return '、'.join(t for t in terms if t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src'); ap.add_argument('out_dir')
    ap.add_argument('--overwrite', action='store_true', help='既存の文字起こしを上書き')
    ap.add_argument('--prompt', default='', help='Whisper の initial_prompt（固有名詞の列挙）。省略時は --glossary の用語')
    ap.add_argument('--glossary', default=str(BASE / 'data' / 'glossary.tsv'),
                    help='用語集 TSV（正しい表記/読み/誤変換/備考）。1 列目を initial_prompt に並べる')
    ap.add_argument('--engine', choices=['local', 'api'], default=os.environ.get('WHISPER_ENGINE', 'local'))
    ap.add_argument('--model', default=os.environ.get('WHISPER_MODEL', DEFAULT_MODEL),
                    help='local 用モデル（Hugging Face repo または ローカルパス）')
    ap.add_argument('--tmp', default=None)
    ap.add_argument('--limit-mb', type=float, default=None, help='api: 分割閾値MB（既定24.5。分割ロジックの試験用）')
    ap.add_argument('--condition-prev', type=int, default=int(os.environ.get('WHISPER_CONDITION_PREV', '1')),
                    help='local: 前の窓のテキストを次の窓のプロンプトにする（1=する・既定／0=しない）')
    ap.add_argument('--repair-json', default=None,
                    help='local: 既存の whisper_verbose.json を読み、ギャップ修復だけ行って <out_dir> に書く')
    args = ap.parse_args()
    if (Path(args.out_dir) / 'whisper_verbose.json').exists() and not args.overwrite:
        sys.exit('文字起こしが既に存在します。再生成する場合のみ --overwrite を指定してください')
    if not Path(args.src).is_file():
        sys.exit('素材ファイルが見つかりません')
    if not args.prompt:
        args.prompt = glossary_prompt(args.glossary)
        if args.prompt:
            print(f'    用語集プロンプト: {args.prompt}')
    global LIMIT
    if args.limit_mb:
        LIMIT = args.limit_mb * 1024 * 1024
    src = Path(args.src); out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(args.tmp) if args.tmp else BASE / 'data' / 'tmp' / f'transcribe_{src.stem}'
    tmp.mkdir(parents=True, exist_ok=True)

    wav = tmp / 'audio16k.wav'
    print(f'1/3 16kHzモノラルWAV化 …（engine={args.engine}）')
    sh(['ffmpeg', '-y', '-v', 'error', '-i', str(src), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(wav)])
    dur = duration(wav)
    print(f'    音声長 {dur:.2f}s（{dur / 60:.1f}分）')

    if args.repair_json:
        merged = repair_existing(args.repair_json, wav, dur, args.prompt, args.model)
    elif args.engine == 'local':
        merged = transcribe_local(wav, dur, args.prompt, args.model, condition_prev=bool(args.condition_prev))
    else:
        merged = transcribe_api(wav, dur, args.prompt, tmp)

    print('3/3 書き出し・検証 …')
    words, segments = merged['words'], merged['segments']
    merged['prompt'] = args.prompt
    (out_dir / 'whisper_verbose.json').write_text(json.dumps(merged, ensure_ascii=False), encoding='utf-8')
    (out_dir / 'whisper_segments.txt').write_text(
        ''.join(f"{s['start']:8.2f}-{s['end']:8.2f} {s['text'].strip()}\n" for s in segments), encoding='utf-8')
    (out_dir / 'whisper_words.txt').write_text(
        ''.join(f"{w['start']:8.2f} {w['end']:8.2f} {w['word']}\n" for w in words), encoding='utf-8')
    total = float(merged['duration'])
    diff = abs(total - dur)
    status = 'OK' if diff <= 1.0 else f'不一致（差 {diff:.2f}s）要確認'
    print(f'    Whisper duration {total:.2f}s vs ffprobe {dur:.2f}s → {status}')
    if words:
        tail = max(w['end'] for w in words)
        print(f'    最終語 end {tail:.2f}s（音声長の {tail / dur * 100:.0f}%。極端に短ければ途中で止まっている）')
    print(f'    words {len(words)} / segments {len(segments)}')
    print(f'出力: {out_dir}/whisper_verbose.json')


if __name__ == '__main__':
    main()
