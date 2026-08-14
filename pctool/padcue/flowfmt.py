"""フロー構造化データ(procedures/*.flow.json)の読み込みとコンパイル(flow-format.md)。

段階1の対応範囲: label / stick / press / hold / release / wait / loop / part / call。
counter_branch / wait_branch は段階2・3で追加済み。
"""
from __future__ import annotations

import json
from pathlib import Path

from .binfmt import BUTTONS, STICK_MAX, STICK_MIN
from .dsl import Compiled, CompileError, Proc, Stmt, compile_procs
from .matrix import PartError, load_part

FLOW_SCHEMA = 1

_NODE_KEYS = {
    "label": {"text"},
    "stick": {"side", "x", "y", "frames"},
    "gyro": {"gp", "gy", "gr", "frames", "sway", "sway_period",
             "sway_interval"},
    "press": {"buttons", "frames"},
    "hold": {"buttons"},
    "release": {"buttons"},
    "wait": {"frames"},
    "loop": {"count", "body"},
    "part": {"ref"},
    "call": {"ref"},
    "counter_branch": {"arms"},
    "wait_branch": {"arms", "timeout_frames", "on_timeout"},
}


class FlowError(Exception):
    def __init__(self, file: str, where: str, msg: str):
        super().__init__(f"{file} {where}: {msg}")
        self.file = file
        self.msg = msg


def _err(file: str, path: str, msg: str) -> FlowError:
    return FlowError(file, path, msg)


def _parse_buttons(v, file, path) -> int:
    if not isinstance(v, list) or not v:
        raise _err(file, path, "buttons はボタン名の配列です")
    mask = 0
    for name in v:
        if name not in BUTTONS:
            raise _err(file, path, f"未知のボタン名: {name}")
        mask |= 1 << BUTTONS[name]
    return mask


def _parse_int(v, file, path, what, lo, hi) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise _err(file, path, f"{what} が整数ではありません: {v!r}")
    if not (lo <= v <= hi):
        raise _err(file, path, f"{what} が範囲外です: {v}(許容 {lo}..{hi})")
    return v


def _is_off(node) -> bool:
    """一時的に無効にされたブロックか。

    無効なブロックは「その時間だけ何もしない」ではなく**丸ごと存在しない**
    ものとして扱う(試しに一部だけ抜いて動かす、という使い方のため)。
    くり返しや分岐を無効にすると、その中身ごと消える。
    """
    return isinstance(node, dict) and bool(node.get("off"))


def _live(nodes):
    """無効なものを除いたブロックの並び。"""
    return [n for n in (nodes or []) if not _is_off(n)]


