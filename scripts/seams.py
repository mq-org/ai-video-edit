#!/usr/bin/env python3
"""カット点の音響解決と継ぎ目の監査（typet_shorts.py から呼ぶ。単体でも使える）。

背景（カットの継ぎ目に生じるノイズ）:
  spec の segs は Whisper の語タイムスタンプを見て人が決めるが、Whisper の語境界は実音声と ±0.1〜0.4 秒ずれる
  （start は遅れがち、end は尾を引いて遅れがち、ポーズをまたいで前後の語に吸収されることもある）。
  連続発話の中でカットすると、区間の頭に直前の語の母音の断片が入ったり（例「…ときに｜大切なのは」で「に」の尾が
  110ms 混入）、次の語の頭子音が欠けたり（「｜例えば」で立ち上がり 15ms を欠損、「｜自分なりに」で 170ms 欠損）して、
  継ぎ目で「プッ」と聞こえる。ここでは spec のカット点を「実音声の静かな区間（語間の閉鎖・ポーズ）」へ寄せて確定し、
  寄せられなければ警告する。spec の値は「意図」であり、最終的なカット点は音声で決まる。

方式:
  1. 「欲しい語」を決める。in 側は区間の最初のテロップの先頭 2 文字と Whisper 語列を照合（spec の値 −0.15〜+0.25s に
     始まる語）、無ければ start ≥ a−0.06 の最初の語（a+0.10 までに終わる短い語は飛ばす）。out 側は区間の最後のテロップの
     末尾 2 文字と照合（語の end が spec の値 ±0.35s で、その語の後にポーズがあるとき）、無ければ start ≤ b−0.03 の最後の語
     （語頭 0.18s 以内でカットしていて実音声がまだ続くなら、その語は不要とみなし前の語にする）
  2. 実音声の「島」（p90−15dB 以上の連続区間、30ms 未満の切れ目は結合）を作り、各島を「島の始まり（in 側）／終わり
     （out 側）に最も近い start／end を持つ語」に割り当てる（距離 0.40s 以内。島の 7 割以上が語の Whisper 区間に入って
     いればその語を優先。ピークが p90−6dB 未満の島は呼吸・相槌として無主）。
     in 側の語頭 onset＝欲しい語（と続く 0.35s 内の語＝同じ句）に割り当てられた島の最初の始まり、out 側の語尾 offset＝
     その最後の終わり。直前（直後）に無主の島が閉鎖程度の間隔（≤0.12s）で続くか、spec のカット点がその島の中・手前に
     あれば（間隔 ≤0.45s）そこまで含める。語頭が Whisper の start より 0.6s 以上前（未文字起こしの発話がつながっている）
     なら不明扱いにして spec の値を基準にする
  3. in 点: spec の値が静かで onset より前ならそのまま（頭の無音が 0.25s 超なら onset−LEAD まで詰める＝tight）。
     そうでなければ ±SEARCH 秒の静かな区間ごとに候補 = 区間の終わり − LEAD（区間内に収める）を作り、
     「候補〜onset の間に別の島がない」ものを onset−LEAD に近い順に採用（moved）。無ければ谷（p90−12dB 以下の極小点）で
     同じ検査（dip）。それも無ければ警告（warn）。onset が決まらないときは spec の値を基準に同様の検査で決める
  4. out 点: 対称（offset + LAG。尻の無音が 0.25s 超なら詰める）
  5. 区間の尺を 1/30 秒格子に量子化する（映像フレーム数 n と音声サンプル数 n×1470 を一致させ、リップシンクの累積ずれを防ぐ）

  LEAD=80ms は鼻音・摩擦音の語頭（RMS に出にくい）を守る余裕、LAG=60ms は無声化した語尾を守る余裕。
  静かさの閾値 quiet_th = clamp(max(前後 1.5 秒の RMS(5ms窓) の p90 − 22dB, ノイズフロア p10 + 8dB), −65, −35)

使い方（単体）: python3 scripts/seams.py <spec.py> [clip id...]   # 解決表を表示。完成動画があれば継ぎ目の監査も
"""

