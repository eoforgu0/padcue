"""静止画(Frame)と PNG の符号化と復号(frames.py)。

PNG の復号は cv2 があれば既定でそちらを通るため、フィルター、カラータイプ、
拒否条件をテストする項目は `frames._cv2` を None にして純 Python 実装を
指定して通す(cv2 の有無でテスト結果が変わらないようにするため)。
"""
from __future__ import annotations

import importlib.util
import struct
import sys
import zlib

import pytest

from padcue import frames
from padcue.frames import Frame, decode_png, encode_png, frame_from_png

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


# ---- テスト専用の小さな PNG エンコーダー(フィルターを指定して作れる) ----


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _filter_row(row: bytes, prev: bytes | None, bpp: int, filt: int) -> bytearray:
    out = bytearray(len(row))
    for i in range(len(row)):
        a = row[i - bpp] if i >= bpp else 0
        b = prev[i] if prev is not None else 0
        c = prev[i - bpp] if (prev is not None and i >= bpp) else 0
        if filt == 0:
            pred = 0
        elif filt == 1:
            pred = a
        elif filt == 2:
            pred = b
        elif filt == 3:
            pred = (a + b) // 2
        elif filt == 4:
            pred = _paeth(a, b, c)
        else:
            raise ValueError(f"未知のフィルター: {filt}")
        out[i] = (row[i] - pred) & 0xFF
    return out


def _chunk(ctype: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", zlib.crc32(ctype + data)))


def make_png(width: int, height: int, pixels: bytes, *, bpp: int, color_type: int,
             filt: int = 0, bit_depth: int = 8, interlace: int = 0,
             palette: bytes = b"", trns: bytes = b"") -> bytes:
    """テスト専用の PNG エンコーダー。フィルターとカラータイプを自由に選べる。"""
    row_bytes = width * bpp
    raw = bytearray()
    prev = None
    for y in range(height):
        row = pixels[y * row_bytes:(y + 1) * row_bytes]
        raw.append(filt)
        raw += _filter_row(row, prev, bpp, filt)
        prev = row
    compressed = zlib.compress(bytes(raw), 6)
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0,
                       interlace)
    body = _chunk(b"IHDR", ihdr)
    if palette:
        body += _chunk(b"PLTE", palette)
    if trns:
        body += _chunk(b"tRNS", trns)
    body += _chunk(b"IDAT", compressed)
    body += _chunk(b"IEND", b"")
    return _PNG_SIG + body


@pytest.fixture
def pure(monkeypatch):
    """cv2 があっても純 Python 実装を強制的に通す。"""
    monkeypatch.setattr(frames, "_cv2", None)


# ---- Frame ----


def gray(n):
    """明るさ n の灰色 1 画素ぶんの RGB(n, n, n)。"""
    return bytes([n, n, n])


def test_frame_length_mismatch_rejected():
    with pytest.raises(ValueError, match="width\\*height"):
        Frame(2, 2, b"\x00\x00\x00")


def test_resize_box_average_exact():
    # 4x1 -> 2x1: [10,20,30,40] -> [(10+20)/2, (30+40)/2]
    big = Frame(4, 1, b"".join(gray(v) for v in (10, 20, 30, 40)))
    small = big.resize(2, 1)
    assert list(small.rgb) == [15, 15, 15, 35, 35, 35]


def test_resize_box_average_2d():
    # 4x2 -> 2x1: 上下左右 4 画素ずつの平均
    src = b"".join(gray(v) for v in (0, 10, 20, 30,
                                     40, 50, 60, 70))
    big = Frame(4, 2, src)
    small = big.resize(2, 1)
    # 左ブロック(0,10,40,50)平均 25 / 右ブロック(20,30,60,70)平均 45
    assert list(small.rgb) == [25, 25, 25, 45, 45, 45]


