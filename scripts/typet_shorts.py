#!/usr/bin/env python3
"""Type-T ショート動画ビルダー（汎用・spec駆動）。

長尺動画のクリップ（segs）を切り出し→縦型化（ぼかし背景＋メイン映像）→ Type-T テロップ
（テレビ風テロップ Type-T: 丸ゴBlack・三重フチ＋硬い影・色＝心理役割・角丸座布団・
強調ワードの1文字バウンス）を焼き込んで `output/<sub>/<id>_typet.mp4` を書き出す。
一連の手順は references/editing-guide.md を参照。

使い方:
  python3 scripts/typet_shorts.py <spec.py> words  [clip id...]   # テロップ台本用に発話語の時刻を表示
  python3 scripts/typet_shorts.py <spec.py> ass    [clip id...]   # ASS のみ生成
  python3 scripts/typet_shorts.py <spec.py> check  [clip id...]   # グレー背景に全テロップを焼いてシート化（本レンダ前の検証）
  python3 scripts/typet_shorts.py <spec.py> main   [clip id...]   # 本レンダリング
  python3 scripts/typet_shorts.py <spec.py> frames [clip id...]   # 完成動画から全テロップ時刻のフレームをシート化（検収）
  python3 scripts/typet_shorts.py <spec.py> seams  [clip id...]   # カット点の音響解決表と、完成動画の継ぎ目監査
  （spec.py 自身を実行しても同じ: python3 scripts/spec_xxx.py main）

spec.py が定義するもの（テンプレは references/editing-guide.md 付録）:
  SRC        元動画パス
  WHISPER    whisper_verbose.json のパス（words モードで使用）
  OUT_DIR    出力先（例: BASE/'output'/'xxx'）
  LAYOUTS    {'name': ('crop=W:H:X:Y', scale_w, scale_h)}  メイン映像のクロップ→幅1080スケール
  CLIPS      [dict(id, title('/'改行), layout, segs=[(src_in, src_out[, layout]), ...],
                   telops=[(src_start, style, text[, src_end]), ...])]
  任意: BLOCK_BOTTOMS={'layout': y}（本文ブロック下端。既定=映像下端+256）、TITLE_POS=(540,250)、
        TITLE_FS_MAX=130（タイトル自動サイズの上限）、FONTS_DIR、TMP_DIR
        PRECROP=True … クロップ→スケールを Pass1（区間切り出し）側で行う。ぼかし背景もクロップ後の映像から
                       作られ（元フレームの黒地・バナーが背景に写り込まない）、segs の第3要素で区間ごとに
                       レイアウトを切り替えられる（出力サイズ (scale_w, scale_h) は clip の layout と同一であること）。
                       縦型ソース（黒地にパネル埋め込みの配信画面）向け。既定 False＝従来どおり Pass2 でクロップ
                       PRECROP 時、crop 文字列中の `{a}` は区間の開始秒（source）に置換される。ffmpeg の式変数 t は
                       区間内の相対秒なので `t+{a}` が元動画秒になり、レイアウトが動く瞬間をまたぐ区間で
                       クロップ位置を時刻追従させられる（例: y=if(lt(t+{a},1089.55),457,329)。カンマを含む式は ' ' で囲む）
  clip 任意: title_fs（タイトル px を固定）、title_pos、block_bottom

尺: 1本 30〜90 秒（視聴者が1本として違和感なく理解できる長さを優先し、60秒に寄せない。90.5秒超は assert）。
タイトル: 最長行が幅 TITLE_MAX_WIDTH(980px) に収まる最大サイズ（上限 TITLE_FS_MAX）に自動で拡大する。
カット点: segs の値は「意図」。実際のカット点は scripts/seams.py が元音声の無音（語間の閉鎖・ポーズ）へ寄せて確定する
  （継ぎ目のノイズ対策）。ass/seams モードで動かした先と警告が出る。spec に RESOLVE_CUTS = False で無効化。
音声: Pass1 で区間ごとに AAC にすると各区間が 1024 サンプル単位に丸められ、継ぎ目に 13〜30ms の穴が空く。
  そこで映像だけ区間ごとに再エンコードし、音声は 1 本の filter_complex で元音声から切り出して PCM で結合し、
  最終エンコードで初めて AAC にする（音声は 1 回しか劣化しない）。区間の尺は 1/30 秒格子に量子化し、映像 n フレーム＝
  音声 n×1470 サンプルで継ぎ目を一致させる。
用語集: data/glossary.tsv の「誤変換」列の語がタイトル・テロップ・区間内の語に含まれると警告する（同音異義語の見落とし対策）。
"""

import _venv  # noqa: F401  .venv があればその python で再実行する（PIL 等の依存は .venv 側）

import importlib.util
import itertools
import math
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seams  # noqa: E402  カット点の音響解決・継ぎ目監査

BASE = Path(__file__).resolve().parent.parent
W, H = 1080, 1920
MARGIN_LR = 50                      # 左右余白50px（有効幅980）
FG_CENTER_Y = 725                   # メイン映像の中心y（縦型ショートの既定レイアウト）
BLOCK_BOTTOM_BELOW_FG = 256         # 本文ブロック下端 = 映像下端 + 256px
DEFAULT_TITLE_POS = (540, 250)      # タイトル（フック様式）中心。上部ぼかし帯内
TITLE_MAX_WIDTH = 980               # タイトル最長行の上限幅（左右余白 50px。黄フチ15px込みで余白≈35px）
DEFAULT_TITLE_FS_MAX = 130          # タイトル自動サイズの上限 px（2行 260px が上部帯 0〜466 に収まる）
DUR_MIN, DUR_MAX = 30.0, 90.0       # 1本の尺の許容範囲（秒）。理解しやすい長さを優先し 60 秒に寄せない
EMPH = 1.2                          # 強調ワードの拡大率
STAGGER_MS = 45                     # 1文字ずつのバウンス時間差