import _venv  # noqa: F401

import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

SR = 44100
FPS = 30
WIN_MS, HOP_MS = 5, 1
SEARCH = 0.30          # spec のカット点からの探索範囲（秒）
MIN_RUN = 0.020        # 「静かな区間」と認める最短長（秒）
LEAD = 0.080           # in 点: 語頭の立ち上がりからこれだけ手前（語頭の弱い子音を守る）
LAG = 0.060            # out 点: 語尾の消音点からこれだけ後ろ（無声化した語尾を守る）
CLEAN_PRE, CLEAN_POST = 0.005, 0.020   # 「そのままで良い」と判定する静かさの確認幅
FADE = 0.010           # Pass1 で各区間の頭尻に掛けるフェード（秒）。カット点が静かなら聞こえない
END_FADE = 0.200       # クリップ最後の区間の尻に掛けるフェード（動画の終わりとして自然に消す）
ISLAND_DB = 15         # 島 = 話声 p90 − これ以上のレベル
ISLAND_GAP = 0.03      # これ未満の切れ目は同じ島に結合（語中のごく短い切れ目。閉鎖 40ms 以上は島を分ける）
ASSIGN_MAX = 0.40      # 島を語に割り当てるときの、島の始まり／終わりと語の start／end の最大距離（秒）
INSIDE_RATIO = 0.70    # 島のこの割合以上が語の Whisper 区間に入っていれば、距離に関係なくその語の島とする
ISLAND_PEAK_DB = 6     # ピークが p90 − これ未満の島は語に割り当てない（相槌・呼吸・小さな声）
PHRASE_SPAN = 0.35     # 欲しい語に続く語をこの範囲まで同じ句として扱う（Whisper が 1 文字ずつ分割するため）

_ENV_CACHE = {}
_WORDS_CACHE = {}


# ================= 音声の取り出しとエンベロープ =================
def _pcm(src, t0, dur):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{max(0.0, t0):.4f}', '-i', str(src), '-t', f'{dur:.4f}',
                          '-vn', '-ac', '1', '-ar', str(SR), '-f', 's16le', '-'], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


def envelope(src, t, span=1.5):
    """t の前後 span 秒の RMS エンベロープ（dB）。戻り: (times, db)。5ms 窓・1ms 刻み"""
    key = (str(src), round(t, 3), span)
    if key in _ENV_CACHE:
        return _ENV_CACHE[key]
    t0 = max(0.0, t - span)
    x = _pcm(src, t0, 2 * span)
    win, hop = int(SR * WIN_MS / 1000), int(SR * HOP_MS / 1000)
    if len(x) < win + hop:
        return np.array([t0]), np.array([-100.0])
    cs = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    idx = np.arange(0, len(x) - win, hop)
    rms = np.sqrt((cs[idx + win] - cs[idx]) / win)
    db = 20 * np.log10(rms + 1e-9)
    times = t0 + idx / SR
    _ENV_CACHE[key] = (times, db)
    return times, db


def quiet_threshold(db):
    """話声 p90 − 22dB か、ノイズフロア p10 + 8dB の高い方（−65〜−35dB に制限）"""
    p90, p10 = np.percentile(db, 90), np.percentile(db, 10)
    return float(min(-35.0, max(-65.0, max(p90 - 22.0, p10 + 8.0))))


def _runs(times, mask, min_len):
    runs, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if times[j - 1] - times[i] >= min_len:
                runs.append((float(times[i]), float(times[j - 1])))
            i = j
        else:
            i += 1
    return runs


def quiet_runs(times, db, th):
    """静かな連続区間 [(start, end), ...]（MIN_RUN 以上）"""
    return _runs(times, db < th, MIN_RUN)