def _node_to_stmt(node, file: str, path: str, project: _Project) -> Stmt:
    if not isinstance(node, dict) or "type" not in node:
        raise _err(file, path, "ノードは {'type': ...} のオブジェクトです")
    t = node["type"]
    if t not in _NODE_KEYS:
        raise _err(file, path, f"未知のノード種別: {t}")
    # note は覚え書き。変換には一切使わないが、どのノードにも付けられる
    extra = set(node.keys()) - _NODE_KEYS[t] - {"type", "allow", "off", "note"}
    if extra:
        raise _err(file, path, f"未知のキー: {sorted(extra)}(誤記の検出を優先しエラー)")
    allow = frozenset(node.get("allow", []))
    line = project.next_ordinal()

    if t == "label":
        text = node.get("text")
        if not isinstance(text, str) or not text:
            raise _err(file, path, "text が空です")
        return Stmt("label", line, (text,), allow=allow)
    if t == "stick":
        side = node.get("side")
        if side not in ("L", "R"):
            raise _err(file, path, f"side は L か R: {side!r}")
        x = _parse_int(node.get("x"), file, path, "x", STICK_MIN, STICK_MAX)
        y = _parse_int(node.get("y"), file, path, "y", STICK_MIN, STICK_MAX)
        # frames > 0: その長さだけ倒して自動で中央へ戻す(押して離す と同じ形)。
        # 0 または省略: 時間を消費せず、次に変えるまで倒したまま(従来どおり)
        frames = _parse_int(node.get("frames", 0), file, path, "frames",
                            0, 10**9)
        return Stmt("stick", line, (side, x, y, frames), allow=allow)
    if t == "press":
        mask = _parse_buttons(node.get("buttons"), file, path)
        n = _parse_int(node.get("frames"), file, path, "frames", 1, 10**9)
        return Stmt("press", line, (mask, n), allow=allow)
    if t in ("hold", "release"):
        mask = _parse_buttons(node.get("buttons"), file, path)
        return Stmt(t, line, (mask,), allow=allow)
    if t == "wait":
        n = _parse_int(node.get("frames"), file, path, "frames", 1, 10**9)
        return Stmt("wait", line, (n,), allow=allow)
    if t == "loop":
        n = _parse_int(node.get("count"), file, path, "count", 1, 1_000_000)
        body = node.get("body")
        if not isinstance(body, list):
            raise _err(file, path, "body はノードの配列です")
        st = Stmt("loop", line, (n,), body=[
            _node_to_stmt(ch, file, f"{path}.body[{i}]", project)
            for i, ch in enumerate(body) if not _is_off(ch)
        ], allow=allow)
        return st
    if t == "gyro":
        g = [_parse_int(node.get(k, 0), file, path, k, -32768, 32767)
             for k in ("gp", "gy", "gr")]
        # frames > 0: その長さだけ回して自動で止まる(押して離す と同じ形)。
        # 0 または省略: 時間を消費せず、次に変えるまで続く(従来どおり)
        frames = _parse_int(node.get("frames", 0), file, path, "frames",
                            0, 10**9)
        # sway > 0: ゆらぎ(Switch 側のゼロ点自動較正よけの糖衣)。
        # 素の値を sway_interval フレーム続けるたびに、+sway と −sway を
        # sway_period フレームずつ対で入れる(間欠方式)。interval = 0 は
        # 常時 ± 交互。どの形でも合計の回転量は 値×frames に厳密に一致する
        sway = _parse_int(node.get("sway", 0), file, path, "sway", 0, 32767)
        period = _parse_int(node.get("sway_period", 2), file, path,
                            "sway_period", 1, 600)
        interval = _parse_int(node.get("sway_interval", 60), file, path,
                              "sway_interval", 0, 100_000)
        return Stmt("gyro", line, (*g, frames, sway, period, interval),
                    allow=allow)
    if t == "part":
        ref = node.get("ref")
        part = project.load_part(ref, file, path)
        return Stmt("part", line, (ref, part.columns, part.rows), allow=allow)
    if t == "call":
        ref = node.get("ref")
        if not isinstance(ref, str) or not ref:
            raise _err(file, path,
                       "呼ぶ相手が選ばれていません"
                       "(このブロックを選んで一覧から指定してください)")
        project.load_flow(ref)  # 参照先を procs へ読み込む(循環は dsl 側で検出)
        return Stmt("call", line, (ref,), allow=allow)
    if t == "counter_branch":
        arms = node.get("arms")
        if not isinstance(arms, list) or len(arms) < 2:
            raise _err(file, path, "arms は2つ以上の腕(ノード配列)の配列です")
        parsed = []
        for i, arm in enumerate(arms):
            if not isinstance(arm, list):
                raise _err(file, f"{path}.arms[{i}]", "腕はノードの配列です")
            parsed.append([
                _node_to_stmt(ch, file, f"{path}.arms[{i}][{j}]", project)
                for j, ch in enumerate(arm) if not _is_off(ch)
            ])
        return Stmt("counter_branch", line, (parsed,), allow=allow)
    if t == "wait_branch":
        arms = node.get("arms")
        if not isinstance(arms, dict) or not arms:
            raise _err(file, path,
                       'arms は {"腕の名前": [ノード…]} のオブジェクトです')
        names, bodies = [], []
        for label, arm in arms.items():
            if not isinstance(arm, list):
                raise _err(file, f"{path}.arms[{label}]", "腕はノードの配列です")
            names.append(str(label))
            bodies.append([
                _node_to_stmt(ch, file, f"{path}.arms[{label}][{j}]", project)
                for j, ch in enumerate(arm) if not _is_off(ch)
            ])
        timeout = _parse_int(node.get("timeout_frames", 0), file, path,
                             "timeout_frames", 0, 10**9)
        on_timeout = _parse_int(node.get("on_timeout", 0), file, path,
                                "on_timeout", 0, len(names))
        return Stmt("wait_branch", line, (bodies, timeout, on_timeout, names),
                    allow=allow)
    raise AssertionError(t)


