"""タイムラインの図のデータ。装置へ送るバイト列から、ボタンと軸の横棒を作る。

実行・監視のレーンと編集画面の図が使う。図は手順を参照実装(engine.py)で
たどって作るので、装置が実際に入力するものと一致する。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from . import binfmt, engine
from .frametable import AXES

# 図に並べるボタンの順
BUTTON_ORDER = ["A", "B", "X", "Y", "L", "R", "ZL", "ZR",
                "DU", "DD", "DL", "DR", "PLUS", "MINUS", "HOME", "CAPTURE",
                "LS", "RS"]


def picks_for(awaits: list, chosen: dict) -> dict:
    """選択待ちごとに、通る飛び先の番号を決める。

    利用者が押すのは編集画面に並んでいる選択肢だが、装置に入るのはその回に
    「対象の回」が当てはまる選択肢だけである。2 周目に 1 本減れば
    番号がずれるので、awaits の arm_indices で調べ直す。押した選択肢がその回に
    なければ先頭を通る。その回にはないのだから、描ける経路がほかにない。
    """
    picks = {}
    for a in awaits:
        want = chosen.get(a.get("block", ""), 0)
        arms = a["arm_indices"]
        picks[a["index"]] = arms.index(want) if want in arms else 0
    return picks


@dataclass(frozen=True)
class ByTurn:
    """選択肢が 2 つ以上の選択待ちの、くり返しの回ごとの選択(engine.run の picks の値)。

    arms は ((その選択待ちを囲むくり返しの回の順番, 飛び先の番号), ...) で、順番は
    pass_ordinal の数え方である。並びにない回は、まだ選んでいない(engine.OPEN)。通過の
    回数ではなく回の順番で探すのは、選択肢の中の選択待ちが、外の選択肢を選んだ回に
    しか通らないからである。refs は選択待ちを囲む対応表(loop_map)の項目である。
    対応表は変換結果ごとに決まるので、比べるときには見ない。
    """
    refs: tuple
    arms: tuple
    loop_map: list = field(compare=False, hash=False, repr=False)

    def __call__(self, counters: dict) -> int:
        turns = turns_at(self.loop_map, self.refs, counters)
        at = (None if turns is None
              else pass_ordinal(self.loop_map, list(self.refs), turns))
        return dict(self.arms).get(at, engine.OPEN)


def lane_picks(view, chosen: dict) -> dict:
    """レーンの図の経路(engine.run の picks)。決まっている所だけを通る。

    view は図の変換結果(awaits、marks、loop_map を持つ)、chosen は選んだ選択肢で、
    {(選択待ちのレコード番号, 囲むくり返しの回の順番): 編集画面に並んでいる選択肢の
    番号} である。選択肢が 2 つ以上の選択待ちは、選んだ回はその選択肢を通り、ほかの回は
    どの選択肢にも入らない。選んだ選択肢がその回の選択待ちになければ、選んでいない
    ものとする。道が 1 本の待ち(合流点、スクリプト、選択肢が 1 つの分岐)は、そのまま
    通る。
    """
    refs = {m["index"]: tuple(m.get("loop_refs") or []) for m in view.marks or []
            if m.get("kind") in ("branch", "join", "script")}
    picks = {}
    for a in view.awaits or []:
        arms = list(a.get("arm_indices") or [])
        if len(arms) < 2:
            picks[a["index"]] = 0
            continue
        got = sorted((at, arms.index(arm)) for (index, at), arm in chosen.items()
                     if index == a["index"] and arm in arms)
        picks[a["index"]] = ByTurn(refs.get(a["index"], ()), tuple(got),
                                   view.loop_map or [])
    return picks


def turns_at(loop_map: list, refs, counters) -> list[int] | None:
    """対応表の項目 refs のくり返しのそれぞれが、カウンターの値が counters のとき
    何回目かを出す(外側から)。回番号 = first + Σ (total - いまの値) × step
    (dsl.Compiled.loop_map の式)。counters は {番号: 値} か、番号の順の並びである。
    項目が見つからなければ None。
    """
    if not isinstance(counters, dict):
        counters = dict(enumerate(counters or []))
    out = []
    for ref in refs:
        if not (isinstance(ref, int) and 0 <= ref < len(loop_map or [])):
            return None
        item = loop_map[ref]
        n = item["first"]
        for c in item.get("counters") or []:
            n += (c["total"] - counters.get(c["counter"], 0)) * c["step"]
        out.append(n)
    return out


def rows_of(*event_lists) -> list[str]:
    """レコードの並びのどこかで使う入力の名前(図の行の名前。並びは図と同じ)。
    ボタンは押す STATE があるもの、軸は静止の値と違う STATE があるもの。"""
    states = [ev for events in event_lists for ev in events
              if isinstance(ev, binfmt.State)]
    names = [b for b in BUTTON_ORDER
             if any(ev.buttons & (1 << binfmt.BUTTONS[b]) for ev in states)]
    names += [label for label, attr, rest in AXES
              if any(getattr(ev, attr) != rest for ev in states)]
    return names


def rows_of_builds(*results) -> list[str]:
    """変換の結果の並び(None は飛ばす)のレコードが使う入力の名前(rows_of)。"""
    return rows_of(*(binfmt.decode(r.blob)[0] for r in results if r is not None))


def merge_rows(*rows) -> list[str]:
    """図の行の名前の並びを 1 つに合わせる(並びは図と同じ)。"""
    have = {n for names in rows for n in names or []}
    return [n for n in [*BUTTON_ORDER, *(label for label, _attr, _rest in AXES)]
            if n in have]


def _one_lap(events: list[binfmt.Event]) -> list[binfmt.Event]:
    """無限のくり返しを 1 回に読み替えた、1 周ぶんのイベント列を作る。

    総量なしの手順をそのまま実行すると終わらない(参照実装は歩数の上限まで
    たどってから停止する)。総量のある手順でも、選択待ちの選択肢の中に無限の
    くり返しがあれば、その選択肢を通る経路は終わらない。タイムラインに必要なのは
    1 周ぶんなので、SETCNT の無限を 1 に置き換えて 1 周で終わらせる。総量なしの
    手順の 1 周の長さは、最後の出力の時刻である(build)。変換器は、手順の終わりの
    時刻(無限のくり返しを 1 回として数えた長さ)に STATE がなければ、終端の
    直前に置く(dsl.compile_procs)ので、1 周の終わりに出力がある。
    """
    return [
        binfmt.SetCnt(ev.counter, 1)
        if isinstance(ev, binfmt.SetCnt) and ev.value == binfmt.INFINITE_COUNT
        else ev
        for ev in events]


# 図の横の解像度。1 区間の長さや区間どうしの隙間がこれより細かいものは、
# 画面では 1 画素に満たず区別できない。値の変化ごとに区間を作ると、
# スティックを毎フレーム動かす手順で 1 レーン 12 万の要素になり、画面が固まる
_RESOLUTION = 1000


def _coalesce(spans: list, total: int, axis: bool) -> list:
    """区別できないほど細かい区間を、隣とつないで 1 つにする。

    短い区間(全体の 1/解像度 未満)が、隙間も短く並んでいるときだけつなぐ。
    長い区間どうしは、隣り合っていても値が違えば別のまま残す(マウスポインターを
    合わせたときの値が読める)。つないだ区間の値は「最小〜最大」の文字にする。
    """
    if total <= 0 or len(spans) < 2:
        return spans
    tol = total / _RESOLUTION
    out = [list(spans[0])]
    lo = hi = spans[0][2] if axis else None
    for s in spans[1:]:
        cur = out[-1]
        gap = s[0] - cur[1]
        tiny = (cur[1] - cur[0] < tol) or (s[1] - s[0] < tol)
        if gap <= tol and tiny:
            cur[1] = s[1]
            if axis:
                lo, hi = min(lo, s[2]), max(hi, s[2])
                cur[2] = lo if lo == hi else f"{lo}〜{hi}"
            continue
        out.append(list(s))
        if axis:
            lo = hi = s[2]
    return out


def build(blob: bytes, picks: dict | None = None, rows=None) -> dict:
    """変換した手順(装置へ送るバイト列)から、タイムラインの図のデータを作る。

    picks を渡すと、選択待ちで止まらずに指定の選択肢へ進む(編集画面とレーンの
    図が、1 周の終わりまでの経路を描くのに使う)。通った選択待ちは cuts に入れる。
    時間軸をそこで切るためである。待っている間は時間が進まないので、
    切らずに描くと、待ちの前と後ろがつながって見えてしまう。まだ選んでいない選択待ち
    (engine.OPEN)の cuts の項目は open を持つ。

    rows(入力の名前の並び)を渡すと、描いた経路で使わない入力も、横棒のない行として
    出す。レーンの図は、選ぶたびと周が替わるたびに描き直すので、その実行で通りうる
    経路が使う入力の全部を渡して、行の数を変えない。

    passes と pass_counters は、区切りを図の上に置くための値である(mark_time)。
    画面へは渡さない。
    """
    events, total = binfmt.decode(blob)
    cuts: list = []
    passes: dict = {}
    pass_counters: dict = {}
    # 無限のくり返しは 1 回に読み替えて描く。選択待ちの選択肢の中の無限は総量の
    # ある手順にもあり、図が通る選択肢にあれば、そのままでは描き終わらない
    ems = engine.run(_one_lap(events), picks=picks, awaits_out=cuts,
                     passes_out=passes, counters_out=pass_counters)
    if not total:
        # 総量なしの手順(選択待ちの選択肢の外に無限のくり返しがあるもの、正準の
        # 長さが 0 のもの)は 1 周ぶんだけ描き、その長さを横棒の右端にする
        total = ems[-1].frame if ems else 0
    if picks is not None and ems:
        # 経路を選んで描くときは、その経路が終わる時刻が図の右端である。
        # 手順に書いてある総量は 1 つの経路のもので、選択肢の中身の長さが
        # 違えば通る経路ごとに変わる(合流で時刻を揃えるため)
        total = max(total, ems[-1].frame)
    tracks = []

    for bname in BUTTON_ORDER:
        bit = 1 << binfmt.BUTTONS[bname]
        spans, start = [], None
        for e in ems:
            on = bool(e.buttons & bit)
            if on and start is None:
                start = e.frame
            elif not on and start is not None:
                spans.append([start, e.frame])
                start = None
        if start is not None:
            spans.append([start, total])
        if spans:
            tracks.append({"name": bname, "kind": "button",
                           "spans": _coalesce(spans, total, False)})

    # 軸の横棒は、空欄の値(静止の値)と違う区間だけを「動いている」として描く。
    # 加速度は静止でも重力ぶん(AZ=4096)が出続けるため、0 ではなく静止の値が基準
    for label, attr, rest in AXES:
        spans, start, val = [], None, rest
        for e in ems:
            v = getattr(e, attr)
            if v != val:
                if start is not None:
                    spans.append([start, e.frame, val])
                start = e.frame if v != rest else None
                val = v
        if start is not None and val != rest:
            spans.append([start, total, val])
        if spans:
            tracks.append({"name": label, "kind": "axis",
                           "spans": _coalesce(spans, total, True)})

    # 手順のどこかの経路に入力があるか。描いた経路に入力がない図で、手順そのものに
    # 入力がないのか、描いた経路にないだけなのかを区別するのに使う。選択肢の中身は
    # どれもレコードに並ぶので、通らなかった経路と呼び出し先の中も数える
    rest = {attr: value for _label, attr, value in AXES}
    any_input = any(
        isinstance(ev, binfmt.State)
        and (ev.buttons or any(getattr(ev, a) != v for a, v in rest.items()))
        for ev in events)
    if rows:
        have = {t["name"]: t for t in tracks}
        axes = [label for label, _attr, _rest in AXES]
        tracks = [have.get(n) or {"name": n, "spans": [],
                                  "kind": "axis" if n in axes else "button"}
                  for n in [*BUTTON_ORDER, *axes] if n in have or n in rows]
    return {"total_frames": total, "tracks": tracks, "cuts": cuts,
            "any_input": any_input, "passes": passes,
            "pass_counters": pass_counters}


def pass_ordinal(loop_map: list, refs: list, turns: list) -> int | None:
    """区切りのレコードを、図の上で何度目に通るか(0 から)。

    refs はその区切りを囲む対応表(loop_map)の項目、turns は囲むくり返しの
    それぞれが何回目か(外側から)である。図は周回数のくり返しを 1 周だけ描くので、
    周回数のくり返しは数えない。くり返しの中のレコードは、外側の回ごとに内側の
    回の数だけ通るので、カウンターの回数を桁にした数になる。その項目が担当しない
    回番号なら None を返す(「対象の回」で分けた別の区間のレコードである)。終わりの
    ないくり返しは、図では 1 回だけ通る。
    """
    if len(refs) != len(turns):
        return None
    ordinal = 0
    for ref, turn in zip(refs, turns, strict=True):
        if not (isinstance(ref, int) and 0 <= ref < len(loop_map or [])):
            return None
        item = loop_map[ref]
        if item.get("is_laps"):
            continue
        rem = turn - item["first"]
        if rem < 0:
            return None
        for c in sorted(item.get("counters") or [], key=lambda c: -c["step"]):
            used = rem // c["step"]
            # 終わりのないくり返しの回数は INFINITE_COUNT で入る。図では 1 回だけ
            # 通るので、何回目でも桁に数えない
            endless = c["total"] == binfmt.INFINITE_COUNT
            total = 1 if endless else c["total"]
            if not endless and used >= total:
                return None
            rem -= used * c["step"]
            ordinal = ordinal * total + min(used, total - 1)
        if rem:
            return None
    return ordinal


def on_drawing(tl: dict, m: dict) -> bool:
    """区切り m を、図(tl。build の結果)が通るか。

    図は選択待ちで多くても 1 つの選択肢だけを通る(編集画面の図は選んだ選択肢、
    レーンの図は今の周に選んだ選択肢。まだ選んでいなければ、どの選択肢も通らない)。
    通らない選択肢の中の区切りは、置く所がないので図には置かない。
    """
    return m.get("index") in tl["passes"]


def mark_time(tl: dict, loop_map: list, m: dict, turns: list) -> int | None:
    """区切り m を、図(tl。build の結果)の上の時刻に置く。

    変換が区切りに付ける時刻は、選択肢の長さを数えない(続きは選択待ちの時刻から
    数える)。図は選択肢を通って描くので、それでは横棒とずれる。図を描くときに
    通った時刻(passes)を使う。くり返しの中は、その回に通った時刻を、通ったときの
    カウンターの値(pass_counters)で特定する。通った順番では特定しない。選択肢の中の
    区切りは、その選択肢を選んだ回にしか通らないので、通った順番は回の順番と
    ずれる。turns は囲むくり返しのそれぞれが何回目かで、置けなければ None。図の
    通らない所(on_drawing)の区切りと、図がその回には通らない区切りも None である。
    図がどの回にも通る区切りの、図が描く回より後の回は、最後に通った時刻に置く。
    """
    refs = list(m.get("loop_refs") or [])
    ordinal = pass_ordinal(loop_map, refs, turns)
    times = tl["passes"].get(m.get("index"))
    if ordinal is None or not times:
        return None
    seen = (tl.get("pass_counters") or {}).get(m.get("index")) or []
    at = []
    for counters in seen:
        got = turns_at(loop_map, refs, counters)
        at.append(None if got is None else pass_ordinal(loop_map, refs, got))
    if ordinal in at:
        return times[at.index(ordinal)]
    if at != list(range(len(times))):
        return None
    return times[min(ordinal, len(times) - 1)]


def first_pass(tl: dict, m: dict) -> int | None:
    """区切り m を、図(tl)が最初に通った時刻。通らなければ None。"""
    times = tl["passes"].get(m.get("index"))
    return times[0] if times else None


# 図のキャッシュ。キーは装置へ送る中身の id と経路の選び。長い手順の図は作るのに
# 数百ミリ秒かかるので、同じものを作り直さない
_CACHE: dict = {}
_CACHE_MAX = 32
_CACHE_LOCK = threading.Lock()


def build_cached(proc_hash: str, blob: bytes, picks: dict | None, rows=None) -> dict:
    key = (proc_hash, tuple(sorted(picks.items(), key=lambda p: p[0])) if picks
           else None, tuple(rows or ()))
    with _CACHE_LOCK:
        got = _CACHE.get(key)
    if got is None:
        got = build(blob, picks, rows)
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[key] = got
    # 呼び出し元は、全体の欄を追加し、区切り(cuts)の 1 つずつにも名前などを
    # 書き込む。キャッシュを書き換えないよう、区切りまでコピーして返す。区切りを
    # キャッシュと共有すると、同じ id で選択肢の名前だけが違う図を描いたとき、前の図の
    # 名前が混ざる
    return {**got, "cuts": [dict(c) for c in got["cuts"]]}