def islands(times, db, p90):
    """実音声の島 [(start, end), ...]: p90−ISLAND_DB 以上の区間（25ms 以上）を、ISLAND_GAP 未満の切れ目で結合"""
    out = []
    for s, e in _runs(times, db >= p90 - ISLAND_DB, 0.025):
        if out and s - out[-1][1] < ISLAND_GAP:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _level(times, db, t0, t1):
    m = (times >= t0) & (times <= t1)
    return float(db[m].max()) if m.any() else -100.0


def _has_speech(times, db, t0, t1, p90, min_ms=40):
    m = (times >= t0) & (times <= t1)
    return bool(m.any() and _runs(times[m], db[m] >= p90 - 12, min_ms / 1000))


def _has_dip(times, db, t0, t1, p90, depth=16):
    m = (times >= t0) & (times <= t1)
    return bool(m.any() and db[m].min() <= p90 - depth)


def _local_minima(times, db, lo, hi, p90):
    """lo〜hi の谷（p90−12dB 以下の極小点）の時刻。深い順に最大 12 個"""
    m = (times >= lo) & (times <= hi)
    if m.sum() < 3:
        return []
    tt, dd = times[m], db[m]
    idx = [i for i in range(1, len(dd) - 1) if dd[i] <= dd[i - 1] and dd[i] <= dd[i + 1] and dd[i] <= p90 - 12]
    idx.sort(key=lambda i: dd[i])
    return [float(tt[i]) for i in idx[:12]]


# ================= 語と島の対応（実音声の語頭・語尾） =================
def _overlap(a, b):
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _assign(isl, words_near, side, times, db, p90):
    """各島を語に割り当てる。島の INSIDE_RATIO 以上が語の Whisper 区間に入っていればその語（長い語の末尾の音節を
    次の語に付けない）。そうでなければ in 側は島の始まりに最も近い start を持つ語、out 側は島の終わりに最も近い end を
    持つ語（距離 ≤ ASSIGN_MAX）。ピークが p90−ISLAND_PEAK_DB 未満の島（呼吸・小さな相槌）は無主にする。
    戻り: {島 index: 語 index}"""
    out = {}
    for k, (s, e) in enumerate(isl):
        m = (times >= s) & (times <= e)
        if not m.any() or db[m].max() < p90 - ISLAND_PEAK_DB:
            continue
        best = None
        for j, w in enumerate(words_near):
            if e > s and _overlap((s, e), (w['start'], w['end'])) / (e - s) >= INSIDE_RATIO:
                best = (-1.0, j)
                break
            d = abs(w['start'] - s) if side == 'in' else abs(w['end'] - e)
            if d <= ASSIGN_MAX and (best is None or d < best[0]):
                best = (d, j)
        if best is not None:
            out[k] = best[1]
    return out


def onset_of(times, db, p90, want, prev, a, words_near):
    """欲しい語の実音声の語頭 = 欲しい語（とそれに続く PHRASE_SPAN 内の語）に割り当てられた島の最初の始まり。
    その直前に無主の島が閉鎖程度の間隔（≤0.12s）で続くか、spec のカット点がその島の中・手前にあれば（間隔 ≤0.45s）
    語頭に含める。語頭が Whisper の start より 0.6s 以上前なら不明扱い（None）"""
    if not want or not words_near:
        return None
    isl = islands(times, db, p90)
    asg = _assign(isl, words_near, 'in', times, db, p90)
    wi = [j for j, w in enumerate(words_near) if w is want]
    if not wi:
        return None
    phrase = {j for j, w in enumerate(words_near) if want['start'] <= w['start'] <= want['start'] + PHRASE_SPAN}
    mine = [k for k, j in asg.items() if j in phrase]
    if not mine:
        return None
    k = min(mine)
    s = isl[k][0]
    others = [w for j, w in enumerate(words_near) if j not in phrase]
    while k > 0:
        ps, pe = isl[k - 1]
        gap = s - pe
        if (k - 1) in asg and asg[k - 1] not in phrase:
            break
        if any(_overlap((ps, pe), (w['start'], w['end'])) / max(1e-6, pe - ps) >= 0.5 for w in others):
            break     # 無主でも別の語の区間に半分以上入っている（無声化した前の語の末尾など）
        if gap <= 0.12 or (gap <= 0.45 and (pe - ps) <= 0.45 and a <= pe + 0.06):
            k -= 1; s = ps
            continue
        break
    return None if s < want['start'] - 0.60 else s


