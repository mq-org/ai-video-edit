#!/usr/bin/env python3
"""JSONの編集計画を検証し、採用区間とシンプルな字幕から1本の動画を作る。"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

BASE = Path(__file__).resolve().parent.parent
FONT_PATH = BASE / 'assets/fonts/MPLUSRounded1c-Black.ttf'
FONT_NAME = 'Rounded Mplus 1c Black'
FPS_CHOICES = {24, 25, 30, 48, 50, 60}


def run(command, *, cwd=None):
    result = subprocess.run([str(x) for x in command], cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f'コマンド失敗: {command[0]}\n{result.stderr[-4000:]}')
    return result


def read_json(path):
    with Path(path).open(encoding='utf-8') as handle:
        return json.load(handle)


def number(value, label, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label}: 有限の数値が必要です')
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError(f'{label}: 範囲外です ({minimum}〜{maximum})')
    return float(value)


def local_path(value, base, label):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(f'{label}: ローカルのファイルパスが必要です')
    if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', value):
        raise ValueError(f'{label}: URLではなく取得済みのローカルファイルを指定してください')
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def probe(source):
    obj = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', source]).stdout)
    video = next((s for s in obj['streams'] if s['codec_type'] == 'video'), None)
    if video is None:
        raise ValueError('映像ストリームがありません')
    audio = next((s for s in obj['streams'] if s['codec_type'] == 'audio'), None)
    duration = float(obj.get('format', {}).get('duration') or video.get('duration') or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('素材の長さを取得できません')
    width, height = int(video['width']), int(video['height'])
    rotation = next((abs(float(s.get('rotation', 0))) for s in video.get('side_data_list', []) if 'rotation' in s), 0)
    if int(rotation) % 180 == 90:
        width, height = height, width
    return {'duration': duration, 'width': width, 'height': height, 'audio': audio is not None,
            'channels': int(audio.get('channels', 0)) if audio else 0,
            'frames': int(video['nb_frames']) if str(video.get('nb_frames', '')).isdigit() else None,
            'video_duration': float(video['duration']) if video.get('duration') else None,
            'audio_duration': float(audio['duration']) if audio and audio.get('duration') else None}


def validate_intervals(items, duration, label, *, allow_zero=False):
    if not isinstance(items, list):
        raise ValueError(f'{label}: 配列が必要です')
    result = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f'{label}[{i}]: オブジェクトが必要です')
        start = number(item.get('start'), f'{label}[{i}].start', 0, duration)
        end = number(item.get('end'), f'{label}[{i}].end', 0, duration + 0.05)
        if end < start or (end == start and not allow_zero):
            raise ValueError(f'{label}[{i}]: end は start より後にしてください')
        result.append({**item, 'start': start, 'end': min(end, duration)})
    return result


def quantize_segments(segments, fps, preview=None):
    """各区間の尺をフレーム単位にそろえ、字幕用の出力時刻を持たせる。"""
    output, offset = [], 0.0
    budget = math.floor(preview * fps + 1e-8) if preview is not None else None
    for item in segments:
        n = max(1, round((item['end'] - item['start']) * fps))
        if budget is not None:
            n = min(n, budget)
            if n <= 0:
                break
            budget -= n
        duration = n / fps
        output.append({'start': item['start'], 'end': item['start'] + duration,
                       'frames': n, 'duration': duration, 'offset': offset})
        offset += duration
    return output


def join_words(words):
    text = ''
    for word in words:
        value = str(word['word'])
        if text and value and not value.startswith(' ') and re.search(r'[A-Za-z0-9]$', text) and re.match(r'[A-Za-z0-9]', value):
            text += ' '
        text += value
    return text.strip()


def wrap_text(text, max_chars):
    """明示改行を保ち、長い行は空白/句読点を優先して折り返す。"""
    lines = []
    for paragraph in text.splitlines() or ['']:
        rest = paragraph.strip()
        while len(rest) > max_chars:
            cut = max_chars
            candidates = [i + 1 for i, ch in enumerate(rest[:max_chars]) if ch in ' 、。,.!?！？']
            if candidates and candidates[-1] >= max_chars // 2:
                cut = candidates[-1]
            lines.append(rest[:cut].strip())
            rest = rest[cut:].strip()
        if rest:
            lines.append(rest)
    return '\n'.join(lines)


def captions_from_words(words, segments, max_chars, max_duration=4.0):
    """カット境界で必ず句を分ける。再利用区間を含むため区間ごとに生成する。"""
    result = []
    for seg in segments:
        selected = [w for w in words if (w['end'] > seg['start'] + 1e-6 or w['start'] == w['end'] >= seg['start'])
                    and w['start'] < seg['end'] - 1e-6]
        group = []
        def flush():
            if not group:
                return
            start = seg['offset'] + max(0, group[0]['start'] - seg['start'])
            end = seg['offset'] + min(seg['duration'], max(w['end'] for w in group) - seg['start'])
            # Whisperは句読点や短い語を0秒として返す。文言を捨てず、単独なら短い表示時間を補う。
            if end <= start + 0.001:
                end = min(seg['offset'] + seg['duration'], start + 0.12)
            result.append({'start': start, 'end': end, 'text': wrap_text(join_words(group), max_chars)})
            group.clear()
        for word in selected:
            # 単一トークンが異常に長ければ無理に3行へせず、明示字幕の作成を求める。
            if len(wrap_text(str(word['word']), max_chars).splitlines()) > 2:
                raise ValueError('1語が字幕2行を超えます。captionsを短いフレーズに分けて指定してください')
            if group:
                proposed = wrap_text(join_words(group + [word]), max_chars)
                if len(proposed.splitlines()) > 2 or word['end'] - group[0]['start'] > max_duration or word['start'] - group[-1]['end'] > 0.6:
                    flush()
            group.append(word)
            if re.search(r'[。！？!?]$', str(word['word']).strip()) and group[-1]['end'] > group[0]['start'] + 0.001:
                flush()
        flush()
    return reconcile_auto_captions([c for c in result if c['end'] > c['start'] + 0.001], max_chars)


def reconcile_auto_captions(captions, max_chars):
    """Whisperの語時刻重複を字幕だけで調整。語の文字列やカット計画は変えない。"""
    output = []
    for original in captions:
        current = dict(original)
        if output and current['start'] < output[-1]['end'] - 0.001:
            previous = output[-1]
            if current['start'] > previous['start'] + 0.04:
                previous['end'] = current['start']
                previous['timing_adjusted'] = True
            else:
                combined = wrap_text(join_words([{'word': previous['text'].replace('\n', '')},
                                                 {'word': current['text'].replace('\n', '')}]), max_chars)
                if len(combined.splitlines()) <= 2:
                    previous['text'] = combined
                    previous['end'] = max(previous['end'], current['end'])
                    previous['timing_adjusted'] = True
                    continue
                # 同時刻に長い文が詰め込まれた場合は、文字数比で表示時間を配分する。
                finish = max(previous['end'], current['end'])
                ratio = len(previous['text']) / (len(previous['text']) + len(current['text']))
                boundary = previous['start'] + (finish - previous['start']) * ratio
                if boundary - previous['start'] < 0.04 or finish - boundary < 0.04:
                    raise ValueError('単語時刻が同時刻に集中しています。captionsで表示時刻を指定してください')
                previous['end'] = current['start'] = boundary
                current['end'] = finish
                previous['timing_adjusted'] = current['timing_adjusted'] = True
        output.append(current)
    return output


def map_captions(captions, segments, max_chars):
    """区間と字幕の交差だけを写像。同一区間の反復/時系列を遡る編集も保つ。"""
    output = []
    for seg in segments:
        for caption in captions:
            start, end = max(seg['start'], caption['start']), min(seg['end'], caption['end'])
            if end <= start + 0.001:
                continue
            text = wrap_text(caption['text'], max_chars)
            if len(text.splitlines()) > 2:
                raise ValueError(f'字幕が3行以上です。captionsを分割してください: {caption["text"][:80]}')
            output.append({'start': seg['offset'] + start - seg['start'],
                           'end': seg['offset'] + end - seg['start'], 'text': text})
    output.sort(key=lambda c: (c['start'], c['end']))
    return output


def load_plan(path, preview=None):
    path = Path(path).resolve()
    plan = read_json(path)
    if not isinstance(plan, dict) or type(plan.get('schema_version')) is not int or plan.get('schema_version') != 1:
        raise ValueError('schema_version: 1 のJSONオブジェクトが必要です')
    source = local_path(plan.get('source'), path.parent, 'source')
    output = local_path(plan.get('output'), path.parent, 'output')
    if not source.is_file():
        raise ValueError(f'素材が見つかりません: {source}')
    if output.suffix.lower() != '.mp4':
        raise ValueError('output は .mp4 にしてください')
    transcript = local_path(plan['transcript'], path.parent, 'transcript') if plan.get('transcript') else None
    if output == source or output == path or output == transcript:
        raise ValueError('入力ファイルを出力で上書きできません')
    info = probe(source)
    video = plan.get('video', {})
    subtitle = plan.get('subtitle', {})
    audio = plan.get('audio', {})
    if not all(isinstance(x, dict) for x in (video, subtitle, audio)):
        raise ValueError('video/subtitle/audio はオブジェクトにしてください')
    fps = number(video.get('fps', 30), 'video.fps')
    if fps not in FPS_CHOICES:
        raise ValueError('fps は24/25/30/48/50/60から選んでください')
    fps = int(fps)
    width = video.get('width', info['width'] // 2 * 2)
    height = video.get('height', info['height'] // 2 * 2)
    for value, name in [(width, 'width'), (height, 'height')]:
        number(value, f'video.{name}', 16, 7680)
        if not isinstance(value, int) or value % 2:
            raise ValueError(f'video.{name} は偶数の整数にしてください')
    fit = video.get('fit', 'contain')
    if fit not in ('contain', 'cover'):
        raise ValueError('video.fit は contain または cover です')
    channel = audio.get('channel', 'auto')
    if channel not in ('auto', 'left', 'right', 'stereo'):
        raise ValueError('audio.channel は auto/left/right/stereo です')
    if channel == 'right' and info['channels'] < 2:
        raise ValueError('右チャンネルを指定しましたが素材に右チャンネルがありません')
    loudness = number(audio.get('loudness', -14), 'audio.loudness', -30, -5)
    segments = validate_intervals(plan.get('segments'), info['duration'], 'segments')
    if not segments:
        raise ValueError('採用区間segmentsが空です')
    for item in segments:
        if item['end'] - item['start'] < 1 / fps:
            raise ValueError('1フレーム未満の区間は採用できません')
    if preview is not None:
        number(preview, 'preview-seconds', 1 / fps)
    timeline = quantize_segments(segments, fps, preview)
    if not timeline:
        raise ValueError('出力区間が空です')
    enabled = subtitle.get('enabled', True)
    if not isinstance(enabled, bool):
        raise ValueError('subtitle.enabled はtrue/falseです')
    burn_in = subtitle.get('burn_in', True)
    if not isinstance(burn_in, bool):
        raise ValueError('subtitle.burn_in はtrue/falseです')
    max_chars = subtitle.get('max_chars', 12 if height > width else 26)
    if not isinstance(max_chars, int) or not 4 <= max_chars <= 100:
        raise ValueError('subtitle.max_chars は4〜100の整数です')
    font_size = number(subtitle.get('font_size', round(height * (0.042 if height > width else 0.06))), 'subtitle.font_size', 8, height / 4)
    margin_bottom = number(subtitle.get('margin_bottom', round(height * (0.18 if height > width else 0.06))), 'subtitle.margin_bottom', 0, height * 0.75)
    captions = []
    if enabled:
        if 'captions' in plan:
            raw = validate_intervals(plan['captions'], info['duration'], 'captions')
            for c in raw:
                if not isinstance(c.get('text'), str) or not c['text'].strip():
                    raise ValueError('captions.text は空でない文字列にしてください')
            captions = map_captions(raw, timeline, max_chars)
        else:
            if not transcript or not transcript.is_file():
                raise ValueError('字幕用のtranscriptまたはcaptionsを指定してください')
            words = validate_intervals(read_json(transcript).get('words', []), info['duration'], 'words', allow_zero=True)
            if not words:
                raise ValueError('文字起こしにwordsがありません。captionsを指定できます')
            for w in words:
                if not isinstance(w.get('word'), str):
                    raise ValueError('words.word は文字列が必要です')
            words.sort(key=lambda w: (w['start'], w['end']))
            captions = captions_from_words(words, timeline, max_chars)
        for previous, current in zip(captions, captions[1:]):
            if current['start'] < previous['end'] - 0.01:
                raise ValueError('字幕の表示時間が重複しています。captions/wordsを修正してください')
        if not captions:
            raise ValueError('採用区間に字幕がありません。字幕不要ならsubtitle.enabled:falseにしてください')
    if preview is not None:
        output = output.with_name(output.stem + '.preview.mp4')
    # プレビュー名や付随ファイルでも入力を上書きしない。
    targets = [output, output.with_suffix('.srt'), output.with_suffix('.ass'), output.with_suffix('.report.json')]
    for target in targets:
        if target in (source, path, transcript):
            raise ValueError('出力先が入力ファイルと衝突しています')
    return {'plan': plan, 'plan_path': path, 'source': source, 'output': output, 'info': info,
            'segments': timeline, 'captions': captions, 'width': width, 'height': height,
            'fps': fps, 'fit': fit, 'subtitle': {'enabled': enabled, 'burn_in': burn_in, 'font_size': font_size, 'margin_bottom': margin_bottom},
            'channel': channel, 'loudness': loudness, 'preview': preview, 'targets': targets}


def timecode(seconds, *, ass=False):
    ticks = round(seconds * (100 if ass else 1000))
    units = 100 if ass else 1000
    whole, fraction = divmod(ticks, units)
    hour, rest = divmod(whole, 3600)
    minute, second = divmod(rest, 60)
    return f'{hour}:{minute:02d}:{second:02d}.{fraction:02d}' if ass else f'{hour:02d}:{minute:02d}:{second:02d},{fraction:03d}'


def ass_escape(text):
    # ASS制御構文として解釈されない全角記号へ置換。字幕の命令埋め込みを防ぐ。
    return text.replace('\\', '＼').replace('{', '｛').replace('}', '｝').replace('\r', '').replace('\n', r'\N')


def write_subtitles(config, ass_path, srt_path):
    margin = round(config['width'] * 0.04)
    size = config['subtitle']['font_size']
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: {config['width']}
PlayResY: {config['height']}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT_NAME},{size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,{max(1, round(size * 0.055, 1))},0,2,{margin},{margin},{round(config['subtitle']['margin_bottom'])},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events, srt = [], []
    for i, c in enumerate(config['captions'], 1):
        events.append(f"Dialogue: 0,{timecode(c['start'], ass=True)},{timecode(c['end'], ass=True)},Default,,0,0,0,,{ass_escape(c['text'])}\n")
        srt.append(f"{i}\n{timecode(c['start'])} --> {timecode(c['end'])}\n{c['text']}\n\n")
    ass_path.write_text(header + ''.join(events), encoding='utf-8')
    srt_path.write_text(''.join(srt), encoding='utf-8')


def check_caption_width(config):
    """同梱フォントで実幅を検査する。はみ出す字幕を無言で納品しない。"""
    if not config['captions']:
        return
    if not FONT_PATH.is_file():
        raise ValueError(f'同梱フォントがありません: {FONT_PATH}')
    try:
        from PIL import ImageFont
    except ImportError as exc:
        raise ValueError('字幕検証にはPillowが必要です。セットアップ後の.venv/bin/pythonを使用してください') from exc
    font = ImageFont.truetype(str(FONT_PATH), max(1, round(config['subtitle']['font_size'])))
    # libassは行高にfontsizeを適用するため、このem幅での検証は保守的になる。
    available = config['width'] * 0.92 - config['subtitle']['font_size'] * 0.12
    for caption in config['captions']:
        for line in caption['text'].splitlines():
            if font.getlength(line) > available:
                raise ValueError(f'字幕が横幅を超えます。max_chars/font_sizeを小さくしてください: {line}')


def audio_filter(config):
    info, channel = config['info'], config['channel']
    if not info['audio']:
        return 'aformat=sample_rates=48000:channel_layouts=stereo', '無音音声を付与'
    if channel == 'auto' and info['channels'] == 2:
        result = run(['ffmpeg', '-hide_banner', '-nostdin', '-i', config['source'], '-vn', '-af',
                      'astats=measure_perchannel=RMS_level:measure_overall=none', '-f', 'null', '-'])
        pairs = re.findall(r'Channel:\s*(\d+).*?RMS level dB:\s*(-?inf|[-\d.]+)', result.stderr, re.S)
        levels = {int(k): float(v) for k, v in pairs}
        left, right = levels.get(1, -math.inf), levels.get(2, -math.inf)
        if left < -65 and right > -50:
            channel = 'right'
        elif right < -65 and left > -50:
            channel = 'left'
    if channel == 'left':
        return 'pan=stereo|c0=c0|c1=c0,aresample=48000', '左チャンネルを両側へ'
    if channel == 'right':
        return 'pan=stereo|c0=c1|c1=c1,aresample=48000', '右チャンネルを両側へ'
    return 'aresample=48000,aformat=channel_layouts=stereo', 'ステレオ（モノラルは両側へ）'


def render(config, overwrite=False):
    if config['plan'].get('reviewed') is not True:
        raise ValueError('計画を確認し reviewed:true にしてください。言い間違い・語欠け・意味の通りはAI/人の確認が必要です')
    check_caption_width(config)
    if not overwrite:
        existing = [p for p in config['targets'] if p.exists()]
        if existing:
            raise ValueError(f'出力済みです。別名を使うか --overwrite を指定してください: {existing[0]}')
    out = config['output']
    out.parent.mkdir(parents=True, exist_ok=True)
    audio_chain, channel_note = audio_filter(config)
    with tempfile.TemporaryDirectory(prefix='.ai-video-', dir=out.parent) as td:
        temp = Path(td)
        fonts = temp / 'fonts'
        fonts.mkdir()
        if config['captions']:
            shutil.copy2(FONT_PATH, fonts / FONT_PATH.name)
        write_subtitles(config, temp / 'subs.ass', temp / 'subs.srt')
        width, height, fps = config['width'], config['height'], config['fps']
        if config['fit'] == 'contain':
            fit = f'scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2'
        else:
            fit = f'scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2,crop={width}:{height}'
        listing = []
        for i, seg in enumerate(config['segments']):
            print(f"区間 {i + 1}/{len(config['segments'])}: {seg['start']:.3f}〜{seg['end']:.3f}s", flush=True)
            samples = round(seg['duration'] * 48000)
            fade = min(0.01, seg['duration'] / 4)
            command = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-nostdin',
                       '-ss', f"{seg['start']:.9f}", '-t', f"{seg['duration'] + 1 / fps:.9f}", '-i', config['source']]
            if not config['info']['audio']:
                command += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
            audio_input = '0:a:0' if config['info']['audio'] else '1:a:0'
            filters = (f"[0:v:0]setpts=PTS-STARTPTS,fps={fps},{fit},setsar=1,tpad=stop_mode=clone:stop_duration=0.1,trim=end_frame={seg['frames']},setpts=PTS-STARTPTS[v];"
                       f'[{audio_input}]{audio_chain},asetpts=PTS-STARTPTS,apad=whole_len={samples},atrim=end_sample={samples},'
                       f'afade=t=in:st=0:d={fade},afade=t=out:st={seg["duration"] - fade}:d={fade}[a]')
            name = f'seg{i:05d}.mkv'
            command += ['-filter_complex', filters, '-map', '[v]', '-map', '[a]',
                        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
                        '-c:a', 'pcm_s16le', temp / name]
            run(command)
            listing.append(f"file '{name}'\nduration {seg['duration']:.9f}\n")
        (temp / 'concat.txt').write_text(''.join(listing), encoding='utf-8')
        command = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-nostdin', '-f', 'concat', '-safe', '1', '-i', 'concat.txt']
        if config['captions'] and config['subtitle']['burn_in']:
            command += ['-vf', 'ass=filename=subs.ass:fontsdir=fonts']
        command += ['-map', '0:v:0', '-map', '0:a:0', '-c:v', 'libx264', '-preset', 'medium', '-crf', '19',
                    '-pix_fmt', 'yuv420p', '-r', str(fps), '-af', f'loudnorm=I={config["loudness"]}:TP=-1.5:LRA=11',
                    '-ar', '48000', '-ac', '2', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', 'finished.mp4']
        run(command, cwd=temp)
        result = probe(temp / 'finished.mp4')
        expected = sum(s['duration'] for s in config['segments'])
        expected_frames = sum(s['frames'] for s in config['segments'])
        if result['frames'] != expected_frames:
            raise ValueError(f'出力映像のフレーム数不一致: {result["frames"]} / {expected_frames}')
        if (result['video_duration'] is None or result['audio_duration'] is None
                or abs(result['video_duration'] - result['audio_duration']) > 2 / fps):
            raise ValueError('映像と音声の尺が一致しません')
        if abs(result['duration'] - expected) > max(0.15, 2 / fps):
            raise ValueError(f'出力尺が計画と不一致です: {result["duration"]:.3f} / {expected:.3f}')
        report = {'schema_version': 1, 'duration': result['duration'], 'expected_duration': expected,
                  'preview_seconds': config['preview'], 'segment_count': len(config['segments']),
                  'caption_count': len(config['captions']),
                  'caption_timing_adjustments': sum(bool(c.get('timing_adjusted')) for c in config['captions']),
                  'subtitle_burned_in': bool(config['captions']) and config['subtitle']['burn_in'],
                  'segments': config['segments'],
                  'audio': channel_note, 'checks': {'duration': 'passed', 'caption_width': 'passed', 'frame_count': 'passed', 'av_duration': 'passed'},
                  'frames': result['frames'], 'video_duration': result['video_duration'], 'audio_duration': result['audio_duration'],
                  'human_review': ['全編の意味・誤字', '継ぎ目の語頭/語尾と音の自然さ', '字幕が顔や資料を隠さないか']}
        (temp / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        for src, dest in zip([temp / 'finished.mp4', temp / 'subs.srt', temp / 'subs.ass', temp / 'report.json'], config['targets']):
            # 最後まで成功した結果だけを納品先へ移す。
            if not overwrite and dest.exists():
                raise ValueError(f'処理中に出力先が作成されました: {dest}')
            os.replace(src, dest)
    print(f'完成: {out} ({expected:.2f}秒)')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan')
    parser.add_argument('mode', choices=['check', 'render'])
    parser.add_argument('--preview-seconds', type=float)
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    try:
        config = load_plan(args.plan, args.preview_seconds)
        check_caption_width(config)
        print(f"計画: {len(config['segments'])}区間 / {sum(s['duration'] for s in config['segments']):.2f}秒 / 字幕{len(config['captions'])}枚 / {config['width']}x{config['height']}")
        if config['plan'].get('reviewed') is not True:
            print('未レビュー: checkは入力検証です。意味・音声境界を確認してreviewed:trueにしてください')
        if args.mode == 'render':
            render(config, args.overwrite)
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(1, f'エラー: {exc}\n')


if __name__ == '__main__':
    import _venv  # noqa: F401  同梱仮想環境があれば切り替える
    main()