def test_resize_box_average_keeps_channels_apart():
    """ボックス平均は R と G と B をそれぞれ平均すること。

    R、G、B を混ぜると、明るさが同じで色だけ違う画面を区別できなくなる。
    """
    src = bytes([10, 20, 30, 50, 60, 70])      # 2x1 の 2 画素
    small = Frame(2, 1, src).resize(1, 1)
    assert list(small.rgb) == [30, 40, 50]


def test_decimate_takes_every_kth_pixel():
    """間引きは k 画素ごとに 1 画素を取り出し、R、G、B の並びを保つこと。"""
    w, h = 5, 4
    rgb = b"".join(bytes((x, y, 100 + x)) for y in range(h) for x in range(w))
    f = frames.Frame(w, h, rgb)
    g = f.decimate(2)
    assert (g.width, g.height) == (3, 2)
    got = [tuple(g.rgb[i:i + 3]) for i in range(0, len(g.rgb), 3)]
    assert got == [(0, 0, 100), (2, 0, 102), (4, 0, 104),
                   (0, 2, 100), (2, 2, 102), (4, 2, 104)]
    assert f.decimate(1) is f


def test_resize_nearest_enlarge():
    small = Frame(2, 1, gray(15) + gray(35))
    big = small.resize(4, 1)
    assert list(big.rgb) == [15, 15, 15, 15, 15, 15, 35, 35, 35, 35, 35, 35]


def test_resize_same_size_is_noop():
    f = Frame(2, 2, bytes(12))
    assert f.resize(2, 2) is f


def test_crop_inside():
    f = Frame(3, 3, b"".join(gray(v) for v in range(9)))
    c = f.crop(1, 1, 2, 2)
    assert (c.width, c.height) == (2, 2)
    assert list(c.rgb) == [4, 4, 4, 5, 5, 5, 7, 7, 7, 8, 8, 8]


def test_crop_truncates_out_of_range():
    f = Frame(3, 3, b"".join(gray(v) for v in range(9)))
    c = f.crop(-1, -1, 2, 2)
    assert (c.width, c.height) == (1, 1)
    assert list(c.rgb) == [0, 0, 0]
    c2 = f.crop(2, 2, 5, 5)
    assert (c2.width, c2.height) == (1, 1)
    assert list(c2.rgb) == [8, 8, 8]
    c3 = f.crop(10, 10, 2, 2)
    assert (c3.width, c3.height) == (0, 0)
    assert c3.rgb == b""


# ---- PNG 往復(公開 API。cv2 の有無に関わらず通る) ----


def test_encode_decode_roundtrip():
    w, h = 5, 3
    rgb = bytes((i * 37) % 256 for i in range(w * h * 3))
    png = encode_png(w, h, rgb)
    w2, h2, rgb2 = decode_png(png)
    assert (w2, h2, rgb2) == (w, h, rgb)


def test_frame_from_png():
    w, h = 2, 2
    rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])
    f = frame_from_png(encode_png(w, h, rgb))
    assert (f.width, f.height) == (w, h)
    assert f.rgb == rgb


def test_encode_png_length_mismatch_rejected():
    with pytest.raises(ValueError, match="width\\*height\\*3"):
        encode_png(2, 2, b"\x00" * 5)


# ---- 純 Python 復号: フィルター 0〜4 をすべて使う ----


@pytest.mark.parametrize("filt", [0, 1, 2, 3, 4])
def test_all_filters_decode(pure, filt):
    w, h = 5, 4
    # 単調でない値にして各予測子の違いが出るようにする
    rgb = bytes((i * 53 + i * i) % 256 for i in range(w * h * 3))
    png = make_png(w, h, rgb, bpp=3, color_type=2, filt=filt)
    w2, h2, rgb2 = decode_png(png)
    assert (w2, h2, rgb2) == (w, h, rgb)


# ---- 純 Python 復号: カラータイプ 0/3/4/6 ----