def offset_of(times, db, p90, want, nxt, b, words_near):
    """欲しい語の実音声の語尾（対称: 欲しい語とその直前 PHRASE_SPAN 内の語に割り当てられた島の最後の終わり）"""
    if not want or not words_near:
        return None
    isl = islands(times, db, p90)
    asg = _assign(isl, words_near, 'out', times, db, p90)
    phrase = {j for j, w in enumerate(words_near) if want['end'] - PHRASE_SPAN <= w['end'] <= want['end']}
    mine = [k for k, j in asg.items() if j in phrase]
    if not mine:
        return None
    k = max(mine)
    e = isl[k][1]
    others = [w for j, w in enumerate(words_near) if j not in phrase]
    while k + 1 < len(isl):
        ns, ne = isl[k + 1]
        gap = ns - e
        if (k + 1) in asg and asg[k + 1] not in phrase:
            break
        if any(_overlap((ns, ne), (w['start'], w['end'])) / max(1e-6, ne - ns) >= 0.5 for w in others):
            break
        if gap <= 0.12 or (gap <= 0.45 and (ne - ns) <= 0.45 and b >= ns - 0.06):
            k += 1; e = ne
            continue
        break
    return None if e > want['end'] + 0.60 else e


# ================= 候補の検査と選択 =================
def _island_between(isl, t0, t1):
    """t0〜t1 に完全に含まれる島があるか（60ms 以上＝音節相当。呼吸の短い山は無視）"""
    return any(s >= t0 and e <= t1 and e - s >= 0.06 for s, e in isl)


def _ok_in(c, a, onset, isl, times, db, p90, want):
    """in 候補 c の検査"""
    if onset is not None:
        return not _island_between(isl, c + 0.01, onset - 0.005)
    if c < a:
        # 手前の音が孤立した断片か: 0.20s 未満の話声（p90−12dB 以上）の島の後に谷がある
        mm = (times >= c + 0.01) & (times <= a + 0.25)
        sp = _runs(times[mm], db[mm] >= p90 - 12, 0.025) if mm.any() else []
        sp = [(s, e) for s, e in sp if s < a]
        if sp and (sp[0][1] - sp[0][0]) < 0.20 and _has_dip(times, db, sp[0][1], a + 0.25, p90):
            return False
        return True
    if want and want['start'] <= a - 0.05 and _has_speech(times, db, a, c - 0.01, p90):
        return False
    return True


def _ok_out(c, b, offset, isl, times, db, p90, want):
    """out 候補 c の検査"""
    if offset is not None:
        return not _island_between(isl, offset + 0.01, c - 0.01)
    if c > b:
        if not (want and want['end'] >= b - 0.05):
            m = [(s, e) for s, e in isl if s > b - 0.15 and s < c]
            if m and _has_dip(times, db, b - 0.15, m[0][0], p90):
                return False
        return True
    if want and want['end'] > c + 0.05 and _has_speech(times, db, c, b, p90):
        return False
    return True


def _clamp(v, lo, hi):
    return None if lo > hi else min(max(v, lo), hi)


