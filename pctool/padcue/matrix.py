"""行列部品(parts/*.csv)の読み込みと検証(flow-format.md §3)。

- 1行=1フレーム。列は使うものだけ書く
- 記載列の空セル = 離す/0。未記載列 = 直前の状態を継続(展開はコンパイラ側)
- 検証: 未知ヘッダ・重複ヘッダ・値域外・空ファイル・空行はエラー
- F 列: CSV 行番号(1始まり)の連番検証。rep 列: 行の反復回数。
  off 列: 1 ならその行を丸ごと飛ばす(時間も消費しない)
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .binfmt import BUTTONS, REST_AX, REST_AY, REST_AZ, STICK_MAX, STICK_MIN

_AXIS_COLS = {"LX", "LY", "RX", "RY"}
_MOTION_COLS = {"GP", "GY", "GR", "AX", "AY", "AZ"}
# 空欄のときに入る値。「何も指定しない = 静止して構えている」という意味なので、
# 加速度だけは 0 ではなく重力ぶんが入る(全軸 0 は自由落下。binfmt.REST_A* 参照)
_BLANK = {"AX": REST_AX, "AY": REST_AY, "AZ": REST_AZ}
_META_COLS = {"F", "rep", "off"}   # off: その行を丸ごと無かったことにする
_MAX_ROWS = 100_000  # 展開後の上限(約28分。誤記入の防止策)


class PartError(Exception):
    def __init__(self, msg: str, row: int | None = None):
        where = f"{row}行目: " if row is not None else ""
        super().__init__(f"{where}{msg}")
        self.row = row
        self.msg = msg


@dataclass(frozen=True)
class Part:
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
        raise PartError(f"ボタン列 {col} の値が不正です: {cell!r}(1/0/空のみ)", rowno)
    if cell == "":
        return _BLANK.get(col, 0)
    try:
        v = int(cell)
    except ValueError:
        raise PartError(f"列 {col} の値が整数ではありません: {cell!r}", rowno) from None
    if col in _AXIS_COLS:
        if not (STICK_MIN <= v <= STICK_MAX):
            raise PartError(
                f"列 {col} が範囲外です: {v}(許容 {STICK_MIN}..{STICK_MAX})", rowno)
    else:  # motion
        if not (-32768 <= v <= 32767):
            raise PartError(f"列 {col} が i16 範囲外です: {v}", rowno)
    return v


def load_part(name: str, text: str) -> Part:
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = next(reader)
    except StopIteration:
        raise PartError("空のファイルです") from None
    header = [h.strip() for h in header]
    if header and header[-1] == "":  # 末尾カンマ由来の空列のみ許容
        header = header[:-1]
    seen: set[str] = set()
    for h in header:
        if h == "":
            raise PartError("空のヘッダ列があります", 1)
        if h not in BUTTONS and h not in _AXIS_COLS and h not in _MOTION_COLS \
                and h not in _META_COLS:
            raise PartError(f"未知のヘッダ列です: {h}", 1)
        if h in seen:
            raise PartError(f"ヘッダ列が重複しています: {h}", 1)
        seen.add(h)
    data_cols = tuple(h for h in header if h not in _META_COLS)
    if not data_cols:
        raise PartError("データ列(ボタン/軸/モーション)がありません", 1)

    rows: list[dict] = []
    csv_rowno = 0  # データ行の番号(1始まり)
    for lineno, raw in enumerate(reader, start=2):
        if not raw:
            # セルが1つも無い行(区切りの無い空の行)。「全て離す1フレーム」との
            # 混同を避けるためエラー(列数が揃った全空セル行は有効なフレーム)
            raise PartError("空行があります(削除してください)", lineno)
        cells = [c.strip() for c in raw]
        if len(cells) > len(header) and all(c == "" for c in cells[len(header):]):
            cells = cells[:len(header)]  # 末尾カンマ由来の余分な空セル
        if len(cells) != len(header):
            raise PartError(
                f"列数がヘッダと一致しません: {len(cells)} / {len(header)}", lineno)
        csv_rowno += 1
        rec = dict(zip(header, cells, strict=True))
        if "F" in rec and rec["F"].strip() != "":
            try:
                f_val = int(rec["F"])
            except ValueError:
                raise PartError(
                    f"F 列が整数ではありません: {rec['F']!r}", lineno) from None
            if f_val != csv_rowno:
                raise PartError(
                    f"F 列が連番ではありません: {f_val}(期待 {csv_rowno}。"
                    "F は CSV 行番号基準で rep とは無関係)", lineno)
        # 無効にされた行は、時間も消費せず丸ごと飛ばす(F の連番検査の後で判定
        # するので、行番号のつけ方は変わらない)
        if rec.get("off", "").strip() not in ("", "0"):
            continue
        rep = 1
        if "rep" in rec and rec["rep"].strip() != "":
            try:
                rep = int(rec["rep"])
            except ValueError:
                raise PartError(
                    f"rep 列が整数ではありません: {rec['rep']!r}", lineno) from None
            if rep < 1:
                raise PartError(f"rep は 1 以上です: {rep}", lineno)
        values = {c: _parse_cell(c, rec[c], lineno) for c in data_cols}
        for _ in range(rep):
            rows.append(values)
        if len(rows) > _MAX_ROWS:
            raise PartError(f"展開後の行数が上限({_MAX_ROWS})を超えました", lineno)

    if not rows:
        raise PartError("データ行がありません")
    return Part(name, data_cols, tuple(rows))