# ---- フォント（assets/fonts/ 同梱分は ffmpeg の ass フィルタへ fontsdir で渡す） ----
DEFAULT_FONTS_DIR = BASE / 'assets' / 'fonts'
FONT_BODY = 'Rounded Mplus 1c Black'      # libass は fullname でマッチさせる
FONT_POP = 'Mochiy Pop One'               # リアクション・つなぎ語
FONT_MINCHO = 'Shippori Mincho B1 ExtraBold'
PATH_MINCHO = str(DEFAULT_FONTS_DIR / 'ShipporiMinchoB1-ExtraBold.ttf')

# ---- テレビ風カラートークン（ASS は &HAABBGGRR） ----
C_WHITE = '&H00FFFFFF'
C_BLACK = '&H00000000'
C_YELLOW = '&H0000F1FF'     # #FFF100 強調ワード・ベネフィット
C_RED = '&H001200E6'        # #E60012 主張・事件
C_BLUE = '&H00FF5B2D'       # #2D5BFF 痛み（青）
C_PURPLE = '&H00E22B8A'     # #8A2BE2 痛み（紫）
C_AZURE = '&H00D47D1A'      # #1A7DD4 権威・実績
C_GOLD = '&H0000D2FF'       # #FFD200 転換のキーワード
BOX_RED = '1200E6'          # \1c 用 BGR
BOX_BLUE = 'D96F1E'         # #1E6FD9

# ---- スタイルレシピ（fs は「見た目の em(px)」。libass へは K を掛けて渡す） ----
RECIPES = {
    'Hook':       dict(fs=92, font='Mincho', fill=C_WHITE, inner=(C_BLACK, 7),
                       outer=(C_YELLOW, 15), shad=6, anim='slam'),
    'Neutral':    dict(fs=85, font='Body', fill=C_WHITE, inner=None,
                       outer=(C_BLACK, 9), shad=6, anim='cut', emph=C_YELLOW),
    'Connector':  dict(fs=66, font='Pop', fill=C_WHITE, inner=None,
                       outer=(C_BLACK, 9), shad=5, anim='slamshake'),
    'PainBlue':   dict(fs=85, font='Body', fill=C_BLUE, inner=(C_WHITE, 5),
                       outer=(C_BLACK, 12), shad=6, anim='slam'),
    'PainPurple': dict(fs=85, font='Body', fill=C_PURPLE, inner=(C_WHITE, 5),
                       outer=(C_BLACK, 12), shad=6, anim='slam'),
    'ClaimRed':   dict(fs=85, font='Body', fill=C_RED, inner=(C_WHITE, 5),
                       outer=(C_BLACK, 12), shad=6, anim='slam'),
    'Authority':  dict(fs=85, font='Body', fill=C_AZURE, inner=(C_WHITE, 5),
                       outer=(C_BLACK, 12), shad=6, anim='slam'),
    'Yellow':     dict(fs=85, font='Body', fill=C_YELLOW, inner=None,
                       outer=(C_BLACK, 10), shad=6, anim='slam'),
    'Gold':       dict(fs=100, font='Body', fill=C_GOLD, inner=(C_WHITE, 5),
                       outer=(C_BLACK, 12), shad=7, glow=(C_WHITE, 18, 10),
                       anim='pop'),
    'BoxText':    dict(fs=78, font='Body', fill=C_WHITE, inner=None, outer=None, shad=0),
}
BOX_STYLES = {'BoxRed': BOX_RED, 'BoxBlue': BOX_BLUE}
BOX_TILTS = {'BoxRed': [-2.5, 2, -2, 2], 'BoxBlue': [-2, 2, -2, 2]}