def resolve_in(src, a, want=None, prev=None, words_near=()):
    """区間の頭。戻り (時刻, 状態, 補足)。状態: ok / tight / moved / dip / warn"""
    times, db = envelope(src, a)
    th = quiet_threshold(db)
    p90 = float(np.percentile(db, 90))
    isl = islands(times, db, p90)
    onset = onset_of(times, db, p90, want, prev, a, words_near) if want else None
    lv = _level(times, db, a - CLEAN_PRE, a + CLEAN_POST)
    tag = f'語頭 {onset:.3f}' if onset is not None else '語頭不明'
    if lv < th or (prev and want and prev['end'] <= a - 0.05 and want['start'] >= a + 0.05 and lv < th + 8):
        # 頭の無音が長ければ詰める
        for s, e in quiet_runs(times, db, th):
            if s <= a + 0.10 and e - max(s, a) > 0.25:
                c = min(e - LEAD, a + SEARCH, onset - 0.005 if onset is not None else 1e9)
                if c > a + 0.05:
                    return c, 'tight', f'{(c - a) * 1000:+.0f}ms（頭の無音 {e - a:.2f}s を詰めた・{tag}）'
        return a, 'ok', f'静か {lv:.0f}dB（{tag}）'
    lo = max(min(a - SEARCH, onset - 0.20) if onset is not None else a - SEARCH,
             prev['start'] + 0.05 if prev else -1e9,
             ((onset if onset is not None else want['start']) - 0.50) if want else -1e9)
    hi = min(a + SEARCH, (onset - 0.005) if onset is not None
             else (max(want['end'], want['start'] + 0.15) - 0.05 if want else 1e9))
    target = (onset - LEAD) if onset is not None else a
    cands = []
    for s, e in quiet_runs(times, db, th):
        c = _clamp(e - LEAD, max(s + 0.01, lo), min(e - 0.005, hi))
        if c is not None:
            cands.append(c)
    for c in sorted(cands, key=lambda c: abs(c - target)):
        if _ok_in(c, a, onset, isl, times, db, p90, want):
            return c, 'moved', f'{(c - a) * 1000:+.0f}ms（発話中 {lv:.0f}dB → 無音へ・{tag}）'
    dips = _local_minima(times, db, lo, hi, p90)
    for c in sorted(dips, key=lambda c: (int(abs(c - target) / 0.10), float(db[int(np.argmin(np.abs(times - c)))]))):
        if _ok_in(c, a, onset, isl, times, db, p90, want):
            k = int(np.argmin(np.abs(times - c)))
            return c, 'dip', f'{(c - a) * 1000:+.0f}ms（無音なし。谷 {db[k]:.0f}dB へ・話声 {p90:.0f}dB・{tag}）'
    return a, 'warn', f'発話中（{lv:.0f}dB, 閾値 {th:.0f}dB）で {lo:.2f}〜{hi:.2f} に無音も谷もなし（{tag}）'


def resolve_out(src, b, want=None, nxt=None, words_near=()):
    """区間の尻。戻り (時刻, 状態, 補足)"""
    times, db = envelope(src, b)
    th = quiet_threshold(db)
    p90 = float(np.percentile(db, 90))
    isl = islands(times, db, p90)
    offset = offset_of(times, db, p90, want, nxt, b, words_near) if want else None
    lv = _level(times, db, b - CLEAN_POST, b + CLEAN_PRE)
    tag = f'語尾 {offset:.3f}' if offset is not None else '語尾不明'
    if lv < th:
        for s, e in quiet_runs(times, db, th):
            if s <= b <= e + 0.01 and b - s > 0.25:
                c = max(s + LAG, b - SEARCH, offset + 0.005 if offset is not None else -1e9)
                if c < b - 0.05:
                    return c, 'tight', f'{(c - b) * 1000:+.0f}ms（尻の無音 {b - s:.2f}s を詰めた・{tag}）'
        return b, 'ok', f'静か {lv:.0f}dB（{tag}）'
    lo = max(b - SEARCH, (offset - 0.08) if offset is not None else (want['start'] + 0.05 if want else -1e9))
    hi = (min(max(b + SEARCH, offset + 0.20), offset + 0.60) if offset is not None
          else min(b + SEARCH, nxt['start'] + 0.15 if nxt else 1e9))
    target = (offset + LAG) if offset is not None else b
    cands = []
    for s, e in quiet_runs(times, db, th):
        c = _clamp(s + LAG, max(s + 0.01, lo), min(e - 0.01, hi))
        if c is not None:
            cands.append(c)
    for c in sorted(cands, key=lambda c: abs(c - target)):
        if _ok_out(c, b, offset, isl, times, db, p90, want):
            return c, 'moved', f'{(c - b) * 1000:+.0f}ms（発話中 {lv:.0f}dB → 無音へ・{tag}）'
    dips = _local_minima(times, db, lo, hi, p90)
    for c in sorted(dips, key=lambda c: (int(abs(c - target) / 0.10), float(db[int(np.argmin(np.abs(times - c)))]))):
        if _ok_out(c, b, offset, isl, times, db, p90, want):
            k = int(np.argmin(np.abs(times - c)))
            return c, 'dip', f'{(c - b) * 1000:+.0f}ms（無音なし。谷 {db[k]:.0f}dB へ・話声 {p90:.0f}dB・{tag}）'
    return b, 'warn', f'発話中（{lv:.0f}dB, 閾値 {th:.0f}dB）で {lo:.2f}〜{hi:.2f} に無音も谷もなし（{tag}）'


