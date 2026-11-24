"""フレーム表(frametables/*.csv)の読み込みと検証(file-format.md §9-2)。

- 1 行=1 フレーム。列は使うものだけ書く
- 空セルと未記載列は、ボタンは離し、軸は 0 になる(加速度は静止の値)。表はすべての
  入力を行のとおりに決める
- 検証: 知らないヘッダー・重なったヘッダー・値域外・空ファイル・空行はエラー
- F 列: データの行の番号(ヘッダーを除いて 1 から。無効にした行も数える)の連番検証。
  rep 列: 行の反復回数。
  disabled 列: 1 ならその行を無効にする(丸ごとなかったことにし、時間も消費しない)
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .binfmt import BUTTONS, REST_AX, REST_AY, REST_AZ, STICK_MAX, STICK_MIN
from .words import sentences

# 軸の列。(列名、入力の状態の欄、空欄のときに入る値)。空欄は「何も指定しない =
# 静止して構えている」という意味なので、加速度だけは 0 ではなく重力ぶんが入る
# (全軸 0 は自由落下。binfmt.REST_A* 参照)。表の読み込み、変換、手動操作の記録、
# タイムラインの図は、どれもこの並びを使う
AXES = (("LX", "lx", 0), ("LY", "ly", 0), ("RX", "rx", 0), ("RY", "ry", 0),
        ("GY", "gy", 0), ("GR", "gz", 0), ("GP", "gx", 0),
        ("AX", "ax", REST_AX), ("AY", "ay", REST_AY), ("AZ", "az", REST_AZ))
_AXIS_COLS = {"LX", "LY", "RX", "RY"}
_MOTION_COLS = {"GP", "GY", "GR", "AX", "AY", "AZ"}
_BLANK = {col: rest for col, _key, rest in AXES}
_META_COLS = {"F", "rep", "disabled"}   # disabled: その行を丸ごとなかったことにする
# 展開後の行数の上限(60 fps で約 27 分。誤記入の防止策)。手動操作の記録も
# この長さで停止する(record.py)
MAX_ROWS = 100_000


class FrametableError(Exception):
    """フレーム表を読めない理由。row は CSV のファイルの行の番号(ヘッダーの行が 1)。

    文では「ファイルの N 行目」と書く。編集画面の表には行の番号がなく、左端の
    「フレーム」は rep と無効にした行で行の数とずれるので、表で数える番号では
    場所を間違える。ファイルの行なら、エディターの行の番号と同じである。
    """

    def __init__(self, msg: str, row: int | None = None):
        where = f"ファイルの {row} 行目: " if row is not None else ""
        super().__init__(f"{where}{msg}")
        self.row = row
        self.msg = msg


def frametable_failure(name: str, e: Exception) -> str:
    """フレーム表を読めなかった理由を、画面に出す文にする。

    e は読み込みの失敗(FrametableError、UnicodeDecodeError、OSError)である。手順の
    変換の失敗、フレーム表の一覧の「エラー」、編集画面で開けないときの文は、
    どれもこれで作る。どのフレーム表かを文の中で示すのは、手順の失敗として
    出るときに、手順のどこが悪いのかと読み違えないためである。OS の文と
    ファイルの場所は出さない。
    """
    if isinstance(e, FrametableError):
        if e.row is not None:
            return f"フレーム表「{name}」のファイルの {e.row} 行目: {e.msg}"
        return f"フレーム表「{name}」: {e.msg}"
    if isinstance(e, UnicodeDecodeError):
        return f"フレーム表「{name}」の文字コードが UTF-8 ではありません"
    if isinstance(e, FileNotFoundError):
        return f"フレーム表「{name}」が見つかりません"
    if isinstance(e, PermissionError):
        return sentences(f"フレーム表「{name}」のファイルを読み込めません",
                         "ほかのアプリで開いていないか確認してください")
    return f"フレーム表「{name}」のファイルを読み込めません"


def frametable_reason(e: Exception) -> str:
    """フレーム表を開けない理由を、どの表かを示さずに書く。

    編集画面は、開けない表の名前をメッセージの頭で 1 回だけ示し
    (「「名前」を開けません。」)、この文を続ける。frametable_failure の文は
    「フレーム表「名前」の」で始まるので、続けると同じ名前を 2 度読ませる。行の番号は、
    コロンでつながず「N 行目の」と書く(表は開いていないので、表の行と混同しない)。
    手順の変換の失敗、一覧の「エラー」、コマンドの出力は、frametable_failure のまま
    である。
    """
    if isinstance(e, FrametableError):
        return f"{e.row} 行目の{e.msg}" if e.row is not None else e.msg
    if isinstance(e, UnicodeDecodeError):
        return "文字コードが UTF-8 ではありません"
    if isinstance(e, FileNotFoundError):
        return "ファイルが見つかりません"
    if isinstance(e, PermissionError):
        return sentences("ファイルを読み込めません",
                         "ほかのアプリで開いていないか確認してください")
    return "ファイルを読み込めません"


@dataclass(frozen=True)
class Frametable:
    name: str
    columns: tuple[str, ...]          # メタ列を除く記載列(順序保持)
    rows: tuple[dict, ...]            # rep 展開済み。各行: {列名: int}


def _parse_cell(col: str, cell: str, rowno: int) -> int:
    cell = cell.strip()
    if col in BUTTONS:
        if cell in ("", "0"):
            return 0
        if cell == "1":
            return 1
        raise FrametableError(f"ボタン列 {col} の値は 1、0、空欄のいずれか"
                              f"である必要があります: 「{cell}」", rowno)
    if cell == "":
        return _BLANK.get(col, 0)
    try:
        v = int(cell)
    except ValueError:
        raise FrametableError(f"列 {col} の値が整数ではありません: 「{cell}」",
                              rowno) from None
    if col in _AXIS_COLS:
        if not (STICK_MIN <= v <= STICK_MAX):
            raise FrametableError(
                f"列 {col} の値は {STICK_MIN} から {STICK_MAX} までの整数"
                            f"である必要があります: {v}", rowno)
    else:  # motion
        if not (-32768 <= v <= 32767):
            raise FrametableError(f"列 {col} の値は -32768 から 32767 までの整数"
                                  f"である必要があります: {v}", rowno)
    return v


def check_header(cells: list[str]) -> list[str]:
    """ヘッダーの行の列名を確認し、並びを返す。

    末尾の空の列名は 1 つだけ取り除く(表計算ソフトが行の末尾に付けるカンマ)。
    空の列名・フレーム表にない列名・2 つ目の同じ列名があれば FrametableError を
    投げる。編集画面の読み込み(project.load_frametable_grid)もこれで確認する。
    表に当てられない列は画面に出せず、保存すると値が警告なしに消えるからである。
    """
    header = [h.strip() for h in cells]
    if header and header[-1] == "":  # 末尾カンマ由来の空列のみ許容
        header = header[:-1]
    seen: set[str] = set()
    for h in header:
        if h == "":
            raise FrametableError("列名が空欄です", 1)
        if h not in BUTTONS and h not in _AXIS_COLS and h not in _MOTION_COLS \
                and h not in _META_COLS:
            raise FrametableError(f"「{h}」はフレーム表の列名ではありません", 1)
        if h in seen:
            raise FrametableError(f"列名「{h}」が 2 つあります", 1)
        seen.add(h)
    return header


def width_text(got: int, width: int) -> str:
    """行の列の数がヘッダーの行と違うときの文。"""
    return f"列の数が正しくありません(この行は {got} 列、ヘッダーの行は {width} 列)"


def fit_cells(raw: list[str], width: int, lineno: int) -> list[str]:
    """データの行のセルを、ヘッダーの列の数(width)より多くならないように整える。

    ヘッダーより後ろのセルがすべて空なら取り除く(表計算ソフトが行の末尾に付ける
    カンマ)。空でないセルがヘッダーより多ければ、その値を当てる列がないので
    FrametableError を投げる。lineno はファイルの行の番号である。編集画面の
    読み込み(project.load_frametable_grid)もこれで確認する。当てる列のない値は
    画面に出せず、保存すると警告なしに消えるからである。ヘッダーより少ない行は返す
    (足りないセルは空欄と同じ意味で、読み込みは load_frametable が拒否する)。
    """
    cells = [c.strip() for c in raw]
    if len(cells) > width and all(c == "" for c in cells[width:]):
        cells = cells[:width]  # 末尾カンマ由来の余分な空セル
    if len(cells) > width:
        raise FrametableError(width_text(len(cells), width), lineno)
    return cells


def load_frametable(name: str, text: str) -> Frametable:
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    try:
        header = next(reader)
    except StopIteration:
        raise FrametableError("空のファイルです") from None
    header = check_header(header)
    data_cols = tuple(h for h in header if h not in _META_COLS)
    if not data_cols:
        raise FrametableError("ボタン・スティック・ジャイロ・加速度の列がありません", 1)

    rows: list[dict] = []
    csv_rowno = 0  # データ行の番号(1 始まり)
    for lineno, raw in enumerate(reader, start=2):
        if not raw:
            # セルが 1 つもない行(区切りのない空の行)。「すべて離す 1 フレーム」との
            # 混同を避けるためエラー(列数が揃った全空セル行は、何も押さない
            # 1 フレームとして読む)
            raise FrametableError("空行があります。削除してください。", lineno)
        cells = fit_cells(raw, len(header), lineno)
        if len(cells) != len(header):
            raise FrametableError(width_text(len(cells), len(header)), lineno)
        csv_rowno += 1
        rec = dict(zip(header, cells, strict=True))
        if "F" in rec and rec["F"].strip() != "":
            try:
                f_val = int(rec["F"])
            except ValueError:
                raise FrametableError(
                    f"F 列の値が正しくありません: 「{rec['F']}」。"
                    "フレーム表の編集画面で保存し直してください。",
                    lineno) from None
            if f_val != csv_rowno:
                raise FrametableError(
                    f"F 列の値は {csv_rowno} である必要があります: {f_val}。"
                    "フレーム表の編集画面で保存し直してください。", lineno)
        # 無効にした行は、時間も消費せず丸ごとなかったことにする(F の連番チェックの
        # 後で判定するので、無効にした行もデータの行の番号に数える)
        flag = rec.get("disabled", "").strip()
        if flag not in ("", "0", "1"):
            raise FrametableError(f"disabled 列の値は 1、0、空欄のいずれか"
                                  f"である必要があります: 「{flag}」", lineno)
        if flag == "1":
            continue
        rep = 1
        if "rep" in rec and rec["rep"].strip() != "":
            try:
                rep = int(rec["rep"])
            except ValueError:
                raise FrametableError(
                    f"rep 列が整数ではありません: 「{rec['rep']}」", lineno) from None
            if rep < 1:
                raise FrametableError(
                    f"rep は 1 以上である必要があります: {rep}", lineno)
        values = {c: _parse_cell(c, rec[c], lineno) for c in data_cols}
        # 上限は展開する前に確認する。大きな rep を先に展開すると、上限で拒否する
        # 前に、その数だけメモリと時間を使う
        if len(rows) + rep > MAX_ROWS:
            raise FrametableError(
                f"展開後の行数が上限({MAX_ROWS})を超えました", lineno)
        rows.extend([values] * rep)

    if not rows:
        raise FrametableError("データ行がありません")
    return Frametable(name, data_cols, tuple(rows))
