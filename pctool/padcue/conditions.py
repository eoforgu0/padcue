"""条件(observation condition)の定義・保存・照合。

条件 = 判定する映像 + 基準画像 + 判定する範囲 + しきい値。判定には基準画像が
1 枚以上必要である。手順・装置から独立して定義・保存され、名前で参照される。装置番号とは
無関係。

照合は、いまの映像と基準画像を同じ大きさ(COMPARE_W x COMPARE_H)の RGB に縮め、
範囲ごとに次の差を出す(docs/specs/observe-and-decide.md §2)。

1. 基準画像の範囲に対する、いまの映像の明るさと濃さのずれを、一律の倍率と足し算
   (GAIN_MIN〜GAIN_MAX 倍、±OFFSET_MAX 段)とみなして打ち消す
2. 打ち消した後の R、G、B の差の平均を取る
3. いまの映像の範囲を前後左右 SEARCH 画素まで動かし、最も小さい差を取る

一致度は 100 から差を引き、小数を切り捨てたもの(0 未満は 0)。範囲が複数あれば、
最も低い一致度がその基準画像の一致度になり(どの範囲も合っているときだけ
一致)、基準画像が複数あれば、最も高いものが条件の一致度になる(いずれか 1 枚に
一致すれば一致)。一致度がしきい値以上なら一致である。

比べ方を選ばせる項目は持たない。ほかの方式と比べて、これに決めた理由は
observe-and-decide.md の末尾の「設計判断」にある。
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from operator import mul, sub
from pathlib import Path

from .files import read_text, remove_tree, retry_while_in_use, write_file
from .frames import CHANNELS, PNG_HEAD_SIZE, Frame, check_png_head, decode_png
from .names import apply_order, refuse_taken, validate_name
from .procfmt import is_int
from .sources import (
    FILE_MISSING,
    NOT_PNG,
    DesktopSource,
    DShowSource,
    FileSource,
    WindowSource,
)
from .words import as_json, sentences

try:
    import numpy as _np
except ImportError:
    _np = None

# 比較はここで固定する大きさで行う(16:9。しきい値はこの大きさの画素差を
# 基準に決めるので、取り込みサイズが変わっても意味が変わらない)。
# 1080p の 3 分の 1 である。横 3 画素が 1 画素になるので、1 文字の幅が
# およそ 20 画素の表示なら縮めた後も 6 画素残り、形が保たれる
COMPARE_W, COMPARE_H = 640, 360
# 範囲を指定しない条件が比べる範囲(画面全体)
WHOLE_FRAME = (0.0, 0.0, 1.0, 1.0)
# 基準画像といまの映像の縦横の比が違うとみなす差(比どうしの比が 1 からこれより
# 離れたら違う。比が違うと困る理由は aspect_differs)。間引き(thin_to_view)と
# 切り出しの端数で生じる差は 1% に達しない
ASPECT_TOLERANCE = 0.01
# 位置のずれを探す幅(比較の大きさの画素で、前後左右それぞれ。1080p の 9 画素)。
# 取り込むたびに 1〜2 画素ずれる映像でも、範囲の中の縁で差が跳ねないようにする
SEARCH = 3
# 明るさと濃さのずれとして打ち消す幅。これを超えるずれは画面の違いとして差に残す
# (白い字と灰色の字、真っ黒な画面と暗い場面を、同じとみなさないため)
GAIN_MIN, GAIN_MAX = 0.7, 1.4
OFFSET_MAX = 40.0
# 基準画像の範囲の明るさの分散がこれ以下なら、倍率は合わせない(単色の範囲では
# 倍率が決まらない)。足し算のずれだけを打ち消す
FLAT_VAR = 1.0
# しきい値の既定値(一致度)。範囲ごとの差が 12 以下で一致にあたる。分けられない
# 場面は、しきい値の目安で改めて決める(observe-and-decide.md の「設計判断」)
DEFAULT_THRESHOLD = 88
# 基準画像を登録した直後に、同じ映像から続けて取得する枚数。しきい値の目安のうち、
# 一致する側のばらつき(同じ場面でも取り込むたびに変わる分)を測るのに使う
AFTER_COUNT = 2
# 条件の形式のバージョン。手順とは別に数える
FORMAT_VERSION = 1
# 画像を置くフォルダー。condition.json はこの中を相対で指す
IMAGES = "images"
# 条件の中身を書くファイル。padcue ファイルの中でも同じ名前である
DOC_NAME = "condition.json"
# 片付け(sweep)で消すのは、置いてからこの秒数(7 日)がたった画像だけである。編集の
# 画面は padcue を起動し直しても接続し直して編集を続けられるので、開いたままの編集が
# 追加した、まだ保存していない画像を消さない。週をまたいで開いたままにする使い方まで
# 含める長さにする(残る画像は小さい)
SWEEP_AGE_S = 7 * 24 * 3600


def _validate_regions(
    regions: list,
) -> list[tuple[float, float, float, float]]:
    """判定する範囲を確認して (x, y, w, h) の並びにする。

    空でもよい。範囲を指定しない条件は画面全体を比べる(WHOLE_FRAME)。
    形の違う値は、ほかの書き間違いと同じく ValueError で拒否する。型ごとの例外が
    漏れると、読み込めない条件として扱えない。
    """
    if not isinstance(regions, (list, tuple)):
        raise ValueError("regions は範囲の配列である必要があります: "
                         f"{as_json(regions)}")
    return [_validate_rect(r, "region ") for r in regions]


def _validate_rect(r, subject: str) -> tuple[float, float, float, float]:
    """映像に対する割合の [x, y, w, h](判定する範囲と切り出し範囲)を確認する。

    判定する範囲と切り出し範囲は同じ形なので、同じ決まりで確かめる。別々に
    確かめると、片方の決まりだけを修正したときに、もう片方に古い決まりが残る。
    subject は文の主語で、文の初めに置く(英字で終わるなら後ろに空白を付けて渡す)。
    """
    if (not isinstance(r, (list, tuple)) or len(r) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       for v in r)):
        raise ValueError(f"{subject}は x、y、w、h の 4 つの数値である必要が"
                         f"あります: {as_json(r)}")
    x, y, w, h = (_as_float(v) for v in r)
    if not (0 <= x < 1 and 0 <= y < 1):
        raise ValueError(f"{subject}の x と y は 0 以上 1 未満の数値である必要が"
                         f"あります: {as_json(r)}")
    if not (0 < w <= 1 and 0 < h <= 1):
        raise ValueError(f"{subject}の w と h は 0 より大きく 1 以下である必要が"
                         f"あります: {as_json(r)}")
    # 映像の外にはみ出す範囲は、はみ出した所を比べられない。小数の誤差は許す
    if x + w > 1 + 1e-9 or y + h > 1 + 1e-9:
        raise ValueError(f"{subject}が映像の範囲を超えています: {as_json(r)}")
    return (x, y, w, h)


def _validate_threshold(threshold) -> int:
    """しきい値(一致度。1 から 100 までの整数)を確認する。

    整数に限るのは、画面に出す一致度が整数だからである。小数のしきい値は、
    出ている数との大小が見て分からない。
    """
    t = _json_int(threshold)
    if t is None or not 1 <= t <= 100:
        raise ValueError("threshold は 1 から 100 までの整数である必要があります: "
                         f"{as_json(threshold)}")
    return t


def _json_int(v) -> int | None:
    """JSON の数としての整数なら、その int を返す。違えば None。

    JSON は 88 と 88.0 を区別しないので、小数点以下が 0 の数も整数として受ける。
    文字列は数に変換しない。変換して受けると、手で書いた誤りを黙って通す。true は
    Python では 1 と等しいので、真偽値も除く。
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return int(v)
    return None