# ================= 欲しい語の決定 =================
def _words(path):
    p = str(path)
    if p not in _WORDS_CACHE:
        _WORDS_CACHE[p] = json.load(open(p))['words'] if p and Path(p).exists() else []
    return _WORDS_CACHE[p]


_PUNCT = re.compile(r'[\[\]/、。，．,.!?！？…・「」『』（）()\s]')


def _telop_head(clip, a0):
    """区間頭の近く（a0−0.15〜a0+0.8）に始まる最初のテロップの先頭 2 文字（記号除去）"""
    for e in sorted(clip.get('telops', []), key=lambda e: e[0]):
        if a0 - 0.15 <= e[0] <= a0 + 0.8:
            txt = _PUNCT.sub('', e[2])
            return txt[:2] if len(txt) >= 2 else (txt or None)
    return None


def _telop_tail(clip, a0, b0):
    """区間内（a0〜b0）に始まる最後のテロップの末尾 2 文字（記号除去）"""
    tails = [e for e in clip.get('telops', []) if a0 - 0.15 <= e[0] < b0]
    if not tails:
        return None
    txt = _PUNCT.sub('', max(tails, key=lambda e: e[0])[2])
    return txt[-2:] if len(txt) >= 2 else (txt or None)


def _match_tokens(words, idxs, text_key):
    """語列を連結した文字列で text_key を探し、(先頭語 index, 末尾語 index) の候補を全部返す"""
    text, owner = '', []
    for i in idxs:
        for _ in words[i]['word']:
            owner.append(i)
        text += words[i]['word']
    out, pos = [], text.find(text_key)
    while pos >= 0:
        out.append((owner[pos], owner[pos + len(text_key) - 1]))
        pos = text.find(text_key, pos + 1)
    return out


def _want_in(words, a0, head):
    """in 側の欲しい語と直前の語。テロップ先頭と語列の照合を優先し、無ければ時刻規則"""
    idxs = [i for i, w in enumerate(words) if a0 - 0.3 <= w['start'] <= a0 + 0.8]
    if head and idxs:
        hits = [(i0, i1) for i0, i1 in _match_tokens(words, idxs, head) if a0 - 0.15 <= words[i0]['start'] <= a0 + 0.25]
        if hits:
            i = min(hits, key=lambda h: abs(words[h[0]]['start'] - a0))[0]
            return words[i], (words[i - 1] if i > 0 else None)
    after = [i for i, w in enumerate(words) if w['start'] >= a0 - 0.06]
    if not after:
        return None, None
    i = after[0]
    if words[i]['end'] <= a0 + 0.10 and len(after) > 1:   # 区間の頭で終わりかけの短い語は欲しい語ではない
        i = after[1]
    return words[i], (words[i - 1] if i > 0 else None)


