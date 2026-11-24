"""プリセット(presets/<名前>.json)の読み書き。

プリセットは、レーンごとの手順・周回数・開始位置である。利用者が作るもの(手順・
フレーム表と同じ扱い)なので、プロジェクト直下に置く(concepts.md §11)。レーンは
装置 ID で持つ。名前は表示のためだけのものなので、改名してもプリセットの参照は
切れない(P23)。
"""
from __future__ import annotations

import json
from pathlib import Path

from .files import read_text, remove_file, write_file
from .names import apply_order, name_key, refuse_taken, validate_name
from .procfmt import ProcedureError, is_int, start_points
from .procmodel import LAPS_BAD
from .words import sentences

# プリセット(presets/<名前>.json)の形式のバージョン。レーンごとに
# 手順・周回数・開始位置を持つ。違うバージョンのファイルは読まない
PRESET_FORMAT = 1


def _is_count(v) -> bool:
    """1 以上の整数か。"""
    return is_int(v) and v >= 1


def _is_preset_start(start) -> bool:
    """プリセットのレーンの開始位置が、PresetStore.save の書く形か。先頭は None。"""
    if start is None:
        return True
    return (isinstance(start, dict)
            and set(start) == {"name", "path", "block", "turns"}
            and isinstance(start["name"], str)
            and isinstance(start["block"], str)
            and isinstance(start["path"], list)
            and all(isinstance(s, str) for s in start["path"])
            and isinstance(start["turns"], list)
            and all(_is_count(n) for n in start["turns"]))


def _is_preset_lane(lane) -> bool:
    """プリセットのレーンが、PresetStore.save の書く形か。"""
    return (isinstance(lane, dict)
            and set(lane) == {"id", "name", "proc", "laps", "start"}
            and all(isinstance(lane[k], str) for k in ("id", "name", "proc"))
            and (lane["laps"] is None or _is_count(lane["laps"]))
            and _is_preset_start(lane["start"]))


def preset_problem(doc) -> str | None:
    """プリセットの文書を拒否する文を返す。読めるプリセットなら None を返す。

    読めるのは PresetStore.save が書く形だけで、欄の過不足と型の違いを拒否する。
    バージョンの番号だけを見ると、番号の重なる別の形のファイルを、レーンのない
    プリセットや、周回数が文字列のプリセットとして読んでしまう。名前はファイルの名前そのもので、
    文書の中には持たない(手順と同じ。file-format.md §2)。

    読み込み、一覧、使用している場所、参照の書き換えは、どれもこれで判定する。
    拒否する文は画面の一覧にそのまま出る。
    """
    ok = (isinstance(doc, dict)
          and set(doc) == {"format", "lanes"}
          and is_int(doc["format"]) and doc["format"] == PRESET_FORMAT
          and isinstance(doc["lanes"], list) and len(doc["lanes"]) > 0
          and all(_is_preset_lane(x) for x in doc["lanes"]))
    return None if ok else "プリセットの形式が正しくありません"


