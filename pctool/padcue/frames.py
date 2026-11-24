"""画像の基本(画素の配列、PNG の符号化と復号)。

映像判定が使う部品の 1 つ。ここには画面を RGB の配列として扱う共通の型 Frame と、
PNG との相互変換だけを置く。映像ソースは sources.py、条件の定義と
照合は conditions.py に分ける。ここは sources.py / conditions.py に依存しない
(向きは一方通行)。

PNG の復号は標準ライブラリ(zlib)だけの純 Python 実装を持つ。cv2 が
import できる環境では cv2.imdecode を先に試して高速化するが(cv2 は
なくても全機能が動く前提の任意最適化)、対応形式の検証は cv2 の有無に
関わらず常に同じ規則で行う(下記 decode_png 参照)。
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

# numpy と cv2 は別々に確認する。numpy だけが入っていても、縮小
# (Frame.shrink)は速い経路を使える。cv2 は import のときに numpy を読み込むので、
# cv2 が使えるなら numpy も使える
try:
    import numpy as _np
except ImportError:
    _np = None
try:
    import cv2 as _cv2
except ImportError:
    _cv2 = None


# ---- Frame ----


# 1 画素のバイト数。R, G, B がこの順で並ぶ
CHANNELS = 3


@dataclass(frozen=True)
class Frame:
    """RGB を持つ静止画(行優先、8bit、1 画素 3 バイト)。

    rgb の長さは必ず width*height*3。判定はすべてこの型を経由して行う
    (映像ソースが PNG でもほかの形式でも、比較の直前には Frame に変換する)。

    色まで持つのは、明るさが同じで色だけ違う画面(同じ位置に出る赤と青の
    表示、色で種類を分けるアイコン)を区別するためである。
    """

    width: int
    height: int
    rgb: bytes

    def __post_init__(self) -> None:
        if len(self.rgb) != self.width * self.height * CHANNELS:
            raise ValueError(
                f"rgb の長さが width*height*3 と合いません: "
                f"{len(self.rgb)} != {self.width}*{self.height}*3")

    def resize(self, width: int, height: int) -> Frame:
        """新しい大きさへ変える。

        両辺とも縮む(width<=元, height<=元)ときはボックス平均、それ以外
        (拡大、または片辺だけ縮む混在)は最近傍で行う。判定の主用途は
        取り込みサイズから比較サイズ(conditions.py の COMPARE_W x COMPARE_H)
        への縮小で、これは常にボックス平均になる。
        """
        if width <= 0 or height <= 0:
            raise ValueError(f"width/height は正の整数にしてください: {width}x{height}")
        if width == self.width and height == self.height:
            return self
        if width <= self.width and height <= self.height:
            return self._box_average(width, height)
        return self._nearest(width, height)

    def _box_average(self, width: int, height: int) -> Frame:
        src_w, src_h, src = self.width, self.height, self.rgb
        src_row = src_w * CHANNELS
        x_bounds = [(i * src_w // width,
                     max(i * src_w // width + 1,
                         ((i + 1) * src_w + width - 1) // width))
                    for i in range(width)]
        y_bounds = [(j * src_h // height,
                     max(j * src_h // height + 1,
                         ((j + 1) * src_h + height - 1) // height))
                    for j in range(height)]
        out = bytearray(width * height * CHANNELS)
        for j, (y0, y1) in enumerate(y_bounds):
            y1 = min(y1, src_h)
            row_out = j * width * CHANNELS
            for i, (x0, x1) in enumerate(x_bounds):
                x1 = min(x1, src_w)
                r = g = b = count = 0
                for y in range(y0, y1):
                    off = y * src_row + x0 * CHANNELS
                    for _ in range(x0, x1):
                        r += src[off]
                        g += src[off + 1]
                        b += src[off + 2]
                        off += CHANNELS
                        count += 1
                k = row_out + i * CHANNELS
                if count:
                    out[k] = r // count
                    out[k + 1] = g // count
                    out[k + 2] = b // count
        return Frame(width, height, bytes(out))

    def _nearest(self, width: int, height: int) -> Frame:
        src_w, src_h, src = self.width, self.height, self.rgb
        src_row = src_w * CHANNELS
        xs = [min(src_w - 1, i * src_w // width) * CHANNELS
              for i in range(width)]
        out = bytearray(width * height * CHANNELS)
        for j in range(height):
            y = min(src_h - 1, j * src_h // height)
            row_off = y * src_row
            base = j * width * CHANNELS
            for i, x in enumerate(xs):
                k = base + i * CHANNELS
                out[k:k + CHANNELS] = src[row_off + x:row_off + x + CHANNELS]
        return Frame(width, height, bytes(out))

    def shrink(self, width: int, height: int) -> Frame:
        """縮める(画像処理の速い経路)。値は resize のボックス平均と同じになる。

        numpy があれば、累積和で同じボックス平均を求める(1080p を比較の大きさへ
        数十 ms)。なければ resize(純 Python、0.4 秒)。どちらも同じ箱の境界と
        同じ整数の割り算なので、環境によって判定の差の値が変わらない(面積で
        重み付けする縮め方を混ぜると、縮小比が整数でないときに差が大きく変わり、
        調整したしきい値の一致・不一致が入れ替わる)。拡大は resize に任せる。
        """
        if width == self.width and height == self.height:
            return self
        if _np is None or width > self.width or height > self.height:
            return self.resize(width, height)
        src_w, src_h = self.width, self.height
        arr = _np.frombuffer(self.rgb, dtype=_np.uint8).reshape(
            src_h, src_w, CHANNELS)

        def bounds(n_out: int, n_src: int):
            # _box_average と同じ境界: 始まりは切り捨て、終わりは切り上げ(最低 1 画素)
            i = _np.arange(n_out)
            lo = i * n_src // n_out
            end = ((i + 1) * n_src + n_out - 1) // n_out
            hi = _np.minimum(_np.maximum(lo + 1, end), n_src)
            return lo, hi

        x0, x1 = bounds(width, src_w)
        y0, y1 = bounds(height, src_h)
        # 縦の累積和から、行の範囲ごとの和を出す(縦 1 列の和は 255 x 高さで、int32 に
        # 収まる)。範囲ごとにしてから横の累積和を取ると、大きな配列を 1 度しか通らない
        acc_y = _np.zeros((src_h + 1, src_w, CHANNELS), dtype=_np.int32)
        _np.cumsum(arr, axis=0, dtype=_np.int32, out=acc_y[1:])
        bands = (acc_y[y1] - acc_y[y0]).astype(_np.int64)
        acc_x = _np.zeros((height, src_w + 1, CHANNELS), dtype=_np.int64)
        _np.cumsum(bands, axis=1, out=acc_x[:, 1:])
        total = acc_x[:, x1] - acc_x[:, x0]
        count = ((y1 - y0)[:, None] * (x1 - x0)[None, :])[:, :, None]
        out = (total // count).astype(_np.uint8)
        return Frame(width, height, out.tobytes())

    def decimate(self, k: int) -> Frame:
        """k 画素ごとに 1 画素を取り出して縮める(最近傍)。

        行と列の抜き出しだけ(バイト列のスライス)なので、ボックス平均の縮小より
        桁違いに速い。細部は失われるが、巨大な絵を画面に出す前の間引きには
        足りる。
        """
        if k <= 1:
            return self
        w, h = self.width, self.height
        ow, oh = (w + k - 1) // k, (h + k - 1) // k
        row = w * CHANNELS
        orow = ow * CHANNELS
        step = k * CHANNELS
        out = bytearray(orow * oh)
        for j, y in enumerate(range(0, h, k)):
            src = self.rgb[y * row:(y + 1) * row]
            base = j * orow
            for c in range(CHANNELS):
                out[base + c:base + orow:CHANNELS] = src[c::step]
        return Frame(ow, oh, bytes(out))

    def crop(self, x: int, y: int, w: int, h: int) -> Frame:
        """(x, y) を左上とする w×h を切り出す。範囲外は切り詰める。"""
        x0 = max(0, min(x, self.width))
        y0 = max(0, min(y, self.height))
        x1 = max(x0, min(x + w, self.width))
        y1 = max(y0, min(y + h, self.height))
        out_w, out_h = x1 - x0, y1 - y0
        row = out_w * CHANNELS
        out = bytearray(row * out_h)
        for r in range(out_h):
            src_off = ((y0 + r) * self.width + x0) * CHANNELS
            out[r * row:(r + 1) * row] = self.rgb[src_off:src_off + row]
        return Frame(out_w, out_h, bytes(out))


# ---- PNG ----

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_CRC_CHECKED = (b"IHDR", b"PLTE", b"IDAT", b"IEND")
# 受けるカラータイプと、1 画素のバイト数(ビット深度 8)
_BPP_BY_COLOR_TYPE = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
# 受ける画像の画素の数の上限。映像の 1 枚は大きくても 4K(3840x2160)で、その
# 4 倍(8K、7680x4320)までを受ける。上限がないと、IHDR に大きな幅と高さを書いた
# 小さなファイルで、復号の前に大きなメモリを確保させられる
MAX_PIXELS = 7680 * 4320
# PNG の頭(署名と、先頭の IHDR のチャンク)のバイト数。check_png_head が見る
PNG_HEAD_SIZE = 8 + 8 + 13 + 4


class ImageTooLarge(ValueError):
    """画素数が上限(MAX_PIXELS)を超える画像。文はそのまま利用者に出せる。

    PNG でないものと区別して知らせるのに使う。大きすぎる画像に「PNG として読めない」と
    表示すると、PNG で保存し直しても解決しない。
    """


def check_png_head(head: bytes) -> tuple[int, int]:
    """PNG の頭(署名と、先頭の IHDR のチャンク)だけを見て、(幅, 高さ) を返す。

    ファイルの残りを読む前に、PNG でないものと、大きすぎる画像(MAX_PIXELS を
    超えるもの)を拒否するのに使う。拒否するときは ValueError(理由つき)。CRC と
    残りのチャンクは見ない(decode_png が見る)。
    """
    if head[:8] != _PNG_SIG:
        raise ValueError("PNG の署名が不正です")
    if len(head) < PNG_HEAD_SIZE or head[8:16] != b"\x00\x00\x00\x0dIHDR":
        raise ValueError("先頭のチャンクが IHDR ではありません")
    width, height = struct.unpack_from(">II", head, 16)
    if not width or not height:
        raise ValueError("幅か高さが 0 の画像です")
    if width * height > MAX_PIXELS:
        raise ImageTooLarge(f"画像が大きすぎます({width}x{height})。"
                            "7680x4320 の画素数まで受け付けます。")
    return width, height


def _iter_chunks(data: bytes):
    """PNG のチャンク列を辿る。IHDR/PLTE/IDAT/IEND は CRC を検証する。"""
    if data[:8] != _PNG_SIG:
        raise ValueError("PNG の署名が不正です")
    pos = 8
    n = len(data)
    while pos + 8 <= n:
        length = struct.unpack_from(">I", data, pos)[0]
        ctype = data[pos + 4:pos + 8]
        cdata_start = pos + 8
        cdata_end = cdata_start + length
        if cdata_end + 4 > n:
            raise ValueError("PNG のチャンクがデータ末尾を超えています")
        cdata = data[cdata_start:cdata_end]
        crc = struct.unpack_from(">I", data, cdata_end)[0]
        if ctype in _CRC_CHECKED and zlib.crc32(ctype + cdata) != crc:
            raise ValueError(f"{ctype.decode('ascii', 'replace')} の CRC が不正です")
        yield ctype, cdata
        pos = cdata_end + 4
        if ctype == b"IEND":
            return


def _paeth_row(row: bytearray, prev: bytearray, bpp: int, row_bytes: int) -> None:
    for i in range(bpp):
        row[i] = (row[i] + prev[i]) & 0xFF     # a=c=0 のとき予測子は常に b
    for i in range(bpp, row_bytes):
        a = row[i - bpp]
        b = prev[i]
        c = prev[i - bpp]
        p = a + b - c
        pa = p - a if p >= a else a - p
        pb = p - b if p >= b else b - p
        pc = p - c if p >= c else c - p
        if pa <= pb and pa <= pc:
            pr = a
        elif pb <= pc:
            pr = b
        else:
            pr = c
        row[i] = (row[i] + pr) & 0xFF


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytearray:
    """スキャンラインのフィルター(0..4)を解除する(PNG 仕様 §9)。

    行ごとに bytearray で作業し、1 画素ごとの Python オブジェクト生成を
    避ける。行頭 bpp バイトは「左」「左上」が存在しない(=0 扱い)ので、
    分岐を減らすために先頭 bpp バイトだけ別ループで処理する。
    """
    row_bytes = width * bpp
    stride = row_bytes + 1
    if len(raw) != stride * height:
        raise ValueError(
            f"展開後のデータ長が不正です: {len(raw)} (期待 {stride * height})")
    out = bytearray(row_bytes * height)
    prev = bytearray(row_bytes)     # 1 行目の「上」は全 0
    buf = memoryview(raw)
    for y in range(height):
        base = y * stride
        filt = buf[base]
        row = bytearray(buf[base + 1:base + 1 + row_bytes])
        if filt == 0:
            pass
        elif filt == 1:      # Sub: 左だけ足す(先頭 bpp バイトは無変化)
            for i in range(bpp, row_bytes):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif filt == 2:      # Up: 上だけ足す
            for i in range(row_bytes):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif filt == 3:      # Average: 左と上の平均
            for i in range(bpp):
                row[i] = (row[i] + (prev[i] >> 1)) & 0xFF
            for i in range(bpp, row_bytes):
                row[i] = (row[i] + ((row[i - bpp] + prev[i]) >> 1)) & 0xFF
        elif filt == 4:      # Paeth
            _paeth_row(row, prev, bpp, row_bytes)
        else:
            raise ValueError(f"未対応のフィルターです: {filt}")
        out[y * row_bytes:(y + 1) * row_bytes] = row
        prev = row
    return out


def _to_rgb(pixels: bytearray, width: int, height: int, color_type: int,
            palette: bytes) -> bytearray:
    """フィルターを解いた画素を R, G, B の並びにする。

    並べ替えはスライスの代入で行う。1 画素ずつの Python のループは、1080p で
    秒の単位がかかる。パレットにない色の番号は、呼ぶ前に拒否しておく。
    """
    n = width * height
    if color_type == 2:
        return pixels
    out = bytearray(n * 3)
    if color_type == 0:              # グレー
        for c in range(3):
            out[c::3] = pixels
    elif color_type == 4:            # グレー+α(αは捨てる)
        gray = pixels[0::2]
        for c in range(3):
            out[c::3] = gray
    elif color_type == 6:            # RGBA(αは捨てる)
        for c in range(3):
            out[c::3] = pixels[c::4]
    elif color_type == 3:            # パレット(tRNS は無視する)
        # 色の番号から、R、G、B ごとの 256 個の表で色を求める
        table = palette[:768].ljust(768, b"\0")
        for c in range(3):
            out[c::3] = pixels.translate(table[c::3])
    return out


def _check_png(data: bytes) -> tuple[int, int, int, bytes, bytes]:
    """署名、チャンクの並びと CRC、IHDR、圧縮したデータの展開を確認し、対応できる
    PNG かを判定する。対応できなければ ValueError(理由つき)。

    返すのは (幅, 高さ, カラータイプ, パレット, 展開したデータ) である。

    cv2 を使う経路でも必ずここを通す。cv2 は 16bit やインターレースも黙って
    解き、CRC の壊れたチャンクも警告だけで通し、展開すると IHDR の大きさより
    長くなるデータも余りを捨てて読む。拒否する規則が cv2 の有無で変わらない
    ように、検証はここで一本化する。展開は zlib(C の実装)なので速い。
    """
    chunks = _iter_chunks(data)
    try:
        ctype, cdata = next(chunks)
    except StopIteration:
        raise ValueError("IHDR チャンクがありません") from None
    if ctype != b"IHDR":
        raise ValueError(f"先頭のチャンクが IHDR ではありません: {ctype!r}")
    # IHDR は 13 バイトと決まっている。違う長さを解釈すると struct の例外になり、
    # 読めない画像として拒否できない
    if len(cdata) != 13:
        raise ValueError(f"IHDR チャンクの長さが違います({len(cdata)} バイト)")
    (width, height, bit_depth, color_type, _comp, _filt,
     interlace) = struct.unpack(">IIBBBBB", cdata)
    if bit_depth != 8:
        raise ValueError("ビット深度 8 以外の PNG には対応していません"
                         f"(この画像は {bit_depth} ビット)")
    if color_type not in _BPP_BY_COLOR_TYPE:
        raise ValueError(f"未対応のカラータイプです: {color_type}")
    if interlace != 0:
        raise ValueError("インターレース PNG は未対応です")
    if not width or not height:
        raise ValueError("幅か高さが 0 の画像です")
    if width * height > MAX_PIXELS:
        raise ImageTooLarge(f"画像が大きすぎます({width}x{height})。"
                            "7680x4320 の画素数まで受け付けます。")
    # 残りのチャンクも終わりまで読み、CRC を確認する
    seen = []
    palette = b""
    idat = bytearray()
    for ctype, cdata in chunks:
        seen.append(ctype)
        if ctype == b"PLTE":
            palette = cdata
        elif ctype == b"IDAT":
            idat += cdata
    # IHDR は先頭の 1 つだけである。2 つ目を受けると、上で確認した大きさと、
    # 復号が使う大きさが一致しない
    if b"IHDR" in seen:
        raise ValueError("IHDR チャンクが 2 つあります")
    if b"IDAT" not in seen:
        raise ValueError("IDAT チャンクがありません")
    if b"IEND" not in seen:
        raise ValueError("IEND チャンクがありません")
    if color_type == 3 and not palette:
        raise ValueError("パレット形式なのに PLTE チャンクがありません")
    # 展開するのは IHDR の大きさの 1 バイト先までにする。それより長いデータは
    # 長さの違いとして拒否する(展開の量を、書かれた大きさで抑える)
    expected = height * (width * _BPP_BY_COLOR_TYPE[color_type] + 1)
    inflate = zlib.decompressobj()
    try:
        raw = inflate.decompress(bytes(idat), expected + 1)
    except zlib.error as e:
        # チャンクの CRC は合っていても、圧縮したデータそのものが壊れていることがある
        raise ValueError(f"画像のデータを展開できません: {e}") from None
    # 圧縮の終わり(Adler-32 まで)が揃っていなければ、途中で切れたデータである
    if len(raw) == expected and not inflate.eof:
        raise ValueError("画像のデータが途中で切れています")
    if len(raw) != expected:
        raise ValueError(f"展開後のデータ長が不正です: {len(raw)} (期待 {expected})")
    return width, height, color_type, palette, raw


def _decode_with_cv2(data: bytes) -> tuple[int, int, bytes] | None:
    """cv2 が使えれば imdecode で復号する(失敗すれば None を返す)。"""
    try:
        arr = _np.frombuffer(data, dtype=_np.uint8)
        img = _cv2.imdecode(arr, _cv2.IMREAD_UNCHANGED)
        if img is None:
            return None
        if img.ndim == 2:
            rgb = _cv2.cvtColor(img, _cv2.COLOR_GRAY2RGB)
        elif img.shape[2] == 4:
            rgb = _cv2.cvtColor(img, _cv2.COLOR_BGRA2RGB)
        else:
            rgb = _cv2.cvtColor(img, _cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        return w, h, rgb.tobytes()
    except Exception:      # noqa: BLE001  高速化経路の失敗は純 Python 実装へ切り替える
        return None


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """PNG を復号し (width, height, RGB) を返す。

    対応: ビット深度 8、カラータイプ 0(グレー)、2(RGB)、3(パレット。tRNS は
    無視)、4(グレー+α)、6(RGBA)、フィルター 0〜4(Paeth 含む)、
    非インターレース。16bit とインターレースは ValueError(理由つき)。CRC は
    IHDR、PLTE、IDAT、IEND で検証する(zlib.crc32)。

    cv2 が import できれば cv2.imdecode を先に試す(cv2 は BGR で返すため
    RGB に並べ替える)。失敗すれば純 Python 実装に切り替わる。対応形式と
    チャンクの検証と展開した長さの確認(上記)は cv2 の有無に関わらず先に行う。
    パレット形式は、パレットにない色の番号を見つけるのにフィルターを解く必要が
    あるので、cv2 を使わずに純 Python で復号する(cv2 はその画素を黒にして読む)。
    これで、拒否する画像は環境で変わらない。
    """
    # 16bit・インターレース・壊れたチャンク・長さの合わないデータはここで拒否する
    width, height, color_type, palette, raw = _check_png(data)
    if _cv2 is not None and color_type != 3:
        result = _decode_with_cv2(data)
        if result is not None:
            return result
    return _decode_png_pure(width, height, color_type, palette, raw)


def _decode_png_pure(width: int, height: int, color_type: int, palette: bytes,
                     raw: bytes) -> tuple[int, int, bytes]:
    """_check_png が確かめて展開したデータから、RGB を作る。"""
    bpp = _BPP_BY_COLOR_TYPE[color_type]
    pixels = _unfilter(raw, width, height, bpp)
    # PLTE が短いと、色の番号からパレットの色を求められない
    if color_type == 3 and pixels and max(pixels) * 3 + 3 > len(palette):
        raise ValueError("パレットにない色の番号があります")
    rgb = _to_rgb(pixels, width, height, color_type, palette)
    return width, height, bytes(rgb)


def _make_chunk(ctype: bytes, cdata: bytes) -> bytes:
    return (struct.pack(">I", len(cdata)) + ctype + cdata
            + struct.pack(">I", zlib.crc32(ctype + cdata)))


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """RGB から PNG を作る(フィルター 0 固定・カラータイプ 2・zlib 圧縮)。

    条件の画像の保存やテストで使う。速さより単純さを優先し、フィルターは
    常に 0(無変換)にする。
    """
    if len(rgb) != width * height * 3:
        raise ValueError(
            f"rgb の長さが width*height*3 と合いません: "
            f"{len(rgb)} != {width}*{height}*3")
    row_bytes = width * 3
    stride = row_bytes + 1
    raw = bytearray(stride * height)
    for y in range(height):
        raw[y * stride + 1:(y + 1) * stride] = rgb[y * row_bytes:(y + 1) * row_bytes]
    compressed = zlib.compress(bytes(raw), 6)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (_PNG_SIG
            + _make_chunk(b"IHDR", ihdr)
            + _make_chunk(b"IDAT", compressed)
            + _make_chunk(b"IEND", b""))


def encode_png_fast(width: int, height: int, rgb: bytes) -> bytes:
    """RGB から PNG を作る(速い経路)。

    cv2 があれば imencode で作る(1080p で 30 ms 前後。純 Python は 100 ms)。
    できる PNG は 8 bit の RGB で、decode_png がそのまま読める。なければ
    encode_png と同じ。画面へ毎秒送る絵に使う。
    """
    if _cv2 is None:
        return encode_png(width, height, rgb)
    arr = _np.frombuffer(rgb, dtype=_np.uint8).reshape(height, width, CHANNELS)
    ok, buf = _cv2.imencode(".png", _cv2.cvtColor(arr, _cv2.COLOR_RGB2BGR),
                            [_cv2.IMWRITE_PNG_COMPRESSION, 1])
    if not ok:
        return encode_png(width, height, rgb)
    return buf.tobytes()


def frame_from_png(data: bytes) -> Frame:
    """PNG バイト列から Frame を作る(縮小はしない。元の大きさのまま)。"""
    width, height, rgb = decode_png(data)
    return Frame(width, height, rgb)