# ================= spec の読み込み =================
def load_spec(path):
    p = Path(path).resolve()
    spec = importlib.util.spec_from_file_location(p.stem, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for key in ('SRC', 'WHISPER', 'OUT_DIR', 'TMP_DIR', 'FONTS_DIR', 'GLOSSARY'):
        if hasattr(mod, key):
            value = Path(getattr(mod, key)).expanduser()
            setattr(mod, key, (p.parent / value).resolve() if not value.is_absolute() else value.resolve())
    return mod


def validate_spec(spec, targets):
    """書き出し前に素材、区間、識別子を検証する。Python spec自体は信頼したコードのみを使う。"""
    if not Path(spec.SRC).is_file():
        raise ValueError(f'素材が見つかりません: {spec.SRC}')
    meta = json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(spec.SRC)]).stdout)
    if not any(s['codec_type'] == 'video' for s in meta['streams']):
        raise ValueError('映像ストリームがありません')
    if not any(s['codec_type'] == 'audio' for s in meta['streams']):
        raise ValueError('Type-Tには音声付き素材が必要です。音声なしは edit_video.py を使ってください')
    duration = float(meta['format']['duration'])
    ids = [c['id'] for c in spec.CLIPS]
    if not ids or len(ids) != len(set(ids)) or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', str(i)) for i in ids):
        raise ValueError('clip idは重複しない英数字・ハイフン・アンダースコアで指定してください')
    if set(targets) - set(ids):
        raise ValueError('不明なclip id: ' + ', '.join(sorted(set(targets) - set(ids))))
    for clip in spec.CLIPS:
        if clip['id'] not in targets:
            continue
        if not clip.get('segs'):
            raise ValueError('segsには1つ以上の採用区間が必要です')
        if out_path(spec, clip).resolve() == Path(spec.SRC).resolve():
            raise ValueError('素材を出力で上書きすることはできません')
        for seg in clip['segs']:
            a, b = seg[:2]
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (a, b)) or not 0 <= a < b <= duration + 0.01:
                raise ValueError(f'不正な区間: {seg}')
            lay = seg[2] if len(seg) > 2 else clip['layout']
            if lay not in spec.LAYOUTS:
                raise ValueError(f'未定義のレイアウト: {lay}')
        texts = [clip['title']]
        for event in clip['telops']:
            if len(event) not in (3, 4) or event[1] not in (set(RECIPES) | set(BOX_STYLES)):
                raise ValueError(f'不正なテロップ定義: {event}')
            for t in [event[0]] + ([event[3]] if len(event) == 4 else []):
                if not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 <= t <= duration:
                    raise ValueError(f'テロップ時刻が素材範囲外: {t}')
            texts.append(event[2])
        if any(any(ch in str(t) for ch in ('{', '}', '\\', '\r', '\n')) for t in texts):
            raise ValueError('テロップにはASS制御文字を直接入れず、改行は /、強調は [ ] を使ってください')


def audio_channel_filter(spec, clip):
    """片chが無音の素材は有音chを複製。明示指定も可能。"""
    choice = getattr(spec, 'AUDIO_CHANNEL', 'auto')
    if choice not in {'auto', 'left', 'right', 'stereo'}:
        raise ValueError('AUDIO_CHANNELはauto/left/right/stereoのいずれかです')
    if choice == 'auto':
        start = clip['segs'][0][0]
        result = run(['ffmpeg', '-hide_banner', '-ss', str(start), '-i', str(spec.SRC), '-t', '30',
                      '-vn', '-af', 'astats=measure_perchannel=RMS_level:measure_overall=none', '-f', 'null', '-'])
        values = re.findall(r'RMS level dB:\s*(-?inf|[-\d.]+)', result.stderr)
        if len(values) == 2:
            left, right = map(float, values)
            if right > -60 and right - left > 30:
                choice = 'right'
            elif left > -60 and left - right > 30:
                choice = 'left'
    if choice == 'left':
        return 'pan=stereo|c0=c0|c1=c0,'
    if choice == 'right':
        return 'pan=stereo|c0=c1|c1=c1,'
    return ''


def _font_paths(spec):
    fd = Path(getattr(spec, 'FONTS_DIR', DEFAULT_FONTS_DIR))
    return fd, {'Body': fd / 'MPLUSRounded1c-Black.ttf',
                'Pop': fd / 'MochiyPopOne-Regular.ttf',
                'Mincho': Path(PATH_MINCHO) if PATH_MINCHO else None}