def _want_out(words, b0, src=None, tail=None):
    """out 側の欲しい語と次の語。テロップ末尾と語列の照合（語の end が b0±0.35s で、その語の後にポーズがある）を優先"""
    env = envelope(src, b0) if src is not None else None
    idxs = [i for i, w in enumerate(words) if b0 - 1.0 <= w['start'] <= b0 + 0.3]
    if tail and idxs:
        hits = [i1 for i0, i1 in _match_tokens(words, idxs, tail) if abs(words[i1]['end'] - b0) <= 0.35]
        if hits:
            i = min(hits, key=lambda i: abs(words[i]['end'] - b0))
            paused = True
            if env is not None:
                times, db = env
                th = quiet_threshold(db)
                we = words[i]['end']
                paused = any(we - 0.15 <= s <= we + 0.35 and e - s >= 0.04 for s, e in quiet_runs(times, db, th))
            if paused:
                return words[i], (words[i + 1] if i + 1 < len(words) else None)
    before = [i for i, w in enumerate(words) if w['start'] <= b0 - 0.03]
    if not before:
        return None, None
    i = before[-1]
    w = words[i]
    if i > 0 and (b0 - w['start']) < 0.18 and (w['end'] - b0) >= 0.10:
        # 語頭 0.18s 以内でカットしていてその語がまだ続く＝その語は不要（前の語で終わる意図）。
        # ただし Whisper の end は尾を引くので、実音声がカット点の後 120ms も続いているときだけ
        cont = True
        if env is not None:
            times, db = env
            cont = _has_speech(times, db, b0, b0 + 0.12, float(np.percentile(db, 90)), min_ms=60)
        if cont:
            i -= 1
    return words[i], (words[i + 1] if i + 1 < len(words) else None)


# ================= 区間の解決 =================
def resolve_segs(clip, spec):
    """clip['segs'] を音響で確定した区間のリストに変換する。
    戻り: [dict(a, b, n, layout, a0, b0, in_state, out_state, in_note, out_note, in_words, out_words)]
    （n = 30fps のフレーム数、b = a + n/30）。spec.RESOLVE_CUTS = False で解決を止められる（量子化だけ行う）"""
    key = (clip['id'], tuple(tuple(s) for s in clip['segs']))
    cache = getattr(spec, '_RESOLVED', None)
    if cache is None:
        cache = spec._RESOLVED = {}
    if key in cache:
        return cache[key]
    words = _words(getattr(spec, 'WHISPER', ''))
    enabled = getattr(spec, 'RESOLVE_CUTS', True)
    out = []
    for seg in clip['segs']:
        a0, b0 = seg[:2]
        layout = seg[2] if len(seg) > 2 else clip.get('layout')
        want_in, prev = _want_in(words, a0, _telop_head(clip, a0))
        want_out, nxt = _want_out(words, b0, spec.SRC if enabled else None, _telop_tail(clip, a0, b0))
        if enabled:
            near_a = [w for w in words if a0 - 2.0 <= w['start'] <= a0 + 2.0]
            near_b = [w for w in words if b0 - 2.0 <= w['start'] <= b0 + 2.0]
            a, ist, inote = resolve_in(spec.SRC, a0, want_in, prev, near_a)
            if ist == 'warn':   # テロップ照合の語で決まらなければ時刻規則の語で再試行
                w2, p2 = _want_in(words, a0, None)
                if w2 is not want_in:
                    a, ist, inote = resolve_in(spec.SRC, a0, w2, p2, near_a)
                    want_in, prev = w2, p2
            b, ost, onote = resolve_out(spec.SRC, b0, want_out, nxt, near_b)
            if ost == 'warn':
                w2, n2 = _want_out(words, b0, spec.SRC, None)
                if w2 is not want_out:
                    b, ost, onote = resolve_out(spec.SRC, b0, w2, n2, near_b)
                    want_out, nxt = w2, n2
            if ost == 'warn' and seg is clip['segs'][-1]:
                ost, onote = 'endfade', f'クリップ末尾（{END_FADE * 1000:.0f}ms フェードアウトで処理）'
        else:
            a, ist, inote, b, ost, onote = a0, 'off', '', b0, 'off', ''
        n = max(1, round((b - a) * FPS))
        b = a + n / FPS
        out.append(dict(a=a, b=b, n=n, layout=layout, a0=a0, b0=b0,
                        in_state=ist, out_state=ost, in_note=inote, out_note=onote,
                        in_words=(prev, want_in), out_words=(want_out, nxt)))
    cache[key] = out
    return out