def _as_float(v: int | float) -> float:
    """数を float にする。float で表せない大きさの整数(JSON では書ける)は
    無限大にして、続く範囲のチェックで拒否する。float() の OverflowError をそのまま
    漏らすと、読み込めない条件として扱えない。"""
    try:
        return float(v)
    except OverflowError:
        return math.inf if v > 0 else -math.inf


def validate_source(source: dict) -> dict:
    """source 定義の形を確認し、正規化して返す。

    未知のキーはエラーにする(前方互換より誤記検出を優先。file-format.md §9 の
    「これ以外の欄は拒否する」と同じ方針)。手編集の JSON の打ち間違いを黙って
    無視しない。
    """
    if not isinstance(source, dict):
        raise ValueError("source はオブジェクトである必要があります")
    kind = source.get("kind")

    def only(*allowed):
        """kind ごとに書ける項目だけかを確認する(procfmt と同じ言い方で拒否する)。"""
        extra = sorted(set(source) - set(allowed))
        if extra:
            raise ValueError(f"source に使用できない項目です: {'、'.join(extra)}。"
                             f"kind が {as_json(kind)} のときに使用できるのは "
                             f"{'、'.join(allowed)} です。")

    def need(key):
        if not source.get(key):
            raise ValueError(f"kind が {as_json(kind)} のときは、source に {key} が"
                             "必要です")
        if not isinstance(source[key], str):
            raise ValueError(f"{key} は文字列である必要があります: "
                             f"{as_json(source[key])}")
    if kind == "lane":
        # 装置に設定している映像。どの映像かは実行する装置が持つので、判定の
        # ときにレーンの設定へ解決する。条件そのものは装置に依らない
        only("kind")
        return {"kind": "lane"}
    if kind == "file":
        only("kind", "path")
        need("path")
        return {"kind": "file", "path": source["path"]}
    if kind == "dshow":
        # 取り込みを待つ長さは padcue が決める(判定の周期と画面の取り込みが
        # 同じ上限で待つ。condwatch.CondWatcher.GRAB_TIMEOUT_S)
        only("kind", "device")
        need("device")
        return {"kind": "dshow", "device": source["device"]}
    if kind == "desktop":
        only("kind", "monitor")
        out = {"kind": "desktop"}
        if "monitor" in source:
            m = _json_int(source["monitor"])
            if m is None:
                raise ValueError("monitor は整数である必要があります: "
                                 f"{as_json(source['monitor'])}")
            out["monitor"] = m
        return out
    if kind == "window":
        only("kind", "title")
        need("title")
        return {"kind": "window", "title": source["title"]}
    raise ValueError(f"サポートされていない source の種類です: {as_json(kind)}")


def validate_crop(crop) -> tuple[float, float, float, float]:
    """レーンの映像の切り出し範囲を確認し、正規化して返す。

    映像ソースに対する 0〜1 の相対の値で、幅と高さは 0 より大きい。判定する範囲は
    切り出した後の映像に対する相対なので、ここで形を確認しておかないと
    条件の判定が静かにずれる。決まりは判定する範囲と同じである(_validate_rect)。
    """
    return _validate_rect(crop, "切り出し範囲")


@dataclass
class Condition:
    """覚えた 1 つの条件。

    source は映像ソースの種類と設定(例
    ``{"kind":"dshow","device":"OBS Virtual Camera"}``)。regions は
    正規化座標(0..1)の (x, y, w, h) のリストで、空なら画面全体を比べる。
    refs は比較の大きさへ縮めた基準画像で、登録した順に並ぶ。空なら何にも
    一致しない。threshold は一致度のしきい値で、一致度がこれ以上なら一致である。
    ref_files は基準画像の相対の位置、negatives は一致しないはずの画面の相対の
    位置で、どちらも追加した順に並ぶ。編集の画面が採点するときは、保存する前の
    並び(追加したばかりの画像を含む)が入る。ref_sizes は基準画像の縮める前の
    (幅, 高さ) で、refs と同じ順に並ぶ(縦横の比を、いまの映像と比べるのに使う)。
    """

    name: str
    source: dict
    regions: list[tuple[float, float, float, float]]
    refs: list[Frame]
    threshold: int = DEFAULT_THRESHOLD
    ref_files: list = field(default_factory=list)
    negatives: list = field(default_factory=list)
    ref_sizes: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self.source = validate_source(self.source)
        self.regions = _validate_regions(self.regions)
        self.threshold = _validate_threshold(self.threshold)


# 画像の中身をキーにした、縮めた Frame のキャッシュ。画像の読み込みは重い(1080p の
# PNG の復号と縮小)。編集の画面は範囲を動かすたびにすべての基準画像を採点するので、同じ
# 画像をくり返し復号しない。判定は、先にファイルの場所で探す file_frame を使う。
# 満杯になったら最も長く使っていないものから捨てる。すべてを捨てると、条件が
# 上限を超えた途端に毎回すべてを作り直すことになる。縮めた 1 枚は 0.7 MB で、
# 上限は下の _FILE_CACHE と別々に 64 枚ずつである
_FRAME_CACHE: OrderedDict[bytes, Frame] = OrderedDict()
_FRAME_CACHE_MAX = 64
_FRAME_CACHE_LOCK = threading.Lock()


def _cache_get(cache: OrderedDict, key) -> Frame | None:
    """キャッシュから探す。見つかったものは、最近使ったものとして後ろへ回す。"""
    with _FRAME_CACHE_LOCK:
        got = cache.get(key)
        if got is not None:
            cache.move_to_end(key)
        return got


def _cache_put(cache: OrderedDict, key, frame: Frame) -> None:
    """キャッシュに入れる。上限を超えたら、最も長く使っていないものから捨てる。"""
    with _FRAME_CACHE_LOCK:
        cache[key] = frame
        while len(cache) > _FRAME_CACHE_MAX:
            cache.popitem(last=False)


def _decode(raw: bytes, what: str) -> tuple[int, int, bytes]:
    """PNG を読む。読めなければ、何の画像かを利用者の用語で示して拒否する。

    frames.py の例外の文(PNG の署名・CRC・IHDR)は開発者向けで、画面に出さない。
    """
    try:
        return decode_png(raw)
    except ValueError:
        raise ValueError(f"{what}を読み込めません") from None


# 基準画像のファイルから縮めた Frame へのキャッシュ。キーはファイルの場所と更新時刻と
# 大きさで、見つかればファイルを読まず、中身の要約も計算しない。画像の名前は乱数で
# 作り、別の画像に使い回さない(file-format.md §9-1)ので、場所で探しても古い絵が
# 見つかることはない。手で書き替えたときも、更新時刻か大きさが変わる
_FILE_CACHE: OrderedDict[tuple, Frame] = OrderedDict()


def file_frame(path: Path) -> Frame:
    """基準画像のファイルを、比較の大きさへ縮めた Frame にする(キャッシュつき)。"""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    got = _cache_get(_FILE_CACHE, key)
    if got is None:
        got = shrunk_frame(path.read_bytes())
        _cache_put(_FILE_CACHE, key, got)
    return got