def test_color_type_0_gray(pure):
    w, h = 3, 2
    levels = bytes([0, 40, 80, 120, 160, 200])
    png = make_png(w, h, levels, bpp=1, color_type=0, filt=2)
    w2, h2, rgb = decode_png(png)
    assert (w2, h2) == (w, h)
    expected = b"".join(gray(g) for g in levels)
    assert rgb == expected


def test_color_type_3_palette_ignores_trns(pure):
    w, h = 2, 2
    palette = bytes([255, 0, 0,   0, 255, 0,   0, 0, 255,   10, 20, 30])
    # 無視されるべき値(index3 を全透明扱いにしても結果に影響しないはず)
    trns = bytes([0, 128, 255, 0])
    idx = bytes([0, 1, 2, 3])
    png = make_png(w, h, idx, bpp=1, color_type=3, filt=1,
                   palette=palette, trns=trns)
    w2, h2, rgb = decode_png(png)
    assert (w2, h2) == (w, h)
    expected = b"".join(palette[i * 3:i * 3 + 3] for i in idx)
    assert rgb == expected


@pytest.mark.parametrize("fast", [True, False], ids=["cv2 があれば使う", "純 Python"])
def test_a_palette_shorter_than_the_indexes_is_refused(monkeypatch, fast):
    """パレットにない色の番号を使う PNG は、cv2 の有無に関わらず理由つきで拒否すること。

    パレットにない番号のまま変換すると IndexError が外へ抜け、条件の画面の「画像を
    読み込めません」にならない。cv2 はその画素を黒にして読むので、拒否する規則が
    環境で変わると、ある PC で読める条件の画像が、別の PC では読めない。
    """
    if not fast:
        monkeypatch.setattr(frames, "_cv2", None)
    palette = bytes([255, 0, 0,   0, 255, 0])          # 2 色だけ
    png = make_png(2, 1, bytes([0, 2]), bpp=1, color_type=3, palette=palette)
    with pytest.raises(ValueError, match="パレットにない色の番号"):
        decode_png(png)


def test_color_type_4_gray_alpha_drops_alpha(pure):
    w, h = 2, 2
    # (gray, alpha) のグループ。alpha は捨てられる
    pixels = bytes([10, 255, 20, 0, 30, 128, 40, 64])
    png = make_png(w, h, pixels, bpp=2, color_type=4, filt=3)
    w2, h2, rgb = decode_png(png)
    assert (w2, h2) == (w, h)
    assert rgb == bytes([10, 10, 10, 20, 20, 20, 30, 30, 30, 40, 40, 40])


def test_color_type_6_rgba_drops_alpha(pure):
    w, h = 2, 1
    pixels = bytes([255, 0, 0, 200, 0, 255, 0, 50])
    png = make_png(w, h, pixels, bpp=4, color_type=6, filt=4)
    w2, h2, rgb = decode_png(png)
    assert (w2, h2) == (w, h)
    assert rgb == bytes([255, 0, 0, 0, 255, 0])


# ---- 拒否条件 ----


def test_16bit_rejected(pure):
    ihdr = struct.pack(">IIBBBBB", 1, 1, 16, 2, 0, 0, 0)
    png = _PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IEND", b"")
    with pytest.raises(ValueError, match="16"):
        decode_png(png)


def test_interlace_rejected(pure):
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 1)
    png = _PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IEND", b"")
    with pytest.raises(ValueError, match="インターレース"):
        decode_png(png)


def test_unsupported_color_type_rejected(pure):
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 1, 0, 0, 0)  # 1 は未定義
    png = _PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IEND", b"")
    with pytest.raises(ValueError, match="カラータイプ"):
        decode_png(png)


def test_crc_corruption_in_idat_rejected(pure):
    png = bytearray(encode_png(4, 4, bytes(4 * 4 * 3)))
    # IDAT のデータ先頭 1 バイトを壊す(署名 8 + IHDR chunk 25 = 33、
    # IDAT のデータは長さ 4+種別 4 を挟んだ 33+8=41 バイト目から)
    png[41] ^= 0xFF
    with pytest.raises(ValueError, match="CRC"):
        decode_png(bytes(png))