# ================= 共通ユーティリティ =================
def ass_time(sec):
    sec = max(0.0, sec)
    h = int(sec // 3600); m = int(sec % 3600 // 60); s = sec % 60
    return f'{h}:{m:02d}:{s:05.2f}'


def run(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        print(r.stderr[-3000:]); sys.exit(1)
    return r


def load_words(path):
    return json.load(open(path))['words']


def load_glossary(spec=None):
    """data/glossary.tsv → [(正しい表記, [誤変換...], 備考)]（誤変換列が空の行は除く）"""
    p = Path(getattr(spec, 'GLOSSARY', BASE / 'data' / 'glossary.tsv'))
    out = []
    if not p.exists():
        return out
    for ln in p.read_text(encoding='utf-8').splitlines():
        if not ln.strip() or ln.startswith('#'):
            continue
        cols = (ln.split('\t') + ['', '', '', ''])[:4]
        miscs = [m.strip() for m in cols[2].split(',') if m.strip()]
        if miscs:
            out.append((cols[0].strip(), miscs, cols[3].strip()))
    return out


def glossary_warnings(texts, label, spec=None):
    """テキスト群に用語集の誤変換が含まれていれば警告文を返す"""
    warns = []
    for term, miscs, note in load_glossary(spec):
        for m in miscs:
            for t in texts:
                if m in t:
                    warns.append(f'誤変換の可能性「{m}」→「{term}」（{note}）: {label}「{plain(t)}」')
    return warns


def resolved_segs(clip, spec):
    """カット点を音響で確定した区間（seams.resolve_segs）。[(a, b, layout), ...] と生の解決結果"""
    segs = seams.resolve_segs(clip, spec)
    return [(s['a'], s['b'], s['layout']) for s in segs], segs


def words_in(words, a, b):
    return [w for w in words if w['end'] > a + 0.02 and w['start'] < b - 0.02]


def make_mapper(segs):
    """source秒 → 出力タイムライン秒（segs の区分写像。カット除去時刻は境界にクランプ）"""
    subs, acc = [], 0.0
    for a, b, *_ in segs:
        subs.append((a, b, acc)); acc += b - a
    total = acc

    def m(t_src):
        # まず区間内かを全区間で調べる（segs は時系列順とは限らない。後半で source を遡る構成でも正しく写像する）
        for a, b, off in subs:
            if a <= t_src <= b:
                return off + (t_src - a)
        # カット除去区間の時刻: その時刻より後に始まる最初の区間の頭（＝直前区間の尻）へクランプ
        for a, b, off in subs:
            if t_src < a:
                return off
        return total
    return m, total


def layout_geom(spec, clip):
    crop, fw, fh = spec.LAYOUTS[clip['layout']]
    fg_y = FG_CENTER_Y - fh // 2
    bb = (clip.get('block_bottom')
          or getattr(spec, 'BLOCK_BOTTOMS', {}).get(clip['layout'])
          or fg_y + fh + BLOCK_BOTTOM_BELOW_FG)
    tp = clip.get('title_pos') or getattr(spec, 'TITLE_POS', DEFAULT_TITLE_POS)
    return crop, fw, fh, fg_y, bb, tp


# ================= フォント実測 =================
# 【罠】libass の \fs は em ではなく「行高 = ascender+descender」。丸ゴ Black は 1.40em、
# Mochiy は 1.45em なので、レシピの fs（見た目の em）に K を掛けて \fs へ渡す。
class Metrics:
    def __init__(self, font_paths):
        self.paths = font_paths
        self.cache = {}

    def font(self, fs, font='Body'):
        key = (font, fs)
        if key not in self.cache:
            self.cache[key] = ImageFont.truetype(str(self.paths[font]), fs)
        return self.cache[key]

    def k_ratio(self, font):
        if self.paths.get(font) is None:
            return 1.0
        asc, desc = self.font(1000, font).getmetrics()
        return (asc + desc) / 1000

    def ass_fs(self, fs, font):
        return round(fs * self.k_ratio(font))

    def line_height(self, fs, font='Body'):
        return self.ass_fs(fs, font)

    def baseline_off(self, fs, font='Body'):
        asc, desc = self.font(fs, font).getmetrics()
        return (asc - desc) / 2

    def line_width(self, line, fs, font):
        """[ ] 強調の拡大込みで1行の実描画幅（em基準）を返す"""
        w = 0.0
        for txt, is_e in parse_segments(line):
            w += self.font(round(fs * EMPH) if is_e else fs, font).getlength(txt)
        return w


def parse_segments(line):
    segs = []
    for part in re.split(r'(\[[^\]]+\])', line):
        if not part:
            continue
        if part.startswith('[') and part.endswith(']'):
            segs.append((part[1:-1], True))
        else:
            segs.append((part, False))
    return segs


def plain(text):
    return text.replace('[', '').replace(']', '')


# ================= 演出プリセット（\t は event start 起点・ms） =================
def pop_tags(delay=0):
    d = int(delay)
    return (rf'\fscx0\fscy0\t({d},{d + 140},0.5,\fscx118\fscy118)'
            rf'\t({d + 140},{d + 230},\fscx94\fscy94)'
            rf'\t({d + 230},{d + 300},\fscx100\fscy100)')


SLAM = r'\fscx150\fscy150\t(0,120,1.3,\fscx100\fscy100)'
SLAMSHAKE = (SLAM + r'\t(120,160,\frz-2.5)\t(160,200,\frz2)'
             r'\t(200,240,\frz-1)\t(240,280,\frz0)')


def anim_tags(name):
    return {'cut': '', 'pop': pop_tags(), 'slam': SLAM, 'slamshake': SLAMSHAKE}[name]


def rounded_rect(w, h, r):
    w, h, r = round(w), round(h), round(r)
    k = round(r * 0.552)
    return (f'm {r} 0 l {w - r} 0 b {w - r + k} 0 {w} {r - k} {w} {r} '
            f'l {w} {h - r} b {w} {h - r + k} {w - r + k} {h} {w - r} {h} '
            f'l {r} {h} b {r - k} {h} 0 {h - r + k} 0 {h - r} '
            f'l 0 {r} b 0 {r - k} {r - k} 0 {r} 0')


# ================= ASS 生成 =================
def build_ass(clip, spec):
    """(ASSテキスト, 総尺, QC用イベント一覧[(out_start, out_end, style, text)], 警告一覧)"""
    _, fpaths = _font_paths(spec)
    M = Metrics(fpaths)
    crop, fw, fh, fg_y, BLOCK_BOTTOM, TITLE_POS = layout_geom(spec, clip)
    segs, raw = resolved_segs(clip, spec)
    m, TOTAL = make_mapper(segs)
    _, seam_warns = seams.describe(clip, raw)
    warnings = [f'継ぎ目: {w}' for w in seam_warns]
    warnings += glossary_warnings([clip['title']] + [e[2] for e in clip['telops']], 'テロップ', spec)
    qc_events = []

    styles = [('Body', FONT_BODY, 85), ('Pop', FONT_POP, 66), ('Mincho', FONT_MINCHO, 92)]
    lines = [
        '[Script Info]', 'ScriptType: v4.00+', f'PlayResX: {W}', f'PlayResY: {H}',
        'WrapStyle: 2', 'ScaledBorderAndShadow: yes', '', '[V4+ Styles]',
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, '
        'OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, '
        'ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, '
        'Alignment, MarginL, MarginR, MarginV, Encoding',
    ]
    for name, font, size in styles:
        lines.append(f'Style: {name},{font},{size},{C_WHITE},{C_WHITE},{C_BLACK},'
                     f'&H80000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1')
    lines += ['', '[Events]',
              'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Text']

    def ev(layer, start, end, style, text):
        lines.append(f'Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,{text}')

    def text_layers(rc, fill):
        out, L = [], 0
        if rc.get('glow'):
            gc, gb, gblur = rc['glow']
            out.append((L, rf'\1c{fill}\3c{gc}\bord{gb}\shad0\blur{gblur}')); L += 1
        if rc.get('outer'):
            oc, ob = rc['outer']
            out.append((L, rf'\1c{fill}\3c{oc}\bord{ob}\shad{rc["shad"]}\4c{C_BLACK}\4a&H30&')); L += 1
        if rc.get('inner'):
            ic, ib = rc['inner']
            out.append((L, rf'\1c{fill}\3c{ic}\bord{ib}\shad0')); L += 1
        out.append((L, rf'\1c{fill}\bord0\shad0'))
        return out

    def inline(line, fs, fs_e, fill, emph_fill):
        out = ''
        for txt, is_e in parse_segments(line):
            out += (rf'{{\fs{fs_e}\1c{emph_fill}}}{txt}{{\fs{fs}\1c{fill}}}' if is_e else txt)
        return out

    def check_width(text, rc, label):
        tls = text.split('/')
        for ln in tls:
            wpx = M.line_width(ln, rc['fs'], rc['font'])
            if wpx > W - 2 * MARGIN_LR:
                warnings.append(f'幅超過 {wpx:.0f}px>{W - 2 * MARGIN_LR}: 「{ln}」({label})')
        if len(tls) > 2 and rc is not RECIPES['Hook']:
            warnings.append(f'{len(tls)}行（顎に掛かる恐れ。2テロップに分割推奨）: 「{text}」({label})')

    def emit_block(s, e, text, rc, anim, cx, cy, base, extra='', emph_color=None):
        font = rc['font']
        fs, fs_e = M.ass_fs(rc['fs'], font), M.ass_fs(round(rc['fs'] * EMPH), font)
        for L, tags in text_layers(rc, rc['fill']):
            body = r'\N'.join(inline(ln, fs, fs_e, rc['fill'], emph_color or rc['fill'])
                              for ln in text.split('/'))
            ev(base + L, s, e, font, rf'{{\an5\pos({cx},{cy})\fs{fs}{tags}{anim}{extra}}}{body}')

    def emit_perchar(s, e, text, rc, cx=540):
        """白行の強調ワード用: 全文字を個別イベントにし、強調文字だけ時間差バウンス"""
        font = rc['font']
        fs = rc['fs']; fs_e = round(fs * EMPH)
        f, fe = M.font(fs, font), M.font(fs_e, font)
        tls = text.split('/'); n = len(tls)
        LP = M.line_height(fs, font)
        block_cy = BLOCK_BOTTOM - n * LP / 2
        dy_e = M.baseline_off(fs, font) - M.baseline_off(fs_e, font)
        for i, ln in enumerate(tls):
            cy = block_cy + (i - (n - 1) / 2) * LP
            items = []
            for txt, is_e in parse_segments(ln):
                ff = fe if is_e else f
                for ch in txt:
                    items.append((ch, is_e, ff.getlength(ch)))
            x = cx - sum(a for _, _, a in items) / 2
            k = 0
            for ch, is_e, adv in items:
                ccx = x + adv / 2; x += adv
                if is_e:
                    size, y, fill = fs_e, cy + dy_e, rc.get('emph') or rc['fill']
                    anim = pop_tags(k * STAGGER_MS); k += 1
                else:
                    size, y, fill, anim, k = fs, cy, rc['fill'], '', 0
                for L, tags in text_layers(rc, fill):
                    ev(10 + L, s, e, font,
                       rf'{{\an5\pos({ccx:.1f},{y:.1f})\fs{M.ass_fs(size, font)}{tags}{anim}}}{ch}')

    def emit_box(s, e, text, color_bgr, rc_text, *, cx=540, bottom=None, tilt=0.0,
                 padx=40, pady=16, radius=18, shadow=(6, 8), base=4):
        """角丸座布団＋影＋テキスト（同じ pos 中心・同じアニメで剛体として動く）"""
        fs, font = rc_text['fs'], rc_text['font']
        tls = text.split('/')
        width = max(M.font(fs, font).getlength(plain(ln)) for ln in tls)
        LP = M.line_height(fs, font)
        bw, bh = width + 2 * padx, len(tls) * LP + 2 * pady
        cy = bottom + pady - bh / 2
        anim = pop_tags()
        tilt_tag = rf'\frz{tilt}' if tilt else ''
        shape = rounded_rect(bw, bh, radius)
        sx, sy = shadow
        ev(base, s, e, 'Body',
           rf'{{\an5\pos({cx + sx},{round(cy + sy)})\1c&H000000&\1a&H70&\bord0\shad0{anim}{tilt_tag}\p1}}{shape}{{\p0}}')
        ev(base + 1, s, e, 'Body',
           rf'{{\an5\pos({cx},{round(cy)})\1c&H{color_bgr}&\bord0\shad0{anim}{tilt_tag}\p1}}{shape}{{\p0}}')
        emit_block(s, e, text, rc_text, anim, cx, round(cy), base + 2, extra=tilt_tag)

    if not (DUR_MIN <= TOTAL <= DUR_MAX):
        warnings.append(f'尺 {TOTAL:.1f}s が許容範囲 {DUR_MIN:.0f}〜{DUR_MAX:.0f}s の外')

    # タイトル＝フック様式（三重フチ・スラム）。上部ぼかし帯に全編表示。
    # サイズは最長行が TITLE_MAX_WIDTH に収まる最大値（上限 TITLE_FS_MAX）に自動で拡大する
    title = clip['title'].replace('\\N', '/')
    rc = RECIPES['Hook']
    fs_max = getattr(spec, 'TITLE_FS_MAX', DEFAULT_TITLE_FS_MAX)
    title_fs = clip.get('title_fs')
    if not title_fs:
        w1 = max(M.line_width(ln, 100, rc['font']) / 100 for ln in title.split('/'))   # 1px あたりの幅
        title_fs = min(fs_max, int(TITLE_MAX_WIDTH / w1)) if w1 > 0 else fs_max
    if title_fs < 100:
        warnings.append(f'タイトルが小さくなる fs{title_fs}（行を短く: 1行 8 文字目安）: 「{title}」')
    rc = dict(rc, fs=title_fs)
    check_width(title, rc, 'title')
    emit_block(0.0, TOTAL, title, rc, anim_tags(rc['anim']), TITLE_POS[0], TITLE_POS[1], 10)

    events = clip['telops']
    tilt_iter = {k: itertools.cycle(v) for k, v in BOX_TILTS.items()}
    starts = [m(e[0]) for e in events]
    for i, e in enumerate(events):
        style, text = e[1], e[2]
        s = starts[i]
        if len(e) >= 4:
            end = min(m(e[3]), TOTAL)
        elif i + 1 < len(events):
            end = starts[i + 1]
        else:
            end = TOTAL
        if end <= s:
            warnings.append(f'表示尺0（カット除去区間の時刻?）: 「{text}」 src={e[0]}')
            continue
        qc_events.append((s, end, style, text))
        if style in BOX_STYLES:
            check_width(text, RECIPES['BoxText'], style)
            emit_box(s, end, text, BOX_STYLES[style], RECIPES['BoxText'],
                     bottom=BLOCK_BOTTOM, tilt=next(tilt_iter[style]))
        else:
            rc = RECIPES[style]
            check_width(text, rc, style)
            anim = rc['anim']
            if style == 'Neutral' and '[' in text and anim == 'cut':
                emit_perchar(s, end, text, rc)
            else:
                n = len(text.split('/'))
                cy = BLOCK_BOTTOM - n * M.line_height(rc['fs'], rc['font']) / 2
                emit_block(s, end, text, rc, anim_tags(anim), 540, round(cy), 10)
    return '\n'.join(lines) + '\n', TOTAL, qc_events, warnings


# ================= レンダリング =================
def tmp_dir(spec, clip):
    root = Path(getattr(spec, 'TMP_DIR', BASE / 'data' / 'tmp'))
    return root / f"{clip['id']}_typet"


def out_path(spec, clip):
    return Path(spec.OUT_DIR) / f"{clip['id']}_typet.mp4"


def write_ass(clip, spec, verbose=True):
    tmp = tmp_dir(spec, clip); tmp.mkdir(parents=True, exist_ok=True)
    text, total, qc, warns = build_ass(clip, spec)
    ass_path = tmp / 'subs.ass'
    ass_path.write_text(text, encoding='utf-8')
    if verbose:
        print(f"{clip['id']}: ASS生成 {ass_path}（尺 {total:.2f}s・テロップ {len(qc)} 枚）")
        moved, _ = seams.describe(clip, seams.resolve_segs(clip, spec))
        if moved:
            print(f'  カット点の調整 {len(moved)} 箇所（詳細は seams モード）')
        for w in warns:
            print(f'  警告: {w}')
    return ass_path, total, qc, warns


def pass1_concat(clip, spec, tmp):
    """区間切り出し。映像: 区間ごとに再エンコード（フレーム精度・フレーム数 n を固定）→ concat demuxer で無劣化結合。
    音声: 全区間を 1 本の filter_complex で元音声から切り出し（頭尻 10ms フェード・最終区間の尻は 200ms）→ PCM で結合。
    区間ごとに AAC にすると 1024 サンプル単位の丸めで継ぎ目に穴が空くため、AAC 化は最終エンコードの 1 回だけにする。
    戻り: (base_v.mp4, base_a.wav)"""
    concat_list = []
    precrop = getattr(spec, 'PRECROP', False)
    _, fw, fh, _, _, _ = layout_geom(spec, clip)
    segs, _ = resolved_segs(clip, spec)
    for i, (a, b, lay) in enumerate(segs):
        n = round((b - a) * 30)
        dur = n / 30
        vf = 'fps=30,setsar=1'
        if precrop:
            # 区間ごとのレイアウト（第3要素）。無ければ clip の layout
            crop, w, h = spec.LAYOUTS[lay]
            assert (w, h) == (fw, fh), \
                f"{clip['id']} seg{i}: layout '{lay}' の出力サイズ {(w, h)} が clip の {(fw, fh)} と異なる"
            # `{a}` = 区間開始秒。式中の t（区間内相対秒）+{a} で元動画の時刻を参照できる（レイアウト切替の追従用）
            crop = crop.replace('{a}', f'{a:.3f}')
            vf = f'{crop},scale={w}:{h}:flags=lanczos,fps=30,setsar=1'
        seg = tmp / f'seg{i:02d}.mp4'
        run(['ffmpeg', '-y', '-v', 'error', '-ss', f'{a:.4f}', '-i', str(spec.SRC), '-t', f'{dur + 0.5 / 30:.4f}',
             '-vf', vf, '-an', '-frames:v', str(n),
             '-c:v', 'libx264', '-crf', '18', '-preset', 'veryfast', str(seg)])
        nb = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=nb_frames',
                             '-of', 'csv=p=0', str(seg)], capture_output=True, text=True).stdout.strip()
        assert nb == str(n), f"{clip['id']} seg{i}: 映像フレーム数 {nb} != {n}"
        concat_list.append(f"file '{seg.name}'\n")
    (tmp / 'list.txt').write_text(''.join(concat_list))
    base_v = tmp / 'base_v.mp4'
    run(['ffmpeg', '-y', '-v', 'error', '-f', 'concat', '-safe', '0',
         '-i', str(tmp / 'list.txt'), '-c', 'copy', str(base_v)])
    # 音声: N 入力（-ss/-t は入力側＝サンプル精度）→ フェード → apad/atrim で尺を n/30 に固定 → concat → PCM
    cmd = ['ffmpeg', '-y', '-v', 'error']
    chains = []
    channel_filter = audio_channel_filter(spec, clip)
    for i, (a, b, _) in enumerate(segs):
        n = round((b - a) * 30)
        dur = n / 30
        cmd += ['-ss', f'{a:.4f}', '-t', f'{dur + 0.05:.4f}', '-i', str(spec.SRC)]
        fo = seams.END_FADE if i == len(segs) - 1 else seams.FADE
        ns = n * 1470    # 30fps 格子 × 44.1kHz = 1 フレーム 1470 サンプル（サンプル数で切って端数を出さない）
        chains.append(f'[{i}:a]{channel_filter}aresample=44100,aformat=channel_layouts=stereo,'
                      f'afade=t=in:st=0:d={seams.FADE},afade=t=out:st={max(0.0, dur - fo):.4f}:d={fo},'
                      f'apad=whole_len={ns},atrim=end_sample={ns},asetpts=PTS-STARTPTS[a{i}]')
    fc = ';'.join(chains) + ';' + ''.join(f'[a{i}]' for i in range(len(segs))) + f'concat=n={len(segs)}:v=0:a=1[a]'
    base_a = tmp / 'base_a.wav'
    run(cmd + ['-filter_complex', fc, '-map', '[a]', '-c:a', 'pcm_s16le', str(base_a)])
    total_n = sum(round((b - a) * 30) for a, b, _ in segs)
    samples = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=duration_ts',
                              '-of', 'csv=p=0', str(base_a)], capture_output=True, text=True).stdout.strip()
    assert samples == str(total_n * 1470), f"{clip['id']}: 音声サンプル数 {samples} != {total_n * 1470}（{total_n} フレーム）"
    return base_v, base_a