class PresetStore:
    """プロジェクトのプリセット。"""

    def __init__(self, project):
        self.project = project
        self.root = project.root

    def names(self) -> list[str]:
        d = self.root / "presets"
        if not d.is_dir():
            return []
        return apply_order(self.root, [p.stem for p in d.glob("*.json")], "presets")

    def _path(self, name: str) -> Path:
        return self.root / "presets" / f"{validate_name(name)}.json"

    def _preset_starts(self, proc: str) -> list[dict]:
        """手順のラベルの位置(開始位置に選べるもの)。変換できなければ空。"""
        r, _err = self.project.build_safe(proc)
        return list(r.resume_points) if r is not None else []

    @staticmethod
    def _same_start(a: dict, b: dict) -> bool:
        """開始位置が同じ位置を指しているか。

        名前ではなく、呼び出しの経路とブロックの識別子で見る。同じ名前の
        ラベルは呼び出し階層の中に複数あり得る。
        """
        return (list(a.get("path") or []) == list(b.get("path") or [])
                and str(a.get("block", "")) == str(b.get("block", "")))

    def save(self, name: str, data: dict, *,
                    overwrite: bool = True) -> None:
        """プリセット(グループのひな形)を書く。overwrite が偽で同じ名前が
        あれば ValueError(新しく作るつもりで、あるものを黙って消さない)。

        持ち物は、レーンごとの手順・周回数・開始位置である。名前はファイルの
        名前そのもので、中には書かない。
        周回数は合計で、無限は None である(0 は使わない)。開始位置は
        変換器が返す開始位置の項目そのもの({name, path, block, turns})で、
        先頭は None である。

        保存できる開始位置は、先頭と手順のラベルだけである。「停止した位置」は
        前回の実行に結び付いた一度きりの位置なので、次に開始するための型には
        入れられない。
        """
        lanes = []
        for x in data.get("lanes", []):
            proc = str(x.get("proc", ""))
            raw = x.get("laps")
            if raw in (None, ""):
                laps = None
            else:
                try:
                    laps = int(raw)
                except (TypeError, ValueError):
                    raise ValueError(LAPS_BAD) from None
                if laps < 1:
                    raise ValueError(LAPS_BAD)
            start = x.get("start") or None
            if start is not None:
                if not any(self._same_start(start, p)
                           for p in self._preset_starts(proc)):
                    # 停止した位置は、回番号がどのくり返しの回か(loops)か、周回数の
                    # くり返しの直後(after)を持つ(positions.stopped_at)。持たない
                    # のは一覧から選んだラベルで、見つからないのは、プリセットを呼び
                    # 出した後に手順からそのラベルを消したときである
                    if "loops" in start or start.get("after"):
                        raise ValueError("「停止した位置」はプリセットに保存できません")
                    raise ValueError(sentences(
                        f"開始位置「{start.get('name', '')}」が手順「{proc}」に"
                        "見つかりません",
                        "開始位置を選び直してください"))
                start = {"name": str(start.get("name", "")),
                         "path": list(start.get("path") or []),
                         "block": str(start.get("block", "")),
                         "turns": list(start.get("turns") or [])}
            lanes.append({
                "id": str(x.get("id", "")),
                # 名前は、装置 ID で見つけられないときの手がかりである。割り当てを
                # 解除した装置で保存すると ID が空になり、基板を交換すると
                # ID の装置がいなくなる。どちらも同じ名前のレーンへ当てる
                "name": str(x.get("name", "")),
                "proc": proc,
                "laps": laps,
                "start": start,
            })
        # レーンのないプリセットは、呼び出してもチェックを外すだけで何もしない
        if not lanes:
            raise ValueError("保存するレーンが選択されていません")
        payload = {"format": PRESET_FORMAT, "lanes": lanes}
        if not overwrite:
            refuse_taken(self.names(), validate_name(name), path=self._path(name))
        d = self.root / "presets"
        d.mkdir(exist_ok=True)
        write_file(self._path(name),
                   json.dumps(payload, ensure_ascii=False, indent=2))

    def load(self, name: str) -> dict:
        """プリセットをそのまま読む(毎秒の一覧が使う)。形は preset_problem で
        確認する。

        読めない理由は、そのまま画面に出す文にして ValueError で返す(json と
        OS の例外の文は利用者に読ませない)。出す例外は ValueError だけである。
        """
        path = self._path(name)
        try:
            data = json.loads(read_text(path))
        # 入れ子が深すぎて JSON の読み込みが再帰の上限に当たるのも、壊れた
        # ファイルとして扱う(一覧の行の理由になり、毎秒の状態の取得を妨げない)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise ValueError("プリセットのファイルが壊れています") from None
        except FileNotFoundError:
            raise ValueError(f"プリセット「{name}」が見つかりません") from None
        except OSError:
            raise ValueError("プリセットのファイルを読み込めません") from None
        problem = preset_problem(data)
        if problem:
            raise ValueError(problem)
        return data

    def delete(self, name: str) -> None:
        remove_file(self._path(name))
        self.project.update_org_refs(name, None, "presets")

    def rename(self, old: str, new: str) -> None:
        """プリセットの名前を変える。

        名前はファイルの名前そのものなので、中身は読まずにそのままコピーする。読めない
        プリセットも名前を変えられ、中身は変わらない。
        """
        old, new = validate_name(old), validate_name(new)
        if old == new:
            return
        if not self._path(old).is_file():
            raise ValueError(f"プリセット「{old}」が見つかりません")
        refuse_taken(self.names(), new, old, self._path(new))
        write_file(self._path(new), self._path(old).read_bytes())
        remove_file(self._path(old))
        self.project.update_org_refs(old, new, "presets")

    def _preset_lanes(self):
        """プリセットのレーンを (プリセットの名前, レーンの名前, レーン) で返す。

        レーンは装置 ID で持つので、名前は今の登録情報から探す(改名に追従する)。
        """
        names = {d.get("id"): d.get("name")
                 for d in self.project.load_config().get("devices", []) if d.get("id")}
        for fname in self.names():
            try:
                data = self.load(fname)
            except ValueError:
                continue
            for x in data["lanes"]:
                yield fname, str(names.get(x["id"]) or x["name"] or x["id"]), x

    def _start_finder(self, docs: dict | None = None):
        """手順の名前から、その開始位置の一覧を調べる関数(調べた一覧は覚える)。

        docs は保存する前の中身で、ここにある手順はファイルではなくこれを読む。
        docs の名前は同一性のキー(name_key)で照合し、探す綴りに合わせて渡す。
        レーンが綴りの違う名前で手順を指していても(Windows ではファイルとして同じ)、
        保存する前の中身で照合する。読めない手順の一覧は空である(どの開始位置も
        見つからない)。
        """
        memo: dict[str, list] = {}

        def points(proc: str) -> list:
            if proc not in memo:
                mine = {(proc if name_key(k) == name_key(proc) else k): v
                        for k, v in (docs or {}).items()}
                try:
                    memo[proc] = start_points(self.root, proc, mine or None)
                except (ProcedureError, ValueError, OSError):
                    memo[proc] = []
            return memo[proc]
        return points

    def _preset_start(self, lane: dict, points) -> tuple[str, bool] | None:
        """プリセットのレーンの開始位置を、手順と照合する。

        (今の名前, 見つかるか) を返す。先頭から始めるレーンは None である。
        照合は実行の画面と同じく、呼び出しの経路とブロックの識別子で行う
        (_same_start)。見つかれば名前は手順の今のラベルの名前で、ラベルの名前を
        変えた後でも今の名前で示す。
        """
        start = lane["start"]
        if start is None:
            return None
        hit = next((p for p in points(lane["proc"])
                    if self._same_start(start, p)), None)
        if hit is None:
            # 要求で受け取ったレーンの開始位置は、名前を持たないことがある(gui の
            # _lane_starts は名前を求めない。あれば文字列であることだけを確かめる)
            return start.get("name", ""), False
        return str(hit.get("name", "")), True

    def lanes(self, proc: str) -> list[dict]:
        """手順を直接使っているプリセットのレーン。

        start は開始位置のラベルの名前(先頭から始めるなら None)、lost は
        その開始位置が今の手順に見つからないか(レーンの開始位置の欄が
        「名前(見つかりません)」になる)。
        """
        points = self._start_finder()
        out = []
        for fname, lane, x in self._preset_lanes():
            # 名前は同一性のキーで比べる(改名の follow_rename と同じ)。手で編集した
            # "retry" が手順「Retry」を指すレーンも、削除の確認に出す
            if name_key(x["proc"]) != name_key(proc):
                continue
            at = self._preset_start(x, points)
            out.append({"preset": fname, "lane": lane,
                        "start": at[0] if at else None,
                        "lost": at is not None and not at[1]})
        return out

    def lost_starts(self, name: str, doc: dict,
                    lanes: list[dict] = ()) -> list[dict]:
        """手順 name を doc の中身で保存すると見つからなくなる、プリセットとレーンの
        開始位置。

        開始位置は、呼び出しの経路とラベルの識別子で指す(file-format.md §6-2)。
        ラベルを消したときのほか、ラベルや途中の呼び出しを無効にしたときと、
        途中の呼び出しの手順を替えたときにも見つからなくなる。保存した後の
        一覧を実際に作って照合するので、どの形でも同じに分かる。いまも
        見つからない開始位置は数えない(保存のたびに聞かない)。label は
        開始位置の今の名前である。
        lanes は実行・監視のレーンが選んでいる開始位置 {lane, proc, start} の並びで
        ある(レーンの開始位置は画面だけが持つ)。プリセットと同じく照合し、
        見つからなくなるものを preset のない項目で返す。
        """
        return self.lost_starts_of({name: doc}, lanes)

    def lost_starts_of(self, docs: dict, lanes: list[dict] = ()) -> list[dict]:
        """手順を docs({名前: 中身})のとおりに書くと見つからなくなる、プリセットと
        レーンの開始位置(lost_starts)。

        インポートは何件も一度に書く。置き換える手順が、一緒にインポートする
        新しい手順を呼ぶこともあるので、書く手順をまとめて渡す。
        """
        now = self._start_finder()
        after = self._start_finder(docs)
        out = []
        for fname, lane, x in self._preset_lanes():
            at = self._preset_start(x, now)
            if at is None or not at[1]:
                continue
            if not self._preset_start(x, after)[1]:
                out.append({"preset": fname, "lane": lane, "label": at[0]})
        for x in lanes:
            at = self._preset_start(x, now)
            if at is None or not at[1]:
                continue
            if not self._preset_start(x, after)[1]:
                out.append({"lane": x["lane"], "label": at[0]})
        return out

    def follow_rename(self, old: str, new: str) -> int:
        """プリセットが指す手順名を付け替え、書き替えたプリセットの数を返す(画面は
        使用している手順と同じく件数を示す)。

        付け替えないと、呼び出したレーンの手順の欄が「名前(見つかりません)」に
        なり、改名しただけなのに開始できなくなる。
        読めないプリセットは変えない。形の分からない中身を書き換えると、
        利用者の知らないうちにファイルが変わる。
        """
        changed = 0
        for fname in self.names():
            try:
                data = self.load(fname)
            except ValueError:
                continue
            hit = False
            for x in data["lanes"]:
                if name_key(x["proc"]) == name_key(old):
                    x["proc"] = new
                    hit = True
            if hit:
                write_file(self._path(fname),
                           json.dumps(data, ensure_ascii=False, indent=2))
                changed += 1
        return changed
