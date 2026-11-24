"""手順の保存形式(procedures/*.json)の読み込み(file-format.md)。

扱うブロックは _BLOCK_KEYS のとおり 13 種である。press、hold、release、stick、
gyro、wait、label、loop、frametable、call、branch、join、script である。

読み込みが拒否するのは値の形の誤りだけである(file-format.md §8-1)。形は正しいが
意味が成り立たないもの、例えば「対象の回」の指すくり返しがその分岐を囲んでいない
ものは、読み込みを通し、変換のときに拒否か警告する(file-format.md §8-2・§8-3)。
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from .binfmt import BUTTONS, STICK_MAX, STICK_MIN
from .dsl import Compiled, compile_procs
from .files import read_text
from .frametable import FrametableError, frametable_failure, load_frametable
from .lapplan import LapsLeft
from .names import name_key, validate_name
from .procmodel import (
    INFINITE,
    WHOLE_LOOP,
    Arm,
    CompileError,
    Proc,
    StartPos,
    Stmt,
    What,
    When,
    call_at,
    iter_calls,
)
from .startpos import start_positions
from .words import as_json, sentences

# このツールが読み書きする手順の形式のバージョン(file-format.md §3)
FORMAT_VERSION = 1

# 長さを表す欄とくり返しの回数の上限(file-format.md §4-2)。60 fps で
# 24 時間にあたる値である。装置の欄はもっと
# 大きな値を表せるが、桁を打ち間違えたときに気づけるようにここで区切る
FRAMES_MAX = 5_184_000

# 手順の文書の入れ子(オブジェクトと配列)の深さの上限(file-format.md §8-1)。
# 読み込み、変換、インポートの参照の書き換え、保存は、どれも入れ子を再帰で
# たどる。上限がないと、手で書いた深い入れ子の手順 1 本で、処理が途中で
# 異常終了する(分岐を 128 段入れ子にした手順、つまり 512 段の入れ子で、
# インポートの書き換えのコピーが Python の再帰の上限に当たる)。256 段は、分岐だけ
# なら約 60 段、くり返しだけなら約 120 段の入れ子にあたる。くり返しは装置の
# カウンターの数で 32 段までなので(file-format.md §8-2)、実行できる手順は
# この上限のずっと手前に収まる
NEST_MAX = 256
NEST_TEXT = "入れ子が深すぎます。ブロックの入れ子を浅くしてください。"


def nest_too_deep(doc) -> bool:
    """文書の入れ子が NEST_MAX 段より深いか。

    深すぎる文書でも数えられるよう、再帰せずに数える。手順を開く処理は、
    どれもこれで拒否してから中をたどる。
    """
    stack = [(doc, 1)]
    while stack:
        value, depth = stack.pop()
        kids = (value.values() if isinstance(value, dict)
                else value if isinstance(value, list) else None)
        if kids is None:
            continue
        if depth > NEST_MAX:
            return True
        stack.extend((k, depth + 1) for k in kids if isinstance(k, (dict, list)))
    return False


# ブロックの種類ごとの、その種類だけが持つ欄(file-format.md §4-2)
_BLOCK_KEYS = {
    "press": {"buttons", "frames"},
    "hold": {"buttons"},
    "release": {"buttons"},
    "stick": {"side", "x", "y", "frames"},
    "gyro": {"gp", "gy", "gr", "frames", "sway", "sway_period",
             "sway_interval"},
    "wait": {"frames"},
    "label": {"name"},
    "loop": {"name", "count", "laps", "body"},
    "frametable": {"ref"},
    "call": {"ref"},
    "branch": {"name", "wait_input", "arms", "otherwise"},
    "join": {"name", "timeout_frames", "wait_input"},
    "script": {"cmd", "wait", "capture"},
}

# どのブロックも持てる欄(file-format.md §4-1)。note はメモで、変換には使わない
_COMMON_KEYS = {"type", "note", "disabled", "allow"}

# 識別子を持つブロックと、文言に使うその種類の呼び名と識別子の例(file-format.md §6-1)。
# 外から位置として指されうるブロックだけが識別子を持つ
_ID_BLOCKS = {
    "label": ("ラベル", "lb-1"),
    "loop": ("くり返し", "lp-1"),
    "branch": ("分岐", "br-1"),
    "join": ("合流点", "jn-1"),
    "script": ("スクリプト", "sc-1"),
    "call": ("別の手順", "cl-1"),
}

_ID_CHARS = re.compile(r"[A-Za-z0-9_-]+")

# allow に書ける値(file-format.md §4-1)。綴りを間違えた値を通すと、消したはずの警告が
# 出続けるので、一覧にない値は読み込みで拒否する
_ALLOW_VALUES = ("gyro-const", "branch-reset")

_WAIT_INPUT = ("release", "keep")

_WHEN_KINDS = ("first", "last", "nth", "every", "remaining")
_WHEN_N_KINDS = ("nth", "every", "remaining")

# 「選択条件」の種類と、その種類だけが持つ欄(file-format.md §5-2)
_WHAT_KEYS = {
    "manual": set(),
    "condition": {"ref"},
    "script": {"cmd", "capture"},
    "timeout": {"frames"},
}


class ProcedureError(Exception):
    """手順が読めない・変換できないときの失敗。

    文字列(コマンド行向け)は「ファイル 場所: 文」のままにし、画面が
    ブロックを見つけられるように、通し番号(line)とファイルの中のパス(path)も
    持つ。分からないときは、line は None、path は空である。
    """

    def __init__(self, file: str, where: str, msg: str, *,
                 line: int | None = None, path: str = "", block: str = ""):
        super().__init__(f"{file} {where}: {msg}" if where else f"{file}: {msg}")
        self.file = file
        self.where = where
        self.msg = msg
        self.line = line
        self.path = path
        # 失敗が呼び出し先の中にあるとき、開いている手順の中でそこへ入っていく
        # 「別の手順」のブロック(呼び出しの経路 via と識別子 block)。画面は line や path
        # ではなくこちらを指す(line と path は呼び出し先のファイルの中の話)
        self.via = ""
        self.block = block
        self.callee = ""


def _err(file: str, path: str, msg: str) -> ProcedureError:
    # path は「body[5]」か、識別子を付けた「body[5] (sc-xxxx)」である。
    # 画面が探すのはパスの部分だけ
    return ProcedureError(file, path, msg, path=path.split(" (")[0])


def _joined(values) -> str:
    return "、".join(str(v) for v in values)


def _values(values) -> str:
    """ファイルに書く値の並び。値は文字列なので、ファイルと同じく二重引用符で囲む。

    欄の名前(キー)は _joined で囲まずに並べる。
    """
    return _joined(f'"{v}"' for v in values)


# 欄の名前の、画面での呼び方。ない欄の文は、ファイルを書く人(欄の名前で探す)と
# 実行する人(画面の用語で読む)の両方が読む
_KEY_JA = {"name": "名前", "frames": "長さ", "buttons": "ボタン", "ref": "参照先",
           "cmd": "コマンド", "body": "中身", "arms": "選択肢", "side": "スティック",
           "x": "横の値", "y": "縦の値", "kind": "種類", "loop": "くり返し"}


def _need(node: dict, key: str, file: str, path: str):
    if key not in node:
        ja = _KEY_JA.get(key)
        raise _err(file, path, f"{ja}({key})がありません" if ja
                   else f"{key} がありません")
    return node[key]


def _parse_buttons(v, file: str, path: str) -> int:
    if isinstance(v, list) and not v:
        raise _err(file, path, "ボタンが選択されていません")
    if not isinstance(v, list):
        raise _err(file, path, "buttons はボタン名の配列である必要があります")
    mask = 0
    for name in v:
        # 先に文字列かどうかを見る。BUTTONS は辞書なので、配列や辞書が来ると
        # 「入っているか」を調べる時点で TypeError になり、文言で拒否できなくなる
        if not isinstance(name, str) or name not in BUTTONS:
            raise _err(file, path,
                       f"使用できないボタン名です: {as_json(name)}。"
                       f"使用できるのは {_values(BUTTONS)} です。")
        mask |= 1 << BUTTONS[name]
    return mask


def _parse_int(v, file: str, path: str, what: str, lo: int, hi: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise _err(file, path, f"{what} が整数ではありません: {as_json(v)}")
    if not (lo <= v <= hi):
        raise _err(file, path, f"{what} は {lo} から {hi} までの整数"
                   f"である必要があります: {v}")
    return v


def _parse_str(v, file: str, path: str, what: str) -> str:
    if not isinstance(v, str):
        raise _err(file, path, f"{what} は文字列である必要があります: {as_json(v)}")
    return v


def _parse_bool(v, file: str, path: str, what: str) -> bool:
    if not isinstance(v, bool):
        raise _err(file, path, f"{what} は true か false "
                   f"である必要があります: {as_json(v)}")
    return v


def _parse_choice(v, file: str, path: str, what: str, choices) -> str:
    """決まった値のどれかを読む。合わなければ受け付ける値を並べて知らせる。"""
    if v not in choices:
        shown = _values(choices)
        raise _err(file, path, f"{what} は {shown} のいずれか"
                   f"である必要があります: {as_json(v)}")
    return v


def _parse_allow(v, file: str, path: str) -> frozenset:
    if not isinstance(v, list):
        raise _err(file, path, "allow は文字列の配列である必要があります")
    for name in v:
        if name not in _ALLOW_VALUES:
            raise _err(file, path,
                       f"サポートされていない allow の値です: {as_json(name)}。"
                       f"使用できるのは {_values(_ALLOW_VALUES)} です。")
    return frozenset(v)


def _parse_ref(v, file: str, path: str, kind: str) -> str:
    """別の文書の名前を読む。

    名前の規則はファイル名のものと同じである(file-format.md §2)。

    変換とインポートがこの値をそのままファイルの場所として使うので、保存先の外を
    指す名前をここで拒否する。kind は選ぶものの名前(手順・フレーム表・条件)で、
    空のときの文に使う。
    """
    if not isinstance(v, str) or not v:
        raise _err(file, path, f"{kind}を選択してください")
    try:
        return validate_name(v)
    except ValueError as e:
        raise _err(file, path, str(e)) from None


def _parse_cmd(v, file: str, path: str) -> str:
    """スクリプトのコマンド(cmd)を確認する。

    空と、改行・NUL などの制御文字を拒否する(タブは許す)。制御文字のある
    コマンドは、シェルが別のコマンドとして読むか、起動の前に拒否される。どちらも
    書いた本人には見えない形で起きるので、読み込みの時点で拒否する。
    """
    cmd = _parse_str(v, file, path, "cmd")
    if not cmd.strip():
        raise _err(file, path, "コマンドが入力されていません")
    if any((ord(c) < 32 and c != "\t") or ord(c) == 127 for c in cmd):
        raise _err(file, path, "コマンドに改行や制御文字は使用できません")
    return cmd


def _parse_capture(v, file: str, path: str) -> tuple:
    """スクリプトに渡す映像ソースの名前の並びを読む(file-format.md §4-2・§5-2)。

    映像ソースの実体は PC ごとに違うので、手順には名前だけを書く。名前に
    あたる映像ソースがこの PC にあるかどうかは、開始の前のチェックで確認する。
    """
    if not isinstance(v, list):
        raise _err(file, path, "capture は映像ソースの名前の配列である必要があります")
    names = []
    for i, name in enumerate(v):
        if not isinstance(name, str) or not name:
            raise _err(file, f"{path}.capture[{i}]",
                       "映像ソースの名前は空でない文字列"
                       f"である必要があります: {as_json(name)}")
        names.append(name)
    return tuple(names)


def _parse_count(v, file: str, path: str) -> int | str:
    """くり返しの回数を読む(file-format.md §4-3)。無限のときは "infinite" を返す。"""
    if v == INFINITE:
        return v
    if isinstance(v, bool) or not isinstance(v, int):
        raise _err(file, path, 'count は 1 以上の整数か "infinite" '
                   f'である必要があります: {as_json(v)}')
    if v < 1:
        raise _err(file, path,
                   'count は 1 以上である必要があります。'
                   '無限にするときは "infinite" を指定してください。')
    if v > FRAMES_MAX:
        raise _err(file, path, f"count は 1 から {FRAMES_MAX} までの整数"
                   f"である必要があります: {v}")
    return v


def _is_disabled(node) -> bool:
    """無効にしたブロックか(file-format.md §4-4)。

    無効にしたブロックは「その時間だけ何もしない」ではなく、丸ごと存在しない
    ものとして扱う。試しに一部だけ外して動かす、という使い方のためである。
    くり返しや分岐を無効にすると、その中身ごとなくなる。
    """
    return isinstance(node, dict) and node.get("disabled") is True


class _File:
    """1 つの手順のファイルを読む間だけ使う入れもの。

    識別子の一覧は、ブロックを Stmt に変換する前に、ファイル全体を一度走査して作る。
    「対象の回」がくり返し以外の識別子を指していないか(file-format.md §8-1)を、
    そのブロックより後ろに書いてあるくり返しも含めて調べるためである。この走査は
    無効にしたブロックも数える。手で書いたファイルで片方を無効にしておき、
    編集画面で有効に戻した瞬間にファイルが読めなくなる、ということを
    避けるためである(file-format.md §4-4)。
    """

    def __init__(self, name: str):
        self.name = name
        self.path = f"procedures/{name}.json"
        self.ids: dict[str, str] = {}   # 識別子とそれを持つブロックの種類
        self.laps = 0                   # laps が真のくり返しの数
        self.loops: list[str] = []      # いま囲んでいるくり返しの識別子
        # 取り除くくり返し(無効にしたものと、無効にしたブロックの中にあるもの)の
        # 識別子と名前。「対象の回」がそれを指すとき、変換が「見つかりません」では
        # なく「無効になっています」と書くのに使う(dsl._check_when)
        self.off_loops: dict[str, str] = {}


def _scan_body(nodes, f: _File, path: str, off: bool = False) -> None:
    """ブロックの配列を走査する。off は、無効にしたブロックの中かである。"""
    if not isinstance(nodes, list):
        return   # 形の誤りは、Stmt に変換するときの走査が場所つきで報告する
    for i, node in enumerate(nodes):
        _scan_node(node, f, f"{path}[{i}]", off)


def _scan_node(node, f: _File, path: str, off: bool = False) -> None:
    if not isinstance(node, dict):
        return
    off = off or _is_disabled(node)
    t = node.get("type")
    ident = node.get("id")
    if isinstance(ident, str) and ident:
        if ident in f.ids:
            raise _err(f.path, path, f"識別子 {ident} が 2 か所にあります")
        f.ids[ident] = t if isinstance(t, str) else ""
    if t == "loop":
        if node.get("laps") is True:
            f.laps += 1
            if f.laps > 1:
                raise _err(f.path, path,
                           "周回数を指定できるくり返しは手順の中で 1 つだけです")
        if off and isinstance(ident, str) and ident:
            # 無効にしたブロックの形はチェックしないので、名前が文字列でなければ
            # 名前のないくり返しとして扱う
            name = node.get("name", "")
            f.off_loops[ident] = name if isinstance(name, str) else ""
        _scan_body(node.get("body"), f, f"{path}.body", off)
    elif t == "branch":
        arms = node.get("arms")
        if isinstance(arms, list):
            for j, arm in enumerate(arms):
                if isinstance(arm, dict):
                    _scan_body(arm.get("body"), f, f"{path}.arms[{j}].body", off)
        _scan_body(node.get("otherwise"), f, f"{path}.otherwise", off)


def _parse_id(node: dict, t: str, f: _File, path: str) -> str:
    """識別子を読む(file-format.md §6-1)。識別子を持たない種類では空文字列を返す。

    識別子を持たない種類に id が書いてあれば、使用できない項目として拒否される。
    """
    if t not in _ID_BLOCKS:
        return ""
    label, example = _ID_BLOCKS[t]
    ident = node.get("id")
    if not isinstance(ident, str) or not ident:
        raise _err(f.path, path,
                   f"id がありません。{label}には id が必要です"
                   f'(例: "id": "{example}")。')
    if ident.startswith("@"):
        raise _err(f.path, path, "@ で始まる識別子は使用できません")
    if _ID_CHARS.fullmatch(ident) is None:
        raise _err(f.path, path, "識別子に使用できるのは英数字と _ と - です")
    if len(ident) > 32:
        raise _err(f.path, path, "識別子は 1 から 32 文字である必要があります")
    return ident


def _parse_when(v, f: _File, arm_path: str) -> When:
    """選択肢の「対象の回」を読む(file-format.md §5-1)。"""
    path = f"{arm_path}.when"
    if not isinstance(v, dict):
        raise _err(f.path, path, "when は {loop, kind, n} のオブジェクト"
                   "である必要があります")
    extra = set(v.keys()) - {"loop", "kind", "n"}
    if extra:
        raise _err(f.path, path,
                   f"使用できない項目です: {_joined(sorted(extra))}。"
                   "使用できるのは loop、kind、n です。")
    loop = _need(v, "loop", f.path, path)
    if not isinstance(loop, str) or not loop:
        raise _err(f.path, path, "対象の回のくり返し(loop)がありません")
    pointed = f.ids.get(loop)
    if loop != WHOLE_LOOP and pointed and pointed != "loop":
        # 識別子を持てない種類を手で書いたときは、その種類の呼び名がない。
        # 英字の type をそのまま文に混ぜず、くり返しではないとだけ伝える
        known = _ID_BLOCKS.get(pointed)
        what = f"{known[0]}です" if known else "くり返しではありません"
        raise _err(f.path, path,
                   f"「対象の回」に指定できるのはくり返しだけです: {loop} は{what}")
    kind = _need(v, "kind", f.path, path)
    if kind not in _WHEN_KINDS:
        raise _err(f.path, path,
                   f"サポートされていない kind の値です: {as_json(kind)}。"
                   f"使用できるのは {_values(_WHEN_KINDS)} です。")
    n = 0
    if kind in _WHEN_N_KINDS:
        if "n" not in v:
            raise _err(f.path, path, f"kind が {as_json(kind)}"
                       " のときは、1 以上の整数の n が必要です")
        n = v["n"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise _err(f.path, path, "n は 1 以上の整数"
                       f"である必要があります: {as_json(n)}")
    elif "n" in v:
        raise _err(f.path, path, f"kind が {as_json(kind)} のときは n を使用できません")
    return When(loop, kind, n)


def _parse_what(v, f: _File, arm_path: str) -> What:
    """選択肢の「選択条件」を読む(file-format.md §5-2)。

    「選択条件」は 1 つの欄なので、2 つ書くことがそもそもできない。待たない選択肢は
    what を書かないことで表すので、null は拒否する。
    """
    path = f"{arm_path}.what"
    if v is None:
        raise _err(f.path, path,
                   "what には kind が必要です。"
                   "待たない選択肢では what を省略します。")
    if not isinstance(v, dict):
        raise _err(f.path, path, "what は {kind, ...} のオブジェクト"
                   "である必要があります")
    kind = _need(v, "kind", f.path, path)
    # 文字列かどうかを先に見る理由は _parse_buttons と同じである
    if not isinstance(kind, str) or kind not in _WHAT_KEYS:
        raise _err(f.path, path,
                   f"サポートされていない kind の値です: {as_json(kind)}。"
                   f"使用できるのは {_values(_WHAT_KEYS)} です。")
    known = _WHAT_KEYS[kind] | {"kind"}
    extra = set(v.keys()) - known
    if extra:
        raise _err(f.path, path,
                   f"使用できない項目です: {_joined(sorted(extra))}。"
                   f"kind が {as_json(kind)} のときに使用できるのは "
                   f"{_joined(sorted(known))} です。")
    if kind == "condition":
        return What("condition",
                    ref=_parse_ref(_need(v, "ref", f.path, path), f.path, path,
                                   "条件"))
    if kind == "script":
        cmd = _parse_cmd(_need(v, "cmd", f.path, path), f.path, path)
        return What("script", cmd=cmd,
                    capture=_parse_capture(v.get("capture", []), f.path, path))
    if kind == "timeout":
        frames = _need(v, "frames", f.path, path)
        if isinstance(frames, bool) or not isinstance(frames, int):
            raise _err(f.path, path, f"frames が整数ではありません: {as_json(frames)}")
        if frames < 1:
            raise _err(f.path, path, "時間切れは 1 フレーム以上である必要があります")
        if frames > FRAMES_MAX:
            raise _err(f.path, path,
                       f"frames は 1 から {FRAMES_MAX} までの整数"
                       f"である必要があります: {frames}")
        return What("timeout", frames=frames)
    return What("manual")


def _parse_arm(arm, f: _File, path: str, project: _Project) -> Arm:
    """分岐の選択肢を読む(file-format.md §5)。"""
    if not isinstance(arm, dict):
        raise _err(f.path, path,
                   "選択肢は {name, when, what, body} のオブジェクトです")
    extra = set(arm.keys()) - {"name", "when", "what", "body"}
    if extra:
        raise _err(f.path, path,
                   f"使用できない項目です: {_joined(sorted(extra))}。"
                   "使用できるのは name、when、what、body です。")
    name = _parse_str(arm.get("name", ""), f.path, path, "name")
    when = _parse_when(arm["when"], f, path) if "when" in arm else None
    what = _parse_what(arm["what"], f, path) if "what" in arm else None
    body = arm.get("body", [])
    if not isinstance(body, list):
        raise _err(f.path, path, "body はブロックの配列である必要があります")
    return Arm(name, when, what,
               _parse_body(body, f, f"{path}.body", project))


def _node_to_stmt(node, f: _File, path: str, project: _Project) -> Stmt:
    """ブロック 1 つを Stmt に変換する。

    path はファイルの中のパスで、body[12].arms[1].body[0] のように書く。
    手で開いたときにそのまま探せる形にする(file-format.md §8-1)。
    """
    if not isinstance(node, dict) or "type" not in node:
        raise _err(f.path, path, 'ブロックは {"type": ...} のオブジェクト'
                   'である必要があります')
    t = node["type"]
    # 文字列かどうかを先に見る理由は _parse_buttons と同じである
    if not isinstance(t, str) or t not in _BLOCK_KEYS:
        raise _err(f.path, path,
                   f"サポートされていないブロックの種類です: {as_json(t)}。"
                   f"使用できるのは {_values(_BLOCK_KEYS)} です。")
    ident = _parse_id(node, t, f, path)
    where = f"{path} ({ident})" if ident else path
    known = _BLOCK_KEYS[t] | _COMMON_KEYS
    if t in _ID_BLOCKS:
        known = known | {"id"}
    extra = set(node.keys()) - known
    if extra:
        raise _err(f.path, where,
                   f"使用できない項目です: {_joined(sorted(extra))}。"
                   f"使用できるのは {_joined(sorted(known))} です。")
    if "note" in node:
        _parse_str(node["note"], f.path, where, "note")
    if "disabled" in node:
        _parse_bool(node["disabled"], f.path, where, "disabled")
    allow = _parse_allow(node.get("allow", []), f.path, where)
    line = project.next_ordinal(f.name)

    def make(kind: str, args: tuple, body: list | None = None) -> Stmt:
        return Stmt(kind, line, args, body=body, allow=allow, proc=f.name,
                    id=ident)

    if t == "press":
        mask = _parse_buttons(_need(node, "buttons", f.path, where), f.path, where)
        n = _parse_int(_need(node, "frames", f.path, where), f.path, where,
                       "frames", 1, FRAMES_MAX)
        return make("press", (mask, n))
    if t in ("hold", "release"):
        mask = _parse_buttons(_need(node, "buttons", f.path, where), f.path, where)
        return make(t, (mask,))
    if t == "wait":
        n = _parse_int(_need(node, "frames", f.path, where), f.path, where,
                       "frames", 1, FRAMES_MAX)
        return make("wait", (n,))
    if t == "stick":
        side = _parse_choice(_need(node, "side", f.path, where), f.path, where,
                             "side", ("L", "R"))
        x = _parse_int(_need(node, "x", f.path, where), f.path, where, "x",
                       STICK_MIN, STICK_MAX)
        y = _parse_int(_need(node, "y", f.path, where), f.path, where, "y",
                       STICK_MIN, STICK_MAX)
        # frames が 1 以上なら、その長さだけ倒して自動で中央へ戻す。0 または
        # 省略なら、時間を消費せず、次に変えるまで倒したままになる
        frames = _parse_int(node.get("frames", 0), f.path, where, "frames",
                            0, FRAMES_MAX)
        return make("stick", (side, x, y, frames))
    if t == "gyro":
        # 軸の対応は GP がひねり、GY が縦、GR が横
        # である。ワイヤの並びで決まっていて入れ替えられない(glossary.md §2)
        g = [_parse_int(node.get(k, 0), f.path, where, k, -32768, 32767)
             for k in ("gp", "gy", "gr")]
        frames = _parse_int(node.get("frames", 0), f.path, where, "frames",
                            0, FRAMES_MAX)
        # sway が 1 以上なら、ゆらぎを付ける。Switch 側のゼロ点自動較正を
        # 避けるための書き方で、素の値を sway_interval フレーム続けるたびに、
        # +sway と -sway を sway_period フレームずつ対で入れる。
        # sway_interval が 0 なら常に交互になる。どの形でも合計の回転量は
        # 値とフレーム数の積に厳密に一致する
        sway = _parse_int(node.get("sway", 0), f.path, where, "sway", 0, 32767)
        period = _parse_int(node.get("sway_period", 2), f.path, where,
                            "sway_period", 1, 600)
        interval = _parse_int(node.get("sway_interval", 60), f.path, where,
                              "sway_interval", 0, 100_000)
        return make("gyro", (*g, frames, sway, period, interval))
    if t == "label":
        # 名前が空のラベルは読み込みを通す。変換のときに警告する(file-format.md §8-3)
        name = _parse_str(_need(node, "name", f.path, where), f.path, where,
                          "name")
        return make("label", (name,))
    if t == "loop":
        name = _parse_str(node.get("name", ""), f.path, where, "name")
        count = _parse_count(node.get("count", 2), f.path, where)
        laps = _parse_bool(node.get("laps", False), f.path, where, "laps")
        body = _need(node, "body", f.path, where)
        if not isinstance(body, list):
            raise _err(f.path, where, "body はブロックの配列である必要があります")
        f.loops.append(ident)
        try:
            stmts = _parse_body(body, f, f"{path}.body", project)
        finally:
            f.loops.pop()
        return make("loop", (count, laps, name), body=stmts)
    if t == "frametable":
        ref = _parse_ref(_need(node, "ref", f.path, where), f.path, where,
                         "フレーム表")
        frametable = project.load_frametable(ref, f.path, where)
        return make("frametable", (ref, frametable.columns, frametable.rows))
    if t == "call":
        ref = _parse_ref(_need(node, "ref", f.path, where), f.path, where, "手順")
        # 呼び出し先を読み込む。なくても読み込みは通し、変換の「別の手順が
        # 見つかりません」がブロックの番号つきで報告する
        # (file-format.md §6-3・§8-2)。循環も変換のときに拒否する
        try:
            project.load_procedure(ref, missing_ok=True)
        except ProcedureError as e:
            # 呼び出し先(やその先)が読めない。上へ戻るたびに書き直すので、
            # 最後に残るのは一番上の手順の中の呼び出しのブロックである
            e.via = path
            e.block = node.get("id") or ""
            if not e.callee:
                e.callee = e.file.removeprefix("procedures/").removesuffix(
                    ".json")
            raise
        return make("call", (ref,))
    if t == "branch":
        name = _parse_str(node.get("name", ""), f.path, where, "name")
        # 待たない分岐でも wait_input の欄は消さない。選択肢を追加すればまた
        # 使われるからである(file-format.md §5-2)
        wait_input = _parse_choice(node.get("wait_input", "release"), f.path,
                                   where, "wait_input", _WAIT_INPUT)
        arms = _need(node, "arms", f.path, where)
        if not isinstance(arms, list):
            raise _err(f.path, where, "arms は選択肢の配列である必要があります")
        parsed = [_parse_arm(a, f, f"{path}.arms[{i}]", project)
                  for i, a in enumerate(arms)]
        otherwise = node.get("otherwise", [])
        if not isinstance(otherwise, list):
            raise _err(f.path, where, "otherwise はブロックの配列である必要があります")
        # 囲むくり返しの識別子を覚えておく。「対象の回」で指定できるのは、同じファイルの
        # 中にあってこの分岐を囲むくり返しだけだからである(file-format.md §5-1)。
        # 「対象の回」が無効にしたくり返しを指すなら、その名前も覚えておく。変換の
        # 時点では取り除かれていて、名前が見つからない
        off = {a.when.loop: f.off_loops[a.when.loop] for a in parsed
               if a.when is not None and a.when.loop in f.off_loops}
        return make("branch",
                    (name, wait_input, parsed,
                     _parse_body(otherwise, f, f"{path}.otherwise", project),
                     tuple(f.loops), off))
    if t == "join":
        # 名前が空の合流点は読み込みを通す。変換のときに警告する(file-format.md §8-3)
        name = _parse_str(_need(node, "name", f.path, where), f.path, where,
                          "name")
        timeout = None
        if "timeout_frames" in node:
            timeout = _parse_int(node["timeout_frames"], f.path, where,
                                 "timeout_frames", 1, FRAMES_MAX)
        wait_input = _parse_choice(node.get("wait_input", "release"), f.path,
                                   where, "wait_input", _WAIT_INPUT)
        return make("join", (name, timeout, wait_input))
    if t == "script":
        cmd = _parse_cmd(_need(node, "cmd", f.path, where), f.path, where)
        wait = _parse_bool(node.get("wait", True), f.path, where, "wait")
        capture = _parse_capture(node.get("capture", []), f.path, where)
        return make("script", (cmd, wait, capture))
    raise AssertionError(t)


def _parse_body(nodes, f: _File, path: str, project: _Project) -> list[Stmt]:
    """ブロックの配列を Stmt の並びにする。無効にしたブロックは取り除く。

    取り除いたブロックも、人に見せる番号(_Project.shown)には数える。
    """
    out = []
    for i, node in enumerate(nodes):
        if _is_disabled(node):
            project.skip_shown(node)
            continue
        out.append(_node_to_stmt(node, f, f"{path}[{i}]", project))
    return out


def parse_procedure_body(body: list, name: str, project: _Project) -> list[Stmt]:
    """1 つの手順のブロックの配列を Stmt の並びにする。

    識別子を集める走査と、Stmt に変換する走査の 2 回を続けて行う。ファイルからでは
    なく引数からブロックを受け取る呼び出し口(テストのツール)もここを通る。
    """
    f = _File(name)
    _scan_body(body, f, "body")
    return _parse_body(body, f, "body", project)


def is_int(v) -> bool:
    """保存形式の欄の値が整数か。JSON の true は Python では 1 と等しいので、真偽値は
    除く。保存形式を読む処理は、どれもこれで判定する。
    """
    return isinstance(v, int) and not isinstance(v, bool)


def format_problem(fmt) -> str | None:
    """手順の文書の format の値を拒否する文を返す。このツールが読み書きする
    バージョンなら None を返す。

    バージョンは整数で書く(file-format.md §3)。真偽値の true と小数の 1.0 は、Python
    では 1 と等しいが、整数の 1 ではないので拒否する。読み込み、保存、
    参照をたどる処理は、どれもこれで判定する。処理ごとに判定が違うと、
    同じ文書が、ある所では読める手順に、別の所では読めない手順になるからである。
    """
    # 読めるバージョンより新しい手順は、padcue を更新すれば読める。ほかの人と
    # やり取りするときにツールのバージョンが揃っていないことは避けられないので、
    # 次にできることを示す
    if is_int(fmt) and fmt > FORMAT_VERSION:
        return ("この手順は新しいバージョンの padcue で作成されています。"
                "padcue を更新してください。")
    if not is_int(fmt) or fmt != FORMAT_VERSION:
        return f"format は {FORMAT_VERSION} である必要があります: {as_json(fmt)}"
    return None


def _read_header(doc, f: _File) -> tuple[str, list]:
    """手順の文書の頭を読む(file-format.md §3)。

    前提条件(precondition)とブロックの配列を返す。
    """
    if not isinstance(doc, dict):
        raise _err(f.path, "", "トップレベルはオブジェクトである必要があります")
    if doc.get("kind") != "procedure":
        raise _err(f.path, "kind", "手順ファイルを指定してください")
    problem = format_problem(doc.get("format"))
    if problem:
        raise _err(f.path, "format", problem)
    extra = set(doc.keys()) - {"kind", "format", "precondition", "body"}
    if extra:
        raise _err(f.path, "",
                   f"使用できない項目です: {_joined(sorted(extra))}。"
                   "使用できるのは kind、format、precondition、body です。")
    precondition = doc.get("precondition", "")
    if not isinstance(precondition, str):
        raise _err(f.path, "precondition",
                   "precondition は文字列である必要があります: "
                   f"{as_json(precondition)}")
    body = doc.get("body")
    if not isinstance(body, list):
        raise _err(f.path, "body", "body はブロックの配列である必要があります")
    return precondition, body


class _Project:
    def __init__(self, root: Path, docs: dict | None = None):
        self.root = root
        self.procs: dict[str, Proc] = {}
        # ブロックの通し番号(Stmt.line)。無効にしたブロックを数えない。編集画面は
        # この番号でブロックを探す
        self._ordinal = 0
        # 人に見せる番号。無効にしたブロックも数える(編集画面の列に見えている順。
        # file-format.md §8)。(手順の名前, 通し番号) -> 見えている順の番号
        self._shown = 0
        self.shown: dict[tuple[str, int], int] = {}
        # 名前の同一性のキー -> 文書。ここにある手順はファイルではなくこれを読む
        # (編集画面が保存する前の中身を確認するため)。ファイルは大文字と小文字を
        # 区別せずに開けるので、綴りの違う呼び出しも同じ文書を使う(file-format.md §2)
        self._docs = {name_key(k): v for k, v in (docs or {}).items()}

    def next_ordinal(self, name: str = "") -> int:
        """次のブロックの通し番号。name(手順の名前)があれば見えている順の番号も覚えておく。"""
        self._ordinal += 1
        self._shown += 1
        if name:
            self.shown[(name, self._ordinal)] = self._shown
        return self._ordinal

    def skip_shown(self, node) -> None:
        """無効にしたブロックを、中のブロックごと見えている順の番号に数える。"""
        self._shown += _count_blocks(node)

    def load_frametable(self, ref: str, file: str, path: str):
        p = self.root / "frametables" / f"{ref}.csv"
        if not p.is_file():
            raise _err(file, path,
                       f"フレーム表「{ref}」が見つかりません。"
                       "別のフレーム表を選択してください。")
        # 読めないのはフレーム表の側なので、フレーム表の名前で示す(そのまま通すと、
        # 文字コードの誤りが手順のファイルの誤りとして出る)
        try:
            return load_frametable(ref, p.read_text(encoding="utf-8"))
        except (FrametableError, OSError, UnicodeDecodeError) as e:
            raise _err(f"frametables/{ref}.csv", "",
                       frametable_failure(ref, e)) from None

    def load_procedure(self, name: str, *, missing_ok: bool = False) -> None:
        """手順を 1 つ読み込んで procs に登録する。

        missing_ok が真なら、ファイルがないときは登録せずに戻る。呼び出しの
        参照先がないことは読み込みでは拒否せず、変換のときに拒否するからである
        (file-format.md §6-3・§8-2)。登録しなければ compile_procs の検証が
        「別の手順が見つかりません」としてブロックの番号つきで報告する。
        """
        if name in self.procs:
            return
        f = _File(name)
        p = self.root / "procedures" / f"{name}.json"
        if name_key(name) in self._docs:
            doc = self._docs[name_key(name)]
        elif not p.is_file():
            if missing_ok:
                return
            raise ProcedureError(f.path, "", "ファイルが見つかりません")
        else:
            try:
                doc = json.loads(read_text(p))
            except json.JSONDecodeError as e:
                raise ProcedureError(f.path, "", sentences(
                    "ファイルが壊れています",
                    f"{e.lineno} 行目 {e.colno} 文字目から読み込めません")) from None
            except UnicodeDecodeError:
                raise ProcedureError(f.path, "",
                                     "文字コードが UTF-8 ではありません") from None
            except RecursionError:
                # NEST_MAX よりはるかに深いと、JSON の読み込みそのものが再帰の
                # 上限に当たる
                raise ProcedureError(f.path, "", NEST_TEXT) from None
        if nest_too_deep(doc):
            raise ProcedureError(f.path, "", NEST_TEXT)
        precondition, body = _read_header(doc, f)
        _scan_body(body, f, "body")

        # ブロックの番号はファイルごとに 1 から振り直す(手順そのものは 0 で、
        # 最初のブロックが 1)。call でこのファイルを読み込んでいる最中の
        # 呼び出し元にも採番の途中があるので、呼び出し元の分は退避しておき、
        # 戻ったら続きを振れるようにする
        saved = self._ordinal, self._shown
        self._ordinal = self._shown = 0
        proc = Proc(name, 0, precondition)
        # 本体を読む前に登録する。読んでいる手順を呼び出しがたどり直しても、上の
        # 登録の確認で戻るので、循環はここで終わり、compile_procs の検証が
        # ブロックの番号つきで報告する
        self.procs[name] = proc
        proc.body.extend(_parse_body(body, f, "body", self))
        self._ordinal, self._shown = saved


def _call_toward(procs: dict, top: str, target: str) -> str:
    """top の中で、target へ(直接か、さらに先の呼び出しで)入っていく
    「別の手順」のブロックの識別子。見つからなければ空。"""
    seen: set[str] = set()

    def reaches(name: str) -> bool:
        if name == target:
            return True
        if name in seen or name not in procs:
            return False
        seen.add(name)
        return any(reaches(st.args[0]) for st in iter_calls(procs[name].body))

    for st in iter_calls(procs[top].body if top in procs else []):
        if reaches(st.args[0]):
            return st.id
    return ""


def compile_procedure(project_dir: str | Path, name: str,
                      laps: int | str | None = 1,
                      start: StartPos | None = None, lanes: int = 1,
                      docs: dict | None = None, finals: bool | LapsLeft = True,
                      drawn_laps: int | str | None = None,
                      drawn_lap: int = 1) -> Compiled:
    """プロジェクトフォルダーの procedures/<name>.json を変換する。

    laps は実行のときに決まる周回数で、1 以上の整数か "infinite" である。
    装置は周回を数えないので、周回数は手順の中のくり返しの回数として渡す。
    None は周回数を決めない変換である(dsl.compile_procs)。
    start は実行を始める位置である。装置は開始位置も受け取らないので、途中の
    位置から始めるときは、変換器がその位置から続く手順を合成する。
    lanes は一緒に開始するグループのレーンの数で、合流点が選択待ちになるかを
    決める。finals と drawn_laps と drawn_lap は dsl.compile_procs と同じ意味である。
    docs は名前から文書への対応で、ここにある手順はファイルではなくこれを
    読む(編集画面が保存する前の中身を確認するため)。
    """
    project = _Project(Path(project_dir), docs)
    project.load_procedure(name)
    try:
        c = compile_procs(project.procs, name, laps, start, lanes, finals,
                          drawn_laps, drawn_lap)
    except CompileError as e:
        # エラーの起きたブロックが属する手順(呼び出し先のこともある)のファイル名で
        # 報告する。ブロックの番号はそのファイルの中の、列に見えている順の番号で
        # ある(無効にしたブロックも数える。編集画面の「N 番目」と同じ)
        err_proc = e.proc or name
        # 番号 0 は手順そのもの(ブロックを指さない失敗)である
        where = (f"{project.shown.get((err_proc, e.line), e.line)} 番目のブロック"
                 if e.line else "手順全体")
        if err_proc != name:
            # 呼び出し先の中の失敗。番号は呼び出し先のファイルの中の話なので、
            # 開いている手順の中ではそこへ入っていく呼び出しのブロックを指す
            err = ProcedureError(f"procedures/{err_proc}.json", where,
                                 e.msg, line=None,
                                 block=_call_toward(project.procs, name, err_proc))
            err.callee = err_proc
            raise err from None
        raise ProcedureError(f"procedures/{err_proc}.json", where, e.msg,
                             line=e.line or None) from None
    c.warnings = [replace(w, shown=_shown_number(project, name, w.line, w.path))
                  for w in c.warnings]
    return c


def _shown_number(project: _Project, top: str, line: int, path: tuple) -> int:
    """警告の場所を、一番上の手順の列に見えている順の番号にする。0 は手順そのもので
    ある(file-format.md §8)。

    呼び出し先の中の警告(経路のあるもの)は、利用者が開いた手順の画面では指せない
    ので、そこへ入っていく呼び出しのブロック(経路の先頭)の番号にする。編集画面が
    「N 番目(別の手順)」と指すのと同じブロックである。たどれなければ 0 を返す。
    """
    if path:
        st = call_at(project.procs, top, path[:1])
        line = st.line if st is not None else 0
    return project.shown.get((top, line), line) if line else 0


def _count_blocks(node) -> int:
    """ブロックと、その中のブロックの数(編集画面が列に並べる数)。

    無効にしたブロックの中身は形を確認していないので、型の違う欄は中身がない
    ものとして数える。
    """
    if not isinstance(node, dict):
        return 0
    bodies = []
    if node.get("type") == "loop":
        bodies = [node.get("body")]
    elif node.get("type") == "branch":
        arms = node.get("arms")
        if isinstance(arms, list):
            bodies = [a.get("body") for a in arms if isinstance(a, dict)]
        bodies.append(node.get("otherwise"))
    return 1 + sum(_count_blocks(n) for b in bodies if isinstance(b, list) for n in b)


def start_points(project_dir: str | Path, name: str,
                 docs: dict | None = None) -> list[dict]:
    """手順の開始位置の一覧を、変換せずに読む(形は Compiled.resume_points と同じ)。

    プリセットの開始位置を手順と照合するのに使う。無効にしたブロックは読み込みで
    除かれるので、無効にしたラベルや呼び出しの先にある位置は一覧に出ない。docs は
    compile_procedure と同じで、保存する前の中身を確認するのに使う。読めない手順は
    ProcedureError である。
    """
    project = _Project(Path(project_dir), docs)
    project.load_procedure(name)
    return start_positions(project.procs, name)