class _Project:
    def __init__(self, root: Path):
        self.root = root
        self.procs: dict[str, Proc] = {}
        self._loading: set[str] = set()
        self._ordinal = 0

    def next_ordinal(self) -> int:
        self._ordinal += 1
        return self._ordinal

    def load_part(self, ref, file, path):
        if not isinstance(ref, str) or not ref:
            raise _err(file, path,
                       "呼ぶ相手が選ばれていません"
                       "(このブロックを選んで一覧から指定してください)")
        p = self.root / "parts" / f"{ref}.csv"
        if not p.is_file():
            raise _err(file, path,
                       f"部品「{ref}」がありません(削除された可能性があります)")
        try:
            return load_part(ref, p.read_text(encoding="utf-8"))
        except PartError as e:
            raise _err(f"parts/{ref}.csv", "", str(e)) from None

    def load_flow(self, name: str) -> None:
        if name in self.procs:
            return
        if name in self._loading:
            return  # 循環は compile_procs の検証で行番号つきで報告される
        p = self.root / "procedures" / f"{name}.flow.json"
        if not p.is_file():
            raise FlowError(f"procedures/{name}.flow.json", "",
                            "ファイルが見つかりません")
        file = f"procedures/{name}.flow.json"
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise FlowError(file, "",
                            f"ファイルが壊れています"
                            f"({e.lineno}行目{e.colno}文字目から読めません)"
                            ) from None
        except UnicodeDecodeError:
            raise FlowError(file, "",
                            "文字コードが UTF-8 ではありません") from None
        if not isinstance(doc, dict):
            raise _err(file, "", "トップレベルはオブジェクトです")
        if doc.get("schema") != FLOW_SCHEMA:
            raise _err(file, "schema", f"未対応の schema: {doc.get('schema')!r}")
        if doc.get("name") != name:
            raise _err(file, "name",
                       f"name とファイル名が一致しません: {doc.get('name')!r}")
        body = doc.get("body")
        if not isinstance(body, list):
            raise _err(file, "body", "body はノードの配列です")
        pre = doc.get("pre", "")
        extra = set(doc.keys()) - {"schema", "name", "pre", "body"}
        if extra:
            raise _err(file, "", f"未知のキー: {sorted(extra)}")

        self._loading.add(name)
        proc = Proc(name, self.next_ordinal(), pre if isinstance(pre, str) else "")
        self.procs[name] = proc  # 先に登録(自己再帰 call も検出対象にする)
        # 無効にされたブロックは丸ごと無かったものとして扱う
        proc.body.extend(
            _node_to_stmt(ch, file, f"body[{i}]", self)
            for i, ch in enumerate(body) if not _is_off(ch)
        )
        self._loading.discard(name)


def compile_flow(project_dir: str | Path, name: str) -> Compiled:
    """プロジェクトフォルダの procedures/<name>.flow.json をコンパイルする。"""
    project = _Project(Path(project_dir))
    project.load_flow(name)
    try:
        return compile_procs(project.procs, name)
    except CompileError as e:
        raise FlowError(f"procedures/{name}.flow.json",
                        f"ノード#{e.line}", e.msg) from None