def test_crc_corruption_in_ihdr_rejected(pure):
    png = bytearray(encode_png(4, 4, bytes(4 * 4 * 3)))
    png[16] ^= 0xFF     # IHDR のデータ領域(署名 8 + 長さ 4 + 種別 4 = 16 から)
    with pytest.raises(ValueError, match="CRC"):
        decode_png(bytes(png))


def test_bad_signature_rejected(pure):
    with pytest.raises(ValueError, match="署名"):
        decode_png(b"not a png" + b"\x00" * 20)


@pytest.mark.parametrize("fast", [True, False], ids=["cv2 があれば使う", "純 Python"])
def test_a_huge_image_is_refused_before_decoding(monkeypatch, fast):
    """IHDR の画素の数が上限を超える画像は、復号する前に拒否すること。

    小さなファイルでも、IHDR に大きな幅と高さを書けば、復号のために大きな
    メモリを確保させられる。
    """
    if not fast:
        monkeypatch.setattr(frames, "_cv2", None)
    ihdr = struct.pack(">IIBBBBB", 100000, 100000, 8, 2, 0, 0, 0)
    png = (_PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(b"\0"))
           + _chunk(b"IEND", b""))
    with pytest.raises(ValueError, match="大きすぎます"):
        decode_png(png)


@pytest.mark.parametrize("fast", [True, False], ids=["cv2 があれば使う", "純 Python"])
def test_a_second_ihdr_is_refused(monkeypatch, fast):
    """IHDR が 2 つある画像は拒否すること。

    大きさの上限を確認するのは先頭の IHDR である。後の IHDR を受けると、確認して
    いない大きさで復号する。
    """
    if not fast:
        monkeypatch.setattr(frames, "_cv2", None)
    small = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    large = struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0)
    png = (_PNG_SIG + _chunk(b"IHDR", small) + _chunk(b"IHDR", large)
           + _chunk(b"IDAT", zlib.compress(b"\0abcdef")) + _chunk(b"IEND", b""))
    with pytest.raises(ValueError, match="IHDR チャンクが 2 つ"):
        decode_png(png)


@pytest.mark.parametrize("fast", [True, False], ids=["cv2 があれば使う", "純 Python"])
def test_too_much_image_data_is_refused_either_way(monkeypatch, fast):
    """展開すると IHDR の大きさより長くなる画像は、cv2 の有無に関わらず拒否すること。

    cv2 は余った分を警告にとどめて読む。拒否する規則が環境で変わると、ある PC で
    読める条件の画像が、別の PC では読めない。
    """
    if not fast:
        monkeypatch.setattr(frames, "_cv2", None)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    body = zlib.compress(b"\0abc" + b"\0def")       # 1 行の大きさに 2 行ぶん
    png = (_PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", body)
           + _chunk(b"IEND", b""))
    with pytest.raises(ValueError, match="展開後のデータ長"):
        decode_png(png)


@pytest.mark.parametrize("fast", [True, False], ids=["cv2 があれば使う", "純 Python"])
def test_a_cut_compressed_stream_is_refused(monkeypatch, fast):
    """圧縮の終わり(Adler-32)が欠けた画像は、画素の数が合っていても、cv2 の有無に
    関わらず拒否すること。"""
    if not fast:
        monkeypatch.setattr(frames, "_cv2", None)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    body = zlib.compress(b"\0abc")[:-4]          # 末尾の 4 バイトが Adler-32
    png = (_PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", body)
           + _chunk(b"IEND", b""))
    with pytest.raises(ValueError, match="途中で切れています"):
        decode_png(png)


