"""名前の規則と、一覧の並び。

手順・フレーム表・条件・プリセットの名前はファイル名とフォルダー名そのものなので、
名前の規則はファイル名として安全かで決まる(file-format.md §2)。一覧の並びは
プロジェクト直下の order.json に持つ。保存の形式を読む側(procfmt.py・
conditions.py)も、プロジェクトをまとめる側(project.py)も、ここを使う。
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from .files import read_text

# 名前はそのままファイル名になる。フォルダーを跨げる文字を混ぜられると
# プロジェクトの外を読み書き・削除できてしまうので必ずここで拒否する
_NAME_NG = set('/\\:*?"<>|')
# Windows が装置名として予約している綴り。拡張子を付けても(NUL.txt)作れないので、
# 最初の「.」より前で比べる。通らない名前を保存できてしまうと、作った直後に
# 読み書きが失敗する
_NAME_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {
    f'{p}{i}' for p in ('COM', 'LPT') for i in (*'0123456789', '¹', '²', '³')}
# 名前の長さの上限(文字数)。根拠は validate_name の注釈にある
NAME_MAX = 120


def _refuse_lone_surrogate(n: str) -> None:
    """対になっていないサロゲート(絵文字の半分)を拒否する。

    JSON の "\\ud800" はそのまま Python の文字列になるが、UTF-8 にできない。名前に
    通すと、名前を含む応答(毎秒の状態)が UnicodeEncodeError で失敗し続ける。
    文にはその文字を入れない(入れると拒否の文も送れない)。
    """
    if any(0xD800 <= ord(c) <= 0xDFFF for c in n):
        raise ValueError("名前に使用できない文字が含まれています")


def validate_label(name: str) -> str:
    """ファイル名にならない名前(装置・本体・フォルダー)を確認する。

    長さの上限は手順の名前と合わせる。画面の同じ一覧に並ぶので、片方だけ
    いくらでも長く付けられると行が崩れる。使えない文字は見ない。ファイル名に
    ならないので、/ や : を入れても壊れるものがない。制御文字だけは拒否する。
    行が改行で分かれると、どこまでが 1 つの名前なのか読めなくなる。対になって
    いないサロゲートも、名前と同じく拒否する。
    """
    n = (name or "").strip()
    if not n:
        raise ValueError("名前が空です")
    if len(n) > NAME_MAX:
        raise ValueError(f"名前が長すぎます({NAME_MAX} 文字まで)")
    if any(ord(c) < 32 for c in n):
        raise ValueError("名前に改行やタブは使用できません")
    _refuse_lone_surrogate(n)
    return n


def validate_name(name: str) -> str:
    """手順・フレーム表・条件・プリセットの名前を確認し、そのまま返す。

    名前を書き換えて通すことはしない。書き換えると、渡された綴りと書き込む
    ファイルが一致せず、別の名前のつもりの物が元からある物を上書きする。
    """
    n = name or ""
    if not n.strip():
        raise ValueError("名前が空です")
    # 上限は「PC のファイル名(<名前>.json 等)として安全か」だけで決める。
    # 装置へ渡す手順は名前でなく内容ハッシュで識別する(P23)ので、名前の長さに
    # 装置側の制約は関わらない。
    # 120 文字あれば実用上は自由に名付けられ、Windows のファイル名の上限 255 にも
    # 収まる。パス全体の上限 260 に収まるかは、プロジェクトのフォルダーの場所
    # しだいである(file-format.md §2)
    if len(n) > NAME_MAX:
        raise ValueError(f"名前が長すぎます({NAME_MAX} 文字まで)")
    # 改行やタブは文字として見せられないので、使えない文字とは別の文で拒否する
    if any(ord(c) < 32 for c in n):
        raise ValueError("名前に改行やタブは使用できません")
    _refuse_lone_surrogate(n)
    # Windows はファイル名とフォルダー名の末尾の空白と「.」を除く。通すと、条件
    # 「C.」は条件「C」のフォルダーに書かれる。先頭の空白は除かれないが、見た目で
    # 区別できない名前が並ぶので、末尾と合わせて拒否する
    if n != n.strip():
        raise ValueError("名前の先頭と末尾に空白は使用できません")
    bad = sorted({c for c in n if c in _NAME_NG})
    if bad:
        raise ValueError(f"名前に「{' '.join(bad)}」は使用できません。"
                         '使用できない文字は / \\ : * ? " < > | です。')
    if ".." in n:
        raise ValueError("名前に「..」は使用できません")
    if n.startswith("."):
        raise ValueError("名前を「.」で始めることはできません")
    if n.endswith("."):
        raise ValueError("名前の末尾に「.」は使用できません")
    if n.split(".", 1)[0].rstrip(" ").upper() in _NAME_RESERVED:
        raise ValueError(f"「{n}」は Windows が予約している名前のため使用できません")
    return n


def name_key(name: str) -> str:
    """名前の同一性のキー(file-format.md §2)。NFC に正規化し、英字の大小を区別しない。

    プロジェクトの中で名前の重なりを見るときも、インポートで照合するときも、
    これで比べる。比べ方が 2 つあると、インポートでは同じとみなした名前を、
    プロジェクトの中では別の名前として作れてしまう。
    """
    return unicodedata.normalize("NFC", name).casefold()


def same_name(names, name: str) -> str | None:
    """names の中から、name と同じ名前(name_key が等しいもの)を探す。

    名前はファイル名そのもので、Windows のファイル名は大文字と小文字を
    区別しない(file-format.md §10-4)。綴りの完全一致で比べると、既にある物の
    ファイルへ黙って書き込み、中身を失う。NFC に正規化するのは、macOS が NFD の
    ファイル名を作るので、見た目が同じ名前が二重に入るのを防ぐためである。
    """
    key = name_key(name)
    return next((n for n in names if name_key(n) == key), None)


def refuse_taken(names, name: str, old: str | None = None,
                 path: Path | None = None) -> None:
    """name が既にある名前と重なるなら拒否する。綴りは既にある物のとおりに書く。

    old は名前の変更の元の名前。大文字と小文字だけを変える改名は、新しい名前の
    ファイルを書いてから古い方を消す作りでは、同じファイルを消してしまうので拒否する。

    path は name で書き込む先(作る、改名する、コピーする前に渡す)。名前の照合で
    重ならなくても、ファイルの場所では既にある物と同じ場所になることがある
    (Windows の短い名前。「ABCDEF~1.csv」は「abcdefghij.csv」を指す)。その
    まま書くと既にある物を置き換えるので、書き込み先があれば拒否する(インポートと
    同じ確認。unpack.apply)。
    """
    if old is not None and old != name and name_key(old) == name_key(name):
        raise ValueError("大文字と小文字だけを変える名前の変更はできません。"
                         "いったん別の名前に変更してから付け直してください。")
    hit = same_name((n for n in names if n != old), name)
    if hit is None:
        if path is not None and path.exists():
            raise ValueError(f"「{name}」は既にあります")
        return
    if hit == name:
        raise ValueError(f"「{name}」は既にあります")
    raise ValueError(f"「{hit}」は既にあります。名前の大文字と小文字は区別しません。")


# 一覧の並び順のファイル。手順・フレーム表・プリセット・条件の並びと、手順の
# フォルダーと非表示を、プロジェクト直下の 1 つのファイルに持つ
ORDER_NAME = "order.json"


def read_order(root: Path) -> dict:
    """並び順のファイルの中身。ないか読めなければ空。

    files.read_text で読む。並べ替えの保存が置き換えている瞬間に毎秒の状態が読むと、
    Windows は PermissionError で拒否する。待たずに空とすると、その 1 回の状態で
    フォルダーと非表示が消える。
    """
    try:
        d = json.loads(read_text(root / ORDER_NAME))
    except (OSError, ValueError, RecursionError):
        return {}
    return d if isinstance(d, dict) else {}


def _is_name(name: str) -> bool:
    """名前の規則(validate_name)に合うか。"""
    try:
        validate_name(name)
    except ValueError:
        return False
    return True


def _natural_key(name: str) -> tuple:
    """名前の中の数を数として比べる並びのキー(「表 2」が「表 10」より前)。

    名前を数の部分と数でない部分に分ける。re.split は数でない部分を偶数番目、
    数の部分を奇数番目に置くので、同じ番目どうしは同じ型で比べられる。数として
    等しい名前(「表02」と「表2」)は、名前そのものの順で決める。
    """
    parts = re.split(r"(\d+)", name)
    return tuple(int(p) if i % 2 else p for i, p in enumerate(parts)), name


def apply_order(root: Path, names: list[str], kind: str) -> list[str]:
    """フォルダーにあったファイルの名前を、一覧に出す名前とその並びにする。

    手順・フレーム表・プリセット・条件の一覧は、どれもこれで作る。種類に
    よって並べ替えられたりできなかったりする理由がない。並びは並び順の
    ファイルの kind の並びに従う。ファイルにない名前は名前順で末尾に付き、
    ファイルがなければすべてが名前順になる。名前順は、名前の中の数を数として
    比べる(_natural_key。エクスプローラーと同じく「表 2」が「表 10」より前)。
    ファイルにはあるが実在しない名前(名前の変更や削除で残った項目)は無視する。

    名前の規則に合わない名前は一覧に出さない。macOS が USB メモリや共有
    フォルダーへコピーするときに作る「._」で始まるファイルがこれにあたる。どの読み書きも
    名前を規則で確認してから開くので、一覧に出しても開けず、エクスポートも削除も
    できない。毎秒の状態がこの名前で読みに行くと、読み込みの拒否で状態ごと失敗する。

    並び順のファイルの名前は、名前の同一性のキー(name_key)で照らす。エクスプローラーで
    ファイル名の大文字と小文字だけを変えても、同じ物として元の場所に並ぶ。
    """
    names = [n for n in names if _is_name(n)]
    order = read_order(root).get(kind)
    if not isinstance(order, list):
        return sorted(names, key=_natural_key)
    by_key = {name_key(n): n for n in names}
    # 並び順のファイルは手で編集できるので、同じ名前が 2 回書かれうる。一覧は
    # 実体ごとに 1 行なので、先に出た方だけを残す
    listed = list(dict.fromkeys(by_key[name_key(n)] for n in order
                                if isinstance(n, str) and name_key(n) in by_key))
    return listed + sorted((n for n in names if n not in listed), key=_natural_key)