def describe(clip, segs, verbose=False):
    """解決結果の表示行と警告のリストを返す"""
    lines, warns = [], []

    def wl(w):
        return f"{w['word']}({w['start']:.2f}-{w['end']:.2f})" if w else '-'
    for i, s in enumerate(segs):
        for side, st, note, t0, t1, ws in (('in', s['in_state'], s['in_note'], s['a0'], s['a'], s['in_words']),
                                           ('out', s['out_state'], s['out_note'], s['b0'], s['b'], s['out_words'])):
            if st in ('moved', 'dip', 'tight', 'endfade') or verbose:
                lines.append(f"  seg{i} {side:3s} {t0:9.3f} → {t1:9.3f}  {note}  [{wl(ws[0])} | {wl(ws[1])}]")
            if st == 'warn':
                warns.append(f"seg{i} {side} {t0:.2f}: {note}  [{wl(ws[0])} | {wl(ws[1])}]")
    return lines, warns


# ================= 完成動画の継ぎ目監査 =================
def audit_output(video, segs):
    """各継ぎ目（出力秒）で、完全ゼロの連続長（AAC パディングの穴）・隣接サンプル差の最大（クリック）・
    継ぎ目前後 40ms のレベル（断片の混入）を測る"""
    rows = []
    pos = 0.0
    for i, s in enumerate(segs[:-1]):
        pos += s['n'] / FPS
        x = _pcm(video, pos - 0.10, 0.20)
        if len(x) < 1000:
            continue
        zero = cur = 0
        for v in x:
            cur = cur + 1 if v == 0 else 0
            zero = max(zero, cur)
        d = float(np.abs(np.diff(x)).max())
        mid = len(x) // 2
        w = int(SR * 0.04)
        pre = 20 * np.log10(np.sqrt(np.mean(x[mid - w:mid] ** 2)) + 1e-9)
        post = 20 * np.log10(np.sqrt(np.mean(x[mid:mid + w] ** 2)) + 1e-9)
        flag = []
        if zero / SR > 0.012:
            flag.append(f'穴 {zero / SR * 1000:.0f}ms')
        if d > 0.25:
            flag.append(f'クリック Δ{d:.2f}')
        rows.append((i + 1, pos, zero / SR * 1000, d, pre, post, '・'.join(flag) or 'OK'))
    return rows


def main(spec, argv):
    targets = [a for a in argv] or [c['id'] for c in spec.CLIPS]
    for c in spec.CLIPS:
        if c['id'] not in targets:
            continue
        segs = resolve_segs(c, spec)
        print(f"=== {c['id']} ===")
        lines, warns = describe(c, segs, verbose=True)
        print('\n'.join(lines))
        for w in warns:
            print(f'  警告: {w}')
        total = sum(s['n'] for s in segs) / FPS
        print(f'  尺 {total:.2f}s（{sum(s["n"] for s in segs)} フレーム）')
        video = Path(spec.OUT_DIR) / f"{c['id']}_typet.mp4"
        if video.exists():
            print('  継ぎ目監査（完成動画）:')
            for i, pos, zero_ms, d, pre, post, flag in audit_output(video, segs):
                print(f'    seam{i} @{pos:7.3f}s  ゼロ連続 {zero_ms:4.1f}ms  maxΔ {d:.3f}  前40ms {pre:.0f}dB 後40ms {post:.0f}dB  {flag}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    import importlib.util
    p = Path(sys.argv[1]).resolve()
    sp = importlib.util.spec_from_file_location(p.stem, p)
    mod = importlib.util.module_from_spec(sp); sp.loader.exec_module(mod)
    main(mod, sys.argv[2:])