def render(clip, spec, overwrite=False):
    if out_path(spec, clip).exists() and not overwrite:
        raise ValueError("出力が既に存在します。確認して --overwrite を指定してください")
    planned = sum(b - a for a, b, _ in resolved_segs(clip, spec)[0])
    assert planned <= DUR_MAX + 0.5, f"{clip['id']} が{DUR_MAX:.0f}秒超過: {planned:.1f}s"
    tmp = tmp_dir(spec, clip)
    shutil.rmtree(tmp, ignore_errors=True)
    ass_path, total, qc, warns = write_ass(clip, spec)
    base_v, base_a = pass1_concat(clip, spec, tmp)
    crop, fw, fh, fg_y, _, _ = layout_geom(spec, clip)
    fonts_dir, _ = _font_paths(spec)
    # PRECROP 時は Pass1 でクロップ・スケール済み（背景もその映像から作る）
    fg_chain = 'null' if getattr(spec, 'PRECROP', False) else f'{crop},scale={fw}:{fh}:flags=lanczos'
    vf = (f'[0:v]split=2[bgs][fgs];'
          f'[bgs]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,'
          f'gblur=sigma=35,eq=brightness=-0.12:saturation=1.05[bg];'
          f'[fgs]{fg_chain}[fg];'
          f'[bg][fg]overlay=0:{fg_y}[vv];'
          f'[vv]ass=subs.ass:fontsdir=fonts[v]')
    (tmp / 'fonts').symlink_to(fonts_dir.resolve(), target_is_directory=True)
    out = out_path(spec, clip)
    out.parent.mkdir(parents=True, exist_ok=True)
    run(['ffmpeg', '-y', '-v', 'error', '-i', str(base_v), '-i', str(base_a),
         '-filter_complex', vf, '-map', '[v]', '-map', '1:a',
         '-af', 'loudnorm=I=-14:TP=-1.5:LRA=11',
         '-c:v', 'libx264', '-crf', '19', '-preset', 'medium', '-pix_fmt', 'yuv420p',
         '-ar', '48000', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(out)], cwd=tmp)   # loudnorm は内部 192kHz なので出力レートを明示
    d = subprocess.run(['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                        '-of', 'csv=p=0', str(out)], capture_output=True, text=True).stdout.strip()
    print(f"{clip['id']}: {float(d):.2f}s -> {out}")
    return out


# ================= 検証用シート =================
def _sample_times(qc, total):
    """各テロップの落ち着いた時刻（start+0.5s、短いものは中間）"""
    times = []
    for s, e, style, text in qc:
        t = s + min(0.5, max(0.1, (e - s) / 2))
        times.append((min(t, total - 0.05), style, text))
    return times


def _sheet(frames, path, fpaths, cols=6, thumb_w=216):
    th = thumb_w * H // W
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * thumb_w, rows * (th + 26)), (40, 40, 40))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(str(fpaths['Body']), 14)
    for i, (img_path, label) in enumerate(frames):
        im = Image.open(img_path).convert('RGB').resize((thumb_w, th))
        x, y = (i % cols) * thumb_w, (i // cols) * (th + 26)
        sheet.paste(im, (x, y))
        draw.text((x + 4, y + th + 4), label[:22], fill=(255, 255, 255), font=font)
    sheet.save(path)
    return path


def check_sheet(clip, spec):
    """グレー背景に ASS だけを焼き、全テロップの見た目・幅・位置をシートで確認（本レンダ前）"""
    ass_path, total, qc, warns = write_ass(clip, spec)
    fonts_dir, fpaths = _font_paths(spec)
    tmp = tmp_dir(spec, clip) / 'check'; tmp.mkdir(parents=True, exist_ok=True)
    font_link = tmp.parent / 'fonts'
    if not font_link.exists():
        font_link.symlink_to(fonts_dir.resolve(), target_is_directory=True)
    frames = []
    for i, (t, style, text) in enumerate(_sample_times(qc, total)):
        png = tmp / f'c{i:02d}.png'
        run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'color=c=0x777777:s={W}x{H}:d=0.1:r=30',
             '-vf', f'setpts=PTS+{t:.2f}/TB,ass=subs.ass:fontsdir=fonts', '-frames:v', '1', str(png)], cwd=tmp.parent)
        frames.append((png, f'{t:5.1f}s {style} {plain(text)}'))
    out = BASE / 'data' / 'frames' / f"{clip['id']}_typet_check.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    _sheet(frames, out, fpaths)
    print(f"{clip['id']}: 検証シート {out}")
    return out