@pytest.mark.parametrize("fast", [True, False], ids=["cv2 があれば使う", "純 Python"])
def test_a_broken_end_chunk_is_rejected_either_way(monkeypatch, fast):
    """IEND の CRC が壊れた PNG と、IEND のない PNG は、cv2 の有無に関わらず拒否する
    こと。

    cv2 は IEND の CRC が壊れていても警告だけで復号する。拒否する規則が環境で
    変わると、ある PC で読める条件の画像が、別の PC では読めない。
    """
    if not fast:
        monkeypatch.setattr(frames, "_cv2", None)
    png = encode_png(1, 1, b"abc")
    broken = bytearray(png)
    broken[-1] ^= 0x01                  # 末尾の 4 バイトは IEND の CRC
    with pytest.raises(ValueError, match="IEND の CRC"):
        decode_png(bytes(broken))
    with pytest.raises(ValueError, match="IEND チャンクがありません"):
        decode_png(png[:-12])           # IEND のチャンクは 12 バイト


@pytest.mark.parametrize("fast", [True, False], ids=["cv2 があれば使う", "純 Python"])
def test_an_ihdr_of_the_wrong_length_is_refused(monkeypatch, fast):
    """IHDR の長さが 13 バイトでない画像は、読めない画像として拒否すること。

    長さを確認せずに解釈すると struct の例外になり、画面には「予期しないエラー」と出る。
    """
    if not fast:
        monkeypatch.setattr(frames, "_cv2", None)
    short = struct.pack(">IIBBBB", 1, 1, 8, 2, 0, 0)
    png = (_PNG_SIG + _chunk(b"IHDR", short) + _chunk(b"IDAT", zlib.compress(b"\0abc"))
           + _chunk(b"IEND", b""))
    with pytest.raises(ValueError, match="IHDR チャンクの長さ"):
        decode_png(png)


@pytest.mark.parametrize("fast", [True, False], ids=["cv2 があれば使う", "純 Python"])
def test_a_corrupt_compressed_stream_is_refused(monkeypatch, fast):
    """CRC は合っていても圧縮したデータが壊れた画像は、読めない画像として拒否すること。

    zlib の例外のまま上へ出すと、画面には「予期しないエラー」と出る。
    """
    if not fast:
        monkeypatch.setattr(frames, "_cv2", None)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    broken = b"\x78\x9c\xff\xff\xff\xff"        # zlib の頭の後が壊れている
    png = (_PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", broken)
           + _chunk(b"IEND", b""))
    with pytest.raises(ValueError, match="展開できません"):
        decode_png(png)


# ---- numpy だけが入っている環境 ----


def test_numpy_alone_gives_the_fast_shrink(monkeypatch):
    """cv2 がなく numpy だけがある環境でも、縮小は速い経路を通ること。値は
    純 Python のボックス平均と同じになること。

    cv2 がないだけで numpy の経路まで使わないと、1080p の縮小に 0.4 秒かかる
    (速い経路は数十 ms)。条件の判定は numpy を別に読み込むので、同じ環境で
    片方だけが速くなる。
    """
    pytest.importorskip("numpy")
    # frames.py をもう 1 つ別の名前で読み込み、その中でだけ cv2 をないことにする。
    # 本物の frames を読み直すと、ほかのモジュールが持つ Frame と別物になる
    monkeypatch.setitem(sys.modules, "cv2", None)   # import cv2 が ImportError になる
    spec = importlib.util.spec_from_file_location("_frames_without_cv2",
                                                  frames.__file__)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    assert mod._cv2 is None
    assert mod._np is not None, "cv2 がないだけで numpy も使わなくなっている"
    # 縮小比が整数でない大きさにする(境界の扱いの違いが値に出やすい)
    w, h = 321, 181
    f = mod.Frame(w, h, bytes((x * 7 + y * 13) % 256
                              for y in range(h) for x in range(w * 3)))
    plain = f.resize(160, 90)
    # 速い経路を通ったことは、純 Python の縮小を呼ばないことで見る
    monkeypatch.setattr(mod.Frame, "resize", lambda *a: pytest.fail("純 Python の経路"))
    assert f.shrink(160, 90).rgb == plain.rgb