def shrunk_frame(raw: bytes) -> Frame:
    """PNG(基準画像など)を、比較の大きさへ縮めた Frame にする(キャッシュつき)。

    いまの映像と同じく、条件の編集の画面の大きさまで間引いてから縮める
    (thin_to_view)。インポートした条件の画像は大きさのまま置くので、8K の画像を
    そのまま縮めると、縮める途中の配列だけで数百 MB を使う。処理が同じなら、同じ
    映像から取得した基準画像といまの映像が同じ値になる。
    """
    key = hashlib.blake2b(raw, digest_size=16).digest()
    got = _cache_get(_FRAME_CACHE, key)
    if got is None:
        width, height, rgb = _decode(raw, "画像")
        got = thin_to_view(Frame(width, height, rgb)).shrink(COMPARE_W, COMPARE_H)
        _cache_put(_FRAME_CACHE, key, got)
    return got


def _region_px(r: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    x, y, w, h = r
    pw = max(1, round(w * COMPARE_W))
    ph = max(1, round(h * COMPARE_H))
    # 幅と高さを 1 画素に上げると、右端と下端の範囲は比べる絵の外へ出る。中に収める
    px = min(round(x * COMPARE_W), COMPARE_W - pw)
    py = min(round(y * COMPARE_H), COMPARE_H - ph)
    return px, py, pw, ph


def agreement(diff: float) -> int:
    """差(0..255。小さいほど近い)を一致度(0..100 の整数。大きいほど近い)にする。

    小数は切り捨てる。画面に出す一致度としきい値(整数)の大小が、判定と食い
    違わないようにするためである(87.6 を 88 と出して不一致、ということが
    起きない)。
    """
    return max(0, math.floor(100.0 - diff))


def _pad_edge(frame: Frame, s: int) -> bytes:
    """端の画素をくり返して、上下左右を s 画素ずつ広げた RGB を返す。"""
    w, h, rgb = frame.width, frame.height, frame.rgb
    row = w * CHANNELS
    out = []
    for y in range(-s, h + s):
        yy = min(max(y, 0), h - 1)
        line = rgb[yy * row:(yy + 1) * row]
        out.append(line[:CHANNELS] * s + line + line[-CHANNELS:] * s)
    return b"".join(out)


def _patch(rgb: bytes, width: int, x: int, y: int, w: int, h: int) -> bytes:
    row = w * CHANNELS
    return b"".join(
        rgb[((y + r) * width + x) * CHANNELS:((y + r) * width + x) * CHANNELS + row]
        for r in range(h))


class _Current:
    """いまの映像(比較の大きさ)を、位置の探索のために縁を広げて持つ。

    縁は端の画素をくり返して SEARCH 画素ずつ広げる。範囲が画面の端にあっても、
    どの位置も同じ大きさで比べられる。numpy があれば配列で、なければバイト列で
    持つ。どちらで比べても同じ値になる(_fit_luts の注記)。
    """

    def __init__(self, frame: Frame):
        self.width, self.height = frame.width, frame.height
        if _np is not None:
            arr = _np.frombuffer(frame.rgb, dtype=_np.uint8).reshape(
                frame.height, frame.width, CHANNELS)
            # 型は 1 度だけ変える(位置ごとに変えると、49 回分のコピーが必要)
            self.arr = _np.pad(arr, ((SEARCH, SEARCH), (SEARCH, SEARCH), (0, 0)),
                               mode="edge").astype(_np.int32)
            self.rgb = b""
        else:
            self.arr = None
            self.rgb = _pad_edge(frame, SEARCH)


def _fit_luts(n: int, sr: int, srr: int, sc: int,
              scr: int) -> list[tuple[bytes, float]]:
    """いまの映像 ≒ a × 基準画像 + b となる a と b の候補を、範囲の和から求める。

    候補ごとに返すのは、基準画像の値(0..255)を a 倍して b を足した値へ変換する表と、
    a である。呼び出し元は、候補ごとに差を出して小さい方を取る。

    1 つ目は、最小二乗の傾きを倍率の幅に切り詰め、その倍率で足し算を求めて幅に
    切り詰めた組である。2 つ目は、倍率 1 で、足し算を平均の差(幅に切り詰める)に
    した組で、1 つ目が切り詰めに当たったときだけ足す。倍率と足し算を別々に
    切り詰めると、幅の中にもっと差の小さい組があっても使わない。明るくほぼ一様な
    範囲で、取り込みごとのばらつきが基準画像といまの映像で別々だと、傾きが 0 近くに
    出て倍率が下限に張り付き、足し算も上限に張り付いて、同じ画面が一致しない。
    切り詰めに当たらなければ 1 つ目が最小二乗の解そのものなので、2 つ目は足さない
    (差は組ごとに 49 か所で出すので、要らない組を足すと採点が遅くなる)。2 つの組が
    同じなら 1 つだけ返す。

    和はどれも整数で、a と b は同じ式で求めるので、numpy で和を取っても純 Python
    で取っても、表と a は同じになる。差の和も整数なので、差の値まで一致する。
    """
    vr = (srr - sr * sr / n) / n
    a0 = ((scr - sc * sr / n) / n) / vr if vr > FLAT_VAR else 1.0
    a = min(max(a0, GAIN_MIN), GAIN_MAX)
    b0 = sc / n - a * sr / n
    b = min(max(b0, -OFFSET_MAX), OFFSET_MAX)
    pairs = [(a, b)]
    if a != a0 or b != b0:
        b1 = min(max(sc / n - sr / n, -OFFSET_MAX), OFFSET_MAX)
        if (a, b) != (1.0, b1):
            pairs.append((1.0, b1))
    return [(bytes(min(255, max(0, round(g * v + o))) for v in range(256)), g)
            for g, o in pairs]


def _region_diff(cur: _Current, ref: Frame, x: int, y: int, w: int, h: int) -> float:
    """1 つの範囲の差(0..255。小さいほど近い)。モジュールの説明の 1〜3 である。

    差は打ち消した後の値の差を a で割り、基準画像の明るさの尺度へ戻す。
    """
    rp = ref.crop(x, y, w, h)
    if not rp.rgb:
        return 255.0
    x, y = min(max(x, 0), ref.width), min(max(y, 0), ref.height)
    pw, ph = rp.width, rp.height
    shifts = [(dx, dy) for dy in range(-SEARCH, SEARCH + 1)
              for dx in range(-SEARCH, SEARCH + 1)]
    best = math.inf
    if cur.arr is not None:
        r8 = _np.frombuffer(rp.rgb, dtype=_np.uint8).reshape(ph, pw, CHANNELS)
        r = r8.astype(_np.int32)
        n = int(r.size)
        sr = int(r.sum(dtype=_np.int64))
        srr = int((r * r).sum(dtype=_np.int64))
        for dx, dy in shifts:
            top, left = y + dy + SEARCH, x + dx + SEARCH
            c = cur.arr[top:top + ph, left:left + pw]
            for lut, a in _fit_luts(n, sr, srr, int(c.sum(dtype=_np.int64)),
                                    int((c * r).sum(dtype=_np.int64))):
                mapped = _np.frombuffer(lut, dtype=_np.uint8).astype(_np.int32)[r8]
                best = min(best,
                           int(_np.abs(c - mapped).sum(dtype=_np.int64)) / n / a)
        return best
    r = rp.rgb
    n = len(r)
    sr = sum(r)
    srr = sum(map(mul, r, r))
    width = cur.width + 2 * SEARCH
    for dx, dy in shifts:
        c = _patch(cur.rgb, width, x + dx + SEARCH, y + dy + SEARCH, pw, ph)
        for lut, a in _fit_luts(n, sr, srr, sum(c), sum(map(mul, c, r))):
            best = min(best, sum(map(abs, map(sub, c, r.translate(lut)))) / n / a)
    return best


@dataclass(frozen=True)
class Judgement:
    """いまの映像を条件と比べた結果。

    score は条件の一致度。refs は基準画像ごとの一致度(登録した順)、best は
    最も高い基準画像の位置(同じ値なら先に登録したもの)、regions は
    その基準画像の範囲ごとの一致度である。
    """

    score: int
    refs: list
    best: int
    regions: list


def judge(frame: Frame, cond: Condition) -> Judgement:
    """frame を cond と比べる。組み合わせ方はモジュールの説明のとおり。

    基準画像がない条件は比べる対象がないので拒否する。呼び出し元はその選択肢を
    不一致として次へ進む。
    """
    if not cond.refs:
        raise ValueError("基準画像がありません")
    cur = _Current(frame.shrink(COMPARE_W, COMPARE_H))
    regions = [_region_px(r) for r in cond.regions or [WHOLE_FRAME]]
    per_ref = [[agreement(_region_diff(cur, ref, *r)) for r in regions]
               for ref in cond.refs]
    totals = [min(each) for each in per_ref]
    best = max(range(len(totals)), key=lambda i: totals[i])
    return Judgement(totals[best], totals, best, per_ref[best])


def score(frame: Frame, cond: Condition) -> int:
    """frame の、cond に対する一致度(0..100。大きいほど近い)。"""
    return judge(frame, cond).score


def matches(frame: Frame, cond: Condition) -> bool:
    """frame の一致度が cond のしきい値以上かどうか。"""
    return score(frame, cond) >= cond.threshold


def png_size(raw: bytes) -> tuple[int, int]:
    """PNG の (幅, 高さ)。頭(署名と IHDR)だけを読む。"""
    return check_png_head(raw[:PNG_HEAD_SIZE])


def aspect_differs(cond: Condition, frame: Frame) -> bool:
    """cond の基準画像のどれかと、いまの映像 frame の縦横の比が違うか。

    比べる前にどちらも比較の大きさへ揃えるので、比が違うと形の違う絵どうしを
    比べ、一致するはずの画面でも一致しなくなる(映像に切り出しを付けた後や、映像
    ソースを変えた後)。解像度だけの違いは比が同じなので含めない。条件の編集と選択待ちの
    判定が、同じくこれで決める。
    """
    return any(abs((w * frame.height) / (h * frame.width) - 1) > ASPECT_TOLERANCE
               for w, h in cond.ref_sizes)


def suggest_threshold(hits: list, misses: list) -> tuple[int | None, str]:
    """しきい値の目安と、出せないときの理由を返す。

    hits は一致するはずの画面(基準画像を登録した直後に続けて取得したもの)の
    一致度、misses は一致しないはずの画面の一致度である。目安は hits の最小と
    misses の最大の真ん中で、misses の最大より大きく、hits の最小以下になる
    (どちらの画面も正しく分ける)。

    出せないときに理由を返すのは、目安が警告なしに消えると、なぜ出ないかを利用者が
    目盛りの目印から読み取ることになるからである。一致しないはずの画面がないときは、
    目安を出す材料がないだけなので理由も出さない。

    hits が空なのは、基準画像はあるのに、どの基準画像にも続けて取得した画像がない
    ときである(基準画像がなければ、画面は目安を要求しない)。取得している間(小さな絵の
    「取得中」)に画面を離れた、映像が取れなかった、ほかから来た条件、のどれでも起きる。
    「基準画像を追加してください」とだけ表示すると、もう入っているのにと迷うので、
    足りない物を示してから、もう 1 枚追加することを書く。
    """
    if not misses:
        return None, ""
    if not hits:
        return None, ("しきい値の目安を表示できません。基準画像の追加に続けて取得した"
                      "画像がありません。判定したい場面を表示した状態で、基準画像を"
                      "もう 1 枚追加してください。")
    lo, hi = min(hits), max(misses)
    if hi >= lo:
        return None, "一致しないはずの画面と分けられません"
    return (lo + hi + 1) // 2, ""


def make_source(
    source: dict,
) -> FileSource | DShowSource | DesktopSource | WindowSource:
    """条件の source 定義から映像ソースを作る。

    装置に設定している映像(kind が lane)は、そのままでは作れない。どの映像かは
    レーンが持つので、判定のときに resolve_lane_source でレーンの設定へ
    解決してから呼ぶ。
    """
    source = validate_source(source)
    kind = source["kind"]
    if kind == "lane":
        raise ValueError(NO_LANE_VIDEO_WHY)
    if kind == "file":
        return FileSource(source["path"])
    if kind == "desktop":
        kwargs = {}
        if "monitor" in source:
            kwargs["monitor"] = source["monitor"]
        return DesktopSource(**kwargs)
    if kind == "window":
        return WindowSource(source["title"])
    return DShowSource(source["device"])


def validate_video(video) -> dict | None:
    """装置に設定している映像(padcue.json の装置の video)を確認し、正規化して返す。

    形は {"source": 映像ソース, "crop": 切り出し範囲か null} で、映像が設定されて
    いなければ None を返す。条件の判定と、スクリプトへ渡す画像と、開始の前のチェックは、
    どれもこの関数を通して読む。経路ごとに読むと、手で書き換えた padcue.json の
    誤りを、ある経路は拒否し、別の経路は例外で失敗する。source に「装置に設定している
    映像」(lane)は書けない。装置の映像がそれ自身を指すことになる。
    """
    if video is None or video == {}:
        return None
    if not isinstance(video, dict):
        raise ValueError("装置の映像の設定は、source と crop を持つオブジェクトである"
                         f"必要があります: {as_json(video)}")
    src = video.get("source")
    if not src:
        return None
    src = validate_source(src)
    if src["kind"] == "lane":
        raise ValueError("装置の映像に「装置に設定している映像」は使用できません")
    crop = video.get("crop")
    return {"source": src, "crop": None if crop is None else validate_crop(crop)}


def resolve_lane_source(source: dict, video: dict | None) -> dict | None:
    """条件の判定する映像を、レーンの設定へ解決する。

    装置に設定している映像(kind が lane)なら、そのレーンの映像の映像ソースを
    返す。レーンが映像を持たなければ None を返し、呼び出し元はそのレーンでは
    判定しない。映像ソースを指定した条件は、そのまま返す。レーンの映像の設定が
    読めなければ ValueError である(validate_video)。
    """
    if (source or {}).get("kind") != "lane":
        return source
    own = validate_video(video)
    return own["source"] if own else None


# 条件の判定する映像が「装置に設定している映像」なのに、その装置に映像がない
NO_LANE_VIDEO_WHY = ("この装置に映像が設定されていません。実行・監視の装置の一覧で、"
                     "「映像」を設定してください。")


def is_lane_source(source: dict | None) -> bool:
    """条件の判定する映像が「装置に設定している映像」か。"""
    return (source or {}).get("kind") == "lane"


def lane_file_why(lane: str, source: dict, error: str = "") -> str:
    """装置の映像が画像ファイルで、それが読めないときの理由(読めそうなら空)。

    error は取り込めなかったときの理由。ファイルの映像ソースの文(「場所を確認
    してください」「PNG で保存したファイルを指定してください」)は、条件の編集の
    画面にも実行中のレーンにもない欄を修正するよう求めることになるので、修正する
    場所を示す。
    """
    if (source or {}).get("kind") != "file":
        return ""
    problem = FileSource(source.get("path") or "").problem()
    if problem == FILE_MISSING:
        what, fix = "ファイルが見つかりません", "の場所を修正してください"
    elif problem:
        what, fix = "ファイルを読み込めません", "の場所を修正してください"
    elif error == NOT_PNG:
        what = "ファイルを PNG の画像として読み込めません"
        fix = "に PNG のファイルを指定してください"
    else:
        return ""
    return f"装置「{lane}」の映像の{what}。実行・監視で、装置の「映像」{fix}。"


def lane_crop(video: dict | None) -> tuple[float, float, float, float] | None:
    """レーンの映像の切り出し範囲(映像ソースに対する相対)。なければ None。"""
    own = validate_video(video)
    return own["crop"] if own else None


def crop_to(frame: Frame, crop) -> Frame:
    """レーンの切り出し範囲(映像に対する割合)で切り出す。crop がなければそのまま。

    判定、条件の編集、スクリプトに渡す画像で同じ切り出し方にする。幅と高さは
    1 画素を下回らない(0 になると、比べる大きさへ揃えられない)。
    """
    if not crop:
        return frame
    x, y, w, h = crop
    px = min(round(x * frame.width), frame.width - 1)
    py = min(round(y * frame.height), frame.height - 1)
    return frame.crop(px, py, max(1, round(w * frame.width)),
                      max(1, round(h * frame.height)))


# 条件の編集の画面で扱う絵の大きさの上限。これより大きい映像(複数モニターの
# デスクトップ全体、4K の映像機器)は、整数倍で間引いてから使う。基準画像は
# その間引いた絵から作るので、実行中の判定も同じく間引いてから比べる。間引き方が
# 違うと、細い線のある所で、同じ画面の一致度が編集で見た値より下がる
VIEW_MAX_W, VIEW_MAX_H = 1920, 1080


def thin_to_view(frame: Frame) -> Frame:
    """条件の編集の画面と同じ大きさまで、整数倍で間引く。収まっていればそのまま。"""
    k = max(-(-frame.width // VIEW_MAX_W), -(-frame.height // VIEW_MAX_H))
    return frame.decimate(k) if k > 1 else frame


# condition.json が持つ欄。これ以外は拒否する。手で書いた
# 打ち間違いを黙って捨てると、書いたはずの設定が反映されない理由が分からなくなる
_DOC_KEYS = {"kind", "format", "source", "regions", "threshold",
             "refs", "negatives"}


# 画像の名前の綴り。小文字の英字・数字・ハイフン・下線の 1〜64 文字に .png を付ける。
# 直後の画像は、基準画像の名前の .png の前に .a<k> を挟む。綴りを 1 つに決めるのは、
# 同じファイルを別の綴りで指定できないようにするためである。Windows は大文字と小文字の
# 違い、末尾の「.」や空白、短い別名でも同じファイルを開くので、綴りが違うだけで
# 片付けやインポートが別の画像と混同する。Windows が装置名として予約している
# 名前(con.png など)も、ファイルとして開けないので拒否する
_IMAGE_RE = re.compile(r"[0-9a-z_-]{1,64}\.png")
_AFTER_RE = re.compile(r"[0-9a-z_-]{1,64}\.a[1-9]\.png")


def image_name(rel, after: bool = True) -> str:
    """画像の相対の位置(images/<名前>.png)を確認し、ファイルの名前を返す。

    指定できるのは images/ の中の、決まった綴りの .png だけである(区切りは /)。
    after が偽なら、直後の画像の名前は受けない(並びに書けるのは基準画像と
    一致しないはずの画面だけ)。
    """
    parts = str(rel).split("/")
    if len(parts) != 2 or parts[0] != IMAGES or not parts[1]:
        raise ValueError(
            f"画像は {IMAGES}/ 内のファイルを指定してください: {as_json(rel)}")
    ok = bool(_IMAGE_RE.fullmatch(parts[1])
              or (after and _AFTER_RE.fullmatch(parts[1])))
    try:
        validate_name(parts[1])        # Windows が予約している名前を拒否する
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"画像の保存場所に使用できない名前です: {as_json(rel)}")
    return parts[1]


def after_rel(rel: str, k: int) -> str:
    """基準画像 rel の直後に取得した k 枚目(1 から)の相対の位置。"""
    return f"{rel[:-len('.png')]}.a{k}.png"


def fresh_image_names(doc: dict, images: dict) -> tuple[dict, dict]:
    """インポートする条件の画像に、この PC で新しい名前を付ける。

    images は {画像の相対の位置: 中身} である。受け取った名前のまま置くと、
    元からある条件の画像と同じ名前で別の絵が来ることがある。編集の画面の
    「元に戻す」の履歴は画像を位置で指すので、戻したときに別の絵になる。直後の画像は、
    基準画像の新しい名前に合わせる。並びから指されていない画像は置かない。
    """
    rename = {rel: f"{IMAGES}/{secrets.token_hex(8)}.png"
              for rel in [*doc["refs"], *doc["negatives"]]}
    out = {new: images[rel] for rel, new in rename.items() if rel in images}
    for rel in doc["refs"]:
        for k in range(1, AFTER_COUNT + 1):
            if after_rel(rel, k) in images:
                out[after_rel(rename[rel], k)] = images[after_rel(rel, k)]
    doc = dict(doc, refs=[rename[r] for r in doc["refs"]],
               negatives=[rename[r] for r in doc["negatives"]])
    return doc, out


def check_doc(doc: dict, who: str) -> None:
    """condition.json の欄をひととおり確認する。

    見るのは、欄の種類、判定する映像・判定する範囲・しきい値の値、
    基準画像・一致しないはずの画面の並びの形と画像の位置である。プロジェクトの
    条件を読むときも、padcue ファイルからインポートするときも、ここを通す
    (file-format.md §10-1)。同じ画像を 2 か所から指していると、片方を
    消したときにもう片方が読めなくなる。

    who は文の主語である。プロジェクトの条件は「条件「名前」」と書く。インポートでは
    画面がその件の名前を先に示すので、「条件の設定」と書く。
    """
    extra = sorted(set(doc) - _DOC_KEYS)
    if extra:
        raise ValueError(
            f"{who}に使用できない項目があります: {'、'.join(extra)}。"
            f"使用できるのは {'、'.join(sorted(_DOC_KEYS))} です。")
    missing = sorted(_DOC_KEYS - set(doc))
    if missing:
        raise ValueError(
            f"{who}に {'、'.join(missing)} がありません")
    if not isinstance(doc["refs"], list):
        raise ValueError(
            f"{who}の refs は画像の位置の配列である必要があります")
    if not isinstance(doc["negatives"], list):
        raise ValueError(
            f"{who}の negatives は画像の位置の配列である必要があります")
    seen = set()

    def one(rel) -> None:
        if not isinstance(rel, str) or not rel:
            raise ValueError(f"{who}の画像の位置は文字列である必要が"
                             f"あります: {as_json(rel)}")
        if rel in seen:
            raise ValueError(f"{who}で同じ画像を 2 回指しています: {rel}")
        seen.add(rel)
        try:
            image_name(rel, after=False)
        except ValueError as e:
            raise ValueError(f"{who}の{e}") from None

    for rel in doc["refs"]:
        one(rel)
    for rel in doc["negatives"]:
        one(rel)
    validate_source(doc["source"])
    _validate_regions(doc["regions"])
    _validate_threshold(doc["threshold"])


# 条件のフォルダーを書き換える操作のロック。いまの映像の処理(直後の画像を置く)は、
# 条件の削除・改名・置き換えと並んで実行される(ThreadingHTTPServer)。基準画像が
# あるのを確認してから書くまでの間に消されると、消した基準画像の続きだけが残る
_WRITE = threading.RLock()

# 条件のフォルダーを置き換える作業用のフォルダー(put_folder)。プロジェクトの直下に
# この頭と 16 進 8 桁の名前で作る。同じ頭のほかのファイル(.padcue-client-id)と
# 区別するのは、名前の残りの形である
_WORK_PREFIX = ".padcue-"
_WORK_RE = re.compile(r"\.padcue-[0-9a-f]{8}")


def _writes(fn):
    """ConditionStore の書き換える操作を、_WRITE の中で行う。"""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        with _WRITE:
            return fn(*args, **kwargs)
    return run


def _pointed(doc: dict) -> set[str]:
    """条件の文書が指す画像の位置。基準画像とその登録の直後の画面、一致しないはずの画面。"""
    out = set(doc.get("negatives") or [])
    for rel in doc.get("refs") or []:
        out |= {rel, *(after_rel(rel, k) for k in range(1, AFTER_COUNT + 1))}
    return out


class ConditionStore:
    """プロジェクト直下 conditions/<名前>/ に 1 件ずつ置く。

    ::

        conditions/<名前>/condition.json
        conditions/<名前>/images/3f2a9c0d1b7e4a55.png

    condition.json は
    {"kind":"condition", "format":1, "source":{...},
     "regions":[[x,y,w,h],...], "threshold":88,
     "refs":["images/3f2a9c0d1b7e4a55.png",...],
     "negatives":["images/8c01d2e95a3f7b60.png",...]}
    である。名前はフォルダー名なので文書の中には書かない。

    refs は基準画像で、いずれか 1 枚に一致すれば一致である。negatives は一致しない
    はずの画面で、判定には使わず、しきい値の目安の一致しない側に使う。

    基準画像を登録した直後に続けて取得した画像(比較の大きさ)は、基準画像の隣に
    images/3f2a9c0d1b7e4a55.a1.png、images/3f2a9c0d1b7e4a55.a2.png の名前で
    置く。判定には使わず、目安の一致する側に使う。condition.json には書かない。
    名前で基準画像と結び付くので、一覧を持つと同じ事実が 2 か所になる。

    画像はフォルダーの中の相対の位置で指す。フォルダーの外や絶対の位置は
    指定できないので、フォルダーごと置き換えても名前を変えても condition.json の中を
    書き換えずに済む。

    画像は、編集の画面が追加したときに置く(add_image)。条件がその画像を使うのは、
    保存して condition.json の並びに入れてからである。並びから外した画像も、
    元に戻せるようにすぐには消さない。どこからも指されず、置いてから 7 日たった
    画像は、操作画面を起動したときに消す(sweep)。

    画像の名前は、追加したときに作る 16 桁の 16 進の乱数である。同じ名前を別の画像に
    使わない。編集の画面の「元に戻す」の履歴は画像を位置で指すので、消した画像の位置に
    別の画像が来ると、戻したときに別の絵になる。

    条件の一覧と保存場所の決まりは、このクラスだけが持つ。project.py も
    条件の一覧と condition.json の位置をここで調べる。

    書き込みは一時ファイルに書いてから replace_file で置き換える(project.py の
    _save_config_locked と同じ。書き込みの途中で異常終了しても、元のファイルを
    壊さない)。
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    @property
    def dir(self) -> Path:
        return self.root / "conditions"

    def cond_dir(self, name: str) -> Path:
        return self.dir / validate_name(name)

    def doc_path(self, name: str) -> Path:
        """条件の condition.json の位置。"""
        return self.cond_dir(name) / DOC_NAME

    def image_path(self, name: str, rel: str) -> Path:
        """画像の相対の位置を、実際の保存場所に変換する。

        指定できるのは images/ の中の、決まった綴りのファイルだけである(image_name)。
        手で書いた condition.json がフォルダーの外を指していたら拒否する。
        """
        return self.cond_dir(name) / IMAGES / image_name(rel)

    def names(self) -> list[str]:
        """条件の名前。並べ替えてあればその順、なければ名前順。

        並び順は手順・フレーム表と同じファイルに持つ(names.apply_order)。
        """
        if not self.dir.is_dir():
            return []
        names = [d.name for d in self.dir.iterdir()
                 if (d / DOC_NAME).is_file()]
        return apply_order(self.root, names, "conditions")

    def exists(self, name: str) -> bool:
        return self.doc_path(name).is_file()

    def _read_doc(self, name: str) -> dict:
        """condition.json を読んで、頭の 2 欄を確認する。"""
        path = self.doc_path(name)
        if not path.is_file():
            raise ValueError(f"条件「{name}」が見つかりません")
        # json の例外の文は利用者に読ませない(手順の読み込みと同じ言い方にする)
        try:
            doc = json.loads(read_text(path))
        except json.JSONDecodeError as e:
            raise ValueError(sentences(
                f"条件「{name}」のファイルが壊れています",
                f"{e.lineno} 行目 {e.colno} 文字目から読み込めません")) from None
        except UnicodeDecodeError:
            raise ValueError(
                f"条件「{name}」の文字コードが UTF-8 ではありません") from None
        except RecursionError:
            # 入れ子が深すぎて JSON の読み込みが再帰の上限に当たる。一覧の行の
            # 理由にして、毎秒の状態の取得を妨げない
            raise ValueError(f"条件「{name}」のファイルが壊れています") from None
        except OSError:
            raise ValueError(f"条件「{name}」のファイルを読み込めません") from None
        if not isinstance(doc, dict):
            raise ValueError(f"条件「{name}」の中身がオブジェクトではありません")
        if doc.get("kind") != "condition":
            raise ValueError(f"条件「{name}」は条件ファイルの形式ではありません")
        fmt = doc.get("format")
        # 読めるバージョンより新しい条件は、padcue を更新すれば読める(手順と同じ)
        if is_int(fmt) and fmt > FORMAT_VERSION:
            raise ValueError(f"条件「{name}」は新しいバージョンの padcue で"
                             "作成されています。padcue を更新してください。")
        if not is_int(fmt) or fmt != FORMAT_VERSION:
            raise ValueError(f"条件「{name}」の format は {FORMAT_VERSION} である"
                             f"必要があります: {as_json(fmt)}")
        check_doc(doc, f"条件「{name}」")
        return doc

    def _write_doc(self, name: str, doc: dict) -> None:
        d = self.cond_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        write_file(d / DOC_NAME, json.dumps(doc, ensure_ascii=False, indent=2))

    def source_of(self, name: str) -> dict:
        """条件が判定する映像の定義だけを読む(画像は読まない)。

        開始の前の参照チェックは、手順が使う条件の数だけこれを呼ぶ。load は
        基準画像の PNG を復号して比較の大きさへ縮めるので、映像の定義を見る
        だけならその手間が必要ない。
        """
        return self._read_doc(name)["source"]

    def problem(self, name: str) -> str:
        """条件を読めないときの理由(読めれば空)。一覧の行の「エラー」に出す。

        load と同じく condition.json の欄と値を確認し、基準画像のファイルがあるかを
        見る。ない条件は開けない(load)ので、一覧で普通の行に見えると、どの条件が
        開けないかが開くまで分からない。文は load と同じにする。画像の中身は読まない。
        一覧は毎秒作るので、復号は重すぎる。読めない画像は、開いたときと開始の前の
        チェックが知らせる。一覧はこの答えを problem_key が同じ間だけ使い回す。
        """
        try:
            doc = self._read_doc(name)
        except ValueError as e:      # 読めないファイルも ValueError で来る(_read_doc)
            return str(e)
        for rel in doc["refs"]:
            try:
                there = self.image_path(name, rel).is_file()
            except ValueError as e:     # 並びの綴りが画像の位置でない
                return str(e)
            if not there:
                return f"条件「{name}」の基準画像が見つかりません({rel})"
        return ""

    def problem_key(self, name: str, rev: str) -> str:
        """problem の答えを使い回してよい間、同じになる値。

        condition.json のリビジョン(rev)に、画像のフォルダーにあるファイルの名前を
        付ける。画像だけが消されても condition.json は変わらないので、rev だけでは
        消えたことに気づかない。名前を並べるだけで、画像は読まない。
        """
        try:
            images = sorted(os.listdir(self.cond_dir(name) / IMAGES))
        except (OSError, ValueError):
            images = []
        return rev + "\n" + "\n".join(images)

    @_writes
    def save(self, name: str, source: dict,
             regions: list[tuple[float, float, float, float]],
             threshold, refs: list[str] | None = None,
             negatives: list[str] | None = None) -> list[str]:
        """条件の設定と画像の並びを書く(なければ、条件を作る)。

        refs と negatives は、保存した後の基準画像と一致しないはずの画面の並びで、
        要素は images/ の中の画像の相対の位置である。add_image で置いたばかりの
        画像も指定できる。None の側は、画像の並びを今のままにする。

        書くのは condition.json だけで、画像は消さない。並びから外した画像も
        フォルダーに残るので、編集の画面は元に戻して保存し直せる。どこからも
        指されず、置いてから 7 日たった画像は、操作画面を起動したときに sweep が
        消す。並びから外した画像は、外した時刻を置いた時刻とする(更新時刻を今に
        する)。置いた時刻のままだと、日がたった画像を外すと次の起動で消え、
        起動し直した後の元に戻すで戻せない。

        並びのうち、フォルダーにない画像は外して保存し、外した位置を返す。条件や
        画像がほかの操作で消されていると、並びに入れた画像でもフォルダーにない
        ことがある。消えた画像は作り直せないので外す。
        """
        name = validate_name(name)
        source = validate_source(source)
        regions = _validate_regions(regions)
        threshold = _validate_threshold(threshold)
        doc = self._read_doc(name) if self.exists(name) else {
            "kind": "condition", "format": FORMAT_VERSION,
            "refs": [], "negatives": [],
        }
        pointed_before = _pointed(doc)
        doc["source"] = source
        doc["regions"] = [list(r) for r in regions]
        doc["threshold"] = threshold
        dropped: list[str] = []
        if refs is not None or negatives is not None:
            given = {"refs": refs, "negatives": negatives}
            # 並びを渡さなかった側の画像も、2 か所から指していないかの照合に入れる
            seen = {rel for key, rels in given.items() if rels is None
                    for rel in doc[key]}
            for key, side in (("refs", "基準画像"),
                              ("negatives", "一致しないはずの画面")):
                rels = given[key]
                if rels is None:
                    continue
                kept = []
                for rel in rels:
                    if not isinstance(rel, str) or not rel:
                        raise ValueError(
                            f"{side}の指定が正しくありません: {as_json(rel)}")
                    if rel in seen:
                        raise ValueError(f"画像「{rel}」が 2 回指定されています")
                    seen.add(rel)
                    image_name(rel, after=False)
                    if self.image_path(name, rel).is_file():
                        kept.append(rel)
                    else:
                        dropped.append(rel)
                doc[key] = kept
        self._write_doc(name, doc)
        for rel in pointed_before - _pointed(doc):
            # 外した時刻から数える(sweep)。消えていれば何もしない
            with contextlib.suppress(OSError):
                os.utime(self.image_path(name, rel))
        return dropped

    @_writes
    def add_image(self, name: str, png: bytes, what: str = "画像") -> str:
        """画像を 1 枚置き、その相対の位置を返す。condition.json は書き換えない。

        条件がその画像を使うのは、保存(save)で並びに入れてからである。名前は
        乱数で作り、使い回さない。what は、壊れた画像を拒否するときに何の画像かを
        示す語である。
        """
        _decode(png, what)
        self._read_doc(name)
        while True:
            rel = f"{IMAGES}/{secrets.token_hex(8)}.png"
            path = self.image_path(name, rel)
            if not path.exists():
                break
        path.parent.mkdir(parents=True, exist_ok=True)
        write_file(path, png)
        return rel

    @_writes
    def add_after(self, name: str, rel: str, png: bytes) -> int:
        """基準画像 rel の直後に取得した画像を、空いている次の番号で置く。

        rel は保存する前の基準画像でもよい(追加した直後に取得するため)。置いた番号
        (1 から)を返す。条件か基準画像がもうないか、AFTER_COUNT 枚揃って
        いれば、置かずに 0 を返す。
        """
        _decode(png, "画像")
        self._read_doc(name)
        if not self.image_path(name, rel).is_file():
            return 0
        for k in range(1, AFTER_COUNT + 1):
            path = self.image_path(name, after_rel(rel, k))
            if not path.is_file():
                write_file(path, png)
                return k
        return 0

    @_writes
    def sweep(self) -> int:
        """images/ の中の、どの条件からも指されず、置いてから SWEEP_AGE_S(7 日)
        たったファイルを消し、その数を返す。

        編集の画面は、追加した画像を保存する前に置き、外した画像も元に戻せるように
        残す(add_image・save)。編集の画面は padcue を起動し直しても開いたままで、
        接続し直すと編集を続けられるので、指されていない画像も、置いてから日の
        浅いものはその編集が使っているかもしれない。操作画面は起動のときに片付ける。
        condition.json を読めない条件のフォルダーは変えない(どれが何の画像か
        分からない)。

        指されている画像は、名前の綴りではなく、開いたファイルそのもので照合する。
        Windows は大文字と小文字の違いや短い別名でも同じファイルを開くので、綴りで
        照合すると、読めていた画像を消すことがある。ファイルを区別できない
        ファイルシステムでは消さない。
        """
        if not self.dir.is_dir():
            return 0
        n = 0
        now = time.time()
        for folder in sorted(self.dir.iterdir()):
            images = folder / IMAGES
            if not (folder / DOC_NAME).is_file() or not images.is_dir():
                continue
            try:
                doc = self._read_doc(folder.name)
            except (ValueError, OSError):
                continue
            keep = set()
            for rel in _pointed(doc):
                try:
                    st = self.image_path(folder.name, rel).stat()
                except OSError:
                    continue
                keep.add((st.st_dev, st.st_ino))
            for path in images.iterdir():
                try:
                    st = path.stat()
                except OSError:
                    continue
                same = (st.st_dev, st.st_ino) in keep
                if not path.is_file() or not st.st_ino or same:
                    continue
                if now - st.st_mtime < SWEEP_AGE_S:
                    continue
                try:
                    path.unlink()
                except OSError:
                    continue
                n += 1
        return n

    def after_files(self, name: str, rel: str) -> list[str]:
        """基準画像 rel の直後に取得した画像のうち、置いてあるもの(取得した順)。"""
        out = []
        for k in range(1, AFTER_COUNT + 1):
            each = after_rel(rel, k)
            if self.image_path(name, each).is_file():
                out.append(each)
        return out

    def refs(self, name: str) -> list[str]:
        """基準画像の一覧(登録した順)。"""
        return list(self._read_doc(name)["refs"])

    def negatives(self, name: str) -> list[str]:
        """一致しないはずの画面の一覧(追加した順)。"""
        return list(self._read_doc(name)["negatives"])

    def files(self, name: str) -> list[str]:
        """条件が持つ画像のすべて(エクスポートとインポートで使う)。"""
        doc = self._read_doc(name)
        out = []
        for rel in doc["refs"]:
            out += [rel, *self.after_files(name, rel)]
        return out + list(doc["negatives"])

    def has_refs(self, name: str) -> bool:
        """基準画像を持つか(画像は読まない)。開始の前のチェックに使う。"""
        return bool(self._read_doc(name)["refs"])

    def image_bytes(self, name: str, rel: str) -> bytes:
        # 確認してから読むと、その間に消されたとき(条件の削除と毎秒の採点が
        # 重なったとき)に例外が抜ける。読んだ結果で拒否する
        try:
            return self.image_path(name, rel).read_bytes()
        except FileNotFoundError:
            raise ValueError(f"画像「{rel}」が見つかりません") from None
        except OSError:
            raise ValueError(f"画像「{rel}」を読み込めません") from None

    def load(self, name: str) -> Condition:
        name = validate_name(name)
        doc = self._read_doc(name)
        refs, sizes = [], []
        for rel in doc["refs"]:
            path = self.image_path(name, rel)
            try:
                refs.append(file_frame(path))
                with path.open("rb") as f:
                    sizes.append(png_size(f.read(PNG_HEAD_SIZE)))
            except FileNotFoundError:
                raise ValueError(f"条件「{name}」の基準画像が見つかりません"
                                 f"({rel})") from None
            except (OSError, ValueError):
                raise ValueError(f"条件「{name}」の基準画像を読み込めません"
                                 f"({rel})") from None
        return Condition(name=name, source=doc["source"],
                         regions=[tuple(r) for r in doc["regions"]],
                         refs=refs, threshold=doc["threshold"],
                         ref_files=list(doc["refs"]),
                         negatives=list(doc["negatives"]), ref_sizes=sizes)

    @_writes
    def delete(self, name: str) -> None:
        """条件 name のフォルダーを消す。消せなければ OSError(画像を別のアプリで開いて
        いるときなど)。何も知らせないと、条件が一覧に残ったまま成功に見える。もう
        消えていた条件は、消せたものとする。
        """
        remove_tree(self.cond_dir(name))

    @_writes
    def put_folder(self, name: str, doc_text: str, images: dict) -> None:
        """条件 name のフォルダーを、渡した中身で丸ごと作る(あれば置き換える)。

        中身は先に別の場所へすべて書き、書き終えてから入れ替える。元のフォルダーを
        消してから書くと、途中で 1 枚でも書けなかったときに、元の条件も新しい
        条件も失う。別の場所は conditions/ の外に作る(中に作ると、書いている間
        だけ条件の一覧に出る)。
        """
        dst = self.cond_dir(name)
        work = self.dir.parent / f"{_WORK_PREFIX}{secrets.token_hex(4)}"
        old = work / "old"
        try:
            (work / "new" / IMAGES).mkdir(parents=True)
            (work / "new" / DOC_NAME).write_text(doc_text, encoding="utf-8")
            for rel, data in images.items():
                (work / "new" / rel).write_bytes(data)
            self.dir.mkdir(parents=True, exist_ok=True)
            # フォルダーの名前の変更は、ほかのプロセスが中のファイルを開いている間は
            # 拒否される。書いた直後のフォルダーは、たまにこれで拒否され、すぐに
            # 変えられるようになるので、少し待ってやり直す
            if dst.exists():
                retry_while_in_use(lambda: dst.replace(old))
            try:
                retry_while_in_use(lambda: (work / "new").replace(dst))
            except BaseException:
                if old.exists():
                    retry_while_in_use(lambda: old.replace(dst))    # 元の条件を戻す
                raise
        except BaseException as e:
            # 元の条件を戻せなかったときは、バックアップを消さずに場所を示す。消すと、
            # 元の条件はどこにも残らない。文はそのまま画面に出す(OSError に
            # すると、画面の文が決まり文句に置き換わり、場所が消える)。場所は
            # プロジェクトのフォルダーからの位置で示す
            if old.exists():
                where = old.relative_to(self.dir.parent)
                raise ValueError(sentences(
                    f"条件「{name}」を置き換えられず、元に戻すこともできません",
                    f"元の条件は、プロジェクトのフォルダーの「{where}」に残っています",
                    f"conditions フォルダーへ移して、名前を「{name}」に変更して"
                    "ください")) from e
            with contextlib.suppress(OSError):
                remove_tree(work)
            raise
        # 置き換えは済んだ。前の中身(old)は、読んでいる途中の読む側がいても少し待って
        # 消す。それでも消せないときは残す(保存は済んでいるので、失敗にはしない。
        # 残ったものは、次に操作画面を起動したときに sweep_work が消す)
        with contextlib.suppress(OSError):
            remove_tree(work)

    @_writes
    def sweep_work(self) -> int:
        """put_folder の作業用フォルダーの残りを消し、消した数を返す。

        残るのは、置き換えた後に前の中身(old)を消せなかったときと、書いている途中で
        padcue が終了したときである。どちらもプロジェクトの直下にあり、利用者には
        何のフォルダーか分からない。new と old の両方があるものは消さない。元の条件を
        戻せなかったと場所を知らせたコピーか、入れ替えの途中で終了して conditions/ に
        ない元の条件である。操作画面を起動したときに、sweep と同じく片付ける。消せない
        ものは残す。
        """
        n = 0
        for d in sorted(self.dir.parent.glob(f"{_WORK_PREFIX}*")):
            if not _WORK_RE.fullmatch(d.name) or not d.is_dir():
                continue
            if (d / "new").exists() and (d / "old").exists():
                continue
            try:
                remove_tree(d)
            except OSError:
                continue
            n += 1
        return n

    @_writes
    def copy(self, src: str, dst: str) -> None:
        """条件を別の名前へコピーする。画像も一緒に来る。

        名前はフォルダー名そのもので、画像はフォルダーの中の相対の位置で指して
        いるので、フォルダーを丸ごとコピーするだけで済む(file-format.md §9)。
        """
        src, dst = validate_name(src), validate_name(dst)
        if not self.exists(src):
            raise ValueError(f"条件「{src}」が見つかりません")
        refuse_taken(self.names(), dst)
        shutil.copytree(self.cond_dir(src), self.cond_dir(dst))

    @_writes
    def rename(self, old: str, new: str) -> None:
        old, new = validate_name(old), validate_name(new)
        if old == new:
            return
        if not self.exists(old):
            raise ValueError(f"条件「{old}」が見つかりません")
        refuse_taken(self.names(), new, old)
        # 名前はフォルダー名そのものなので、付け替えるだけで済む。condition.json の
        # 中は名前を持たず、画像もフォルダーの中の相対の位置で指している。中の画像を
        # 画面や別のプロセスが一瞬開いていると名前の変更が拒否されるので、少し待って
        # やり直す
        retry_while_in_use(lambda: self.cond_dir(old).rename(self.cond_dir(new)))