def frames_sheet(clip, spec):
    """完成動画から全テロップ時刻のフレームを抜き、実背景での顔との重なり等を確認（検収）"""
    _, total, qc, _ = build_ass(clip, spec)
    _, fpaths = _font_paths(spec)
    video = out_path(spec, clip)
    tmp = tmp_dir(spec, clip) / 'real'; tmp.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, (t, style, text) in enumerate(_sample_times(qc, total)):
        png = tmp / f'r{i:02d}.png'
        run(['ffmpeg', '-y', '-v', 'error', '-ss', f'{t:.2f}', '-i', str(video), '-frames:v', '1', str(png)])
        frames.append((png, f'{t:5.1f}s {style} {plain(text)}'))
    out = BASE / 'data' / 'frames' / f"{clip['id']}_typet_real.png"
    _sheet(frames, out, fpaths)
    print(f"{clip['id']}: 検収シート {out}")
    return out


def dump_words(clip, spec):
    """テロップ台本を書くための発話語一覧（source秒|出力秒|語）。segごとに表示"""
    words = load_words(spec.WHISPER)
    gl = load_glossary(spec)
    off = 0.0
    print(f"=== {clip['id']} 「{clip['title']}」 ===")
    segs, raw = resolved_segs(clip, spec)
    for i, ((a, b, _), r) in enumerate(zip(segs, raw)):
        ws = words_in(words, a, b)
        print(f"--- seg{i} src {a:.2f}-{b:.2f}（spec {r['a0']:.2f}-{r['b0']:.2f}）→ out {off:.2f}-{off + b - a:.2f}")
        print('  ' + ' '.join(f"{w['start']:.2f}|{w['word']}" for w in ws))
        for w in ws:
            for term, miscs, note in gl:
                if any(m in w['word'] for m in miscs):
                    print(f"  ⚠ 誤変換の可能性 {w['start']:.2f}「{w['word']}」→「{term}」（{note}）")
        off += b - a
    print(f'TOTAL {off:.2f}s')


# ================= CLI =================
def main(spec, argv):
    modes = {'words', 'ass', 'check', 'main', 'frames', 'seams'}
    mode = next((a for a in argv if a in modes), 'main')
    overwrite = '--overwrite' in argv
    targets = [a for a in argv if a not in modes and a != '--overwrite'] or [c['id'] for c in spec.CLIPS]
    validate_spec(spec, targets)
    if mode == 'main' and not getattr(spec, 'REVIEWED', False):
        raise ValueError('カット・台本を確認し、specの REVIEWED = True を設定してください')
    Path(spec.OUT_DIR).mkdir(parents=True, exist_ok=True)
    for c in spec.CLIPS:
        if c['id'] not in targets:
            continue
        if mode == 'words':
            dump_words(c, spec)
        elif mode == 'ass':
            write_ass(c, spec)
        elif mode == 'check':
            check_sheet(c, spec)
        elif mode == 'main':
            render(c, spec, overwrite=overwrite)
        elif mode == 'frames':
            frames_sheet(c, spec)
        elif mode == 'seams':
            seams.main(spec, [c['id']])


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    try:
        main(load_spec(sys.argv[1]), sys.argv[2:])
    except (ValueError, FileNotFoundError) as exc:
        sys.exit(str(exc))
